# Prompt A/B

```json
{
  "ideas": [
    "adaptive-retrieval",
    "iterative-rewrite",
    "retrieval-evaluator",
    "reflection-tokens",
    "hypothetical-document",
    "out-of-domain"
  ],
  "configs": {
    "baseline": {
      "P1-decomposer": "v1",
      "P3-novelty-judge": "v1"
    },
    "judge-v2": {
      "P1-decomposer": "v1",
      "P3-novelty-judge": "v2"
    },
    "full-v2": {
      "P1-decomposer": "v2",
      "P3-novelty-judge": "v2"
    }
  },
  "repeats": 2,
  "pause_s": 60
}
```

| Config | Runs ok | Expected papers in candidates | Expected in closest work | Query similarity | False overlap | LLM states scope | LLM overclaims | LLM discusses unspecified facets | Label agreement across repeats | Judge attempts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 12/12 | 8/22 | 8/22 | 0.15 | 0/12 | 0/12 | 5/12 | 7/12 | 0.83 | 1.08 |
| judge-v2 | 12/12 | 9/22 | 9/22 | 0.16 | 0/12 | 12/12 | 0/12 | 4/12 | 0.96 | 1.17 |
| full-v2 | 12/12 | 10/22 | 10/22 | 0.14 | 0/12 | 12/12 | 0/12 | 4/12 | 0.84 | 1.00 |

Higher is better for: expected papers found, LLM states scope, label agreement.
Lower is better for: query similarity (1.0 = paraphrases), false overlap, overclaims, discussing unspecified facets, judge attempts.
Counts are over successful runs; the regular expressions are in scripts/prompt_ab.py.
