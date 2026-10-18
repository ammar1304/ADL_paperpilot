"""Postgres access for run and job state. pp-tools is the only writer of the app schema."""

import logging
import time
from importlib import resources
from typing import Any
from uuid import UUID, uuid4

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .models import TERMINAL_STATUSES

log = logging.getLogger("pp_tools.db")

_RUN_COLUMNS = (
    "run_id, mode, status, input, result, error, tokens_used, cost_usd::float AS cost_usd, "
    "n8n_execution_id, to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"') AS created_at, "
    "to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"') AS updated_at"
)


_JOB_COLUMNS = (
    "job_id, kind, status, params, progress, result, error, n8n_execution_id, "
    "to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"') AS created_at, "
    "to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"') AS updated_at"
)


class RunConflict(Exception):
    """Raised when a run is already in a terminal state."""


class JobConflict(Exception):
    """Raised when a job of the same kind is already queued or running."""


class Database:
    def __init__(self, url: str, timeout_s: float) -> None:
        # open=False: the app must start (and /health must answer) even if
        # Postgres is down. Connections are made lazily.
        self.pool = ConnectionPool(
            conninfo=url,
            min_size=0,
            max_size=5,
            timeout=timeout_s,
            open=False,
            kwargs={"connect_timeout": max(1, int(timeout_s))},
        )
        self.schema_ready = False

    def open(self) -> None:
        self.pool.open(wait=False)

    def close(self) -> None:
        self.pool.close()

    def ping(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("SELECT 1")

    def apply_schema(self) -> None:
        sql = resources.files("app").joinpath("schema.sql").read_text(encoding="utf-8")
        with self.pool.connection() as conn:
            conn.execute(sql)
        self.schema_ready = True
        log.info("schema applied")

    def apply_schema_with_retry(self, wait_s: float) -> None:
        deadline = time.monotonic() + wait_s
        attempt = 0
        while True:
            attempt += 1
            try:
                self.apply_schema()
                return
            except Exception as exc:  # noqa: BLE001 - we log and retry any connection failure
                if time.monotonic() >= deadline:
                    log.error("schema not applied, giving up for now", extra={"attempt": attempt, "error": str(exc)})
                    return
                log.warning("database not ready, retrying", extra={"attempt": attempt, "error": str(exc)})
                time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))

    def ensure_schema(self) -> None:
        if not self.schema_ready:
            self.apply_schema()

    # ---- runs -------------------------------------------------------------

    def create_run(self, mode: str, request: dict[str, Any], n8n_execution_id: str | None) -> UUID:
        self.ensure_schema()
        run_id = uuid4()
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, mode, status, input, n8n_execution_id) VALUES (%s, %s, 'received', %s, %s)",
                (run_id, mode, Jsonb(request), n8n_execution_id),
            )
        return run_id

    def list_runs(self, limit: int) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT {_RUN_COLUMNS} FROM runs ORDER BY created_at DESC LIMIT %s", (limit,))
            return cur.fetchall()

    def get_run_by_ref(self, client_ref: str) -> dict[str, Any] | None:
        """The run the web app started with this client ref (D41)."""
        self.ensure_schema()
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT {_RUN_COLUMNS} FROM runs WHERE input->>'client_ref' = %s ORDER BY created_at DESC LIMIT 1", (client_ref,))
            return cur.fetchone()

    def find_done_audit(self, field: str, value: str) -> dict[str, Any] | None:
        """The latest finished audit of the same PDF (same upload hash or same URL), complete runs first."""
        if field not in ("draft_pdf_upload", "draft_pdf_url"):
            raise ValueError(field)
        self.ensure_schema()
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""SELECT {_RUN_COLUMNS} FROM runs
                    WHERE mode = 'audit' AND status = 'done' AND input->>'{field}' = %s
                    ORDER BY (result->'coverage'->>'stopped' IS NULL) DESC, created_at DESC LIMIT 1""",
                (value,))
            return cur.fetchone()

    def get_run(self, run_id: UUID) -> dict[str, Any] | None:
        self.ensure_schema()
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT {_RUN_COLUMNS} FROM runs WHERE run_id = %s", (run_id,))
            return cur.fetchone()

    def update_run(
        self, run_id: UUID, status: str, result: dict[str, Any] | None, error: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Update a run. Returns None if it does not exist; raises RunConflict if it is terminal."""
        self.ensure_schema()
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT status FROM runs WHERE run_id = %s FOR UPDATE", (run_id,))
            row = cur.fetchone()
            if row is None:
                return None
            if row["status"] in TERMINAL_STATUSES:
                raise RunConflict(f"run is already '{row['status']}'")
            cur.execute(
                f"""UPDATE runs
                    SET status = %s,
                        result = COALESCE(%s, result),
                        error  = COALESCE(%s, error),
                        updated_at = now()
                    WHERE run_id = %s
                    RETURNING {_RUN_COLUMNS}""",
                (status, _jsonb_or_none(result), _jsonb_or_none(error), run_id),
            )
            return cur.fetchone()

    def fail_by_execution(self, n8n_execution_id: str, error: dict[str, Any]) -> list[str]:
        """Mark every non-terminal run created by this n8n execution as failed."""
        self.ensure_schema()
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE runs
                   SET status = 'failed', error = %s, updated_at = now()
                   WHERE n8n_execution_id = %s AND status NOT IN ('done', 'failed')
                   RETURNING run_id::text""",
                (Jsonb(error), n8n_execution_id),
            )
            return [r[0] for r in cur.fetchall()]

    # ---- run steps -----------------------------------------------------------

    def add_step(self, run_id: UUID, step_name: str, status: str, detail: dict[str, Any]) -> int:
        """Record one pipeline step (finished) with its full input/output detail."""
        self.ensure_schema()
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO run_steps (run_id, step_name, status, finished_at, detail)
                   VALUES (%s, %s, %s, now(), %s) RETURNING step_id""",
                (run_id, step_name, status, Jsonb(detail)),
            )
            return cur.fetchone()[0]

    def get_steps(self, run_id: UUID, step_name: str | None = None) -> list[dict[str, Any]]:
        """Steps of a run in the order they were recorded."""
        self.ensure_schema()
        query = (
            "SELECT step_id, step_name, status, detail, "
            "to_char(finished_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"') AS finished_at "
            "FROM run_steps WHERE run_id = %s"
        )
        params: list[Any] = [run_id]
        if step_name:
            query += " AND step_name = %s"
            params.append(step_name)
        query += " ORDER BY step_id"
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            return cur.fetchall()

    # ---- jobs -------------------------------------------------------------

    def create_job(self, kind: str, params: dict[str, Any], n8n_execution_id: str | None) -> UUID:
        self.ensure_schema()
        job_id = uuid4()
        try:
            with self.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO jobs (job_id, kind, status, params, n8n_execution_id) VALUES (%s, %s, 'queued', %s, %s)",
                    (job_id, kind, Jsonb(params), n8n_execution_id),
                )
        except errors.UniqueViolation as exc:
            raise JobConflict(f"a '{kind}' job is already queued or running") from exc
        return job_id


    def update_job(
        self,
        job_id: UUID,
        status: str | None = None,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """UPDATE jobs
                   SET status = COALESCE(%s, status),
                       progress = COALESCE(%s, progress),
                       result = COALESCE(%s, result),
                       error = COALESCE(%s, error),
                       updated_at = now()
                   WHERE job_id = %s""",
                (status, _jsonb_or_none(progress), _jsonb_or_none(result), _jsonb_or_none(error), job_id),
            )


    def get_job(self, job_id: UUID) -> dict[str, Any] | None:
        self.ensure_schema()
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE job_id = %s", (job_id,))
            return cur.fetchone()


    def fail_interrupted_jobs(self) -> int:
        """At startup, no job can still be running in this process: mark leftovers failed."""
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE jobs SET status = 'failed', updated_at = now(),
                       error = '{"message": "pp-tools restarted while the job was running"}'::jsonb
                   WHERE status IN ('queued', 'running')"""
            )
            return cur.rowcount


def _jsonb_or_none(value: dict[str, Any] | None) -> Jsonb | None:
    return Jsonb(value) if value is not None else None
