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

**F-006 and F-007 after the first fix (2026-10-15, team PC, same idea, P2 v2):**

- F-007 is fixed: the judge saw 8 candidates and 6 overlapped (Adaptive-RAG `same`/`same`, Self-RAG and 2604.07985 `partial` on the method), instead of 1.
- F-006 is not fixed: P2 v2 again answered "None of the current candidates explicitly describe training a small model to decide whether retrieval is needed before answering". This time it chose the web, but Semantic Scholar refused the request (HTTP 429, no API key). Cause found in the planner's input: the passages were cut at 500 characters, and Adaptive-RAG's abstract states "a classifier, which is a smaller LM trained to predict the complexity level of incoming queries" after about 900 characters, so the planner never saw it (the same truncation as F-004, in a new place). Fix: the planner sees 1,200 characters, abstract first.
- New finding, attribution: the round's web search failed, yet the result credited the agent with Self-RAG and 2606.29090 as new candidates. They were already in the pool (ranks 7 and 8) and were admitted because the agent's rule allowed 2 extra papers. Fix: the agent may only admit papers its own searches brought into the pool.

## M2 final runs: agent off vs on, E2 (2026-10-15)

Results: `eval/results/20261015-213516-agent-off`, `20261015-215304-agent-on`, `20261015-211913-e2-retrieval` (branch `eval/m2-final-runs`). Same 6 ideas, 2 repeats, P1 v2, P2 v2, P3 v2, hybrid order, idea as an extra query.

| | Agent off | Agent on |
| --- | --- | --- |
| Expected papers among the judge's candidates | 16/22 | 15/22 |
| False overlap (out-of-domain idea) | 0/12 | 1/12 |
| Label agreement across repeats | 0.88 | 0.85 |
| Rounds in which the agent searched | 0 | 6 in 12 runs |
| Expected papers added by the agent | 0 | 0 |

**The one-paper gap is not caused by the agent.** The only difference is reflection-tokens, repeat 1. Self-RAG was in that run's first pool with the highest reranker score (0.82) but fused rank 12, so the hybrid order put it just below the top 6. In the agent-off run of the same idea the decomposer's queries differed (Groq is not fully deterministic at temperature 0) and Self-RAG had fused rank 14 yet made the top 6. The agent only adds papers new to the pool (D29), so it could not promote Self-RAG; it admitted AB-RAG (2606.29090) instead. Offline replay of the ordering rules (`scripts/order_replay.py`, D30) shows that guaranteeing a slot to strongly reranked papers would fix this case but loses 3 of 57 expected papers elsewhere, so the order stays.

**What the planner decided (P2 v2, 12 runs).**

- **Stopped at once, correctly, in 6 runs:** adaptive-retrieval ×2 (citing 2604.07985 as matching), hypothetical-document ×2 (HyDE), iterative-rewrite #1 (EfficientRAG), retrieval-evaluator #2 (CRAG). F-006's "too literal" failure is gone for the idea that produced it.
- **Searched although the matching paper was shown, in 2 runs (F-006, residual):** retrieval-evaluator #1 ("none mention triggering a web search fallback", although CRAG's abstract does) and reflection-tokens #2 ("none demonstrate fine-tuning ... both types of tokens", with Self-RAG at relevance 0.78). Both recognised the paper in round 2. Cost: one wasted local query each; the result was not harmed.
- **Searched and added useful papers in 2 runs:** iterative-rewrite #2 added Reasoning in Trees (2601.11255, judged `same`/`same`) and EfficientRAG; reflection-tokens #1 added AB-RAG (`same` task, `different` method). None is in the idea's expected list, so the metric gives the agent 0 although the judge's evidence improved.
- **Out-of-domain idea (protein structure prediction with diffusion): web search, both repeats.** The planner chose the web, as P2 asks for a topic outside the library. Repeat 1: 8 Semantic Scholar papers, none reached the admission floor. Repeat 2: three web queries, two papers admitted, both 2024 diffusion models for *inverse* protein folding (2412.07815, 2411.01737); the judge labelled task `partial` and method `partial`.

