"""The audit's first step: PDF -> parsed paper, as a background job (it takes minutes on CPU)."""

import logging
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .paper import build_paper, pdf_caption_lines, picture_images, strip_images
from .parser import DoclingClient, fetch_pdf
from .store import PaperStore, sha256

log = logging.getLogger("pp_tools.audit")


def summary(paper: dict[str, Any]) -> dict[str, Any]:
    """What the run step and the job result record about a parsed paper."""
    tables, figures = paper["tables"], paper["figures"]
    return {
        "title": paper["title"],
        "pages": paper["pages"],
        "blocks": len(paper["blocks"]),
        "references": len(paper["references"]),
        "tables": [{"id": t["id"], "page": t["page"], "rows": len(t["grid"]), "caption_source": t["caption_source"]} for t in tables],
        "figures": [{"id": f["id"], "page": f["page"], "caption_source": f["caption_source"], "has_image": f["has_image"]} for f in figures],
        "numbered": sum(1 for x in tables + figures if x["number"] is not None),
        "items": len(tables) + len(figures),
    }


def parse_paper(
    url: str,
    parser: DoclingClient,
    store: PaperStore,
    max_bytes: int,
    allow_private: bool,
    progress: Callable[[dict[str, Any]], None],
    pdf: bytes | None = None,
) -> dict[str, Any]:
    """Parse the PDF at ``url``, or the bytes in ``pdf`` (an upload; ``url`` is then only its name)."""
    started = time.monotonic()
    if pdf is None:
        progress({"stage": "downloading"})
        pdf = fetch_pdf(url, max_bytes, allow_private)
    digest = sha256(pdf)
    cached = store.has(digest)
    if not cached:
        doc = parser.convert(pdf, Path(url).name or "draft.pdf", progress)
        progress({"stage": "normalising"})
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(pdf)
            tmp.flush()
            captions = pdf_caption_lines(Path(tmp.name))
        paper = build_paper(doc, picture_images(doc), captions)
        store.save(digest, strip_images(doc), paper)
    paper_dict = store.paper(digest)
    return {"sha256": digest, "cached": cached, "seconds": round(time.monotonic() - started, 1), **summary(paper_dict)}
