"""Claim extraction (audit step 3): which sentences the LLM reads, and what code checks after it.

Code does the cheap, exact parts and the LLM only the part that needs reading:

1. ``candidate_sentences``: the body text is split into sentences, and only
   sentences that can carry a checkable claim are kept: a number with a
   decimal point or a percent sign, a reference to a table or figure, or a
   citation. On the spike papers this is a small fraction of the text, so
   a paper costs a few LLM calls instead of one call per page (Groq's free
   tier allows about 8,000 tokens per minute, D12).
2. ``batches``: candidates grouped under a token budget, each batch one call
   of P4-claim-extractor (the budget is set by Groq's per-request limit, see
   ``steps.BATCH_TOKENS``).
3. ``validate``: every claim the LLM returns is checked against the paper.
   The sentence must exist; every number it extracted must appear in that
   sentence as written (an extractor that invents or "corrects" a number
   would make the number check meaningless); table and figure references
   must exist in the paper, otherwise they are kept but flagged.
"""

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..textutil import count_tokens

SKIP_SECTIONS = re.compile(r"(references|bibliography|acknowledg|ethics statement|limitations?)\b", re.IGNORECASE)
NUMBER = re.compile(r"\d+\.\d+|\d+(\.\d+)?\s?%|\d+(\.\d+)?\s?(×|x\b)|\b\d+\s?(points?|pp)\b")
TABLE_FIG = re.compile(r"\b(tables?|tab\.|figures?|figs?\.)\s*\(?\d+", re.IGNORECASE)
CITATION = re.compile(r"\bet al\.?,?\s*\(?\d{4}|\[\d+(\s*[,–-]\s*\d+)*\]|\(\s*[A-Z][A-Za-z'-]+(\s+(and|&)\s+[A-Z][A-Za-z'-]+)?,?\s+\d{4}[a-z]?\s*[;)]")

# Abbreviations whose period does not end a sentence.
_ABBREV = re.compile(r"\b(et al|e\.g|i\.e|vs|Fig|Figs|Tab|Eq|Sec|cf|approx|resp|w/o|w)\.", re.IGNORECASE)
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[\"'])")


@dataclass
class Sentence:
    id: str  # "b:12/s:3" = 4th sentence of block b:12
    block: str
    section: str
    page: int | None
    text: str
    signals: list[str]  # why it was selected: number, table_figure, citation


def split_sentences(text: str) -> list[str]:
    protected = _ABBREV.sub(lambda m: m.group(0).replace(".", "§"), text)
    parts = _SPLIT.split(protected)
    return [p.replace("§", ".").strip() for p in parts if p.strip()]


def signals(sentence: str) -> list[str]:
    out = []
    if NUMBER.search(sentence):
        out.append("number")
    if TABLE_FIG.search(sentence):
        out.append("table_figure")
    if CITATION.search(sentence):
        out.append("citation")
    return out


def candidate_sentences(paper: dict[str, Any]) -> tuple[list[Sentence], int]:
    """(candidates, total sentences in the body)."""
    out, total = [], 0
    for block in paper["blocks"]:
        if block["label"] not in ("text", "list_item", "paragraph") or SKIP_SECTIONS.search(block.get("section") or ""):
            continue
        for i, s in enumerate(split_sentences(block["text"])):
            total += 1
            sig = signals(s)
            if sig:
                out.append(Sentence(f"{block['id']}/s:{i}", block["id"], block.get("section") or "", block.get("page"), s, sig))
    return out, total


def batches(sentences: list[Sentence], max_tokens: int = 1800) -> list[list[Sentence]]:
    out: list[list[Sentence]] = [[]]
    used = 0
    for s in sentences:
        n = count_tokens(s.text) + 12  # id and section label
        if out[-1] and used + n > max_tokens:
            out.append([])
            used = 0
        out[-1].append(s)
        used += n
    return [b for b in out if b]


def sentences_block(batch: list[Sentence]) -> str:
    """The SENTENCES section of the P4 prompt: one line per sentence, with its id and section."""
    return "\n".join(f"[{s.id}] ({s.section[:60] or 'no section'}) {s.text}" for s in batch)


