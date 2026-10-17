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
- [x] Write the E1 queries in the worksheet (rules in `eval/queries/README.md`; written by Claude, D38)
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

## Milestone 2, part 2a: reranker (2026-10-15)

Branch `feat/m2-reranker`. Driven by E7 run 1: retrieval, not the judge, is the bottleneck (25 of 57 expected papers reached the judge).

### Built

- **`reranker` service:** TEI with `BAAI/bge-reranker-base`; pp-tools client with retries; reported in `/health` (not required).
- **Two-stage ranking (D28):** 50 chunks per query, a fused pool of 30 papers, cross-encoder re-ordering against the idea text, evidence chosen by reranker score. Falls back to the fused order with a recorded reason if the reranker fails. Switches: `NOVELTY_RERANK`, `NOVELTY_POOL_PAPERS`, `NOVELTY_IDEA_QUERY`.
- **`POST /novelty/rank`:** stateless ranking for experiments.
- **E8, `scripts/retrieval_ab.py`:** replays the 25 query sets stored in E7 run 1 under four settings, no LLM calls.
- n8n: the retrieve call may take up to 3 minutes (CPU reranking).

### Verified

Sandbox with stub models (a word-overlap stand-in for the cross-encoder):

| What | Result |
| --- | --- |
| Tests | 111 passed |
| Reranker re-orders the pool and picks the most relevant passage as evidence | Test passes |
| Reranker failure or absence keeps the fused order, with the reason | Tests pass |
| Scores come back in input order although TEI sorts them | Test passes |
| Novelty run through real n8n 2.42.6 | `ranking_method: reranked`, 7 of 7 smoke steps |
| E8 script end to end | 25 query sets x 4 settings; stored-run baseline 25/57 read from E7 run 1 |

### Verified on the team PC (2026-10-15)

- [x] TEI loads `BAAI/bge-reranker-base` on the CPU image (download and start: 9 minutes); reranker 3.8 GB, embedder 3.4 GB of 9.6 GB
- [x] About 19 seconds per reranked ranking (60 pairs) on the team PC's CPU
- [x] E8 run 1: expected papers reaching the judge 25 → 34 of 57 with reranking and the idea as a query; iterative rewriting got worse (4 → 1 of 12). Details in `docs/DECISIONS.md` D28.

## Milestone 2, part 2b: reranker tuning (2026-10-15)

Branch `feat/m2-rerank-tuning`.

- `NOVELTY_IDEA_QUERY` on by default (E8 run 1).
- **Abstract scoring** (`NOVELTY_RERANK_ABSTRACT`): the reranker scores each pool paper's abstract, fetched by id from Qdrant, plus its best chunk.
- **Hybrid order** (`NOVELTY_ORDER=hybrid`): reciprocal rank fusion of the fused and rerank ranks.
- `RERANKER_MAX_BATCH_TOKENS` default 4096 → 2048 (RAM).
- E8 script: one pp-tools call per pool setting; fused, reranked and hybrid orders derived from the returned pool; every pool saved (`pools.jsonl`) for later analysis.
- Sandbox: 115 tests pass; smoke test with real n8n passes.

### Not verified yet: needs the team PC

- [x] E8 run 2: the hybrid order recovers iterative rewriting (4/12) and keeps adaptive retrieval (18/30): 37 of 57 expected papers reach the judge, from 25 before the reranker. Abstract scoring made it worse. Hybrid is now the default (D28).
- [x] Reranker RAM with 2048 batch tokens: 3.5 GB

## Milestone 2, part 2c: the search agent (2026-10-15)

Branch `feat/m2-search-agent`. The agentic part of the novelty check (D29).

### Built

