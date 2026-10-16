"""Figure check (milestone 3, step 5): a vision model checks what the text says about a figure.

Standard library only, so the offline evaluation (scripts/e4_figure_check.py) builds
exactly the request the n8n workflow sends.

Code decides which claims go with which figure, builds the multimodal request
(image as a base64 data URL, Groq's vision format) and validates the answer;
the model only looks and judges (P5-figure-checker, D36).
"""

import base64
import json
from typing import Any

from ..textutil import count_tokens

FIGURE_MODEL = "qwen/qwen3.8-27b"  # the only generally available vision model on Groq (docs, 2026-10)
# qwen on Groq also has a limit of 1,000 *output* tokens a minute (not on the rate-limits page,
# seen in a 429, F-021), and Groq checks the output budget against it before answering.
FIGURE_MAX_OUTPUT = 900
# Request settings, chosen by E4 (scripts/e4_figure_check.py --variants): reasoning "none"
# or "low", and the answer as a strict JSON schema ("schema") or plain JSON mode ("json").
VARIANTS = {"A": ("none", "schema"), "B": ("none", "json"), "C": ("low", "json")}
# E4 (eval/results/20261016-233441-e4-figures-comparison.md): A answered 10 of 14 calls and got
# all 34 verdicts right; B answered every call but got 2 of 35 wrong (one false alarm, one error
# accepted). A's failures are visible and retried (3 attempts); B's are silent. So A (D36).
FIGURE_VARIANT = "A"
MAX_CLAIMS_PER_FIGURE = 8
# Groq counts input + output budget against 8,000 tokens a minute. An image is a flat 2,048,
# the prompt with a long caption and the schema about 950 counted tokens (x1.4 for real
# tokens), the output budget 1,500: about 1,950 counted tokens are left, 1,500 are used.
CLAIMS_TOKENS = 1500
CLAIM_CHARS, SENTENCE_CHARS = 300, 400
FIGURE_PACE_S = 45
VERDICTS = ("supported", "contradicted", "not_shown")

FIGURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "figure": {"type": "string"},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "integer"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "evidence": {"type": "string"},
                    "read_values": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["claim", "verdict", "evidence", "read_values"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["figure", "checks"],
    "additionalProperties": False,
}


def figure_jobs(paper: dict[str, Any], claims: list[dict[str, Any]], has_image) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(one job per figure with an image: {"figure", "claims": [claim indices]},
        findings that need no model: a claim about a figure the parser kept no image of).

    A claim goes with every figure it names in ``refers_to`` (already restricted to
    figures the paper has, D33); claims naming a figure that does not exist were
    flagged at extraction ("unknown_refs").
    """
    figures = {f["id"] for f in paper.get("figures", [])}
    by_figure: dict[str, list[int]] = {}
    findings = []
    for i, claim in enumerate(claims):
        for ref in claim.get("refers_to") or []:
            if not ref.startswith("figure:") or ref not in figures:
                continue
            if has_image(ref):
                by_figure.setdefault(ref, []).append(i)
            else:
                findings.append({"claim": i, "figure": ref, "verdict": "not_shown", "evidence": "the parser kept no image of this figure"})
        for ref in claim.get("unknown_refs") or []:
            if ref.startswith("figure:"):
                findings.append({"claim": i, "figure": ref, "verdict": "missing_figure",
                                 "evidence": f"the text names {ref.replace('figure:', 'Figure ')}, which the paper does not contain"})
    jobs = []
    for fid in sorted(by_figure, key=_figure_order):
        group: list[int] = []
        used = 0
        for i in by_figure[fid]:
            cost = count_tokens(claims_block([claims[i]]))
            if group and (len(group) == MAX_CLAIMS_PER_FIGURE or used + cost > CLAIMS_TOKENS):
                jobs.append({"figure": fid, "claims": group})
                group, used = [], 0
            group.append(i)
            used += cost
        jobs.append({"figure": fid, "claims": group})
    return jobs, findings


def _figure_order(fid: str) -> tuple[int, str]:
    n = fid.split(":", 1)[1]
    return (int(n), "") if n.isdigit() else (10**6, n)


def claims_block(claims: list[dict[str, Any]]) -> str:
    """Numbered claims, each with the sentence it comes from (the claim is a restatement)."""
    lines = []
    for k, c in enumerate(claims, 1):
        claim = (c.get("claim") or "").strip()
        line = f"{k}. {claim[:CLAIM_CHARS]}"
        sentence = (c.get("sentence") or "").strip()
        if sentence and sentence != claim:
            line += f'\n   Sentence in the paper: "{sentence[:SENTENCE_CHARS]}"'
        lines.append(line)
    return "\n".join(lines)


def figure_caption(paper: dict[str, Any], fid: str) -> str:
    fig = next((f for f in paper.get("figures", []) if f["id"] == fid), {})
    return (fig.get("caption") or "(no caption)")[:600]


def figure_request(system: str, user: str, image_png: bytes, model: str = FIGURE_MODEL,
                   variant: str | None = None) -> dict[str, Any]:
    """Body for POST https://api.groq.com/openai/v1/chat/completions with one image (D36).

    Reasoning is hidden (with JSON output Groq refuses reasoning_format "raw");
    hidden reasoning still counts as output tokens, so "none" saves most of the
    output budget. In "schema" mode Groq checks the answer against the schema
    after generating it and refuses it with a 400 when it does not match; in
    "json" mode only valid JSON is required, and validate() does the rest.
    """
    reasoning, mode = VARIANTS[variant or FIGURE_VARIANT]
    data_url = "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")
    body: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "reasoning_effort": reasoning,
        "reasoning_format": "hidden",
        "max_completion_tokens": FIGURE_MAX_OUTPUT,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [{"type": "text", "text": user}, {"type": "image_url", "image_url": {"url": data_url}}]},
        ],
    }
    if mode == "schema":
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "figure_checks", "strict": True, "schema": FIGURE_SCHEMA}}
    else:
        body["response_format"] = {"type": "json_object"}
    return body


def validate(output: Any, job_claims: list[int]) -> tuple[str, list[dict[str, Any]]]:
    """(the model's description of the figure, one check per claim of the job).

    Raises ValueError if the output is not the expected object. Claim numbers are
    1-based positions in the job; numbers the job does not have are ignored, a
    claim answered twice keeps its first answer, and a claim not answered gets
    "not_answered".
    """
    if isinstance(output, str):
        output = json.loads(output)
    if not isinstance(output, dict) or not isinstance(output.get("checks"), list):
        raise ValueError("figure check output must be an object with a 'checks' list")
    description = str(output.get("figure") or "")[:1500]
    by_pos: dict[int, dict[str, Any]] = {}
    for c in output["checks"]:
        if not isinstance(c, dict):
            continue
        pos = c.get("claim")
        if not isinstance(pos, int) or not 1 <= pos <= len(job_claims) or pos in by_pos:
            continue
        verdict = c.get("verdict") if c.get("verdict") in VERDICTS else "not_shown"
        by_pos[pos] = {"claim": job_claims[pos - 1], "verdict": verdict, "evidence": str(c.get("evidence") or "")[:600],
                       "read_values": [str(v)[:60] for v in (c.get("read_values") or [])][:12]}
    if not by_pos:
        # E4: with the strict schema the model sometimes answers `"checks": []`; that is a failed
        # call to retry, not a figure with nothing to say
        raise ValueError("the answer has no verdict for any claim")
    checks = [by_pos.get(k) or {"claim": job_claims[k - 1], "verdict": "not_answered", "evidence": "", "read_values": []}
              for k in range(1, len(job_claims) + 1)]
    return description, checks
