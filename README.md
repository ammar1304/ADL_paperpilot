# PaperPilot

Pre-submission copilot for research papers, built on n8n. Given an idea or a draft PDF, PaperPilot finds the closest prior work, checks whether the draft's claims are supported by its own tables and cited papers, and returns a venue-readiness report. It does not predict acceptance.

**Current state: milestone 0 (foundation).** The pipeline runs end to end, but both branches are placeholders that answer `"implemented": false`. See [docs/PROGRESS.md](docs/PROGRESS.md) for what is verified and [docs/DECISIONS.md](docs/DECISIONS.md) for why it is built this way.

## What runs today

```
POST /webhook/paperpilot
        │
   00-router (n8n)
        ├─ Create run ──────────────► pp-tools POST /runs  (validate + store, 422 on bad input)
        ├─ Mark running ────────────► pp-tools PATCH /runs/{id}
        ├─ Route by mode (Switch, no LLM)
        │     ├─ novelty ─► 20-novelty sub-workflow (placeholder)
        │     └─ audit ───► 30-audit sub-workflow   (placeholder)
        ├─ Mark done ───────────────► pp-tools PATCH /runs/{id}
        └─ Respond with run_id, mode, status, result

Any failure ─► 90-error-handler ─► pp-tools POST /runs/fail-by-execution
```

| Service | Port (localhost only) | Role |
| --- | --- | --- |
| n8n 2.42.6 | 5678 | Orchestration, webhook, routing |
| pp-tools | 8000 | Python service: validation, run state; later chunkers and verifier |
| Postgres 17 | 5433 | n8n's database and the `paperpilot` app database |
| Qdrant v1.19.2 | 6333 | Vector store (empty until milestone 1) |

## Prerequisites

- Docker with Compose v2 (Docker Desktop on Windows and macOS)
- Python 3.10+ for the scripts in `scripts/` (standard library only)
- Bash for `scripts/export_workflows.sh` (Git Bash on Windows)
- About 4 GB free RAM

## Setup

```bash
git clone <your-repo-url> paperpilot && cd paperpilot
cp .env.example .env
```

Edit `.env`:

1. `POSTGRES_PASSWORD`: letters and digits only.
2. `N8N_ENCRYPTION_KEY`: generate with `python3 -c "import secrets; print(secrets.token_hex(24))"`. Use the same key on every machine that shares a database volume, and never change it after the first start.

Start everything:

```bash
docker compose up -d --build --wait
```

The first start pulls images and builds pp-tools. n8n imports and publishes the workflows before it starts listening, so it takes 1 to 3 minutes to report healthy.

## Check that it works

```bash
python scripts/smoke_test.py
```

It starts the stack if needed, checks health, sends a novelty, an audit, an invalid and a deliberately failing request, and checks the stored runs. The failing-request step needs `PP_ALLOW_TEST_FAULTS=true` in `.env`; otherwise add `--skip-fault-test`.

To test services that are already running: `python scripts/smoke_test.py --no-compose`.

## Calling the webhook

Bash:

```bash
curl -X POST http://localhost:5678/webhook/paperpilot \
  -H 'Content-Type: application/json' \
  -d '{"mode": "novelty", "idea_text": "Retrieval-augmented verification of numeric claims against result tables."}'
```

PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:5678/webhook/paperpilot `
  -ContentType 'application/json' `
  -Body '{"mode": "audit", "draft_pdf_url": "https://arxiv.org/pdf/1706.03762", "venue": "NeurIPS"}'
```

Request fields:

| Field | Required | Rule |
| --- | --- | --- |
| `mode` | yes | `novelty` or `audit` |
| `idea_text` | for `novelty` | 20 to 5000 characters |
| `draft_pdf_url` | for `audit` | an http(s) URL |
| `venue` | no | up to 200 characters |
| `query_language` | no | `en` (default) or `fr` |

Unknown fields are rejected. Responses: `200` with `run_id`, `mode`, `status`, `result`; `422` with `{"errors": [{"field", "message"}]}`; `500` if the workflow fails (the run is then marked `failed`).

Inspect runs: `curl http://localhost:8000/runs?limit=5` or `curl http://localhost:8000/runs/<run_id>`. API docs: http://localhost:8000/docs.

## Working on workflows

The JSON files in `n8n/workflows/` are the source of truth. On every start, n8n replaces its workflows with these files.

1. Open http://localhost:5678. The first visit asks you to create a local owner account.
2. Edit a workflow in the UI and save it.
3. **Before restarting n8n**, export your changes: `./scripts/export_workflows.sh`
4. Review `git diff n8n/workflows/` and commit.

To keep UI edits across restarts while experimenting, set `PP_IMPORT_WORKFLOWS_ON_START=false` in `.env`, and export before turning it back on.

A new workflow needs a fixed top-level `"id"` (16 letters or digits) so that other workflows can reference it. Export it once, add it to `n8n/workflows/`, and the scripts handle it from then on.

## Running the pp-tools tests

```bash
cd services/pp_tools
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest                                           # unit tests only
```

The integration tests need a throwaway Postgres database; they delete rows from `runs`. With the stack running:

```bash
docker compose exec postgres createdb -U paperpilot paperpilot_test
TEST_DATABASE_URL=postgresql://paperpilot:<password>@localhost:5433/paperpilot_test pytest
```

## Project layout

```
docker-compose.yml        all services
.env.example              every variable, placeholders only
n8n/entrypoint.sh         unpublish, import, publish, then start n8n
n8n/workflows/            00-router, 20-novelty, 30-audit, 90-error-handler
services/pp_tools/        FastAPI service, schema.sql, tests
infra/postgres/init/      creates the app database on first start
scripts/                  smoke test, workflow export and normalizer
prompts/                  prompt library (from milestone 2)
eval/                     test sets and results (from milestone 1)
docs/                     DECISIONS.md, PROGRESS.md
```

## Troubleshooting

These are problems we actually hit while building milestone 0, except where marked.

- **Webhook returns 404 right after n8n is healthy.** n8n reports healthy a few seconds before the webhook is registered. Wait 10 seconds and retry; the smoke test already does.
- **`You do not have permission to deactivate this workflow` on restart.** Re-importing over published workflows fails in n8n 2.x. `n8n/entrypoint.sh` unpublishes them first; if you see this, check that you are using the repo's entrypoint.
- **`n8n's address '::' is not available`.** The host has no IPv6. Compose sets `N8N_LISTEN_ADDRESS=0.0.0.0` for this.
- **`Postgres 16 is outside the supported range`.** n8n 2.42 warns on Postgres 16, which is why compose uses Postgres 17.
- **`/pp/entrypoint.sh: not found` or `\r` errors (not hit here).** The script has Windows line endings. `.gitattributes` forces LF; if you cloned before it existed, run `git rm --cached -r . && git reset --hard`.
- **Port already in use.** Change `POSTGRES_HOST_PORT` in `.env`, or stop the other service on 5678, 8000 or 6333.
- **n8n cannot read credentials after changing `N8N_ENCRYPTION_KEY` (not hit here).** The key is stored with the data. Restore the old key, or reset with `docker compose down -v` (deletes all data).
- **`pp-tools` unhealthy.** `curl localhost:8000/health` shows which dependency is down; `docker compose logs pp-tools` has JSON logs.
- **A request returns `{"message": "Error in workflow"}`.** Open the execution in the n8n UI (Executions tab) to see which node failed. If pp-tools was down, no run row exists.

Reset everything, including all data: `docker compose down -v`.
