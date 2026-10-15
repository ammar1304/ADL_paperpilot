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
    "agent-off": {
      "P1-decomposer": "v2",
      "P2-search-planner": "v2",
      "P3-novelty-judge": "v2"
    }
  },
  "repeats": 2,
  "pause_s": 60
}
```

| Config | Runs ok | Expected papers in candidates | Expected in closest work | Query similarity | False overlap | LLM states scope | LLM overclaims | LLM calls unspecified facets (un)covered | LLM mentions unspecified facets | Label agreement across repeats | Judge attempts | Agent search rounds | Expected papers added by the agent |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| agent-off | 12/12 | 16/22 | 16/22 | 0.13 | 0/12 | 12/12 | 0/12 | 0/12 | 2/12 | 0.88 | 1.00 | 0.00 | 0 |

Expected papers among the judge's candidates, per idea (found / expected, summed over repeats):

| Idea | agent-off |
| --- | --- |
| adaptive-retrieval | 8/10 |
| iterative-rewrite | 2/6 |
| retrieval-evaluator | 2/2 |
| reflection-tokens | 2/2 |
| hypothetical-document | 2/2 |
| out-of-domain | 0/0 |

Higher is better for: expected papers found, LLM states scope, label agreement.
Lower is better for: query similarity (1.0 = paraphrases), false overlap, overclaims, coverage claims and mentions of unspecified facets, judge attempts.
A coverage claim ("data and results remain uncovered") is the F-003 failure; a mention ("no passage gives data or results") is a milder rule breach.
Counts are over successful runs; the regular expressions are in scripts/prompt_ab.py.
