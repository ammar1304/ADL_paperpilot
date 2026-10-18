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

## D12. LLM providers and models (all free)

- **Date:** 2026-10-12, before milestone 2
- **Decision:**

  | Role | Model | Provider |
  | --- | --- | --- |
  | Decomposer, query generator, claim extractor | `openai/gpt-oss-20b` | Groq free tier |
  | Novelty judge, audit judge, citation checker | `openai/gpt-oss-120b` | Groq free tier |
  | Figure reader (vision) | `qwen/qwen3.8-27b` | Groq free tier |
  | Fallback when Groq is rate-limited | `gemini-3.5-flash-lite` | Gemini API free tier |

- **Why Groq first:**
  - Groq publishes its free-tier limits; Google does not, and cut its free limits sharply in December 2025.
  - Both gpt-oss models support strict JSON-schema output on Groq, which the agents' contracts rely on.
  - Groq does not retain inference data by default.
  - Each model has its own quota, so splitting roles spreads the load.
- **Why Gemini only as a fallback:** Google states that free-tier content is used to improve its products. That is acceptable for public arXiv papers. Never send an unpublished draft to the Gemini free tier.
- **Constraint:** about 8,000 tokens per minute and 200,000 tokens per day per gpt-oss model (check your own limits under Settings, then Limits in the Groq console). This means caching every LLM response, backoff and fallback in n8n, short prompts, top-ranked chunks only, and evaluations spread over days. Each teammate uses their own key for their own development; keys are never pooled onto one pipeline to get around the limits.
- **Sources:** [Groq rate limits](https://console.groq.com/docs/rate-limits), [Groq structured outputs](https://console.groq.com/docs/structured-outputs), [Groq data retention](https://console.groq.com/docs/your-data), [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing).

## D13. Domain and corpus selection

- **Decision:** arXiv papers on retrieval-augmented generation for question answering, 2020 onward, in cs.CL, cs.IR, cs.AI or cs.LG. Up to 500 papers by arXiv relevance; 50 get their full text chunked, chosen by relevance with a per-year cap and then reviewed by the team. The query is in `app/corpus/select.py`.
- **Why:**
  - The team studies RAG, so it can judge method overlap and label gold claims.
  - Papers report results in tables (EM, F1, Recall@k), so the audit has numbers to check.
  - The corpus is open access on arXiv, so the citation check works.
  - The field is narrow enough that a few hundred papers give meaningful coverage.
- **Reproducibility:** `corpus/manifest.csv` (ids, versions, titles) is committed, so everyone indexes the same papers at the same versions. arXiv metadata is CC0; PDFs and extracted text are cached locally and never committed, since arXiv allows research use but not re-hosting.
- **arXiv etiquette:** one request every 3 seconds, one connection, a User-Agent with a contact address (`ARXIV_CONTACT_EMAIL`), as the [arXiv API terms](https://info.arxiv.org/help/api/tou.html) ask.

## D14. Embedding model: BGE-M3

- **Decision:** `BAAI/bge-m3` (568M parameters, dense vectors) for both chunking strategies.
- **Why:**
  - Free, runs locally on CPU, no rate limits, no data leaves the machine.
  - Multilingual, so French queries work if the team keeps them in scope.
  - Its 8192-token limit is far above our 512-token chunks, so nothing is truncated.
  - It can also produce sparse vectors, if hybrid search turns out to be needed (not used yet).
- **Still to do:** compare it against an English-only model on the dev queries (planned in the design). Keep BGE-M3 only if it does not lose on English.

## D15. Serve embeddings with Text Embeddings Inference (TEI)

- **Decision:** a separate `embedder` container running Hugging Face TEI (`cpu-1.9`); pp-tools calls `POST /embed` over HTTP.
- **Alternatives:** sentence-transformers inside pp-tools.
- **Why:** pp-tools stays a small image without PyTorch (the model alone is about 2.3 GB). The model loads once and stays loaded. The same API serves the reranker in milestone 2. TEI's source confirms BGE-M3 support (XLM-RoBERTa architecture) and its `/health` endpoint.
- **Memory (found on a team PC, 2026-10-14):** with TEI's default `--max-batch-tokens 16384`, warm-up on CPU used 8.8 of 9.6 GB of Docker memory and the container was killed in a loop. We set `MAX_BATCH_TOKENS=2048`: it is also the longest accepted input, and our chunks are about 700 model tokens, so nothing is truncated.
- **Cost:** the first start downloads the model, taking a few minutes. pp-tools reports the embedder in `/health` but does not require it, so run tracking works while the model loads. Apple Silicon needs the `cpu-arm64-1.9` image (`EMBEDDER_IMAGE` in `.env`).

## D16. Plain PDF text first, MinerU later

- **Decision:** milestone 1 extracts full text with pdfminer.six. Structure (sections, table blocks, figure captions) is recovered with documented heuristics in `app/corpus/structure.py`. MinerU's layout model replaces this in milestone 3.
- **Why:** it unblocks the chunking experiment now, without a GPU or the MinerU service. It also creates the planned ablation for free: the same experiments with plain extraction and with MinerU show what layout parsing adds.
- **Found while building:** pdfminer's default ordering read the right column before the left on two-column pages. The extractor now sorts text boxes itself, left column top to bottom and then right column (`_reading_key`).
- **Known limits:** table cells come out as a flat run of values joined by `|`, with no cell coordinates (MinerU will provide those). Heading detection misses unnumbered headings with unusual names. Appendices after the references are skipped. `build_report.json` lists papers where no heading was found.

## D17. Chunking experiment design

- **Decision:** both strategies index the same text (title, abstract, body; references and acknowledgements excluded) with the same size budget (512 approximate tokens). A uses 64 tokens of overlap. The difference between them is the boundaries (fixed windows against sections, tables and captions) and B's short header naming the paper and section.
- **Why:** with the same text and the same budget, a difference in Recall@k can be attributed to structure-awareness rather than to more text or bigger chunks.
- **Token counting:** a regex counts words and punctuation marks (`app/textutil.py`), because pp-tools does not ship the model's tokenizer. It undercounts BGE-M3 subword tokens by roughly 20 to 40%, which is harmless here because both strategies use the same ruler and every chunk stays far below 8192 model tokens.

## D18. Long builds run as background jobs that n8n polls

- **Decision:** `POST /corpus/build` queues a job and answers 202 with a `job_id`. The `10-ingest-papers` workflow replies to its caller at once, then polls `GET /jobs/{id}` every 15 seconds until `done` or `failed`, with a 4-hour cap (raised from 1 hour after the first full build took 88 minutes on CPU).
- **Why:** a build takes minutes, longer than any sensible HTTP timeout. Jobs are rows in Postgres, so their status survives n8n restarts. A unique index allows one active build at a time; a second request gets 409. Jobs left running when pp-tools restarts are marked failed at startup.

## D19. Retrieval evaluation through the deployed API

- **Decision:** `app.eval.retrieval` queries the running service's `/search` by default (`--direct` bypasses it). Metrics are paper-level Recall@k and MRR. Every run writes its config (git commit, manifest hash, query file hash, chunk sizes) next to its results.
- **Why:**
  - The numbers describe exactly what the novelty branch will receive.
  - Paper-level relevance is what novelty search needs, and it can be labelled reliably by hand.
  - Queries are written by teammates from a generated worksheet, not by an LLM, so milestone 1 needs no API key and the queries are not biased toward one model's phrasing.
  - Whole papers are assigned to dev or test, so tuning on dev cannot leak into test.

## D20. Planned for milestone 2: an agentic search loop

- **Decision:** the novelty branch's search agent may run up to 3 search rounds. After each round it judges which facets are poorly covered and writes new queries, then decides when to stop.
- **Why:** it is the part of the system where autonomy helps, since a single search often misses work described in different words. It also answers "where is the agency?" honestly. The rest of the pipeline stays a fixed order on purpose, so that it can be tested.

## D21. LLM calls run in n8n's own AI nodes

- **Date:** 2026-10-14, milestone 2
- **Decision:** each LLM step is an n8n **Basic LLM Chain** node (v1.9) with a **Groq Chat Model** as primary, a **Google Gemini Chat Model** as fallback (the chain's built-in "fallback model"), and a **Structured Output Parser** holding the JSON schema. pp-tools never calls an LLM.
- **Alternatives:** calling Groq from Python inside pp-tools.
- **Why:**
  - The LLM, its model, its fallback and its output schema are visible on the n8n canvas, which is what a low-code automation course grades and what the demo shows.
  - Retries (3 tries, 5 s apart), fallback and output parsing come with the nodes.
  - pp-tools keeps only the deterministic parts (validation, retrieval, the verifier), so they stay unit-testable.
- **Credentials:** the entrypoint imports them from `GROQ_API_KEY` and `GEMINI_API_KEY` (encrypted with `N8N_ENCRYPTION_KEY`, fixed ids referenced by the workflow), then removes both keys from the environment before n8n starts, so no expression can read them (D6).
- **Found while building:** n8n's parser expects the model to answer `{"output": {...}}`, so prompt examples use that wrapper. n8n doubles braces in system messages, so JSON examples in prompts are safe. The Groq SDK honours `GROQ_BASE_URL`, which lets tests point the real Groq node at a local stand-in model.
- **Limit:** n8n does not report token usage per run in a form we store yet, so `runs.tokens_used` stays 0 for now.

## D22. Prompts are files, rendered by pp-tools

- **Decision:** prompts live in `prompts/<name>/v<n>.md`, with the active version in `prompts/active.json`. n8n calls `POST /prompts/{name}/render` (or `/novelty/{run_id}/judge-prompt`), and the chain node uses the returned `system` and `user` text.
- **Why:**
  - One source of truth for the graded prompt library: what runs is exactly what is versioned in git.
  - Every run step records the prompt version.
  - Switching or rolling back a version is one line in `active.json`, with no restart.
  - A missing template variable fails loudly instead of sending a half-filled prompt.
- **Cost:** the JSON schemas live in two places, the n8n parser node and the pp-tools contract. A test (`test_prompts.py`) checks that they agree.

## D23. The verifier reads the retrieved set from the database

- **Decision:** the verifier compares the judge's paper ids and chunk ids with the `retrieve` step stored in `run_steps`, not with anything n8n passes along. One retry with the rejection reasons as feedback; anything still wrong is reported as `unverified`, never dropped silently.
- **Why:** the check cannot be bypassed by a payload, and every decision is stored for audit. The retry closes the loop between agents (verifier to judge) without letting it run forever.

## D24. Multi-query retrieval with reciprocal rank fusion

- **Decision:** each decomposer query searches the knowledge base (20 chunks). Papers are scored by reciprocal rank fusion (k = 60) across queries. The top 6 papers go to the judge with their 2 best chunks (900 characters each).
- **Why:** one query rarely covers every facet. Fusion rewards papers that several facet queries find, without tuning weights. The caps keep the judge prompt around 3,000 tokens, inside Groq's 8,000-tokens-per-minute free limit (D12).
- **Next:** the agentic search loop (D20) adds rounds when facets are poorly covered, and a reranker can replace the raw fusion order.

## D25. The novelty report is assembled by code; the LLM summary is secondary

- **Context:** the first real runs (`prompts/failure_log.md` F-001 to F-003) showed the judge's labels were right but its prose overreached: papers different on every facet were shown as "closest work", and missing evidence was described as "uncovered".
- **Decision:**
  - `closest_work` holds only papers with at least one `same` or `partial` label, ranked by code (task and method first). The others go to `checked_not_close`, never hidden.
  - A `summary` is written by code from the verified labels and the search coverage. The judge's summary is kept as `summary_llm_written`.
  - P3 v2 receives the search scope (papers, full texts, queries) and labelled passages, with explicit rules separating `unknown` from "not covered".
- **Why:** every sentence a user reads first is now backed by verified labels; the LLM still does the judging, which is what it is good at. The report is also easier to evaluate: overlap found or not is a boolean.
- **Trade-off:** the code summary is plainer than the LLM's.

## D26. Hand-picked additions to the corpus, checked by title

- **Context:** the arXiv relevance search returned mostly 2024 to 2026 papers; Self-RAG, RAG, DPR and FiD were missing, and the first known-answer test could not find Self-RAG.
- **Decision:** `corpus/additions.csv` lists foundational papers with their expected titles; `app.corpus.add` fetches each id and refuses it if the real title differs (similarity below 0.85). The additions are recorded in `selection.json`.
- **Why:** the ids were written by hand (and partly from memory); the title check turns a typo into a visible error instead of a wrong paper in the knowledge base.

## D27. Rebuilds reuse stored vectors

- **Context:** the first full build took 88 minutes on CPU, nearly all embedding. Every manifest change would cost that again.
- **Decision:** before indexing, the build reads the vectors already in Qdrant for the same chunk ids and reuses those whose stored text is identical and whose `embedding_model` (from TEI's `/info`) matches. Only new or changed chunks are embedded. Points from earlier builds have no model recorded and are treated as BGE-M3, the only model used so far.
- **Why:** adding 30 papers costs minutes, not an hour and a half. Reuse is exact (same text, same model), so results are identical to a full rebuild.
- **Off switch:** `REUSE_VECTORS=false`.

## D28. A cross-encoder reranker over a wider fused pool

- **Context:** E7 run 1 (`prompts/failure_log.md`) showed that the judge places every expected paper it sees in closest work, but only 25 of 57 expected papers reached it (as run, over the 25 distinct query sets). For "adaptive retrieval", Self-RAG, SKR and Mallen et al. never did. Better decomposer queries (P1 v2) did not measurably help.
- **Decision:**
  - Each query retrieves 50 chunks instead of 20, and fusion keeps a pool of 30 papers.
  - A cross-encoder, `BAAI/bge-reranker-base` on a second TEI container, scores the idea text against each pool paper's two best chunks (1,200 characters each). A paper's score is its best chunk's. The 6 best papers go to the judge, and their evidence is the chunks the reranker scored highest.
  - If the reranker is down or fails, the fused order is kept and the step records why (`ranking_note`); a novelty run never fails because of it.
  - `NOVELTY_RERANK`, `NOVELTY_POOL_PAPERS` and `NOVELTY_IDEA_QUERY` (also search with the idea text) are switches, so their effect can be measured.
- **Why a cross-encoder:** the embedding model encodes the idea and a passage separately, so two passages sharing most words get similar scores even when one "always retrieves" and the other "decides whether to retrieve". A cross-encoder reads both together. It costs one model call per pair, which is why it only re-orders a small pool.
- **Why bge-reranker-base and not bge-reranker-v2-m3 (the plan in D15):** on the team PC's CPU, BGE-M3 (an XLM-RoBERTa large model) embedded about one chunk per second; v2-m3 is the same size, so 60 pairs would add about a minute per run. The base model is about three times smaller (about 1 GB of RAM) and is listed by TEI as a supported reranker; v2-m3 is not listed. If E7 shows the base model is not good enough, v2-m3 is a one-line change (`RERANKER_MODEL`).
- **How it is measured:** `scripts/retrieval_ab.py` replays the decomposer queries stored from E7 run 1 through `POST /novelty/rank` under four settings (fused, fused plus the idea as a query, reranked, both). No LLM is called, so every setting sees exactly the same queries.

**E8 run 1 (2026-10-15, team PC, 25 stored query sets, 57 expected papers):**

| Setting | Reaching the judge (top 6) | In the pool (top 30) | MRR | Seconds per ranking |
| --- | --- | --- | --- | --- |
| as run in E7 (20 chunks per query, no reranker) | 25 | - | - | - |
| fused | 24 | 45 | 0.29 | 2.6 |
| fused + idea as a query | 26 | 50 | 0.33 | 2.8 |
| reranked | 30 | 45 | 0.40 | 18.7 |
| reranked + idea as a query | **34** | **50** | **0.41** | 18.9 |

- Reranking with the idea as an extra query raised expected papers reaching the judge from 25 to 34 of 57. Adding the idea as a query raised the pool ceiling from 45 to 50. Both are now defaults (`NOVELTY_IDEA_QUERY=true`).
- Per idea, adaptive retrieval went from 7 to 18 of 30, Self-RAG and HyDE reached 3/3 and 6/6, but **iterative rewriting fell from 4 to 1 of 12**: IRCoT moved from ranks 2-6 to 7-10 and FLARE to 22-30. Their best vector matches were results and model sections, which do not say what the papers do; the reranker scored those passages, not the papers' contributions.
- RAM: the reranker used 3.8 GB with `MAX_BATCH_TOKENS=4096`, the embedder 3.4 GB, about 8 GB of the 9.6 GB available. The default is now 2048.
- **Follow-up (E8 run 2):** two options, measured from the same pools: the reranker also scores each paper's abstract (`NOVELTY_RERANK_ABSTRACT`), and a hybrid order, reciprocal rank fusion of the fused rank and the rerank rank (`NOVELTY_ORDER=hybrid`), so a paper both stages rank well stays high.

**E8 run 2 (2026-10-15, same 25 query sets, orders derived from the same pools):**

| Method | Reaching the judge (top 6) | MRR | Adaptive retrieval | Iterative rewriting |
| --- | --- | --- | --- | --- |
| fused + idea | 26 | 0.33 | 8/30 | 4/12 |
| reranked + idea | 34 | 0.41 | 18/30 | 1/12 |
| **hybrid + idea** | **37** | 0.39 | **18/30** | **4/12** |
| reranked + idea + abstract | 29 | 0.41 | 13/30 | 1/12 |
| hybrid + idea + abstract | 34 | 0.37 | 15/30 | 4/12 |

- **Decision: hybrid order with the idea as a query is the default** (`NOVELTY_ORDER=hybrid`). It keeps the reranker's gain on adaptive retrieval and the vector search's ranking of IRCoT, and gets the most expected papers to the judge (37 of 57, from 25 before the reranker). Its MRR is slightly lower than pure reranking (0.39 vs 0.41): the first expected paper is sometimes one place lower, but more of them reach the top 6, which is what the judge sees.
- **Scoring abstracts made things worse** (29 and 34): with two scored passages per paper, the abstract replaced the second-best chunk, and abstracts written in general terms outscored specific passages (Mallen et al. dropped from rank 2 to 15-20 in several query sets). Left off.
- **Caveat:** the 6 ideas were used both to choose the setting and to measure it, so 37/57 is optimistic. The leave-one-out evaluation (E2) will measure it on papers not used for tuning.
- RAM with 2048 batch tokens: reranker 3.5 GB (3.8 GB before), embedder 3.4 GB.

## D29. The search agent: an LLM planner over two search tools, with limits in code

- **Context:** D20 planned an agentic search loop. E7 and E8 showed why it is needed: one set of queries misses papers described in other words (3 of 57 expected papers are never in the pool of 30), and papers outside the 531-paper library cannot be found at all.
- **Decision:** after the first retrieval, a loop in n8n (`20-novelty`):
  1. **Render planner prompt** (pp-tools): P2 with the idea, its facets, the best 8 candidates (title, source, rerank score, a passage), every search made so far with how many new papers it found, and the round budget.
  2. **Plan search** (LLM, gpt-oss-20b, Gemini fallback, JSON parser): `stop`, or `search` with 1 to 3 queries, each with a tool: `local` (the library, by meaning) or `web` (Semantic Scholar, titles and abstracts), and the facets it targets.
  3. **Apply plan** (pp-tools): runs the queries, fuses Semantic Scholar results with the local ones (each web query is one more ranked list in the reciprocal rank fusion, its papers represented by their abstracts), re-ranks the whole pool with the hybrid order (D28), and stores a new `retrieve` step and a `plan` step.
  4. **Search again?** loops back while pp-tools answers `continue`.
- **What the LLM decides:** whether to search, what is missing, the queries, and which tool each uses. **What code decides, whatever the LLM says:** at most `NOVELTY_MAX_SEARCH_ROUNDS` rounds (default 2; 0 turns the agent off), at most 3 queries a round, no repeated query, stop when a round adds no new paper to the pool, an invalid plan ends the search instead of failing the run, and a Semantic Scholar failure is recorded per query while the local queries still run. n8n's loop also stops after 4 iterations as a second guard.
- **Identity of external papers:** a Semantic Scholar paper with an arXiv id becomes `arxiv:<id>`, so a paper already in the library is one candidate with source `local+semantic_scholar`; others are `s2:<paperId>`. Their evidence is the abstract (`<id>#abs`), and the verifier accepts it only if that paper was retrieved, like any local chunk.
- **Why this split:** the LLM does what needs judgement over meaning (is the method covered? which vocabulary would other papers use? is this outside the library?). Code keeps cost, termination and evidence under control. Every decision and its reason is stored (`plan` steps) and returned in the result (`search_agent`), so a run can be audited.
- **Why Semantic Scholar:** free, covers about 200 million papers across venues (not only arXiv), and returns abstracts and arXiv ids in one call. Without an API key requests share a public pool and are often refused; with a free key the limit is 1 request per second. pp-tools spaces requests by 1.1 s and retries 429s.
- **How it is measured:** `scripts/prompt_ab.py` reports agent search rounds and expected papers the agent added; comparing runs with `NOVELTY_MAX_SEARCH_ROUNDS=0` and `2` isolates its effect. The leave-one-out evaluation (E2) will also test it on papers whose references are partly outside the library.

**First real run and revision (2026-10-15).** On the team PC the first version made the result worse for the known-answer idea: 1 overlapping paper instead of 4, because the agent's queries changed the fused ranks of every paper and pushed Self-RAG and SKR out of the judge's top 6 (`prompts/failure_log.md` F-007); its plans were also too literal (F-006). Revised rule: **the agent only adds.** The previous candidates stay with their evidence, and at most `NOVELTY_AGENT_EXTRA_PAPERS` (2) new papers are admitted over all rounds, each with a reranker score at least that of the weakest candidate already shown. A search can therefore never make the judge's evidence worse, only add to it, at the cost of up to two more papers in the judge prompt. P2 v2 addresses the planning failures.

**Second real run and revision (2026-10-15).** With the "agent only adds" rule the judge's evidence no longer got worse (6 overlapping papers of 8). Two more changes:
- **Web search has two providers.** Without an API key, Semantic Scholar's shared pool refused the agent's request (HTTP 429). OpenAlex answers keyless queries (10 requests per second) and returns abstracts and DOIs, from which arXiv ids are recovered (`10.48550/arxiv.<id>`), so a paper already in the library is still one candidate. Order: Semantic Scholar first when `SEMANTIC_SCHOLAR_API_KEY` is set, OpenAlex first otherwise; a failure falls back to the other and is recorded (`fallback_from`). Each web query records which provider answered.
- **Attribution:** the agent may only admit papers that its own round brought into the pool, so `new_candidates` means "found by the agent". In that run the result had credited the agent with two papers that were already in the pool below the top 6.

## D30. The hybrid order stays; ordering rules are replayed offline before any change

- **Context:** in one agent-on run Self-RAG had the highest reranker score in the pool (0.82) but fused rank 12, and the hybrid order (D28) left it 7th, outside the judge's top 6. Two fixes looked plausible: a smaller RRF constant k in the hybrid order (one strong rank counts more), or a guaranteed slot for any paper the reranker scores above a threshold.
- **How it was tested:** every stored pool carries fused rank, rerank rank and rerank score, so `scripts/order_replay.py` re-scores any rule without running retrieval: E8's 25 pools (57 expected papers), the 10 first-pass pools of the agent-off E7 run (22), and E2's 40 pools (273 cited papers).

| Rule | E8 top 6 | E7 top 6 | E2 top 6 |
| --- | --- | --- | --- |
| fused | 26/57 | 9/22 | 14/273 |
| reranked | 34/57 | 16/22 | 10/273 |
| hybrid, k=60 (current) | **37/57** | **16/22** | 11/273 |
| hybrid, k=20 | 36/57 | 16/22 | 11/273 |
| hybrid, k=10 | 35/57 | 16/22 | 12/273 |
| guaranteed slot, rerank ≥ 0.5 | 34/57 | 16/22 | 10/273 |
| guaranteed slot, rerank ≥ 0.7 | 34/57 | 16/22 | 10/273 |

  A third variant, showing up to 2 extra papers (8 instead of 6) when they score above both 0.2 and the weakest of the 6, gains 3/57 (E8) and 2/22 (E7) at the cost of 16 and 9 extra papers shown, of which 13 and 7 are not expected.
- **Decision:** keep hybrid with k=60. The Self-RAG case is a boundary case; every alternative that fixes it loses more elsewhere or mostly adds noise to the judge prompt. Any future ordering change must first be replayed on the stored pools.
- **Limit:** E8 and E7 share the same 5 ideas, so they are not independent; E2's gold is loose (citations, D31 pending). The differences between the hybrid variants (1 or 2 papers) are within noise; only the guaranteed-slot loss on E8 is consistent.

## D31. PDF layout parsing: Docling (CPU), not MinerU

- **Date:** 2026-10-16, start of milestone 3. Replaces the MinerU plan in D16.
- **Context:** the audit has to read a paper's tables as cell grids, so a number in the text can be checked against a cell. It also has to read its figures as images, so a vision model can check claims about them. The team PC has no GPU and about 9.6 GB of memory available to Docker, of which the running stack already uses about 5 GB.
- **Options considered:**
  - **MinerU:** strongest on scientific tables, but its pipeline lists a 4 GB VRAM floor and 16 GB of system RAM, and its better modes need a GPU.
  - **Rule-based table detection (PyMuPDF `find_tables`):** no models.
  - **Docling:** a layout model plus TableFormer, a CPU image, MIT licence.
- **Measured:**
  - **PyMuPDF.** On CRAG (2401.15884), with its 8 captioned tables, the "lines" strategy found 1 table, and it was garbled. arXiv tables have only horizontal rules. The "text" strategy called almost every page a table (16).
  - **Docling** (`docling-serve-cpu:v1.30.0`, OCR off, TableFormer accurate) on the team PC: `eval/m3_spike/20261016-154255/summary.md`.

    | Paper | Pages | Time | Tables | Pictures |
    | --- | --- | --- | --- | --- |
    | CRAG | 16 | 314 s (includes the first model load) | 7 | 3 |
    | Adaptive-RAG | 15 | 139 s | 9 | 5 |
    | Self-RAG | 30 | 169 s | 8 | 6 |

    Peak container memory was 3.5 GB.
  - **Table quality, checked by hand on CRAG:**
    - All 6 result tables came out as correct grids, with row labels, column headers and the metric in the header ("PopQA (Accuracy)").
    - Spot check against the PDF text: Table 2's 16 values matched in order (16/16), and so did two rows of Table 1.
    - Figures come out as clean crops at 2× scale, readable by eye and so by a vision model.
- **Decision:** Docling, as a separate `parser` service (the docling-serve CPU image), called by pp-tools. It runs as an asynchronous job, like the corpus build, because a paper takes 2 to 5 minutes.
- **Known limits, from the spike:**
  - **Captions are sometimes missing or misattached.** Self-RAG: 4 of 8 tables had no caption, and one table received a figure's caption. CRAG: 1 of 7 missing. Adaptive-RAG: 1 of 9. Docling's own table order also differs from the paper's numbering (Docling's "Table 7" is the paper's Table 11), so claims must be linked to tables through the caption number, never through the order.
    - Fix: a caption-repair step (the nearest "Table N" caption on the same page), measured on these papers.
  - **Some prompt boxes or appendix listings are labelled as tables, and some region crops overlap a table.** Harmless for the number check, which starts from the claims.
  - **CPU time.** An audit of a 15 to 30-page paper takes about 3 to 5 minutes. Acceptable for an asynchronous job with progress shown; the result is cached per PDF hash.
- **Sources:** [MinerU vs Docling vs Marker](https://builderai.tools/blog/pdf-parsing-for-rag-mineru-docling-marker-compared), [docling-serve](https://github.com/docling-project/docling-serve) and its [usage options](https://github.com/docling-project/docling-serve/blob/main/docs/usage.md).

## D32. The parsed paper: ids from captions, caption repair, cache by PDF hash

- **Date:** 2026-10-16.
- **Decision:** pp-tools turns Docling's document into a flat `Paper` (`app/audit/paper.py`), stored per SHA-256 of the PDF (`<cache>/audit/<sha>/`). It holds:
  - blocks in reading order (`b:N`, with section and page);
  - tables (`table:N`, with cell grid and header rows);
  - figures (`figure:N`, with a PNG crop);
  - the bibliography entries.
- **Ids come from the paper's own caption numbers**, never from Docling's order: claims say "Table 3", and Docling's 7th table was CRAG's Table 11 (D31). An item whose number cannot be found gets `table:uN` rather than a guess.
- **Caption repair**, most reliable source first:
  1. Docling's own link, if it names the right kind ("Table" for a table).
  2. The nearest unclaimed caption of the right kind on the same page, within 120 pt.
  3. For a picture, a caption Docling put inside the picture region.
  4. The PDF's text layer (pdfminer, already a dependency), used only when exactly one caption number of that kind is unused on that page.
- **Split tables:** an uncaptioned table directly below a numbered one, as wide and with the same number of columns, is merged into it (Adaptive-RAG's Table 2 came out in two halves).
- **Measured on the three spike papers** (37 tables and figures, numbers checked against the captions in the PDF text):

  | | Numbered | Wrong number |
  | --- | --- | --- |
  | Docling's caption links only | 28 | 0 |
  | With repair | 31 | 0 |

  The 6 left unnumbered are uncaptioned in the PDF (appendix images) or Docling boxes that are not tables.
- **Security:** the PDF URL comes from the user, so pp-tools refuses non-http(s) URLs and addresses that resolve to private, loopback or link-local IPs, which would include the stack's own services. It checks every redirect hop by hand, caps the size (`AUDIT_MAX_PDF_MB`, 30) and requires the `%PDF` signature.
- **Why a cache:** parsing costs 2 to 5 CPU-minutes. An audit rerun, an evaluation, or two runs on the same draft must not pay that again.

## D33. Claim extraction: code picks the sentences, the LLM structures them, code checks the numbers

- **Date:** 2026-10-16. Audit step 3.
- **Decision:** a claim is extracted in three stages (`app/audit/claims.py`, prompt `P4-claim-extractor`).
  1. **Code selects the candidate sentences:** those with a decimal or percent number, a table or figure reference, or a citation, outside References, Acknowledgements and Limitations.
     - On the spike papers: CRAG 79 of 304 sentences, Adaptive-RAG 71 of 212, Self-RAG 123 of 507.
     - That is about 3,000 to 4,700 tokens, so 3 or 4 calls per paper.
  2. **The LLM (gpt-oss-20b) turns each batch of about 700 counted tokens into typed claims:**
     - five types: `result`, `comparison`, `figure`, `citation`, `other`;
     - values copied as written, with their system, dataset, metric and setting;
     - table and figure references only when the sentence names them;
     - citation markers as written.
  3. **Code validates the output:**
     - a claim whose numbers (values or stated difference) do not appear in its sentence is dropped, because an extractor that rounds or computes would hide exactly the errors the audit looks for;
     - unknown sentence ids are dropped;
     - references to tables or figures the paper does not contain are kept but flagged.
- **Why this split:** finding "is there a number here" is exact and free in code. Deciding which system and dataset a number belongs to, and whether a sentence is a comparison, needs reading. Validating the reading against the source text stops the LLM from being the weak link.
- **Rate limits and the loop:** pp-tools, not n8n, keeps the loop's state (`POST /audit/{run}/claims/next`). n8n asks for the next batch, waits 65 seconds, calls the LLM and saves the output. An LLM error goes back to "next", which serves the same batch again; a batch is skipped after 3 attempts, and a rerun resumes where it stopped (F-012).
  - Groq's free tier refuses any single request above 8,000 tokens per minute, and it counts the input plus the output budget.
  - The first version (batches of 1,800 tokens, 4,096 reserved for the answer) was refused on every call (F-011).
  - A batch is now 700 counted tokens (about 1,000 real) with 3,000 reserved for the answer, about 6,500 per request. A test keeps the budget under 7,000.
  - gpt-oss-20b's hidden reasoning tokens count too, so one call uses 6,000 to 7,000 tokens, and calls must be a minute apart (F-012). n8n's own retries cannot do this: they wait at most 5 seconds.
  - Cost: 5 to 9 calls and 7 to 12 minutes per paper. The daily cap (about 200,000 tokens per model) allows roughly 4 to 6 papers a day, so evaluations must be spread over days.
- **Privacy:** the audit's LLM nodes are Groq only, with no Gemini fallback. A draft is unpublished work, and the Gemini free tier uses content to improve Google's products (D12). If Groq is down, the audit fails visibly instead of sending the draft elsewhere.

## D34. The audit's claim extractor calls Groq's API directly, with low reasoning and a strict schema

- **Date:** 2026-10-16. An exception to D21 (LLM calls in n8n's AI nodes).
- **Context:** gpt-oss-20b reasons before answering, and reasoning tokens count against the output budget and the 8,000 tokens per minute. With default reasoning it used its whole budget and returned an empty answer (F-013). n8n's Groq chat-model node cannot set `reasoning_effort`, and its output parser adds about 700 tokens of format instructions.
- **Decision:** the "Extract claims" node is an n8n HTTP Request to `https://api.groq.com/openai/v1/chat/completions`. It authenticates with a header credential that `n8n/entrypoint.sh` creates from `GROQ_API_KEY`, so the key never appears in the workflow (F-014: the `groqApi` credential type only works in the Groq chat-model node). It sends:
  - `reasoning_effort: "low"` and `include_reasoning: false`;
  - `max_completion_tokens: 2500`;
  - `response_format` with a strict JSON schema: every field required, optional ones nullable, `additionalProperties: false`.
- **Fallback model and rate limits:** `openai/gpt-oss-120b` when gpt-oss-20b's daily tokens are spent (Groq's limits are per model). A 429 does not use one of a batch's attempts; pp-tools tells n8n how long to wait, or stops the run when both models are spent (F-017).
- **Who builds the body:** pp-tools returns the full request with each batch (`claims_request()`), and the node posts it unchanged. The schema was first written inline in the node; n8n cut the expression at the first `}}` and every call failed (F-016).
- **Why it is still the automation platform's job:** n8n makes the call, handles its errors (error output to "Record LLM error") and controls the pacing. pp-tools still renders the prompt from the library and validates the output.
- **Measured (probe, CRAG batch 0):** 2,586 tokens in total (2,351 in, 235 out, of which 133 reasoning), 1.2 s, valid JSON. Calls are therefore paced 30 s apart instead of 65 s.
- **What the strict schema buys:** the decoder cannot produce JSON outside the schema, so the "doesn't fit required format" failures disappear by construction. pp-tools' own validation (numbers must appear in the sentence) stays, because a schema cannot check meaning.
- **Trade-off:** the novelty branch keeps the LLM-chain nodes, with Gemini as a fallback, which the audit must not use (D12). The two styles are documented side by side; moving the novelty branch is not worth the risk now.
- **Sources:** [Groq reasoning](https://console.groq.com/docs/reasoning), [Groq structured outputs](https://console.groq.com/docs/structured-outputs), [Groq rate limits](https://console.groq.com/docs/rate-limits).

## D35. The number check is deterministic code, not an LLM

- **Date:** 2026-10-16. Milestone 3, step 4.
- **Context:** a claim such as "CRAG outperforms RAG by 7.0% accuracy on PopQA (SelfRAG-LLaMA2-7b)" is checked by finding two table cells and subtracting. An LLM is unreliable at arithmetic and costs tokens on every claim; the hard part is only locating the cells, and parsed tables carry enough structure for code to do it.
- **Decision:** `app/audit/numbers.py` turns each table into numeric cells with their context (row label and the group labels carried down, column headers of the current header block, the full-width section row, and for "w/o ..." ablation rows the method above them). A claim's system must match a row (or column) label; dataset, metric and setting narrow the candidates when something matches them; the claim's own words decide between the rest, with the system's and baseline's names removed first. A value is compared at the precision it was written with; a margin is recomputed as an absolute difference, or a relative one only when the text does not say absolute. Verdicts: supported, contradicted, unverifiable. Unverifiable is never reported as an error.
- **Why not an LLM to locate cells:** on the three papers every result claim was located by code. Names a table does not use ("relevance" for "ISREL") stay unverifiable; an LLM locator is a possible extension if E3 on unseen papers shows many of those.
- **Saying "wrong" needs more evidence than saying "right" (held-out lesson):** a matching number is evidence by itself; a different number is only evidence if the cell is surely the one meant. So a claim is contradicted only when one cell (or one column pair) is left, every dataset, metric and setting it names is in that cell's labels or its table's caption, and, for a margin, the claim's words chose the column. A system name must match a row label exactly (with its setting: "RAPTOR" + "GPT-4" is "RAPTOR + GPT-4"), never as an abbreviation. A number written as "by X" or "an improvement of X" is a margin; without a named baseline it is unverifiable, never a score.
- **Measured (E3, offline, no tokens; seed 13):**

  | Set | Real claims: correct numbers | Real: planted errors | Synthetic: correct numbers | Synthetic: planted errors |
  | --- | --- | --- | --- | --- |
  | Tuning papers (CRAG, Adaptive-RAG, Self-RAG), final rules | 13 supported, 0 false alarms, 17 unverifiable | 30/30 caught | 467/472 supported, 0 false alarms | 1,090/1,277 caught, 0 accepted |
  | Held-out (RAPTOR, FLARE), rules as first written | 3 supported, **2 false alarms**, 11 unverifiable | 3/10 caught | 300/322 supported, 0 false alarms | 722/785 caught, 0 accepted |
  | Held-out, after the fixes above | 3 supported, 0 false alarms, 13 unverifiable | 0/10 caught, 10 unverifiable | 340/362 supported, 0 false alarms | 819/882 caught, 0 accepted |

  The second row is the only blind measurement; the third is no longer blind (the fixes came from it). FLARE produced no claim with a numeric value and a system, so its real-claim part is empty.
- **What this means:** on numbers that are clearly located, the check is reliable in both directions (no false alarm, no planted error accepted in any run). Its weakness is coverage on real prose: on RAPTOR, 13 of 16 numbers stay unverifiable because the text says "RAPTOR" where the table says "RAPTOR + GPT-4", refers to "it" from the previous sentence, gives ranges ("1.7 to 5.8"), or because the parser swapped two captions (F-019). An LLM locator, or giving the extractor the previous sentence, are the next steps if coverage matters more than the remaining milestone-3 steps.

## D36. The figure check: one vision call per figure, the image as evidence

- **Date:** 2026-10-16. Milestone 3, step 5: the audit's multimodal step.
- **Context:** papers say things about their figures ("accuracy drops as retrieval gets worse, Figure 3"). Text alone cannot check that; the figure has to be looked at. Groq's only generally available vision model is `qwen/qwen3.8-27b`: at most 3 images per request, each billed at a flat 2,048 input tokens, 8,000 tokens a minute and 200K a day on the free tier ([Groq vision](https://console.groq.com/docs/vision), [rate limits](https://console.groq.com/docs/rate-limits)).
- **Decision:**
  - code groups the extracted claims by the figure they name and sends one request per figure (the image is paid once), split by measured tokens so a request stays under the per-minute limit (1,500 counted tokens of claims, at most 8);
  - the figure image as the parser cropped it, as a base64 data URL; its caption; the numbered claims with their original sentences;
  - P5 asks the model to describe the figure first (type, axes, legend, readable values), then to give one verdict per claim: supported, contradicted, or not shown. The image is the evidence; the caption only identifies the figure (otherwise every claim that restates the caption would pass);
  - hidden reasoning, output budget 900 tokens (qwen allows 1,000 output tokens a minute, F-021); reasoning off or low and strict schema or plain JSON mode are chosen by E4 (`FIGURE_VARIANT` in `app/audit/figures.py`); pp-tools builds the request, n8n posts it (the D34 pattern), with the same loop state and rate-limit handling as the claims (F-012, F-017). There is no second vision model, so a spent daily limit stops the figure checks and the result says so;
  - claims about a figure the parser kept no image of, or about a figure the paper does not contain, are reported without a model call.
- **Images:** Docling's crops (400 to 900 pixels wide) were checked by eye: axis labels, legends and point labels are readable, so no re-parse at a higher resolution was needed.
- **First measurement (P5 v1, strict schema, low reasoning):** every verdict that came back was right (E4 22/22: 12 true claims supported, 9 false claims contradicted, 1 not shown; audits of CRAG and Self-RAG: 9 claims about 6 figures supported), but 7 of 14 calls failed to produce valid JSON (F-021).
- **Request settings chosen by E4** (`eval/results/20261016-233441-e4-figures-comparison.md`, P5 v2, one attempt per figure):

  | Variant | Figures answered | Verdicts right | False alarms | Errors accepted | Output tokens |
  | --- | --- | --- | --- | --- | --- |
  | A: reasoning off, strict schema | 10 of 14 (2 refused by Groq, 2 with an empty verdict list) | 34/34 | 0 | 0 | 342 |
  | B: reasoning off, plain JSON | 10 of 10 (then the daily limit) | 33/35 | 1 | 1 | 554 |
  | C: reasoning low, plain JSON | not run (daily limit) | | | | |

  A is used. Its failures are visible (a refused answer, an empty list) and the loop serves the figure again, up to 3 attempts: at about 30% failures per attempt, a figure fails all three about 3% of the time. B's two errors were silent wrong verdicts; one came from the model reasoning in its evidence field ("68.6 ... higher than 72.9. Wait, looking closer") and keeping the wrong answer. A wrong answer nobody can see is worse than a failure that is retried. An empty verdict list now counts as a failed answer. The samples are small (34 and 35 verdicts); the final E4 run uses A with retries, as the workflow does.
- **Final E4** (2026-10-17, `eval/results/20261017-231104-e4-figures-final-A/`, variant A, up to 3 attempts): **14/14 figures answered** (13 at the first attempt, 15 calls), **47/48 verdicts right**: 24/25 true claims supported, 19/19 false claims caught, 0 accepted, 4/4 "not shown" right. The one false alarm: "StrategyQA peaks at 77.3 when about 40% of the steps retrieve" (FLARE figure 5); the point is at about 40% on the x-axis, and the model read it as 50.0. Reading a point's position between labelled ticks is the weak spot.
- **Evaluation (E4, `scripts/e4_figure_check.py`):** 48 gold claims about 14 figures of 5 papers, written by looking at the figures: 25 true, 19 false (reversed trend, wrong order, wrong value), 4 that neither the image nor the caption answers. Measured: accuracy, false alarms on true claims, errors caught, errors accepted.

## D37. The citation check: resolve in code, retrieve from the cited paper, judge with P6

- **Date:** 2026-10-17. Milestone 3, step 6: the audit's RAG step.
- **Context:** drafts attribute statements to other papers ("Asai et al. (2024) train an LM to critique its own generations"). Checking one means finding the cited paper and reading the relevant part. The library (531 papers, 61 in full text) is already chunked and indexed for the novelty branch.
- **Decision:**
  - code links each citation marker to one entry of the draft's bibliography (first author's surname and year, the letter suffix or the second author when needed, or the number), joining entries the parser split, then that entry to a library paper (arXiv id, or the full title inside the entry);
  - for each claim and cited library paper, the paper's chunks are retrieved with the claim as the query (dense search filtered to that paper) and reranked by the cross-encoder; the top three passages are stored with the plan, so retries and the report use the same evidence;
  - P6 (gpt-oss-20b, 120b on the daily limit, strict schema, the D34 pattern) judges batches of claim-passage pairs: supported, contradicted, or not found. Absence of evidence is "not found", never "contradicted" (the lesson of F-020). A verdict must name the passages it rests on; one that names none becomes "not found";
  - a marker with no bibliography entry, or a paper outside the library, is reported as such without a model call.
- **Measured before building (resolution only, code):** on the claims extracted from the five audited papers, 68 of 69 citation markers resolve to exactly one bibliography entry (after joining split entries, ignoring arXiv ids as years, and reading "Tom B. Brown" correctly); 28 point to a library paper, 20 of them in full text. Self-RAG cites mostly work outside the library's scope (26 of 29).
- **Measured (E5, 2026-10-18, `eval/results/20261017-232728-e5-citations/`, gpt-oss-120b):** 40 gold pairs about 15 library papers.
  - Retrieval: the gold evidence phrase is in the three passages for 29/40 pairs; read by hand, the passages are enough to judge 37/40.
  - Verdicts: 25/25 supported pairs recognised, 0 false alarms; 14/15 changed claims rejected (11 contradicted, 3 not found); 1 accepted, from a passage cut mid-sentence (F-029).
  - P6 v2 (judge only the part attributed to each citation, F-024) gave the same verdicts as v1 on every pair, so v1 stays active.
- **Limits:** a cited paper outside the library is not checked (fetching its abstract from Semantic Scholar is a possible extension); for an abstract-only library paper, only the abstract is evidence, so "not found" is more likely and the report says so.

## D38. The E1 queries were written by Claude, not by teammates (revises D19)

- **Decision:** the 173 E1 queries in `eval/queries/worksheet.csv` (41 dev, 132 test; 4 section rows skipped because their section list was empty or uninformative) were written by Claude, the coding assistant, from the worksheet's context column, following `eval/queries/README.md`. D19 had planned for teammates to write them.
- **Why:** the teammates' time goes to the demo and the defence, and the queries had to exist before tuning could start. The worksheet rules were kept and checked by a script: 8 to 25 words, no 5 consecutive words shared with the row's context (12 queries were rewritten after the check), no system names.
- **Risks, stated in the report:**
  - The writer saw the abstract or section text, as a teammate would have; queries may still be closer to the paper's wording than a real user's.
  - The queries are not biased toward the retrieval model (BGE-M3 never saw them being written), but they may share an LLM's phrasing habits.
  - Only the source paper is marked relevant; another paper answering a broad query counts as a miss for both strategies equally, so the A vs B comparison stays fair while absolute recall is a lower bound.
- **Mitigation:** a teammate reviews a sample of 20 test queries before the single test run, and the queries are frozen after the first evaluation (rule 7).

## D39. A form in n8n as the user interface, with a result page written by code

- **Decision:** `05-form` uses n8n's Form Trigger (`/form/paperpilot-form`): pick novelty or audit, type the idea, upload a PDF or paste a link. An uploaded PDF goes to `POST /uploads` and is kept under its SHA-256; the run refers to it as `draft_pdf_upload`. The form then calls the same webhook as every other client (`/webhook/paperpilot`), and a Form Ending node shows a page that `POST /form/result` builds from the stored run.
- **Why:**
  - The form is a client of the system, not a second path through it: validation, run tracking, the error workflow and both branches are the ones already tested.
  - n8n holds the browser on a waiting page until the Form Ending node runs, so a 10-minute audit needs no polling code of ours.
  - Uploads are stored by pp-tools rather than passed through the router as base64: the run record stays small, the same file is parsed once (the hash also names the parsed-paper cache), and a URL is no longer the only way in. The download path's SSRF guard is not involved, since nothing is fetched.
  - The page is written by code from verified steps and checks, with every string from the user, the paper or a model HTML-escaped. Problems come first; supported checks and citations outside the library are folded away. The LLM's own summary is shown only with a "not verified" label.
- **Limits:** no authentication on the form (the stack listens on localhost only); one file per request; the result is shown once on the page and otherwise kept in the run record (`GET /runs/{id}`).

## D40. gpt-oss-120b extracts claims and judges citations; gpt-oss-20b is the fallback

- **Date:** 2026-10-17. Revises the model choice of D33 and D37 (the roles D12 had planned for 120b).
- **Context:** the final audits (`eval/results/20261017-214450-audit-final/`) ran with gpt-oss-20b only, for the first time on all three papers. The citation check had almost nothing to check: CRAG produced 1 claim with a citation marker, Adaptive-RAG 3. Earlier runs on the same candidate sentences, where gpt-oss-120b did most of the work because 20b's daily tokens were spent, produced 7 to 11 (CRAG) and 12 (Adaptive-RAG). 20b also failed the strict schema three times on one FLARE batch, which was skipped (F-023).

  | Paper | Claims with a citation, 120b runs | 20b only (final run) |
  | --- | --- | --- |
  | CRAG | 7 (2026-10-16 20:21), 11 (23:53) | 1 |
  | Adaptive-RAG | 12 | 3 |
  | Self-RAG | 17 | not run |

- **Decision:** `CLAIMS_MODEL` is `openai/gpt-oss-120b` for P4 and P6, with `openai/gpt-oss-20b` as the fallback on the daily limit. Same parameters, same strict schema, same rate-limit handling.
- **Why:** the citation check (the audit's RAG step) depends on claims that carry a marker; a cheaper extractor that skips them makes the most expensive step look idle. 120b has its own 200K tokens a day, and a call costs about the same tokens.
- **Limits:** the comparison is across runs, not a controlled A/B on identical batches, and gpt-oss output varies between runs; the next audits measure 120b alone. Each model still allows about three three-paper runs a day.
- **Outcome (2026-10-18, `eval/results/20261018-172535-audit-120b/`):** with 120b alone on the same candidate sentences, claims kept went from 55 to 110 over the three papers, claims with a citation marker from 5 to 33, citation verdicts from 5 to 33 and figure verdicts from 7 to 20. No LLM error, no rate limit. Among the 33 citation verdicts, one contradiction, checked by hand and found to be a false alarm (F-030). About 25 % more time per audit.

## D41. A web page served by pp-tools as the main interface; the n8n form stays

- **Date:** 2026-10-18. Adds to D39.
- **Decision:** pp-tools serves a single page at `/app`. The browser posts to `POST /app/api/runs`; pp-tools validates the request with the same `RunRequest` model, adds a random `client_ref` (20 characters), and calls the n8n webhook (`N8N_WEBHOOK_URL`, default `http://n8n:5678/webhook/paperpilot`) in a background thread. The page polls `GET /app/api/runs/{ref}` every 2 seconds; pp-tools finds the run by `input->>'client_ref'` and turns its recorded steps into stages (who works: an LLM or code; batches done out of total). When the run is done the page draws the result from the stored run.
- **Why:**
  - n8n stays the orchestrator: the page is one more client of the webhook, like the form, the smoke test and the evaluation scripts.
  - The browser cannot call n8n's webhook directly (no CORS headers) and a 15-minute request is fragile; a background call plus polling survives a closed tab, and `#run=<ref>` reopens a run.
  - Progress comes from the `run_steps` the workflows already record, so it shows what really happened, including a check stopped by the daily token limit.
  - The n8n form only shows a spinner until the end; the page shows each stage as it finishes and lets the reader tab through numbers, figures and citations with their evidence.
- **Safety:** every string from the user, a paper or a model is set with `textContent`; links pass `safeUrl()` (http/https only); the ref must match `^[a-z0-9]{12,40}$`; only `mode`, `idea_text`, `draft_pdf_url` and `draft_pdf_upload` are forwarded. Fonts are bundled (OFL), so the page works offline during the demo.
- **Additions (2026-10-18):**
  - **Reusing a finished audit.** An audit request for a PDF already audited (same upload hash or same URL) returns the latest finished run instead of starting a new one, complete runs first. The page says so and offers "Run the audit again" (`fresh: true`). An audit costs 5 to 15 minutes and a large share of the daily free tokens, so the live demo can show a real result at once. Novelty checks are never reused, because the web search changes. The n8n form does not reuse runs.
  - **Reopening a run by id.** A finished run is reopened with the ref `u` followed by its id's 32 hex digits. Client refs are 20 characters, so the two never collide, and this works after a pp-tools restart.
  - **Page numbers and figures.** Each checked sentence shows its page, from the parsed paper's blocks. Each figure verdict shows the figure image the vision model read, served by the existing `GET /audit/{run_id}/figures/{id}`.
  - **Novelty headline from the labels.** A paper "does much of the idea" only if its method is `same` or two facets are `same`. Otherwise the page says no paper does the idea and lists the partial overlaps. This replaces "N papers already overlap", which disagreed with the judge's own summary when every overlap was partial.
  - **Report download.** A print stylesheet lays out every check (all three tabs) for the browser's "Save as PDF".
- **Limits:** the webhook call is kept in memory: if pp-tools restarts during a run, the run continues in n8n and its status is still read from Postgres, but a webhook error that happened before the run was created is lost. No authentication (localhost only, as D39).
