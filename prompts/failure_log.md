# Prompt failure log

Every wrong output seen in a real run goes here the same day, including failures that were never fixed. This log and the before/after examples are what the prompt-engineering grade rests on.

How to add an entry:
1. Copy the template row and give it the next id (F-001, F-002, ...).
2. Keep the exact input and output: the run id is enough if the run is stored (`GET /runs/{id}` and the `run_steps` table hold the prompt version and the model output).
3. When a new prompt version fixes it, fill in "Fixed in" and add the before/after to the new version's changelog.

| Id | Date | Prompt and version | Run id or input | Wrong output | Likely cause | Fix | Fixed in | Still failing on |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F-000 | (template) | P3-novelty-judge v1 | run id | e.g. marked "same" on method for a paper that only shares the word "retrieval" | e.g. no definition of "same" strict enough | e.g. add a counter-example | v2 | — |
| F-001 | 2026-10-14 | P3-novelty-judge v1 (gpt-oss-120b), plus result assembly | run `e36edefe-cc61-41cb-a912-6853379d75a1`, idea "Train a small model to decide, for each question, whether retrieval is needed at all before answering." | All 3 papers in `closest_work` are labelled `different` on task and method, `unknown` on data and result. The report still presents them as "closest prior work". | The judge must label every candidate, and assembly keeps every verified match. Nothing lets the system say "no retrieved paper overlaps". | Planned: assembly splits papers with at least one `same` or `partial` (closest work) from papers checked and found different; the report says plainly when the first list is empty. | code (D25): false overlap 0 of 12 out-of-domain runs, E7 | — |
| F-002 | 2026-10-14 | P3-novelty-judge v1 (gpt-oss-120b) | same run | Summary: "the task of deciding retrieval necessity per question and the method of training a small predictor remain largely uncovered". The library held only 5 papers, so this reads as a novelty claim. | The prompt asks which facets "look least covered" but never tells the model how large the searched library is. | Planned for v2: pass `papers_in_knowledge_base` and the coverage note into the prompt and require the summary to state what was searched. | P3 v2: scope stated in 0/12 runs with v1, 12/12 with v2 (E7) | — |
| F-003 | 2026-10-14 | P3-novelty-judge v1 (gpt-oss-120b) | run `659c5d05-b3e6-4c5c-841e-ff68c3dd8096`, same idea, full 500-paper library | Summary: "none of the examined papers provide details on the specific datasets or concrete results ... leaving the data and result facets largely uncovered". Those facets were "not specified" in the idea itself, and the evidence for the two matching papers was abstracts only. | The prompt does not separate "unknown" (no evidence either way) from "not covered". The model turned missing evidence into a coverage claim. | Planned for v2: say in the prompt that `unknown` facets and facets the idea leaves unspecified must not be called covered or uncovered; add this run as a counter-example. | P3 v2: coverage claims on unspecified facets in 7/12 runs with v1, 0/12 with v2 (E7) | v2 still mentions those facets neutrally in 4/12 runs ("no passage gives data or results") |
| F-004 | 2026-10-15 | P3-novelty-judge v2 (gpt-oss-120b); cause in retrieval code | run `6d4001f3-7351-47ae-ac4c-2ce474bb6025` | Adaptive-RAG labelled method `unknown` ("does not specify training a small model"), although the paper trains a smaller LM as a classifier. | Not the prompt: evidence passages were cut at 900 characters, and Adaptive-RAG's abstract was cut at "based on the query complex", one sentence before "operationalized with a classifier, which is a smaller LM trained to predict...". Given what it saw, `unknown` was the right label. | Abstracts are now kept whole up to 2,000 characters; other passages stay at 900 (token budget). | code fix, branch `feat/prompt-ab-eval` | — |
| F-005 | 2026-10-15 | P1-decomposer v1 (gpt-oss-20b) | runs `6d4001f3`, `0482fce4` | The four queries are near-paraphrases of the idea ("small model predicts retrieval necessity for QA", "retrieval decision model ...", "predicting retrieval requirement ...", "learning to skip retrieval ..."). Self-RAG and SKR, now in the library, are not in the top 20 papers of the first query; "When Not to Trust Language Models" is 7th, an uncertainty-based adaptive retrieval paper (2607.07380) 13th, and none reaches the judge's 6 candidates. | The prompt asks for "different wording from the idea" but not for the field's own vocabulary ("adaptive retrieval", "when to retrieve"). | P1 v2: at least two queries with established names for the task or mechanism, one concrete mechanism query, 4 to 6 queries, no near-duplicates. Retrieval itself (reranker, agentic search loop) is milestone 2 part 2. | not fixed by P1 v2 (E7: 9/22 → 10/22 expected papers retrieved, within noise) | adaptive-retrieval: Self-RAG, SKR and Mallen et al. never among the candidates in 12 runs |
| F-006 | 2026-10-15 | P2-search-planner v1 (gpt-oss-20b) | team PC, idea "Train a small model to decide, for each question, whether retrieval is needed at all before answering." | Round 1: "None of the current candidates explicitly describe a small model trained to predict whether retrieval is needed for each question, so the method facet remains uncovered", although the previous run's judge labelled Self-RAG and SKR `same` on task and method. It searched the local library with "small neural network predicts retrieval necessity for QA", then in round 2 gave the same reason word for word and two more local paraphrases; Semantic Scholar was never used. | The prompt asks whether a candidate matches the method without saying that the match is by meaning; it only saw 300 characters of a passage that was often not the abstract; nothing tells it to change strategy after an unproductive round. | P2 v2: match by meaning with this idea as the counter-example, relevance threshold, switch tool or stop after an unproductive round. Code: the planner sees the judge's candidates with the abstract first, 500 characters. | v2 (to be measured) | — |
| F-007 | 2026-10-15 | design of the search agent (code, D29 first version) | same run | With the agent, the judge saw 1 overlapping paper (Adaptive-RAG, partial); without it, 4, including Self-RAG and SKR as `same`/`same`. The agent's queries brought DPR (2004.04906) and 2512.23836 into the top 6 and pushed the good candidates out. | After each round the whole pool was re-ranked with the new queries in the fusion; queries that drift change every paper's fused rank. | The agent can only add: earlier candidates are kept with their evidence, and at most `NOVELTY_AGENT_EXTRA_PAPERS` (2) new papers are admitted, each at least as relevant to the idea (reranker score) as the weakest candidate already shown. | code, branch `fix/agent-keeps-candidates` | — |

