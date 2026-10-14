"""Novelty branch logic that must be exact: output contracts, retrieval, the verifier.

The LLM calls themselves run in n8n (nodes "Decompose idea" and "Judge novelty").
pp-tools does everything around them that must be deterministic and testable:

1. save_facets      validate the decomposer's JSON and store it
2. retrieve         search the knowledge base with every query, fuse the rankings
3. judge_prompt     render P3 with the facets, the candidates and any feedback
4. verify           check the judge's output against what retrieval really returned
5. assemble         build the branch result from the stored steps

Every step is stored in run_steps, so a run can be replayed and audited, and the
verifier reads the retrieved set from the database rather than trusting what
n8n passes back.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .db import Database
from .prompts import PromptLibrary, RenderedPrompt
from .retrieval.search import Retriever

FacetName = Literal["task", "method", "data", "result"]
Label = Literal["same", "partial", "different", "unknown"]
FACETS: tuple[str, ...] = ("task", "method", "data", "result")

DECOMPOSER = "P1-decomposer"
JUDGE = "P3-novelty-judge"
MAX_VERIFY_ATTEMPTS = 2  # first answer + one retry with the verifier's feedback


class NoveltyError(Exception):
    """A step was called out of order or its inputs are missing."""


# ---- contracts for LLM output -----------------------------------------------------


class Facets(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task: str = Field(min_length=2, max_length=400)
    method: str = Field(min_length=2, max_length=600)
    data: str = Field(min_length=2, max_length=400)
    result: str = Field(min_length=2, max_length=400)


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=3, max_length=300)
    facets: list[FacetName] = Field(min_length=1, max_length=4)


class DecomposerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facets: Facets
    queries: list[SearchQuery] = Field(min_length=1, max_length=8)

    @field_validator("queries")
    @classmethod
    def distinct_queries(cls, queries: list[SearchQuery]) -> list[SearchQuery]:
        seen: set[str] = set()
        out = []
        for q in queries:
            key = q.text.lower()
            if key not in seen:
                seen.add(key)
                out.append(q)
        return out


class Overlap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Label
    method: Label
    data: Label
    result: Label


class JudgeMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    paper_id: str
    overlap: Overlap
    evidence: list[str] = Field(default_factory=list, max_length=10)
    rationale: str = Field(min_length=3, max_length=800)


class JudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    matches: list[JudgeMatch] = Field(max_length=12)
    summary: str = Field(min_length=3, max_length=1500)


# ---- steps ---------------------------------------------------------------------------------


def _field_errors(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc']) or 'output'}: {e['msg']}" for e in exc.errors()]


def save_facets(db: Database, run_id: UUID, output: dict[str, Any], prompt_version: str) -> DecomposerOutput:
    _require_run(db, run_id)
    parsed = DecomposerOutput.model_validate(output)  # ValidationError -> 422 in the API
    db.add_step(run_id, "decompose", "done", {"prompt": DECOMPOSER, "prompt_version": prompt_version, "output": parsed.model_dump()})
    return parsed


@dataclass
class RetrievalConfig:
    strategy: str = "structured"
    per_query_k: int = 20  # chunks retrieved per query
    top_papers: int = 6  # candidates sent to the judge
    evidence_per_paper: int = 2
    evidence_chars: int = 900  # per passage, keeps the judge prompt within Groq's per-minute token budget
    rrf_k: int = 60  # standard reciprocal rank fusion constant


def retrieve(db: Database, retriever: Retriever, run_id: UUID, cfg: RetrievalConfig, coverage: dict[str, Any]) -> dict[str, Any]:
    """Search with every decomposer query and fuse the rankings with reciprocal rank fusion.

    A paper found by several queries, or ranked high by one, rises to the top.
    For each candidate, the best-scoring distinct chunks become its evidence.
    """
    decomposed = DecomposerOutput.model_validate(_last_step(db, run_id, "decompose")["output"])

    paper_score: dict[str, float] = defaultdict(float)
    paper_queries: dict[str, set[int]] = defaultdict(set)
    chunk_score: dict[str, float] = defaultdict(float)
    chunks: dict[str, Any] = {}
    for qi, query in enumerate(decomposed.queries):
        hits = retriever.chunks(query.text, cfg.strategy, cfg.per_query_k)
        seen_papers: set[str] = set()
        for rank, hit in enumerate(hits, start=1):
            chunk_score[hit.chunk_id] += 1.0 / (cfg.rrf_k + rank)
            chunks[hit.chunk_id] = hit
            if hit.paper_id not in seen_papers:  # a paper's rank in this query = its best chunk's rank
                seen_papers.add(hit.paper_id)
                paper_score[hit.paper_id] += 1.0 / (cfg.rrf_k + len(seen_papers))
                paper_queries[hit.paper_id].add(qi)

    ranked = sorted(paper_score, key=lambda p: paper_score[p], reverse=True)[: cfg.top_papers]
    candidates = []
    for pid in ranked:
        own = sorted((c for c in chunks.values() if c.paper_id == pid), key=lambda c: chunk_score[c.chunk_id], reverse=True)
        evidence = [
            {
                "chunk_id": c.chunk_id,
                "chunk_type": c.chunk_type,
                "section": c.section,
                "text": c.text[: cfg.evidence_chars],
            }
            for c in own[: cfg.evidence_per_paper]
        ]
        first = own[0]
        candidates.append(
            {
                "paper_id": pid,
                "title": first.title,
                "year": first.year,
                "fused_score": round(paper_score[pid], 6),
                "matched_queries": sorted(paper_queries[pid]),
                "evidence": evidence,
            }
        )

    detail = {
        "strategy": cfg.strategy,
        "queries": [q.text for q in decomposed.queries],
        "candidates": candidates,
        "coverage": coverage,
    }
    db.add_step(run_id, "retrieve", "done", detail)
    return detail


def _facets_block(facets: Facets) -> str:
    return "\n".join(f"- {name}: {getattr(facets, name)}" for name in FACETS)


def _candidates_block(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "(no candidate papers were retrieved)"
    lines = []
    for i, c in enumerate(candidates, start=1):
        lines.append(f"[C{i}] paper_id: {c['paper_id']} | title: {c['title']} | year: {c['year']}")
        for e in c["evidence"]:
            text = " ".join(e["text"].split())
            lines.append(f"  - ({e['chunk_id']}) {text}")
        lines.append("")
    return "\n".join(lines).rstrip()


def judge_prompt(db: Database, library: PromptLibrary, run_id: UUID) -> tuple[RenderedPrompt, int]:
    """Render P3 for the next judge attempt; on a retry, include the verifier's feedback."""
    decomposed = DecomposerOutput.model_validate(_last_step(db, run_id, "decompose")["output"])
    retrieved = _last_step(db, run_id, "retrieve")
    verifications = db.get_steps(run_id, "verify")
    attempt = len(verifications) + 1
    if attempt > MAX_VERIFY_ATTEMPTS:
        raise NoveltyError(f"run {run_id} already used {MAX_VERIFY_ATTEMPTS} judge attempts")

    feedback = ""
    if verifications:
        reasons = [r for item in verifications[-1]["detail"]["rejected"] for r in item["reasons"]]
        feedback = (
            "\nFEEDBACK ON YOUR PREVIOUS ANSWER\n"
            "Your previous answer was checked automatically and these problems were found:\n"
            + "\n".join(f"- {r}" for r in reasons)
            + "\nAnswer again, fixing every problem. Use only the paper ids and chunk ids shown above."
        )

    rendered = library.load(JUDGE).render(
        {
            "facets_block": _facets_block(decomposed.facets),
            "candidates_block": _candidates_block(retrieved["candidates"]),
            "feedback_block": feedback,
        }
    )
    db.add_step(run_id, "judge_prompt", "done", {"prompt": JUDGE, "prompt_version": rendered.version, "attempt": attempt})
    return rendered, attempt


