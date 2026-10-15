"""scripts/retrieval_ab.py: reading stored runs and scoring ranks (the rankings need the real stack)."""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("retrieval_ab", REPO / "scripts" / "retrieval_ab.py")
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)

RUN1 = REPO / "eval" / "results" / "20261015-000708-prompt-ab" / "runs.jsonl"


def test_query_sets_from_the_first_real_e7_run():
    sets = ab.query_sets(RUN1)
    assert {s["idea"] for s in sets} == {
        "adaptive-retrieval", "iterative-rewrite", "retrieval-evaluator",
        "reflection-tokens", "hypothetical-document", "out-of-domain",
    }
    assert len({(s["idea"], tuple(s["queries"])) for s in sets}) == len(sets)  # no duplicates
    assert {s["p1"] for s in sets} == {"v1", "v2"}
    assert "small model predicts retrieval necessity for QA" in [q for s in sets for q in s["queries"]]
    assert all(len(s["as_run"]) == 6 for s in sets)
    ideas = {i["id"]: i for i in __import__("json").loads((REPO / "eval" / "prompt_ab" / "ideas.json").read_text())}
    expected_sets = [s for s in sets if ideas[s["idea"]]["expected"]]
    found, total = ab.as_run_at6(expected_sets, ideas)
    assert 0 < found < total  # the stored runs found some expected papers, not all


def test_ranks_and_aggregate():
    pool = [{"paper_id": f"arxiv:{i}"} for i in range(1, 31)]
    ranks = ab.expected_ranks(pool, ["arxiv:2", "arxiv:10", "arxiv:99"])
    assert ranks == {"arxiv:2": 2, "arxiv:10": 10, "arxiv:99": None}
    rows = [{"idea": "x", "method": "reranked", "ranks": ranks, "seconds": 4.0, "ranking_method": "reranked"},
            {"idea": "x", "method": "fused", "ranks": ranks, "seconds": 1.0, "ranking_method": "fused"}]
    a = ab.aggregate(rows, "reranked")
    assert (a["at_6"], a["in_pool"], a["expected"], a["not_reranked"]) == (1, 2, 3, 0)
    assert a["mrr"] == round((1 / 2 + 1 / 10) / 3, 3)
    table = ab.summarize(rows, ["x"])
    assert "| reranked | 1/3 | 2/3 |" in table and "| x | 1/3 | - | 1/3 | - |" in table
