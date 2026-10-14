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

- [x] All four images pull with the pinned tags (team PC, Windows 11 + Docker Desktop, 2026-10-14)
- [x] pp-tools image builds
- [x] Qdrant healthcheck (bash `/dev/tcp` probe) turns healthy
- [x] n8n container: entrypoint runs under the image's shell, after fixing a BusyBox `mktemp` incompatibility (PR #4)
- [x] Postgres 17 works with n8n and pp-tools
- [ ] `scripts/export_workflows.sh` against the n8n container
- [x] Smoke test on Windows (Docker Desktop): steps 1 to 5 passed, 2026-10-14
- [ ] Smoke test on macOS

**Problems found on the first real run (all fixed):**

| Problem | Cause | Fix |
| --- | --- | --- |
| `docker compose up` refused port 8000, then 8010 | Windows (Hyper-V/WSL) reserves port ranges | Ports configurable in `.env` (PR #3); `net stop winnat` and `net start winnat` freed them |
| n8n restarted in a loop: `mktemp: Invalid argument` | BusyBox `mktemp` on Alpine needs the X's at the end of the template | New template, entrypoint tested under BusyBox (PR #4) |
| The embedder restarted after "Warming up model" at 8.8 of 9.6 GB | TEI's default 16384 batch tokens is too much for CPU warm-up | `MAX_BATCH_TOKENS=2048` (D15) |

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

- [x] Images pull: `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`, plus the milestone 0 list
- [ ] TEI downloads and serves BGE-M3; `/health` shows `embedder.ok: true` (download done in 13 minutes; serving pending the memory fix)
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

## Milestone 2, part 1: the LLM layer and the novelty check (2026-10-14)

**Goal:** the first real LLM features. "Check my idea" now runs end to end: LLM decomposer, RAG retrieval, LLM judge, code verifier. Branch `feat/m2-novelty` (on top of milestone 1).

### Built

- **n8n `20-novelty`:**
  - Two Basic LLM Chain nodes: "Decompose idea" uses Groq gpt-oss-20b, "Judge novelty" uses gpt-oss-120b. Each has a Gemini fallback and a strict JSON output parser.
  - pp-tools calls in between: render prompt, save facets, retrieve, render judge prompt, verify, assemble.
  - A verifier loop: one retry with feedback.
- **Prompt library:** `prompts/` holds P1-decomposer v1 and P3-novelty-judge v1, plus `active.json`, `failure_log.md` and a README. pp-tools renders them (`/prompts/{name}/render`).
- **pp-tools `app/novelty.py`:**
  - Output contracts, and multi-query retrieval with reciprocal rank fusion.
  - Judge prompt rendering with candidates, evidence chunk ids and verifier feedback.
  - The verifier, and result assembly with coverage and prompt versions.
  - Every step is stored in `run_steps`; see `GET /runs/{id}/steps`.
- **Credentials:** Groq and Gemini credentials are created at n8n start from `.env`, then the keys are removed from n8n's environment.
- **Smoke test:** new optional step 7, `--with-llm` (a full novelty check). Step 3 now uses the audit branch, which is still a placeholder.

### Verified

Run in the sandbox with real n8n 2.42.6. The LLM nodes talked to a local stand-in model through the real Groq and Gemini node code (`GROQ_BASE_URL` and the Gemini credential host pointed at `tests/stubs.py`). This proves the wiring, contracts, fallback and verifier logic, **not answer quality**.

| What | How | Result |
| --- | --- | --- |
| Tests | `pytest` with Postgres | 81 passed |
| Normal run | Novelty request through the router | 3 verified matches, 1 judge attempt, every step stored |
| Hallucinated paper | Stand-in judge cites a paper not retrieved | Verifier rejects it, judge retries with feedback, run passes on attempt 2 (n8n loop ran twice) |
| Persistent hallucination | Stand-in judge repeats it after feedback | Reported under `unverified` with the reason; not in `closest_work` |
| Groq down | Groq endpoint returns 500 | Gemini fallback nodes answered; run done |
| Both down | Both endpoints return 500 | 3 tries, then the run is marked `failed` by the error workflow |
| Credentials | Entrypoint on a fresh and an existing database | 2 credentials imported, workflows published |
| Export round trip | Export `20-novelty`, normalize, compare | Identical, credentials kept |
| Schemas agree | Workflow parser schemas against pp-tools contracts | Test passes |
| Smoke test | `--with-corpus --corpus-limit 3 --with-llm` | 7 of 7 steps |

### Not verified yet: needs a Groq key and internet

- [ ] A real run with `GROQ_API_KEY`: gpt-oss-20b and gpt-oss-120b return output the parser accepts
- [ ] Gemini fallback with a real `GEMINI_API_KEY`
- [ ] The model ids `openai/gpt-oss-20b`, `openai/gpt-oss-120b` and `models/gemini-3.5-flash-lite` are accepted by the live APIs
- [ ] Tokens per judge call stay under the free per-minute limit
- [ ] First real failures logged in `prompts/failure_log.md`

### Known issues

- Token usage is not stored per run yet (D21).
- The router waits for the whole novelty branch before answering the webhook; with real models and rate limits this can take a minute or more.
- The LLM-written summary is shown, labelled `summary_llm_written`: the verifier checks ids, not prose.

## Next: milestone 2, part 2

- Agentic search loop with Semantic Scholar (D20): the LLM judges facet coverage and decides to search again.
- Reranker (`bge-reranker-v2-m3` on TEI).
- E2 leave-one-out evaluation.
- First prompt iterations from real failures.
