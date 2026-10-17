#!/usr/bin/env python3
"""Run full audits through the n8n webhook, as a user would, and save the results.

    python scripts/audit_run.py                                   # CRAG
    python scripts/audit_run.py --url https://arxiv.org/pdf/2403.14403v2 --url https://arxiv.org/pdf/2310.11511v1

Results: eval/results/<timestamp>-audit/ (one <name>.json per paper: the result
and every run step) and summary.md.
"""

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_test import _dotenv, http  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    env = _dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", action="append")
    ap.add_argument("--n8n-url", default=f"http://127.0.0.1:{env.get('N8N_HOST_PORT', '5678')}")
    ap.add_argument("--pp-tools-url", default=f"http://127.0.0.1:{env.get('PP_TOOLS_HOST_PORT', '8000')}")
    ap.add_argument("--label", default="audit")
    args = ap.parse_args()
    urls = args.url or ["https://arxiv.org/pdf/2401.15884v3"]
    out = REPO / "eval" / "results" / f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{args.label}"
    out.mkdir(parents=True)
    lines = ["| Paper | Seconds | Status | Candidate sentences | Batches | Claims kept | Dropped by the guardrail | By type | Number checks (supported / contradicted / unverifiable) | Figure checks (supported / contradicted / not shown; figures) | Citations (in library: supported / contradicted / not found; not in library) |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for url in urls:
        name = url.rstrip("/").split("/")[-1]
        print(f"{name}: auditing (parse, claims, numbers, figures, citations; several minutes)...", flush=True)
        started = time.monotonic()
        status, body = http("POST", f"{args.n8n_url}/webhook/paperpilot", {"mode": "audit", "draft_pdf_url": url}, timeout=3600)
        secs = round(time.monotonic() - started)
        if status != 200 or not isinstance(body, dict):
            print(f"  HTTP {status}: {str(body)[:300]}")
            lines.append(f"| {name} | {secs} | HTTP {status} | | | | | | | | |")
            continue
        result, run_id = body.get("result") or {}, body.get("run_id")
        _, steps = http("GET", f"{args.pp_tools_url}/runs/{run_id}/steps")
        (out / f"{name}.json").write_text(json.dumps({"url": url, "run_id": run_id, "result": result, "steps": steps}, ensure_ascii=False, indent=1), encoding="utf-8")
        cov = result.get("coverage", {})
        lines.append(f"| {name} | {secs} | {body.get('status')} | {cov.get('candidate_sentences')}/{cov.get('total_sentences')} | "
                     f"{cov.get('batches_done')}/{cov.get('batches')} | {len(result.get('claims', []))} | {result.get('claims_dropped')} | {cov.get('by_type')} | "
                     + (f"{nc['summary']['supported']} / {nc['summary']['contradicted']} / {nc['summary']['unverifiable']} |" if (nc := result.get("number_checks")) else "not run |")
                     + (f" {fs['supported']} / {fs['contradicted']} / {fs['not_shown']}; {fs['jobs_done']}/{fs['jobs']} |"
                        if (fs := (result.get("figure_checks") or {}).get("summary")) else " not run |")
                     + (f" {cs['in_library']}: {cs['supported']} / {cs['contradicted']} / {cs['not_found']}; {cs['not_in_library']} |"
                        if (cs := (result.get("citation_checks") or {}).get("summary")) else " not run |"))
        print("  " + lines[-1], flush=True)
        for c in result.get("claims", [])[:8]:
            vals = ", ".join(f"{v['system']}={v['value']}" for v in c.get("values", []))
            print(f"    [{c['type']}] {c['claim'][:110]} | {vals} | refs {c.get('refers_to')} cites {c.get('cites')}")
        for c in (result.get("number_checks") or {}).get("checks", []):
            if c["verdict"] != "unverifiable":
                print(f"    {c['verdict'].upper():12} {c['kind']:10} {c['claimed']:>8} {c.get('system', '')[:25]} vs {c.get('baseline', '')[:20]}: {c.get('note', '')[:90]}")
        for c in (result.get("citation_checks") or {}).get("checks", []):
            if c.get("cited_paper"):
                print(f"    CITATION {c['verdict'].upper():12} {c['marker'][:24]:24} -> {(c.get('cited_title') or '')[:40]} | {(c.get('text') or '')[:70]} | {c.get('reason', '')[:90]}")
        for c in (result.get("figure_checks") or {}).get("checks", []):
            print(f"    FIGURE {c['verdict'].upper():12} {c['figure']:10} {(c.get('text') or '')[:80]} | {c.get('evidence', '')[:90]}")
        reasons = Counter(d.get("reason", "")[:40] for s in steps if s["step_name"] == "claims_batch" for d in s["detail"].get("dropped", []))
        if reasons:
            print("    dropped:", dict(reasons))
    (out / "summary.md").write_text("# Audit runs\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines) + f"\n\nWrote {out.relative_to(REPO)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
