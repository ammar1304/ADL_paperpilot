# E2 leave-one-out (retrieval (stored pools))

```json
{
  "mode": "retrieval (stored pools)",
  "pools_from": "eval\\results\\20261015-211913-e2-retrieval",
  "queries": 40,
  "built": {
    "built_at": "2026-10-15T23:10:04+00:00",
    "papers_with_reference_lists": 501,
    "papers_citing_library_papers": 466,
    "min_cited": 3,
    "seed": 13,
    "queries": 40,
    "cited_total": 273,
    "cited_influential_total": 39,
    "cited_method_total": 39
  }
}
```

| Gold | Method | Cited papers reaching the judge (top 6), micro | In the pool (top 30) | MRR | Macro recall@6 | Query papers |
| --- | --- | --- | --- | --- | --- | --- |
| all | fused | 14/273 (5%) | 46/273 | 0.033 | 0.06 | 40 |
| all | reranked | 10/273 (4%) | 46/273 | 0.026 | 0.04 | 40 |
| all | hybrid | 11/273 (4%) | 46/273 | 0.029 | 0.05 | 40 |
| method | fused | 2/39 (5%) | 6/39 | 0.026 | 0.06 | 13 |
| method | reranked | 0/39 (0%) | 6/39 | 0.012 | 0.00 | 13 |
| method | hybrid | 0/39 (0%) | 6/39 | 0.015 | 0.00 | 13 |
| influential | fused | 4/39 (10%) | 7/39 | 0.037 | 0.06 | 19 |
| influential | reranked | 2/39 (5%) | 7/39 | 0.030 | 0.03 | 19 |
| influential | hybrid | 2/39 (5%) | 7/39 | 0.028 | 0.03 | 19 |

Each query paper's own abstract is the idea; the paper itself is removed from the ranking; the expected
papers are the ones it cites that are in the library (Semantic Scholar reference lists): all of them,
only influential citations, or only citations for the method. Query papers with no citation of a kind are
left out of that kind's rows.