def verify(db: Database, run_id: UUID, output: Any) -> dict[str, Any]:
    """The verifier: plain code, no LLM. Accepts only what retrieval can back up.

    Rejects any paper the retrieval did not return, any chunk id that was not
    shown for that paper, and any non-"unknown" verdict without evidence.
    Returns status "pass", "retry" (one more judge attempt allowed) or "final"
    (rejections remain; they are reported as unverified, never silently dropped).
    """
    retrieved = _last_step(db, run_id, "retrieve")
    attempt = len(db.get_steps(run_id, "verify")) + 1
    shown = {c["paper_id"]: {e["chunk_id"] for e in c["evidence"]} for c in retrieved["candidates"]}

    verified: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    summary = None
    try:
        parsed = JudgeOutput.model_validate(output)
    except ValidationError as exc:
        rejected.append({"item": None, "reasons": [f"invalid output format: {m}" for m in _field_errors(exc)][:10]})
        parsed = None

    if parsed is not None:
        summary = parsed.summary
        seen: set[str] = set()
        for match in parsed.matches:
            reasons = []
            if match.paper_id in seen:
                reasons.append(f"paper {match.paper_id} is listed more than once")
            elif match.paper_id not in shown:
                reasons.append(f"paper {match.paper_id} was not among the retrieved candidates")
            else:
                for cid in match.evidence:
                    if cid not in shown[match.paper_id]:
                        reasons.append(f"chunk {cid} was not shown for paper {match.paper_id}")
                labels = match.overlap.model_dump().values()
                if not match.evidence and any(label != "unknown" for label in labels):
                    reasons.append(f"paper {match.paper_id}: labels other than 'unknown' need at least one evidence chunk")
            seen.add(match.paper_id)
            item = match.model_dump()
            if reasons:
                rejected.append({"item": item, "reasons": reasons})
            else:
                verified.append(item)

    if not rejected:
        status = "pass"
    elif attempt < MAX_VERIFY_ATTEMPTS:
        status = "retry"
    else:
        status = "final"
    detail = {"attempt": attempt, "status": status, "verified": verified, "rejected": rejected, "summary": summary}
    db.add_step(run_id, "verify", "done" if status != "retry" else "failed", detail)
    return detail


