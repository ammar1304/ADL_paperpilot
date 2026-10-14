"""/health must tell the truth about each dependency."""

from conftest import free_port, make_client


def test_health_reports_both_dependencies_down():
    dead_pg = f"postgresql://nobody:x@127.0.0.1:{free_port()}/nothing"
    dead_qdrant = f"http://127.0.0.1:{free_port()}"
    with make_client(dead_pg, dead_qdrant, db_wait_s=0) as client:
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"]["ok"] is False
    assert body["checks"]["qdrant"]["ok"] is False
    assert body["checks"]["qdrant"]["error"]


def test_health_reports_only_qdrant_down(test_db_url):
    with make_client(test_db_url, f"http://127.0.0.1:{free_port()}") as client:
        resp = client.get("/health")
    assert resp.status_code == 503
    checks = resp.json()["checks"]
    assert checks["postgres"] == {"ok": True}
    assert checks["qdrant"]["ok"] is False


def test_health_ok_when_both_reachable(test_db_url, fake_qdrant):
    with make_client(test_db_url, fake_qdrant) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["qdrant"] == {"ok": True, "http_status": 200}


def test_create_run_returns_503_when_database_is_down():
    dead_pg = f"postgresql://nobody:x@127.0.0.1:{free_port()}/nothing"
    with make_client(dead_pg, f"http://127.0.0.1:{free_port()}", db_wait_s=0) as client:
        resp = client.post(
            "/runs",
            json={"request": {"mode": "novelty", "idea_text": "A long enough idea about table claims."}},
        )
    assert resp.status_code == 503


def test_json_responses_declare_utf8(client):
    """PowerShell 5.1 garbles non-ASCII text unless the charset is explicit."""
    for path in ("/health", "/runs", "/prompts"):  # explicit and default responses
        resp = client.get(path)
        assert resp.headers["content-type"] == "application/json; charset=utf-8", path
