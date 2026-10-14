# Prompt failure log

Every wrong output seen in a real run goes here the same day, including failures that were never fixed. This log and the before/after examples are what the prompt-engineering grade rests on.

How to add an entry:
1. Copy the template row and give it the next id (F-001, F-002, ...).
2. Keep the exact input and output: the run id is enough if the run is stored (`GET /runs/{id}` and the `run_steps` table hold the prompt version and the model output).
3. When a new prompt version fixes it, fill in "Fixed in" and add the before/after to the new version's changelog.

| Id | Date | Prompt and version | Run id or input | Wrong output | Likely cause | Fix | Fixed in | Still failing on |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F-000 | (template) | P3-novelty-judge v1 | run id | e.g. marked "same" on method for a paper that only shares the word "retrieval" | e.g. no definition of "same" strict enough | e.g. add a counter-example | v2 | — |
| F-001 | 2026-10-14 | P3-novelty-judge v1 (gpt-oss-120b), plus result assembly | run `e36edefe-cc61-41cb-a912-6853379d75a1`, idea "Train a small model to decide, for each question, whether retrieval is needed at all before answering." | All 3 papers in `closest_work` are labelled `different` on task and method, `unknown` on data and result. The report still presents them as "closest prior work". | The judge must label every candidate, and assembly keeps every verified match. Nothing lets the system say "no retrieved paper overlaps". | Planned: assembly splits papers with at least one `same` or `partial` (closest work) from papers checked and found different; the report says plainly when the first list is empty. | open | — |
| F-002 | 2026-10-14 | P3-novelty-judge v1 (gpt-oss-120b) | same run | Summary: "the task of deciding retrieval necessity per question and the method of training a small predictor remain largely uncovered". The library held only 5 papers, so this reads as a novelty claim. | The prompt asks which facets "look least covered" but never tells the model how large the searched library is. | Planned for v2: pass `papers_in_knowledge_base` and the coverage note into the prompt and require the summary to state what was searched. | open | — |

Observations from the same run that are **not** failures: the decomposer (P1 v1, gpt-oss-20b) produced sensible facets, wrote "not specified" for data and result instead of inventing them, and its 4 queries are good paraphrases. The judge's per-facet labels were correct for the papers it saw, and every evidence id it cited was real (verifier passed on attempt 1). This run used the 5-paper test library: it started 33 seconds before the full build. Rerun on the full library before drawing conclusions about retrieval.