def assemble(db: Database, run_id: UUID) -> dict[str, Any]:
    """The novelty branch's result, built only from stored, verified steps."""
    decompose = _last_step(db, run_id, "decompose")
    retrieved = _last_step(db, run_id, "retrieve")
    verifications = db.get_steps(run_id, "verify")
    if not verifications:
        raise NoveltyError("no verification step yet")
    last = verifications[-1]["detail"]
    if last["status"] == "retry":
        raise NoveltyError("the last verification asked for a retry; run the judge again first")

    titles = {c["paper_id"]: c for c in retrieved["candidates"]}

    def enrich(item: dict[str, Any]) -> dict[str, Any]:
        c = titles.get(item["paper_id"], {})
        arxiv_id = item["paper_id"].removeprefix("arxiv:")
        return {
            **item,
            "title": c.get("title"),
            "year": c.get("year"),
            "url": f"https://arxiv.org/abs/{arxiv_id}" if item["paper_id"].startswith("arxiv:") else None,
        }

    judge_prompts = db.get_steps(run_id, "judge_prompt")
    return {
        "branch": "novelty",
        "implemented": True,
        "idea_facets": decompose["output"]["facets"],
        "closest_work": [enrich(i) for i in last["verified"]],
        "unverified": [
            {"item": enrich(r["item"]) if r["item"] else None, "reasons": r["reasons"]} for r in last["rejected"]
        ],
        # Free text written by the LLM: shown, but labelled, because the verifier checks ids, not prose.
        "summary_llm_written": last.get("summary"),
        "coverage": {
            **retrieved["coverage"],
            "strategy": retrieved["strategy"],
            "queries": retrieved["queries"],
            "candidates_considered": len(retrieved["candidates"]),
            "note": "Not found in this knowledge base does not mean novel.",
        },
        "judge_attempts": len(verifications),
        "prompt_versions": {
            DECOMPOSER: decompose["prompt_version"],
            JUDGE: judge_prompts[-1]["detail"]["prompt_version"] if judge_prompts else None,
        },
    }


# ---- helpers -------------------------------------------------------------------------------


def _require_run(db: Database, run_id: UUID) -> None:
    if db.get_run(run_id) is None:
        raise LookupError(f"run {run_id} not found")


def _last_step(db: Database, run_id: UUID, name: str) -> dict[str, Any]:
    _require_run(db, run_id)
    steps = db.get_steps(run_id, name)
    if not steps:
        raise NoveltyError(f"run {run_id} has no '{name}' step yet")
    return steps[-1]["detail"]
