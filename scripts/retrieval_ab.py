#!/usr/bin/env python3
"""Retrieval A/B: replay the queries of stored runs under several ranking settings.

No LLM is called. The decomposer queries come from an earlier E7 run
(runs.jsonl), so every setting ranks exactly the same queries, and the only
difference is how the candidates are ranked:

  fused            vector search per query + reciprocal rank fusion (the old behaviour)
  fused+idea       the same, plus the idea text itself as one more query
  reranked         fused pool of 30 papers re-ordered by the cross-encoder
  reranked+idea    both

For each idea's expected papers (eval/prompt_ab/ideas.json) it reports whether
they reach the judge (top 6), whether they are in the pool at all (top 30), and
the mean reciprocal rank. Recall@6 is what the user sees; recall@30 is the
ceiling any reranking of this pool can reach.

Usage (stack running with the reranker):

    python scripts/retrieval_ab.py                         # latest *-prompt-ab results
    python scripts/retrieval_ab.py --runs eval/results/20261015-000708-prompt-ab/runs.jsonl

Results: eval/results/<timestamp>-retrieval-ab/ (summary.md, rows.csv).
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_test import _dotenv, http  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
METHODS = {
    "fused": {"rerank": False, "idea_as_query": False},
    "fused+idea": {"rerank": False, "idea_as_query": True},
    "reranked": {"rerank": True, "idea_as_query": False},
    "reranked+idea": {"rerank": True, "idea_as_query": True},
}
TOP = 6


def query_sets(runs_jsonl: Path) -> list[dict]:
    """Distinct (idea, queries) pairs from stored runs, with the decomposer version that wrote them."""
    seen: set[tuple] = set()
    out = []
    for line in runs_jsonl.read_text(encoding="utf-8-sig").splitlines():
        rec = json.loads(line)
        retrieved = (rec.get("steps") or {}).get("retrieve") or {}
        queries = retrieved.get("queries")
        if not queries or not rec.get("result"):
            continue
        idea = rec["row"]["idea"]
        key = (idea, tuple(queries))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "idea": idea,
            "queries": queries,
            "p1": rec["result"].get("prompt_versions", {}).get("P1-decomposer"),
            "as_run": [c["paper_id"] for c in retrieved.get("candidates", [])],  # what the judge actually saw
        })
    return out


def expected_ranks(pool: list[dict], expected: list[str]) -> dict[str, int | None]:
    """1-based rank of each expected paper in the ranked pool, None if absent."""
    order = [p["paper_id"] for p in pool]
    return {pid: order.index(pid) + 1 if pid in order else None for pid in expected}


def aggregate(rows: list[dict], method: str) -> dict:
    rs = [r for r in rows if r["method"] == method]
    ranks = [rank for r in rs for rank in r["ranks"].values()]
    n = len(ranks)
    return {
        "method": method,
        "query_sets": len(rs),
        "expected": n,
        "at_6": sum(1 for x in ranks if x is not None and x <= TOP),
        "in_pool": sum(1 for x in ranks if x is not None),
        "mrr": round(sum(1 / x for x in ranks if x) / n, 3) if n else None,
        "seconds": round(sum(r["seconds"] for r in rs) / len(rs), 1) if rs else None,
        "not_reranked": sum(1 for r in rs if METHODS[method]["rerank"] and r["ranking_method"] != "reranked"),
    }


def as_run_at6(sets: list[dict], ideas: dict) -> tuple[int, int]:
    """Expected papers among the candidates the judge really saw in the stored runs (20 chunks per query, no reranker)."""
    found = total = 0
    for qs in sets:
        expected = ideas[qs["idea"]]["expected"]
        total += len(expected)
        found += len(set(expected) & set(qs["as_run"][:TOP]))
    return found, total


def summarize(rows: list[dict], ideas: list[str], as_run: tuple[int, int] | None = None) -> str:
    lines = [
        "| Method | Expected papers reaching the judge (top 6) | In the pool (top 30) | MRR | Seconds per ranking | Rerank did not run |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    if as_run:
        lines.append(f"| as run in E7 (20 chunks per query, no reranker) | {as_run[0]}/{as_run[1]} | - | - | - | - |")
    for m in METHODS:
        a = aggregate(rows, m)
        if not a["query_sets"]:
            continue
        lines.append(
            f"| {m} | {a['at_6']}/{a['expected']} | {a['in_pool']}/{a['expected']} | {a['mrr']} | {a['seconds']} | {a['not_reranked']} |"
        )
    lines += ["", f"Per idea, expected papers in the top {TOP} (summed over the stored query sets):", "",
              "| Idea | " + " | ".join(METHODS) + " |", "| --- |" + " --- |" * len(METHODS)]
    for idea in ideas:
        cells = []
        for m in METHODS:
            rs = [r for r in rows if r["method"] == m and r["idea"] == idea]
            ranks = [x for r in rs for x in r["ranks"].values()]
            cells.append(f"{sum(1 for x in ranks if x and x <= TOP)}/{len(ranks)}" if rs else "-")
        lines.append(f"| {idea} | " + " | ".join(cells) + " |")
    lines += ["", "Every setting ranks the same stored queries; no LLM is called. MRR counts an expected paper outside the pool as 0."]
    return "\n".join(lines)


def latest_prompt_ab() -> Path | None:
    found = sorted((REPO / "eval" / "results").glob("*-prompt-ab/runs.jsonl"))
    return found[-1] if found else None


def main() -> int:
    env = _dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=Path, default=latest_prompt_ab(), help="runs.jsonl of an E7 run")
    parser.add_argument("--ideas-file", type=Path, default=REPO / "eval" / "prompt_ab" / "ideas.json")
    parser.add_argument("--methods", default=",".join(METHODS), help="comma-separated subset of: " + ", ".join(METHODS))
    parser.add_argument("--pp-tools-url", default=f"http://localhost:{env.get('PP_TOOLS_HOST_PORT', '8000')}")
    parser.add_argument("--label", default="retrieval-ab")
    args = parser.parse_args()
    if not args.runs or not args.runs.exists():
        print("no runs.jsonl found; pass --runs eval/results/<...>-prompt-ab/runs.jsonl", file=sys.stderr)
        return 2

    ideas = {i["id"]: i for i in json.loads(args.ideas_file.read_text(encoding="utf-8"))}
    sets = [s for s in query_sets(args.runs) if ideas.get(s["idea"], {}).get("expected")]
    methods = [m for m in args.methods.split(",") if m in METHODS]
    status, health = http("GET", f"{args.pp_tools_url}/health")
    reranker_ok = isinstance(health, dict) and health.get("checks", {}).get("reranker", {}).get("ok")
    print(f"{len(sets)} query sets from {args.runs.relative_to(REPO) if args.runs.is_relative_to(REPO) else args.runs}, "
          f"methods: {', '.join(methods)}; reranker healthy: {bool(reranker_ok)}")
    if any(METHODS[m]["rerank"] for m in methods) and not reranker_ok:
        print("warning: the reranker is not healthy; reranked methods will fall back to fused order (see 'Rerank did not run')")

    rows = []
    for i, qs in enumerate(sets, start=1):
        idea = ideas[qs["idea"]]
        for m in methods:
            started = time.monotonic()
            status, body = http(
                "POST", f"{args.pp_tools_url}/novelty/rank",
                {"idea_text": idea["idea"], "queries": qs["queries"], **METHODS[m]}, timeout=300,
            )
            if status != 200:
                print(f"  [{i}/{len(sets)}] {m}: HTTP {status} {str(body)[:200]}", file=sys.stderr)
                return 1
            ranks = expected_ranks(body["pool"], idea["expected"])
            rows.append({"idea": qs["idea"], "p1": qs["p1"], "method": m, "queries": qs["queries"], "ranks": ranks,
                         "ranking_method": body["ranking_method"], "seconds": round(time.monotonic() - started, 2)})
            shown = ", ".join(f"{pid.removeprefix('arxiv:')}={r or '-'}" for pid, r in ranks.items())
            print(f"  [{i}/{len(sets)}] {qs['idea']:<22} {m:<14} {shown}")

    out = REPO / "eval" / "results" / f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{args.label}"
    out.mkdir(parents=True)
    with (out / "rows.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["idea", "p1", "method", "ranking_method", "seconds", "expected_paper", "rank", "queries"])
        for r in rows:
            for pid, rank in r["ranks"].items():
                writer.writerow([r["idea"], r["p1"], r["method"], r["ranking_method"], r["seconds"], pid, rank or "", " | ".join(r["queries"])])
    table = summarize(rows, list(dict.fromkeys(s["idea"] for s in sets)), as_run_at6(sets, ideas))
    meta = {"runs": str(args.runs.name if not args.runs.is_relative_to(REPO) else args.runs.relative_to(REPO)),
            "query_sets": len(sets), "methods": methods, "top": TOP, "health": health}
    (out / "summary.md").write_text(f"# Retrieval A/B\n\n```json\n{json.dumps(meta, indent=2)}\n```\n\n{table}\n", encoding="utf-8")
    print("\n" + table + f"\n\nWrote {out.relative_to(REPO)}/ (summary.md, rows.csv)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
