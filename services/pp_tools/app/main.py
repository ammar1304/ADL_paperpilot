"""pp-tools: deterministic helpers for the PaperPilot n8n pipeline.

Milestone 0: health checks and run-state management.
Milestone 1: corpus build jobs, the knowledge-base search API and corpus stats.
Later milestones add the verifier and ID helpers here, so that n8n, the tests
and the evaluation harness all run the same code.
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import Settings, get_settings
from .corpus.arxiv import ArxivClient
from .corpus.build import CorpusBuilder
from .corpus.manifest import ManifestError, manifest_hash, read_manifest
from .db import Database, JobConflict, RunConflict
from .logging_setup import setup_logging
from .models import (
    BuildRequest,
    CreateRunBody,
    CreateRunResponse,
    FailByExecutionBody,
    FieldError,
    JobAccepted,
    JobRecord,
    RunRecord,
    RunRequest,
    SearchHit,
    SearchRequest,
    SearchResponse,
    UpdateRunBody,
    ValidationErrorResponse,
)
from .retrieval.embedder import Embedder, EmbedderError
from .retrieval.search import Retriever, hit_dict
from .retrieval.store import Store, collection_name, make_client

log = logging.getLogger("pp_tools")

BUILD_JOB = "corpus_build"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    db = Database(settings.database_url, settings.health_timeout_s)
    store = Store(make_client(settings.qdrant_url, settings.health_timeout_s * 5))
    embedder = Embedder(settings.embedder_url, settings.embed_batch_size, settings.embed_timeout_s)
    retriever = Retriever(embedder, store)
    # One worker: builds run one at a time, and never inside a request.
    jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pp-job")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.open()
        await run_in_threadpool(db.apply_schema_with_retry, settings.startup_db_wait_s)
        if db.schema_ready:
            interrupted = await run_in_threadpool(db.fail_interrupted_jobs)
            if interrupted:
                log.warning("marked interrupted jobs as failed", extra={"count": interrupted})
        yield
        jobs.shutdown(wait=False, cancel_futures=True)
        embedder.close()
        db.close()

    app = FastAPI(title="pp-tools", version="0.2.0", lifespan=lifespan)
    app.state.db = db
    app.state.settings = settings
    app.state.store = store
    app.state.embedder = embedder

    # ---- health -----------------------------------------------------------

    @app.get("/health")
    async def health() -> JSONResponse:
        checks: dict[str, dict[str, Any]] = {}

        try:
            await run_in_threadpool(db.ping)
            checks["postgres"] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            checks["postgres"] = {"ok": False, "error": _short(exc)}

        if settings.qdrant_url.startswith("local:"):
            try:
                await run_in_threadpool(store.client.get_collections)
                checks["qdrant"] = {"ok": True, "mode": "local"}
            except Exception as exc:  # noqa: BLE001
                checks["qdrant"] = {"ok": False, "error": _short(exc)}
        else:
            try:
                async with httpx.AsyncClient(timeout=settings.health_timeout_s) as client:
                    resp = await client.get(f"{settings.qdrant_url.rstrip('/')}/readyz")
                checks["qdrant"] = {"ok": resp.status_code == 200, "http_status": resp.status_code}
            except Exception as exc:  # noqa: BLE001
                checks["qdrant"] = {"ok": False, "error": _short(exc)}

        # Reported but not required: the embedding model can take minutes to
        # download on first start, and run tracking works without it.
        embedder_ok = await run_in_threadpool(embedder.healthy)
        checks["embedder"] = {"ok": embedder_ok, "required": False}

        ok = all(c["ok"] for c in checks.values() if c.get("required", True))
        body = {"status": "ok" if ok else "degraded", "service": "pp-tools", "checks": checks}
        return JSONResponse(status_code=200 if ok else 503, content=body)

    # ---- runs -------------------------------------------------------------

    @app.post(
        "/runs",
        status_code=201,
        response_model=CreateRunResponse,
        responses={422: {"model": ValidationErrorResponse}},
    )
    async def create_run(body: CreateRunBody) -> Any:
        try:
            req = RunRequest.model_validate(body.request)
        except ValidationError as exc:
            errors = [FieldError(field=_loc(e["loc"]), message=e["msg"]) for e in exc.errors()]
            log.info("run rejected", extra={"errors": [e.model_dump() for e in errors]})
            return JSONResponse(status_code=422, content={"errors": [e.model_dump() for e in errors]})

        stored = req.model_dump(mode="json")
        try:
            run_id = await run_in_threadpool(db.create_run, req.mode, stored, body.n8n_execution_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("could not create run")
            raise HTTPException(status_code=503, detail=f"database unavailable: {_short(exc)}") from exc

        log.info("run created", extra={"run_id": str(run_id), "mode": req.mode})
        return CreateRunResponse(run_id=run_id, mode=req.mode, status="received", request=req)

    @app.get("/runs", response_model=list[RunRecord])
    async def list_runs(limit: int = Query(default=20, ge=1, le=100)) -> Any:
        """Most recent runs first. Used by the smoke test and for debugging."""
        return await run_in_threadpool(db.list_runs, limit)

    @app.get("/runs/{run_id}", response_model=RunRecord)
    async def get_run(run_id: UUID) -> Any:
        row = await run_in_threadpool(db.get_run, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return row

    @app.patch("/runs/{run_id}", response_model=RunRecord)
    async def update_run(run_id: UUID, body: UpdateRunBody) -> Any:
        try:
            row = await run_in_threadpool(db.update_run, run_id, body.status, body.result, body.error)
        except RunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        log.info("run updated", extra={"run_id": str(run_id), "status": body.status})
        return row

    @app.post("/runs/fail-by-execution")
    async def fail_by_execution(body: FailByExecutionBody) -> dict[str, Any]:
        failed = await run_in_threadpool(db.fail_by_execution, body.n8n_execution_id, body.error)
        log.warning(
            "runs marked failed by error workflow",
            extra={"n8n_execution_id": body.n8n_execution_id, "run_ids": failed},
        )
        return {"n8n_execution_id": body.n8n_execution_id, "failed_run_ids": failed}

    # ---- corpus build jobs ------------------------------------------------

    @app.post("/corpus/build", status_code=202, response_model=JobAccepted)
    async def start_build(body: BuildRequest) -> Any:
        try:
            rows = await run_in_threadpool(read_manifest, settings.manifest_path)
        except ManifestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        params = {"recreate": body.recreate, "limit": body.limit, "manifest_rows": len(rows)}
        try:
            job_id = await run_in_threadpool(db.create_job, BUILD_JOB, params, body.n8n_execution_id)
        except JobConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        jobs.submit(_run_build, job_id, body)
        log.info("corpus build queued", extra={"job_id": str(job_id), **params})
        return JobAccepted(job_id=job_id, status="queued")

    def _run_build(job_id: UUID, body: BuildRequest) -> None:
        last = [0.0]
        lock = threading.Lock()

        def progress(info: dict[str, Any]) -> None:
            now = time.monotonic()
            with lock:
                if now - last[0] < 1.0:  # at most one database write per second
                    return
                last[0] = now
            db.update_job(job_id, progress=info)

        arxiv = ArxivClient(settings.arxiv_api_url, settings.arxiv_pdf_url, settings.arxiv_delay_s, settings.arxiv_contact)
        try:
            db.update_job(job_id, status="running", progress={"stage": "starting"})
            builder = CorpusBuilder(settings, arxiv, embedder, store, progress)
            report = builder.run(recreate=body.recreate, limit=body.limit)
            result = json.loads(json.dumps(asdict(report), default=str))
            db.update_job(job_id, status="done", progress={"stage": "done"}, result=result)
            log.info("corpus build done", extra={"job_id": str(job_id), "chunks": report.chunks})
        except Exception as exc:  # noqa: BLE001 - every failure must end in a failed job
            log.exception("corpus build failed", extra={"job_id": str(job_id)})
            try:
                db.update_job(job_id, status="failed", error={"message": _short(exc), "type": type(exc).__name__})
            except Exception:  # noqa: BLE001 - database down too: startup cleanup will fail the job
                log.exception("could not record the failed job", extra={"job_id": str(job_id)})
        finally:
            arxiv.close()

    @app.get("/jobs/{job_id}", response_model=JobRecord)
    async def get_job(job_id: UUID) -> Any:
        row = await run_in_threadpool(db.get_job, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        return row

    @app.get("/corpus/stats")
    async def corpus_stats() -> dict[str, Any]:
        """What the knowledge base contains. The novelty report quotes this as its coverage."""
        out: dict[str, Any] = {"manifest": None, "collections": None, "last_build": None}
        try:
            rows = await run_in_threadpool(read_manifest, settings.manifest_path)
            out["manifest"] = {
                "papers": len(rows),
                "fulltext": sum(r.fulltext for r in rows),
                "sha256_12": manifest_hash(settings.manifest_path),
            }
        except ManifestError as exc:
            out["manifest"] = {"error": str(exc)}
        try:
            out["collections"] = await run_in_threadpool(store.stats)
        except Exception as exc:  # noqa: BLE001
            out["collections"] = {"error": _short(exc)}
        report = settings.cache_dir / "build_report.json"
        if report.exists():
            data = json.loads(report.read_text(encoding="utf-8"))
            out["last_build"] = {k: data.get(k) for k in ("finished_at", "manifest_sha256_12", "papers_indexed", "fulltext_indexed", "chunks")}
        return out

    # ---- search -------------------------------------------------------------

    @app.post("/search", response_model=SearchResponse)
    async def search(body: SearchRequest) -> Any:
        if not await run_in_threadpool(store.client.collection_exists, collection_name(body.strategy)):
            raise HTTPException(status_code=409, detail=f"no '{body.strategy}' collection yet; run a corpus build first")
        filters = body.filters.model_dump(exclude_none=True) if body.filters else None
        try:
            if body.level == "paper":
                hits = await run_in_threadpool(retriever.papers, body.query, body.strategy, body.k, None, filters)
            else:
                hits = await run_in_threadpool(retriever.chunks, body.query, body.strategy, body.k, filters)
        except EmbedderError as exc:
            raise HTTPException(status_code=503, detail=f"embedder unavailable: {_short(exc)}") from exc
        return SearchResponse(
            query=body.query,
            strategy=body.strategy,
            level=body.level,
            hits=[SearchHit(**hit_dict(h)) for h in hits],
        )

    return app


def _loc(loc: tuple[Any, ...]) -> str:
    # Model-level validators report an empty location; name it explicitly.
    return ".".join(str(p) for p in loc) or "request"


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:300]


# Run with: uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