**F-008 (metric, not system): "false overlap" assumes the out-of-domain idea has no prior work.** That holds for the local library, which is what the metric was designed for (F-001). With web search, the idea does have real prior work, and diffusion models for inverse folding are a defensible `partial` on both facets. The 1/12 counts a correct answer as an error. Fix: false overlap counts only closest work from the local library for ideas without expected papers; closest work found on the web is listed separately for manual review. Done on branch `feat/m2-last-round`: re-scoring the agent-on run gives false overlap 0/12 and 2 closest-work papers from the web.

**E2 leave-one-out (40 library papers, 273 cited library papers).**

| Order | Cited papers in the top 6 | In the pool of 30 | MRR |
| --- | --- | --- | --- |
| fused | 14/273 (5%) | 46/273 | 0.033 |
| reranked | 10/273 (4%) | 46/273 | 0.026 |
| hybrid | 11/273 (4%) | 46/273 | 0.029 |

A random choice of 6 of 530 papers would find about 1%, so retrieval is 3 to 5 times better than chance, but the gold is loose. For 30 of the 40 papers no cited paper reaches the top 6. The most-missed cited papers are background citations: RAG (2005.11401, missed 33 times), DPR (18), REALM (17), the HotpotQA dataset (13), FiD (12), a RAG survey (8). A paper's abstract does not describe the methods it cites as background, so the reranker scores them low, correctly for a novelty check. This is also why the reranker loses to the fused order here, unlike E8. Next: keep only the citations Semantic Scholar marks as influential (`isInfluential`, the cited work is used or extended), and compare.

