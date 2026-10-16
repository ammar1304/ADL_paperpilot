#!/usr/bin/env python3
"""E3: does the number check catch wrong numbers, and does it leave correct ones alone?

Offline and free: no stack, no LLM. Reads the claims of an audit run
(eval/results/*-audit-claims/<paper>.json) and the parsed papers
(<cache>/audit/<sha256>/paper.json), then:

  part A, real claims  every number check the extracted claims produce, as they are
                       (all published numbers, so any "contradicted" is a false alarm),
                       then the same claims with one planted error each
  part B, synthetic    claims written from every table cell, with the table's own
                       labels: a true value and planted errors, plus margins between
                       two rows of the same column. The labels are the table's, so
                       locating the cell is easier than for real text: an upper bound.

Planted errors (seeded, reproducible):
  digit      the last digit changed by 2 to 5 units (7.0 -> 7.3)
  transpose  two adjacent digits swapped (59.8 -> 58.9)
  inflate    the number made 10-30% larger (an exaggerated result)

Why mutate the claim and not the PDF: the extraction guardrail only keeps
numbers written literally in the sentence (D33), so a typo in the text reaches
the check unchanged; planting it in the claim measures the same thing.

    python scripts/e3_number_check.py
    python scripts/e3_number_check.py --run eval/results/<ts>-audit-claims --cache .cache/pp-tools
"""

import argparse
import json
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "services" / "pp_tools"))
from app.audit.numbers import LOWER_BETTER, check_claims, claimed_number, index_paper  # noqa: E402

SEED = 13
KINDS = ("digit", "transpose", "inflate")


