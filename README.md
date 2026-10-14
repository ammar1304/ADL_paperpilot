# PaperPilot

Pre-submission copilot for research papers, built on n8n. Given an idea or a draft PDF, PaperPilot finds the closest prior work, checks whether the draft's claims are supported by its own tables and cited papers, and returns a venue-readiness report. It does not predict acceptance.

**Current state: milestone 2, part 1 (LLM layer and novelty check).**

- **"Check my idea" works end to end:** an LLM splits the idea into facets, the knowledge base is searched, a second LLM compares the idea with the closest papers citing evidence, and a code verifier rejects anything retrieval cannot back up.
- **The audit branch** is still a placeholder that answers `"implemented": false`.
- **The papers knowledge base** (RAG for question answering, from arXiv) can be selected, built through n8n, searched, and evaluated with two chunking strategies.

See [docs/PROGRESS.md](docs/PROGRESS.md) for what is verified and [docs/DECISIONS.md](docs/DECISIONS.md) for why it is built this way.

## What runs today

```
POST /webhook/paperpilot
        │
   00-router (n8n)
        ├─ Create run ──────────────► pp-tools POST /runs  (validate + store, 422 on bad input)
        ├─ Mark running ────────────► pp-tools PATCH /runs/{id}
        ├─ Route by mode (Switch, no LLM)
        │     ├─ novelty ─► 20-novelty sub-workflow
        │     │      render P1 ─► LLM "Decompose idea" (Groq gpt-oss-20b, Gemini fallback, JSON schema)
        │     │      ─► save facets ─► retrieve (all queries, rank fusion) ─► render P3
        │     │      ─► LLM "Judge novelty" (Groq gpt-oss-120b, Gemini fallback, JSON schema)
        │     │      ─► verifier (code) ─► retry once with feedback, or assemble result
        │     └─ audit ───► 30-audit sub-workflow   (placeholder)
        ├─ Mark done ───────────────► pp-tools PATCH /runs/{id}
        └─ Respond with run_id, mode, status, result

POST /webhook/ingest
        │
   10-ingest-papers (n8n)
        ├─ Start build ─────────────► pp-tools POST /corpus/build  (background job, 202 + job_id)
        ├─ Respond job_id to caller
        └─ every 15 s: GET /jobs/{id} until done (summary) or failed (error)
                  pp-tools: manifest ─► arXiv metadata + PDFs ─► text + structure
                            ─► chunking A (fixed) and B (structured) ─► BGE-M3 ─► Qdrant

Any failure ─► 90-error-handler ─► pp-tools POST /runs/fail-by-execution
```

| Service | Port (localhost only) | Role |
| --- | --- | --- |
| n8n 2.42.6 | 5678 | Orchestration, webhook, routing, LLM calls (Groq, Gemini fallback) |
| pp-tools | 8000 | Python service: validation, run state, corpus build jobs, search; later the verifier |
| embedder (TEI cpu-1.9) | internal | Serves the BGE-M3 embedding model |
| Postgres 17 | 5433 | n8n's database and the `paperpilot` app database (runs, jobs) |
| Qdrant v1.19.2 | 6333 | Vector store: `papers_fixed` and `papers_structured` |

## Prerequisites

- Docker with Compose v2 (Docker Desktop on Windows and macOS)
- Python 3.10+ for the scripts in `scripts/` (standard library only)
- Bash for `scripts/export_workflows.sh` (Git Bash on Windows)
- About 6 GB of RAM for Docker (the embedding server needs about 2 to 3 GB). On Windows, check the WSL memory limit in `.wslconfig` if containers get killed.
- About 6 GB of free disk space (images and model)

## Setup

```bash
git clone <your-repo-url> paperpilot && cd paperpilot
cp .env.example .env
```

Edit `.env`:

1. `POSTGRES_PASSWORD`: letters and digits only.
1. `GROQ_API_KEY`: free at https://console.groq.com (needed for "check my idea"). `GEMINI_API_KEY` is optional (fallback model, free at https://aistudio.google.com).
2. `N8N_ENCRYPTION_KEY`: generate with `python3 -c "import secrets; print(secrets.token_hex(24))"`. Use the same key on every machine that shares a database volume, and never change it after the first start.

Start everything:

```bash
docker compose up -d --build --wait
```

The first start pulls images and builds pp-tools. n8n imports and publishes the workflows before it starts listening, so it takes 1 to 3 minutes to report healthy.

The embedder downloads the BGE-M3 model (about 2.3 GB) on its first start, which can take several more minutes. Everything else works meanwhile; `curl localhost:8000/health` shows `"embedder": {"ok": true}` once it is ready.

## Check that it works

```bash
python scripts/smoke_test.py
```

It starts the stack if needed, checks health, sends a novelty, an audit, an invalid and a deliberately failing request, and checks the stored runs. The failing-request step needs `PP_ALLOW_TEST_FAULTS=true` in `.env`; otherwise add `--skip-fault-test`.

Add `--with-corpus` to also build a small knowledge base (5 papers) through the ingestion workflow and search it. This needs `corpus/manifest.csv`, internet access, and the embedding model; the first run waits for the model download.

Add `--with-llm` to also run a full novelty check with the real LLMs. This needs a built knowledge base and `GROQ_API_KEY`.

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

## The papers knowledge base

Full guide: [corpus/README.md](corpus/README.md). In short:

```bash
docker compose exec pp-tools python -m app.corpus.select           # once: writes corpus/manifest.csv
docker compose exec pp-tools python -m app.corpus.add              # adds corpus/additions.csv (checks every title)
curl -X POST http://localhost:5678/webhook/ingest -d '{}' -H 'Content-Type: application/json'
curl http://localhost:8000/jobs/<job_id>                          # follow the build
curl http://localhost:8000/corpus/stats                           # what was indexed
curl -X POST http://localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"query": "query rewriting for multi-hop questions", "strategy": "structured", "k": 5}'
```

