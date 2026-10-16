#!/usr/bin/env python3
"""E4 (figures): does the vision check judge claims about figures correctly?

A gold set of claims about 14 figures of 5 papers (eval/e4/figure_claims.json),
written by looking at each figure: true claims, false claims (a reversed trend, a
wrong order, a wrong value) and claims the figure cannot answer. Each figure goes
to Groq once, exactly as the 30-audit workflow sends it: same prompt (rendered by
pp-tools' library), same request builder, same validation (app/audit/figures.py).

Needs GROQ_API_KEY in .env and the parsed papers in the pp-tools cache
(.cache/pp-tools/audit/<sha256>/), which the audit runs leave behind. Costs
about 4,500 tokens per figure (the image is a flat 2,048), about 65K in all, of
the vision model's 200K a day; calls are 45 s apart.

    python scripts/e4_figure_check.py            # all figures
    python scripts/e4_figure_check.py --limit 1  # one figure: a probe of the request format
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "services" / "pp_tools"))
from app.audit import figures as fig  # noqa: E402
from app.prompts import PromptLibrary  # noqa: E402

GROQ = "https://api.groq.com/openai/v1/chat/completions"
LABELS = ("supported", "contradicted", "not_shown")


def dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def call_groq(body: dict, key: str) -> tuple[int, dict | str, dict]:
    req = urllib.request.Request(GROQ, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "PaperPilot/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw), dict(exc.headers)
        except ValueError:
            return exc.code, raw, dict(exc.headers)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", type=Path, default=REPO / "eval" / "e4" / "figure_claims.json")
    ap.add_argument("--cache", type=Path, default=REPO / ".cache" / "pp-tools")
    ap.add_argument("--limit", type=int, default=0, help="only the first N figures")
    ap.add_argument("--label", default="e4-figures")
    args = ap.parse_args()
    key = dotenv().get("GROQ_API_KEY")
    if not key:
        print("GROQ_API_KEY is empty in .env")
        return 2
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    figures = gold["figures"][: args.limit or None]
    prompt = PromptLibrary(REPO / "prompts").load("P5-figure-checker")
    out = REPO / "eval" / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.label}"
    out.mkdir(parents=True)
    rows, calls = [], []
    for n, item in enumerate(figures):
        folder = args.cache / "audit" / item["sha256"]
        image = folder / "figures" / (item["figure"].replace(":", "_") + ".png")
        if not image.exists():
            print(f"{item['paper']} {item['figure']}: no image at {image}, skipped")
            continue
        paper = json.loads((folder / "paper.json").read_text(encoding="utf-8"))
        rendered = prompt.render({"title": paper["title"], "figure_id": item["figure"].replace("figure:", "Figure "),
                                  "caption": fig.figure_caption(paper, item["figure"]),
                                  "claims_block": fig.claims_block(item["claims"])})
        body = fig.figure_request(rendered.system, rendered.user, image.read_bytes())
        if n:
            time.sleep(fig.FIGURE_PACE_S)
        for attempt in range(4):
            started = time.monotonic()
            status, answer, headers = call_groq(body, key)
            secs = round(time.monotonic() - started, 1)
            if status != 429:
                break
            wait = float(headers.get("retry-after") or headers.get("Retry-After") or 60)
            if wait > 900:
                print(f"Groq daily limit for {fig.FIGURE_MODEL}: retry in {wait:.0f} s. Stopping; results so far are saved.")
                break
            print(f"  429, waiting {wait:.0f} s")
            time.sleep(wait + 1)
        if status != 200 or not isinstance(answer, dict):
            print(f"{item['paper']} {item['figure']}: HTTP {status}: {str(answer)[:600]}")
            calls.append({"paper": item["paper"], "figure": item["figure"], "status": status, "error": str(answer)[:2000]})
            if status == 429:
                break
            continue
        content = answer["choices"][0]["message"].get("content") or ""
        usage = answer.get("usage", {})
        try:
            description, checks = fig.validate(content, list(range(len(item["claims"]))))
        except ValueError as exc:
            print(f"{item['paper']} {item['figure']}: invalid output ({exc}): {content[:300]}")
            calls.append({"paper": item["paper"], "figure": item["figure"], "status": status, "error": f"invalid: {exc}", "content": content[:2000]})
            continue
        calls.append({"paper": item["paper"], "figure": item["figure"], "status": status, "seconds": secs, "usage": usage,
                      "description": description})
        print(f"{item['paper']} {item['figure']} ({secs} s, {usage.get('total_tokens')} tokens): {description[:160]}")
        for c, check in zip(item["claims"], checks):
            ok = check["verdict"] == c["expected"]
            rows.append({"paper": item["paper"], "figure": item["figure"], "claim": c["claim"], "expected": c["expected"],
                         "got": check["verdict"], "correct": ok, "evidence": check["evidence"], "read_values": check["read_values"]})
            print(f"   {'ok ' if ok else 'XX '} expected {c['expected']:12} got {check['verdict']:12} {c['claim'][:90]}")

    (out / "checks.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (out / "calls.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in calls), encoding="utf-8")
    lines = [f"# E4 figure check ({len(calls)} calls, {len(rows)} claims, {fig.FIGURE_MODEL}, P5 {prompt.version})", "",
             "| Expected \\ got | " + " | ".join(LABELS + ("not_answered",)) + " |", "| --- |" + " --- |" * 4]
    for e in LABELS:
        n = Counter(r["got"] for r in rows if r["expected"] == e)
        lines.append(f"| {e} | " + " | ".join(str(n[g]) for g in LABELS + ("not_answered",)) + " |")
    if rows:
        acc = sum(r["correct"] for r in rows) / len(rows)
        true_claims = [r for r in rows if r["expected"] == "supported"]
        false_claims = [r for r in rows if r["expected"] == "contradicted"]
        said_wrong = [r for r in rows if r["got"] == "contradicted"]
        lines += ["", f"Accuracy: {acc:.0%} ({sum(r['correct'] for r in rows)}/{len(rows)})",
                  f"False alarms (a true claim called contradicted): {sum(r['got'] == 'contradicted' for r in true_claims)}/{len(true_claims)}",
                  f"Errors caught (a false claim called contradicted): {sum(r['got'] == 'contradicted' for r in false_claims)}/{len(false_claims)}",
                  f"Errors accepted (a false claim called supported): {sum(r['got'] == 'supported' for r in false_claims)}/{len(false_claims)}",
                  f"Precision of 'contradicted': {sum(r['expected'] == 'contradicted' for r in said_wrong)}/{len(said_wrong)}"]
        tokens = [c["usage"].get("total_tokens", 0) for c in calls if c.get("usage")]
        lines.append(f"Tokens per call: {min(tokens)}-{max(tokens)}, total {sum(tokens)}" if tokens else "")
        lines += ["", "Wrong verdicts:"] + [f"- {r['paper']} {r['figure']}: expected {r['expected']}, got {r['got']}: {r['claim']} | model: {r['evidence'][:200]}"
                                            for r in rows if not r["correct"]]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines) + f"\n\nWrote {out.relative_to(REPO)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
