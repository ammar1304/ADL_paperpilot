"""Add hand-picked papers to corpus/manifest.csv.

The arXiv search behind the first selection favours recent papers, so the
foundational ones (RAG, DPR, Self-RAG, ...) were missing. The team lists them in
corpus/additions.csv with the title each id is expected to have:

    arxiv_id,fulltext,expected_title,reason
    2310.11511,1,"Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection",adaptive retrieval baseline

Run inside the pp-tools container (it needs internet access to arXiv):

    docker compose exec pp-tools python -m app.corpus.add --dry-run
    docker compose exec pp-tools python -m app.corpus.add

Every id is looked up on arXiv and its real title compared with the expected
one, so a mistyped id is reported instead of silently adding the wrong paper.
Papers already in the manifest are kept; fulltext=1 upgrades them to full text.
"""

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from ..config import get_settings
from ..logging_setup import setup_logging
from .arxiv import ArxivClient, ArxivPaper
from .manifest import ManifestRow, manifest_hash, read_manifest, write_manifest

ADDITION_FIELDS = ["arxiv_id", "fulltext", "expected_title", "reason"]
TITLE_MATCH = 0.85  # similarity of normalised titles needed to accept an id


class AdditionsError(Exception):
    pass


@dataclass
class Addition:
    arxiv_id: str
    fulltext: bool
    expected_title: str
    reason: str


@dataclass
class AddReport:
    added: list[str] = field(default_factory=list)
    upgraded_to_fulltext: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    title_mismatch: list[dict[str, str]] = field(default_factory=list)

    @property
    def problems(self) -> int:
        return len(self.not_found) + len(self.title_mismatch)


def read_additions(path: Path) -> list[Addition]:
    if not path.exists():
        raise AdditionsError(f"{path} does not exist")
    out: list[Addition] = []
    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(ADDITION_FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise AdditionsError(f"{path} is missing columns: {sorted(missing)}")
        for n, raw in enumerate(reader, start=2):
            arxiv_id = raw["arxiv_id"].strip()
            if not re.fullmatch(r"\d{4}\.\d{4,5}", arxiv_id):
                raise AdditionsError(f"{path} line {n}: {arxiv_id!r} is not a bare arXiv id like 2310.11511")
            if arxiv_id in seen:
                raise AdditionsError(f"{path} line {n}: duplicate id {arxiv_id}")
            seen.add(arxiv_id)
            out.append(
                Addition(
                    arxiv_id=arxiv_id,
                    fulltext=raw["fulltext"].strip().lower() in {"1", "true", "yes"},
                    expected_title=raw["expected_title"].strip(),
                    reason=raw["reason"].strip(),
                )
            )
    return out


def _norm(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def merge(rows: list[ManifestRow], additions: list[Addition], found: dict[str, ArxivPaper]) -> tuple[list[ManifestRow], AddReport]:
    """Pure merge step: what the manifest becomes. No network, easy to test."""
    report = AddReport()
    by_id = {r.arxiv_id: r for r in rows}
    out = list(rows)
    for a in additions:
        paper = found.get(a.arxiv_id)
        if paper is None:
            report.not_found.append(a.arxiv_id)
            continue
        score = title_similarity(a.expected_title, paper.title)
        if score < TITLE_MATCH:
            report.title_mismatch.append(
                {"arxiv_id": a.arxiv_id, "expected": a.expected_title, "arxiv_title": paper.title, "similarity": f"{score:.2f}"}
            )
            continue
        existing = by_id.get(a.arxiv_id)
        if existing is not None:
            if a.fulltext and not existing.fulltext:
                existing.fulltext = True
                report.upgraded_to_fulltext.append(a.arxiv_id)
            else:
                report.already_present.append(a.arxiv_id)
            continue
        row = ManifestRow(
            arxiv_id=paper.arxiv_id,
            version=paper.version,
            year=paper.year,
            primary_category=paper.primary_category,
            fulltext=a.fulltext,
            title=" ".join(paper.title.split()),
        )
        out.append(row)
        by_id[row.arxiv_id] = row
        report.added.append(a.arxiv_id)
    return out, report


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, default=settings.corpus_dir / "additions.csv")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args(argv)
    setup_logging(settings.log_level)

    try:
        additions = read_additions(args.file)
        rows = read_manifest(settings.manifest_path)
    except Exception as exc:  # noqa: BLE001 - shown to the user as is
        print(f"error: {exc}", file=sys.stderr)
        return 2

    client = ArxivClient(settings.arxiv_api_url, settings.arxiv_pdf_url, settings.arxiv_delay_s, settings.arxiv_contact)
    try:
        found = client.fetch_latest([a.arxiv_id for a in additions])
    finally:
        client.close()

    merged, report = merge(rows, additions, found)
    summary = {
        "additions_file": str(args.file.name),
        "requested": len(additions),
        "added": report.added,
        "upgraded_to_fulltext": report.upgraded_to_fulltext,
        "already_present": report.already_present,
        "not_found": report.not_found,
        "title_mismatch": report.title_mismatch,
        "manifest_papers": len(merged),
        "manifest_fulltext": sum(r.fulltext for r in merged),
    }
    if not args.dry_run and (report.added or report.upgraded_to_fulltext):
        write_manifest(settings.manifest_path, merged)
        summary["manifest_sha256_12"] = manifest_hash(settings.manifest_path)
        selection = settings.corpus_dir / "selection.json"
        data = json.loads(selection.read_text(encoding="utf-8")) if selection.exists() else {}
        data["added_by_hand"] = {k: summary[k] for k in ("additions_file", "added", "upgraded_to_fulltext")}
        data["manifest_sha256_12"] = summary["manifest_sha256_12"]
        selection.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    summary["written"] = not args.dry_run and bool(report.added or report.upgraded_to_fulltext)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if report.problems:
        print(
            f"\n{report.problems} id(s) were not added: fix them in {args.file.name} (not_found, title_mismatch) and run again.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
