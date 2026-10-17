# Prompt library

Every prompt PaperPilot sends to an LLM lives here, versioned. The n8n workflows never contain prompt text: they ask pp-tools to render the active version (`POST /prompts/{name}/render`), so what runs is always what is in this folder, and every run records the version it used (`GET /runs/{id}/steps`).

This folder is a graded deliverable (20% of the grade).

## Prompts

| Prompt | Active | Role | Model | Technique | Called by |
| --- | --- | --- | --- | --- | --- |
| [P1-decomposer](P1-decomposer/v2.md) | v2 (v1 kept) | Splits an idea into task, method, data, result; writes search queries | gpt-oss-20b (Groq), Gemini fallback | Role, rules, one worked example, vocabulary-diversity rule, JSON output | 20-novelty "Decompose idea" |
| [P2-search-planner](P2-search-planner/v2.md) | v2 (v1 kept) | The search agent: each round, stop or search again (local library or Semantic Scholar) with new queries | gpt-oss-20b (Groq), Gemini fallback | Decision criteria, tool descriptions, state (round budget, history), one worked example, JSON output; limits enforced in code | 20-novelty "Plan search" |
| [P3-novelty-judge](P3-novelty-judge/v2.md) | v2 (v1 kept) | Compares the idea with each retrieved paper per facet, citing chunk ids | gpt-oss-120b (Groq), Gemini fallback | Fixed taxonomy with definitions, search scope, unknown versus not covered, counter-example from a real failure, evidence guardrail, JSON output, verifier feedback on retry | 20-novelty "Judge novelty" |

| [P4-claim-extractor](P4-claim-extractor/v1.md) | v1 | Audit: turns candidate sentences of a draft into typed, checkable claims (values, comparisons, table/figure references, citations) | gpt-oss-20b (Groq API, `reasoning_effort: low`, strict JSON schema, D34), **no fallback** (drafts never go to the Gemini free tier) | Closed claim taxonomy with definitions, copy-numbers-exactly rule, explicit-reference-only rule, skip rules, one worked example, JSON output; code pre-selects sentences and drops claims whose numbers are not in the sentence | 30-audit "Extract claims" |

| [P5-figure-checker](P5-figure-checker/v2.md) | v2 (v1 kept) | Audit, multimodal: checks the claims about a figure against the figure image (supported, contradicted, not shown), after describing what the figure shows | qwen/qwen3.8-27b (Groq vision, `reasoning_effort: low`, hidden reasoning, request settings chosen by E4, D36), **no fallback** | Image-as-evidence rule (the caption identifies, it does not prove), read-before-judge, explicit "not shown", tolerance for approximate plot reading, numbered claims, JSON output; code groups claims per figure and validates | 30-audit "Check figure" |

| [P6-citation-checker](P6-citation-checker/v1.md) | v1 | Audit, RAG: judges whether passages retrieved from the cited library paper support what the draft attributes to it | gpt-oss-20b (Groq API, `reasoning_effort: low`, strict JSON schema), gpt-oss-120b on the daily limit, **no Gemini** | Retrieval-grounded judging, absence is "not found" never "contradicted", weakest-part rule, evidence ids required, JSON output; code resolves the citation, retrieves and reranks the passages, and validates | 30-audit "Check citations" |

## How the prompts are protected

1. **n8n's Structured Output Parser** rejects output that does not match the JSON schema (`Facets schema`, `Judgement schema` nodes).
2. **pp-tools validates the same output again,** strictly (`app/novelty.py`). A test checks that the two schemas agree.
3. **The verifier** (code, no LLM) rejects any paper or chunk id the retrieval did not return. The judge gets one retry, with the rejection reasons added to its prompt as feedback. Whatever is still wrong is shown as "unverified", never hidden.
4. **Failure hypotheses:** each prompt lists them, and real failures go to [failure_log.md](failure_log.md).

## Files

```
prompts/
  active.json            which version of each prompt is in use
  P1-decomposer/v1.md    one file per version
  P1-decomposer/v2.md
  P2-search-planner/v1.md
  P2-search-planner/v2.md
  P3-novelty-judge/v1.md
  P3-novelty-judge/v2.md
  P4-claim-extractor/v1.md
  failure_log.md         every observed failure
```

A version file has a front-matter header (name, version, model, technique), free documentation (purpose, design choices, failure hypotheses, changelog), and exactly two sections sent to the model: `## System` and `## User`. Variables are written `{{name}}`. Rendering fails if one is missing.

## Changing a prompt

1. **Never edit a version that has been used in a run.** Copy `v1.md` to `v2.md` and change the copy (front matter `version: v2`).
2. **Write the changelog** at the top of v2: what changed, which failure it fixes (`F-007`), and a before/after example on the same input.
3. **Switch** `active.json` to `v2`. pp-tools reads the library on every call, so no restart is needed.
4. **Run the tests** (`pytest tests/test_prompts.py`) and the same input again; record the outcome in `failure_log.md`.
5. **If you change the output format,** change the parser schema in the n8n workflow and the pp-tools contract together; the tests fail if they disagree.

Rolling back is the reverse: set `active.json` to the old version.
