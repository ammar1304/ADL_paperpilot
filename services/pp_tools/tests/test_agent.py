"""The search agent (D29): planner prompt, plan execution, Semantic Scholar, guards.

Uses the stub library, stub Semantic Scholar and stub planner (tests/stubs.py);
needs TEST_DATABASE_URL like test_novelty.py.
"""

import pytest

import stubs
from app.external.semantic_scholar import SemanticScholar, SemanticScholarError
from app.novelty import (
    RetrievalConfig,
    apply_plan,
    assemble,
    judge_prompt,
    plan_prompt,
    retrieve,
    save_facets,
    verify,
)
from app.prompts import PromptLibrary
from test_novelty import REPO, db, decompose, kb, new_run, stub_url  # noqa: F401  (fixtures)

IDEA = "Rewrite the search query after every retrieval step to answer multi-hop questions on HotpotQA."
COVERAGE = {"papers_in_knowledge_base": 3, "papers_with_full_text": 2, "chunks_searched": 20}
LIB = PromptLibrary(REPO / "prompts")


@pytest.fixture
def s2(stub_url):  # noqa: F811
    client = SemanticScholar(f"{stub_url}/graph/v1", min_interval_s=0.0, retries=2)
    yield client
    client.close()


def start(db, kb, idea=IDEA):  # noqa: F811
    run_id = new_run(db, idea)
    save_facets(db, run_id, decompose(idea), "v2")
    retrieve(db, kb, run_id, RetrievalConfig(top_papers=3), COVERAGE)
    return run_id


def planner(rendered: dict) -> dict:
    """What n8n's "Plan search" node returns: the stub model's answer, already unwrapped."""
    return stubs._plan(rendered["user"])


def test_semantic_scholar_client(s2):
    papers = s2.search("query rewriting", 10)
    assert [p.paper_id for p in papers] == ["arxiv:2203.00002", "s2:s2bbb"]  # no-abstract paper skipped
    assert papers[0].url == "https://arxiv.org/abs/2203.00002" and papers[1].url == "https://s2/bbb"
    hit = papers[1].as_hit()
    assert hit.chunk_id == "s2:s2bbb#abs" and hit.chunk_type == "abstract" and "Abstract." in hit.text
    with pytest.raises(SemanticScholarError):
        s2.search("S2_DOWN anything", 10)


def test_planner_prompt_shows_state_and_budget(db, kb):  # noqa: F811
    run_id = start(db, kb)
    rendered = plan_prompt(db, LIB, run_id, max_rounds=2)
    assert rendered["run"] is True and rendered["round"] == 1 and rendered["version"] == "v1"
    assert "ROUND 1 of 2. Searches left after this one: 1." in rendered["user"]
    assert "source: local; relevance" in rendered["user"]  # candidates with where they came from
    assert "Decomposer queries (local library):" in rendered["user"]
    assert plan_prompt(db, LIB, run_id, max_rounds=0)["run"] is False  # agent switched off


def test_agent_searches_then_stops_and_the_judge_sees_web_papers(db, kb, s2):  # noqa: F811
    run_id = start(db, kb)
    r1 = plan_prompt(db, LIB, run_id, 2)
    out1 = apply_plan(db, kb, None, s2, run_id, planner(r1), RetrievalConfig(top_papers=4), COVERAGE, 2)
    assert out1["continue"] is True and out1["decision"] == "search"
    assert "s2:s2bbb" in out1["new_in_pool"]
    web = next(q for q in out1["queries"] if q["where"] == "web")
    assert web["new_papers"] == 1 and web["results_count"] == 2  # the arXiv duplicate is not new

    latest = db.get_steps(run_id, "retrieve")[-1]["detail"]
    sources = {c["paper_id"]: c["source"] for c in latest["pool"]}
    assert sources["s2:s2bbb"] == "semantic_scholar"
    assert sources["arxiv:2203.00002"] == "local+semantic_scholar"  # one paper, two sources
    assert latest["web_queries"] and latest["round"] == 1

    r2 = plan_prompt(db, LIB, run_id, 2)
    assert r2["round"] == 2 and "Round 1, web:" in r2["user"] and "-> 1 new papers" in r2["user"]
    out2 = apply_plan(db, kb, None, s2, run_id, planner(r2), RetrievalConfig(top_papers=4), COVERAGE, 2)
    assert out2["continue"] is False and out2["stop_reason"] == "planner decided to stop"
    assert plan_prompt(db, LIB, run_id, 2)["run"] is False

    rendered, _ = judge_prompt(db, LIB, run_id)
    assert "on Semantic Scholar" in rendered.user
    if "s2:s2bbb" in [c["paper_id"] for c in latest["candidates"]]:
        assert "| found on Semantic Scholar" in rendered.user
    assert verify(db, run_id, stubs._judge(rendered.user))["status"] == "pass"  # s2 evidence ids are accepted
    final = assemble(db, run_id)
    assert [r["decision"] for r in final["search_agent"]] == ["search", "stop"]
    assert final["coverage"]["web_queries"] and "Semantic Scholar" in final["summary"]
    assert final["prompt_versions"]["P2-search-planner"] == "v1"


