-- PaperPilot app schema. Idempotent: pp-tools applies it at startup.
-- Change it by adding statements that are safe to re-run; never edit a
-- statement in a way that silently diverges from existing databases.

CREATE TABLE IF NOT EXISTS runs (
    run_id            uuid PRIMARY KEY,
    mode              text        NOT NULL CHECK (mode IN ('novelty', 'audit')),
    status            text        NOT NULL CHECK (status IN ('received', 'running', 'done', 'failed')),
    input             jsonb       NOT NULL,
    result            jsonb,
    error             jsonb,
    tokens_used       integer     NOT NULL DEFAULT 0,
    cost_usd          numeric(12, 6) NOT NULL DEFAULT 0,
    -- Added beyond the original spec: the n8n execution that created the run.
    -- n8n's Error Trigger only reports the failed execution's id, so this is
    -- how the error-handler workflow finds which run to mark as failed.
    n8n_execution_id  text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS runs_n8n_execution_id_idx ON runs (n8n_execution_id);
CREATE INDEX IF NOT EXISTS runs_created_at_idx ON runs (created_at DESC);

-- One row per pipeline step (decomposer, retriever, judge, verifier, ...).
-- Not written yet in milestone 0; created now so later milestones only add code.
CREATE TABLE IF NOT EXISTS run_steps (
    step_id      bigserial PRIMARY KEY,
    run_id       uuid        NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    step_name    text        NOT NULL,
    status       text        NOT NULL CHECK (status IN ('running', 'done', 'failed', 'skipped')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    detail       jsonb
);

CREATE INDEX IF NOT EXISTS run_steps_run_id_idx ON run_steps (run_id);

-- Long-running background work (corpus builds). n8n starts a job, then polls it.
CREATE TABLE IF NOT EXISTS jobs (
    job_id            uuid PRIMARY KEY,
    kind              text        NOT NULL,
    status            text        NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
    params            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    progress          jsonb,
    result            jsonb,
    error             jsonb,
    n8n_execution_id  text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- At most one active job of each kind: a second build request gets HTTP 409.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_per_kind
    ON jobs (kind) WHERE status IN ('queued', 'running');
