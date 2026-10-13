"""Prepare a worksheet for writing E1 queries by hand.

    docker compose exec pp-tools python -m app.eval.make_queries

Writes eval/queries/worksheet.csv with three rows per full-text paper
(paraphrase, section, table). Each row shows context taken from the paper;
a teammate writes the `query` column, following eval/queries/README.md.
Rows left empty are skipped by the evaluation. Papers are assigned to the dev
or test split as a whole, so no paper leaks between the two.

Run it after a corpus build, because it reads the cached text.
"""

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

from ..config import get_settings
from ..corpus.build import EXTRACTOR_VERSION
from ..corpus.manifest import read_manifest
from ..corpus.pdftext import TextBox
from ..corpus.structure import structure

COLUMNS = ["id", "split", "type", "relevant_papers", "query", "context"]


def split_for(paper_id: str, dev_share: int = 3) -> str:
    """Stable assignment: about one paper in `dev_share` goes to dev."""
    digest = int(hashlib.sha256(paper_id.encode()).hexdigest(), 16)
    return "dev" if digest % dev_share == 0 else "test"


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("/pp/eval/queries/worksheet.csv"))
    parser.add_argument("--force", action="store_true", help="overwrite an existing worksheet")
    args = parser.parse_args(argv)

    if args.out.exists() and not args.force:
        print(f"{args.out} exists and may contain written queries. Use --force to replace it.", file=sys.stderr)
        return 1

    rows_out: list[dict[str, str]] = []
    missing = 0
    for row in read_manifest(settings.manifest_path):
        if not row.fulltext:
            continue
        meta_path = settings.cache_dir / "meta" / f"{row.versioned_id}.json"
        text_path = settings.cache_dir / "text" / f"{row.versioned_id}.{EXTRACTOR_VERSION}.json"
        if not (meta_path.exists() and text_path.exists()):
            missing += 1
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        boxes = [TextBox(lines=tuple(b["lines"]), page=b["page"]) for b in json.loads(text_path.read_text(encoding="utf-8"))]
        paper = structure(boxes)
        sections = []
        for b in paper.blocks:
            name = " ".join(p for p in (b.section_number, b.section_title) if p)
            if name and name not in sections:
                sections.append(name)
        tables = [b.text.split("\n")[0][:160] for b in paper.blocks if b.kind == "table"]

        split = split_for(row.paper_id)
        base = row.arxiv_id.replace(".", "_")
        rows_out.append(
            {
                "id": f"{base}-para",
                "split": split,
                "type": "paraphrase",
                "relevant_papers": row.paper_id,
                "query": "",
                "context": f"TITLE: {meta['title']} | ABSTRACT: {meta['abstract'][:600]}",
            }
        )
        rows_out.append(
            {
                "id": f"{base}-sec",
                "split": split,
                "type": "section",
                "relevant_papers": row.paper_id,
                "query": "",
                "context": "SECTIONS: " + "; ".join(sections[:15]),
            }
        )
        if tables:
            rows_out.append(
                {
                    "id": f"{base}-tbl",
                    "split": split,
                    "type": "table",
                    "relevant_papers": row.paper_id,
                    "query": "",
                    "context": "TABLES: " + " || ".join(tables[:6]),
                }
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows_out)
    print(f"Wrote {len(rows_out)} rows to {args.out}.")
    if missing:
        print(f"{missing} full-text papers had no cached text; run a corpus build first.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