def test_round_limit_repeated_queries_and_no_new_papers(db, kb, s2):  # noqa: F811
    run_id = start(db, kb, "PLAN_LOOP " + IDEA)
    r1 = plan_prompt(db, LIB, run_id, 2)
    plan = planner(r1)
    assert apply_plan(db, kb, None, s2, run_id, plan, RetrievalConfig(), COVERAGE, 2)["continue"] is True
    # The same queries again: skipped, nothing new, so the loop stops on its own.
    again = apply_plan(db, kb, None, s2, run_id, plan, RetrievalConfig(), COVERAGE, 2)
    assert all(q.get("skipped") == "already searched" for q in again["queries"])
    assert again["continue"] is False and again["stop_reason"] == "the round found no new paper"
    # A third round is refused whatever the planner says.
    third = apply_plan(db, kb, None, s2, run_id, planner(r1), RetrievalConfig(), COVERAGE, 2)
    assert third["continue"] is False and third["stop_reason"] == "round limit reached"


def test_invalid_plan_ends_the_search_without_failing_the_run(db, kb, s2):  # noqa: F811
    run_id = start(db, kb)
    out = apply_plan(db, kb, None, s2, run_id, {"decision": "maybe"}, RetrievalConfig(), COVERAGE, 2)
    assert out["continue"] is False and out["decision"] == "invalid" and out["errors"]
    assert plan_prompt(db, LIB, run_id, 2)["run"] is False
    judge_prompt(db, LIB, run_id)  # the judge still runs on the first retrieval


def test_semantic_scholar_outage_is_recorded_and_local_search_still_runs(db, kb, s2):  # noqa: F811
    run_id = start(db, kb, "S2_DOWN " + IDEA)
    out = apply_plan(db, kb, None, s2, run_id, planner(plan_prompt(db, LIB, run_id, 2)), RetrievalConfig(), COVERAGE, 2)
    web = next(q for q in out["queries"] if q["where"] == "web")
    assert web["error"].startswith("search failed")
    local = next(q for q in out["queries"] if q["where"] == "local")
    assert "error" not in local


def test_agent_through_the_api(tmp_path, test_db_url, stub_url, db):  # noqa: F811
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app
    from test_build_search_eval import write_stub_manifest

    settings = Settings(
        APP_DATABASE_URL=test_db_url, QDRANT_URL="local::memory:", EMBEDDER_URL=stub_url, RERANKER_URL=stub_url,
        SEMANTIC_SCHOLAR_URL=f"{stub_url}/graph/v1", SEMANTIC_SCHOLAR_INTERVAL_S=0, CORPUS_DIR=tmp_path / "corpus",
        CACHE_DIR=tmp_path / "cache", PROMPTS_DIR=REPO / "prompts", ARXIV_API_URL=f"{stub_url}/api/query",
        ARXIV_PDF_URL=f"{stub_url}/pdf", ARXIV_DELAY_S=0, CHUNK_TOKENS=120, CHUNK_OVERLAP=20, LOG_LEVEL="WARNING",
    )
    write_stub_manifest(settings, {"2101.00001"})
    with TestClient(create_app(settings)) as client:
        import time

        job = client.post("/corpus/build", json={}).json()["job_id"]
        for _ in range(100):
            if client.get(f"/jobs/{job}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        run_id = str(new_run(db))
        client.post(f"/novelty/{run_id}/facets", json={"output": decompose(IDEA), "prompt_version": "v2"})
        client.post(f"/novelty/{run_id}/retrieve", json={})
        rendered = client.post(f"/novelty/{run_id}/plan-prompt").json()
        assert rendered["run"] is True and rendered["system"].startswith("You are the search planner")
        out = client.post(f"/novelty/{run_id}/plan", json={"output": stubs._plan(rendered["user"])}).json()
        assert out["continue"] is True and "results" not in out["queries"][1]  # raw results stay in the step
        steps = [s_["step_name"] for s_ in client.get(f"/runs/{run_id}/steps").json()]
        assert steps == ["decompose", "retrieve", "plan_prompt", "retrieve", "plan"]
