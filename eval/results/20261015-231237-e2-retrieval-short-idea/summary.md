# E2 leave-one-out (retrieval)

```json
{
  "mode": "retrieval",
  "idea_chars": 600,
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
| all | fused | 15/273 (5%) | 50/273 | 0.035 | 0.08 | 40 |
| all | reranked | 12/273 (4%) | 50/273 | 0.028 | 0.05 | 40 |
| all | hybrid | 11/273 (4%) | 50/273 | 0.029 | 0.05 | 40 |
| method | fused | 4/39 (10%) | 8/39 | 0.050 | 0.13 | 13 |
| method | reranked | 1/39 (3%) | 8/39 | 0.018 | 0.03 | 13 |
| method | hybrid | 2/39 (5%) | 8/39 | 0.024 | 0.05 | 13 |
| influential | fused | 5/39 (13%) | 8/39 | 0.037 | 0.09 | 19 |
| influential | reranked | 2/39 (5%) | 8/39 | 0.045 | 0.04 | 19 |
| influential | hybrid | 2/39 (5%) | 8/39 | 0.032 | 0.03 | 19 |

Each query paper's own abstract is the idea; the paper itself is removed from the ranking; the expected
papers are the ones it cites that are in the library (Semantic Scholar reference lists): all of them,
only influential citations, or only citations for the method. Query papers with no citation of a kind are
left out of that kind's rows.
