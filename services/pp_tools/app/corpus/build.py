"""Build the papers knowledge base from corpus/manifest.csv.

Steps: metadata (cached) -> PDFs for full-text papers (cached) -> plain text
and structure -> both chunking strategies -> embeddings -> Qdrant.
A paper whose PDF cannot be read falls back to its abstract and is listed in
the build report; one bad PDF never stops the build.
"""

import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings
from ..retrieval.embedder import Embedder
from ..retrieval.store import Store
from .arxiv import ArxivClient, ArxivPaper
from .chunking import STRATEGIES, Chunk, PaperText, chunk_paper
from .manifest import ManifestRow, manifest_hash, read_manifest
from .pdftext import TextBox, extract_boxes
from .structure import structure

log = logging.getLogger("pp_tools.build")

# Bump when extraction changes, so cached text is regenerated.
EXTRACTOR_VERSION = "pdfminer-1"

Progress = Callable[[dict[str, Any]], None]


@dataclass
class BuildReport:
    started_at: str
    finished_at: str = ""
    manifest_sha256_12: str = ""
    papers_in_manifest: int = 0
    papers_indexed: int = 0
    fulltext_requested: int = 0
    fulltext_indexed: int = 0
    missing_metadata: list[str] = field(default_factory=list)
    pdf_failures: dict[str, str] = field(default_factory=dict)
    papers_without_headings: list[str] = field(default_factory=list)
    papers_without_references_cut: list[str] = field(default_factory=list)
    tables_found: int = 0
    figures_found: int = 0
    chunks: dict[str, dict[str, int]] = field(default_factory=dict)
    chunks_embedded: dict[str, int] = field(default_factory=dict)  # sent to the embedder
    chunks_reused: dict[str, int] = field(default_factory=dict)  # vector unchanged, taken from Qdrant
    embedding_model: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)


