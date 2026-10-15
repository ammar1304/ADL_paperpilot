#!/usr/bin/env python3
"""E2 leave-one-out: does PaperPilot find the papers a real paper builds on?

For papers in the library, the paper's own abstract is used as the "idea", the
paper itself is left out of the results, and the papers it actually cites
(from Semantic Scholar's reference lists, kept only if they are in the library)
are the expected answers. Unlike E7 and E8, nothing here was used to tune the
system, so it checks whether those choices generalise.

Two steps, both cached so the evaluation is reproducible:

  1. build   fetch reference lists from Semantic Scholar (needs SEMANTIC_SCHOLAR_API_KEY
             in .env; about 1 request per second) and read each abstract from the
             library; writes eval/e2/queries.json (commit it)
  2. run     rank with pp-tools and score. Default mode "retrieval": the abstract is
             the only query, no LLM, fused / reranked / hybrid orders compared from
             one call. Mode "full": the abstract goes through the whole pipeline
             (decomposer, search agent, judge) via the n8n webhook.

    python scripts/e2_leave_one_out.py build
    python scripts/e2_leave_one_out.py run
    python scripts/e2_leave_one_out.py run --mode full --limit 10

Metrics per method: cited papers reaching the judge (top 6 after removing the
paper itself), in the pool (top 30), MRR; micro (all cited papers together) and
macro (mean over query papers).
"""

import argparse
import csv
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrieval_ab import order_pool  # noqa: E402
from smoke_test import _dotenv, http  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
QUERIES = REPO / "eval" / "e2" / "queries.json"
CACHE = REPO / "eval" / "e2" / "s2_references.json"
S2 = "https://api.semanticscholar.org/graph/v1"
TOP, POOL = 6, 30
ORDERS = ("fused", "reranked", "hybrid")


# ---- pure parts (unit-tested) ----------------------------------------------------------------


