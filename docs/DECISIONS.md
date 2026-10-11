# Technical decisions

One entry per decision: what we chose, what else we considered, and why. Add new entries at the end; when a decision is reversed, add a new entry that supersedes the old one instead of editing it.

## D1. pp-tools owns the app database; n8n talks to it over HTTP

- **Date:** 2026-10-11, milestone 0
- **Decision:** n8n never writes to the `paperpilot` database directly. The router calls pp-tools (`POST /runs`, `PATCH /runs/{id}`, `POST /runs/fail-by-execution`), and pp-tools runs the SQL.
- **Alternatives:** the n8n Postgres node writing `runs` rows, as first planned.
- **Why:** one component owns the schema, so it is tested with pytest instead of by clicking through n8n. n8n needs no database credentials, so workflows import with no manual credential step. The status rules (a `done` or `failed` run cannot change) are enforced in one place.
- **Cost:** one extra HTTP hop per step, a few milliseconds locally.

## D2. One call validates and creates the run

- **Decision:** `POST /runs` validates the request and inserts the row in one call. Invalid requests return 422 and are never stored.
- **Alternatives:** a separate `/runs/validate` endpoint followed by an insert.
- **Why:** one round trip, and no state where a request was validated but not stored. Field errors come back as `{"errors": [{"field", "message"}]}` so the router can return them unchanged.

## D3. Image and runtime versions

| Component | Pinned | How it was checked (2026-10-11) |
| --- | --- | --- |
| n8n | `n8nio/n8n:2.42.6` | `stable` and `latest` npm dist-tags; this exact version was installed and run in development |
| Node for n8n | 24 (inside the n8n image) | n8n 2.42.6 refuses to start on Node 22 (`>=24.0.0` required) |
| Qdrant | `qdrant/qdrant:v1.19.2` | Latest release tag in the qdrant/qdrant git repository; not run yet |
| Postgres | `postgres:17-alpine` | n8n 2.42 warns that Postgres 16 is outside its supported range. Major version pinned; no specific minor tag could be checked |
| pp-tools base | `python:3.13-slim` | Same Python as the test runs |
| Python packages | `services/pp_tools/requirements.txt` | Exact versions the 21 tests passed with |

The container registries were not reachable from the development sandbox, so the Docker tags themselves were not pulled there. The first `docker compose up` on a team machine confirms them (docs/PROGRESS.md).

## D4. Workflows are code: JSON in git, imported on every start

- **Decision:** `n8n/workflows/*.json` is the source of truth. `n8n/entrypoint.sh` unpublishes, imports and publishes them on every start. `scripts/export_workflows.sh` writes UI edits back to the files.
- **Alternatives:** edit only in the UI and export by hand before the deadline; or import once and never again.
- **Why:** every teammate and the graders run the same workflows from a clean clone, and changes show up in `git diff`. The brief asks for an exported workflow that runs end to end.
- **Cost:** UI edits are overwritten on restart unless exported first. `PP_IMPORT_WORKFLOWS_ON_START=false` turns the import off while experimenting.
- **Found while building:** re-importing over a published workflow fails in n8n 2.x with a permission error, so the entrypoint unpublishes first. `import:workflow --activeState=fromJson` is refused outside queue mode, so publishing is a separate step.

## D5. Error handling through an error workflow and the execution id

- **Decision:** every workflow names `90-error-handler` as its error workflow. The router stores the n8n execution id in `runs.n8n_execution_id`, and the error handler marks that execution's open runs as `failed`.
- **Why:** n8n's Error Trigger receives the failed execution's id and error, but not our `run_id`. Storing the execution id is the smallest link between the two. Runs already `done` are never overwritten.
- **Limits:** if pp-tools is down, the run is never created, and the error handler cannot reach pp-tools either. The failure is then visible only in the n8n execution log.

## D6. Workflows read `PP_TOOLS_URL` from the environment

- **Decision:** `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`, and nodes use `{{ $env.PP_TOOLS_URL }}`.
- **Alternatives:** hardcode `http://pp-tools:8000` in every node; n8n Variables (a paid feature).
- **Why:** the same workflow JSON runs in Docker and on a local machine.
- **Risk:** any expression in any workflow can read every environment variable of the n8n container. Do not put API keys in n8n's environment; store them as n8n credentials, or keep them in pp-tools.

## D7. One sub-workflow per branch from the start

- **Decision:** the router calls `20-novelty` and `30-audit` with Execute Workflow nodes. Both are placeholders that return `implemented: false`.
- **Why:** the router contract (input: the run record; output: the branch result) is fixed now, so later milestones only fill in the sub-workflows. Each branch can also be run and tested on its own.

## D8. A test-only fault hook, gated twice

- **Decision:** with `PP_ALLOW_TEST_FAULTS=true` and the header `X-PaperPilot-Test-Fault: fail-after-create`, the router fails on purpose after creating the run.
- **Why:** it is the only way to test the error workflow end to end without breaking a real dependency. Both conditions are required, and with the variable false the header is ignored (verified).

## D9. Smoke test in Python, not Bash

- **Decision:** `scripts/smoke_test.py`, standard library only.
- **Why:** it runs on Windows without WSL, and checking JSON responses is clearer in Python than with `jq`.

## D10. Schema applied by pp-tools at startup

- **Decision:** `schema.sql` is idempotent (`CREATE ... IF NOT EXISTS`) and pp-tools applies it on every start. The Postgres init script only creates the database.
- **Why:** init scripts run only when the volume is first created, so a later schema change would never reach existing databases. A migration tool (Alembic) is more than this project needs; revisit if a column ever has to change type.

## D11. Network exposure

- **Decision:** every published port binds to `127.0.0.1`. Postgres uses host port 5433 by default.
- **Why:** nothing is reachable from the local network, and 5433 avoids clashing with a Postgres already installed on a teammate's machine.
