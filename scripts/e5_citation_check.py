#!/usr/bin/env python3
"""E5 (citations): does the citation check find the right passage, and judge it right?

A gold set of 40 claim-citation pairs about 15 library papers (eval/e5/citation_claims.json),
written by reading those papers: 25 restate a statement of the cited paper ("supported"),
15 change its method or a number, or reverse its finding ("unsupported"). Eight pairs come
from four sentences that each cite two papers for different things (F-024).

Two things are measured separately, because they fail separately:
  1. Retrieval (no LLM): do the three passages the audit would send contain the gold
     evidence phrase? Passages come from pp-tools' POST /citations/passages, the same code
     the audit uses (dense search in the cited paper, cross-encoder rerank, best window).
  2. Verdicts (P6 on Groq): supported pairs called supported; unsupported pairs never
     called supported (contradicted is the best answer, not_found is safe).

Several prompt versions can be compared on the same passages (--versions v1,v2).

Needs the stack running (pp-tools on PP_TOOLS_HOST_PORT from .env, default 8000; the library built) and GROQ_API_KEY in .env.
Costs about 2,500 tokens per call and 15 to 20 calls per version (gpt-oss-120b first, 20b
on its daily limit), 30 s apart.

    python scripts/e5_citation_check.py                     # active P6 version
    python scripts/e5_citation_check.py --versions v1,v2    # compare prompt versions
    python scripts/e5_citation_check.py --retrieval-only    # no tokens: retrieval only
"""

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "services" / "pp_tools"))
from app.audit import citations as cit  # noqa: E402


def _models() -> tuple[str, tuple[str, ...]]:
    """The audit's model order, read from app/audit/claims.py without importing it (it needs
    pydantic, which the host Python running this script may not have)."""
    src = (REPO / "services" / "pp_tools" / "app" / "audit" / "claims.py").read_text(encoding="utf-8")
    first = re.search(r'^CLAIMS_MODEL = "([^"]+)"', src, re.M).group(1)
    rest = tuple(re.findall(r'"([^"]+)"', re.search(r"^CLAIMS_FALLBACK_MODELS = \(([^)]*)\)", src, re.M).group(1)))
    return first, rest


CLAIMS_MODEL, CLAIMS_FALLBACK_MODELS = _models()
from app.prompts import PromptLibrary  # noqa: E402

GROQ = "https://api.groq.com/openai/v1/chat/completions"
PACE_S = 30
MAX_ATTEMPTS = 3


def dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def post(url: str, body: dict, headers: dict | None = None, timeout: float = 120) -> tuple[int, object, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "PaperPilot/0.1", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw), dict(exc.headers)
        except ValueError:
            return exc.code, raw, dict(exc.headers)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"network: {type(exc).__name__}: {exc}", {}


def norm(text: str) -> str:
    """Lower case, without spaces or hyphens: a phrase still matches across a line break
    ("9%- 19%") or a ligature ("ﬁne")."""
    text = unicodedata.normalize("NFKC", text or "").lower().replace("’", "'")
    return re.sub(r"[\s\-‐‑‒–]+", "", text)


def retrieve(items: list[dict], pp_tools: str) -> list[dict]:
    """One job per gold item, shaped like the audit's jobs, with its passages and retrieval hit."""
    jobs = []
    for it in items:
        status, body, _ = post(f"{pp_tools}/citations/passages", {"paper_id": it["paper_id"], "claim": it["claim"], "cites": [it["marker"]]})
        if status != 200:
            raise SystemExit(f"pp-tools /citations/passages answered {status}: {str(body)[:300]}")
        passages = body["passages"]
        evidence = norm(it["evidence"])
        hit_rank = next((k for k, p in enumerate(passages, 1) if evidence in norm(p["text"])), None)
        jobs.append({"item": it["id"], "marker": it["marker"], "title": it["title"], "fulltext": True,
                     "paper_id": it["paper_id"], "query": body["query"], "passages": passages, "hit_rank": hit_rank})
    return jobs


