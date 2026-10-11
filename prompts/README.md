# Prompt library

Empty until milestone 2. This folder is a graded deliverable (20%): every prompt is versioned and every observed failure is logged.

## Layout

```
prompts/
  P3-novelty-judge/
    v1.md
    v2.md
  P4-claim-extractor/
    v1.md
  failure_log.md
```

## Each version file

```markdown
# P5 audit-judge — v3
- Role in system: what this prompt decides, and which node calls it
- Technique: few-shot, chain-of-thought, structured output, role, guardrails...
- Input / output schema: link to the contract
- Model and settings: model id, temperature
- Changelog: what changed from the previous version, and why
- Failures fixed: F-ids from failure_log.md
- Before / after: the same input with the old and new output, and its test id

## Prompt
<the exact prompt text>
```

## Rules

- Never edit a published version. Copy it to the next version number and change the copy.
- Log a failure the day you see it, in `failure_log.md`: id, prompt and version, input, wrong output, cause, fix, version that fixed it.
- Keep failures that were never fixed. The brief gives credit for them.
