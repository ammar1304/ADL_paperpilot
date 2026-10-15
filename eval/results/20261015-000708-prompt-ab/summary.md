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
  "pause_s": 60,
  "rescored_at": "2026-10-15T00:55:28+00:00"
}
```

| Config | Runs ok | Expected papers in candidates | Expected in closest work | Query similarity | False overlap | LLM states scope | LLM overclaims | LLM calls unspecified facets (un)covered | LLM mentions unspecified facets | Label agreement across repeats | Judge attempts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 12/12 | 8/22 | 8/22 | 0.15 | 0/12 | 0/12 | 5/12 | 7/12 | 7/12 | 0.83 | 1.08 |
| judge-v2 | 12/12 | 9/22 | 9/22 | 0.16 | 0/12 | 12/12 | 0/12 | 0/12 | 4/12 | 0.96 | 1.17 |
| full-v2 | 12/12 | 10/22 | 10/22 | 0.14 | 0/12 | 12/12 | 0/12 | 0/12 | 4/12 | 0.84 | 1.00 |

Expected papers among the judge's candidates, per idea (found / expected, summed over repeats):

| Idea | baseline | judge-v2 | full-v2 |
| --- | --- | --- | --- |
| adaptive-retrieval | 3/10 | 3/10 | 2/10 |
| iterative-rewrite | 1/6 | 1/6 | 2/6 |
| retrieval-evaluator | 2/2 | 2/2 | 2/2 |
| reflection-tokens | 0/2 | 1/2 | 2/2 |
| hypothetical-document | 2/2 | 2/2 | 2/2 |
| out-of-domain | 0/0 | 0/0 | 0/0 |

Higher is better for: expected papers found, LLM states scope, label agreement.
Lower is better for: query similarity (1.0 = paraphrases), false overlap, overclaims, coverage claims and mentions of unspecified facets, judge attempts.
A coverage claim ("data and results remain uncovered") is the F-003 failure; a mention ("no passage gives data or results") is a milder rule breach.
Counts are over successful runs; the regular expressions are in scripts/prompt_ab.py.