def objects_block(paper: dict[str, Any]) -> str:
    """Tables and figures the paper contains, so the extractor only links to real ones."""
    lines = [f"{t['id']}: {t['caption'][:80] or '(no caption)'}" for t in paper["tables"] if t["number"] is not None]
    lines += [f"{f['id']}: {f['caption'][:80] or '(no caption)'}" for f in paper["figures"] if f["number"] is not None]
    return "\n".join(lines) or "(none found)"


# ---- the LLM's output contract -------------------------------------------------------------


class ClaimValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=40)  # exactly as written, e.g. "54.9", "7.0%"
    metric: str = Field(default="", max_length=80)
    dataset: str = Field(default="", max_length=80)
    system: str = Field(default="", max_length=120)  # the method or model the value belongs to
    setting: str = Field(default="", max_length=120)  # e.g. the backbone LLM, "w/o refinement"


class Comparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system: str = Field(max_length=120)
    baseline: str = Field(max_length=120)
    difference: str = Field(default="", max_length=40)  # as written: "7.0", "7.0%", "" if none
    kind: Literal["absolute", "relative", "unspecified"] = "unspecified"
    direction: Literal["better", "worse", "equal"] = "better"


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sentence_id: str = Field(max_length=40)
    type: Literal["result", "comparison", "figure", "citation", "other"]
    claim: str = Field(min_length=5, max_length=400)  # the claim restated in one plain sentence
    values: list[ClaimValue] = Field(default_factory=list, max_length=12)
    comparison: Comparison | None = None
    refers_to: list[str] = Field(default_factory=list, max_length=6)  # "table:2", "figure:3"
    cites: list[str] = Field(default_factory=list, max_length=6)  # citation markers as written


class ClaimsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[Claim] = Field(default_factory=list, max_length=60)


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_REF = re.compile(r"\b(table|tab\.|figure|fig\.)\s*(\d+)", re.I)


def _norm_number(s: str) -> str:
    return s.replace(",", "")


def _numbers(text: str) -> list[str]:
    """Every number in the text, without thousands separators: "0.77B vs 7B" -> ["0.77", "7"]."""
    return [_norm_number(m) for m in _NUMBER.findall(text)]


def _sentence_refs(sentence: str, objects: set[str]) -> list[str]:
    """"Table 1", "table 1", "Fig. 3" written in the sentence, as object ids that exist."""
    found = []
    for kind, n in _REF.findall(sentence):
        oid = f"{'table' if kind.lower().startswith('tab') else 'figure'}:{n}"
        if oid in objects and oid not in found:
            found.append(oid)
    return found


def _canon_id(sid: str) -> str:
    return re.sub(r"[^0-9a-z]+", ":", sid.lower()).strip(":")


