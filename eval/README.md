# Evaluation

Retrieval quality and answer quality are measured separately.

| Test | Milestone | Question | Status |
| --- | --- | --- | --- |
| E1 chunking | 1 | Does structure-aware chunking (B) retrieve the right paper more often than fixed-size (A)? Recall@k and MRR | Tooling ready; queries to write |
| E2 novelty | 2 | Leave-one-out: does the system find the papers a paper builds on? | Not started |
| E3 planted errors | 3 | Detection rate of altered table values, inflated claims, swapped citations | Not started |
| E4 gold claims | 3 | Audit judge accuracy on about 30 labelled claims | Not started |
| E5 citation faithfulness | 3 | Faithful vs unfaithful citation pairs | Not started |
| E6 verifier on/off | 3 | Invented citations and untraceable numbers per run | Not started |
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

Runs the same ideas through the real pipeline (n8n webhook, Groq models) once per prompt configuration and repeat, then counts failures automatically from the stored runs. The ideas, with the papers each should find, are in [prompt_ab/ideas.json](prompt_ab/ideas.json); one idea is out of domain and must find no overlap.

```bash
python scripts/prompt_ab.py               # 6 ideas x 3 configurations x 2 repeats, about 45 minutes
```

The default configurations are `baseline` (P1 v1, P3 v1), `judge-v2` (P1 v1, P3 v2) and `full-v2` (P1 v2, P3 v2), so the effect of each prompt change is visible on its own. The script switches `prompts/active.json` between configurations and restores it at the end. Runs are 60 seconds apart because of Groq's free per-minute token limit; a rate-limited run is retried once.

Output in `eval/results/<timestamp>-prompt-ab/`: `summary.md` (the table for the report), `runs.csv` (one row per run) and `runs.jsonl` (every result and step, for reading the actual LLM outputs). The scoring rules are unit-tested against the failures in `prompts/failure_log.md`.