- **P2-search-planner v1:** each round, stop or search again, with up to 3 queries, each in the local library or on Semantic Scholar.
- **n8n `20-novelty`:** a loop of Render planner prompt → Agent should plan? → Plan search (LLM) → Apply plan → Search again?, between retrieval and the judge.
- **pp-tools:** a Semantic Scholar client (`app/external/semantic_scholar.py`), `POST /novelty/{run}/plan-prompt` and `/plan`, and fusion of web results with local search. Limits enforced in code: round budget, 3 queries per round, no repeats, stop when nothing new, invalid plans and outages handled.
- **Result:** `search_agent` (each round's decision, reason, queries, new candidates), web queries in `coverage` and in the summary, and the source of each paper (`local`, `semantic_scholar`, or both).
- **E7 script:** reports agent rounds and expected papers added by the agent. It also writes `prompts/active.json` with Unix line endings, so git no longer sees a change on Windows.

### Verified

Sandbox with stand-ins for the LLMs, the reranker and Semantic Scholar:

| What | Result |
| --- | --- |
| Tests | 125 passed |
| Search once, then stop | Through real n8n 2.42.6: round 1 searched (1 local, 1 web query), a web-only paper reached the judge, round 2 stopped; verifier accepted the `s2:` evidence |
| Stop at once | Through n8n: one round, `stop` |
| Planner that always searches | Through n8n: round 2 found nothing new, the loop stopped |
| Repeated queries, round limit, invalid plan, Semantic Scholar down | Tests pass |
| Same paper from both sources | Counted once, source `local+semantic_scholar` |

### Not verified yet: needs the team PC

- [ ] Real Semantic Scholar from the PC, with and without an API key
- [x] gpt-oss-20b's plans on the known-answer idea (team PC): the loop ran 2 rounds, but the plans were poor (F-006) and re-ranking after the agent's queries lost Self-RAG and SKR (F-007). Fixed in part 2d.
- [ ] E7 with the agent on (rounds 2) and off (0): expected papers reaching the judge, and run time

## Milestone 2, part 2d: the agent only adds (2026-10-15)

Branch `fix/agent-keeps-candidates`.

- **F-007 fix (code):** earlier candidates are always kept with their evidence; the agent may add at most `NOVELTY_AGENT_EXTRA_PAPERS` (2) papers, each scored by the reranker at least as relevant as the weakest candidate already shown. The `retrieve` step records the admission (`floor_rerank_score`, `slots`, `admitted`).
- **F-006 fix (prompt):** P2-search-planner v2. The planner sees the judge's current candidates, abstract first, 500 characters.
- Local queries now report how many new papers they found, so the planner's history shows unproductive rounds.
- Smoke test step 7 checks that the planner ran, and says to restart n8n when the workflow is out of date. README: restart n8n after pulling workflow changes.
- Sandbox: 126 tests pass; smoke test with real n8n passes.

### Not verified yet: needs the team PC

- [x] The known-answer idea again (team PC): the judge kept its candidates (6 of 8 overlapping, F-007 fixed); P2 v2 still judged the method uncovered (truncated abstract, F-006) and Semantic Scholar answered 429 without a key. Fixed in part 2e.
- [ ] E7 with the agent on and off

## Milestone 2, part 2e: OpenAlex fallback, honest attribution (2026-10-15)

Branch `fix/agent-web-and-admission`.

- **OpenAlex** client (`app/external/openalex.py`) and a provider-fallback web search (`app/external/web.py`): OpenAlex first without a Semantic Scholar key, Semantic Scholar first with one; failures recorded per query.
- **Attribution:** the agent only admits papers its own round brought into the pool.
- **Planner input:** 1,200 characters per candidate, abstract first (F-006 cause).
- Sandbox: 129 tests pass; smoke test with real n8n passes (web query answered by the OpenAlex stand-in).

### Not verified yet: needs the team PC

- [x] Semantic Scholar key works from the PC (direct query returned results)
- [x] The known-answer idea with the key and full abstracts (team PC): round 1 searched locally (4 new papers in the pool), round 2 stopped for a correct reason ("Candidate 2 explicitly proposes a predictor that decides whether retrieval is beneficial for each question"); 4 of 7 judged papers overlap, Adaptive-RAG `same`/`same`. The one paper the agent added was DPR (unrelated): fixed in part 2f.
- [ ] OpenAlex from the PC (no key)

## Milestone 2, part 2f: E2 leave-one-out, agent evaluation (2026-10-15)

Branch `feat/m2-e2-and-agent-eval`.

- **Agent admission floor:** papers the agent adds must also reach a reranker score of 0.2 (`NOVELTY_AGENT_MIN_RELEVANCE`). In E8 run 2's pools, 0.2 keeps 62% of the expected papers and 10% of the others.
- **E2, `scripts/e2_leave_one_out.py`:** `build` (Semantic Scholar reference lists, 40 query papers, committed `eval/e2/queries.json`) and `run` (retrieval only, or the whole pipeline).
- **`GET /chunks/{chunk_id}`:** one chunk by id (E2 reads abstracts from the library).
- **Agent on/off** instructions for E7 (eval/README.md).
- Sandbox: E2 `build` and both `run` modes end to end against stand-ins for Semantic Scholar, the models and n8n.

### Not verified yet: needs the team PC

- [x] E2 build and retrieval run (`eval/results/20261015-211913-e2-retrieval`)
- [x] E7 with the agent off and on (`20261015-213516-agent-off`, `20261015-215304-agent-on`)

## Milestone 2, part 2g: analysis of the final runs (2026-10-15)

Branch `eval/m2-final-runs`. Details in `prompts/failure_log.md` (section "M2 final runs") and D30.

- Agent off 16/22 expected papers, agent on 15/22; the gap is first-pass variance, not the agent (it only adds).
- Planner: stopped correctly in 6 of 12 runs; searched needlessly in 2 (F-006 residual, harmless); added useful papers outside the expected list in 2; used the web for the out-of-domain idea and found adjacent real work (diffusion for inverse folding).
- F-008: the false-overlap metric counted that correct web finding as an error.
- E2: 4 to 5% of cited papers in the top 6 (about 1% by chance); misses are mostly background citations (RAG, DPR, REALM, HotpotQA).
- `scripts/order_replay.py`: ordering rules scored on stored pools; the hybrid order stays (D30).

### Next (last measurement round of milestone 2)

- [x] E2 gold from influential citations, and from method citations (`build` re-fetches with the flags, `score` re-scores stored pools); branch `feat/m2-last-round`
- [x] False overlap counts local closest work only (F-008); re-scored agent-on run: 0/12
- [x] 3 ideas whose closest work is outside the library (speculative decoding, LoRA, Chain-of-Verification), with a check that those papers are absent
- [x] Team PC: E2 build + score: influential 4/39 fused, 2/39 hybrid; the pool (top 30) holds only 15 to 18% of cited papers (failure_log)
- [x] Team PC: E2 `--idea-chars 600`: the reranker is still below the fused order, so truncation hypothesis rejected; E2 measures related work, not overlap (failure_log)
- [x] Team PC: E7 agent off and on with the 3 outside-library ideas and out-of-domain: 0/8 both; found F-009 (web papers never reached the reranker) and F-010 (planner took "uses" for "introduces")
- [x] F-009 fixed in code (web papers a round fetched are always reranked); P2 v3 for F-010
- [ ] Team PC: rebuild pp-tools; agent on with P2 v2 (code fix alone) and P2 v3 (fix + prompt), outside-library ideas plus two in-library ideas as a regression check

## Next: milestone 2, part 2

- Reranker (`bge-reranker-v2-m3` on TEI).
- E2 leave-one-out evaluation.
- First prompt iterations from real failures.

## Milestone 3, part 1: PDF parser spike (2026-10-16)

Branch `feat/m3-parser-spike`. Decision D31: Docling (CPU) instead of MinerU.

- `scripts/parse_spike.py`: sends cached library PDFs to docling-serve and records time, pages, tables (grid and caption), pictures and peak memory.
- Team PC: CRAG, Adaptive-RAG and Self-RAG all parsed, in 139 to 314 s, at 3.5 GB peak. CRAG's tables were checked by hand against the PDF (16/16 values of Table 2).
- PyMuPDF baseline: 1 garbled table out of 8 on CRAG, so a layout model is needed.

### Next

- [x] `parser` service in docker-compose (docling-serve CPU v1.30.0), pp-tools client, audit parse job with progress, cache by PDF hash (D32); branch `feat/m3-parser-service`
- [x] Caption repair and split-table merge: 28 → 31 of 37 tables and figures numbered, 0 wrong (D32)
- [x] `POST /audit/{run_id}/parse`, `GET /audit/{run_id}/paper`, `GET /audit/{run_id}/figures/{figure_id}`; SSRF guard on the PDF URL; tests against a docling-serve stand-in (153 passed)
- [x] Team PC: `scripts/audit_parse_check.py`. CRAG fetched from arXiv and parsed through the stack in 179 s; 10/10 tables and figures numbered (Table 6's caption recovered from the PDF text layer); figure PNG served; the second run came from the cache in 2.7 s
- [x] Claim extraction (D33): candidate sentences in code, P4-claim-extractor v1 (gpt-oss-20b, Groq only), validation that drops invented numbers; `POST /audit/{run}/claim-prompts`, `POST|GET /audit/{run}/claims`, `POST /audit/{run}/result`; branch `feat/m3-claims`
- [x] n8n `30-audit` v1: parse job polling → claim prompts → loop over batches (20 s pause) → result (the router stores it)
- [x] First full runs on the team PC (2026-10-16). Run 1: 0 claims, every call failed inside n8n (F-016, inline schema cut at `}}`). Run 2 (`eval/results/20261016-194137-audit-claims/`): 6 of 22 batches done, 28 claims kept; the other 16 batches hit Groq's daily token limit, which today's debugging had used up, and each 429 wasted an attempt (F-017).
- [x] Read the 28 claims by hand: all are faithful to their sentence (no invented numbers, no wrong system or dataset); the CRAG results paragraph gives 13 clean comparisons (system, baseline, margin, metric, dataset, backbone). Four repairable defects, fixed in code (F-018): units ("0.77B") wrongly dropped, a typo in a sentence id, a lowercase "table 1" not linked, margins also listed as values. Replayed on the stored outputs: 28 → 30 claims, 7 more table links, 13 margins marked.
- [x] Rate limits (F-017): a 429 no longer uses an attempt; pp-tools tells n8n how long to wait; a daily-limit 429 switches to gpt-oss-120b (its own 200K tokens a day), and when both are spent the run stops and reports it.
- [x] Third run (`eval/results/20261016-202111-audit-claims/`): **22/22 batches done, 105 claims kept, 0 dropped**, 241-378 s per paper (parse from cache). gpt-oss-20b's daily tokens were spent, so every paper switched to gpt-oss-120b after one 429 (F-017 working); 6 rate limits in total, none cost an attempt, no run stopped. About 21K-32K tokens per paper, 74K for the three.
- [x] Read by hand: every claim is faithful to its sentence. Recall, measured on candidate sentences that contain a real number (not "Section 4.3"): 16 of 25 have a claim; the 9 others are table captions, dataset descriptions and two hyperparameter sentences in Self-RAG (weights 1.0/1.0/0.5, threshold 0.2), i.e. no result sentence was missed. All 13 result margins of CRAG's main paragraph are extracted, 7 of them linked to Table 1; the Self-CRAG ones have no table link because the sentence names none (the number check will search every table).
- [x] Model difference worth knowing: gpt-oss-120b puts a margin only in `comparison.difference`, gpt-oss-20b also lists it as a value; `role: "difference"` (F-018) makes both readable the same way.
- [x] Number check (D35): `app/audit/numbers.py`, deterministic; `POST /audit/{run}/check-numbers`; n8n node "Check numbers" before "Assemble result"; the result carries `number_checks`. Branch `feat/m3-numbers`, 182 tests.
- [x] E3 on the tuning papers (`scripts/e3_number_check.py`, offline, no tokens): see D35. Seven rules came from E3 failures: margins absolute-only when the text says so, exact precision, short headers as whole words, ablation rows attached to their method, a row's own label preferred to an inherited one, stacked sub-tables kept apart, row names removed before matching columns.
- [x] Team PC: held-out run on two unseen papers (RAPTOR 2401.18059v1, FLARE 2305.06983v2) through n8n, number check included (end to end: 3 supported, 2 contradicted, 11 unverifiable on RAPTOR). Blind E3: synthetic 0 false alarms, 92% caught; real claims 2 false alarms (F-020).
- [x] Fixes from the held-out run (F-019, F-020): contradiction needs every named detail, strict system names, margins written as values, spaced decimals, captions as mentions. After: 0 false alarms and 0 accepted errors on both sets; real-claim coverage on RAPTOR is low (3 of 16 decided). 186 tests.
- [x] Figure check (D36): `app/audit/figures.py`, P5-figure-checker v1 (qwen/qwen3.8-27b vision on Groq), `POST /audit/{run}/figures/next`, `POST /audit/{run}/figure-checks` (+ `/error`), `GET /audit/{run}/figure-checks`; n8n loop "Next figure" → "Check figure" after "Check numbers"; the result carries `figure_checks`. Branch `feat/m3-figures`, 193 tests.
- [x] E4 gold set: 48 claims about 14 figures (`eval/e4/figure_claims.json`), `scripts/e4_figure_check.py` (the workflow's own prompt, request and validation)
- [x] Team PC, first run: E4 22/22 verdicts right but 7 of 14 calls failed (F-021); audits of CRAG (3/3 figures) and Self-RAG (3/4) through n8n with the figure check, end to end
- [x] P5 v2 (output shape in the prompt), output budget 900, request variants A/B/C; 195 tests
- [x] Team PC: E4 on variants A and B (C not reached: daily limit). A chosen (D36): 34/34 verdicts right, failures retried; empty answers now count as failures
- [x] First attempt at the final run (2026-10-17 00:06): the vision model's 200K tokens a day were already spent by the variant comparison. E4 got 1 figure (5/5 right). The audits showed the designed degradation: one 429, then the figure check stopped with "Groq daily token limit reached on qwen/qwen3.8-27b" in the result, while claims and the number check completed (CRAG: 13 margins supported).
- [ ] Team PC: final E4 with variant A and 3 attempts, and the two audits, once the vision tokens are free again (Groq counts them over a rolling 24 hours); then merge PR #27
- [x] Citation check (D37): `app/audit/citations.py` (marker → bibliography entry → library paper, in code), retrieval of the cited paper's passages with reranking, P6-citation-checker v1; `POST /audit/{run}/citations/next`, `POST /audit/{run}/citation-checks` (+ `/error`), `GET /audit/{run}/citation-checks`; n8n loop "Next citations" → "Check citations" after the figures; the result carries `citation_checks`. Resolution measured on the five audited papers: 68/69 markers to a reference, 28 in the library. Branch `feat/m3-citations`, 207 tests.
- [x] Team PC, first citation audit (CRAG): 15 citations placed (7 in the library, 8 outside), passages retrieved and reranked, 4 pairs judged (2 supported, 2 not found) before the gpt-oss daily limit stopped the run cleanly; Adaptive-RAG and FLARE could not even extract claims (limit). One "not found" was a passage cut before the relevant sentence (F-022, fixed: best-matching window). 209 tests.
- [ ] Team PC: audits of CRAG, Adaptive-RAG and FLARE with the citation check, once the gpt-oss tokens are free
- [ ] E5: gold citation pairs (faithful, and unfaithful versions), retrieval measured apart from the verdicts

