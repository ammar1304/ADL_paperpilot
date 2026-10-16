"""Number check (milestone 3, step 4): does a number in the text match the paper's own tables?

Deterministic, no LLM (D35). Standard library only, so the offline evaluation
(scripts/e3_number_check.py) imports it without the service's dependencies.

1. Every table becomes a list of numeric cells, each with its context: the row
   label (and the group labels carried down from rows above), the column
   headers of the current header block, and the current section row
   ("SelfRAG-LLaMA2-7b" spanning the whole width).
2. A claim's fields (system, dataset, metric, setting) are matched against that
   context. The system must match; the other fields narrow the candidates
   when some cell matches them and are ignored otherwise (a paper may say
   "accuracy" where the table says "acc").
3. A located value is compared at the precision it was written with. A margin
   ("outperforms RAG by 7.0%") is recomputed from the system's and the
   baseline's cells in the same column, as an absolute difference and as a
   relative one.

Verdicts: "supported", "contradicted" (located, and the table says otherwise),
"unverifiable" (no cell could be located; never reported as an error).
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_CELL_NUMBER = re.compile(r"^\s*[*_]*([+-]?\d[\d,]*(?:\.\d+)?)\s*(%?)\s*[*_†‡§¶∗\s]*(?:\(.*\))?\s*$")
_NUMBER = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?")
LOWER_BETTER = re.compile(r"time|latency|step|error|loss|perplexity|\bppl\b|cost|\bwer\b|seconds|\bsec\b", re.I)
_ABLATION = re.compile(r"^\s*(w/o|w/|without|with|[-−–+])[\s.]", re.I)  # "w/o. refinement", "- rewriting"
_OURS = re.compile(r"\bours?\b|our (method|model|approach|system|framework)|proposed (method|model|approach)", re.I)

SYSTEM_MIN = 0.75  # how well the system name must match a row (or column) label
FIELD_MIN = 0.6  # how well dataset, metric and setting must match to narrow the candidates


@dataclass
class Cell:
    table: str
    row: int
    col: int
    text: str
    value: float
    decimals: int
    percent: bool
    row_ctx: list[str] = field(default_factory=list)
    col_ctx: list[str] = field(default_factory=list)
    section: str = ""
    block: int = 0  # which header block: tables can stack sub-tables that reuse the same columns

    def context(self) -> list[str]:
        return self.row_ctx + self.col_ctx + ([self.section] if self.section else [])

    def where(self) -> dict[str, Any]:
        return {"table": self.table, "row": " / ".join(self.row_ctx), "column": " / ".join(self.col_ctx),
                "section": self.section, "text": self.text.strip()}


def parse_number(text: str) -> tuple[float, int, bool] | None:
    """(value, decimals, has %) for a cell or claimed value that is one number; None otherwise."""
    m = _CELL_NUMBER.match(text or "")
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    return float(raw), decimals, bool(m.group(2))


def claimed_number(text: str) -> tuple[float, int, bool] | None:
    """The first number of a claimed value ("7.0%", "0.77B", "+4.4") with its precision."""
    m = _NUMBER.search(text or "")
    if not m:
        return None
    raw = m.group(0).replace(",", "").lstrip("+")
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    return float(raw), decimals, "%" in text[m.end():m.end() + 2]


# ---- text matching ----------------------------------------------------------------------------


def _compact(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", s.lower())


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[0-9a-z]+", s.lower()))


def similarity(a: str, b: str) -> float:
    """1.0 same name ignoring case and punctuation; 0.9 one is an abbreviation of the
    other ("Bio" / "Biography", "ARC" / "Arc-Challenge"); else token overlap."""
    ca, cb = _compact(a), _compact(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    short, long_ = sorted((ca, cb), key=len)
    if len(short) >= 3 and long_.startswith(short) and not short.isdigit():
        return 0.9
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def best_match(name: str, texts: list[str]) -> float:
    """How well a name matches the best of the texts; a text such as "PopQA (Accuracy)"
    is also tried part by part ("PopQA", "Accuracy")."""
    best = 0.0
    for t in texts:
        parts = [t] + [p for p in re.split(r"[()\[\],/]| - ", t) if p.strip()]
        best = max(best, *(similarity(name, p) for p in parts))
    return best


# ---- tables as cells --------------------------------------------------------------------------


def _section_label(row: list[str]) -> str | None:
    """A full-width row such as "LLaMA2-hf-7b | LLaMA2-hf-7b | ..." (one distinct text, no number)."""
    texts = {c.strip() for c in row if c and c.strip()}
    if len(texts) == 1 and len([c for c in row if c and c.strip()]) >= 2 and parse_number(next(iter(texts))) is None:
        return next(iter(texts))
    return None


def index_table(table: dict[str, Any]) -> list[Cell]:
    grid = [[str(c or "") for c in row] for row in table.get("grid") or []]
    if not grid:
        return []
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    cells: list[Cell] = []
    header_block: list[list[str]] = []
    seen_data = False
    section = ""
    carried = [""] * width  # group labels written once and left blank below ("Types: Simple")
    base = ""  # the method an ablation row belongs to: the last row above that is not "w/o ..."
    block = 0
    for r, row in enumerate(grid):
        numeric = [c for c, text in enumerate(row) if parse_number(text) is not None]
        label = _section_label(row)
        following = grid[r + 1] if r + 1 < len(grid) else []
        # one text across the width is a section ("SelfRAG-LLaMA2-7b") when data follows it, and a
        # column header spanning every column ("SQuAD" over "EM | Step") when another header follows
        spans_header = bool(following) and _section_label(following) is None and \
            not any(parse_number(t) is not None for t in following)
        if label is not None and not spans_header:
            section = label
            continue
        if not numeric:
            if seen_data:  # a second header block further down (a stacked sub-table)
                header_block, seen_data, section, carried, base = [], False, "", [""] * width, ""
                block += 1
            header_block.append(row)
            continue
        seen_data = True
        first_num = min(numeric)
        row_ctx = []
        for c in range(first_num):
            text = row[c].strip()
            if text:
                carried[c] = text
                for k in range(c + 1, first_num):  # a new group resets the finer ones
                    carried[k] = ""
            if carried[c]:
                row_ctx.append(carried[c])
        label_text = row_ctx[-1] if row_ctx else ""
        if label_text and _ABLATION.match(label_text):
            if base:
                row_ctx.insert(len(row_ctx) - 1, base)
        elif label_text:
            base = label_text
        for c in numeric:
            if c < first_num:
                continue
            value, decimals, percent = parse_number(row[c])
            col_ctx = []
            for h in header_block:
                t = h[c].strip()
                if t and t not in col_ctx:
                    col_ctx.append(t)
            cells.append(Cell(table["id"], r, c, row[c], value, decimals, percent, list(row_ctx), col_ctx, section, block))
    return cells


def index_paper(paper: dict[str, Any]) -> dict[str, list[Cell]]:
    return {t["id"]: index_table(t) for t in paper.get("tables", [])}


# ---- locating a claim's cell ------------------------------------------------------------------


def _system_names(system: str, paper: dict[str, Any]) -> list[str]:
    """The claim's system, plus "(Ours)" when the claim says "our method"."""
    names = [system]
    if _OURS.search(system):
        names.append("Ours")
    return names


