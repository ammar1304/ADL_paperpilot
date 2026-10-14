#!/usr/bin/env python3
"""End-to-end smoke test for the PaperPilot milestone 0 stack.

Standard library only, so it runs on Linux, macOS and Windows without a venv.

    python scripts/smoke_test.py               # docker compose up, then test
    python scripts/smoke_test.py --no-compose  # test services that are already running

Exits non-zero with a clear message on the first failure.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SmokeFailure(Exception):
    pass


def _dotenv() -> dict[str, str]:
    """Port settings from the repo's .env, so the smoke test follows PP_TOOLS_HOST_PORT and N8N_HOST_PORT."""
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def http(method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: float = 30):
    """Returns (status, parsed JSON or raw text). Never raises on HTTP error codes."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read().decode()
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def check(condition: bool, message: str, detail: object = None) -> None:
    if not condition:
        extra = f"\n    got: {json.dumps(detail, default=str)[:600]}" if detail is not None else ""
        raise SmokeFailure(message + extra)
    print(f"  ok  {message}")


def wait_for(label: str, fn, timeout_s: float) -> object:
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            ok, last = fn()
            if ok:
                print(f"  ok  {label}")
                return last
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            last = str(exc)
        time.sleep(3)
    raise SmokeFailure(f"timed out after {timeout_s:.0f}s waiting for {label}\n    last: {str(last)[:600]}")


def compose_up() -> None:
    print("\n[1] docker compose up (builds pp-tools, waits for healthchecks)")
    cmd = ["docker", "compose", "up", "-d", "--build", "--wait", "--wait-timeout", "300"]
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        raise SmokeFailure("docker compose up failed; run `docker compose ps` and `docker compose logs`")


def corpus_check(pp: str, n8n: str, limit: int) -> None:
    print(f"\n[6] knowledge base: ingest {limit} papers through n8n, then search")

    def embedder_ready():
        status, body = http("GET", f"{pp}/health", timeout=10)
        return isinstance(body, dict) and body.get("checks", {}).get("embedder", {}).get("ok") is True, body

    # The first start downloads the embedding model (about 2.3 GB).
    wait_for("embedding model loaded", embedder_ready, 1200)
    status, body = http("POST", f"{n8n}/webhook/ingest", {"recreate": True, "limit": limit})
    if status == 400:
        raise SmokeFailure(f"no manifest yet: {body}. See corpus/README.md (corpus selection).")
    check(status == 202 and "job_id" in body, "ingest workflow started a build job", body)
    job_id = body["job_id"]

    def job_done():
        s, job = http("GET", f"{pp}/jobs/{job_id}", timeout=10)
        return s == 200 and job["status"] in ("done", "failed"), job

    job = wait_for("build job finished", job_done, 1800)
    check(job["status"] == "done", "build job succeeded", job)
    result = job["result"]
    check(result["papers_indexed"] == min(limit, result["papers_in_manifest"]), "every requested paper indexed", result)
    if result["pdf_failures"]:
        print(f"  !!  PDFs that fell back to the abstract: {result['pdf_failures']}")
    for strategy in ("fixed", "structured"):
        s, res = http("POST", f"{pp}/search", {"query": "retrieval-augmented question answering", "strategy": strategy, "k": 3})
        check(s == 200 and len(res["hits"]) > 0, f"search on the {strategy} collection returns hits", res)


def novelty_check(pp: str, webhook: str) -> None:
    print("\n[7] novelty check: LLM decomposer, retrieval, LLM judge, verifier")
    idea = (
        "Let the language model rewrite its search query after each retrieval step, "
        "so that it can answer multi-hop questions that need several documents."
    )
    status, body = http("POST", webhook, {"mode": "novelty", "idea_text": idea}, timeout=300)
    if status == 500:
        raise SmokeFailure(
            f"the novelty run failed: {body}. Check GROQ_API_KEY in .env, the knowledge base (step 6), "
            "and the failed execution in the n8n UI (Executions)."
        )
    check(status == 200, "novelty request returns HTTP 200", body)
    result = body.get("result", {})
    check(result.get("branch") == "novelty" and result.get("implemented") is True, "novelty branch ran for real", result)
    check(set(result.get("idea_facets", {})) == {"task", "method", "data", "result"}, "the LLM split the idea into four facets", result)
    check(result.get("coverage", {}).get("papers_in_knowledge_base", 0) > 0, "result states the knowledge-base coverage", result)
    for m in result.get("closest_work", []):
        check(all(e.startswith(m["paper_id"] + "#") for e in m["evidence"]), f"evidence of {m['paper_id']} cites its own chunks", m)
    print(f"  ..  {len(result.get('closest_work', []))} verified matches, {len(result.get('unverified', []))} unverified, "
          f"judge attempts: {result.get('judge_attempts')}")
    s, steps = http("GET", f"{pp}/runs/{body['run_id']}/steps")
    names = [st["step_name"] for st in steps] if s == 200 else []
    check(names[:2] == ["decompose", "retrieve"] and "verify" in names, "every step was recorded for audit", names)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-compose", action="store_true", help="do not start docker compose; test running services")
    env = _dotenv()
    pp_port = os.environ.get("PP_TOOLS_HOST_PORT") or env.get("PP_TOOLS_HOST_PORT") or "8000"
    n8n_port = os.environ.get("N8N_HOST_PORT") or env.get("N8N_HOST_PORT") or "5678"
    parser.add_argument("--pp-tools-url", default=os.environ.get("SMOKE_PP_TOOLS_URL", f"http://localhost:{pp_port}"))
    parser.add_argument("--n8n-url", default=os.environ.get("SMOKE_N8N_URL", f"http://localhost:{n8n_port}"))
    parser.add_argument(
        "--allow-degraded-qdrant",
        action="store_true",
        help="accept /health reporting Qdrant down (only for running without Qdrant; never in the real stack)",
    )
    parser.add_argument(
        "--with-corpus",
        action="store_true",
        help="also build a small knowledge base through the 10-ingest-papers workflow and search it "
        "(needs corpus/manifest.csv, internet access and the embedding model)",
    )
    parser.add_argument("--corpus-limit", type=int, default=5, help="papers to index with --with-corpus")
    parser.add_argument(
        "--with-llm",
        action="store_true",
        help="also run a full novelty check (decomposer and judge LLM calls); needs a built knowledge base "
        "(--with-corpus or an earlier build) and GROQ_API_KEY in .env",
    )
    parser.add_argument(
        "--skip-fault-test",
        action="store_true",
        help="skip the error-workflow test (use when n8n runs with PP_ALLOW_TEST_FAULTS=false)",
    )
    args = parser.parse_args()
    pp, n8n = args.pp_tools_url.rstrip("/"), args.n8n_url.rstrip("/")
    webhook = f"{n8n}/webhook/paperpilot"

    try:
        if args.no_compose:
            print("\n[1] skipping docker compose (--no-compose)")
        else:
            compose_up()

        print("\n[2] health checks")

        def pp_health():
            status, body = http("GET", f"{pp}/health", timeout=10)
            if status == 200:
                return True, body
            if args.allow_degraded_qdrant and isinstance(body, dict):
                checks = body.get("checks", {})
                return checks.get("postgres", {}).get("ok") is True, body
            return False, body

        health = wait_for("pp-tools /health", pp_health, 120)
        if args.allow_degraded_qdrant and health.get("status") != "ok":
            print(f"  !!  Qdrant reported down and accepted because of --allow-degraded-qdrant: {health['checks']['qdrant']}")
        wait_for("n8n /healthz", lambda: (http("GET", f"{n8n}/healthz", timeout=10)[0] == 200, None), 180)

        print("\n[3] audit request through the webhook (branch still a placeholder)")

        def audit():
            status, body = http(
                "POST", webhook, {"mode": "audit", "draft_pdf_url": "https://arxiv.org/pdf/1706.03762", "venue": "NeurIPS"}
            )
            # 404 means the webhook is not registered yet (n8n still activating workflows).
            return status != 404, (status, body)

        status, body = wait_for("webhook registered", audit, 120)
        check(status == 200, "audit request returns HTTP 200", body)
        check(body.get("result", {}).get("branch") == "audit", "routed to the audit branch", body)
        check(body.get("result", {}).get("implemented") is False, "audit branch honestly reports implemented=false", body)
        s, run = http("GET", f"{pp}/runs/{body['run_id']}")
        check(s == 200 and run["status"] == "done", "audit run stored with status done", run)
        check(run["input"]["draft_pdf_url"] == "https://arxiv.org/pdf/1706.03762", "stored input matches what was sent", run)

        print("\n[4] invalid request is rejected before any run is created")
        before = len(http("GET", f"{pp}/runs?limit=100")[1])
        status, body = http("POST", webhook, {"mode": "novelty", "idea_text": "too short"})
        check(status == 422, "invalid request returns HTTP 422", body)
        check(body["errors"][0]["field"] == "idea_text", "error names the offending field", body)
        after = len(http("GET", f"{pp}/runs?limit=100")[1])
        check(after == before or before == 100, "no run row was created", {"before": before, "after": after})

        print("\n[5] error workflow marks the run failed")
        if args.skip_fault_test:
            print("  --  skipped (--skip-fault-test)")
        else:
            marker = f"fault-{uuid.uuid4().hex[:8]}"
            status, body = http(
                "POST",
                webhook,
                {"mode": "novelty", "idea_text": f"{marker}: this run must end up failed."},
                headers={"X-PaperPilot-Test-Fault": "fail-after-create"},
            )
            check(status == 500, "faulted request returns HTTP 500 (n8n workflow error)", body)

            def failed_run():
                runs = http("GET", f"{pp}/runs?limit=20")[1]
                match = [r for r in runs if marker in (r["input"].get("idea_text") or "")]
                return bool(match) and match[0]["status"] == "failed", match

            match = wait_for("run marked failed by 90-error-handler", failed_run, 30)
            check("Injected test fault" in match[0]["error"]["message"], "stored error explains the failure", match[0])

        if args.with_corpus:
            corpus_check(pp, n8n, args.corpus_limit)
        if args.with_llm:
            novelty_check(pp, webhook)

    except SmokeFailure as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        return 1

    print("\nAll smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