def mutate(text: str, kind: str, rng: random.Random) -> str | None:
    """The claimed number with one planted error, written the same way; None if impossible."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    if not m:
        return None
    raw = m.group(0).replace(",", "")
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    value = float(raw)
    unit = 10 ** -decimals
    if kind == "digit":
        new = value + rng.choice([-1, 1]) * rng.randint(2, 5) * unit
    elif kind == "inflate":
        new = value * rng.uniform(1.1, 1.3)
        if round(new, decimals) - value < 2 * unit:
            new = value + 2 * unit
    else:
        digits = [i for i, ch in enumerate(raw) if ch.isdigit()]
        swaps = [(a, b) for a, b in zip(digits, digits[1:]) if raw[a] != raw[b] and b == a + 1]
        if not swaps:
            return None
        a, b = rng.choice(swaps)
        swapped = raw[:a] + raw[b] + raw[a] + raw[b + 1:]
        if swapped.startswith("0") and not swapped.startswith("0."):
            return None
        new = float(swapped)
    if new < 0 or abs(new - value) < unit / 2:
        return None
    return text[:m.start()] + f"{new:.{decimals}f}" + text[m.end():]


def planted_claims(claim: dict, kind: str, rng: random.Random) -> list[tuple[dict, str]]:
    """One copy of the claim per checkable number, with that number wrong."""
    out = []
    comp = claim.get("comparison") or {}
    if comp.get("difference"):
        new = mutate(comp["difference"], kind, rng)
        if new:
            values = [dict(v, value=new) if v.get("role") == "difference" else v for v in claim.get("values") or []]
            out.append((dict(claim, comparison=dict(comp, difference=new), values=values), f"difference {comp['difference']} -> {new}"))
    for i, v in enumerate(claim.get("values") or []):
        if v.get("role") == "difference" or not v.get("system"):
            continue
        new = mutate(v["value"], kind, rng)
        if new:
            values = [dict(x, value=new) if j == i else x for j, x in enumerate(claim["values"])]
            out.append((dict(claim, values=values), f"value {v['value']} -> {new}"))
    return out


def synthetic_claims(paper: dict, rng: random.Random, per_table: int = 25) -> list[dict]:
    """Claims written from table cells with the table's labels: values and margins."""
    claims = []
    for table_id, cells in index_paper(paper).items():
        usable = [c for c in cells if c.row_ctx and c.col_ctx]
        rng.shuffle(usable)
        for c in usable[:per_table]:
            claims.append({
                "sentence_id": f"synthetic/{table_id}", "type": "result", "refers_to": [table_id],
                "claim": f"{' '.join(c.row_ctx)} reaches {c.text.strip()} on {' '.join(c.col_ctx)} {c.section}".strip(),
                "values": [{"value": c.text.strip(), "metric": c.col_ctx[-1], "dataset": c.col_ctx[0],
                            "system": c.row_ctx[-1], "setting": c.section, "role": "value"}],
                "comparison": None, "cites": [],
            })
        by_col: dict[tuple, list] = {}
        for c in usable:
            by_col.setdefault((c.block, c.col, c.section), []).append(c)
        # "a beats b": a has the higher value, or the lower one in a time or step column
        pairs = [(a, b) for group in by_col.values() for a in group for b in group
                 if a.row != b.row and a.row_ctx[-1] != b.row_ctx[-1]
                 and (a.value < b.value if LOWER_BETTER.search(" ".join(a.col_ctx)) else a.value > b.value)]
        rng.shuffle(pairs)
        for a, b in pairs[:per_table // 2]:
            diff = f"{abs(a.value - b.value):.{max(a.decimals, b.decimals)}f}"
            claims.append({
                "sentence_id": f"synthetic/{table_id}", "type": "comparison", "refers_to": [table_id],
                "claim": f"{' '.join(a.row_ctx)} beats {' '.join(b.row_ctx)} by {diff} on {' '.join(a.col_ctx)} {a.section}".strip(),
                "values": [], "cites": [],
                "comparison": {"system": a.row_ctx[-1], "baseline": b.row_ctx[-1], "difference": diff,
                               "kind": "absolute", "direction": "better"},
            })
    return claims


def evaluate(paper: dict, claims: list[dict], rng: random.Random, label: str) -> list[dict]:
    rows = []
    original = check_claims(paper, claims)["checks"]
    for c in original:
        rows.append({"part": label, "planted": None, "verdict": c["verdict"], "kind": c["kind"], "claimed": c["claimed"],
                     "note": c.get("note", "")})
    checkable = {c["claim"] for c in original if c["verdict"] == "supported"}
    for kind in KINDS:
        for i in sorted(checkable):
            for wrong, what in planted_claims(claims[i], kind, rng):
                for c in check_claims(paper, [wrong])["checks"]:
                    if claimed_number(c["claimed"]) and c["claimed"] in what.split(" -> ")[1]:
                        rows.append({"part": label, "planted": kind, "verdict": c["verdict"], "kind": c["kind"],
                                     "claimed": c["claimed"], "change": what, "note": c.get("note", "")})
    return rows


def table(rows: list[dict], part: str) -> list[str]:
    lines = [f"| {part} | Checks | Supported | Contradicted (caught) | Unverifiable |", "| --- | --- | --- | --- | --- |"]
    for planted in (None, *KINDS):
        sel = [r for r in rows if r["part"] == part and r["planted"] == planted]
        if not sel:
            continue
        n = Counter(r["verdict"] for r in sel)
        name = "original (no error)" if planted is None else f"planted: {planted}"
        lines.append(f"| {name} | {len(sel)} | {n['supported']} | {n['contradicted']} | {n['unverifiable']} |")
    return lines


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path, default=None, help="an *-audit-claims results folder (default: the latest)")
    ap.add_argument("--cache", type=Path, default=REPO / ".cache" / "pp-tools", help="pp-tools cache folder")
    ap.add_argument("--label", default="e3-numbers")
    args = ap.parse_args()
    run = args.run or sorted((REPO / "eval" / "results").glob("*-audit-claims"))[-1]
    rng = random.Random(SEED)
    rows = []
    for f in sorted(run.glob("*.json")):
        result = json.loads(f.read_text(encoding="utf-8-sig"))["result"]
        paper_file = args.cache / "audit" / result["paper"]["sha256"] / "paper.json"
        if not paper_file.exists():
            print(f"{f.stem}: {paper_file} not found, skipped")
            continue
        paper = json.loads(paper_file.read_text(encoding="utf-8"))
        part_a = evaluate(paper, result["claims"], rng, "A real claims")
        part_b = evaluate(paper, synthetic_claims(paper, rng), rng, "B synthetic")
        for r in part_a + part_b:
            r["paper"] = f.stem
        rows += part_a + part_b
        print(f"{f.stem}: {len(part_a)} real checks, {len(part_b)} synthetic checks")

    out = REPO / "eval" / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.label}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "checks.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    lines = [f"# E3 number check (claims from {run.name}, seed {SEED})", ""]
    for part in ("A real claims", "B synthetic"):
        lines += table(rows, part) + [""]
    alarms = [r for r in rows if r["planted"] is None and r["verdict"] == "contradicted"]
    lines.append(f"False alarms on original numbers: {len(alarms)}")
    for r in alarms[:20]:
        lines.append(f"- {r['paper']} {r['kind']} {r['claimed']}: {r['note']}")
    missed = [r for r in rows if r["planted"] and r["verdict"] == "supported"]
    lines.append(f"\nPlanted errors accepted as supported: {len(missed)}")
    for r in missed[:20]:
        lines.append(f"- {r['paper']} {r['part']} {r['change']}: {r['note']}")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nWrote {out.relative_to(REPO)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