def _without(text: str, names: list[str]) -> str:
    for n in sorted({n for n in names if n and n.strip()}, key=len, reverse=True):
        text = re.sub(r"(?<![0-9A-Za-z])" + re.escape(n.strip()) + r"(?![0-9A-Za-z])", " ", text, flags=re.I)
    return text


def _parts(texts: list[str]) -> set[str]:
    out = set()
    for t in texts:
        out.update(p.strip() for p in [t, *re.split(r"[()\[\],/]| - ", t)] if _compact(p))
    return out


def text_score(cell: Cell, text: str, names: list[str] = (), others: list[str] = ()) -> int:
    """How much of the cell's context the claim's own words name; longer names weigh more.

    Row groups and the method an ablation belongs to are looked for in the whole
    text ("Self-CRAG w/o. refinement": the longer "Self-CRAG" beats "CRAG").
    Column headers and the section are looked for in the text without the
    system's and baseline's names, or "Single-step Approach" would pick the
    "Step" column. Short headers ("EM", "F1") must be whole words; a header may
    abbreviate a word of the text ("Bio" for "Biography"). ``others`` are the
    names of the other row of a comparison: "CRAG w/o. Correct beats Self-CRAG"
    must not place the ablation under Self-CRAG.
    """
    score = 0
    whole = _compact(_without(text, list(others)))  # not the other row's name: it is not this row's group
    for part in _parts(cell.row_ctx[:-1]):
        if len(_compact(part)) >= 3 and _compact(part) in whole:
            score += len(_compact(part))
    rest = _without(text, list(names) + cell.row_ctx)
    seq = " " + " ".join(re.findall(r"[0-9a-z]+", rest.lower())) + " "
    words, rest_tokens = _compact(rest), _tokens(rest)
    for part in _parts(cell.col_ctx + ([cell.section] if cell.section else [])):
        c, toks = _compact(part), re.findall(r"[0-9a-z]+", part.lower())
        if (" " + " ".join(toks) + " " in seq
                or (len(c) >= 5 and c in words)
                or (len(toks) == 1 and len(c) >= 3 and any(t.startswith(c) for t in rest_tokens))):
            score += len(c)
    return score


