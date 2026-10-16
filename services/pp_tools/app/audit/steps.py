"""Audit pipeline steps that n8n calls between LLM nodes (the novelty branch's pattern, D21)."""

from typing import Any
from uuid import UUID

from ..db import Database
from ..prompts import PromptLibrary
from .claims import as_dicts, batches, candidate_sentences, objects_block, sentences_block, validate
from .store import PaperStore

EXTRACTOR = "P4-claim-extractor"
# Groq's free tier refuses a single request above 8,000 tokens per minute, and it
# counts the input plus the output budget (max tokens). With 1,800 tokens of
# sentences every request came to 8,069-8,213 (team PC, 2026-10-16). Our token
# count also undercounts real tokens by 20-40% (app/textutil.py). 700 counted
# tokens of sentences + prompt (~2,600) + 3,000 for the answer stays near 6,500.
BATCH_TOKENS = 700


class AuditError(Exception):
    """A step was called out of order or its inputs are missing."""


def parsed_digest(db: Database, run_id: UUID) -> str:
    if db.get_run(run_id) is None:
        raise LookupError(f"run {run_id} not found")
    steps = [s for s in db.get_steps(run_id, "parse") if s["status"] == "done"]
    if not steps:
        raise AuditError("the run's PDF has not been parsed yet")
    return steps[-1]["detail"]["sha256"]


def claim_prompts(db: Database, library: PromptLibrary, papers: PaperStore, run_id: UUID) -> dict[str, Any]:
    """Render P4 once per batch of candidate sentences and record which sentences each batch holds."""
    paper = papers.paper(parsed_digest(db, run_id))
    candidates, total = candidate_sentences(paper)
    groups = batches(candidates, BATCH_TOKENS)
    prompt = library.load(EXTRACTOR)
    objects = objects_block(paper)
    out = []
    for i, group in enumerate(groups):
        rendered = prompt.render({"title": paper["title"], "objects_block": objects, "sentences_block": sentences_block(group)})
        out.append({"batch": i, "system": rendered.system, "user": rendered.user, "version": rendered.version, "sentences": len(group)})
    db.add_step(run_id, "claim_prompts", "done", {
        "prompt": EXTRACTOR, "prompt_version": prompt.version, "total_sentences": total,
        "candidates": as_dicts(candidates), "batches": [[s.id for s in g] for g in groups],
    })
    return {"total_sentences": total, "candidates": len(candidates), "batches": out}


MAX_BATCH_ATTEMPTS = 3


def next_claim_batch(db: Database, library: PromptLibrary, papers: PaperStore, run_id: UUID) -> dict[str, Any]:
    """The next batch n8n should send to the LLM, or {"done": true}.

    pp-tools keeps the loop's state, not n8n: a batch counts as finished once a
    valid output was saved, or once it was served MAX_BATCH_ATTEMPTS times (then
    it is recorded as skipped). A rate-limited or invalid call therefore costs
    one attempt, and n8n simply asks again after its pause. n8n's own retries
    wait at most 5 s, too short for Groq's per-minute window (F-012).
    """
    if not db.get_steps(run_id, "claim_prompts"):
        claim_prompts(db, library, papers, run_id)
    detail = _prompts_step(db, run_id)
    paper = papers.paper(parsed_digest(db, run_id))
    finished = {s["detail"]["batch"] for s in db.get_steps(run_id, "claims_batch") if s["status"] in ("done", "skipped")}
    attempts: dict[int, int] = {}
    for s in db.get_steps(run_id, "claims_attempt"):
        attempts[s["detail"]["batch"]] = attempts.get(s["detail"]["batch"], 0) + 1
    for i, ids in enumerate(detail["batches"]):
        if i in finished:
            continue
        if attempts.get(i, 0) >= MAX_BATCH_ATTEMPTS:
            db.add_step(run_id, "claims_batch", "skipped", {"batch": i, "reason": f"no valid output after {MAX_BATCH_ATTEMPTS} attempts"})
            continue
        db.add_step(run_id, "claims_attempt", "done", {"batch": i, "attempt": attempts.get(i, 0) + 1})
        group = [c for c in detail["candidates"] if c["id"] in set(ids)]
        rendered = library.load(EXTRACTOR, detail["prompt_version"]).render({
            "title": paper["title"], "objects_block": objects_block(paper),
            "sentences_block": "\n".join(f"[{c['id']}] ({(c['section'] or 'no section')[:60]}) {c['text']}" for c in group),
        })
        return {"done": False, "batch": i, "batches": len(detail["batches"]), "attempt": attempts.get(i, 0) + 1,
                "system": rendered.system, "user": rendered.user, "version": rendered.version}
    return {"done": True, "batches": len(detail["batches"]), "finished": len(finished)}


