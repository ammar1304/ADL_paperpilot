"""Select the corpus once and write corpus/manifest.csv.

Run inside the pp-tools container (it needs internet access to arXiv):

    docker compose exec pp-tools python -m app.corpus.select

Review the result, adjust the `fulltext` column by hand if needed (the gold
set needs papers the team understands), then commit manifest.csv and
selection.json. Re-running overwrites the manifest only with --force.
"""

import argparse
import json
import logging
import math
import sys
from collections import Counter
from datetime import datetime, timezone

from ..config import get_settings
from ..logging_setup import setup_logging
from .arxiv import ArxivClient, ArxivPaper
from .manifest import ManifestRow, manifest_hash, write_manifest

log = logging.getLogger("pp_tools.select")

# Domain: retrieval-augmented generation for question answering (docs/DECISIONS.md D13).
DEFAULT_QUERY = (
    '(abs:"retrieval augmented" OR abs:"retrieval-augmented")'
    ' AND abs:"question answering"'
    " AND (cat:cs.CL OR cat:cs.IR OR cat:cs.AI OR cat:cs.LG)"
    " AND submittedDate:[202001010000 TO 202612312359]"
)
MIN_YEAR = 2020


def keep(paper: ArxivPaper) -> bool:
    if paper.year < MIN_YEAR:
        return False
    if "withdrawn" in paper.abstract.lower()[:200]:
        return False
    return bool(paper.title and paper.abstract)


def choose_fulltext(papers: list[ArxivPaper], n: int) -> set[str]:
    """Most relevant papers first, capped per year so every year is represented."""
    if n <= 0:
        return set()
    years = sorted({p.year for p in papers})
    cap = math.ceil(n / max(1, len(years)))
    per_year: Counter[int] = Counter()
    chosen: list[str] = []
    for p in papers:  # already in relevance order
        if per_year[p.year] < cap:
            chosen.append(p.arxiv_id)
            per_year[p.year] += 1
        if len(chosen) == n:
            break
    # If some years had too few papers, fill the rest by relevance.
    for p in papers:
        if len(chosen) == n:
            break
        if p.arxiv_id not in chosen:
            chosen.append(p.arxiv_id)
    return set(chosen)


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", default=DEFAULT_QUERY, help="arXiv search_query")
    parser.add_argument("--max-papers", type=int, default=500)
    parser.add_argument("--fulltext", type=int, default=50, help="how many papers get their PDF chunked")
    parser.add_argument("--force", action="store_true", help="overwrite an existing manifest.csv")
    args = parser.parse_args(argv)
    setup_logging(settings.log_level)

    path = settings.manifest_path
    if path.exists() and not args.force:
        print(f"{path} already exists. Use --force to replace it.", file=sys.stderr)
        return 1

    client = ArxivClient(settings.arxiv_api_url, settings.arxiv_pdf_url, settings.arxiv_delay_s, settings.arxiv_contact)
    try:
        # Ask for a margin: some results are filtered out below.
        found = client.search(args.query, max_results=int(args.max_papers * 1.2) + 20)
    finally:
        client.close()

    unique: dict[str, ArxivPaper] = {}
    for p in found:
        if keep(p) and p.arxiv_id not in unique:
            unique[p.arxiv_id] = p
    papers = list(unique.values())[: args.max_papers]
    if not papers:
        print("arXiv returned no usable papers for this query.", file=sys.stderr)
        return 1

    fulltext_ids = choose_fulltext(papers, args.fulltext)
    rows = [
        ManifestRow(
            arxiv_id=p.arxiv_id,
            version=p.version,
            year=p.year,
            primary_category=p.primary_category,
            fulltext=p.arxiv_id in fulltext_ids,
            title=p.title,
        )
        for p in papers
    ]
    write_manifest(path, rows)

    years = Counter(p.year for p in papers)
    summary = {
        "selected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "query": args.query,
        "returned_by_arxiv": len(found),
        "kept": len(papers),
        "fulltext": len(fulltext_ids),
        "papers_per_year": dict(sorted(years.items())),
        "manifest_sha256_12": manifest_hash(path),
    }
    (settings.corpus_dir / "selection.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"\nWrote {path}. Review it, then commit manifest.csv and selection.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
