"""pp-tools: deterministic helpers for the PaperPilot n8n pipeline.

Milestone 0 scope: health checks and run-state management. Later milestones add
the chunkers, the verifier and ID helpers here, so that n8n, the tests and the
evaluation harness all run the same code.
"""

import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import Settings, get_settings
from .db import Database, RunConflict
from .logging_setup import setup_logging
from .models import (
    CreateRunBody,
    CreateRunResponse,
    FailByExecutionBody,
    FieldError,
    RunRecord,
    RunRequest,
    UpdateRunBody,
    ValidationErrorResponse,
)

log = logging.getLogger("pp_tools")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    db = Database(settings.database_url, settings.health_timeout_s)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.open()
        await run_in_threadpool(db.apply_schema_with_retry, settings.startup_db_wait_s)
        yield
        db.close()

    app = FastAPI(title="pp-tools", version="0.1.0", lifespan=lifespan)
    app.state.db = db
    app.state.settings = settings

    # ---- health -----------------------------------------------------------

    @app.get("/health")
    async def health() -> JSONResponse:
        checks: dict[str, dict[str, Any]] = {}

        try:
            await run_in_threadpool(db.ping)
            checks["postgres"] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            checks["postgres"] = {"ok": False, "error": _short(exc)}

        try:
            async with httpx.AsyncClient(timeout=settings.health_timeout_s) as client:
                resp = await client.get(f"{settings.qdrant_url.rstrip('/')}/readyz")
            checks["qdrant"] = {"ok": resp.status_code == 200, "http_status": resp.status_code}
        except Exception as exc:  # noqa: BLE001
            checks["qdrant"] = {"ok": False, "error": _short(exc)}

        ok = all(c["ok"] for c in checks.values())
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

    return app


def _loc(loc: tuple[Any, ...]) -> str:
    # Model-level validators report an empty location; name it explicitly.
    return ".".join(str(p) for p in loc) or "request"


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:300]

# Run with: uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
