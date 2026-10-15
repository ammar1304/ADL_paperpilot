"""scripts/e2_leave_one_out.py: reference matching, query selection, scoring without the paper itself."""

import importlib.util
import json
from types import SimpleNamespace
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


def test_dropped_connections_are_retried_then_reported(monkeypatch):
    calls = []

    def boom(req, timeout):
        calls.append(req.full_url)
        raise ConnectionResetError(10054, "connection reset by peer")

    monkeypatch.setattr(e2.urllib.request, "urlopen", boom)
    monkeypatch.setattr(e2.time, "sleep", lambda s: None)
    import pytest

    with pytest.raises(e2.TransientError, match="ConnectionResetError"):
        e2.s2_references("2310.11511", "key", retries=3)
    assert len(calls) == 3


def test_gold_sets_from_cached_references():
    refs = [
        {"arxiv": "2005.11401", "influential": False, "intents": ["background"]},
        {"arxiv": "2212.10509", "influential": True, "intents": ["methodology"]},
        {"arxiv": "2401.15884", "influential": True, "intents": []},
        {"arxiv": "9999.00001", "influential": True, "intents": ["methodology"]},  # not in the library
    ]
    library = {"arxiv:2005.11401", "arxiv:2212.10509", "arxiv:2401.15884"}
    kinds = e2.cited_by_kind(refs, library, "arxiv:self")
    assert kinds["cited"] == ["arxiv:2005.11401", "arxiv:2212.10509", "arxiv:2401.15884"]
    assert kinds["cited_influential"] == ["arxiv:2212.10509", "arxiv:2401.15884"]
    assert kinds["cited_method"] == ["arxiv:2212.10509"]


def test_pool_rows_cover_every_order_and_only_golds_the_paper_has():
    pool = [{"paper_id": p, "fused_rank": i, "rerank_rank": i, "rerank_score": 1 - i / 10}
            for i, p in enumerate(["arxiv:self", "arxiv:a", "arxiv:b"], start=1)]
    q = {"paper_id": "arxiv:self", "cited": ["arxiv:a", "arxiv:b"], "cited_influential": ["arxiv:b"], "cited_method": []}
    rows = e2.pool_rows(q, pool)
    assert {(r["gold"], r["method"]) for r in rows} == {(g, m) for g in ("all", "influential") for m in e2.ORDERS}
    assert next(r for r in rows if r["gold"] == "influential" and r["method"] == "hybrid")["ranks"] == {"arxiv:b": 2}


def test_score_stored_pools_against_new_golds(tmp_path, monkeypatch):
    folder = tmp_path / "run"
    folder.mkdir()
    pool = [{"paper_id": p, "fused_rank": i, "rerank_rank": i, "rerank_score": 0.5} for i, p in enumerate(["arxiv:a", "arxiv:b"], start=1)]
    (folder / "raw.jsonl").write_text(json.dumps({"paper_id": "arxiv:self", "pool": pool}) + "\n", encoding="utf-8")
    queries = tmp_path / "queries.json"
    queries.write_text(json.dumps({"meta": {}, "queries": [
        {"paper_id": "arxiv:self", "cited": ["arxiv:a", "arxiv:b"], "cited_influential": ["arxiv:a"], "cited_method": ["arxiv:a"]}]}))
    monkeypatch.setattr(e2, "REPO", tmp_path)
    args = SimpleNamespace(queries=queries, folder=folder, label="t")
    assert e2.score_stored(args) == 0
    summary = next((tmp_path / "eval" / "results").glob("*-e2-rescored-t")) / "summary.md"
    text = summary.read_text(encoding="utf-8")
    assert "| influential | hybrid | 1/1 (100%) |" in text
    assert "| all | fused | 2/2 (100%) |" in text
