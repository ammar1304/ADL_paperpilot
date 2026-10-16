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

    python scripts/e4_figure_check.py                  # all figures, the workflow's settings
    python scripts/e4_figure_check.py --variants A,B,C # compare request settings (app/audit/figures.py VARIANTS)
    python scripts/e4_figure_check.py --limit 1        # one figure: a probe of the request format
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
    """(status, answer, headers); status 0 for a network failure or a timeout (first E4 run:
    one call hung for over 120 s and the uncaught timeout ended the run)."""
    req = urllib.request.Request(GROQ, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "PaperPilot/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw), dict(exc.headers)
        except ValueError:
            return exc.code, raw, dict(exc.headers)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"network: {type(exc).__name__}: {exc}", {}


def run_pass(figures: list[dict], prompt, key: str, cache: Path, variant: str, out: Path, attempts: int = 1,
             wait_daily: bool = False) -> dict:
    """One pass over the gold figures with one request variant; writes its files to out/."""
    out.mkdir(parents=True)
    rows, calls = [], []
    checks_file, calls_file = (out / "checks.jsonl").open("a", encoding="utf-8"), (out / "calls.jsonl").open("a", encoding="utf-8")

    def keep_call(c: dict) -> None:  # written as it happens, so a crash loses nothing
        calls.append(c)
        calls_file.write(json.dumps(c, ensure_ascii=False) + "\n")
        calls_file.flush()
    for n, item in enumerate(figures):
        folder = cache / "audit" / item["sha256"]
        image = folder / "figures" / (item["figure"].replace(":", "_") + ".png")
        if not image.exists():
            print(f"{item['paper']} {item['figure']}: no image at {image}, skipped")
            continue
        paper = json.loads((folder / "paper.json").read_text(encoding="utf-8"))
        rendered = prompt.render({"title": paper["title"], "figure_id": item["figure"].replace("figure:", "Figure "),
                                  "caption": fig.figure_caption(paper, item["figure"]),
                                  "claims_block": fig.claims_block(item["claims"])})
        body = fig.figure_request(rendered.system, rendered.user, image.read_bytes(), variant=variant)
        stop, checks, used = False, None, 0
        for attempt in range(1, attempts + 1):  # like the workflow: a failed or empty answer is served again
            if n or attempt > 1:
                time.sleep(fig.FIGURE_PACE_S)
            used = attempt
            for _ in range(40 if wait_daily else 4):
                started = time.monotonic()
                status, answer, headers = call_groq(body, key)
                secs = round(time.monotonic() - started, 1)
                if status == 0:
                    print(f"  {answer}; retrying in 30 s")
                    time.sleep(30)
                    continue
                if status != 429:
                    break
                wait = float(headers.get("retry-after") or headers.get("Retry-After") or 60)
                if wait > 900 and not wait_daily:
                    print(f"Groq daily limit for {fig.FIGURE_MODEL}: retry in {wait:.0f} s. Stopping; results so far are saved.")
                    stop = True
                    break
                if wait > 900:  # --wait-daily: Groq frees tokens over a rolling 24 hours; wait for them
                    print(f"  daily limit: waiting {wait / 60:.0f} min ({time.strftime('%H:%M')})", flush=True)
                print(f"  429, waiting {wait:.0f} s")
                time.sleep(wait + 1)
            if status != 200 or not isinstance(answer, dict):
                print(f"{item['paper']} {item['figure']} attempt {attempt}: HTTP {status}: {str(answer)[:200]}")
                keep_call({"paper": item["paper"], "figure": item["figure"], "attempt": attempt, "status": status, "error": str(answer)[:2000]})
                if stop:
                    break
                continue
            content = answer["choices"][0]["message"].get("content") or ""
            usage = answer.get("usage", {})
            try:
                description, checks = fig.validate(content, list(range(len(item["claims"]))))
            except ValueError as exc:
                print(f"{item['paper']} {item['figure']} attempt {attempt}: invalid output ({exc}): {content[:200]}")
                keep_call({"paper": item["paper"], "figure": item["figure"], "attempt": attempt, "status": status, "error": f"invalid: {exc}",
                           "content": content[:2000], "usage": usage})
                checks = None
                continue
            keep_call({"paper": item["paper"], "figure": item["figure"], "attempt": attempt, "status": status, "seconds": secs,
                       "usage": usage, "description": description})
            print(f"{item['paper']} {item['figure']} (attempt {attempt}, {secs} s, {usage.get('completion_tokens')} output tokens): {description[:120]}")
            break
        if stop and checks is None:
            break
        if checks is None:
            continue
        for c, check in zip(item["claims"], checks):
            ok = check["verdict"] == c["expected"]
            row = {"paper": item["paper"], "figure": item["figure"], "claim": c["claim"], "expected": c["expected"],
                   "got": check["verdict"], "correct": ok, "evidence": check["evidence"], "read_values": check["read_values"]}
            rows.append(row)
            checks_file.write(json.dumps(row, ensure_ascii=False) + "\n")
            checks_file.flush()
            print(f"   {'ok ' if ok else 'XX '} expected {c['expected']:12} got {check['verdict']:12} {c['claim'][:90]}")

    checks_file.close()
    calls_file.close()
    reasoning, mode = fig.VARIANTS[variant]
    answered = [c for c in calls if c.get("description") is not None]
    figures_tried = len({(c["paper"], c["figure"]) for c in calls})
    first_try = sum(1 for c in answered if c.get("attempt", 1) == 1)
    tokens = [c["usage"].get("completion_tokens", 0) for c in answered if c.get("usage")]
    true_claims = [r for r in rows if r["expected"] == "supported"]
    false_claims = [r for r in rows if r["expected"] == "contradicted"]
    stats = {"variant": variant, "reasoning": reasoning, "mode": mode, "calls": len(calls), "answered": len(answered),
             "figures": figures_tried, "first_try": first_try,
             "claims": len(rows), "correct": sum(r["correct"] for r in rows),
             "false_alarms": sum(r["got"] == "contradicted" for r in true_claims), "true_claims": len(true_claims),
             "caught": sum(r["got"] == "contradicted" for r in false_claims), "accepted": sum(r["got"] == "supported" for r in false_claims),
             "false_claims": len(false_claims), "output_tokens_mean": round(sum(tokens) / len(tokens)) if tokens else None}
    lines = [f"# E4 figure check, variant {variant} (reasoning {reasoning}, {mode}), up to {attempts} attempt(s) per figure: "
             f"{len(answered)}/{figures_tried} figures answered ({first_try} at the first attempt), {len(calls)} calls, "
             f"{len(rows)} claims, {fig.FIGURE_MODEL}, P5 {prompt.version}", "",
             "| Expected \\ got | " + " | ".join(LABELS + ("not_answered",)) + " |", "| --- |" + " --- |" * 4]
    for e in LABELS:
        n = Counter(r["got"] for r in rows if r["expected"] == e)
        lines.append(f"| {e} | " + " | ".join(str(n[g]) for g in LABELS + ("not_answered",)) + " |")
    if rows:
        lines += ["", f"Accuracy: {stats['correct']}/{len(rows)}",
                  f"False alarms (a true claim called contradicted): {stats['false_alarms']}/{stats['true_claims']}",
                  f"Errors caught: {stats['caught']}/{stats['false_claims']}; accepted as supported: {stats['accepted']}",
                  f"Output tokens per answered call (mean): {stats['output_tokens_mean']}",
                  "", "Wrong verdicts:"] + [f"- {r['paper']} {r['figure']}: expected {r['expected']}, got {r['got']}: {r['claim']} | model: {r['evidence'][:200]}"
                                            for r in rows if not r["correct"]]
    lines += ["", "Failed calls:"] + [f"- {c['paper']} {c['figure']}: HTTP {c['status']} {c['error'][:200]}" for c in calls if c.get("error")]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines) + f"\n\nWrote {out.relative_to(REPO)}/\n")
    return stats


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", type=Path, default=REPO / "eval" / "e4" / "figure_claims.json")
    ap.add_argument("--cache", type=Path, default=REPO / ".cache" / "pp-tools")
    ap.add_argument("--limit", type=int, default=0, help="only the first N figures")
    ap.add_argument("--variants", default=fig.FIGURE_VARIANT, help="comma-separated request variants, e.g. A,B,C")
    ap.add_argument("--attempts", type=int, default=3, help="attempts per figure, as in the workflow (1 = first-attempt reliability)")
    ap.add_argument("--wait-daily", action="store_true",
                    help="on the daily token limit, wait for Groq to free tokens instead of stopping (for an unattended run)")
    ap.add_argument("--label", default="e4-figures")
    args = ap.parse_args()
    key = dotenv().get("GROQ_API_KEY")
    if not key:
        print("GROQ_API_KEY is empty in .env")
        return 2
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    figures = gold["figures"][: args.limit or None]
    prompt = PromptLibrary(REPO / "prompts").load("P5-figure-checker")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    results = []
    for variant in [v.strip() for v in args.variants.split(",") if v.strip()]:
        print(f"===== variant {variant}: reasoning {fig.VARIANTS[variant][0]}, {fig.VARIANTS[variant][1]}")
        results.append(run_pass(figures, prompt, key, args.cache, variant, REPO / "eval" / "results" / f"{stamp}-{args.label}-{variant}",
                                args.attempts, args.wait_daily))
        time.sleep(fig.FIGURE_PACE_S)
    if len(results) > 1:
        lines = ["| Variant | Reasoning | Format | Calls answered | Correct verdicts | False alarms | Errors caught | Errors accepted | Output tokens (mean) |",
                 "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for r in results:
            lines.append(f"| {r['variant']} | {r['reasoning']} | {r['mode']} | {r['answered']}/{r['calls']} | {r['correct']}/{r['claims']} | "
                         f"{r['false_alarms']}/{r['true_claims']} | {r['caught']}/{r['false_claims']} | {r['accepted']} | {r['output_tokens_mean']} |")
        comp = REPO / "eval" / "results" / f"{stamp}-{args.label}-comparison.md"
        comp.write_text(f"# E4 request variants (P5 {prompt.version})\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines) + f"\n\nWrote {comp.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
