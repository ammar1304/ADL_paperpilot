"""The PaperPilot web app (D41): a page served by pp-tools that talks to the n8n webhook.

The browser never calls n8n directly (n8n's webhook sends no CORS headers, and a 15-minute
request is fragile in a browser). Instead:

    browser --POST /app/api/runs--> pp-tools --POST /webhook/paperpilot (background thread)--> n8n
    browser --GET /app/api/runs/{ref} (every 2 s)--> pp-tools: progress from the run's steps

The request carries a random ``client_ref`` that pp-tools stores with the run, so the page can
follow the run before the webhook has answered. n8n stays the orchestrator: this module only
starts a run the way every other client does and reads the steps the workflow records.
"""

import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID

import httpx

log = logging.getLogger("pp_tools.webapp")

REF_CHARS = "abcdefghijkmnpqrstuvwxyz23456789"


def new_ref() -> str:
    return "".join(secrets.choice(REF_CHARS) for _ in range(20))


def run_ref(run_id: Any) -> str:
    """A ref that names an existing run directly ("u" + the run id's 32 hex digits), used to reopen a
    finished run; refs from new_ref() are 20 characters, so the two never collide."""
    return "u" + UUID(str(run_id)).hex


def run_id_from_ref(ref: str) -> UUID | None:
    if len(ref) == 33 and ref.startswith("u"):
        try:
            return UUID(hex=ref[1:])
        except ValueError:
            return None
    return None


def block_pages(paper: dict[str, Any]) -> dict[str, int]:
    """Page of each text block ("b:48" -> 6), so the page can show where each checked sentence is."""
    return {b["id"]: b["page"] for b in paper.get("blocks") or [] if b.get("id") and b.get("page") is not None}


class WebhookRunner:
    """Calls the n8n webhook in the background and remembers how it answered, by client ref."""

    def __init__(self, webhook_url: str, timeout_s: float = 3600.0, workers: int = 2):
        self.webhook_url = webhook_url
        self.timeout_s = timeout_s
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pp-web")
        self.lock = threading.Lock()
        self.calls: dict[str, dict[str, Any]] = {}

    def start(self, ref: str, request: dict[str, Any]) -> None:
        with self.lock:
            self.calls[ref] = {"started": time.time(), "status_code": None, "body": None, "error": None}
        self.pool.submit(self._call, ref, request)

    def _call(self, ref: str, request: dict[str, Any]) -> None:
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                resp = client.post(self.webhook_url, json=request)
            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text[:2000]
            update = {"status_code": resp.status_code, "body": body}
        except Exception as exc:  # noqa: BLE001 - the page must learn that the call failed
            log.warning("webhook call failed", extra={"ref": ref, "error": str(exc)[:300]})
            update = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        with self.lock:
            self.calls.setdefault(ref, {}).update(update)

    def get(self, ref: str) -> dict[str, Any] | None:
        with self.lock:
            call = self.calls.get(ref)
            return dict(call) if call else None

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


# ---- progress, read from the steps the workflows record -----------------------------------------


def _count(value: Any) -> int:
    """A step stores either a count or the list itself (claims_batch keeps the kept claims)."""
    return len(value) if isinstance(value, list) else int(value or 0)


def _done_batches(steps: list[dict[str, Any]], name: str) -> set[int]:
    return {s["detail"].get("batch") for s in steps if s["step_name"] == name and s["status"] in ("done", "skipped")}