def validate(output: Any, batch_ids: set[str], sentences: dict[str, str], paper: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """(kept claims, dropped claims with reasons). Raises ValueError if the output is not a ClaimsOutput.

    Deterministic repairs after the LLM (F-018), each one cheaper and more
    reliable in code than in the prompt:
      - a sentence id with a typo ("b:60/s/9") is mapped back when only one id matches;
      - every number of a value must be in the sentence; units are ignored ("0.77B");
      - a value equal to the comparison's difference is marked role="difference",
        so the number check does not read a margin as the system's score;
      - table and figure references written in the sentence are added when the
        model missed them (it skips a lowercase "table 1").
    """
    try:
        parsed = ClaimsOutput.model_validate(output)
    except ValidationError as exc:
        raise ValueError(f"claims output does not match the schema: {exc.errors()[:3]}") from exc
    objects = {t["id"] for t in paper["tables"]} | {f["id"] for f in paper["figures"]}
    canon: dict[str, list[str]] = {}
    for sid in batch_ids:
        canon.setdefault(_canon_id(sid), []).append(sid)
    kept, dropped = [], []
    for c in parsed.claims:
        d = c.model_dump()
        sid = c.sentence_id
        if sid not in batch_ids:
            match = canon.get(_canon_id(sid), [])
            if len(match) != 1:
                dropped.append({**d, "reason": "unknown sentence id"})
                continue
            d["sentence_id"] = sid = match[0]
            d["repaired_id"] = c.sentence_id
        sentence = sentences[sid]
        digits = set(_numbers(sentence))
        invented = [v.value for v in c.values if not _numbers(v.value) or not set(_numbers(v.value)) <= digits]
        diff = c.comparison.difference if c.comparison else ""
        if diff and not set(_numbers(diff)) <= digits:
            invented.append(diff)
        if invented:
            dropped.append({**d, "reason": f"values not in the sentence: {invented}"})
            continue
        for v in d["values"]:
            v["role"] = "difference" if diff and _numbers(v["value"]) == _numbers(diff) else "value"
        refs = [r for r in c.refers_to if r in objects]
        d["unknown_refs"] = [r for r in c.refers_to if r not in objects]
        d["refers_to"] = refs + [r for r in _sentence_refs(sentence, objects) if r not in refs]
        d["sentence"] = sentence
        kept.append(d)
    return kept, dropped


# ---- the Groq request n8n sends --------------------------------------------------------------
# pp-tools builds the whole request body and n8n posts it unchanged. The schema
# used to be written inline in the n8n node, but n8n ends an expression at the
# first "}}", and a JSON schema is full of them: every call failed with
# "invalid syntax" before reaching Groq (F-016). Keeping it here also puts the
# schema next to the Pydantic models it mirrors.

# gpt-oss-120b first (D40): on the same candidate sentences gpt-oss-20b extracted
# far fewer citation claims (CRAG 1 against 7 to 11, Adaptive-RAG 3 against 12),
# which left the citation check with almost nothing to check (F-023).
CLAIMS_MODEL = "openai/gpt-oss-120b"
# Groq's limits are per model, so the smaller sibling has its own 200K tokens a
# day; it accepts the same parameters and strict schema (F-017).
CLAIMS_FALLBACK_MODELS = ("openai/gpt-oss-20b",)
CLAIMS_MAX_OUTPUT = 2500  # Groq counts input + this budget against 8,000 tokens per minute (F-011)
CLAIMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sentence_id": {
                        "type": "string"
                    },
                    "type": {
                        "type": "string",
                        "enum": [
                            "result",
                            "comparison",
                            "figure",
                            "citation",
                            "other"
                        ]
                    },
                    "claim": {
                        "type": "string"
                    },
                    "values": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "value": {
                                    "type": "string"
                                },
                                "metric": {
                                    "type": "string"
                                },
                                "dataset": {
                                    "type": "string"
                                },
                                "system": {
                                    "type": "string"
                                },
                                "setting": {
                                    "type": "string"
                                }
                            },
                            "required": [
                                "value",
                                "metric",
                                "dataset",
                                "system",
                                "setting"
                            ],
                            "additionalProperties": False
                        }
                    },
                    "comparison": {
                        "type": [
                            "object",
                            "null"
                        ],
                        "properties": {
                            "system": {
                                "type": "string"
                            },
                            "baseline": {
                                "type": "string"
                            },
                            "difference": {
                                "type": "string"
                            },
                            "kind": {
                                "type": "string",
                                "enum": [
                                    "absolute",
                                    "relative",
                                    "unspecified"
                                ]
                            },
                            "direction": {
                                "type": "string",
                                "enum": [
                                    "better",
                                    "worse",
                                    "equal"
                                ]
                            }
                        },
                        "required": [
                            "system",
                            "baseline",
                            "difference",
                            "kind",
                            "direction"
                        ],
                        "additionalProperties": False
                    },
                    "refers_to": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        }
                    },
                    "cites": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        }
                    }
                },
                "required": [
                    "sentence_id",
                    "type",
                    "claim",
                    "values",
                    "comparison",
                    "refers_to",
                    "cites"
                ],
                "additionalProperties": False
            }
        }
    },
    "required": [
        "claims"
    ],
    "additionalProperties": False
}


def claims_request(system: str, user: str, model: str = CLAIMS_MODEL) -> dict[str, Any]:
    """Body for POST https://api.groq.com/openai/v1/chat/completions (D34).

    Low reasoning effort and hidden reasoning, or gpt-oss spends the whole
    budget thinking and answers with nothing (F-013); a strict schema so the
    answer is always parseable JSON.
    """
    return {
        "model": model,
        "temperature": 0,
        "reasoning_effort": "low",
        "include_reasoning": False,
        "max_completion_tokens": CLAIMS_MAX_OUTPUT,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "claims", "strict": True, "schema": CLAIMS_SCHEMA}},
    }


def as_dicts(sentences: list[Sentence]) -> list[dict[str, Any]]:
    return [asdict(s) for s in sentences]