def narrow_by_text(cells: list[Cell], text: str, names: list[str] = ()) -> list[Cell]:
    if len(cells) < 2 or not text:
        return cells
    scores = [(text_score(c, text, names), c) for c in cells]
    best = max(s for s, _ in scores)
    return [c for s, c in scores if s == best] if best > 0 else cells


def locate(cells: list[Cell], system: str, dataset: str = "", metric: str = "", setting: str = "",
           paper: dict[str, Any] | None = None, text: str = "") -> list[Cell]:
    """Cells that fit the claim. The system is required; the other fields, then the claim's
    own words, only narrow the candidates."""
    if not system.strip():
        return []
    names = _system_names(system, paper or {})
    # the row's own label (or a column header, for transposed tables) counts fully; a label the
    # row only inherits (its group, or the method a "w/o ..." ablation belongs to) a little less,
    # so "Adaptive-RAG (Ours)" is its own row, not "Adaptive-RAG (Ours) / w/o Binary"
    scored = [(max(max(best_match(n, c.row_ctx[-1:] + c.col_ctx), 0.95 * best_match(n, c.row_ctx[:-1])) for n in names), c)
              for c in cells]
    top = max((s for s, _ in scored), default=0.0)
    if top < SYSTEM_MIN:
        return []
    found = [c for s, c in scored if s == top]
    for wanted in (setting, dataset, metric):
        if not wanted.strip():
            continue
        scores = [(best_match(wanted, c.context()), c) for c in found]
        best = max((s for s, _ in scores), default=0.0)
        if best >= FIELD_MIN:
            found = [c for s, c in scores if s == best]
    return narrow_by_text(found, text, [system])


def _close(computed: float, claimed: float, decimals: int, slack_units: float = 0.5) -> bool:
    return abs(computed - claimed) <= slack_units * 10 ** -decimals + 1e-9


def _same(cell: Cell, claimed: float, decimals: int) -> bool:
    """The cell shows the claimed number, at the claim's precision, as a percentage or a fraction."""
    return any(_close(round(v, max(decimals, cell.decimals)), claimed, decimals) for v in (cell.value, cell.value * 100))


def _tables_for(claim: dict[str, Any], index: dict[str, list[Cell]]) -> list[Cell]:
    refs = [r for r in claim.get("refers_to") or [] if r in index]
    pool = [c for r in refs for c in index[r]]
    return pool or [c for cs in index.values() for c in cs]


def check_value(claim: dict[str, Any], value: dict[str, Any], index: dict[str, list[Cell]],
                paper: dict[str, Any] | None = None) -> dict[str, Any]:
    parsed = claimed_number(value["value"])
    out: dict[str, Any] = {"kind": "value", "claimed": value["value"], "system": value.get("system", ""),
                           "dataset": value.get("dataset", ""), "metric": value.get("metric", ""),
                           "setting": value.get("setting", "")}
    if parsed is None:
        return out | {"verdict": "unverifiable", "note": "no number"}
    claimed, decimals, _ = parsed
    pool = _tables_for(claim, index)
    found = locate(pool, value.get("system", ""), value.get("dataset", ""), value.get("metric", ""),
                   value.get("setting", ""), paper, claim.get("claim", ""))
    if not found:
        return out | {"verdict": "unverifiable", "note": "no table cell matches the claim's system"}
    matching = [c for c in found if _same(c, claimed, decimals)]
    if matching:
        return out | {"verdict": "supported", "cell": matching[0].where(), "candidates": len(found)}
    if len(found) == 1:
        return out | {"verdict": "contradicted", "cell": found[0].where(), "table_says": found[0].text.strip(),
                      "note": f"the table gives {found[0].text.strip()}"}
    return out | {"verdict": "unverifiable", "note": f"{len(found)} cells fit and none shows {value['value']}",
                  "candidates": [c.where() for c in found[:5]]}


