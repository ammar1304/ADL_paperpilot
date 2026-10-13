"""Recover a paper's structure (sections, tables, figure captions) from plain text boxes.

These are heuristics over pdfminer output, documented as such: they detect
numbered or well-known section headings, "Table N:" and "Figure N:" captions,
and the runs of short, number-heavy boxes that surround a table. They will
miss some structure; MinerU's layout model replaces them in milestone 3.
"""

import re
from dataclasses import dataclass, field

from .pdftext import TextBox

KNOWN_HEADINGS = {
    "abstract",
    "introduction",
    "background",
    "related work",
    "related works",
    "preliminaries",
    "method",
    "methods",
    "methodology",
    "approach",
    "experiments",
    "experimental setup",
    "experimental results",
    "results",
    "evaluation",
    "analysis",
    "discussion",
    "conclusion",
    "conclusions",
    "conclusion and future work",
    "limitations",
    "ethics statement",
    "ethical considerations",
    "broader impact",
    "acknowledgments",
    "acknowledgements",
    "references",
    "bibliography",
    "appendix",
}
END_OF_BODY = {"references", "bibliography"}
SKIP_SECTIONS = {"acknowledgments", "acknowledgements"}

_NUMBERED = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+(?P<title>[A-Z][^.]{1,80})$")
_TABLE_CAPTION = re.compile(r"^Table\s+(?P<num>\d{1,2}|[A-Z]\.?\d{1,2})\s*[:.|]")
_FIGURE_CAPTION = re.compile(r"^(?:Figure|Fig\.)\s+(?P<num>\d{1,2}|[A-Z]\.?\d{1,2})\s*[:.|]")
_NUMBER = re.compile(r"^[\d.,%±+\-−()]+$")


@dataclass
class Block:
    kind: str  # "paragraph" | "table" | "figure"
    text: str
    page: int
    section_number: str | None = None  # "3.2"
    section_title: str | None = None  # "Retrieval"
    label: str | None = None  # "2" for Table 2


@dataclass
class StructuredPaper:
    blocks: list[Block] = field(default_factory=list)
    headings_found: int = 0
    tables_found: int = 0
    figures_found: int = 0
    references_found: bool = False
    body_text: str = ""  # everything kept, in reading order (input of the fixed-size strategy)


def parse_heading(line: str) -> tuple[str | None, str] | None:
    """Return (number, title) if `line` looks like a section heading, else None."""
    line = line.strip()
    if not line or len(line) > 90 or line.endswith((".", ",", ";", ":")):
        return None
    if line.lower().rstrip(":") in KNOWN_HEADINGS:
        return None, line.rstrip(":")
    m = _NUMBERED.match(line)
    if not m:
        return None
    title = m.group("title").strip()
    digits = sum(c.isdigit() for c in title)
    # Table rows like "1 BM25 41.2 38.7" also start with a number; headings rarely contain digits.
    if digits / max(1, len(title)) > 0.15 or len(title.split()) > 12:
        return None
    return m.group("num"), title


def is_tablelike(text: str) -> bool:
    tokens = text.split()
    if not tokens:
        return False
    numeric = sum(1 for t in tokens if _NUMBER.match(t))
    if numeric / len(tokens) >= 0.3:
        return True
    return len(text) < 40 and not text.endswith(".")


def structure(boxes: list[TextBox]) -> StructuredPaper:
    """Turn reading-order text boxes into sections, tables and figure captions."""
    out = StructuredPaper()

    # Pass 1: split headings off the boxes and find where the body starts and ends.
    items: list[tuple[str, TextBox | tuple[str | None, str]]] = []
    for box in boxes:
        heading = parse_heading(box.first_line)
        if heading:
            items.append(("heading", heading))
            rest = box.lines[1:]
            if rest:
                items.append(("box", TextBox(lines=rest, page=box.page)))
        else:
            items.append(("box", box))

    # The body starts at the first heading after the front matter (title, authors, abstract).
    start = 0
    for i, (kind, value) in enumerate(items):
        if kind == "heading" and isinstance(value, tuple) and value[1].lower() != "abstract":
            start = i
            break
    items = items[start:]

    # Pass 2: walk the body, assign sections, cut at the references.
    section_number: str | None = None
    section_title: str | None = None
    skipping = False
    flow: list[Block] = []
    for kind, value in items:
        if kind == "heading":
            number, title = value  # type: ignore[misc]
            if title.lower() in END_OF_BODY:
                out.references_found = True
                break
            out.headings_found += 1
            section_number, section_title = number, title
            skipping = title.lower() in SKIP_SECTIONS
            continue
        if skipping:
            continue
        box: TextBox = value  # type: ignore[assignment]
        flow.append(Block("paragraph", box.text, box.page, section_number, section_title))

    # Pass 3: pull out table blocks (caption + surrounding table-like boxes) and figure captions.
    used = [False] * len(flow)
    special: dict[int, Block] = {}
    for i, block in enumerate(flow):
        table = _TABLE_CAPTION.match(block.text)
        figure = _FIGURE_CAPTION.match(block.text)
        if not (table or figure):
            continue
        used[i] = True
        members = _neighbours(flow, i, used)
        for j in members:
            used[j] = True
        if table:
            body = " | ".join(flow[j].text for j in sorted(members))
            text = block.text + ("\n" + body if body else "")
            special[i] = Block("table", text, block.page, block.section_number, block.section_title, table.group("num"))
            out.tables_found += 1
        else:
            # Text inside charts (axis labels, legends) is noise: keep only the caption.
            special[i] = Block("figure", block.text, block.page, block.section_number, block.section_title, figure.group("num"))
            out.figures_found += 1

    for i, block in enumerate(flow):
        if i in special:
            out.blocks.append(special[i])
        elif not used[i]:
            out.blocks.append(block)

    # Input of the fixed-size strategy: the same body, in reading order, without structure.
    parts: list[str] = []
    current: tuple[str | None, str | None] = (None, None)
    for block in flow:
        heading = (block.section_number, block.section_title)
        if heading != current and block.section_title:
            parts.append(" ".join(p for p in heading if p))
            current = heading
        parts.append(block.text)
    out.body_text = "\n\n".join(parts)
    return out


def _neighbours(flow: list[Block], i: int, used: list[bool], limit: int = 40) -> list[int]:
    """Indices of the contiguous table-like boxes before and after caption i, on the same page."""
    page = flow[i].page
    members: list[int] = []
    for step in (-1, 1):
        j = i + step
        while 0 <= j < len(flow) and len(members) < limit:
            b = flow[j]
            if used[j] or b.page != page or not is_tablelike(b.text):
                break
            if _TABLE_CAPTION.match(b.text) or _FIGURE_CAPTION.match(b.text):
                break
            members.append(j)
            j += step
    return members
