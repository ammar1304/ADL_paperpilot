"""Plain PDF text extraction (no layout model).

This is the baseline extractor of milestone 1. pdfminer.six groups characters
into text boxes and orders them column by column, which is good enough for
two-column papers but loses table structure: a table comes out as a run of
short boxes. MinerU replaces this in milestone 3, and comparing the two is one
of the planned ablations (docs/DECISIONS.md D16).
"""

import re
from dataclasses import dataclass
from pathlib import Path

from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTTextContainer

_HYPHEN_BREAK = re.compile(r"(\w)-\n([a-z])")
_ARXIV_STAMP = re.compile(r"^arXiv:\d{4}\.\d{4,5}(v\d+)?\b")


@dataclass(frozen=True)
class TextBox:
    lines: tuple[str, ...]  # original lines, whitespace-normalized
    page: int  # 1-based

    @property
    def text(self) -> str:
        return " ".join(self.lines)

    @property
    def first_line(self) -> str:
        return self.lines[0] if self.lines else ""


class PdfTextError(Exception):
    pass


def _clean_lines(raw: str) -> tuple[str, ...]:
    raw = _HYPHEN_BREAK.sub(r"\1\2", raw)
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
    return tuple(line for line in lines if line)


def _reading_key(bbox: tuple[float, float, float, float], page_width: float) -> tuple[int, float, float]:
    """Left column top to bottom, then right column. pdfminer's own ordering
    (boxes_flow) sometimes reads the right column first on two-column pages.
    Boxes starting left of 45% of the page width (left column, full-width
    titles, single-column pages) count as the first column."""
    x0, _, _, y1 = bbox
    column = 0 if x0 < page_width * 0.45 else 1
    return column, -y1, x0


def extract_boxes(pdf_path: Path, max_pages: int = 60) -> list[TextBox]:
    """Text boxes in reading order, with page numbers. Drops page numbers and arXiv side stamps."""
    boxes: list[TextBox] = []
    try:
        for page_no, page in enumerate(extract_pages(str(pdf_path), laparams=LAParams()), start=1):
            if page_no > max_pages:
                break
            elements = [e for e in page if isinstance(e, LTTextContainer)]
            elements.sort(key=lambda e: _reading_key(e.bbox, page.width))
            for element in elements:
                lines = _clean_lines(element.get_text())
                if not lines:
                    continue
                joined = " ".join(lines)
                if joined.isdigit() or _ARXIV_STAMP.match(joined):
                    continue
                boxes.append(TextBox(lines=lines, page=page_no))
    except Exception as exc:  # pdfminer raises many exception types on broken files
        raise PdfTextError(f"{pdf_path.name}: {type(exc).__name__}: {exc}") from exc
    if not boxes:
        raise PdfTextError(f"{pdf_path.name}: no text found (scanned PDF?)")
    return boxes