def judge(items: list[dict], jobs: list[dict], version: str, key: str, out) -> dict[str, dict]:
    """P6 over every pair, batched as the audit batches them; returns item id -> check."""
    prompt_lib = PromptLibrary(REPO / "prompts")
    claims = [{"claim": it["claim"], "sentence": it["sentence"]} for it in items]
    for i, job in enumerate(jobs):
        job["claim"] = i
    models = [CLAIMS_MODEL, *CLAIMS_FALLBACK_MODELS]
    results: dict[str, dict] = {}
    for b, batch in enumerate(cit.batches(jobs, claims)):
        block = "\n\n".join(cit.pair_block(k, claims[jobs[j]["claim"]], jobs[j]) for k, j in enumerate(batch, 1))
        rendered = prompt_lib.load("P6-citation-checker", version).render({"pairs_block": block})
        attempts = 0
        while True:
            body = cit.citation_request(rendered.system, rendered.user, models[0])
            started = time.monotonic()
            status, answer, headers = post(GROQ, body, {"Authorization": f"Bearer {key}"}, timeout=90)
            call = {"version": version, "batch": b, "model": models[0], "status": status, "seconds": round(time.monotonic() - started, 1)}
            if status == 429:
                retry = float(headers.get("retry-after") or headers.get("Retry-After") or 30)
                if retry > 90 and len(models) > 1:  # daily limit on this model: next model, same attempt
                    call["note"] = f"daily limit on {models.pop(0)}"
                    out.write(json.dumps(call) + "\n")
                    continue
                if retry > 90:
                    raise SystemExit(f"Groq daily token limit reached on every model (retry-after {retry:.0f} s); results so far are saved")
                call["note"] = f"rate limited, waiting {retry:.0f} s"
                out.write(json.dumps(call) + "\n")
                time.sleep(retry + 1)
                continue
            attempts += 1
            try:
                if status != 200:
                    raise ValueError(f"HTTP {status}: {str(answer)[:300]}")
                content = answer["choices"][0]["message"]["content"]
                checks = cit.validate(content, batch, jobs)
                call["usage"] = answer.get("usage")
                out.write(json.dumps(call) + "\n")
                for c in checks:
                    results[jobs[c["job"]]["item"]] = c
                break
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                call["error"] = str(exc)[:300]
                out.write(json.dumps(call) + "\n")
                if attempts >= MAX_ATTEMPTS:
                    for j in batch:
                        results[jobs[j]["item"]] = {"job": j, "verdict": "failed", "evidence": [], "reason": str(exc)[:200]}
                    break
            finally:
                out.flush()
                time.sleep(PACE_S)
    return results


