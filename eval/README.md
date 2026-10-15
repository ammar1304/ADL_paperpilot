# Evaluation

Retrieval quality and answer quality are measured separately.

| Test | Milestone | Question | Status |
| --- | --- | --- | --- |
| E1 chunking | 1 | Does structure-aware chunking (B) retrieve the right paper more often than fixed-size (A)? Recall@k and MRR | Tooling ready; queries to write |
| E2 novelty | 2 | Leave-one-out: does the system find the papers a paper builds on? | Tooling ready (`scripts/e2_leave_one_out.py`); run on the team PC |
| E3 planted errors | 3 | Detection rate of altered table values, inflated claims, swapped citations | Not started |
| E4 gold claims | 3 | Audit judge accuracy on about 30 labelled claims | Not started |
| E5 citation faithfulness | 3 | Faithful vs unfaithful citation pairs | Not started |
| E6 verifier on/off | 3 | Invented citations and untraceable numbers per run | Not started |
| E8 retrieval A/B | 2 | Same stored queries, four ranking settings (fused, plus idea query, reranked, both): expected papers reaching the judge, in the pool, MRR | Tooling ready (`scripts/retrieval_ab.py`); run on the team PC |
| E7 prompt A/B | 2 | Per prompt version: expected papers found, query diversity, false overlap, LLM summary failures (F-002, F-003), label stability across repeats | Tooling ready (`scripts/prompt_ab.py`); run on the team PC |

## E1: chunking A vs B

### 1. Prepare the worksheet

After the corpus build (see [corpus/README.md](../corpus/README.md)):

```bash
docker compose exec pp-tools python -m app.eval.make_queries
```

This writes `eval/queries/worksheet.csv`: three rows per full-text paper (paraphrase, section, table). Each row shows context from the paper. Whole papers are assigned to `dev` or `test`, so no paper is in both.

### 2. Write the queries

Fill in the `query` column (in Excel, Google Sheets or an editor), following [queries/README.md](queries/README.md). Rows left empty are skipped. Commit the worksheet.

### 3. Run the evaluation

```bash
docker compose exec -e PP_GIT_COMMIT=$(git rev-parse --short HEAD) pp-tools \
  python -m app.eval.retrieval --queries /pp/eval/queries/worksheet.csv --split dev --label dev-v1
```

PowerShell: replace `$(git rev-parse --short HEAD)` with the commit hash, or leave `-e ...` out (the result then records the commit as `unknown`).

Results go to `eval/results/<timestamp>-<label>/`:

| File | Content |
| --- | --- |
| `summary.md` | Recall@1, @5, @10, @20 and MRR@20 for A and B, overall and per query type |
| `summary.json` | The same numbers, machine-readable |
| `per_query.json` | The paper ranking each strategy returned for each query: use it to find and explain failures |
| `config.json` | Git commit, manifest hash, query file hash, chunk sizes, cutoffs |

Commit result folders you report. Queries go through the running service's `/search` endpoint, so the numbers describe the deployed system. `--direct` bypasses the API.

### Rules that keep the numbers honest

- **Tune on `dev` only:** chunk size, overlap and anything else. Run `--split test` once, at the end, and report that.
- **Never edit a query after seeing how it scored.** Add new ones instead, and say so.
- **Report losses too:** where A beats B, `per_query.json` shows which queries; explain them.
- **Papers can be relevant to many queries:** list every relevant paper in `relevant_papers`, separated by `;`.

## Layout

```
eval/
  queries/      worksheet.csv (E1), later gold claims and mutation logs
  results/      one folder per evaluation run
```

## E7: prompt A/B

Runs the same ideas through the real pipeline (n8n webhook, Groq models) once per prompt configuration and repeat, then counts failures automatically from the stored runs. The ideas, with the papers each should find, are in [prompt_ab/ideas.json](prompt_ab/ideas.json); one idea is out of domain and must find no overlap in the local library (closest work found on the web is counted apart and checked by hand, F-008), and three ideas (`outside_library`) have their closest work outside the library, so only the search agent's web search can find it. Before running, the script checks that those papers really are absent from the library and warns otherwise.

```bash
python scripts/prompt_ab.py               # 9 ideas x 3 configurations x 2 repeats, about 70 minutes
```

The default configurations are `baseline` (P1 v1, P3 v1), `judge-v2` (P1 v1, P3 v2) and `full-v2` (P1 v2, P3 v2), so the effect of each prompt change is visible on its own. The script switches `prompts/active.json` between configurations and restores it at the end. Runs are 60 seconds apart because of Groq's free per-minute token limit; a rate-limited run is retried once.

