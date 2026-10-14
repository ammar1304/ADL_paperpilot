"""E1: compare the fixed-size and structure-aware chunking strategies.

    docker compose exec pp-tools python -m app.eval.retrieval \\
        --queries /pp/eval/queries/dev.jsonl --label dev-first-run

Queries come from a .csv worksheet (app.eval.make_queries; rows with an
empty query are skipped, relevant_papers separated by ";") or a .jsonl file,
one query per line:
    {"id": "q001", "query": "...", "relevant_papers": ["arxiv:2005.11401"],
     "type": "paraphrase" | "section" | "table", "split": "dev" | "test"}

Metrics are paper-level: retrieved chunks are reduced to a ranked list of
distinct papers (best chunk first). By default queries go through the running
service's POST /search, so the numbers describe the system as deployed;
--direct queries Qdrant and the embedder without the API. Results go to eval/results/<date>-<label>/
with the exact configuration, so every number in the report can be traced.
Tune on dev; run test once, at the end.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from ..config import get_settings
from ..corpus.chunking import STRATEGIES
from ..corpus.manifest import manifest_hash
from ..logging_setup import setup_logging
from ..retrieval.embedder import Embedder
from ..retrieval.search import Retriever
from ..retrieval.store import Store, make_client
from .metrics import mean, recall_at_k, reciprocal_rank

QUERY_TYPES = ("paraphrase", "section", "table")


class ApiRetriever:
    """Same interface as Retriever.papers, through pp-tools' POST /search."""

    def __init__(self, base_url: str, timeout_s: float = 120.0, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = client or httpx.Client(timeout=timeout_s)

    def papers(self, query: str, strategy: str, k: int) -> list[Any]:
        resp = self.http.post(
            f"{self.base_url}/search", json={"query": query, "strategy": strategy, "k": k, "level": "paper"}
        )
        if resp.status_code != 200:
            raise RuntimeError(f"POST /search returned HTTP {resp.status_code}: {resp.text[:300]}")
        return [SimpleNamespace(**h) for h in resp.json()["hits"]]

    def close(self) -> None:
        self.http.close()


@dataclass
class Query:
    id: str
    query: str
    relevant: set[str]
    type: str
    split: str


class QueryFileError(Exception):
    pass


def _raw_rows(path: Path) -> list[tuple[int, dict[str, Any]]]:
    """(line number, record) pairs from a .jsonl file or a .csv worksheet."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".csv":
        rows = []
        for n, rec in enumerate(csv.DictReader(io.StringIO(text)), start=2):
            if not (rec.get("query") or "").strip():
                continue  # worksheet row not written yet
            rec["relevant_papers"] = [p.strip() for p in (rec.get("relevant_papers") or "").split(";") if p.strip()]
            rows.append((n, rec))
        return rows
    rows = []
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            rows.append((n, json.loads(line)))
        except json.JSONDecodeError as exc:
            raise QueryFileError(f"{path}:{n}: {exc}") from exc
    return rows


def load_queries(path: Path) -> list[Query]:
    queries: list[Query] = []
    seen: set[str] = set()
    for n, raw in _raw_rows(path):
        try:
            q = Query(
                id=str(raw["id"]),
                query=str(raw["query"]).strip(),
                relevant=set(raw["relevant_papers"]),
                type=raw.get("type") or "paraphrase",
                split=raw.get("split") or "dev",
            )
        except (KeyError, TypeError) as exc:
            raise QueryFileError(f"{path}:{n}: missing field {exc}") from exc
        if not q.query or not q.relevant:
            raise QueryFileError(f"{path}:{n}: empty query or no relevant_papers")
        if q.type not in QUERY_TYPES:
            raise QueryFileError(f"{path}:{n}: type must be one of {QUERY_TYPES}")
        if q.id in seen:
            raise QueryFileError(f"{path}:{n}: duplicate id {q.id}")
        seen.add(q.id)
        queries.append(q)
    if not queries:
        raise QueryFileError(f"{path}: no queries")
    return queries


def evaluate(retriever: Retriever, queries: list[Query], strategies: list[str], ks: list[int]) -> dict[str, Any]:
    """Per-query rankings and aggregate metrics for each strategy."""
    max_k = max(ks)
    per_query: list[dict[str, Any]] = []
    for q in queries:
        row: dict[str, Any] = {"id": q.id, "type": q.type, "relevant": sorted(q.relevant), "rankings": {}}
        for strategy in strategies:
            ranked = [h.paper_id for h in retriever.papers(q.query, strategy, max_k)]
            row["rankings"][strategy] = ranked
        per_query.append(row)

    def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for strategy in strategies:
            metrics: dict[str, float] = {}
            for k in ks:
                metrics[f"recall@{k}"] = mean([recall_at_k(r["rankings"][strategy], set(r["relevant"]), k) for r in rows])
            metrics[f"mrr@{max_k}"] = mean([reciprocal_rank(r["rankings"][strategy], set(r["relevant"]), max_k) for r in rows])
            out[strategy] = {name: round(v, 4) for name, v in metrics.items()}
        out["n"] = len(rows)
        return out

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in per_query:
        by_type[r["type"]].append(r)
    return {
        "overall": aggregate(per_query),
        "by_type": {t: aggregate(rows) for t, rows in sorted(by_type.items())},
        "per_query": per_query,
    }


def markdown_table(summary: dict[str, Any], strategies: list[str]) -> str:
    groups = [("all", summary["overall"])] + [(t, s) for t, s in summary["by_type"].items()]
    metric_names = [m for m in summary["overall"][strategies[0]]]
    lines = ["| Queries | n | Strategy | " + " | ".join(metric_names) + " |"]
    lines.append("| --- | --- | --- | " + " | ".join("---" for _ in metric_names) + " |")
    for name, group in groups:
        for strategy in strategies:
            values = " | ".join(f"{group[strategy][m]:.3f}" for m in metric_names)
            lines.append(f"| {name} | {group['n']} | {strategy} | {values} |")
    return "\n".join(lines)


def _git_commit() -> str:
    # Inside the container there is no .git: pass PP_GIT_COMMIT (see eval/README.md).
    if os.environ.get("PP_GIT_COMMIT"):
        return os.environ["PP_GIT_COMMIT"]
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--split", choices=["dev", "test", "all"], default="all", help="evaluate only one split")
    parser.add_argument("--k", default="1,5,10,20", help="comma-separated cutoffs")
    parser.add_argument("--strategies", default=",".join(STRATEGIES))
    parser.add_argument("--label", default="run", help="short name for the results folder")
    parser.add_argument("--out", type=Path, default=Path("/pp/eval/results"))
    parser.add_argument("--api", default="http://127.0.0.1:8000", help="pp-tools base URL")
    parser.add_argument("--direct", action="store_true", help="query Qdrant and the embedder directly, not the API")
    args = parser.parse_args(argv)
    setup_logging("WARNING")

    ks = sorted({int(k) for k in args.k.split(",")})
    strategies = [s for s in args.strategies.split(",") if s]
    queries = load_queries(args.queries)
    if args.split != "all":
        queries = [q for q in queries if q.split == args.split]
    if not queries:
        print(f"no queries in split {args.split!r}", file=sys.stderr)
        return 1

    if args.direct:
        embedder = Embedder(settings.embedder_url, settings.embed_batch_size, settings.embed_timeout_s)
        retriever: Any = Retriever(embedder, Store(make_client(settings.qdrant_url)))
        closer = embedder.close
    else:
        retriever = ApiRetriever(args.api)
        closer = retriever.close
    try:
        summary = evaluate(retriever, queries, strategies, ks)
    except RuntimeError as exc:
        print(f"evaluation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        closer()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    folder = args.out / f"{stamp}-{args.label}"
    folder.mkdir(parents=True, exist_ok=True)
    config = {
        "created_at": stamp,
        "git_commit": _git_commit(),
        "queries_file": str(args.queries),
        "queries_sha256_12": hashlib.sha256(args.queries.read_bytes()).hexdigest()[:12],
        "split": args.split,
        "manifest_sha256_12": manifest_hash(settings.manifest_path) if settings.manifest_path.exists() else None,
        "chunk_tokens": settings.chunk_tokens,
        "chunk_overlap": settings.chunk_overlap,
        "embedder_url": settings.embedder_url,
        "retrieval_path": "direct" if args.direct else f"api {args.api}",
        "ks": ks,
        "strategies": strategies,
    }
    (folder / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (folder / "per_query.json").write_text(json.dumps(summary["per_query"], indent=1) + "\n", encoding="utf-8")
    table = markdown_table(summary, strategies)
    (folder / "summary.json").write_text(
        json.dumps({"overall": summary["overall"], "by_type": summary["by_type"]}, indent=2) + "\n", encoding="utf-8"
    )
    (folder / "summary.md").write_text(f"# E1 retrieval: {args.label}\n\n{table}\n", encoding="utf-8")
    print(table)
    print(f"\nResults written to {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
