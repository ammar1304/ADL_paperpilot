# E4 figure check, variant B (reasoning none, json): 10/11 calls answered, 35 claims, qwen/qwen3.8-27b, P5 v2

| Expected \ got | supported | contradicted | not_shown | not_answered |
| --- | --- | --- | --- | --- |
| supported | 17 | 1 | 0 | 0 |
| contradicted | 1 | 13 | 0 | 0 |
| not_shown | 0 | 0 | 3 | 0 |

Accuracy: 33/35
False alarms (a true claim called contradicted): 1/18
Errors caught: 13/14; accepted as supported: 1
Output tokens per answered call (mean): 554

Wrong verdicts:
- FLARE figure:4: expected contradicted, got supported: On StrategyQA, single-time retrieval outperforms no retrieval. | model: In the StrategyQA group, the red bar (Single-time ret.) is labeled 68.6, which is higher than the blue bar (No ret.) labeled 72.9. Wait, looking closer at the image, the blue bar is 72.9 and the red b
- FLARE figure:5: expected supported, got contradicted: StrategyQA peaks at 77.3 when about 40% of the steps retrieve. | model: The peak performance for StrategyQA is 77.3, which occurs at the 25.0% mark on the x-axis, not at 40%.

Failed calls:
- Adaptive-RAG figure:1: HTTP 429 {'error': {'message': 'Rate limit reached for model `qwen/qwen3.8-27b` in organization `org_01m4xjkb00ebnvv25jnnhs431t` service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199890, Req
