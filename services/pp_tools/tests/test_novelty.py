"""Novelty branch steps against a real Postgres and a stub-built knowledge base (needs TEST_DATABASE_URL).

The judge's output is produced by the stub model's rules (tests/stubs.py), so
these tests exercise the contracts, retrieval fusion, the verifier and the
retry-with-feedback logic, not the quality of a real LLM.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import stubs
from app.config import Settings
from app.corpus.arxiv import ArxivClient
from app.corpus.build import CorpusBuilder
from app.db import Database
from app.main import create_app
from app.novelty import (
    NoveltyError,
    RetrievalConfig,
    assemble,
    judge_prompt,
    retrieve,
    save_facets,
    verify,
)
from app.prompts import PromptLibrary
from app.retrieval.embedder import Embedder
from app.retrieval.search import Retriever
from app.retrieval.store import Store, make_client
from test_build_search_eval import write_stub_manifest

REPO = Path(__file__).resolve().parents[3]
IDEA = "Rewrite the search query after every retrieval step to answer multi-hop questions on HotpotQA."


@pytest.fixture(scope="module")
def stub_url():
    server = stubs.serve()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="module")
def kb(tmp_path_factory, stub_url):
    tmp = tmp_path_factory.mktemp("kb")
    settings = Settings(
        APP_DATABASE_URL="postgresql://unused@127.0.0.1:1/x",
        QDRANT_URL="local::memory:",
        EMBEDDER_URL=stub_url,
        CORPUS_DIR=tmp / "corpus",
        CACHE_DIR=tmp / "cache",
        ARXIV_API_URL=f"{stub_url}/api/query",
        ARXIV_PDF_URL=f"{stub_url}/pdf",
        ARXIV_DELAY_S=0,
        CHUNK_TOKENS=120,
        CHUNK_OVERLAP=20,
    )
    write_stub_manifest(settings, {"2101.00001", "2203.00002"})
    embedder = Embedder(stub_url)
    store = Store(make_client("local::memory:"))
    CorpusBuilder(settings, ArxivClient(settings.arxiv_api_url, settings.arxiv_pdf_url, 0), embedder, store).run()
    return Retriever(embedder, store)


@pytest.fixture
def db(test_db_url):
    d = Database(test_db_url, 2.0)
    d.open()
    d.apply_schema()
    with d.pool.connection() as conn:
        conn.execute("DELETE FROM runs")
    yield d
    d.close()


def new_run(db: Database, idea: str = IDEA):
    return db.create_run("novelty", {"mode": "novelty", "idea_text": idea}, "exec-test")


def decompose(idea: str) -> dict:
    """What the stub model returns for the decomposer (already unwrapped by n8n's parser)."""
    return stubs._decompose(idea)


def run_to_judge(db, kb, idea=IDEA):
    run_id = new_run(db, idea)
    save_facets(db, run_id, decompose(idea), "v1")
    retrieve(db, kb, run_id, RetrievalConfig(top_papers=3), {"papers_in_knowledge_base": 3})
    rendered, attempt = judge_prompt(db, PromptLibrary(REPO / "prompts"), run_id)
    return run_id, rendered, attempt


def test_save_facets_validates_and_stores(db):
    run_id = new_run(db)
    out = save_facets(db, run_id, decompose(IDEA), "v1")
    assert out.facets.data == "not specified"
    assert db.get_steps(run_id, "decompose")[0]["detail"]["prompt_version"] == "v1"
    bad = decompose(IDEA)
    bad["facets"]["invented"] = "x"
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        save_facets(db, run_id, bad, "v1")


def test_retrieve_fuses_queries_into_candidates_with_evidence(db, kb):
    run_id = new_run(db)
    save_facets(db, run_id, decompose(IDEA), "v1")
    detail = retrieve(db, kb, run_id, RetrievalConfig(top_papers=3, evidence_per_paper=2), {"papers_in_knowledge_base": 3})
    cands = detail["candidates"]
    assert 1 <= len(cands) <= 3
    assert len({c["paper_id"] for c in cands}) == len(cands)
    for c in cands:
        assert 1 <= len(c["evidence"]) <= 2
        assert all(e["chunk_id"].startswith(c["paper_id"] + "#") for e in c["evidence"])
    scores = [c["fused_score"] for c in cands]
    assert scores == sorted(scores, reverse=True)


def test_steps_out_of_order_are_refused(db, kb):
    run_id = new_run(db)
    with pytest.raises(NoveltyError, match="no 'decompose' step"):
        retrieve(db, kb, run_id, RetrievalConfig(), {})
    with pytest.raises(LookupError):
        save_facets(db, __import__("uuid").uuid4(), decompose(IDEA), "v1")


def test_honest_judge_passes_first_time(db, kb):
    run_id, rendered, attempt = run_to_judge(db, kb)
    assert attempt == 1 and "FEEDBACK" not in rendered.user
    assert "paper_id: arxiv:" in rendered.user and "- method:" in rendered.user
    result = verify(db, run_id, stubs._judge(rendered.user))
    assert result["status"] == "pass" and result["rejected"] == []
    final = assemble(db, run_id)
    assert final["implemented"] is True
    assert final["closest_work"] and final["unverified"] == []
    assert final["closest_work"][0]["url"].startswith("https://arxiv.org/abs/")
    assert final["prompt_versions"] == {"P1-decomposer": "v1", "P3-novelty-judge": "v1"}
    assert "does not mean novel" in final["coverage"]["note"]


def test_hallucinated_paper_is_rejected_then_fixed_with_feedback(db, kb):
    idea = "HALLUCINATE " + IDEA
    run_id, rendered, _ = run_to_judge(db, kb, idea)
    first = verify(db, run_id, stubs._judge(rendered.user))
    assert first["status"] == "retry"
    assert any("arxiv:9999.99999 was not among the retrieved candidates" in r for item in first["rejected"] for r in item["reasons"])
    with pytest.raises(NoveltyError, match="retry"):
        assemble(db, run_id)  # cannot finish while a retry is pending

    rendered2, attempt = judge_prompt(db, PromptLibrary(REPO / "prompts"), run_id)
    assert attempt == 2 and "FEEDBACK ON YOUR PREVIOUS ANSWER" in rendered2.user
    assert "arxiv:9999.99999 was not among the retrieved candidates" in rendered2.user
    second = verify(db, run_id, stubs._judge(rendered2.user))
    assert second["status"] == "pass"
    assert assemble(db, run_id)["judge_attempts"] == 2


def test_stubborn_hallucination_is_reported_not_hidden(db, kb):
    run_id, rendered, _ = run_to_judge(db, kb, "STUBBORN " + IDEA)
    verify(db, run_id, stubs._judge(rendered.user))
    rendered2, _ = judge_prompt(db, PromptLibrary(REPO / "prompts"), run_id)
    second = verify(db, run_id, stubs._judge(rendered2.user))
    assert second["status"] == "final"
    final = assemble(db, run_id)
    assert [u["item"]["paper_id"] for u in final["unverified"]] == ["arxiv:9999.99999"]
    assert all(m["paper_id"] != "arxiv:9999.99999" for m in final["closest_work"])
    with pytest.raises(NoveltyError, match="already used 2"):
        judge_prompt(db, PromptLibrary(REPO / "prompts"), run_id)


def test_verifier_checks_chunk_ownership_and_evidence(db, kb):
    run_id, rendered, _ = run_to_judge(db, kb)
    output = stubs._judge(rendered.user)
    real, other = output["matches"][0], output["matches"][1]
    forged = dict(real, evidence=[other["evidence"][0]])  # a chunk of another paper
    no_evidence = dict(other, evidence=[])  # 'same' on task without evidence
    result = verify(db, run_id, {"matches": [forged, no_evidence], "summary": "s s s"})
    reasons = [r for item in result["rejected"] for r in item["reasons"]]
    assert any("was not shown for paper" in r for r in reasons)
    assert any("need at least one evidence chunk" in r for r in reasons)


def test_malformed_judge_output_is_rejected_as_a_whole(db, kb):
    run_id, _, _ = run_to_judge(db, kb)
    result = verify(db, run_id, {"matches": [{"paper_id": "x"}]})
    assert result["status"] == "retry"
    assert result["rejected"][0]["item"] is None
    assert result["rejected"][0]["reasons"][0].startswith("invalid output format")


def test_novelty_api_end_to_end(tmp_path, test_db_url, stub_url, db):
    """The same steps through HTTP, as n8n calls them."""
    settings = Settings(
        APP_DATABASE_URL=test_db_url,
        QDRANT_URL="local::memory:",
        EMBEDDER_URL=stub_url,
        CORPUS_DIR=tmp_path / "corpus",
        CACHE_DIR=tmp_path / "cache",
        PROMPTS_DIR=REPO / "prompts",
        ARXIV_API_URL=f"{stub_url}/api/query",
        ARXIV_PDF_URL=f"{stub_url}/pdf",
        ARXIV_DELAY_S=0,
        CHUNK_TOKENS=120,
        CHUNK_OVERLAP=20,
        LOG_LEVEL="WARNING",
    )
    write_stub_manifest(settings, {"2101.00001"})
    with TestClient(create_app(settings)) as client:
        assert client.get("/prompts").json() == {"active": {"P1-decomposer": "v1", "P3-novelty-judge": "v1"}, "problems": []}
        r = client.post("/prompts/P1-decomposer/render", json={"variables": {"idea_text": IDEA}})
        assert r.status_code == 200 and IDEA in r.json()["user"] and r.json()["version"] == "v1"
        assert client.post("/prompts/P1-decomposer/render", json={"variables": {}}).status_code == 422
        assert client.post("/prompts/P7-nope/render", json={}).status_code == 404

        run_id = str(new_run(db))
        assert client.post(f"/novelty/{run_id}/retrieve", json={}).status_code == 409  # no collection yet

        job = client.post("/corpus/build", json={}).json()["job_id"]
        import time

        for _ in range(100):
            if client.get(f"/jobs/{job}").json()["status"] == "done":
                break
            time.sleep(0.2)

        assert client.post(f"/novelty/{run_id}/retrieve", json={}).status_code == 409  # no facets yet
        bad = client.post(f"/novelty/{run_id}/facets", json={"output": {"facets": {}}, "prompt_version": "v1"})
        assert bad.status_code == 422 and bad.json()["errors"]
        ok = client.post(f"/novelty/{run_id}/facets", json={"output": decompose(IDEA), "prompt_version": "v1"})
        assert ok.status_code == 200
        ret = client.post(f"/novelty/{run_id}/retrieve", json={}).json()
        assert ret["coverage"]["papers_in_knowledge_base"] == 3
        prompt = client.post(f"/novelty/{run_id}/judge-prompt").json()
        assert prompt["attempt"] == 1
        v = client.post(f"/novelty/{run_id}/verify", json={"output": stubs._judge(prompt["user"])}).json()
        assert v["status"] == "pass"
        result = client.post(f"/novelty/{run_id}/result").json()
        assert result["branch"] == "novelty" and result["closest_work"]
        steps = client.get(f"/runs/{run_id}/steps").json()
        assert [s["step_name"] for s in steps] == ["decompose", "retrieve", "judge_prompt", "verify"]
