# Retrieval A/B

```json
{
  "runs": "eval\\results\\20261015-000708-prompt-ab\\runs.jsonl",
  "query_sets": 25,
  "calls": {
    "idea": {
      "rerank": true,
      "idea_as_query": true,
      "rerank_abstract": false,
      "order": "rerank"
    },
    "idea+abs": {
      "rerank": true,
      "idea_as_query": true,
      "rerank_abstract": true,
      "order": "rerank"
    }
  },
  "methods": {
    "fused+idea": [
      "idea",
      "fused"
    ],
    "reranked+idea": [
      "idea",
      "reranked"
    ],
    "hybrid+idea": [
      "idea",
      "hybrid"
    ],
    "reranked+idea+abs": [
      "idea+abs",
      "reranked"
    ],
    "hybrid+idea+abs": [
      "idea+abs",
      "hybrid"
    ]
  },
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
| fused+idea | 26/57 | 50/57 | 0.332 | 18.9 | 0 |
| reranked+idea | 34/57 | 50/57 | 0.408 | 18.9 | 0 |
| hybrid+idea | 37/57 | 50/57 | 0.388 | 18.9 | 0 |
| reranked+idea+abs | 29/57 | 50/57 | 0.41 | 18.3 | 0 |
| hybrid+idea+abs | 34/57 | 50/57 | 0.371 | 18.3 | 0 |

Per idea, expected papers in the top 6 (summed over the stored query sets):

| Idea | fused+idea | reranked+idea | hybrid+idea | reranked+idea+abs | hybrid+idea+abs |
| --- | --- | --- | --- | --- | --- |
| adaptive-retrieval | 8/30 | 18/30 | 18/30 | 13/30 | 15/30 |
| iterative-rewrite | 4/12 | 1/12 | 4/12 | 1/12 | 4/12 |
| retrieval-evaluator | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 |
| reflection-tokens | 2/3 | 3/3 | 3/3 | 3/3 | 3/3 |
| hypothetical-document | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 |

Every setting ranks the same stored queries; no LLM is called. MRR counts an expected paper outside the pool as 0.
