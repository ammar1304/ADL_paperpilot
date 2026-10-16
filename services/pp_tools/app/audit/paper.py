"""A DoclingDocument turned into what the audit needs: blocks, numbered tables and figures.

Docling returns a tree of items (texts, tables, pictures) with their page and
bounding box. The audit needs something flatter and stable:

- ``blocks``: the body text in reading order, each with an id (``b:12``), its
  label (text, section_header, list_item, caption, footnote, formula, code),
  the section it belongs to and its page;
- ``tables``: each with the paper's own number taken from its caption
  (``table:3``), the caption, the page and the cell grid;
- ``figures``: the same for pictures (``figure:2``), plus the crop as PNG bytes;
- ``references``: the bibliography entries, for the citation check.

Claims in the text say "Table 3", so tables and figures are identified by the
number in their caption, never by Docling's order: in the spike, Docling's 7th
table was the paper's Table 11 (D31).

Caption repair. Docling links a caption to its table or picture most of the
time, but in the spike 4 of Self-RAG's 8 tables had none and one table had a
figure's caption. ``repair_captions`` fills the gaps in three steps, most reliable first:

1. a caption-like text of the right kind on the same page, nearest to the
   item and not linked to anything else ("Table N" for tables, "Figure N" or
   "Fig. N" for pictures);
2. for a picture, a caption-like text Docling put inside the picture region
   (it sometimes swallows the caption with the axis labels);
3. the PDF's own text layer (pdfminer, already used for the corpus): if exactly
   one caption number of that kind on that page is still unused, it is the
   item's. Docling sometimes drops a caption entirely (CRAG's Table 6).

Split tables. Docling sometimes cuts one table in two (Adaptive-RAG's Table 2:
its single-step and multi-step halves). An uncaptioned table placed directly
below a numbered one on the same page, as wide and with the same number of
columns, is merged into it.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any

CAPTION_RE = re.compile(r"^\s*(table|tab\.|figure|fig\.?)\s*(\d+)\s*[:.]", re.IGNORECASE)
# Body text that is not part of the paper's content.
SKIP_LABELS = {"page_header", "page_footer"}
REFERENCE_HEADINGS = re.compile(r"^\s*(\d+\s*\.?\s*)?(references|bibliography)\s*$", re.IGNORECASE)


@dataclass
class Block:
    id: str
    label: str
    text: str
    section: str
    page: int | None


@dataclass
class Table:
    id: str  # "table:3", or "table:u2" when no caption number was found
    number: int | None
    caption: str
    caption_source: str  # "docling", "repaired", "pdf-text" or "none"
    page: int | None
    grid: list[list[str]]
    header_rows: int  # leading rows Docling marked as column headers
    docling_ref: str
    merged_from: list[str] = field(default_factory=list)  # continuation parts of a split table


@dataclass
class Figure:
    id: str  # "figure:2", or "figure:u1"
    number: int | None
    caption: str
    caption_source: str
    page: int | None
    docling_ref: str
    image_png: bytes | None = field(default=None, repr=False)


@dataclass
class Paper:
    title: str
    pages: int
    blocks: list[Block]
    tables: list[Table]
    figures: list[Figure]
    references: list[str]

    def as_dict(self, with_images: bool = False) -> dict[str, Any]:
        """JSON-safe dict; figure images are left out unless asked for (they are large)."""
        d = asdict(self)
        for f in d["figures"]:
            img = f.pop("image_png")
            f["has_image"] = img is not None
            if with_images and img is not None:
                import base64

                f["image_png_base64"] = base64.b64encode(img).decode()
        return d


# ---- helpers on the raw DoclingDocument -----------------------------------------------


def resolve(doc: dict[str, Any], ref: str) -> dict[str, Any]:
    """"#/texts/12" -> doc["texts"][12]."""
    node: Any = doc
    for part in ref.lstrip("#/").split("/"):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _page(item: dict[str, Any]) -> int | None:
    prov = item.get("prov") or []
    return prov[0].get("page_no") if prov else None


def _top(doc: dict[str, Any], item: dict[str, Any]) -> tuple[float, float] | None:
    """Vertical extent (top, bottom) of an item, measured from the top of its page."""
    prov = item.get("prov") or []
    if not prov or "bbox" not in prov[0]:
        return None
    bb, page = prov[0]["bbox"], prov[0].get("page_no")
    height = ((doc.get("pages") or {}).get(str(page)) or {}).get("size", {}).get("height")
    if bb.get("coord_origin") == "BOTTOMLEFT" and height:
        return height - bb["t"], height - bb["b"]
    return bb["t"], bb["b"]


def _distance(doc: dict[str, Any], a: dict[str, Any], b: dict[str, Any]) -> float:
    ea, eb = _top(doc, a), _top(doc, b)
    if ea is None or eb is None:
        return float("inf")
    # gap between the two boxes (0 when they overlap vertically)
    return max(0.0, eb[0] - ea[1], ea[0] - eb[1])


def caption_kind(text: str) -> tuple[str, int] | None:
    """("table", 3) for "Table 3: ...", ("figure", 2) for "Fig. 2." and "Figure 2:"."""
    m = CAPTION_RE.match(text or "")
    if not m:
        return None
    return ("table" if m.group(1).lower().startswith("tab") else "figure", int(m.group(2)))


def grid(table: dict[str, Any]) -> tuple[list[list[str]], int]:
    """Cell grid (spanning cells repeated in every row and column they cover) and header row count."""
    data = table.get("data") or {}
    rows, cols = data.get("num_rows", 0), data.get("num_cols", 0)
    g = [["" for _ in range(cols)] for _ in range(rows)]
    header = [False] * rows
    for cell in data.get("table_cells", []):
        for r in range(cell["start_row_offset_idx"], min(cell["end_row_offset_idx"], rows)):
            if cell.get("column_header"):
                header[r] = True
            for c in range(cell["start_col_offset_idx"], min(cell["end_col_offset_idx"], cols)):
                g[r][c] = (cell.get("text") or "").strip()
    header_rows = 0
    while header_rows < rows and header[header_rows]:
        header_rows += 1
    return g, header_rows


# ---- caption repair --------------------------------------------------------------------


def repair_captions(doc: dict[str, Any], pdf_captions: list[tuple[int, str]] | None = None) -> dict[str, tuple[str, str]]:
    """{docling_ref: (caption text, source)} for every table and picture.

    A Docling-linked caption is kept when it names the right kind ("Table" for a
    table). Otherwise the nearest caption-like text of the right kind on the same
    page, not already linked to another item, is used.
    """
    out: dict[str, tuple[str, str]] = {}
    claimed: set[str] = set()
    items = [(t, "table") for t in doc.get("tables", [])] + [(p, "figure") for p in doc.get("pictures", [])]
    for item, kind in items:
        texts = [resolve(doc, c["$ref"]) for c in item.get("captions", [])]
        good = [t for t in texts if (caption_kind(t.get("text", "")) or ("",))[0] == kind]
        if good:
            out[item["self_ref"]] = (" ".join(t["text"] for t in good).strip(), "docling")
            claimed.update(t["self_ref"] for t in good)
    candidates = [
        t for t in doc.get("texts", [])
        if t.get("label") in ("caption", "text") and caption_kind(t.get("text", "")) and t["self_ref"] not in claimed
    ]
    for item, kind in items:
        if item["self_ref"] in out:
            continue
        page = _page(item)
        same = [t for t in candidates if _page(t) == page and caption_kind(t["text"])[0] == kind and t["self_ref"] not in claimed]
        if same:
            best = min(same, key=lambda t: _distance(doc, item, t))
            if _distance(doc, item, best) < 120:  # points; a caption sits right above or below its object
                out[item["self_ref"]] = (best["text"].strip(), "repaired")
                claimed.add(best["self_ref"])
                continue
        inside = [
            resolve(doc, c["$ref"]) for c in item.get("children", []) if c["$ref"].startswith("#/texts/")
        ] if kind == "figure" else []
        inside = [t for t in inside if (caption_kind(t.get("text", "")) or ("",))[0] == kind and t["self_ref"] not in claimed]
        if inside:
            out[item["self_ref"]] = (inside[0]["text"].strip(), "repaired")
            claimed.add(inside[0]["self_ref"])
            continue
        out[item["self_ref"]] = ("", "none")

    # 3. the PDF text layer, only where it is unambiguous
    used = {(caption_kind(c) or ("", None)) for c, _ in out.values()}
    for item, kind in items:
        if out[item["self_ref"]][1] != "none":
            continue
        page = _page(item)
        free = {}
        for p, text in pdf_captions or []:
            k = caption_kind(text)
            if p == page and k and k[0] == kind and k not in used:
                free.setdefault(k, text)
        missing_here = [i for i, kd in items if kd == kind and _page(i) == page and out[i["self_ref"]][1] == "none"]
        if len(free) == 1 and len(missing_here) == 1:
            (k, text), = free.items()
            out[item["self_ref"]] = (text.strip(), "pdf-text")
            used.add(k)
    return out


def merge_split_tables(doc: dict[str, Any], captions: dict[str, tuple[str, str]]) -> dict[str, str]:
    """{continuation table ref: head table ref} for tables Docling cut in two."""
    merged: dict[str, str] = {}
    tables = doc.get("tables", [])
    for prev, cur in zip(tables, tables[1:]):
        head = merged.get(prev["self_ref"], prev["self_ref"])
        if captions[cur["self_ref"]][1] != "none" or not caption_kind(captions[head][0]) or _page(prev) != _page(cur):
            continue
        a, b = _top(doc, prev), _top(doc, cur)
        pa, pb = prev["prov"][0]["bbox"], cur["prov"][0]["bbox"]
        overlap = min(pa["r"], pb["r"]) - max(pa["l"], pb["l"])
        same_cols = (prev.get("data") or {}).get("num_cols") == (cur.get("data") or {}).get("num_cols")
        if a and b and -5 <= b[0] - a[1] < 30 and overlap > 0.8 * (pa["r"] - pa["l"]) and same_cols:
            merged[cur["self_ref"]] = head
    return merged


# ---- the paper ----------------------------------------------------------------------


def _walk(doc: dict[str, Any], node: dict[str, Any]):
    """Items under a node, in reading order (groups such as lists are expanded)."""
    for child in node.get("children", []):
        item = resolve(doc, child["$ref"])
        if child["$ref"].startswith("#/groups/"):
            yield from _walk(doc, item)
        else:
            yield child["$ref"], item
            if item.get("children") and not child["$ref"].startswith(("#/tables/", "#/pictures/")):
                yield from _walk(doc, item)


def build_paper(
    doc: dict[str, Any], images: dict[str, bytes] | None = None, pdf_captions: list[tuple[int, str]] | None = None
) -> Paper:
    """``images``: PNG bytes per picture self_ref; ``pdf_captions``: (page, text) of caption-like
    lines from the PDF's own text layer, for the last caption-repair step."""
    captions = repair_captions(doc, pdf_captions)
    continuation = merge_split_tables(doc, captions)
    caption_refs = set()
    for item in doc.get("tables", []) + doc.get("pictures", []):
        caption_refs.update(c["$ref"] for c in item.get("captions", []))

    blocks: list[Block] = []
    references: list[str] = []
    section, in_refs, title = "", False, ""
    for ref, item in _walk(doc, doc.get("body", {})):
        if not ref.startswith("#/texts/"):
            continue
        label, text = item.get("label", "text"), (item.get("text") or "").strip()
        if label in SKIP_LABELS or not text:
            continue
        if label in ("title", "section_header"):
            if not title:
                title = text
            section = text
            in_refs = bool(REFERENCE_HEADINGS.match(text))
            if label == "title":
                continue
        elif in_refs and label in ("list_item", "text"):
            references.append(text)
            continue
        blocks.append(Block(f"b:{len(blocks)}", label, text, section, _page(item)))

    tables, figures = [], []
    seen: dict[str, int] = {}

    def make_id(kind: str, number: int | None, unnumbered: int) -> str:
        if number is None:
            return f"{kind}:u{unnumbered}"
        key = f"{kind}:{number}"
        seen[key] = seen.get(key, 0) + 1
        return key if seen[key] == 1 else f"{key}-{seen[key]}"  # a number seen twice (rare): keep both

    unnumbered = {"table": 0, "figure": 0}
    by_ref: dict[str, Table] = {}
    for t in doc.get("tables", []):
        if t["self_ref"] in continuation:
            head = by_ref[continuation[t["self_ref"]]]
            head.grid.extend(grid(t)[0])
            head.merged_from.append(t["self_ref"])
            by_ref[t["self_ref"]] = head
            continue
        cap, src = captions[t["self_ref"]]
        kind = caption_kind(cap)
        number = kind[1] if kind else None
        if number is None:
            unnumbered["table"] += 1
        g, header_rows = grid(t)
        tables.append(Table(make_id("table", number, unnumbered["table"]), number, cap, src, _page(t), g, header_rows, t["self_ref"]))
        by_ref[t["self_ref"]] = tables[-1]
    for p in doc.get("pictures", []):
        cap, src = captions[p["self_ref"]]
        kind = caption_kind(cap)
        number = kind[1] if kind else None
        if number is None:
            unnumbered["figure"] += 1
        figures.append(Figure(make_id("figure", number, unnumbered["figure"]), number, cap, src, _page(p),
                              p["self_ref"], (images or {}).get(p["self_ref"])))

    return Paper(title=title, pages=len(doc.get("pages") or {}), blocks=blocks, tables=tables, figures=figures, references=references)


def picture_images(doc: dict[str, Any]) -> dict[str, bytes]:
    """PNG bytes of every picture exported as an embedded data URI (image_export_mode=embedded)."""
    import base64

    out = {}
    for p in doc.get("pictures", []):
        uri = ((p.get("image") or {}).get("uri") or "")
        if uri.startswith("data:image/png;base64,"):
            out[p["self_ref"]] = base64.b64decode(uri.split(",", 1)[1])
    return out


def strip_images(doc: dict[str, Any]) -> dict[str, Any]:
    """The document without embedded image data (pages and pictures), for storage."""
    slim = dict(doc)
    slim["pictures"] = [{**p, "image": None} for p in doc.get("pictures", [])]
    slim["tables"] = [{**t, "image": None} for t in doc.get("tables", [])]
    slim["pages"] = {k: {**v, "image": None} for k, v in (doc.get("pages") or {}).items()}
    return slim


def pdf_caption_lines(pdf_path) -> list[tuple[int, str]]:
    """(page, text) of every text box in the PDF that starts like a table or figure caption."""
    from ..corpus.pdftext import extract_boxes

    return [(b.page, b.text) for b in extract_boxes(pdf_path, max_pages=200) if caption_kind(b.text)]