Observations from run `e36edefe` that are **not** failures: the decomposer (P1 v1, gpt-oss-20b) produced sensible facets, wrote "not specified" for data and result instead of inventing them, and its 4 queries are good paraphrases. The judge's per-facet labels were correct for the papers it saw, and every evidence id it cited was real (verifier passed on attempt 1). This run used the 5-paper test library: it started 33 seconds before the full build. Rerun on the full library before drawing conclusions about retrieval.

**Known-answer check, run `659c5d05` (full library):** the idea is close to Adaptive-RAG (arXiv 2403.14403), which is in the manifest. The system found it (task and method `partial`, which is defensible: Adaptive-RAG picks one of three retrieval strategies rather than making a yes/no decision), and ranked above it a closer paper, arXiv 2604.07985 "Predicting the Benefit of Retrieval Augmentation in Open-Domain Question Answering" (task and method `same`). Self-RAG (2310.11511) is not in the manifest, so it could not be found. F-001 shows again: 4 of the 6 papers listed are `different` on every known facet.

**First like-for-like comparison, same idea, same 6 candidates (runs `0482fce4` with P3 v1 and `6d4001f3` with P3 v2, both after the code-written report):**

| | P3 v1 | P3 v2 |
| --- | --- | --- |
| LLM summary states what was searched (F-002) | no: "The closest prior work is ..." | yes: "Among the 531 papers searched, ..." |
| Summary claims coverage or novelty (F-003) | no, this time ("showing limited overlap") | no |
| 2604.07985, method | partial ("does not specify that it is a small model") | same |
| Adaptive-RAG, method | partial | unknown (see F-004) |

One run per version cannot show a rate: v1 produced F-003 in two earlier runs and not in this one. The measured comparison follows.

## E7 run 1: measured comparison (2026-10-15)

Team PC, real Groq models, 531-paper library. 6 ideas (5 with known answers, 1 out of domain) x 3 configurations x 2 repeats = 36 runs, all successful. Full data: `eval/results/20261015-000708-prompt-ab/`. Scores recomputed with `--rescore` after adding the coverage-claim rule; every flagged summary was also read by hand and agrees with the rule.

| Config | Expected papers retrieved | False overlap (out of domain) | LLM states scope (F-002) | LLM overclaims | LLM calls unspecified facets (un)covered (F-003) | LLM mentions unspecified facets | Label agreement across repeats |
| --- | --- | --- | --- | --- | --- | --- | --- |
| baseline (P1 v1, P3 v1) | 8/22 | 0/12 | 0/12 | 5/12 | 7/12 | 7/12 | 0.83 |
| judge-v2 (P1 v1, P3 v2) | 9/22 | 0/12 | **12/12** | **0/12** | **0/12** | 4/12 | 0.96 |
| full-v2 (P1 v2, P3 v2) | 10/22 | 0/12 | **12/12** | **0/12** | **0/12** | 4/12 | 0.84 |

Expected papers among the judge's candidates, per idea (found / expected over 2 repeats):

| Idea | baseline | judge-v2 | full-v2 |
| --- | --- | --- | --- |
| adaptive-retrieval | 3/10 | 3/10 | 2/10 |
| iterative-rewrite | 1/6 | 1/6 | 2/6 |
| retrieval-evaluator (CRAG) | 2/2 | 2/2 | 2/2 |
| reflection-tokens (Self-RAG) | 0/2 | 1/2 | 2/2 |
| hypothetical-document (HyDE) | 2/2 | 2/2 | 2/2 |
| out-of-domain | 0/0 | 0/0 | 0/0 |

**Conclusions.**

1. **P3 v2 fixes the prose failures.** Stating the search scope went from 0 of 12 runs to 12 of 12, and coverage or novelty claims from 7 (and 5) of 12 to 0. The effect is the same with either decomposer version, so it comes from the judge prompt.
2. **v2 still mentions unspecified facets** in 4 of 12 runs, always neutrally ("No passages provide information on data or results"). It breaks a rule of the prompt but is true and harmless; left as a known limitation rather than another prompt iteration.
3. **F-001 is fixed by code, not by a prompt:** the out-of-domain idea never produced an overlap in any configuration.
4. **P1 v2 did not measurably improve retrieval.** 8, 9 and 10 of 22 expected papers are within the run-to-run variation (judge-v2 and baseline use the same decomposer, yet differ). When an expected paper reaches the judge it is always placed in closest work, so the judge is not the weak link: retrieval is. It finds the paper when the idea paraphrases one paper (CRAG, HyDE: 2/2 everywhere) and misses families of papers described in other vocabulary (adaptive retrieval: at best 3 of 10). This is the measured motivation for the reranker and the agentic search loop (milestone 2 part 2).
5. **The query-similarity metric did not capture F-005** (0.15, 0.16, 0.14): it counts shared words, and v1's paraphrases used different words for the same meaning. It is kept but not used as evidence.
6. **Labels are stable enough to compare** (0.83 to 0.96 agreement between repeats at temperature 0); single runs are not, which is why every comparison here uses repeats.
