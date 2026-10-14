# Prompt failure log

Every wrong output seen in a real run goes here the same day, including failures that were never fixed. This log and the before/after examples are what the prompt-engineering grade rests on.

How to add an entry:
1. Copy the template row and give it the next id (F-001, F-002, ...).
2. Keep the exact input and output: the run id is enough if the run is stored (`GET /runs/{id}` and the `run_steps` table hold the prompt version and the model output).
3. When a new prompt version fixes it, fill in "Fixed in" and add the before/after to the new version's changelog.

| Id | Date | Prompt and version | Run id or input | Wrong output | Likely cause | Fix | Fixed in | Still failing on |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F-000 | (template) | P3-novelty-judge v1 | run id | e.g. marked "same" on method for a paper that only shares the word "retrieval" | e.g. no definition of "same" strict enough | e.g. add a counter-example | v2 | — |

No real entries yet: the prompts have only run against a stand-in model in development. The first entries come from the first runs with a real Groq key.
