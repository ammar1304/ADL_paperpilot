# Audits with gpt-oss-120b (D40), compared with the 20b-only final run

Run: `eval/results/20261018-172535-audit-120b/` (2026-10-18). Baseline: `eval/results/20261017-214450-audit-final/` (gpt-oss-20b only).
Every batch was done by `openai/gpt-oss-120b`: no LLM error, no rate limit, no skipped figure. Same candidate sentences as the baseline (79, 71 and 75).

| Paper | Claims kept (20b → 120b) | Claims with a citation marker | Citations judged in the library | Number checks (s / c / u) | Figure checks (s / c / n) | Time |
| --- | --- | --- | --- | --- | --- | --- |
| CRAG (2401.15884v3) | 24 → 43 | 0 → 10 | 0 → 7 (5 supported, 2 not found) | 13/0/0 → 13/0/3 | 3/0/0 → 3/0/0 | 431 s → 543 s |
| Adaptive-RAG (2403.14403v2) | 23 → 41 | 3 → 15 | 2 → 17 (7 supported, 1 contradicted, 9 not found) | 0/0/4 → 0/0/4 | 3/0/2 → 11/0/0 | 658 s → 886 s |
| FLARE (2305.06983v2) | 8 → 26 | 2 → 8 | 3 → 9 (3 supported, 6 not found) | 0/0/0 → 0/0/1 | 1/0/0 → 6/0/0 | 481 s → 624 s |
| **Total** | **55 → 110** | **5 → 33** | **5 → 33** | | **7 → 20 supported** | |

(s / c / u = supported / contradicted / unverifiable; s / c / n = supported / contradicted / not shown.)

## What changed

- **120b keeps twice as many claims and six times as many citation claims.** The citation check, the audit's RAG step, went from 5 judged citations to 33. This confirms D40: the cheaper extractor was starving the most expensive check.
- **The figure check also had more to do:** 20 figure verdicts instead of 7, all supported. Adaptive-RAG's two "not shown" from the baseline (F-025, the claim naming Figures 4 and 5) now have one verdict each, supported, because 120b split the claim by figure.
- **Cost:** about 25 % more time per paper (more claims to check). Each audit still fits within the daily free tokens.

## Checked by hand

- **The one "contradicted" is a false alarm (F-030).** Adaptive-RAG: "The same BM25 retriever is used across all models for a fair comparison" (citing Trivedi et al., 2023). The evidence passage says OneR "uses the question as a query to retrieve K paragraphs" and that IRCoT uses BM25; it never says OneR uses another retriever, and in the IRCoT paper both use BM25. The judge read a contradiction into what the passage leaves out.
- **"Not found" is mostly missing evidence, as designed.** 8 of the 17 "not found" are against abstract-only papers (RETRO, Atlas, REPLUG, the self-ask paper), where the claim's detail can only be in the full text. Several others are multi-citation sentences judged as a whole (F-024): the sentence says more than the one cited paper does.
- **One likely miss:** CRAG, "The original PopQA dataset consists of 14k samples, with 1,399 used for testing" (Asai et al., 2024) came back "not found", while another check on the same paper found Self-RAG's passage about the 1,399 long-tail queries. Retrieval returned different passages for the two queries.

## For the report

With 120b, a three-paper audit produces 110 claims, 20 figure verdicts and 33 citation verdicts, with 0 contradictions on numbers and figures and 1 false alarm among 33 citation verdicts.
