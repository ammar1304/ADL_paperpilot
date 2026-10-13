# Progress log

A milestone is done only when its behaviour has been run and checked. "Built" without "verified" stays open.

## Milestone 0: foundation (2026-10-11 to 2026-10-12)

**Goal:** a stack that every later milestone plugs into: n8n router, sub-workflow per branch, error workflow, run state in Postgres, pp-tools service, Qdrant running.

### Verified

Run in the development sandbox (Linux), with the same components installed natively, because container registries were blocked there: n8n 2.42.6 on Node 24.21.0, Postgres 16.15, pp-tools on Python 3.13.

| What | How | Result |
| --- | --- | --- |
| Request validation, run state, status rules | `pytest` (unit tests plus Postgres integration tests) | 21 passed |
| `/health` reports each dependency truthfully | Tests with Postgres down, Qdrant down, both down, both up (Qdrant simulated by a small HTTP stub) | Pass |
| Workflow import and publish | `n8n/entrypoint.sh` on an empty n8n database | 4 workflows imported and published |
| Restart safety | 3 consecutive starts of the entrypoint on the same database | Pass, after fixing the unpublish step (D4) |
| Novelty and audit requests | `scripts/smoke_test.py --no-compose --allow-degraded-qdrant` | HTTP 200, run stored as `done` |
| Invalid request | Smoke test | HTTP 422 with field errors, no row stored |
| Error workflow | Smoke test with the fault header | HTTP 500, run marked `failed` with the error message and node |
| Fault hook disabled | `PP_ALLOW_TEST_FAULTS=false` plus the header | HTTP 200, header ignored |
| pp-tools down | Request sent with pp-tools stopped | HTTP 500 after about 4 s (3 tries), no row |
| Export round trip | Export router with the n8n CLI, normalize, compare | Nodes, parameters, connections and settings identical |
| Postgres init script | Run against a local Postgres | Database created |
| Compose file and shell scripts | `docker compose config`, `sh -n`, `bash -n` | Valid |

### Not verified yet: run on a team machine

Run `docker compose up -d --build --wait` then `python scripts/smoke_test.py` on a clean clone, and record the result here.

- [ ] All four images pull with the pinned tags
- [ ] pp-tools image builds
- [ ] Qdrant healthcheck (bash `/dev/tcp` probe) turns healthy; the smoke test then passes without `--allow-degraded-qdrant`
- [ ] n8n container: entrypoint runs under the image's shell, `n8n_data` volume is writable
- [ ] Postgres 17 works with n8n and pp-tools (tests ran on 16)
- [ ] `scripts/export_workflows.sh` against the n8n container
- [ ] Smoke test on Windows (Docker Desktop) and macOS

### Known issues

- The webhook returns 404 for a few seconds after n8n reports healthy.
- A failed workflow returns n8n's generic `{"message": "Error in workflow"}` without the `run_id`.
- n8n logs a missing Python task runner and several deprecation notices at start. Harmless: no Code node is used.
- n8n and pp-tools share one Postgres user. Acceptable locally; split before any shared deployment.
- Starlette prints a deprecation warning for its httpx-based test client.

## Milestone 1: corpus and chunking (2026-10-13)

**Goal:** a reproducible papers knowledge base, chunked two ways, searchable through an API, with an evaluation that compares the two chunking strategies. Branch `feat/m1-corpus-chunking`.

### Built

- **Corpus selection** from arXiv into a committed manifest (`app.corpus.select`).
- **Build pipeline:**
  - arXiv metadata and PDFs, cached and rate-limited.
  - Plain PDF text with column-aware ordering.
  - Section, table and figure detection.
  - Chunking strategies A and B.
  - BGE-M3 embeddings through TEI, stored in two Qdrant collections.
