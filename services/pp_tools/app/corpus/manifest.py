"""The corpus manifest: the exact list of papers in the knowledge base.

manifest.csv is committed to git, so every teammate and every evaluation run
uses the same papers at the same versions. It holds arXiv ids and descriptive
metadata only (arXiv metadata is CC0); PDFs and full text stay out of git.
"""

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

FIELDS = ["arxiv_id", "version", "year", "primary_category", "fulltext", "title"]


@dataclass
class ManifestRow:
    arxiv_id: str
    version: int
    year: int
    primary_category: str
    fulltext: bool  # True: PDF is downloaded and chunked, not just the abstract
    title: str

    @property
    def versioned_id(self) -> str:
        return f"{self.arxiv_id}v{self.version}"

    @property
    def paper_id(self) -> str:
        return f"arxiv:{self.arxiv_id}"


class ManifestError(Exception):
    pass


def read_manifest(path: Path) -> list[ManifestRow]:
    if not path.exists():
        raise ManifestError(f"{path} does not exist; run corpus selection first (see corpus/README.md)")
    rows: list[ManifestRow] = []
    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ManifestError(f"{path} is missing columns: {sorted(missing)}")
        for n, raw in enumerate(reader, start=2):
            try:
                row = ManifestRow(
                    arxiv_id=raw["arxiv_id"].strip(),
                    version=int(raw["version"]),
                    year=int(raw["year"]),
                    primary_category=raw["primary_category"].strip(),
                    fulltext=raw["fulltext"].strip().lower() in {"1", "true", "yes"},
                    title=raw["title"].strip(),
                )
            except (KeyError, ValueError) as exc:
                raise ManifestError(f"{path} line {n}: {exc}") from exc
            if row.arxiv_id in seen:
                raise ManifestError(f"{path} line {n}: duplicate arxiv_id {row.arxiv_id}")
            seen.add(row.arxiv_id)
            rows.append(row)
    return rows


def write_manifest(path: Path, rows: list[ManifestRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "arxiv_id": r.arxiv_id,
                    "version": r.version,
                    "year": r.year,
                    "primary_category": r.primary_category,
                    "fulltext": 1 if r.fulltext else 0,
                    "title": r.title,
                }
            )
    tmp.replace(path)


def manifest_hash(path: Path) -> str:
    """Short content hash, recorded with every build and evaluation result."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
