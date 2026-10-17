"""Integration tests for run-state endpoints against a real Postgres."""

import uuid

IDEA = "Use retrieval-augmented claim verification on result tables in ML papers."


def _create(client, request, execution_id="exec-1"):
    return client.post("/runs", json={"request": request, "n8n_execution_id": execution_id})


def test_create_novelty_run_stores_normalized_input(client):
    resp = _create(client, {"mode": "novelty", "idea_text": f"  {IDEA} "})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "received"
    run = client.get(f"/runs/{body['run_id']}").json()
    assert run["mode"] == "novelty"
    assert run["input"]["idea_text"] == IDEA
    assert run["input"]["query_language"] == "en"
    assert run["n8n_execution_id"] == "exec-1"
    assert run["tokens_used"] == 0


def test_invalid_request_returns_field_errors_and_stores_nothing(client):
    resp = _create(client, {"mode": "audit"})
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert errors == [{"field": "request", "message": "Value error, draft_pdf_url or draft_pdf_upload is required when mode is 'audit'"}]
    with client.app.state.db.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_update_to_done_then_further_updates_conflict(client):
    run_id = _create(client, {"mode": "audit", "draft_pdf_url": "https://arxiv.org/pdf/1706.03762"}).json()["run_id"]

    resp = client.patch(f"/runs/{run_id}", json={"status": "done", "result": {"branch": "audit"}})
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"
    assert resp.json()["result"] == {"branch": "audit"}

    resp = client.patch(f"/runs/{run_id}", json={"status": "running"})
    assert resp.status_code == 409


def test_update_unknown_run_is_404(client):
    assert client.patch(f"/runs/{uuid.uuid4()}", json={"status": "running"}).status_code == 404
    assert client.get(f"/runs/{uuid.uuid4()}").status_code == 404


def test_fail_by_execution_only_touches_open_runs_of_that_execution(client):
    open_id = _create(client, {"mode": "novelty", "idea_text": IDEA}, "exec-7").json()["run_id"]
    done_id = _create(client, {"mode": "novelty", "idea_text": IDEA}, "exec-7").json()["run_id"]
    other_id = _create(client, {"mode": "novelty", "idea_text": IDEA}, "exec-8").json()["run_id"]
    client.patch(f"/runs/{done_id}", json={"status": "done"})

    resp = client.post("/runs/fail-by-execution", json={"n8n_execution_id": "exec-7", "error": {"message": "boom"}})
    assert resp.status_code == 200
    assert resp.json()["failed_run_ids"] == [open_id]

    assert client.get(f"/runs/{open_id}").json()["status"] == "failed"
    assert client.get(f"/runs/{open_id}").json()["error"] == {"message": "boom"}
    assert client.get(f"/runs/{done_id}").json()["status"] == "done"
    assert client.get(f"/runs/{other_id}").json()["status"] == "received"


def test_list_runs_newest_first_with_limit(client):
    first = _create(client, {"mode": "novelty", "idea_text": IDEA}).json()["run_id"]
    second = _create(client, {"mode": "novelty", "idea_text": IDEA}).json()["run_id"]
    runs = client.get("/runs", params={"limit": 1}).json()
    assert [r["run_id"] for r in runs] == [second]
    assert {r["run_id"] for r in client.get("/runs").json()} == {first, second}
    assert client.get("/runs", params={"limit": 0}).status_code == 422


def test_schema_is_idempotent(client):
    # Re-applying must not fail or wipe data (pp-tools applies it at every start).
    run_id = _create(client, {"mode": "novelty", "idea_text": IDEA}).json()["run_id"]
    client.app.state.db.apply_schema()
    assert client.get(f"/runs/{run_id}").status_code == 200