- **pp-tools endpoints:** `POST /corpus/build` (background job), `GET /jobs/{id}`, `GET /corpus/stats`, `POST /search` (chunk or paper level, filters).
- **n8n workflow `10-ingest-papers`:** webhook, then start the job, reply with the `job_id`, poll every 15 s, and finish with a summary or an error.
- **E1 tooling:** the worksheet generator (`app.eval.make_queries`) and the evaluation runner (`app.eval.retrieval`), giving Recall@k and MRR per strategy and per query type, with the run configuration recorded.
- **Docker:** an `embedder` service (TEI `cpu-1.9`, BGE-M3), and bind mounts for `corpus/`, `eval/`, `.cache/pp-tools/`.
- **Smoke test:** an optional step 7, `--with-corpus`, ingests a few papers through n8n and searches both collections.

### Verified

Run in the development sandbox. arXiv, Hugging Face and the container registries are blocked there, so arXiv and the embedding server were replaced by local stubs (`services/pp_tools/tests/stubs.py`): 3 fake papers, generated two-column PDFs, hashed bag-of-words vectors. Qdrant ran in-process (`QDRANT_URL=local::memory:`). These runs check the plumbing and logic, **not retrieval quality**.

| What | How | Result |
| --- | --- | --- |
| Unit and integration tests | `pytest` with a Postgres test database | 58 passed |
| Pinned requirements | Fresh virtualenv from `requirements-dev.txt`, then pytest | 58 passed |
| Two-column PDF reading order | Generated PDF; the first version read the right column first, now fixed | Headings, table, figure and references detected |
| Corpus selection | `python -m app.corpus.select` against the stub | Manifest and `selection.json` written; refuses to overwrite without `--force` |
| Build via n8n | `POST /webhook/ingest` with real n8n 2.42.6 | 202 with `job_id`; job done; 3 papers, 2 full texts, both collections filled |
| Concurrent build | Second request while one runs | 409 |
| Polling loop | Slowed arXiv stub, build longer than 15 s | Workflow polled twice, ended on the summary |
| Failing build | Embedder unreachable | Job `failed`, workflow ended in "Build failed", error workflow ran |
| Rebuild from cache | arXiv unreachable on the second build | Same chunks, no duplicates |
| Bad PDF | Corrupt cached PDF | Paper falls back to its abstract, listed in `pdf_failures` |
| Search API | `/search` chunk and paper level, `chunk_type` filter | Pass |
| E1 runner | Worksheet with 3 queries, through the API | Results folder with summary, per-query rankings and config |
| Smoke test | `smoke_test.py --no-compose --with-corpus --corpus-limit 3` | 7 of 7 steps passed |

### Not verified yet: needs a team machine with internet

- [ ] Images pull: `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`, plus the milestone 0 list
- [ ] TEI downloads and serves BGE-M3; `/health` shows `embedder.ok: true`
- [ ] Real corpus selection against arXiv (the query syntax has not been run against the real API)
- [ ] Real build: 500 abstracts and 50 PDFs; check `pdf_failures` and `papers_without_headings` in the build report
- [ ] Section and table detection on real papers: inspect a few chunks per paper type
- [ ] Bind-mount permissions on Linux (`PP_UID`, `PP_GID`) and on Docker Desktop
- [ ] `python scripts/smoke_test.py --with-corpus`

### Not done yet: needs the team

- [ ] Review and commit `corpus/manifest.csv` (choose the 50 full-text papers)
- [ ] Write the E1 queries in the worksheet (rules in `eval/queries/README.md`)
- [ ] First E1 run on dev, then tune; one final run on test
- [ ] Compare BGE-M3 with an English-only embedding model (D14)

### Known issues

- Table text has no cell coordinates; every number is joined with `|`. MinerU fixes this in milestone 3.
- Token counts are approximate (D17).
- The first build on CPU may take 10 to 30 minutes; most of it is waiting 3 s between PDF downloads, plus embedding.

## Next: milestone 2, novelty branch

Decomposer, agentic search loop with Semantic Scholar (D20), reranker, novelty judge, verifier on citations; LLM calls through Groq (D12). Blocked only on teammates creating Groq keys.
