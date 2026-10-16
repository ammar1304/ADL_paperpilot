"""Audit pipeline steps that n8n calls between LLM nodes (the novelty branch's pattern, D21)."""

import json
import time

from typing import Any
from uuid import UUID

from ..db import Database
from ..prompts import PromptLibrary
from . import figures as fig
from .claims import CLAIMS_FALLBACK_MODELS, CLAIMS_MODEL, as_dicts, batches, claims_request, candidate_sentences, objects_block, sentences_block, validate
from .numbers import check_claims
from .store import PaperStore

EXTRACTOR = "P4-claim-extractor"
FIGURE_CHECKER = "P5-figure-checker"
# Groq's free tier refuses a single request above 8,000 tokens per minute, and it
# counts the input plus the output budget (max tokens). With 1,800 tokens of
# sentences every request came to 8,069-8,213 (team PC, 2026-10-16). Our token
# count also undercounts real tokens by 20-40% (app/textutil.py). 700 counted
# tokens of sentences + prompt (~2,600) + 3,000 for the answer stays near 6,500.
BATCH_TOKENS = 700
PACE_S = 30  # between calls: one call is ~2,600-4,200 tokens against 8,000 per minute
# A 429 whose retry-after is longer than this is the daily limit, not the
# per-minute one: switch model, or stop the run with what is done (F-017).
MAX_WAIT_S = 90


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


def _model_blocks(db: Database, run_id: UUID, loop: str = "claims") -> dict[str, float]:
    """{model: unix time until which Groq refuses it}, from the rate limits seen in this run."""
    until: dict[str, float] = {}
    for st in db.get_steps(run_id, f"{loop}_llm_error"):
        d = st["detail"]
        if d.get("rate_limited") and d.get("model"):
            until[d["model"]] = max(until.get(d["model"], 0.0), d["until"])
    return until


def _loop_state(db: Database, run_id: UUID, loop: str, total: int, models: tuple[str, ...], now: float) -> dict[str, Any]:
    """The next unfinished item of an LLM loop ("claims" or "figures") and the model to use.

    Shared by both loops: an item is finished once a valid output was saved
    ("<loop>_batch" done) or after MAX_BATCH_ATTEMPTS attempts (skipped). A 429
    gives its attempt back (F-017); a daily limit moves to the next model, and when
    every model is spent the open items are skipped and the loop stops. Records
    the attempt it serves.
    """
    finished = {s["detail"]["batch"] for s in db.get_steps(run_id, f"{loop}_batch") if s["status"] in ("done", "skipped")}
    attempts: dict[int, int] = {}
    for s in db.get_steps(run_id, f"{loop}_attempt"):
        attempts[s["detail"]["batch"]] = attempts.get(s["detail"]["batch"], 0) + 1
    for s in db.get_steps(run_id, f"{loop}_llm_error"):
        if s["detail"].get("rate_limited"):
            attempts[s["detail"]["batch"]] = attempts.get(s["detail"]["batch"], 0) - 1
    open_items = []
    for i in range(total):
        if i in finished:
            continue
        if attempts.get(i, 0) >= MAX_BATCH_ATTEMPTS:
            db.add_step(run_id, f"{loop}_batch", "skipped", {"batch": i, "reason": f"no valid output after {MAX_BATCH_ATTEMPTS} attempts"})
            finished.add(i)
            continue
        open_items.append(i)
    if not open_items:
        return {"done": True, "finished": len(finished)}
    blocked = _model_blocks(db, run_id, loop)
    waits = {m: max(0.0, blocked.get(m, 0.0) - now) for m in models}
    model = min(models, key=lambda m: (waits[m] > MAX_WAIT_S, models.index(m)))
    if waits[model] > MAX_WAIT_S:
        reason = "Groq daily token limit reached on " + ", ".join(models)
        for i in open_items:
            db.add_step(run_id, f"{loop}_batch", "skipped", {"batch": i, "reason": reason})
        db.add_step(run_id, f"{loop}_stopped", "done", {"reason": reason, "batches_left": open_items,
                                                         "retry_in_s": round(min(waits.values()))})
        return {"done": True, "finished": len(finished) + len(open_items), "stopped": reason}
    i = open_items[0]
    db.add_step(run_id, f"{loop}_attempt", "done", {"batch": i, "attempt": attempts.get(i, 0) + 1, "model": model})
    return {"done": False, "batch": i, "model": model, "attempt": attempts.get(i, 0) + 1, "wait": waits[model]}