def read_manifest(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def cited_in_library(references: list[dict], library: set[str], self_id: str) -> list[str]:
    """arXiv ids (as paper ids) of the cited papers that are in the library, without the paper itself."""
    out = []
    for ref in references:
        arxiv = ((ref.get("citedPaper") or {}).get("externalIds") or {}).get("ArXiv")
        pid = f"arxiv:{arxiv}" if arxiv else None
        if pid and pid in library and pid != self_id and pid not in out:
            out.append(pid)
    return out


def select_queries(candidates: list[dict], min_cited: int, limit: int, seed: int) -> list[dict]:
    eligible = [c for c in candidates if len(c["cited"]) >= min_cited]
    random.Random(seed).shuffle(eligible)
    return sorted(eligible[:limit], key=lambda c: c["paper_id"])


def ranks_without_self(order: list[str], self_id: str, expected: list[str]) -> dict[str, int | None]:
    ranked = [p for p in order if p != self_id]
    return {pid: ranked.index(pid) + 1 if pid in ranked else None for pid in expected}


def summarize(rows: list[dict], methods: list[str]) -> str:
    lines = [
        f"| Method | Cited papers reaching the judge (top {TOP}), micro | In the pool (top {POOL}) | MRR | Macro recall@{TOP} | Query papers |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for m in methods:
        rs = [r for r in rows if r["method"] == m]
        if not rs:
            continue
        ranks = [x for r in rs for x in r["ranks"].values()]
        n = len(ranks)
        at = sum(1 for x in ranks if x is not None and x <= TOP)
        pool = sum(1 for x in ranks if x is not None and x <= POOL)
        mrr = sum(1 / x for x in ranks if x) / n if n else 0
        macro = sum(sum(1 for x in r["ranks"].values() if x and x <= TOP) / len(r["ranks"]) for r in rs) / len(rs)
        lines.append(f"| {m} | {at}/{n} ({at / n:.0%}) | {pool}/{n} | {mrr:.3f} | {macro:.2f} | {len(rs)} |")
    lines += [
        "",
        "Each query paper's own abstract is the idea; the paper itself is removed from the ranking; the expected",
        "papers are the ones it cites that are in the library (Semantic Scholar reference lists).",
    ]
    return "\n".join(lines)


# ---- Semantic Scholar ------------------------------------------------------------------------


class TransientError(Exception):
    """The network or Semantic Scholar failed; the paper is retried on the next build."""


def s2_references(arxiv_id: str, key: str, retries: int = 5, base: str = S2) -> list[dict] | None:
    """Reference list, or None if Semantic Scholar does not know the paper (404).

    Dropped connections (seen on the team PC: ConnectionResetError after 200 papers),
    timeouts, 429 and 5xx are retried with a growing pause; if they persist,
    TransientError is raised and nothing is cached for that paper.
    """
    url = f"{base}/paper/arXiv:{arxiv_id}/references?" + urllib.parse.urlencode({"fields": "externalIds,title", "limit": 1000})
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"x-api-key": key, "User-Agent": "PaperPilot-E2/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode()).get("data") or []
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in (429, 500, 502, 503, 504):
                raise
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:  # includes ConnectionResetError
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2 * 2**attempt)
    raise TransientError(f"arXiv:{arxiv_id}: {last}")


# ---- commands --------------------------------------------------------------------------------


def build(args, env) -> int:
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or env.get("SEMANTIC_SCHOLAR_API_KEY", "")
    if not key:
        print("SEMANTIC_SCHOLAR_API_KEY is empty in .env; it is needed to fetch reference lists.", file=sys.stderr)
        return 2
    rows = read_manifest(args.manifest)
    library = {f"arxiv:{r['arxiv_id']}" for r in rows}
    cache = json.loads(args.cache.read_text(encoding="utf-8")) if args.cache.exists() else {}
    todo = [r["arxiv_id"] for r in rows if r["arxiv_id"] not in cache]
    print(f"{len(rows)} papers in the manifest; {len(todo)} reference lists to fetch (about {len(todo) * 1.2 / 60:.0f} min)")
    skipped = []
    for i, arxiv_id in enumerate(todo, start=1):
        try:
            refs = s2_references(arxiv_id, key, base=args.s2_url)
        except TransientError as exc:
            skipped.append(arxiv_id)
            print(f"  skipped for now ({exc}); run build again to retry it")
            continue
        cache[arxiv_id] = None if refs is None else [
            {"arxiv": ((r.get("citedPaper") or {}).get("externalIds") or {}).get("ArXiv"), "title": (r.get("citedPaper") or {}).get("title")}
            for r in refs
        ]
        time.sleep(args.s2_interval)
        if i % 25 == 0 or i == len(todo):
            args.cache.parent.mkdir(parents=True, exist_ok=True)
            args.cache.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
            print(f"  {i}/{len(todo)}")
    args.cache.parent.mkdir(parents=True, exist_ok=True)
    args.cache.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
    if skipped:
        print(f"{len(skipped)} reference lists could not be fetched this time; run build again to fetch them "
              "(the query set is still written from the papers fetched so far).")

    candidates = []
    for r in rows:
        refs = cache.get(r["arxiv_id"])
        if not refs:
            continue
        pid = f"arxiv:{r['arxiv_id']}"
        cited = cited_in_library([{"citedPaper": {"externalIds": {"ArXiv": x["arxiv"]}}} for x in refs], library, pid)
        candidates.append({"paper_id": pid, "title": r["title"], "year": int(r["year"]), "cited": cited})
    chosen = select_queries(candidates, args.min_cited, args.limit, args.seed)

    pp = args.pp_tools_url
    for q in chosen:
        status, body = http("GET", f"{pp}/chunks/{urllib.parse.quote(q['paper_id'] + '#abs', safe=':')}")
        if status != 200:
            status, body = http("GET", f"{pp}/chunks/{urllib.parse.quote(q['paper_id'] + '#abs/p1', safe=':')}")
        if status != 200:
            print(f"  no abstract in the library for {q['paper_id']}: {body}", file=sys.stderr)
            return 1
        text = body["text"]
        q["abstract"] = text.split("Abstract.\n", 1)[-1].strip()
    with_refs = sum(1 for c in candidates if c["cited"])
    meta = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "papers_with_reference_lists": len(candidates),
        "papers_citing_library_papers": with_refs,
        "min_cited": args.min_cited,
        "seed": args.seed,
        "queries": len(chosen),
        "cited_total": sum(len(q["cited"]) for q in chosen),
    }
    args.queries.parent.mkdir(parents=True, exist_ok=True)
    args.queries.write_text(json.dumps({"meta": meta, "queries": chosen}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(meta, indent=2))
    print(f"Wrote {args.queries} (commit it) and {args.cache}")
    return 0


def run(args, env) -> int:
    data = json.loads(args.queries.read_text(encoding="utf-8"))
    queries = data["queries"][: args.limit] if args.limit else data["queries"]
    out = REPO / "eval" / "results" / f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-e2-{args.mode}{'-' + args.label if args.label else ''}"
    out.mkdir(parents=True)
    rows: list[dict] = []
    with (out / "raw.jsonl").open("w", encoding="utf-8") as raw:
        for i, q in enumerate(queries, start=1):
            if args.mode == "retrieval":
                status, body = http("POST", f"{args.pp_tools_url}/novelty/rank", {
                    "idea_text": q["abstract"][:4000], "queries": [q["abstract"][:300]],
                    "rerank": True, "idea_as_query": True, "order": "rerank",
                }, timeout=300)
                if status != 200:
                    print(f"  [{i}] HTTP {status}: {str(body)[:200]}", file=sys.stderr)
                    return 1
                raw.write(json.dumps({"paper_id": q["paper_id"], "pool": body["pool"]}) + "\n")
                for order in ORDERS:
                    ids = [p["paper_id"] for p in order_pool(body["pool"], order)]
                    rows.append({"paper_id": q["paper_id"], "method": order, "ranks": ranks_without_self(ids, q["paper_id"], q["cited"])})
            else:
                if i > 1:
                    time.sleep(args.pause)
                status, body = http("POST", f"{args.n8n_url}/webhook/paperpilot",
                                    {"mode": "novelty", "idea_text": q["abstract"][:4900]}, timeout=600)
                if status != 200:
                    print(f"  [{i}] {q['paper_id']}: HTTP {status} {str(body)[:200]}", file=sys.stderr)
                    continue
                result = body["result"]
                raw.write(json.dumps({"paper_id": q["paper_id"], "result": result}) + "\n")
                judged = [m["paper_id"] for m in result.get("closest_work", []) + result.get("checked_not_close", [])]
                closest = [m["paper_id"] for m in result.get("closest_work", [])]
                rows.append({"paper_id": q["paper_id"], "method": "full: candidates", "ranks": ranks_without_self(judged, q["paper_id"], q["cited"])})
                rows.append({"paper_id": q["paper_id"], "method": "full: closest work", "ranks": ranks_without_self(closest, q["paper_id"], q["cited"])})
            found = rows[-1]["ranks"]
            print(f"  [{i}/{len(queries)}] {q['paper_id']}: {sum(1 for x in found.values() if x and x <= TOP)}/{len(found)} cited papers in top {TOP} ({rows[-1]['method']})")
    methods = list(dict.fromkeys(r["method"] for r in rows))
    table = summarize(rows, methods)
    with (out / "rows.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["paper_id", "method", "cited_paper", "rank"])
        for r in rows:
            for pid, rank in r["ranks"].items():
                w.writerow([r["paper_id"], r["method"], pid, rank or ""])
    meta = {"mode": args.mode, "queries": len(queries), "built": data["meta"]}
    (out / "summary.md").write_text(f"# E2 leave-one-out ({args.mode})\n\n```json\n{json.dumps(meta, indent=2)}\n```\n\n{table}\n", encoding="utf-8")
    print("\n" + table + f"\n\nWrote {out.relative_to(REPO)}/ (summary.md, rows.csv, raw.jsonl)")
    return 0


def main() -> int:
    env = _dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["build", "run"])
    parser.add_argument("--mode", choices=["retrieval", "full"], default="retrieval")
    parser.add_argument("--limit", type=int, default=0, help="build: query papers to keep (default 40); run: first N only")
    parser.add_argument("--min-cited", type=int, default=3, help="build: keep papers citing at least this many library papers")
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--queries", type=Path, default=QUERIES, help="the query set (default eval/e2/queries.json)")
    parser.add_argument("--label", default="", help="added to the results folder name")
    parser.add_argument("--s2-url", default=S2, help=argparse.SUPPRESS)
    parser.add_argument("--manifest", type=Path, default=REPO / "corpus" / "manifest.csv", help=argparse.SUPPRESS)  # tests point this at a stand-in
    parser.add_argument("--s2-interval", type=float, default=1.1, help=argparse.SUPPRESS)
    parser.add_argument("--cache", type=Path, default=CACHE, help=argparse.SUPPRESS)
    parser.add_argument("--pause", type=float, default=60, help="full mode: seconds between runs (Groq free tier)")
    parser.add_argument("--pp-tools-url", default=f"http://localhost:{env.get('PP_TOOLS_HOST_PORT', '8000')}")
    parser.add_argument("--n8n-url", default=f"http://localhost:{env.get('N8N_HOST_PORT', '5678')}")
    args = parser.parse_args()
    if args.command == "build":
        args.limit = args.limit or 40
        return build(args, env)
    return run(args, env)


if __name__ == "__main__":
    sys.exit(main())
