# E1 on the test split: the one run (2026-10-18)

Chunking A (fixed size) against B (structured, by section and table), on the 132 test queries that no tuning ever saw. Run once, after the review below; the dev numbers are from `eval/results/20261017-002740-e1-dev-v1/` (41 queries).

| Queries | n | Fixed R@1 | Structured R@1 | Fixed MRR@20 | Structured MRR@20 |
| --- | --- | --- | --- | --- | --- |
| **Test, all** | 132 | 0.773 | **0.894** | 0.862 | **0.938** |
| Test, paraphrase | 47 | 0.830 | 0.915 | 0.888 | 0.957 |
| Test, section | 44 | 0.705 | 0.818 | 0.828 | 0.883 |
| Test, table | 41 | 0.780 | 0.951 | 0.868 | 0.976 |
| Dev, all (for comparison) | 41 | 0.780 | 0.902 | 0.864 | 0.943 |

## Reading

- **The dev result holds on unseen queries.** Structured chunking finds the right paper first 89 % of the time against 77 %, almost exactly the dev gap (90 % against 78 %). Nothing was tuned to the test queries, so this is the number to report.
- **The gap is not chance.** On the same 132 queries, structured is right at rank 1 where fixed is wrong 20 times, and the reverse 4 times: exact McNemar test, p = 0.0015. The MRR@20 difference is 0.077, with a 95 % bootstrap interval of [0.034, 0.121] (10,000 resamples of queries).
- **Tables gain most:** 0.78 to 0.95 at rank 1. A table kept whole, with its caption, is one chunk that matches a question about it; fixed-size chunks cut tables into pieces without their caption.
- **Both find the paper in the top 20 every time** (recall@20 = 1.0). The difference is ranking: how often the right paper comes first, which matters because the citation check and the novelty judge read only the top passages.
- **Where fixed wins (4 queries):** `2210_06345-sec`, `2412_14304-sec`, `2602_19317-tbl`, `2106_11517-sec`. Three are section queries, the type where structured gains least (0.70 to 0.82); a long section split into one chunk can dilute a narrow question about part of it.

## Review before the run (D38)

A team member reviewed a fixed-seed sample of 20 test queries (`eval/queries/test_review.xlsx`: 7 paraphrase, 7 section, 6 table), asking whether a person could plausibly search with each one, before any test result existed. All 20 were judged fine; no query was changed.

## Limits

- One relevant paper per query: another paper that also answers a broad query counts as a miss for both strategies, so absolute recall is a lower bound, while the A against B comparison stays fair.
- The queries were written by an LLM reading the paper. The review checked 20 of 132 for plausibility, not all of them.