def next_claim_batch(db: Database, library: PromptLibrary, papers: PaperStore, run_id: UUID,
                     now: float | None = None) -> dict[str, Any]:
    """The next batch n8n should send to the LLM, how long to wait first, or {"done": true}.

    pp-tools keeps the loop's state, not n8n: a batch counts as finished once a
    valid output was saved, or once it was served MAX_BATCH_ATTEMPTS times (then
    it is recorded as skipped). n8n's own retries wait at most 5 s, too short for
    Groq's per-minute window (F-012).

    A rate limit (429) is not the batch's fault, so it does not use an attempt
    (F-017). A short retry-after is waited out (``wait_s``); a long one means the
    model's daily tokens are spent, so the next model is used, and when every
    model is spent the run stops: the remaining batches are recorded as skipped
    with that reason, and the result reports what was done.
    """
    now = time.time() if now is None else now
    if not db.get_steps(run_id, "claim_prompts"):
        claim_prompts(db, library, papers, run_id)
    detail = _prompts_step(db, run_id)
    paper = papers.paper(parsed_digest(db, run_id))
    state = _loop_state(db, run_id, "claims", len(detail["batches"]), (CLAIMS_MODEL, *CLAIMS_FALLBACK_MODELS), now)
    if state["done"]:
        return {"done": True, "batches": len(detail["batches"]), "finished": state["finished"],
                **({"stopped": state["stopped"]} if state.get("stopped") else {})}
    i, model, attempt, wait = state["batch"], state["model"], state["attempt"], state["wait"]
    ids = set(detail["batches"][i])
    group = [c for c in detail["candidates"] if c["id"] in ids]
    rendered = library.load(EXTRACTOR, detail["prompt_version"]).render({
        "title": paper["title"], "objects_block": objects_block(paper),
        "sentences_block": "\n".join(f"[{c['id']}] ({(c['section'] or 'no section')[:60]}) {c['text']}" for c in group),
    })
    return {"done": False, "batch": i, "batches": len(detail["batches"]), "attempt": attempt,
            "model": model, "wait_s": max(PACE_S, round(wait) + 1),
            "system": rendered.system, "user": rendered.user, "version": rendered.version,
            "request": claims_request(rendered.system, rendered.user, model)}


def _prompts_step(db: Database, run_id: UUID) -> dict[str, Any]:
    steps = db.get_steps(run_id, "claim_prompts")
    if not steps:
        raise AuditError("claim prompts were not rendered for this run")
    return steps[-1]["detail"]