`/search` body: `strategy` is `fixed` or `structured`; `level` is `chunk` (default) or `paper` (distinct papers); `filters` can hold `{"chunk_type": "table"}` or `{"paper_id": "arxiv:..."}`.

Comparing the two chunking strategies (evaluation E1): [eval/README.md](eval/README.md).

## Checking an idea (novelty)

Needs a built knowledge base and `GROQ_API_KEY`:

```bash
curl -X POST http://localhost:5678/webhook/paperpilot -H 'Content-Type: application/json' \
  -d '{"mode": "novelty", "idea_text": "Let the language model rewrite its search query after each retrieval step to answer multi-hop questions."}'
```

The result holds:

- `summary`: written by code from the verified labels only: what was searched, which papers overlap and on which facets, and which facets the idea leaves unspecified. `summary_llm_written` is the judge's own prose, shown but labelled.
- `overlap_found` and `closest_work`: papers labelled `same` or `partial` on at least one facet, strongest first, with the evidence chunk ids and a rationale.
- `checked_not_close`: papers retrieved and judged, but `different` or `unknown` on every facet.
- `unverified`: anything the verifier rejected, with the reason.
- `unspecified_facets`, `coverage` (what was searched, including how many papers have full text) and the prompt versions used. Every step, including the exact LLM outputs, is in `GET /runs/<run_id>/steps`.

Prompts: [prompts/README.md](prompts/README.md).

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
n8n/workflows/            00-router, 10-ingest-papers, 20-novelty, 30-audit, 90-error-handler
services/pp_tools/        FastAPI service: corpus/, retrieval/, eval/, schema.sql, tests
infra/postgres/init/      creates the app database on first start
scripts/                  smoke test, workflow export and normalizer
corpus/                   manifest.csv (the papers), selection.json, guide
.cache/pp-tools/          downloaded PDFs, metadata, extracted text, build report (not in git)
prompts/                  prompt library: versioned prompts, active.json, failure log
eval/                     queries, results, evaluation guide
docs/                     DECISIONS.md, PROGRESS.md
```

## Troubleshooting

These are problems we actually hit while building milestones 0 and 1, except where marked.

- **Webhook returns 404 right after n8n is healthy.** n8n reports healthy a few seconds before the webhook is registered. Wait 10 seconds and retry; the smoke test already does.
- **`You do not have permission to deactivate this workflow` on restart.** Re-importing over published workflows fails in n8n 2.x. `n8n/entrypoint.sh` unpublishes them first; if you see this, check that you are using the repo's entrypoint.
- **`n8n's address '::' is not available`.** The host has no IPv6. Compose sets `N8N_LISTEN_ADDRESS=0.0.0.0` for this.
- **`Postgres 16 is outside the supported range`.** n8n 2.42 warns on Postgres 16, which is why compose uses Postgres 17.
- **`/pp/entrypoint.sh: not found` or `\r` errors (not hit here).** The script has Windows line endings. `.gitattributes` forces LF; if you cloned before it existed, run `git rm --cached -r . && git reset --hard`.
- **`ports are not available ... access a socket in a way forbidden by its access permissions` (Windows, hit on a team PC).** Windows reserved that port (Hyper-V/WSL port ranges), or another program uses it. Either run `net stop winnat` then `net start winnat` in an administrator PowerShell, or pick another port in `.env` (`PP_TOOLS_HOST_PORT=8010`, `N8N_HOST_PORT`, `QDRANT_HOST_PORT`, `POSTGRES_HOST_PORT`) and run `docker compose up -d --wait`. The smoke test reads these ports from `.env`; for curl commands in this README, replace 8000 or 5678 with your port.
- **Port already in use.** Change the port in `.env` (see above), or stop the other program.
- **n8n cannot read credentials after changing `N8N_ENCRYPTION_KEY` (not hit here).** The key is stored with the data. Restore the old key, or reset with `docker compose down -v` (deletes all data).
- **`pp-tools` unhealthy.** `curl localhost:8000/health` shows which dependency is down; `docker compose logs pp-tools` has JSON logs.
- **A novelty request returns HTTP 500 (not hit with real keys yet).** Open the failed execution in the n8n UI (`20-novelty`, Executions): a red LLM node usually means a missing or invalid `GROQ_API_KEY` or a rate limit. After changing a key in `.env`, run `docker compose up -d` so n8n re-imports the credential.
- **`/search` answers 409.** No corpus build has run yet: see [corpus/README.md](corpus/README.md).
- **`/search` answers 503 "embedder unavailable" (not hit here).** The model is still downloading, or the embedder container ran out of memory: `docker compose logs embedder`.
- **Permission denied writing `corpus/`, `eval/` or `.cache/` (Linux, not hit here).** Set `PP_UID` and `PP_GID` in `.env` to the output of `id -u` and `id -g`, then `docker compose up -d`.
- **The embedder restarts after "Warming up model", using almost all of Docker's memory (hit on a team PC).** Its batch size is too large for the available RAM. `EMBEDDER_MAX_BATCH_TOKENS` in `.env` (default 2048) controls it; lower it to 1024 if it still runs out of memory, then `docker compose up -d --wait`. The model download is cached, so restarts are fast.
- **The embedder fails to start on a Mac with Apple Silicon (not hit here).** Set `EMBEDDER_IMAGE=ghcr.io/huggingface/text-embeddings-inference:cpu-arm64-1.9` in `.env`.
- **A request returns `{"message": "Error in workflow"}`.** Open the execution in the n8n UI (Executions tab) to see which node failed. If pp-tools was down, no run row exists.

Reset everything, including all data: `docker compose down -v`.
