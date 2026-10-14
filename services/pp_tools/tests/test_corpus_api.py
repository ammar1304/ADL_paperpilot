"""HTTP API for corpus builds, jobs, stats and search (needs TEST_DATABASE_URL)."""

import time

import pytest
from fastapi.testclient import TestClient

import stubs
from app.config import Settings
from app.main import create_app
from test_build_search_eval import write_stub_manifest


@pytest.fixture(scope="module")
def stub_url():
    server = stubs.serve()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def api(tmp_path, test_db_url, stub_url):
    settings = Settings(
        APP_DATABASE_URL=test_db_url,
        QDRANT_URL="local::memory:",
        EMBEDDER_URL=stub_url,
        CORPUS_DIR=tmp_path / "corpus",
        CACHE_DIR=tmp_path / "cache",
        ARXIV_API_URL=f"{stub_url}/api/query",
        ARXIV_PDF_URL=f"{stub_url}/pdf",
        ARXIV_DELAY_S=0,
        CHUNK_TOKENS=120,
        CHUNK_OVERLAP=20,
        LOG_LEVEL="WARNING",
    )
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.pool.connection() as conn:
            conn.execute("DELETE FROM jobs")
        yield client, settings


def wait_for_job(client: TestClient, job_id: str, timeout_s: float = 60) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.3)
    raise AssertionError(f"job {job_id} did not finish in {timeout_s}s")


def test_build_without_manifest_is_a_clear_400(api):
    client, _ = api
    resp = client.post("/corpus/build", json={})
    assert resp.status_code == 400
    assert "run corpus selection first" in resp.json()["detail"]


def test_search_before_any_build_is_409(api):
    client, _ = api
    resp = client.post("/search", json={"query": "anything at all", "strategy": "fixed"})
    assert resp.status_code == 409


def test_build_job_then_search_and_stats(api):
    client, settings = api
    write_stub_manifest(settings, {"2405.00003"})

    resp = client.post("/corpus/build", json={"recreate": True, "n8n_execution_id": "exec-42"})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    job = wait_for_job(client, job_id)
    assert job["status"] == "done", job
    assert job["n8n_execution_id"] == "exec-42"
    assert job["result"]["papers_indexed"] == 3
    assert job["result"]["fulltext_indexed"] == 1

    resp = client.post(
        "/search",
        json={"query": "citation faithfulness metric", "strategy": "structured", "k": 2, "level": "paper"},
    )
    assert resp.status_code == 200
    hits = resp.json()["hits"]
    assert hits[0]["paper_id"] == "arxiv:2405.00003"
    assert hits[0]["chunk_id"].startswith("arxiv:2405.00003#")

    resp = client.post(
        "/search",
        json={"query": "citation", "strategy": "structured", "level": "paper", "filters": {"paper_id": "arxiv:2101.00001"}},
    )
    assert [h["paper_id"] for h in resp.json()["hits"]] == ["arxiv:2101.00001"]

    resp = client.post(
        "/search",
        json={"query": "results table", "strategy": "structured", "filters": {"chunk_type": "table"}},
    )
    assert [h["chunk_type"] for h in resp.json()["hits"]] == ["table"]

    # The evaluation harness measures the deployed search API the same way.
    from app.eval.retrieval import ApiRetriever, Query, evaluate

    queries = [Query("q1", "citation faithfulness metric", {"arxiv:2405.00003"}, "paraphrase", "dev")]
    summary = evaluate(ApiRetriever("http://testserver", client=client), queries, ["fixed", "structured"], [1])
    assert summary["overall"]["structured"]["recall@1"] == 1.0

    stats = client.get("/corpus/stats").json()
    assert stats["manifest"]["papers"] == 3 and stats["manifest"]["fulltext"] == 1
    assert stats["collections"]["structured"]["papers"] == 3
    assert stats["last_build"]["papers_indexed"] == 3


def test_second_build_while_one_is_active_is_409(api):
    client, settings = api
    write_stub_manifest(settings, set())
    # Simulate an active job without racing a real one.
    with client.app.state.db.pool.connection() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, kind, status) VALUES (gen_random_uuid(), 'corpus_build', 'running')"
        )
    assert client.post("/corpus/build", json={}).status_code == 409


def test_interrupted_jobs_are_failed_at_startup(api, test_db_url, tmp_path, stub_url):
    client, settings = api
    with client.app.state.db.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO jobs (job_id, kind, status) VALUES (gen_random_uuid(), 'corpus_build', 'running') RETURNING job_id"
        ).fetchone()
    with TestClient(create_app(settings)) as restarted:
        job = restarted.get(f"/jobs/{row[0]}").json()
    assert job["status"] == "failed"
    assert "restarted" in job["error"]["message"]


def test_search_validation(api):
    client, _ = api
    assert client.post("/search", json={"query": "ab"}).status_code == 422
    assert client.post("/search", json={"query": "valid query", "strategy": "semantic"}).status_code == 422
    assert client.post("/search", json={"query": "valid query", "k": 51}).status_code == 422
