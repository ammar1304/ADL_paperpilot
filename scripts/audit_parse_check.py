#!/usr/bin/env python3
"""Milestone 3, step 2 check: parse a real paper through pp-tools and the parser service.

Creates an audit run for an arXiv PDF, starts the parse job, polls it, and
prints what came out: numbered tables (with the first rows of each grid),
figures, and how each caption was found. A second run on the same PDF must be
served from the cache.

    python scripts/audit_parse_check.py
    python scripts/audit_parse_check.py --url https://arxiv.org/pdf/2403.14403v2
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_test import _dotenv, http  # noqa: E402


def run_parse(pp: str, url: str, timeout_s: float) -> tuple[str, dict]:
    status, body = http("POST", f"{pp}/runs", {"request": {"mode": "audit", "draft_pdf_url": url}})
    if status != 201:
        raise SystemExit(f"could not create the run: HTTP {status} {body}")
    run_id = body["run_id"]
    status, job = http("POST", f"{pp}/audit/{run_id}/parse")
    if status != 202:
        raise SystemExit(f"could not start the parse: HTTP {status} {job}")
    started, last = time.monotonic(), None
    while True:
        _, job = http("GET", f"{pp}/jobs/{job['job_id']}")
        line = f"  {job['status']} {job.get('progress')}"
        if line != last:
            print(line, flush=True)
            last = line
        if job["status"] in ("done", "failed"):
            return run_id, job
        if time.monotonic() - started > timeout_s:
            raise SystemExit("timed out")
        time.sleep(3)


def main() -> int:
    env = _dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="https://arxiv.org/pdf/2401.15884v3")  # CRAG
    ap.add_argument("--pp-tools-url", default=f"http://127.0.0.1:{env.get('PP_TOOLS_HOST_PORT', '8000')}")
    ap.add_argument("--timeout", type=float, default=1800)
    args = ap.parse_args()
    pp = args.pp_tools_url

    _, health = http("GET", f"{pp}/health")
    print("parser healthy:", (health.get("checks") or {}).get("parser"))
    print(f"\n1st run (parses the PDF): {args.url}")
    run_id, job = run_parse(pp, args.url, args.timeout)
    if job["status"] != "done":
        print("FAILED:", job.get("error"))
        return 1
    r = job["result"]
    print(f"\n{r['title']} | {r['pages']} pages | {r['seconds']} s | {r['numbered']}/{r['items']} tables+figures numbered")
    _, paper = http("GET", f"{pp}/audit/{run_id}/paper")
    for t in paper["tables"]:
        print(f"\n{t['id']} (page {t['page']}, caption: {t['caption_source']}) {t['caption'][:90]}")
        for row in t["grid"][:4]:
            print("   | " + " | ".join(c[:14] for c in row[:8]))
    print()
    for f in paper["figures"]:
        print(f"{f['id']} (page {f['page']}, caption: {f['caption_source']}, image: {f['has_image']}) {f['caption'][:80]}")
    if paper["figures"]:
        import urllib.request

        with urllib.request.urlopen(f"{pp}/audit/{run_id}/figures/{paper['figures'][0]['id']}", timeout=30) as resp:
            png = resp.read()
        print(f"\nfirst figure image served: {resp.status}, {len(png)} bytes, PNG: {png[:4] == b'\x89PNG'}")

    print("\n2nd run (same PDF, must come from the cache):")
    _, job2 = run_parse(pp, args.url, 120)
    cached = job2["status"] == "done" and job2["result"]["cached"]
    print("cached:", cached, f"({job2['result']['seconds']} s)" if job2["status"] == "done" else job2.get("error"))
    return 0 if cached else 1


if __name__ == "__main__":
    raise SystemExit(main())
