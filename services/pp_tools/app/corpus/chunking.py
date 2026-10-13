"""The two chunking strategies compared in evaluation E1.

Both strategies see the same source text and use the same size budget, so the
comparison isolates what structure-awareness adds:

A. fixed: windows of `size` tokens with `overlap`, over title + abstract + body,
   cut wherever the token count falls.
B. structured: one chunk per abstract, per section (split at paragraph
   boundaries when longer than `size`), per table with its caption, and per
   figure caption. Each chunk starts with a short header naming the paper and
   section, because knowing where a passage comes from is part of structure.

Chunk ids are stable and human-readable, e.g. arxiv:2005.11401#sec:3.2/p2.
They are the ids the verifier will check citations against (milestone 2+).
"""

import re
from dataclasses import asdict, dataclass

from ..textutil import count_tokens, windows
from .structure import Block, StructuredPaper

STRATEGIES = ("fixed", "structured")


@dataclass
class Chunk:
    chunk_id: str
    paper_id: str
    strategy: str  # "fixed" | "structured"
    chunk_type: str  # "window" | "abstract" | "section" | "table" | "figure"
    text: str
    title: str
    year: int
    position: int  # order within the paper
    section: str | None = None
    tokens: int = 0

    def payload(self) -> dict:
        return asdict(self)


@dataclass
class PaperText:
    paper_id: str
    title: str
    abstract: str
    year: int
    structured: StructuredPaper | None = None  # None: abstract only


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "x"


def fixed_chunks(paper: PaperText, size: int, overlap: int) -> list[Chunk]:
    parts = [paper.title, paper.abstract]
    if paper.structured and paper.structured.body_text:
        parts.append(paper.structured.body_text)
    source = "\n\n".join(p for p in parts if p)
    return [
        Chunk(
            chunk_id=f"{paper.paper_id}#w{i}",
            paper_id=paper.paper_id,
            strategy="fixed",
            chunk_type="window",
            text=text,
            title=paper.title,
            year=paper.year,
            position=i,
            tokens=count_tokens(text),
        )
        for i, text in enumerate(windows(source, size, overlap))
    ]


def _pack_paragraphs(paragraphs: list[str], size: int) -> list[str]:
    """Group paragraphs into pieces of at most `size` tokens, never splitting a paragraph
    unless it is longer than `size` on its own."""
    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for para in paragraphs:
        n = count_tokens(para)
        if n > size:
            if current:
                pieces.append("\n\n".join(current))
                current, current_tokens = [], 0
            pieces.extend(windows(para, size, 0))
            continue
        if current and current_tokens + n > size:
            pieces.append("\n\n".join(current))
            current, current_tokens = [], 0
        current.append(para)
        current_tokens += n
    if current:
        pieces.append("\n\n".join(current))
    return pieces


def structured_chunks(paper: PaperText, size: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    used_ids: set[str] = set()

    def add(kind: str, local_id: str, header: str, body: str, section: str | None) -> None:
        cid = f"{paper.paper_id}#{local_id}"
        n = 2
        while cid in used_ids:  # e.g. two "Table 1" (main text and appendix)
            cid = f"{paper.paper_id}#{local_id}-{n}"
            n += 1
        used_ids.add(cid)
        text = f"{header}\n{body}" if header else body
        chunks.append(
            Chunk(
                chunk_id=cid,
                paper_id=paper.paper_id,
                strategy="structured",
                chunk_type=kind,
                text=text,
                title=paper.title,
                year=paper.year,
                position=len(chunks),
                section=section,
                tokens=count_tokens(text),
            )
        )

    abstract_pieces = _pack_paragraphs([paper.abstract], size) if paper.abstract else []
    for i, piece in enumerate(abstract_pieces):
        local = "abs" if len(abstract_pieces) == 1 else f"abs/p{i + 1}"
        add("abstract", local, f"{paper.title}. Abstract.", piece, "Abstract")

    if not paper.structured:
        return chunks

    # Group consecutive paragraphs by section; tables and figures become their own chunks.
    groups: list[tuple[Block, list[str]]] = []
    for block in paper.structured.blocks:
        if block.kind == "paragraph":
            key = (block.section_number, block.section_title)
            if groups and (groups[-1][0].section_number, groups[-1][0].section_title) == key and groups[-1][0].kind == "paragraph":
                groups[-1][1].append(block.text)
            else:
                groups.append((block, [block.text]))
        else:
            groups.append((block, [block.text]))

    for block, texts in groups:
        section = " ".join(p for p in (block.section_number, block.section_title) if p) or None
        if block.kind == "table":
            add("table", f"tbl:{block.label}", f"{paper.title}. Table {block.label}.", texts[0], section)
            continue
        if block.kind == "figure":
            add("figure", f"fig:{block.label}", f"{paper.title}. Figure {block.label}.", texts[0], section)
            continue
        sec_key = block.section_number or _slug(block.section_title or "body")
        header = f"{paper.title}. Section {section}." if section else f"{paper.title}."
        pieces = _pack_paragraphs(texts, size)
        for i, piece in enumerate(pieces):
            local = f"sec:{sec_key}" if len(pieces) == 1 else f"sec:{sec_key}/p{i + 1}"
            add("section", local, header, piece, section)
    return chunks


def chunk_paper(paper: PaperText, strategy: str, size: int, overlap: int) -> list[Chunk]:
    if strategy == "fixed":
        return fixed_chunks(paper, size, overlap)
    if strategy == "structured":
        return structured_chunks(paper, size)
    raise ValueError(f"unknown strategy {strategy!r}; expected one of {STRATEGIES}")