def _prompts_step(db: Database, run_id: UUID) -> dict[str, Any]:
    steps = db.get_steps(run_id, "claim_prompts")
    if not steps:
        raise AuditError("claim prompts were not rendered for this run")
    return steps[-1]["detail"]


def save_claims(db: Database, papers: PaperStore, run_id: UUID, batch: int, output: Any) -> dict[str, Any]:
    paper = papers.paper(parsed_digest(db, run_id))
    detail = _prompts_step(db, run_id)
    if not 0 <= batch < len(detail["batches"]):
        raise AuditError(f"batch {batch} does not exist (0..{len(detail['batches']) - 1})")
    ids = set(detail["batches"][batch])
    sentences = {c["id"]: c["text"] for c in detail["candidates"] if c["id"] in ids}
    try:
        kept, dropped = validate(output, ids, sentences, paper)
    except ValueError as exc:
        db.add_step(run_id, "claims_batch", "failed", {"batch": batch, "error": str(exc)[:1000], "output": output})
        raise AuditError(str(exc)) from exc
    db.add_step(run_id, "claims_batch", "done", {"batch": batch, "kept": kept, "dropped": dropped})
    return {"batch": batch, "kept": len(kept), "dropped": len(dropped)}


def claims(db: Database, run_id: UUID) -> dict[str, Any]:
    """All claims of the run: the latest successful output of every batch."""
    detail = _prompts_step(db, run_id)
    latest: dict[int, dict[str, Any]] = {}
    for s in db.get_steps(run_id, "claims_batch"):
        if s["status"] == "done":
            latest[s["detail"]["batch"]] = s["detail"]
    kept = [c for b in sorted(latest) for c in latest[b]["kept"]]
    dropped = [c for b in sorted(latest) for c in latest[b]["dropped"]]
    by_type: dict[str, int] = {}
    for c in kept:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    skipped = sorted({s["detail"]["batch"] for s in db.get_steps(run_id, "claims_batch") if s["status"] == "skipped"} - set(latest))
    return {
        "prompt_version": detail["prompt_version"],
        "total_sentences": detail["total_sentences"],
        "candidate_sentences": len(detail["candidates"]),
        "batches": len(detail["batches"]),
        "batches_done": len(latest),
        "batches_skipped": skipped,
        "claims": kept,
        "dropped": dropped,
        "by_type": by_type,
    }


def audit_result(db: Database, papers: PaperStore, run_id: UUID) -> dict[str, Any]:
    """The audit result so far (milestone 3 builds it up step by step): the parsed paper and its claims."""
    parse = [s for s in db.get_steps(run_id, "parse") if s["status"] == "done"]
    if not parse:
        raise AuditError("the run's PDF has not been parsed yet")
    p = parse[-1]["detail"]
    extracted = claims(db, run_id)
    return {
        "branch": "audit",
        "implemented": "parse+claims",  # the checks come next (steps 4 to 6)
        "run_id": str(run_id),
        "paper": {k: p[k] for k in ("title", "pages", "sha256", "numbered", "items", "references") if k in p}
                 | {"tables": [t["id"] for t in p["tables"]], "figures": [f["id"] for f in p["figures"]]},
        "claims": extracted["claims"],
        "claims_dropped": len(extracted["dropped"]),
        "coverage": {k: extracted[k] for k in ("total_sentences", "candidate_sentences", "batches", "batches_done", "batches_skipped", "by_type")},
        "prompt_versions": {EXTRACTOR: extracted["prompt_version"]},
    }