def score(items: list[dict], results: dict[str, dict]) -> dict:
    grid = Counter((it["gold"], results.get(it["id"], {}).get("verdict", "failed")) for it in items)
    sup = [it for it in items if it["gold"] == "supported"]
    uns = [it for it in items if it["gold"] == "unsupported"]
    multi = [it for it in items if it["kind"] == "several citations" and it["gold"] == "supported"]
    v = lambda it: results.get(it["id"], {}).get("verdict")  # noqa: E731
    return {
        "grid": {f"{g} -> {p}": n for (g, p), n in sorted(grid.items())},
        "supported_recognised": f"{sum(v(i) == 'supported' for i in sup)}/{len(sup)}",
        "false_alarms": f"{sum(v(i) == 'contradicted' for i in sup)}/{len(sup)}",
        "errors_accepted": f"{sum(v(i) == 'supported' for i in uns)}/{len(uns)}",
        "errors_contradicted": f"{sum(v(i) == 'contradicted' for i in uns)}/{len(uns)}",
        "multi_citation_supported": f"{sum(v(i) == 'supported' for i in multi)}/{len(multi)}",
        "failed": sum(v(i) in (None, "failed", "not_answered") for i in items),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", type=Path, default=REPO / "eval" / "e5" / "citation_claims.json")
    port = dotenv().get("PP_TOOLS_HOST_PORT") or "8000"  # a team PC moved it to 8010 (README, troubleshooting)
    ap.add_argument("--pp-tools-url", default=f"http://127.0.0.1:{port}")
    ap.add_argument("--versions", default="", help="comma-separated P6 versions (default: the active one)")
    ap.add_argument("--retrieval-only", action="store_true", help="measure retrieval only, no LLM call")
    ap.add_argument("--label", default="e5-citations")
    args = ap.parse_args()

    items = json.loads(args.gold.read_text(encoding="utf-8"))["items"]
    folder = REPO / "eval" / "results" / f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{args.label}"
    folder.mkdir(parents=True)

    jobs = retrieve(items, args.pp_tools_url.rstrip("/"))
    hits = [j["hit_rank"] for j in jobs]
    found = sum(h is not None for h in hits)
    (folder / "passages.jsonl").write_text("".join(json.dumps(j, ensure_ascii=False) + "\n" for j in jobs), encoding="utf-8")
    lines = [f"# E5 citation check: {args.label}", "",
             f"Gold: {len(items)} pairs ({sum(i['gold'] == 'supported' for i in items)} supported, "
             f"{sum(i['gold'] == 'unsupported' for i in items)} unsupported), {len({i['paper_id'] for i in items})} papers.", "",
             "## Retrieval (no LLM)", "",
             f"Gold evidence in the 3 passages sent to the judge: **{found}/{len(jobs)}** "
             f"(rank 1: {hits.count(1)}, rank 2: {hits.count(2)}, rank 3: {hits.count(3)}).", ""]
    missed = [j for j in jobs if j["hit_rank"] is None]
    if missed:
        lines.append("Missed: " + ", ".join(f"{j['item']} ({j['paper_id']})" for j in missed))
        lines.append("")
    print("\n".join(lines))

    if not args.retrieval_only:
        key = dotenv().get("GROQ_API_KEY") or ""
        if not key:
            raise SystemExit("GROQ_API_KEY is missing from .env")
        versions = [v for v in args.versions.split(",") if v] or [PromptLibrary(REPO / "prompts").load("P6-citation-checker").version]
        summary = {}
        for version in versions:
            with (folder / f"calls-{version}.jsonl").open("a", encoding="utf-8") as out:
                results = judge(items, [dict(j) for j in jobs], version, key, out)
            (folder / f"verdicts-{version}.jsonl").write_text(
                "".join(json.dumps({"item": it["id"], "gold": it["gold"], "kind": it["kind"], **results.get(it["id"], {})}, ensure_ascii=False) + "\n"
                        for it in items), encoding="utf-8")
            summary[version] = s = score(items, results)
            wrong = [(it, results.get(it["id"], {})) for it in items
                     if (it["gold"] == "supported") != (results.get(it["id"], {}).get("verdict") == "supported")]
            lines += [f"## Verdicts, P6 {version}", "",
                      "| Measure | Result |", "| --- | --- |",
                      f"| Supported pairs recognised | {s['supported_recognised']} |",
                      f"| False alarms (supported pair called contradicted) | {s['false_alarms']} |",
                      f"| Errors accepted (unsupported pair called supported) | {s['errors_accepted']} |",
                      f"| Errors called contradicted | {s['errors_contradicted']} |",
                      f"| Two-citation sentences, supported parts recognised | {s['multi_citation_supported']} |",
                      f"| Pairs without a verdict | {s['failed']} |", "",
                      "Gold -> verdict: " + ", ".join(f"{k}: {n}" for k, n in s["grid"].items()), ""]
            if wrong:
                lines.append("Disagreements:")
                for it, r in wrong:
                    lines.append(f"- {it['id']} ({it['kind']}, gold {it['gold']}): {r.get('verdict')} | {it['claim'][:110]} | {r.get('reason', '')[:160]}")
                lines.append("")
        (folder / "summary.json").write_text(json.dumps({"retrieval": {"found": found, "pairs": len(jobs)}, "verdicts": summary}, indent=2) + "\n", encoding="utf-8")
    (folder / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[lines.index("## Retrieval (no LLM)") + 4:]) if not args.retrieval_only else "")
    print(f"Wrote {folder.relative_to(REPO)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