def save_claims(
    db: Database, papers: PaperStore, run_id: UUID, batch: int, output: Any, output_text: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and store one batch of P4 output: a parsed object, or the model's raw JSON text."""
    paper = papers.paper(parsed_digest(db, run_id))
    if output is None and output_text is not None:
        try:
            output = json.loads(output_text)
        except ValueError as exc:
            db.add_step(run_id, "claims_batch", "failed", {"batch": batch, "error": f"not JSON: {exc}", "output_text": output_text[:2000]})
            raise AuditError(f"the model's output is not JSON: {exc}") from exc
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
    db.add_step(run_id, "claims_batch", "done", {"batch": batch, "kept": kept, "dropped": dropped, "usage": usage})
    return {"batch": batch, "kept": len(kept), "dropped": len(dropped)}


def record_llm_error(db: Database, run_id: UUID, batch: int, error: str, status: int | None = None,
                     retry_after: float | None = None, model: str | None = None,
                     now: float | None = None, loop: str = "claims") -> dict[str, Any]:
    """Why an LLM call failed (rate limit, empty output...), so a skipped batch is never silent.

    A 429 is marked ``rate_limited`` with the time until which the model is refused.
    """
    if db.get_run(run_id) is None:
        raise LookupError(f"run {run_id} not found")
    now = time.time() if now is None else now
    detail: dict[str, Any] = {"batch": batch, "error": error[:2000], "status": status, "model": model}
    if status == 429:
        wait = float(retry_after) if retry_after is not None else 60.0
        detail.update({"rate_limited": True, "retry_after_s": wait, "until": now + wait,
                       "kind": "daily" if wait > MAX_WAIT_S else "per-minute"})
    db.add_step(run_id, f"{loop}_llm_error", "failed", detail)
    return {"batch": batch, "recorded": True, "rate_limited": status == 429}


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
    llm_errors = [s["detail"] for s in db.get_steps(run_id, "claims_llm_error")]
    skipped = sorted({s["detail"]["batch"] for s in db.get_steps(run_id, "claims_batch") if s["status"] == "skipped"} - set(latest))
    return {
        "prompt_version": detail["prompt_version"],
        "total_sentences": detail["total_sentences"],
        "candidate_sentences": len(detail["candidates"]),
        "batches": len(detail["batches"]),
        "batches_done": len(latest),
        "batches_skipped": skipped,
        "llm_errors": len(llm_errors),
        "rate_limited": sum(1 for e in llm_errors if e.get("rate_limited")),
        "models": sorted({s["detail"]["model"] for s in db.get_steps(run_id, "claims_attempt") if s["detail"].get("model")}),
        "stopped": next((s["detail"]["reason"] for s in db.get_steps(run_id, "claims_stopped")), None),
        "claims": kept,
        "dropped": dropped,
        "by_type": by_type,
    }


def number_check(db: Database, papers: PaperStore, run_id: UUID) -> dict[str, Any]:
    """Step 4: every number of every claim against the paper's own tables (D35, no LLM)."""
    paper = papers.paper(parsed_digest(db, run_id))
    result = check_claims(paper, claims(db, run_id)["claims"])
    db.add_step(run_id, "number_check", "done", result)
    return result["summary"]


# ---- step 5: figures, checked by a vision model (D36) ------------------------------------------


def figure_plan(db: Database, library: PromptLibrary, papers: PaperStore, run_id: UUID) -> dict[str, Any]:
    """Which claims go to which figure (one vision call per figure), and what needs no model."""
    digest = parsed_digest(db, run_id)
    paper = papers.paper(digest)
    extracted = claims(db, run_id)["claims"]
    jobs, findings = fig.figure_jobs(paper, extracted, lambda fid: papers.figure(digest, fid) is not None)
    prompt = library.load(FIGURE_CHECKER)
    detail = {"prompt": FIGURE_CHECKER, "prompt_version": prompt.version, "jobs": jobs, "findings": findings}
    db.add_step(run_id, "figure_plan", "done", detail)
    return {"jobs": len(jobs), "figures": sorted({j["figure"] for j in jobs}), "findings": len(findings)}


def _figure_plan(db: Database, run_id: UUID) -> dict[str, Any]:
    steps = db.get_steps(run_id, "figure_plan")
    if not steps:
        raise AuditError("figure checks were not planned for this run")
    return steps[-1]["detail"]


def next_figure_check(db: Database, library: PromptLibrary, papers: PaperStore, run_id: UUID,
                      now: float | None = None) -> dict[str, Any]:
    """The next figure n8n should send to the vision model, with its claims, or {"done": true}.

    Same loop as the claims (pp-tools keeps the state, a 429 does not use an
    attempt), but there is only one vision model on Groq: when its daily tokens
    are spent, the remaining figures are skipped and the result says so.
    """
    now = time.time() if now is None else now
    if not db.get_steps(run_id, "figure_plan"):
        figure_plan(db, library, papers, run_id)
    plan = _figure_plan(db, run_id)
    jobs = plan["jobs"]
    state = _loop_state(db, run_id, "figures", len(jobs), (fig.FIGURE_MODEL,), now)
    if state["done"]:
        return {"done": True, "batches": len(jobs), "finished": state["finished"],
                **({"stopped": state["stopped"]} if state.get("stopped") else {})}
    i = state["batch"]
    job = jobs[i]
    digest = parsed_digest(db, run_id)
    paper = papers.paper(digest)
    extracted = claims(db, run_id)["claims"]
    rendered = library.load(FIGURE_CHECKER, plan["prompt_version"]).render({
        "title": paper["title"], "figure_id": job["figure"].replace("figure:", "Figure "),
        "caption": fig.figure_caption(paper, job["figure"]),
        "claims_block": fig.claims_block([extracted[k] for k in job["claims"]]),
    })
    image = papers.figure(digest, job["figure"])
    if image is None:
        raise AuditError(f"no image for {job['figure']}")
    return {"done": False, "batch": i, "batches": len(jobs), "figure": job["figure"], "claims": len(job["claims"]),
            "attempt": state["attempt"], "model": state["model"], "wait_s": max(fig.FIGURE_PACE_S, round(state["wait"]) + 1),
            "version": rendered.version, "request": fig.figure_request(rendered.system, rendered.user, image, state["model"])}


def save_figure_check(db: Database, run_id: UUID, batch: int, output: Any = None, output_text: str | None = None,
                      usage: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate and store the vision model's verdicts for one figure."""
    plan = _figure_plan(db, run_id)
    if not 0 <= batch < len(plan["jobs"]):
        raise AuditError(f"figure job {batch} does not exist (0..{len(plan['jobs']) - 1})")
    job = plan["jobs"][batch]
    try:
        description, checks = fig.validate(output if output is not None else (output_text or ""), job["claims"])
    except ValueError as exc:
        db.add_step(run_id, "figures_batch", "failed", {"batch": batch, "error": str(exc)[:500], "output_text": (output_text or "")[:2000]})
        raise AuditError(f"the vision model's output is not usable: {exc}") from exc
    db.add_step(run_id, "figures_batch", "done", {"batch": batch, "figure": job["figure"], "description": description,
                                                  "checks": checks, "usage": usage})
    counts: dict[str, int] = {}
    for c in checks:
        counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
    return {"batch": batch, "figure": job["figure"], **counts}


def figure_checks(db: Database, run_id: UUID) -> dict[str, Any] | None:
    """Every claim checked against its figure, with the model's reading of each figure."""
    if not db.get_steps(run_id, "figure_plan"):
        return None
    plan = _figure_plan(db, run_id)
    extracted = claims(db, run_id)["claims"]
    latest: dict[int, dict[str, Any]] = {}
    for st in db.get_steps(run_id, "figures_batch"):
        if st["status"] == "done":
            latest[st["detail"]["batch"]] = st["detail"]
    checks = []
    for b in sorted(latest):
        for c in latest[b]["checks"]:
            claim = extracted[c["claim"]]
            checks.append({**c, "figure": latest[b]["figure"], "sentence_id": claim.get("sentence_id"), "text": claim.get("claim")})
    for f in plan["findings"]:
        claim = extracted[f["claim"]]
        checks.append({**f, "read_values": [], "sentence_id": claim.get("sentence_id"), "text": claim.get("claim")})
    summary: dict[str, Any] = {"figures": len({j["figure"] for j in plan["jobs"]}), "jobs": len(plan["jobs"]),
                               "jobs_done": len(latest), "checks": len(checks)}
    for v in (*fig.VERDICTS, "not_answered", "missing_figure"):
        summary[v] = sum(1 for c in checks if c["verdict"] == v)
    errors = [st["detail"] for st in db.get_steps(run_id, "figures_llm_error")]
    summary["llm_errors"] = len(errors)
    summary["rate_limited"] = sum(1 for e in errors if e.get("rate_limited"))
    summary["skipped"] = sorted({st["detail"]["batch"] for st in db.get_steps(run_id, "figures_batch") if st["status"] == "skipped"} - set(latest))
    summary["stopped"] = next((st["detail"]["reason"] for st in db.get_steps(run_id, "figures_stopped")), None)
    return {"summary": summary, "prompt_version": plan["prompt_version"],
            "figures_read": {latest[b]["figure"]: latest[b]["description"] for b in sorted(latest)}, "checks": checks}


def audit_result(db: Database, papers: PaperStore, run_id: UUID) -> dict[str, Any]:
    """The audit result so far (milestone 3 builds it up step by step): the parsed paper and its claims."""
    parse = [s for s in db.get_steps(run_id, "parse") if s["status"] == "done"]
    if not parse:
        raise AuditError("the run's PDF has not been parsed yet")
    p = parse[-1]["detail"]
    extracted = claims(db, run_id)
    numbers = [st["detail"] for st in db.get_steps(run_id, "number_check")]
    figures = figure_checks(db, run_id)
    implemented = "parse+claims" + ("+numbers" if numbers else "") + ("+figures" if figures else "")  # citations: step 6
    return {
        "branch": "audit",
        "implemented": implemented,
        "run_id": str(run_id),
        "paper": {k: p[k] for k in ("title", "pages", "sha256", "numbered", "items", "references") if k in p}
                 | {"tables": [t["id"] for t in p["tables"]], "figures": [f["id"] for f in p["figures"]]},
        "claims": extracted["claims"],
        "claims_dropped": len(extracted["dropped"]),
        "number_checks": numbers[-1] if numbers else None,
        "figure_checks": figures,
        "coverage": {k: extracted[k] for k in ("total_sentences", "candidate_sentences", "batches", "batches_done", "batches_skipped", "llm_errors",
                                                   "rate_limited", "models", "stopped", "by_type")},
        "prompt_versions": {EXTRACTOR: extracted["prompt_version"]} | ({FIGURE_CHECKER: figures["prompt_version"]} if figures else {}),
    }
