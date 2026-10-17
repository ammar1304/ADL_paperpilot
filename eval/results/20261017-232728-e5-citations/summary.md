# E5 citation check: e5-citations

Gold: 40 pairs (25 supported, 15 unsupported), 15 papers.

## Retrieval (no LLM)

Gold evidence in the 3 passages sent to the judge: **29/40** (rank 1: 19, rank 2: 8, rank 3: 2).

Missed: e5-01 (arxiv:2005.11401), e5-02 (arxiv:2005.11401), e5-07 (arxiv:2007.01282), e5-08 (arxiv:2007.01282), e5-15 (arxiv:2212.10509), e5-24 (arxiv:2310.11511), e5-28 (arxiv:2401.15884), e5-30 (arxiv:2403.14403), e5-m1-atlas (arxiv:2208.03299), e5-m4-dpr (arxiv:2004.04906), e5-m4-realm (arxiv:2002.08909)

## Verdicts, P6 v1

| Measure | Result |
| --- | --- |
| Supported pairs recognised | 25/25 |
| False alarms (supported pair called contradicted) | 0/25 |
| Errors accepted (unsupported pair called supported) | 1/15 |
| Errors called contradicted | 11/15 |
| Two-citation sentences, supported parts recognised | 7/7 |
| Pairs without a verdict | 0 |

Gold -> verdict: supported -> supported: 25, unsupported -> contradicted: 11, unsupported -> not_found: 3, unsupported -> supported: 1

Disagreements:
- e5-24 (changed method, gold unsupported): supported | Self-RAG decides when to retrieve with a fixed rule: it retrieves once, before generation, for every query. | Passage states Self-RAG consistently retrieves a fixed number of documents before generation for every query.

## Verdicts, P6 v2

| Measure | Result |
| --- | --- |
| Supported pairs recognised | 25/25 |
| False alarms (supported pair called contradicted) | 0/25 |
| Errors accepted (unsupported pair called supported) | 1/15 |
| Errors called contradicted | 11/15 |
| Two-citation sentences, supported parts recognised | 7/7 |
| Pairs without a verdict | 0 |

Gold -> verdict: supported -> supported: 25, unsupported -> contradicted: 11, unsupported -> not_found: 3, unsupported -> supported: 1

Disagreements:
- e5-24 (changed method, gold unsupported): supported | Self-RAG decides when to retrieve with a fixed rule: it retrieves once, before generation, for every query. | The abstract states that Self-RAG consistently retrieves a fixed number of documents for generation and never revisits retrieval, matching the claim of retrievi