class CorpusBuilder:
    def __init__(
        self,
        settings: Settings,
        arxiv: ArxivClient,
        embedder: Embedder,
        store: Store,
        progress: Progress | None = None,
    ) -> None:
        self.s = settings
        self.arxiv = arxiv
        self.embedder = embedder
        self.store = store
        self.progress = progress or (lambda _: None)
        self.meta_dir = settings.cache_dir / "meta"
        self.pdf_dir = settings.cache_dir / "pdf"
        self.text_dir = settings.cache_dir / "text"

    # ---- public ----------------------------------------------------------------

    def run(self, recreate: bool = True, limit: int | None = None) -> BuildReport:
        report = BuildReport(started_at=_now())
        rows = read_manifest(self.s.manifest_path)
        if limit:
            rows = rows[:limit]
        report.manifest_sha256_12 = manifest_hash(self.s.manifest_path)
        report.papers_in_manifest = len(rows)
        report.fulltext_requested = sum(r.fulltext for r in rows)
        report.settings = {
            "chunk_tokens": self.s.chunk_tokens,
            "chunk_overlap": self.s.chunk_overlap,
            "extractor": EXTRACTOR_VERSION,
            "embedder_url": self.s.embedder_url,
            "limit": limit,
            "recreate": recreate,
        }

        metadata = self._metadata(rows, report)
        papers = self._paper_texts(rows, metadata, report)
        report.papers_indexed = len(papers)
        report.fulltext_indexed = sum(1 for p in papers if p.structured is not None)

        report.embedding_model = self.embedder.model_id()
        for strategy in STRATEGIES:
            chunks = [c for p in papers for c in chunk_paper(p, strategy, self.s.chunk_tokens, self.s.chunk_overlap)]
            self._index(strategy, chunks, recreate, report)
            report.chunks[strategy] = dict(Counter(c.chunk_type for c in chunks))

        report.finished_at = _now()
        self._write_report(report)
        return report

    # ---- steps -----------------------------------------------------------------

    def _metadata(self, rows: list[ManifestRow], report: BuildReport) -> dict[str, ArxivPaper]:
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        found: dict[str, ArxivPaper] = {}
        missing: list[str] = []
        for r in rows:
            cached = self.meta_dir / f"{r.versioned_id}.json"
            if cached.exists():
                found[r.versioned_id] = ArxivPaper(**json.loads(cached.read_text(encoding="utf-8")))
            else:
                missing.append(r.versioned_id)
        self.progress({"stage": "metadata", "cached": len(found), "to_fetch": len(missing)})
        if missing:
            fetched = self.arxiv.fetch(missing)
            for vid, paper in fetched.items():
                (self.meta_dir / f"{vid}.json").write_text(json.dumps(asdict(paper)), encoding="utf-8")
                found[vid] = paper
        report.missing_metadata = [vid for vid in missing if vid not in found]
        return found

    def _paper_texts(self, rows: list[ManifestRow], metadata: dict[str, ArxivPaper], report: BuildReport) -> list[PaperText]:
        papers: list[PaperText] = []
        fulltext_rows = [r for r in rows if r.fulltext]
        done = 0
        for r in rows:
            meta = metadata.get(r.versioned_id)
            if meta is None:
                continue
            structured = None
            if r.fulltext:
                done += 1
                self.progress({"stage": "pdf", "done": done, "total": len(fulltext_rows), "paper": r.paper_id})
                try:
                    boxes = self._boxes(r)
                    structured = structure(boxes)
                    report.tables_found += structured.tables_found
                    report.figures_found += structured.figures_found
                    if structured.headings_found == 0:
                        report.papers_without_headings.append(r.paper_id)
                    if not structured.references_found:
                        report.papers_without_references_cut.append(r.paper_id)
                except Exception as exc:  # noqa: BLE001 - recorded, paper falls back to its abstract
                    report.pdf_failures[r.paper_id] = f"{type(exc).__name__}: {exc}"[:300]
                    log.warning("full text unavailable, using abstract only", extra={"paper": r.paper_id, "error": str(exc)})
            papers.append(PaperText(meta.paper_id, meta.title, meta.abstract, meta.year, structured))
        return papers

    def _boxes(self, row: ManifestRow) -> list[TextBox]:
        cached = self.text_dir / f"{row.versioned_id}.{EXTRACTOR_VERSION}.json"
        if cached.exists():
            data = json.loads(cached.read_text(encoding="utf-8"))
            return [TextBox(lines=tuple(b["lines"]), page=b["page"]) for b in data]
        pdf = self.arxiv.download_pdf(row.versioned_id, self.pdf_dir / f"{row.versioned_id}.pdf")
        boxes = extract_boxes(pdf)
        self.text_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps([{"lines": list(b.lines), "page": b.page} for b in boxes]), encoding="utf-8")
        return boxes

    def _index(self, strategy: str, chunks: list[Chunk], recreate: bool, report: BuildReport) -> None:
        if not chunks:
            return
        model = report.embedding_model
        # Read reusable vectors before a recreate deletes the collection.
        reuse = self.store.reusable_vectors(chunks, model) if self.s.reuse_vectors else {}
        if reuse:
            log.info("reusing stored vectors", extra={"strategy": strategy, "reused": len(reuse), "total": len(chunks)})
        report.chunks_reused[strategy] = len(reuse)
        report.chunks_embedded[strategy] = 0
        batch = max(1, self.s.embed_batch_size) * 4
        created = False
        for i in range(0, len(chunks), batch):
            part = chunks[i : i + batch]
            todo = [c for c in part if c.chunk_id not in reuse]
            fresh = dict(zip((c.chunk_id for c in todo), self.embedder.embed([c.text for c in todo]))) if todo else {}
            report.chunks_embedded[strategy] += len(todo)
            vectors = [reuse.get(c.chunk_id) or fresh[c.chunk_id] for c in part]
            if not created:
                self.store.ensure_collection(strategy, dim=len(vectors[0]), recreate=recreate)
                created = True
            self.store.upsert(part, vectors, model)
            self.progress(
                {"stage": f"index:{strategy}", "done": min(i + batch, len(chunks)), "total": len(chunks), "reused": len(reuse)}
            )

    def _write_report(self, report: BuildReport) -> None:
        self.s.cache_dir.mkdir(parents=True, exist_ok=True)
        path: Path = self.s.cache_dir / "build_report.json"
        path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