def check_difference(claim: dict[str, Any], index: dict[str, list[Cell]],
                     paper: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Recompute a stated margin from the system's and the baseline's cells; None if no margin is stated."""
    comp = claim.get("comparison") or {}
    parsed = claimed_number(comp.get("difference") or "")
    if parsed is None:
        return None
    claimed, decimals, _ = parsed
    values = claim.get("values") or []
    ctx = next((v for v in values if v.get("role") == "difference"), values[0] if values else {})
    dataset, metric, setting = ctx.get("dataset", ""), ctx.get("metric", ""), ctx.get("setting", "")
    out: dict[str, Any] = {"kind": "difference", "claimed": comp["difference"], "system": comp.get("system", ""),
                           "baseline": comp.get("baseline", ""), "dataset": dataset, "metric": metric,
                           "setting": setting, "direction": comp.get("direction", "")}
    pool = _tables_for(claim, index)
    sys_cells = locate(pool, comp.get("system", ""), dataset, metric, setting, paper)
    base_cells = locate(pool, comp.get("baseline", ""), dataset, metric, setting, paper)
    pairs = [(s, b) for s in sys_cells for b in base_cells
             if s.table == b.table and s.block == b.block and s.col == b.col and s.section == b.section and s.row != b.row]
    if len(pairs) > 1:  # the claim's words pick the column and the section
        names = [comp.get("system", ""), comp.get("baseline", "")]
        scored = [(text_score(s, claim.get("claim", ""), names + b.row_ctx, [comp.get("baseline", ""), *b.row_ctx[-1:]]), (s, b))
                  for s, b in pairs]
        best = max(sc for sc, _ in scored)
        pairs = [p for sc, p in scored if sc == best]
    if not pairs:
        return out | {"verdict": "unverifiable", "note": "the system and the baseline could not both be located in one column"}
    results = []
    for s, b in pairs:
        lower_better = bool(LOWER_BETTER.search(" ".join(s.col_ctx) + " " + metric))
        gain = (b.value - s.value) if lower_better else (s.value - b.value)
        absolute = round(abs(s.value - b.value), max(s.decimals, b.decimals))
        relative = abs(s.value - b.value) / abs(b.value) * 100 if b.value else None
        direction_ok = {"better": gain > 0, "worse": gain < 0, "equal": abs(gain) < 10 ** -decimals}.get(comp.get("direction", "better"), True)
        # A margin between two table values is exact, so it is compared at the claim's precision.
        # When the text says absolute (or relative), only that reading is accepted: otherwise a
        # wrong margin could pass as the other one by coincidence (E3).
        stated = comp.get("kind") if comp.get("kind") in ("absolute", "relative") else None
        kind = None
        if stated != "relative" and _close(round(absolute, decimals), claimed, decimals):
            kind = "absolute"
        elif stated != "absolute" and relative is not None and _close(round(relative, decimals), claimed, decimals, 1.0):
            kind = "relative"  # computed from rounded table values: one unit of slack
        results.append((kind is not None and direction_ok, kind, direction_ok, absolute, relative, s, b))
    results.sort(key=lambda x: (not x[0], x[1] is None))
    ok, kind, direction_ok, absolute, relative, s, b = results[0]
    detail = {"cell": s.where(), "baseline_cell": b.where(), "absolute": absolute,
              "relative": round(relative, 2) if relative is not None else None, "pairs": len(pairs)}
    if ok:
        note = f"{s.text.strip()} vs {b.text.strip()}: {kind} difference"
        return out | detail | {"verdict": "supported", "matched_as": kind, "note": note}
    if len(pairs) == 1 or len({(r[3], r[4]) for r in results}) == 1:
        why = "the direction is reversed" if (kind and not direction_ok) else \
            f"the table gives {s.text.strip()} vs {b.text.strip()}: {absolute} points" + \
            (f" ({relative:.1f}% relative)" if relative is not None else "")
        return out | detail | {"verdict": "contradicted", "note": why}
    return out | detail | {"verdict": "unverifiable", "note": f"{len(pairs)} column pairs fit and none gives {comp['difference']}"}


def check_claims(paper: dict[str, Any], claims: list[dict[str, Any]]) -> dict[str, Any]:
    """Every number of every claim, checked against the tables."""
    index = index_paper(paper)
    checks = []
    for i, claim in enumerate(claims):
        diff = check_difference(claim, index, paper)
        if diff is not None:
            checks.append({"claim": i, "sentence_id": claim.get("sentence_id"), **diff})
        for v in claim.get("values") or []:
            if v.get("role") == "difference" and diff is not None:
                continue  # already checked as the margin
            if not v.get("system"):
                continue  # a value of nothing in particular ("in total, 145,619 instances")
            checks.append({"claim": i, "sentence_id": claim.get("sentence_id"), **check_value(claim, v, index, paper)})
    summary = {"checks": len(checks)}
    for verdict in ("supported", "contradicted", "unverifiable"):
        summary[verdict] = sum(1 for c in checks if c["verdict"] == verdict)
    summary["cells_indexed"] = sum(len(v) for v in index.values())
    return {"summary": summary, "checks": checks}


def cells_as_dicts(cells: list[Cell]) -> list[dict[str, Any]]:
    return [asdict(c) for c in cells]
