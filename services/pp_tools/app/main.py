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
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .audit.parse_job import parse_paper
from .audit.parser import DoclingClient
from .audit.steps import (
    AuditError,
    audit_result,
    claim_prompts,
    citation_checks,
    claims as audit_claims,
    figure_checks,
    next_citation_batch,
    next_claim_batch,
    next_figure_check,
    number_check,
    record_llm_error,
    save_citation_check,
    save_claims,
    save_figure_check,
)
from .audit.store import PaperStore
from .audit.uploads import UploadError, Uploads
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
    JudgePromptOut,
    LlmOutput,
    RenderedPromptOut,
    RenderRequest,
    PlanApplyBody,
    RankRequest,
    RetrieveRequest,
    JobRecord,
    RunRecord,
    RunRequest,
    SearchHit,
    SearchRequest,
    SearchResponse,
    UpdateRunBody,
    ValidationErrorResponse,
)
from .novelty import (
    NoveltyError,
    RetrievalConfig,
    apply_plan,
    assemble,
    plan_prompt,
    rank_candidates,
    judge_prompt,
    retrieve,
    save_facets,
    verify,
)
from .prompts import PromptError, PromptLibrary
from .report import result_page
from .retrieval.embedder import Embedder, EmbedderError
from .external.openalex import OpenAlex
from .external.semantic_scholar import SemanticScholar
from .external.web import WebSearch
from .retrieval.reranker import Reranker
from .retrieval.search import Retriever, hit_dict
from .retrieval.store import Store, collection_name, make_client

log = logging.getLogger("pp_tools")

