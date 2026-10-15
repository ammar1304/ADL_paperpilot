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
- [x] TEI downloads and serves BGE-M3; `/health` shows `embedder.ok: true` (after the memory fix, about 3.4 GB of RAM)
- [x] Real corpus selection against arXiv: 1,298 matches, 620 returned, 500 kept, 50 full text (manifest sha `51836bdfd4db`)
- [x] Real build: 500 abstracts and 50 PDFs, see the table below
- [ ] Section and table detection on real papers: inspect a few chunks per paper type
- [ ] Bind-mount permissions on Linux (`PP_UID`, `PP_GID`) and on Docker Desktop
- [x] `python scripts/smoke_test.py --no-compose --with-corpus` on Windows

**First real full build** (team PC, Windows 11, Docker Desktop, CPU only, 2026-10-14):

| What | Result |
| --- | --- |
| Duration | 88 minutes (17:06 to 18:34 UTC); PDFs took about 5 minutes, embedding the rest (about 1 chunk per second) |
| Papers indexed | 500 of 500; full text 50 of 50 |
| PDF failures | 0 |
| No section headings detected | 2 of 50 (`arxiv:2601.07054`, `arxiv:2506.16037`): these fall back to plain windows |
| References not cut | 1 of 50 (`arxiv:2512.05430`) |
| Tables and figures found | 173 and 177 |
| Chunks | fixed: 1,240 windows; structured: 500 abstracts, 1,217 sections, 177 figures, 173 tables (2,067) |

**Problems found:**

- The `10-ingest-papers` workflow gave up after 1 hour of polling while the build carried on; the cap is now 4 hours.
- The papers per year are skewed to recent work (2020: 1, 2021: 2, 2022: 7, 2023: 23, 2024: 137, 2025: 206, 2026: 124). Foundational RAG papers are probably missing; add them by hand during the manifest review.
- Formula text extracted by pdfminer contains `(cid:NN)` glyph codes (seen in `arxiv:2502.18536`). Harmless for search, ugly in evidence; MinerU (milestone 3) should fix it.

### Not done yet: needs the team

- [ ] Add foundational papers to the manifest (the selection query favours 2024 to 2026)
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

- [x] A real run with `GROQ_API_KEY`: gpt-oss-20b and gpt-oss-120b return output the parser accepts (smoke test step 7 and run `e36edefe`, both verifier pass on attempt 1)
- [ ] Gemini fallback with a real `GEMINI_API_KEY`
- [x] The Groq model ids `openai/gpt-oss-20b` and `openai/gpt-oss-120b` are accepted by the live API
- [ ] The Gemini model id `models/gemini-3.5-flash-lite` is accepted by the live API
- [ ] Tokens per judge call stay under the free per-minute limit
- [x] First real failures logged in `prompts/failure_log.md` (F-001 to F-003)
- [x] A novelty run on the full 500-paper library with a known answer (run `659c5d05`): Adaptive-RAG found and labelled `partial`, a closer 2026 paper labelled `same`; verifier passed on attempt 1

### Known issues

- Token usage is not stored per run yet (D21).
- The router waits for the whole novelty branch before answering the webhook; with real models and rate limits this can take a minute or more.
- The LLM-written summary is shown, labelled `summary_llm_written`: the verifier checks ids, not prose.
- Papers judged `different` on every facet still appear as "closest work" (F-001), and the summary does not say how small the searched library was (F-002).
- pp-tools did not declare a charset, so Windows PowerShell 5.1 garbled non-ASCII text in its responses. Fixed: every JSON response now says `charset=utf-8`.

## Milestone 2, part 1b: prompt v2 and a corpus that includes the classics (2026-10-14)

Branch `feat/prompt-v2`. Driven by the first real runs (F-001 to F-003).

### Built

- **P3-novelty-judge v2** (v1 kept for comparison): the search scope, labelled passages, unknown versus not covered, unspecified facets, a counter-example (D25).
- **Report:** `closest_work` only for papers with some overlap, `checked_not_close`, `overlap_found`, `unspecified_facets`, and a summary written by code.
- **`app.corpus.add`** and `corpus/additions.csv`: 33 foundational papers with expected titles, 11 of them full text (D26).
- **Vector reuse on rebuilds** (D27), with `chunks_embedded` and `chunks_reused` in the build report.

### Verified

Sandbox, stubs for arXiv, TEI and the LLMs:

| What | Result |
| --- | --- |
| Tests | 93 passed |
| Report split, code summary, nothing overlapping | Tests pass |
| v1 still renders with the new code (for before/after runs) | Test passes |
| `app.corpus.add`: added, upgraded, already present, not found, wrong title refused, dry run writes nothing | Tests pass |
| Rebuild with no change embeds 0 chunks; adding a full-text paper embeds only its chunks; another model's vectors are not reused | Tests pass |
| Smoke test with real n8n 2.42.6 | 7 of 7 steps, prompts `P3 v2` |

### Verified on the team PC (2026-10-15)

| What | Result |
| --- | --- |
| `app.corpus.add` against real arXiv | 33 of 33 ids found, every title accepted; 31 added, Adaptive-RAG upgraded to full text, 1 already present. Library: 531 papers, 61 full text |
| Rebuild with vector reuse | 13 minutes instead of 88: 633 chunks embedded, 3,306 reused; TEI reported `BAAI/bge-m3` |
| Known-answer idea, P3 v2 | F-001 and F-002 gone; Adaptive-RAG and 2604.07985 in closest work, 4 papers in `checked_not_close` |
| Same idea, P3 v1 | Same candidates; summary without scope (F-002) |

**Found:** F-004 (abstracts cut at 900 characters), F-005 (near-duplicate queries; Self-RAG and SKR not retrieved), and Groq rate limiting on back-to-back runs with no Gemini key to fall back on.

## Milestone 2, part 1c: measured prompt comparison (2026-10-15)

Branch `feat/prompt-ab-eval`.

- **F-004 fix:** abstracts kept whole up to 2,000 characters.
- **P1-decomposer v2:** query vocabulary diversity (F-005).
- **`papers_with_full_text`** now comes from the build report (61), not from papers with detected sections (59).
- **E7, `scripts/prompt_ab.py`:** 6 ideas with known answers (one out of domain) x 3 prompt configurations x repeats, failures counted automatically; scoring unit-tested on the logged failures. Verified in the sandbox against n8n with stub models (12 runs, plumbing only).

### Verified on the team PC (2026-10-15)

- [x] `python scripts/prompt_ab.py` with real Groq: 36 of 36 runs, about 45 minutes. Results in `eval/results/20261015-000708-prompt-ab/`, analysis in `prompts/failure_log.md` (E7 run 1).

**Result:** P3 v2 fixes F-002 (0/12 to 12/12) and F-003 (7/12 to 0/12); the code-built report fixes F-001; P1 v2 does not measurably improve retrieval (8 to 10 of 22 expected papers), which is the bottleneck.

## Milestone 2, part 1: done (2026-10-15)

Exit criteria met: real LLMs end to end, a 531-paper library built on a team PC, a known-answer test passing, real failures logged (F-001 to F-005), and a measured prompt comparison. Open items carried forward: retrieval recall (part 2), Gemini fallback never exercised with a real key, the manifest PR, the E1 queries.

## Next: milestone 2, part 2

- Agentic search loop with Semantic Scholar (D20): the LLM judges facet coverage and decides to search again.
- Reranker (`bge-reranker-v2-m3` on TEI).
- E2 leave-one-out evaluation.
- First prompt iterations from real failures.
