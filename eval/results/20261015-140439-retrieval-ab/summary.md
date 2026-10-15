# Retrieval A/B

```json
{
  "runs": "eval\\results\\20261015-000708-prompt-ab\\runs.jsonl",
  "query_sets": 25,
  "methods": [
    "fused",
    "fused+idea",
    "reranked",
    "reranked+idea"
  ],
  "top": 6,
  "health": {
    "status": "ok",
    "service": "pp-tools",
    "checks": {
      "postgres": {
        "ok": true
      },
      "qdrant": {
        "ok": true,
        "http_status": 200
      },
      "embedder": {
        "ok": true,
        "required": false
      },
      "reranker": {
        "ok": true,
        "required": false
      }
    }
  }
}
```

| Method | Expected papers reaching the judge (top 6) | In the pool (top 30) | MRR | Seconds per ranking | Rerank did not run |
| --- | --- | --- | --- | --- | --- |
| as run in E7 (20 chunks per query, no reranker) | 25/57 | - | - | - | - |
| fused | 24/57 | 45/57 | 0.29 | 2.6 | 0 |
| fused+idea | 26/57 | 50/57 | 0.332 | 2.8 | 0 |
| reranked | 30/57 | 45/57 | 0.4 | 18.7 | 0 |
| reranked+idea | 34/57 | 50/57 | 0.408 | 18.9 | 0 |

Per idea, expected papers in the top 6 (summed over the stored query sets):

| Idea | fused | fused+idea | reranked | reranked+idea |
| --- | --- | --- | --- | --- |
| adaptive-retrieval | 7/30 | 8/30 | 15/30 | 18/30 |
| iterative-rewrite | 4/12 | 4/12 | 0/12 | 1/12 |
| retrieval-evaluator | 6/6 | 6/6 | 6/6 | 6/6 |
| reflection-tokens | 2/3 | 2/3 | 3/3 | 3/3 |
| hypothetical-document | 5/6 | 6/6 | 6/6 | 6/6 |

Every setting ranks the same stored queries; no LLM is called. MRR counts an expected paper outside the pool as 0.