BUILD_JOB = "corpus_build"
AUDIT_PARSE_JOB = "audit_parse"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    db = Database(settings.database_url, settings.health_timeout_s)
    store = Store(make_client(settings.qdrant_url, settings.health_timeout_s * 5))
    embedder = Embedder(settings.embedder_url, settings.embed_batch_size, settings.embed_timeout_s)
    retriever = Retriever(embedder, store)
    reranker = Reranker(settings.reranker_url, timeout_s=settings.rerank_timeout_s) if settings.reranker_url else None
    web = _web_search(settings)
    library = PromptLibrary(settings.prompts_dir)
    parser = DoclingClient(settings.parser_url, settings.parser_timeout_s) if settings.parser_url else None
    papers = PaperStore(settings.cache_dir / "audit")
    uploads = Uploads(settings.cache_dir / "uploads", settings.audit_max_pdf_mb * 1_000_000)
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
        if reranker is not None:
            reranker.close()
        if web is not None:
            web.close()
        if parser is not None:
            parser.close()
        db.close()

    app = FastAPI(title="pp-tools", version="0.2.0", lifespan=lifespan)
    @app.middleware("http")
    async def declare_utf8(request, call_next):
        # Without an explicit charset, Windows PowerShell 5.1 (Invoke-RestMethod)
        # decodes JSON as Latin-1 and garbles every non-ASCII character. Seen on
        # the first real run: "per‑question" came back as "perâquestion".
        response = await call_next(request)
        if response.headers.get("content-type") == "application/json":
            response.headers["content-type"] = "application/json; charset=utf-8"
        return response

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
        if reranker is not None:
            checks["reranker"] = {"ok": await run_in_threadpool(reranker.healthy), "required": False}
        else:
            checks["reranker"] = {"ok": False, "required": False, "error": "RERANKER_URL not set; novelty uses fused order"}

        if parser is not None:
            checks["parser"] = {"ok": await run_in_threadpool(parser.healthy), "required": False}
        else:
            checks["parser"] = {"ok": False, "required": False, "error": "PARSER_URL not set; audits cannot parse PDFs"}

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

        if req.draft_pdf_upload is not None and not uploads.exists(req.draft_pdf_upload):
            error = FieldError(field="draft_pdf_upload", message="no uploaded PDF with this id; POST the file to /uploads first")
            return JSONResponse(status_code=422, content={"errors": [error.model_dump()]})

        stored = req.model_dump(mode="json")
        try:
            run_id = await run_in_threadpool(db.create_run, req.mode, stored, body.n8n_execution_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("could not create run")
            raise HTTPException(status_code=503, detail=f"database unavailable: {_short(exc)}") from exc

        log.info("run created", extra={"run_id": str(run_id), "mode": req.mode})
        return CreateRunResponse(run_id=run_id, mode=req.mode, status="received", request=req)

    @app.post("/uploads", status_code=201)
    async def upload_pdf(request: Request) -> Any:
        """Store a PDF sent as the raw request body (the form's file field, D39); returns its id."""
        data = await request.body()
        try:
            upload_id = await run_in_threadpool(uploads.save, data)
        except UploadError as exc:
            status = 413 if "larger than" in str(exc) else 415
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        log.info("pdf uploaded", extra={"upload_id": upload_id[:12], "bytes": len(data)})
        return {"upload_id": upload_id, "bytes": len(data)}

    @app.post("/form/result")
    async def form_result(body: dict[str, Any]) -> Any:
        """The form's last page (D39): {"status_code", "body"} as the router answered -> {"html"}."""
        status = int(_num(body, "status_code") or 0)
        answer = body.get("body")
        run = None
        run_id = answer.get("run_id") if isinstance(answer, dict) else None
        if run_id:
            try:
                run = await run_in_threadpool(db.get_run, UUID(str(run_id)))
            except ValueError:
                run = None
        form_url = body.get("form_url") if isinstance(body.get("form_url"), str) else None
        return {"html": result_page(status, answer, run, form_url)}

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

    def _fulltext_count(stats: dict[str, Any]) -> int:
        """Papers whose full text was indexed, from the last build report.

        Counting papers with section chunks undercounts: a paper whose headings
        were not detected is indexed in full but as plain windows.
        """
        report = settings.cache_dir / "build_report.json"
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
            if data.get("limit") is None and isinstance(data.get("fulltext_indexed"), int):
                return data["fulltext_indexed"]
        except (OSError, ValueError):
            pass
        return stats.get("structured", {}).get("papers_with_sections", 0)

    @app.get("/chunks/{chunk_id:path}")
    async def get_chunk(chunk_id: str, strategy: str = "structured") -> Any:
        """One chunk by id, e.g. arxiv:2310.11511#abs (used by the E2 evaluation to read abstracts)."""
        if strategy not in ("structured", "fixed"):
            raise HTTPException(status_code=422, detail="strategy must be structured or fixed")
        if not await run_in_threadpool(store.client.collection_exists, collection_name(strategy)):
            raise HTTPException(status_code=409, detail=f"no '{strategy}' collection yet; run a corpus build first")
        hits = await run_in_threadpool(store.get, strategy, [chunk_id])
        if not hits:
            raise HTTPException(status_code=404, detail=f"chunk {chunk_id} not found")
        return hit_dict(hits[0])

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

    # ---- prompt library -----------------------------------------------------

    @app.get("/prompts")
    async def list_prompts() -> dict[str, Any]:
        """Active prompt versions, and any problem loading them."""
        try:
            active = library.active_versions()
        except PromptError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"active": active, "problems": library.check_all()}

    @app.post("/prompts/{name}/render", response_model=RenderedPromptOut)
    async def render_prompt(name: str, body: RenderRequest) -> Any:
        try:
            rendered = library.load(name, body.version).render(body.variables)
        except PromptError as exc:
            status = 404 if "not found" in str(exc) or "no active version" in str(exc) else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return RenderedPromptOut(**rendered.__dict__)

    # ---- novelty branch steps (called by n8n 20-novelty) -------------------------

    def _novelty_errors(fn, *args):
        try:
            return fn(*args)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NoveltyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PromptError as exc:
            raise HTTPException(status_code=500, detail=f"prompt library: {exc}") from exc

    @app.post("/novelty/{run_id}/facets")
    async def novelty_facets(run_id: UUID, body: LlmOutput) -> Any:
        try:
            parsed = await run_in_threadpool(_novelty_errors, save_facets, db, run_id, body.output, body.prompt_version or "unknown")
        except ValidationError as exc:
            errors = [FieldError(field=_loc(e["loc"]), message=e["msg"]).model_dump() for e in exc.errors()]
            return JSONResponse(status_code=422, content={"errors": errors})
        return parsed.model_dump()

    @app.post("/novelty/{run_id}/retrieve")
    async def novelty_retrieve(run_id: UUID, body: RetrieveRequest) -> Any:
        strategy = body.strategy or settings.novelty_strategy
        if not await run_in_threadpool(store.client.collection_exists, collection_name(strategy)):
            raise HTTPException(status_code=409, detail=f"no '{strategy}' collection yet; run a corpus build first")
        stats = await run_in_threadpool(store.stats)
        s = stats.get(strategy, {})
        coverage = {
            "papers_in_knowledge_base": s.get("papers", 0),
            "papers_with_full_text": await run_in_threadpool(_fulltext_count, stats),
            "chunks_searched": s.get("chunks", 0),
        }
        cfg = _retrieval_config(strategy)
        try:
            return await run_in_threadpool(_novelty_errors, retrieve, db, retriever, run_id, cfg, coverage, reranker)
        except EmbedderError as exc:
            raise HTTPException(status_code=503, detail=f"embedder unavailable: {_short(exc)}") from exc

    async def _coverage(strategy: str) -> dict[str, Any]:
        stats = await run_in_threadpool(store.stats)
        s = stats.get(strategy, {})
        return {
            "papers_in_knowledge_base": s.get("papers", 0),
            "papers_with_full_text": await run_in_threadpool(_fulltext_count, stats),
            "chunks_searched": s.get("chunks", 0),
        }

    @app.post("/novelty/{run_id}/plan-prompt")
    async def novelty_plan_prompt(run_id: UUID) -> Any:
        """P2 for the next search round, or {"run": false} when the agent should not run."""
        return await run_in_threadpool(_novelty_errors, plan_prompt, db, library, run_id, settings.novelty_max_search_rounds)

    @app.post("/novelty/{run_id}/plan")
    async def novelty_plan(run_id: UUID, body: PlanApplyBody) -> Any:
        """Execute the planner's decision: searches, re-ranking, a new retrieve step."""
        cfg = _retrieval_config(settings.novelty_strategy)
        coverage = await _coverage(cfg.strategy)
        try:
            return await run_in_threadpool(
                _novelty_errors, apply_plan, db, retriever, reranker, web, run_id, body.output, cfg, coverage,
                settings.novelty_max_search_rounds, settings.semantic_scholar_results,
            )
        except EmbedderError as exc:
            raise HTTPException(status_code=503, detail=f"embedder unavailable: {_short(exc)}") from exc

    def _retrieval_config(strategy: str, **overrides: Any) -> RetrievalConfig:
        values = {
            "strategy": strategy,
            "top_papers": settings.novelty_top_papers,
            "pool_papers": settings.novelty_pool_papers,
            "rerank": settings.novelty_rerank,
            "idea_as_query": settings.novelty_idea_query,
            "rerank_abstract": settings.novelty_rerank_abstract,
            "order": settings.novelty_order,
            "agent_extra_papers": settings.novelty_agent_extra_papers,
            "agent_min_relevance": settings.novelty_agent_min_relevance,
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return RetrievalConfig(**values)

    @app.post("/novelty/rank")
    async def novelty_rank(body: RankRequest) -> Any:
        """Rank candidates for an idea and given queries, without a run or an LLM.

        Used by scripts/retrieval_ab.py to compare ranking settings on the queries
        stored from real runs. Returns the whole pool, not only the judge's top papers.
        """
        strategy = body.strategy or settings.novelty_strategy
        if not await run_in_threadpool(store.client.collection_exists, collection_name(strategy)):
            raise HTTPException(status_code=409, detail=f"no '{strategy}' collection yet; run a corpus build first")
        cfg = _retrieval_config(
            strategy,
            rerank=body.rerank,
            idea_as_query=body.idea_as_query,
            pool_papers=body.pool_papers,
            rerank_abstract=body.rerank_abstract,
            order=body.order,
        )
        try:
            ranked = await run_in_threadpool(rank_candidates, retriever, reranker, body.idea_text, body.queries, cfg)
        except EmbedderError as exc:
            raise HTTPException(status_code=503, detail=f"embedder unavailable: {_short(exc)}") from exc
        return {"strategy": strategy, "config": {k: getattr(cfg, k) for k in ("rerank", "idea_as_query", "rerank_abstract", "order", "pool_papers", "top_papers", "per_query_k")}, **ranked}

    @app.post("/novelty/{run_id}/judge-prompt", response_model=JudgePromptOut)
    async def novelty_judge_prompt(run_id: UUID) -> Any:
        rendered, attempt = await run_in_threadpool(_novelty_errors, judge_prompt, db, library, run_id)
        return JudgePromptOut(**rendered.__dict__, attempt=attempt)

    @app.post("/novelty/{run_id}/verify")
    async def novelty_verify(run_id: UUID, body: LlmOutput) -> Any:
        return await run_in_threadpool(_novelty_errors, verify, db, run_id, body.output)

    @app.post("/novelty/{run_id}/result")
    async def novelty_result(run_id: UUID) -> Any:
        return await run_in_threadpool(_novelty_errors, assemble, db, run_id)

    # ---- audit branch (milestone 3) ------------------------------------------

    @app.post("/audit/{run_id}/parse", status_code=202, response_model=JobAccepted)
    async def start_audit_parse(run_id: UUID, n8n_execution_id: str | None = None) -> Any:
        """Download the run's PDF and parse it with Docling in the background; poll GET /jobs/{job_id}."""
        if parser is None:
            raise HTTPException(status_code=503, detail="PARSER_URL is not set")
        run = await run_in_threadpool(db.get_run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        given = run.get("input") or {}
        url, upload = given.get("draft_pdf_url"), given.get("draft_pdf_upload")
        if run.get("mode") != "audit" or not (url or upload):
            raise HTTPException(status_code=409, detail="not an audit run with a draft_pdf_url or draft_pdf_upload")
        source = {"url": url} if url else {"upload": upload}
        try:
            job_id = await run_in_threadpool(db.create_job, AUDIT_PARSE_JOB, {"run_id": str(run_id), **source}, n8n_execution_id)
        except JobConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        jobs.submit(_run_audit_parse, job_id, run_id, url, upload)
        return JobAccepted(job_id=job_id, status="queued")

    def _run_audit_parse(job_id: UUID, run_id: UUID, url: str | None, upload: str | None = None) -> None:
        last = [0.0]

        def progress(info: dict[str, Any]) -> None:
            now = time.monotonic()
            if now - last[0] >= 1.0:
                last[0] = now
                db.update_job(job_id, progress=info)

        try:
            db.update_job(job_id, status="running", progress={"stage": "starting"})
            pdf = uploads.read(upload) if upload else None
            result = parse_paper(url or "draft.pdf", parser, papers, settings.audit_max_pdf_mb * 1_000_000,
                                 settings.audit_allow_private_urls, progress, pdf)
            db.add_step(run_id, "parse", "done", result)
            db.update_job(job_id, status="done", progress={"stage": "done"}, result=result)
            log.info("audit parse done", extra={"job_id": str(job_id), "run_id": str(run_id), "cached": result["cached"]})
        except Exception as exc:  # noqa: BLE001 - every failure must end in a failed job and step
            log.exception("audit parse failed", extra={"job_id": str(job_id)})
            error = {"message": _short(exc), "type": type(exc).__name__}
            try:
                db.add_step(run_id, "parse", "failed", {"error": error})
                db.update_job(job_id, status="failed", error=error)
            except Exception:  # noqa: BLE001
                log.exception("could not record the failed parse", extra={"job_id": str(job_id)})

    def _num(body: dict[str, Any], key: str) -> float | None:
        """A number n8n may send as a string ("1009") or leave empty."""
        try:
            return float(body[key]) if body.get(key) not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _audit_errors(fn, *args):
        try:
            return fn(*args)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuditError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PromptError as exc:
            raise HTTPException(status_code=500, detail=f"prompt library: {exc}") from exc

    @app.post("/audit/{run_id}/claim-prompts")
    async def audit_claim_prompts(run_id: UUID) -> Any:
        """P4 rendered once per batch of candidate sentences (step 3)."""
        return await run_in_threadpool(_audit_errors, claim_prompts, db, library, papers, run_id)

    @app.post("/audit/{run_id}/claims/next")
    async def audit_next_claim_batch(run_id: UUID) -> Any:
        """The next batch for the LLM, or {"done": true}; pp-tools keeps the loop's state (F-012)."""
        return await run_in_threadpool(_audit_errors, next_claim_batch, db, library, papers, run_id)

    @app.post("/audit/{run_id}/claims")
    async def audit_save_claims(run_id: UUID, body: dict[str, Any]) -> Any:
        """Validate and store one batch of P4 output: {"batch": 0, "output": {...}}."""
        if not isinstance(body.get("batch"), int):
            raise HTTPException(status_code=422, detail="batch (integer) is required")
        return await run_in_threadpool(
            _audit_errors, save_claims, db, papers, run_id, body["batch"], body.get("output"), body.get("output_text"), body.get("usage")
        )

    @app.post("/audit/{run_id}/claims/error")
    async def audit_llm_error(run_id: UUID, body: dict[str, Any]) -> Any:
        if not isinstance(body.get("batch"), int):
            raise HTTPException(status_code=422, detail="batch (integer) is required")
        status = _num(body, "status")
        return await run_in_threadpool(
            _audit_errors, record_llm_error, db, run_id, body["batch"], str(body.get("error", "")),
            int(status) if status is not None else None, _num(body, "retry_after"), body.get("model") or None,
        )

    @app.get("/audit/{run_id}/claims")
    async def audit_get_claims(run_id: UUID) -> Any:
        return await run_in_threadpool(_audit_errors, audit_claims, db, run_id)

    def _papers_library():
        """The library papers from corpus/manifest.csv, for the citation check; None if unreadable."""
        from .audit.citations import Library, LibraryPaper

        try:
            rows = read_manifest(settings.manifest_path)
        except (ManifestError, OSError):
            return None
        return Library(LibraryPaper(r.paper_id, r.arxiv_id, r.title, r.fulltext) for r in rows)

    @app.post("/audit/{run_id}/citations/next")
    async def audit_next_citations(run_id: UUID) -> Any:
        return await run_in_threadpool(_audit_errors, next_citation_batch, db, library, papers, run_id, _papers_library(),
                                       retriever, reranker)

    @app.post("/audit/{run_id}/citation-checks")
    async def audit_save_citation_check(run_id: UUID, body: dict[str, Any]) -> Any:
        if not isinstance(body.get("batch"), int):
            raise HTTPException(status_code=422, detail="batch (integer) is required")
        return await run_in_threadpool(_audit_errors, save_citation_check, db, run_id, body["batch"], body.get("output"),
                                       body.get("output_text"), body.get("usage"))

    @app.post("/audit/{run_id}/citation-checks/error")
    async def audit_citation_error(run_id: UUID, body: dict[str, Any]) -> Any:
        if not isinstance(body.get("batch"), int):
            raise HTTPException(status_code=422, detail="batch (integer) is required")
        status, retry = _num(body, "status"), _num(body, "retry_after")
        return await run_in_threadpool(
            _audit_errors, record_llm_error, db, run_id, body["batch"], str(body.get("error", "")),
            int(status) if status is not None else None, retry, body.get("model") or None, None, "citations",
        )

    @app.get("/audit/{run_id}/citation-checks")
    async def audit_get_citation_checks(run_id: UUID) -> Any:
        return await run_in_threadpool(_audit_errors, citation_checks, db, run_id)

    @app.post("/audit/{run_id}/figures/next")
    async def audit_next_figure(run_id: UUID) -> Any:
        return await run_in_threadpool(_audit_errors, next_figure_check, db, library, papers, run_id)

    @app.post("/audit/{run_id}/figure-checks")
    async def audit_save_figure_check(run_id: UUID, body: dict[str, Any]) -> Any:
        if not isinstance(body.get("batch"), int):
            raise HTTPException(status_code=422, detail="batch (integer) is required")
        return await run_in_threadpool(_audit_errors, save_figure_check, db, run_id, body["batch"], body.get("output"),
                                       body.get("output_text"), body.get("usage"))

    @app.post("/audit/{run_id}/figure-checks/error")
    async def audit_figure_error(run_id: UUID, body: dict[str, Any]) -> Any:
        if not isinstance(body.get("batch"), int):
            raise HTTPException(status_code=422, detail="batch (integer) is required")
        status, retry = _num(body, "status"), _num(body, "retry_after")
        return await run_in_threadpool(
            _audit_errors, record_llm_error, db, run_id, body["batch"], str(body.get("error", "")),
            int(status) if status is not None else None, retry, body.get("model") or None, None, "figures",
        )

    @app.get("/audit/{run_id}/figure-checks")
    async def audit_get_figure_checks(run_id: UUID) -> Any:
        return await run_in_threadpool(_audit_errors, figure_checks, db, run_id)

    @app.post("/audit/{run_id}/check-numbers")
    async def audit_check_numbers(run_id: UUID) -> Any:
        return await run_in_threadpool(_audit_errors, number_check, db, papers, run_id)

    @app.post("/audit/{run_id}/result")
    async def audit_assemble(run_id: UUID) -> Any:
        return await run_in_threadpool(_audit_errors, audit_result, db, papers, run_id)

    def _parsed_digest(run_id: UUID) -> str:
        if db.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        steps = [s for s in db.get_steps(run_id, "parse") if s["status"] == "done"]
        if not steps:
            raise HTTPException(status_code=409, detail="the run's PDF has not been parsed yet")
        return steps[-1]["detail"]["sha256"]

    @app.get("/audit/{run_id}/paper")
    async def audit_paper(run_id: UUID) -> Any:
        """The parsed paper: blocks, tables (cell grids) and figures (captions; images separately)."""
        digest = await run_in_threadpool(_parsed_digest, run_id)
        return await run_in_threadpool(papers.paper, digest)

    @app.get("/audit/{run_id}/figures/{figure_id}")
    async def audit_figure(run_id: UUID, figure_id: str) -> Response:
        digest = await run_in_threadpool(_parsed_digest, run_id)
        png = await run_in_threadpool(papers.figure, digest, figure_id)
        if png is None:
            raise HTTPException(status_code=404, detail=f"no image for {figure_id}")
        return Response(content=png, media_type="image/png")

    @app.get("/runs/{run_id}/steps")
    async def run_steps(run_id: UUID) -> Any:
        """Every recorded step of a run: prompts used, LLM outputs, verifier decisions."""
        if await run_in_threadpool(db.get_run, run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        return await run_in_threadpool(db.get_steps, run_id)

    return app


def _web_search(settings: Settings) -> WebSearch | None:
    """Semantic Scholar first when it has a key; otherwise OpenAlex first (D29).

    Without a key, Semantic Scholar's shared pool refused the agent's request on
    the team PC (HTTP 429); OpenAlex answers keyless queries at 10 per second.
    """
    s2 = (
        ("semantic_scholar", SemanticScholar(settings.semantic_scholar_url, settings.semantic_scholar_api_key,
                                             settings.semantic_scholar_interval_s))
        if settings.semantic_scholar_url else None
    )
    oa = ("openalex", OpenAlex(settings.openalex_url, settings.openalex_api_key)) if settings.openalex_url else None
    order = [s2, oa] if settings.semantic_scholar_api_key else [oa, s2]
    providers = [p for p in order if p is not None]
    return WebSearch(providers) if providers else None


def _loc(loc: tuple[Any, ...]) -> str:
    # Model-level validators report an empty location; name it explicitly.
    return ".".join(str(p) for p in loc) or "request"


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:300]


# Run with: uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