**E2 with stricter gold sets (2026-10-16, run 1's stored pools re-scored, `eval/results/20261015-231007-e2-rescored`).** The reference lists were fetched again with Semantic Scholar's influence flags. 5 of 501 failed (HTTP 429); none is one of the 40 query papers, so the gold sets are complete.

| Gold | Query papers | Cited papers | fused top 6 | reranked top 6 | hybrid top 6 | In the pool |
| --- | --- | --- | --- | --- | --- | --- |
| all | 40 | 273 | 14 (5%) | 10 (4%) | 11 (4%) | 46 (17%) |
| influential | 19 | 39 | 4 (10%) | 2 (5%) | 2 (5%) | 7 (18%) |
| method | 13 | 39 | 2 (5%) | 0 | 0 | 6 (15%) |

- Restricting to influential citations doubles the fused hit rate (5% to 10%), as the background-citation explanation predicts, but on 39 papers the difference is 4 against 2. It is a direction, not a result.
- The real bottleneck is the pool: whatever the gold, only 15 to 18% of cited papers are in the top 30, so no ordering can find the rest.
- On E2 the reranker is never better than the fused order, the opposite of E7 and E8. One difference may explain it: E2's idea is a whole abstract (up to 4,000 characters), E7's ideas are one sentence. The reranker reads the idea and a passage together in at most 512 tokens (bge-reranker-base, `truncate: true`). With a 250 to 400-token abstract as the idea, most of each passage is cut off before it is scored. This is a hypothesis. Test: `e2_leave_one_out.py run --idea-chars 600 --label short-idea` gives the reranker room to read the passage.

**Truncation hypothesis tested: rejected (2026-10-16, `eval/results/20261015-231237-e2-retrieval-short-idea`, idea = first 600 characters of the abstract).**

| Gold | fused top 6 (4,000 → 600 chars) | reranked | hybrid | In the pool |
| --- | --- | --- | --- | --- |
| all (273) | 14 → 15 | 10 → 12 | 11 → 11 | 46 → 50 |
| influential (39) | 4 → 5 | 2 → 2 | 2 → 2 | 7 → 8 |
| method (39) | 2 → 4 | 0 → 1 | 0 → 2 | 6 → 8 |

The shorter idea helps every order slightly, mostly by putting a few more cited papers in the pool. But the reranker still never beats the fused order, so truncation is not why it loses on E2.

A better explanation is that E2 asks a different question. A cross-encoder scores how far a passage says the same thing as the idea. That is what a novelty check needs: E7 and E8 have known overlapping papers, and there the reranker helps. A paper's citations are, by definition, other work: earlier methods it builds on, baselines, datasets. The more similar a cited paper is to the citing paper, the less reason there was to write the new one. So a ranking tuned to find overlap should demote most citations. E2 measures "related work" retrieval, not overlap retrieval.

**Decision:** keep the hybrid order (D28, D30). E2 is reported as a generalisation check of first-stage retrieval (the pool holds 17 to 18% of cited papers, about 3 times the 6% that 30 random papers would hold), with this limitation stated, not as the measure of the novelty check.

## Outside-library ideas, agent off vs on (2026-10-16)

Results: `eval/results/20261015-235329-agent-off`, `20261016-000420-agent-on` (branch `eval/m2-last-round-runs`). 4 ideas × 2 repeats, P1 v2, P2 v2, P3 v2. Their expected papers are not in the library (`prompt_ab.py` checked).

| | Agent off | Agent on |
| --- | --- | --- |
| Expected papers among the judge's candidates | 0/8 | 0/8 |
| Rounds that searched (all on the web) | 0 | 4 of 8 runs |
| Web papers that reached the judge | 0 | 0 |
| False overlap, out-of-domain idea (local only) | 0/2 | 0/2 |

The agent added nothing, for two different reasons:

**F-009 (code): web papers never reached the reranker.** For out-of-domain and speculative decoding, the planner chose the web, as P2 asks, and each query returned 10 papers. None was in the pool, so none could be admitted (`admission.admitted: []`, stop reason "the round found no new paper"). Cause: a web paper is in one ranked list (its query), while library papers collect fusion credit from all 5 or 6 local queries. A web paper's best fused score is 1/61; a library paper found by two queries already scores 2/110. So web papers could not make the pool of 30 that the reranker scores. The protein run of 2026-10-15 admitted two web papers only because the planner sent three web queries. Fix: the web papers a round fetched always join the pool and are scored by the reranker (`rank_candidates(always_rank=...)`), and admission still requires a reranker score of 0.2 and of the weakest candidate (D29). Test: `test_web_papers_reach_the_reranker_only_when_always_ranked_f009`.

**F-010 (P2 v2): a paper that uses a technique was taken for the work that introduced it.**
- LoRA, both repeats. The planner stopped at once: "Candidate 1 discusses LoRA, which freezes the base model and trains small low-rank adapters — exactly the method and task of the idea". Candidate 1 is a Ukrainian RAG system (2604.22095) that fine-tunes with LoRA. The judge labelled it `same`/`same`.
- Chain-of-Verification, both repeats. It stopped on PAVE (2603.20673), which applies the technique.
- In all four runs the novelty verdict was right (not novel), but the report credits papers that use the technique, and the web was never searched. This was v2's own "failure hypothesis to watch".
- Fix: P2 v3 adds that a paper matches the method only if it proposes or studies it. If the candidates only apply, compare against or cite it, the planner searches the web for the work that introduced it.

**Other observations.**
- Speculative decoding without the agent: closest work was 1 or 2 RAG papers rated `partial` task / `different` method. That is a weak but honest "nothing close here". The out-of-domain idea stayed at 0 overlap in both configurations.
- The planner's web queries for speculative decoding were vague ("auxiliary model token suggestion for parallel decoding"). Whether they find the right papers is now measurable, since web results are scored.

## Audit, step 3: first runs (2026-10-16)

**F-011 (pipeline, not prompt): every P4 call was refused by Groq.** First audit run on the team PC (`eval/results/20261016-170157-audit-claims`, 3 papers, all HTTP 500). n8n's log: `413 Request too large for model openai/gpt-oss-20b ... tokens per minute (TPM): Limit 8000, Requested 8069`. Groq counts a request as input plus the output budget (`max_tokens`). Batches of 1,800 counted tokens of sentences plus the prompt, the parser's format instructions and a 4,096-token output budget came to 8,069 to 8,213. Fix: batches of 700 counted tokens, a 3,000-token output budget, a 40 s pause between batches, 30 s between retries, and a test that keeps the estimate under 7,000 (D33).

**F-012 (pipeline): after the F-011 fix, calls were still refused, now for the minute's total.** Second run (`eval/results/20261016-171408-audit-claims`, 3 papers, HTTP 500). Groq's message: `Limit 8000, Used 7303, Requested 5578. Please try again in 36.6s`. Each request now fits, but two things combined:
- gpt-oss-20b is a reasoning model, and its hidden reasoning tokens count, so one call really uses 6,000 to 7,000 tokens.
- n8n's node retries wait at most 5 seconds (`waitBetweenTries` is capped), so the retries fired inside the same minute and failed too.

Fix:
- **The loop's state moved to pp-tools.** `POST /audit/{run}/claims/next` serves the first batch without a saved valid output.
- **n8n waits 65 s before every call.** An LLM error goes through the node's error output straight back to "Next claim batch", which serves the same batch again after the pause.
- **A batch is skipped and recorded after 3 attempts,** so a run always ends.
- **A rerun resumes** where the previous one stopped.

**F-013 (model and node): gpt-oss-20b returned nothing.** Third run (`eval/results/20261016-172946-audit-claims`, CRAG). The new loop worked: 6 batches, 3 attempts each, every one skipped, run finished in 21 minutes. But all 18 LLM calls failed. n8n's log: "The AI model returned an empty response to the Structured Output Parser" (8 times) and "Model output doesn't fit required format" (4 times). Cause:
- gpt-oss reasons before it answers, and with the default effort it spent the whole 3,000-token output budget on reasoning, leaving no answer.
- The budget cannot simply grow, because of the 8,000-tokens-per-minute cap (F-011).
- n8n's Groq chat-model node exposes neither `reasoning_effort` nor Groq's strict structured outputs.

Fix (D34):
- The extractor calls Groq's API from an n8n HTTP Request node with `reasoning_effort: "low"`, `include_reasoning: false` and a strict JSON schema, so the decoder can only produce schema-valid JSON.
- The output budget is 3,500 tokens, with a test that keeps input plus output under about 7,600.
- LLM errors are now stored as run steps (`claims_llm_error`) instead of disappearing into the retry loop.

**F-014 (n8n credential): the direct Groq call never left n8n.** Fourth run (`eval/results/20261016-181732-audit-claims`): again 18 failed calls, but the recorded error had been cut at 2,000 characters before the error field, because the error item carries the whole prompt first. Now only the error itself is recorded. A probe (`scripts/groq_claims_probe.py`) sent the identical request from Python, and Groq answered in 1.2 s:
- 2,586 tokens in total: 2,351 in, 235 out, of which 133 reasoning;
- finish reason "stop";
- valid JSON, accepted by pp-tools.

So Groq, the settings and the schema were right; n8n's HTTP Request node could not use the `groqApi` credential type, which belongs to n8n's Groq chat-model node. Fix: `n8n/entrypoint.sh` also creates the Groq key as a generic header credential (`httpHeaderAuth`, `Authorization: Bearer ...`), still only from `.env` and never in a workflow file.

With real usage measured (about 2,600 tokens per call, not the 6,000 to 7,000 seen with default reasoning), the pause between calls is 30 s and the output budget 2,500 tokens.

**F-015 (my bug, deployment): n8n stopped starting.** The F-014 change added a comment containing an apostrophe inside the single-quoted `node -e '...'` block of `n8n/entrypoint.sh`. The shell ended the string there, n8n restarted in a loop ("syntax error: unexpected '('"), and the audit request found nothing on port 5678. Fix: the apostrophe removed. `tests/test_n8n_entrypoint.py` now checks the script with `sh -n` and runs it with a stand-in n8n to check that every credential is written.


**F-016 (my bug, workflow): every Groq call failed before leaving n8n.** First full run on the team PC (2026-10-16, `eval/results/20261016-185700-audit-claims/`): 3 papers, 22 batches, 66 attempts, 0 claims; every attempt recorded `{"error":"invalid syntax"}`. The "Extract claims" body was one n8n expression with the JSON schema written inline. n8n ends an expression at the first `}}`, and the schema contains several (`false}}`), so the expression was cut short and never evaluated. The probe script missed it because it built the body in Python. Fix: pp-tools builds the whole request (`claims_request()` in `app/audit/claims.py`, schema next to the Pydantic models it mirrors) and returns it with each batch; the node now sends `{{ JSON.stringify($json.request) }}`, and the probe sends the same object. A new test fails if any expression in any workflow contains a stray `}}`. Lesson: the probe must exercise the same path as the workflow, not a copy of it.
