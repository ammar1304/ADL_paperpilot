#!/usr/bin/env python3
"""Offline replay of candidate-ordering rules on stored pools (no stack, no LLM).

Every stored pool carries each paper's fused rank, rerank rank and rerank score,
so any rule that only uses those three can be scored without re-running
retrieval. Three pool sources, each with its own gold:

  e8   eval/results/*-retrieval-ab/pools.jsonl, pool setting "idea" (the default);
       gold = the idea's expected papers (eval/prompt_ab/ideas.json)
  e7   the first-pass retrieve step of every *-agent-off run;
       gold = the idea's expected papers
  e2   eval/results/*-e2-retrieval/raw.jsonl; the query paper is removed;
       gold = its cited library papers (eval/e2/queries.json)

Rules (all choose TOP=6 candidates from the same pool of 30):

  fused       fused rank only
  reranked    rerank score only
  hybrid60    RRF of fused rank and rerank rank, k=60 (current default, D28)
  hybridK     the same with a smaller k, which lets one strong rank count more
  guardT      hybrid60, but any paper the reranker scores >= T is guaranteed
              a slot (highest score first), the rest filled in hybrid order

Usage:
    python scripts/order_replay.py
    python scripts/order_replay.py --e8 path/pools.jsonl --e7 path/runs.jsonl --e2 path/raw.jsonl
"""

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOP = 6


def _hybrid(pool: list[dict], k: int) -> list[dict]:
    return sorted(pool, key=lambda p: (-(1 / (k + p["fused_rank"]) + 1 / (k + p["rerank_rank"])), p["rerank_rank"]))


def order(pool: list[dict], rule: str) -> list[str]:
    """Paper ids in the order the rule puts them; the first TOP reach the judge."""
    if rule == "fused":
        ranked = sorted(pool, key=lambda p: p["fused_rank"])
    elif rule == "reranked":
        ranked = sorted(pool, key=lambda p: p["rerank_rank"])
    elif rule.startswith("hybrid"):
        ranked = _hybrid(pool, int(rule[len("hybrid"):]))
    elif rule.startswith("guard"):
        floor = float(rule[len("guard"):])
        strong = sorted((p for p in pool if (p["rerank_score"] or 0) >= floor), key=lambda p: -p["rerank_score"])[:TOP]
        ids = {p["paper_id"] for p in strong}
        ranked = strong + [p for p in _hybrid(pool, 60) if p["paper_id"] not in ids]
    else:
        raise ValueError(rule)
    return [p["paper_id"] for p in ranked]


RULES = ["fused", "reranked", "hybrid60", "hybrid20", "hybrid10", "guard0.5", "guard0.7"]


def latest(pattern: str) -> Path | None:
    found = sorted((REPO / "eval" / "results").glob(pattern))
    return found[-1] if found else None


def load_cases(e8: Path | None, e7: Path | None, e2: Path | None) -> dict[str, list[dict]]:
    """{source: [{"pool": [...], "gold": set, "label": str}]}"""
    ideas = json.loads((REPO / "eval" / "prompt_ab" / "ideas.json").read_text(encoding="utf-8"))
    expected = {i["id"]: set(i["expected"]) for i in (ideas if isinstance(ideas, list) else ideas["ideas"])}
    cases: dict[str, list[dict]] = {}
    if e8:
        cases["e8"] = [
            {"pool": r["pool"], "gold": expected[r["idea"]], "label": r["idea"]}
            for r in map(json.loads, e8.read_text(encoding="utf-8-sig").splitlines())
            if r.get("call") == "idea" and expected.get(r["idea"]) and isinstance(r["pool"], list)
        ]
    if e7:
        cases["e7"] = []
        for r in map(json.loads, e7.read_text(encoding="utf-8-sig").splitlines()):
            ret = (r.get("steps") or {}).get("retrieve") or {}
            gold = expected.get(r["row"]["idea"])
            if gold and ret.get("pool"):
                cases["e7"].append({"pool": ret["pool"], "gold": gold, "label": f"{r['row']['idea']}#{r['row']['repeat']}"})
    if e2:
        cited = {q["paper_id"]: set(q["cited"]) for q in json.loads((REPO / "eval" / "e2" / "queries.json").read_text(encoding="utf-8"))["queries"]}
        cases["e2"] = []
        for r in map(json.loads, e2.read_text(encoding="utf-8-sig").splitlines()):
            pool = [p for p in r["pool"] if p["paper_id"] != r["paper_id"]]  # leave the paper itself out
            if r["paper_id"] in cited and pool and all(p.get("rerank_rank") for p in pool):
                cases["e2"].append({"pool": pool, "gold": cited[r["paper_id"]] - {r["paper_id"]}, "label": r["paper_id"]})
    return cases


def score(cases: list[dict], rule: str) -> dict:
    found = total = 0
    rr = 0.0
    for c in cases:
        ids = order(c["pool"], rule)
        found += len(set(ids[:TOP]) & c["gold"])
        total += len(c["gold"])
        rr += sum(1 / (ids.index(g) + 1) for g in c["gold"] if g in ids)
    return {"found": found, "total": total, "mrr": rr / total if total else 0.0}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--e8", type=Path, default=None)
    ap.add_argument("--e7", type=Path, default=None)
    ap.add_argument("--e2", type=Path, default=None)
    args = ap.parse_args()
    e8 = args.e8 or (d / "pools.jsonl" if (d := latest("*-retrieval-ab")) else None)
    e7 = args.e7 or (d / "runs.jsonl" if (d := latest("*-agent-off")) else None)
    e2 = args.e2 or (d / "raw.jsonl" if (d := latest("*-e2-retrieval")) else None)
    cases = load_cases(e8, e7, e2)
    print(f"sources: e8={e8}, e7={e7}, e2={e2}\n")
    head = "| Rule | " + " | ".join(f"{s} ({len(c)} pools) top {TOP} | {s} MRR" for s, c in cases.items()) + " |"
    print(head)
    print("|" + " --- |" * (1 + 2 * len(cases)))
    for rule in RULES:
        cells = []
        for c in cases.values():
            s = score(c, rule)
            cells += [f"{s['found']}/{s['total']}", f"{s['mrr']:.3f}"]
        print(f"| {rule} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
