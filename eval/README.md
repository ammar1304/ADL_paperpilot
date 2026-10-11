# Evaluation

Empty until milestone 1. Retrieval and answer quality are measured separately.

| Test | Starts in | Question |
| --- | --- | --- |
| E1 chunking | Milestone 1 | Fixed-size vs structure-aware: Recall@k and MRR |
| E2 novelty | Milestone 2 | Leave-one-out: does the system find the papers a paper builds on? |
| E3 planted errors | Milestone 3 | Detection rate of altered table values, inflated claims, swapped citations |
| E4 gold claims | Milestone 3 | Audit judge accuracy on about 30 labelled claims |
| E5 citation faithfulness | Milestone 3 | Faithful vs unfaithful citation pairs |
| E6 verifier on/off | Milestone 3 | Invented citations and untraceable numbers per run |

## Layout

```
eval/
  queries/      dev.jsonl and test.jsonl (tune on dev, report test once)
  gold/         labelled claims and citation pairs
  mutations/    the mutation script and a log of every planted error
  results/      one folder per run, dated, with the config used
```

Every result file records the git commit, the chunking strategy, the embedding model and the prompt versions it ran with.
