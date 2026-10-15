# E2 leave-one-out (retrieval)

```json
{
  "mode": "retrieval",
  "queries": 40,
  "built": {
    "built_at": "2026-10-15T21:18:53+00:00",
    "papers_with_reference_lists": 501,
    "papers_citing_library_papers": 466,
    "min_cited": 3,
    "seed": 13,
    "queries": 40,
    "cited_total": 273
  }
}
```

| Method | Cited papers reaching the judge (top 6), micro | In the pool (top 30) | MRR | Macro recall@6 | Query papers |
| --- | --- | --- | --- | --- | --- |
| fused | 14/273 (5%) | 46/273 | 0.033 | 0.06 | 40 |
| reranked | 10/273 (4%) | 46/273 | 0.026 | 0.04 | 40 |
| hybrid | 11/273 (4%) | 46/273 | 0.029 | 0.05 | 40 |

Each query paper's own abstract is the idea; the paper itself is removed from the ranking; the expected
papers are the ones it cites that are in the library (Semantic Scholar reference lists).
