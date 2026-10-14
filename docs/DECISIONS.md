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

- **Decision:** `POST /corpus/build` queues a job and answers 202 with a `job_id`. The `10-ingest-papers` workflow replies to its caller at once, then polls `GET /jobs/{id}` every 15 seconds until `done` or `failed`, with a 1-hour cap.
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