def _last(steps: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    found = [s for s in steps if s["step_name"] == name]
    return found[-1] if found else None


def _stage(key: str, label: str, who: str, state: str, note: str = "", done: int | None = None, total: int | None = None) -> dict[str, Any]:
    return {"key": key, "label": label, "who": who, "state": state, "note": note, "done": done, "total": total}


def _states(done: list[bool], finished: bool) -> list[str]:
    """A stage is done when its own step is recorded or any later stage has started;
    the first stage not done is the active one."""
    out, active_given = [], False
    for i in range(len(done)):
        if finished or any(done[i:]):
            out.append("done")
        elif not active_given:
            out.append("active")
            active_given = True
        else:
            out.append("waiting")
    return out


def novelty_progress(steps: list[dict[str, Any]], finished: bool) -> list[dict[str, Any]]:
    names = [s["step_name"] for s in steps]
    plans = [s for s in steps if s["step_name"] == "plan"]
    verifies = [s for s in steps if s["step_name"] == "verify"]
    decompose = _last(steps, "decompose")
    retrieve = next((s for s in steps if s["step_name"] == "retrieve"), None)
    facets = (decompose or {}).get("detail", {}).get("output", {}).get("facets") or {}
    queries = (decompose or {}).get("detail", {}).get("output", {}).get("queries") or []
    st = _states(["decompose" in names, "retrieve" in names, bool(plans) or "judge_prompt" in names,
                  "judge_prompt" in names and bool(verifies), any(v["detail"].get("status") != "retry" for v in verifies)], finished)
    cands = len((retrieve or {}).get("detail", {}).get("candidates") or [])
    web = sum(len([q for q in (p["detail"].get("queries") or []) if q.get("where") == "web"]) for p in plans)
    return [
        _stage("read", "Reading the idea", "Decomposer (LLM)", st[0],
               f"task: {facets.get('task')}" if facets else "Splitting it into task, method, data and result"),
        _stage("search", "Searching 531 papers", "Retriever (code)", st[1],
               f"{len(queries)} queries, {cands} candidate papers" if retrieve else "Several queries, fused and reranked"),
        _stage("agent", "Looking for what is missing", "Search agent (LLM)", st[2],
               (f"{len(plans)} round(s), {web} web search(es)" if plans else "Decides whether to search again, here or on the web")),
        _stage("judge", "Comparing with each paper", "Judge (LLM)", st[3], "Facet by facet, citing the passages"),
        _stage("verify", "Checking the judge", "Verifier (code)", st[4],
               (f"attempt {len(verifies)}: {verifies[-1]['detail'].get('status', '')}" if verifies else "Rejects any paper or passage retrieval never returned")),
    ]


def audit_progress(steps: list[dict[str, Any]], finished: bool) -> list[dict[str, Any]]:
    names = set(s["step_name"] for s in steps)
    prompts = _last(steps, "claim_prompts")
    claims_total = len((prompts or {}).get("detail", {}).get("batches") or [])
    claims_done = len(_done_batches(steps, "claims_batch"))
    kept = sum(_count(s["detail"].get("kept")) for s in steps if s["step_name"] == "claims_batch" and s["status"] == "done")
    fplan = _last(steps, "figure_plan")
    fig_total = len((fplan or {}).get("detail", {}).get("jobs") or [])
    fig_done = len(_done_batches(steps, "figures_batch"))
    cplan = _last(steps, "citation_plan")
    cit_total = len((cplan or {}).get("detail", {}).get("batches") or [])
    cit_jobs = len((cplan or {}).get("detail", {}).get("jobs") or [])
    cit_done = len(_done_batches(steps, "citations_batch"))
    numbers = _last(steps, "number_check")
    nsum = (numbers or {}).get("detail", {}).get("summary") or {}
    parse = _last(steps, "parse")
    pd = (parse or {}).get("detail", {})
    st = _states(["parse" in names, "number_check" in names or (claims_total > 0 and claims_done >= claims_total),
                  "number_check" in names, "citation_plan" in names or (fig_total > 0 and fig_done >= fig_total),
                  cit_total > 0 and cit_done >= cit_total], finished)
    stopped = {k: (_last(steps, f"{k}_stopped") or {}).get("detail", {}).get("reason") for k in ("claims", "figures", "citations")}
    return [
        _stage("parse", "Reading the PDF", "Parser (Docling)", st[0],
               (f"{pd.get('pages')} pages, {len(pd.get('tables') or [])} tables, {len(pd.get('figures') or [])} figures" if parse
                else "Layout, tables as cell grids, figures as images (2 to 5 min)")),
        _stage("claims", "Finding the claims", "Claim extractor (LLM)", st[1],
               stopped["claims"] or (f"{kept} claims kept" if claims_done else "Sentences with numbers, figures or citations"),
               claims_done, claims_total or None),
        _stage("numbers", "Checking numbers against the tables", "Number check (code)", st[2],
               (f"{nsum.get('supported', 0)} supported, {nsum.get('contradicted', 0)} contradicted, {nsum.get('unverifiable', 0)} unverifiable"
                if numbers else "Recomputes every margin from the table cells")),
        _stage("figures", "Looking at the figures", "Vision model (LLM)", st[3],
               stopped["figures"] or ("One image per call, read before judging" if not fplan else f"{fig_total} figure(s) to check"),
               fig_done, fig_total or None),
        _stage("citations", "Reading the cited papers", "Citation checker (RAG + LLM)", st[4],
               stopped["citations"] or ("Retrieves the cited paper's passages" if not cplan else f"{cit_jobs} citation(s) in the library"),
               cit_done, cit_total or None),
    ]


def status_payload(run: dict[str, Any] | None, steps: list[dict[str, Any]], call: dict[str, Any] | None) -> dict[str, Any]:
    """What the page needs every two seconds: state, stages, and the result once there is one."""
    if run is None:
        if call and call.get("status_code") == 422:
            return {"state": "rejected", "errors": (call.get("body") or {}).get("errors") or []}
        if call and (call.get("error") or (call.get("status_code") or 200) >= 400):
            return {"state": "failed", "error": call.get("error") or f"HTTP {call['status_code']}"}
        return {"state": "starting"}
    finished = run["status"] in ("done", "failed")
    mode = run["mode"]
    stages = novelty_progress(steps, finished) if mode == "novelty" else audit_progress(steps, finished)
    out: dict[str, Any] = {"state": run["status"] if run["status"] in ("done", "failed") else "running", "mode": mode,
                           "run_id": str(run["run_id"]), "input": run.get("input") or {}, "stages": stages,
                           "created_at": run.get("created_at")}
    if run["status"] == "done":
        out["result"] = run.get("result")
    if run["status"] == "failed":
        out["error"] = (run.get("error") or {}).get("message") or "the run failed"
        for s in out["stages"]:
            if s["state"] == "active":
                s["state"] = "failed"
    return out
