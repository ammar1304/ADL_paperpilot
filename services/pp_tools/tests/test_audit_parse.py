"""Audit parse job through the API: download, docling-serve (stub), normalise, cache, endpoints."""

import time
from pathlib import Path

import pytest

import stubs
from app.audit.parser import FetchError, fetch_pdf

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def stub_url():
    server = stubs.serve()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_fetch_refuses_private_addresses_and_non_pdfs(stub_url):
    with pytest.raises(FetchError, match="not a public address"):
        fetch_pdf(f"{stub_url}/pdf/2101.00001v2", 10_000_000)
    with pytest.raises(FetchError, match="only http"):
        fetch_pdf("file:///etc/passwd", 10_000_000, allow_private=True)
    with pytest.raises(FetchError, match="did not return a PDF"):
        fetch_pdf(f"{stub_url}/info", 10_000_000, allow_private=True)
    with pytest.raises(FetchError, match="larger than"):
        fetch_pdf(f"{stub_url}/pdf/2101.00001v2", 1_000, allow_private=True)
    assert fetch_pdf(f"{stub_url}/pdf/2101.00001v2", 10_000_000, allow_private=True).startswith(b"%PDF")


def _wait(client, job_id):
    for _ in range(100):
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_parse_job_end_to_end_and_cache(tmp_path, test_db_url, stub_url):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        APP_DATABASE_URL=test_db_url, QDRANT_URL="local::memory:", EMBEDDER_URL=stub_url, PARSER_URL=stub_url,
        AUDIT_ALLOW_PRIVATE_URLS=True, CACHE_DIR=tmp_path / "cache", CORPUS_DIR=tmp_path / "corpus",
        PROMPTS_DIR=REPO / "prompts", LOG_LEVEL="WARNING",
    )
    url = f"{stub_url}/pdf/2101.00001v2"
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json()["checks"]["parser"]["ok"] is True
        run_id = client.post("/runs", json={"request": {"mode": "audit", "draft_pdf_url": url}}).json()["run_id"]
        assert client.get(f"/audit/{run_id}/paper").status_code == 409  # not parsed yet

        before = len(stubs.DOCLING_CALLS)
        job = _wait(client, client.post(f"/audit/{run_id}/parse").json()["job_id"])
        assert job["status"] == "done", job["error"]
        assert job["result"]["cached"] is False and len(job["result"]["sha256"]) == 64
        assert stubs.DOCLING_CALLS[-1] == {"has_pdf": True, "ocr_off": True}
        assert [t["id"] for t in job["result"]["tables"]][0] == "table:1"

        paper = client.get(f"/audit/{run_id}/paper").json()
        assert paper["title"] == "Corrective Retrieval for Testing"
        assert paper["tables"][0]["grid"][1] == ["BM25", "41.2", "48.9"]
        png = client.get(f"/audit/{run_id}/figures/figure:1")
        assert png.status_code == 200 and png.headers["content-type"] == "image/png"
        assert client.get(f"/audit/{run_id}/figures/figure:9").status_code == 404
        steps = client.get(f"/runs/{run_id}/steps").json()
        assert [(s["step_name"], s["status"]) for s in steps] == [("parse", "done")]

        # same PDF again: served from the cache, docling-serve not called
        run2 = client.post("/runs", json={"request": {"mode": "audit", "draft_pdf_url": url}}).json()["run_id"]
        job2 = _wait(client, client.post(f"/audit/{run2}/parse").json()["job_id"])
        assert job2["result"]["cached"] is True and len(stubs.DOCLING_CALLS) == before + 1


def test_parse_errors_end_in_a_failed_job_and_step(tmp_path, test_db_url, stub_url):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        APP_DATABASE_URL=test_db_url, QDRANT_URL="local::memory:", EMBEDDER_URL=stub_url, PARSER_URL=stub_url,
        CACHE_DIR=tmp_path / "cache", CORPUS_DIR=tmp_path / "corpus", PROMPTS_DIR=REPO / "prompts", LOG_LEVEL="WARNING",
    )  # private URLs not allowed: the stub's address is refused
    with TestClient(create_app(settings)) as client:
        run_id = client.post("/runs", json={"request": {"mode": "audit", "draft_pdf_url": f"{stub_url}/pdf/2101.00001v2"}}).json()["run_id"]
        job = _wait(client, client.post(f"/audit/{run_id}/parse").json()["job_id"])
        assert job["status"] == "failed" and "not a public address" in job["error"]["message"]
        assert client.get(f"/runs/{run_id}/steps").json()[0]["status"] == "failed"
        novelty = client.post("/runs", json={"request": {"mode": "novelty", "idea_text": "x" * 40}}).json()["run_id"]
        assert client.post(f"/audit/{novelty}/parse").status_code == 409
