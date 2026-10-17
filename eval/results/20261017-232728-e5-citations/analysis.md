# E5: reading of the results (by hand)

## Retrieval

The strict measure (the gold evidence phrase appears in one of the three passages) found **29/40**. The phrase is a lower bound: a passage can state the same fact in other words. The 11 misses, read one by one:

| Pair | Gold | What the three passages contain | Enough to judge? |
| --- | --- | --- | --- |
| e5-01 | supported | RAG-Sequence (section 2.1) and RAG-Token (decoding) described in other words | yes |
| e5-02 | unsupported | "the top K documents are retrieved ... choose content from several documents" | yes (contradicts) |
| e5-07 | supported | "increasing the number of passages from 10 to 100 leads to 6% improvement" | yes |
| e5-08 | unsupported | the same sentence | yes (contradicts) |
| e5-15 | supported | two figure captions and the introduction: "IRCoT interleaves chain-of-thought generation and retrieval steps" | yes |
| e5-24 | unsupported | Related work, a mid-sentence abstract chunk about conventional RAG, the inference overview | no: the misleading chunk led to the one accepted error (F-029) |
| e5-28 | unsupported | the evaluator is "lightweight", but no passage names T5 | no (verdict "not found", safe) |
| e5-30 | unsupported | section 3.2: strategies chosen by query complexity | yes (contradicts) |
| e5-m1-atlas | supported | abstract: "strong few-shot learning" | yes |
| e5-m4-dpr | supported | "78.4% vs. 59.1% for top-20 accuracy on Natural Questions" | yes |
| e5-m4-realm | unsupported | implementation details and ablations, nothing on how the retriever is trained | no (verdict "not found", safe) |

So the passages were enough to judge **37 of 40** pairs; the 3 others were the pairs where retrieval missed the deciding statement.

## Verdicts

- Every supported pair was recognised (25/25), with no false alarm, by both prompt versions.
- 14 of 15 changed claims were rejected: 11 contradicted with a passage, 3 "not found". The one accepted (e5-24) is a passage cut mid-sentence (F-029).
- P6 v1 and v2 gave identical verdicts on all 40 pairs. v2 stays inactive.

## Limits

- 40 pairs, 15 papers, written by one author (Claude) from the papers' text; the changed claims are deliberate edits, easier to spot than real misattributions.
- All papers have full text in the library; for abstract-only papers fewer statements are retrievable.
- One run, temperature 0; Groq's gpt-oss output still varies slightly between runs.
