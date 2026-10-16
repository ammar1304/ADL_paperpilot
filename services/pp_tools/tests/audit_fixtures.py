"""A small synthetic DoclingDocument with the defects seen in the D31 spike.

- Table 1: caption linked by Docling.
- Its continuation: no caption, directly below, same columns (a split table).
- Table 2: caption missing from Docling; only the PDF text layer has it.
- Figure 1: caption linked by Docling.
- Figure 2: Docling attached a *table* caption to it, and put its own caption
  inside the picture region.
"""

import base64

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _bbox(top: float, bottom: float, left: float = 72.0, right: float = 540.0) -> dict:
    """A box given from the top of a 792-pt page, stored BOTTOMLEFT like Docling does."""
    return {"l": left, "t": 792 - top, "r": right, "b": 792 - bottom, "coord_origin": "BOTTOMLEFT"}


def _text(i: int, label: str, text: str, page: int, top: float, parent: str = "#/body") -> dict:
    return {"self_ref": f"#/texts/{i}", "parent": {"$ref": parent}, "children": [], "label": label,
            "prov": [{"page_no": page, "bbox": _bbox(top, top + 12)}], "text": text}


def _cells(rows: list[list[str]], header_rows: int = 1) -> dict:
    cells = []
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            cells.append({"text": value, "start_row_offset_idx": r, "end_row_offset_idx": r + 1,
                          "start_col_offset_idx": c, "end_col_offset_idx": c + 1, "column_header": r < header_rows})
    return {"num_rows": len(rows), "num_cols": len(rows[0]), "table_cells": cells}


def synthetic_doc() -> dict:
    texts = [
        _text(0, "title", "Corrective Retrieval for Testing", 1, 40),
        _text(1, "section_header", "1 Introduction", 1, 80),
        _text(2, "text", "Our method improves EM from 41.2 to 47.8 on NQ (Table 1), see also Figure 1.", 1, 100),
        _text(3, "caption", "Table 1: Exact match and F1 on NQ.", 1, 140),
        _text(4, "caption", "Table 9: A caption that belongs to another page's table.", 2, 100),
        _text(5, "caption", "Figure 1: Accuracy as passages grow.", 2, 400),
        _text(6, "text", "Figure 2: Confusion matrix of the classifier.", 3, 300, parent="#/pictures/1"),
        _text(7, "section_header", "References", 3, 500),
        _text(8, "list_item", "A. Author. 2020. Some cited paper. In ACL. arXiv:2101.00002.", 3, 520),
        _text(9, "page_footer", "3", 3, 780),
    ]
    tables = [
        {"self_ref": "#/tables/0", "parent": {"$ref": "#/body"}, "children": [], "label": "table",
         "prov": [{"page_no": 1, "bbox": _bbox(160, 220)}], "captions": [{"$ref": "#/texts/3"}],
         "data": _cells([["Model", "EM", "F1"], ["BM25", "41.2", "48.9"], ["Ours", "47.8", "55.3"]])},
        {"self_ref": "#/tables/1", "parent": {"$ref": "#/body"}, "children": [], "label": "table",
         "prov": [{"page_no": 1, "bbox": _bbox(221, 260)}], "captions": [],
         "data": _cells([["Ours + web", "49.0", "57.1"]], header_rows=0)},
        {"self_ref": "#/tables/2", "parent": {"$ref": "#/body"}, "children": [], "label": "table",
         "prov": [{"page_no": 2, "bbox": _bbox(500, 560)}], "captions": [],
         "data": _cells([["Setting", "Acc"], ["no retrieval", "20.0"]])},
    ]
    pictures = [
        {"self_ref": "#/pictures/0", "parent": {"$ref": "#/body"}, "children": [], "label": "picture",
         "prov": [{"page_no": 2, "bbox": _bbox(250, 390)}], "captions": [{"$ref": "#/texts/5"}],
         "image": {"mimetype": "image/png", "uri": "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()}},
        {"self_ref": "#/pictures/1", "parent": {"$ref": "#/body"}, "children": [{"$ref": "#/texts/6"}], "label": "picture",
         "prov": [{"page_no": 3, "bbox": _bbox(100, 320)}], "captions": [{"$ref": "#/texts/4"}], "image": None},
    ]
    body = [{"$ref": f"#/texts/{i}"} for i in (0, 1, 2)] + [{"$ref": "#/tables/0"}, {"$ref": "#/texts/3"}, {"$ref": "#/tables/1"},
             {"$ref": "#/texts/4"}, {"$ref": "#/pictures/0"}, {"$ref": "#/texts/5"}, {"$ref": "#/tables/2"},
             {"$ref": "#/pictures/1"}, {"$ref": "#/texts/7"}, {"$ref": "#/groups/0"}, {"$ref": "#/texts/9"}]
    return {
        "schema_name": "DoclingDocument", "version": "1.10.0", "name": "synthetic",
        "body": {"self_ref": "#/body", "children": body},
        "groups": [{"self_ref": "#/groups/0", "label": "list", "children": [{"$ref": "#/texts/8"}]}],
        "texts": texts, "tables": tables, "pictures": pictures,
        "pages": {str(p): {"size": {"width": 612.0, "height": 792.0}, "page_no": p} for p in (1, 2, 3)},
    }


# The PDF text layer of the same document: Table 2's caption exists only here.
PDF_CAPTIONS = [(1, "Table 1: Exact match and F1 on NQ."), (2, "Table 2: Accuracy without retrieval."),
                (2, "Figure 1: Accuracy as passages grow."), (3, "Figure 2: Confusion matrix of the classifier.")]
