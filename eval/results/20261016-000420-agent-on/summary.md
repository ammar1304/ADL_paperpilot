# Prompt A/B

```json
{
  "ideas": [
    "out-of-domain",
    "draft-and-verify-decoding",
    "low-rank-finetuning",
    "verify-own-answer"
  ],
  "configs": {
    "agent-on": {
      "P1-decomposer": "v2",
      "P2-search-planner": "v2",
      "P3-novelty-judge": "v2"
    }
  },
  "repeats": 2,
  "pause_s": 60,
  "outside_library_papers_found_in_library": []
}
```

| Config | Runs ok | Expected papers in candidates | Expected in closest work | Query similarity | False overlap | LLM states scope | LLM overclaims | LLM calls unspecified facets (un)covered | LLM mentions unspecified facets | Label agreement across repeats | Judge attempts | Agent search rounds | Expected papers added by the agent | Closest work from the web |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| agent-on | 8/8 | 0/8 | 0/8 | 0.15 | 0/8 | 8/8 | 0/8 | 0/8 | 0/8 | 0.85 | 1.00 | 0.50 | 0 | 0 |

Expected papers among the judge's candidates, per idea (found / expected, summed over repeats):

| Idea | agent-on |
| --- | --- |
| out-of-domain | 0/0 |
| draft-and-verify-decoding | 0/4 |
| low-rank-finetuning | 0/2 |
| verify-own-answer | 0/2 |

Higher is better for: expected papers found, LLM states scope, label agreement.
Lower is better for: query similarity (1.0 = paraphrases), false overlap, overclaims, coverage claims and mentions of unspecified facets, judge attempts.
A coverage claim ("data and results remain uncovered") is the F-003 failure; a mention ("no passage gives data or results") is a milder rule breach.
False overlap counts closest work from the local library only; closest work from the web is real or not, and is checked by hand (F-008).
Counts are over successful runs; the regular expressions are in scripts/prompt_ab.py.