Output in `eval/results/<timestamp>-prompt-ab/`: `summary.md` (the table for the report), `runs.csv` (one row per run) and `runs.jsonl` (every result and step, for reading the actual LLM outputs). The scoring rules are unit-tested against the failures in `prompts/failure_log.md`.

## E8: retrieval A/B

Replays the decomposer queries stored in an E7 run through `POST /novelty/rank`, with no LLM call, so every setting ranks exactly the same queries. pp-tools is called once per query set and pool setting; the orders are derived from the returned pool (each paper carries its fused rank and rerank rank):

| Method | Pool | Order |
| --- | --- | --- |
| `fused+idea` | decomposer queries plus the idea text | vector search and reciprocal rank fusion |
| `reranked+idea` | same | cross-encoder score (the default) |
| `hybrid+idea` | same | reciprocal rank fusion of fused rank and rerank rank |
| `reranked+idea+abs` | same, and the reranker also scores each paper's abstract | cross-encoder score |
| `hybrid+idea+abs` | same | hybrid |

```bash
python scripts/retrieval_ab.py        # uses the latest eval/results/*-prompt-ab/runs.jsonl, about 20 minutes
```

It reports, per method and per idea, how many expected papers reach the judge (top 6), how many are in the pool (top 30, the ceiling for any reordering), the MRR and the time per ranking. The first row repeats what the judge saw in the stored runs. Output: `eval/results/<timestamp>-retrieval-ab/` with `summary.md`, `rows.csv` and `pools.jsonl` (every returned pool). Run 1's results are in `docs/DECISIONS.md` (D28).

## E2: leave-one-out

A paper from the library plays the user: its own abstract is the idea, the paper itself is removed from the results, and the expected answers are the papers it really cites that are in the library (from Semantic Scholar's reference lists). Nothing here was used to choose the system's settings, so it checks whether the choices made on E7 and E8 generalise.

```bash
python scripts/e2_leave_one_out.py build                 # needs SEMANTIC_SCHOLAR_API_KEY in .env; about 11 minutes the first time
python scripts/e2_leave_one_out.py run                   # retrieval only, no LLM: fused vs reranked vs hybrid, about 15 minutes
python scripts/e2_leave_one_out.py run --mode full --limit 15   # whole pipeline through n8n (decomposer, agent, judge)
python scripts/e2_leave_one_out.py score eval/results/<folder>-e2-retrieval   # re-score stored pools against new gold sets, seconds
```

- `build` fetches every library paper's reference list (cached in `eval/e2/s2_references.json`, not committed), keeps papers that cite at least 3 library papers, picks 40 of them with a fixed seed, and reads their abstracts from the library. It writes `eval/e2/queries.json`: **commit it**, so every later run uses the same queries.
- **Three gold sets** from the same reference lists: `all` cited library papers; `influential`, the citations Semantic Scholar marks as influential (the cited work is used or extended, not only mentioned); `method`, the citations whose intent is "methodology". Run 1 used `all` and its misses were mostly background citations (RAG, DPR, REALM, HotpotQA). A `build` after run 1 fetches the reference lists again (about 11 minutes) to get the influence flags; `score` then scores run 1's stored pools against all three, without ranking again.
- `run` reports, per gold set and method: cited papers reaching the judge (top 6, micro and macro), in the pool (top 30), and MRR. Output in `eval/results/<timestamp>-e2-<mode>/`.
- Limits: a paper cites what existed when it was written and what its authors chose to cite, so a "miss" can be a fair answer; the comparison between methods is what matters, not the absolute number.

## Agent on and off (E7)

The search agent's effect, on the same ideas and prompts. `NOVELTY_MAX_SEARCH_ROUNDS` is read by pp-tools at start, so switch it between the two runs (PowerShell):

```powershell
$env:NOVELTY_MAX_SEARCH_ROUNDS = "0"; docker compose up -d --wait
python scripts/prompt_ab.py --config "agent-off=P1-decomposer:v2,P2-search-planner:v2,P3-novelty-judge:v2" --label agent-off
Remove-Item Env:NOVELTY_MAX_SEARCH_ROUNDS; docker compose up -d --wait
python scripts/prompt_ab.py --config "agent-on=P1-decomposer:v2,P2-search-planner:v2,P3-novelty-judge:v2" --label agent-on
```
