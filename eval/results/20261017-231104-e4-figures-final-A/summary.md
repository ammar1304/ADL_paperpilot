# E4 figure check, variant A (reasoning none, schema), up to 3 attempt(s) per figure: 14/14 figures answered (13 at the first attempt), 15 calls, 48 claims, qwen/qwen3.8-27b, P5 v2

| Expected \ got | supported | contradicted | not_shown | not_answered |
| --- | --- | --- | --- | --- |
| supported | 24 | 1 | 0 | 0 |
| contradicted | 0 | 19 | 0 | 0 |
| not_shown | 0 | 0 | 4 | 0 |

Accuracy: 47/48
False alarms (a true claim called contradicted): 1/25
Errors caught: 19/19; accepted as supported: 0
Output tokens per answered call (mean): 384

Wrong verdicts:
- FLARE figure:5: expected supported, got contradicted: StrategyQA peaks at 77.3 when about 40% of the steps retrieve. | model: The red line (StrategyQA) reaches its highest labeled value of 77.3 at the x-axis value of 50.0, not 40.0.

Failed calls:
- FLARE figure:4: HTTP 400 {'error': {'message': "max completion tokens reached before generating a valid document: the output was truncated to fit max_completion_tokens and is missing required content. Increase max_completion_
