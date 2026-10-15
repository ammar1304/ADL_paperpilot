"""scripts/order_replay.py: ordering rules on stored pools."""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("order_replay", REPO / "scripts" / "order_replay.py")
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)


def _p(pid, fused, rr, score):
    return {"paper_id": pid, "fused_rank": fused, "rerank_rank": rr, "rerank_score": score}


POOL = [_p(f"p{i}", i, i + 1, 0.3 - i / 100) for i in range(1, 20)] + [_p("selfrag", 20, 1, 0.82)]


def test_hybrid60_leaves_a_strong_but_badly_fused_paper_out():
    assert "selfrag" not in rp.order(POOL, "hybrid60")[: rp.TOP]


def test_guard_gives_it_a_slot_and_fused_ignores_it():
    assert rp.order(POOL, "guard0.5")[0] == "selfrag"
    assert rp.order(POOL, "fused")[-1] == "selfrag"
    assert rp.order(POOL, "reranked")[0] == "selfrag"


def test_score_counts_gold_in_top_6():
    s = rp.score([{"pool": POOL, "gold": {"selfrag", "p1"}}], "guard0.5")
    assert s["found"] == 2 and s["total"] == 2


def test_stored_results_load():
    d = REPO / "eval" / "results"
    cases = rp.load_cases(d / "20261015-142008-retrieval-ab" / "pools.jsonl",
                          d / "20261015-213516-agent-off" / "runs.jsonl",
                          d / "20261015-211913-e2-retrieval" / "raw.jsonl")
    assert len(cases["e8"]) == 25 and len(cases["e7"]) == 10 and len(cases["e2"]) == 40
    assert rp.score(cases["e8"], "hybrid60")["found"] == 37  # E8 run 2, D28
