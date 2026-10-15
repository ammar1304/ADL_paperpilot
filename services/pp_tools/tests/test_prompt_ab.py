"""Scoring logic of scripts/prompt_ab.py (the runs themselves need the real stack)."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "prompt_ab.py"
spec = importlib.util.spec_from_file_location("prompt_ab", SCRIPT)
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)


def test_parse_config():
    assert ab.parse_config("base=P1-decomposer:v1,P3-novelty-judge:v2") == (
        "base",
        {"P1-decomposer": "v1", "P3-novelty-judge": "v2"},
    )
    with pytest.raises(ValueError):
        ab.parse_config("base=P1-decomposer:latest")


def test_query_similarity_flags_paraphrases():
    # The four v1 queries from the first known-answer run (F-005).
    v1 = [
        "small model predicts retrieval necessity for QA",
        "retrieval decision model for question answering",
        "predicting retrieval requirement in open-domain QA",
        "learning to skip retrieval in QA systems",
    ]
    varied = ["adaptive retrieval for question answering", "when to retrieve", "classifier trained on question complexity labels"]
    assert ab.query_similarity(v1) > ab.query_similarity(varied)
    assert ab.query_similarity(["one query"]) is None


@pytest.mark.parametrize(
    "text, scope, overclaim, mentions, coverage_claim",
    [
        # F-002 and F-003, verbatim from run e36edefe (v1)
        ("Consequently, the task of deciding retrieval necessity per question and the method of training a small "
         "predictor remain largely uncovered.", False, True, False, False),
        ("However, none of the examined papers provide details on the specific datasets or concrete results for this "
         "prediction problem, leaving the data and result facets largely uncovered.", False, True, True, True),
        # E7 run 1, baseline: coverage claim through "those facets"
        ("However, the idea's data and result aspects are not addressed in the presented passages. The remaining "
         "papers only address the general RAG task, leaving those facets uncovered.", False, True, True, True),
        # v2, run 6d4001f3: clean
        ("Among the 531 papers searched, arxiv:2604.07985 overlaps with the idea on both the task and the method.",
         True, False, False, False),
        # E7 run 1, judge-v2: a neutral mention, not a coverage claim
        ("Among the 531 papers searched, C1 overlaps on task and method. No passages provide information on data or "
         "results for any paper.", True, False, True, False),
    ],
)
def test_summary_checks_catch_the_logged_failures(text, scope, overclaim, mentions, coverage_claim):
    got = ab.summary_checks(text, ["data", "result"])
    assert got == {
        "llm_states_scope": scope,
        "llm_overclaims": overclaim,
        "llm_discusses_unspecified": mentions,
        "llm_coverage_claim_unspecified": coverage_claim,
    }


def test_score_and_label_agreement():
    idea = {"expected": ["arxiv:1", "arxiv:2"]}
    match = {"paper_id": "arxiv:1", "overlap": {"task": "same", "method": "partial", "data": "unknown", "result": "unknown"}}
    other = {"paper_id": "arxiv:3", "overlap": {"task": "different", "method": "different", "data": "unknown", "result": "unknown"}}
    result = {"closest_work": [match], "checked_not_close": [other], "overlap_found": True, "judge_attempts": 1,
              "summary_llm_written": "Among the 10 papers searched, arxiv:1 overlaps.", "unspecified_facets": ["data", "result"]}
    row = ab.score(idea, result, ["arxiv:1", "arxiv:3"], ["a b", "c d"])
    assert row["expected_in_candidates"] == 1 and row["expected_in_closest"] == 1 and row["false_overlap"] is False
    assert ab.score({"expect_no_overlap": True}, result, [], [])["false_overlap"] is True

    changed = {**result, "closest_work": [{**match, "overlap": {**match["overlap"], "method": "unknown"}}]}
    assert ab.label_agreement([result, result]) == 1.0
    assert ab.label_agreement([result, changed]) == 7 / 8
