# E4 figure check, variant A (reasoning none, schema): 12/14 calls answered, 41 claims, qwen/qwen3.8-27b, P5 v2

| Expected \ got | supported | contradicted | not_shown | not_answered |
| --- | --- | --- | --- | --- |
| supported | 17 | 0 | 0 | 4 |
| contradicted | 0 | 14 | 0 | 2 |
| not_shown | 0 | 0 | 3 | 1 |

Accuracy: 34/41
False alarms (a true claim called contradicted): 0/21
Errors caught: 14/16; accepted as supported: 0
Output tokens per answered call (mean): 342

Wrong verdicts:
- RAPTOR figure:1: expected supported, got not_answered: The figure shows text chunks being clustered and each cluster summarized by an LLM to form a tree layer. | model: 
- RAPTOR figure:1: expected supported, got not_answered: A node stores an index, its child nodes, a summary text and a text embedding. | model: 
- RAPTOR figure:1: expected contradicted, got not_answered: The RAPTOR tree drawn in the figure has three nodes in its root layer. | model: 
- RAPTOR figure:1: expected not_shown, got not_answered: RAPTOR's clustering step uses Gaussian mixture models. | model: 
- FLARE figure:5: expected supported, got not_answered: On 2WikiMultihopQA, performance rises from 28.2 with no retrieval to 51.0 when every step retrieves. | model: 
- FLARE figure:5: expected contradicted, got not_answered: StrategyQA performance increases steadily as the share of steps with retrieval goes from 0 to 100%. | model: 
- FLARE figure:5: expected supported, got not_answered: StrategyQA peaks at 77.3 when about 40% of the steps retrieve. | model: 

Failed calls:
- FLARE figure:3: HTTP 400 {'error': {'message': "Generated JSON does not match the expected schema. Please adjust your prompt. See 'failed_generation' for more details. Error: jsonschema: '' does not validate with /required: m
- FLARE figure:4: HTTP 400 {'error': {'message': "max completion tokens reached before generating a valid document: the output was truncated to fit max_completion_tokens and is missing required content. Increase max_completion_
