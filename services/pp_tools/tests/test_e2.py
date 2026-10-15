"""scripts/e2_leave_one_out.py: reference matching, query selection, scoring without the paper itself."""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("e2", REPO / "scripts" / "e2_leave_one_out.py")
e2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2)


def test_cited_papers_are_kept_only_if_in_the_library():
    refs = [
        {"citedPaper": {"externalIds": {"ArXiv": "2005.11401"}}},
        {"citedPaper": {"externalIds": {"ArXiv": "1111.11111"}}},  # not in the library
        {"citedPaper": {"externalIds": {"DOI": "10.1/x"}}},  # no arXiv id
        {"citedPaper": None},
        {"citedPaper": {"externalIds": {"ArXiv": "2005.11401"}}},  # duplicate
        {"citedPaper": {"externalIds": {"ArXiv": "2310.11511"}}},  # the paper itself
    ]
    library = {"arxiv:2005.11401", "arxiv:2310.11511"}
    assert e2.cited_in_library(refs, library, "arxiv:2310.11511") == ["arxiv:2005.11401"]


def test_query_selection_is_reproducible():
    cands = [{"paper_id": f"arxiv:{i}", "cited": ["x"] * (i % 5)} for i in range(50)]
    a = e2.select_queries(cands, min_cited=3, limit=10, seed=13)
    assert a == e2.select_queries(cands, min_cited=3, limit=10, seed=13)
    assert len(a) == 10 and all(len(c["cited"]) >= 3 for c in a)


def test_the_paper_itself_is_removed_before_ranking():
    order = ["arxiv:self", "arxiv:a", "arxiv:b", "arxiv:c"]
    assert e2.ranks_without_self(order, "arxiv:self", ["arxiv:b", "arxiv:z"]) == {"arxiv:b": 2, "arxiv:z": None}


def test_summary_micro_and_macro():
    rows = [
        {"paper_id": "p1", "method": "hybrid", "ranks": {"a": 1, "b": None}},
        {"paper_id": "p2", "method": "hybrid", "ranks": {"c": 7, "d": 2, "e": 40}},
    ]
    table = e2.summarize(rows, ["hybrid"])
    assert "| hybrid | 2/5 (40%) | 3/5 |" in table
    assert "| 0.42 | 2 |" in table  # macro recall@6 = (1/2 + 1/3) / 2
