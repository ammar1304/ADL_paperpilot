"""The web app (D41): progress read from run steps, the background webhook call, and the /app routes."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.webapp import REF_CHARS, WebhookRunner, audit_progress, new_ref, novelty_progress, status_payload


def step(name, status="done", **detail):
    return {"step_name": name, "status": status, "detail": detail}


# ---- pure functions --------------------------------------------------------------------------------


def test_new_ref_is_long_random_and_matches_the_request_pattern():
    refs = {new_ref() for _ in range(50)}
    assert len(refs) == 50
    assert all(len(r) == 20 and set(r) <= set(REF_CHARS) for r in refs)


def test_novelty_progress_marks_done_stages_and_one_active():
    steps = [step("decompose", output={"facets": {"task": "speed up decoding"}, "queries": ["a", "b"]}),
             step("retrieve", candidates=[1, 2, 3])]
    stages = novelty_progress(steps, finished=False)
    assert [s["state"] for s in stages] == ["done", "done", "active", "waiting", "waiting"]
    assert stages[0]["note"] == "task: speed up decoding"
    assert stages[1]["note"] == "2 queries, 3 candidate papers"


def test_a_later_step_marks_earlier_stages_done():
    stages = novelty_progress([step("judge_prompt"), step("verify")], finished=False)
    assert [s["state"] for s in stages][:3] == ["done", "done", "done"]


def test_finished_run_has_every_stage_done():
    assert {s["state"] for s in novelty_progress([], finished=True)} == {"done"}


def test_audit_progress_counts_batches_and_kept_claims_given_as_lists():
    steps = [step("parse", pages=15, tables=[1, 2], figures=[1]),
             step("claim_prompts", batches=[0, 1, 2]),
             step("claims_batch", batch=0, kept=[{"claim": "a"}, {"claim": "b"}]),
             step("claims_batch", batch=1, kept=3)]
    stages = audit_progress(steps, finished=False)
    assert stages[0]["note"] == "15 pages, 2 tables, 1 figures"
    claims = stages[1]
    assert (claims["state"], claims["done"], claims["total"]) == ("active", 2, 3)
    assert claims["note"] == "5 claims kept"


def test_audit_progress_shows_why_a_check_stopped():
    steps = [step("parse"), step("claims_stopped", reason="daily token limit reached")]
    assert audit_progress(steps, finished=False)[1]["note"] == "daily token limit reached"


def test_status_before_the_run_exists():
    assert status_payload(None, [], {"status_code": None})["state"] == "starting"
    rejected = status_payload(None, [], {"status_code": 422, "body": {"errors": [{"field": "x", "message": "bad"}]}})
    assert rejected == {"state": "rejected", "errors": [{"field": "x", "message": "bad"}]}
    assert status_payload(None, [], {"error": "ConnectError: refused"})["state"] == "failed"
    assert status_payload(None, [], {"status_code": 500})["error"] == "HTTP 500"


def test_status_of_a_failed_run_marks_the_active_stage_failed():
    run = {"status": "failed", "mode": "audit", "run_id": "r", "input": {}, "error": {"message": "parser timed out"}}
    out = status_payload(run, [step("parse")], None)
    assert out["state"] == "failed" and out["error"] == "parser timed out"
    # a failed run counts as finished, so no stage is left active
    assert "active" not in {s["state"] for s in out["stages"]}


def test_status_of_a_done_run_carries_the_result():
    run = {"status": "done", "mode": "novelty", "run_id": "r", "input": {}, "result": {"verdict": "x"}}
    out = status_payload(run, [], None)
    assert out["state"] == "done" and out["result"] == {"verdict": "x"}


# ---- the background webhook call -------------------------------------------------------------------


class _Webhook(BaseHTTPRequestHandler):
    received: list = []
    code = 200

    def do_POST(self):  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append(body)
        self.send_response(type(self).code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def webhook():
    _Webhook.received, _Webhook.code = [], 200
    server = HTTPServer(("127.0.0.1", 0), _Webhook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/webhook/paperpilot"
    server.shutdown()


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_runner_posts_in_the_background_and_remembers_the_answer(webhook):
    runner = WebhookRunner(webhook, timeout_s=5)
    runner.start("ref1", {"mode": "novelty", "idea_text": "x"})
    assert wait_for(lambda: runner.get("ref1")["status_code"] == 200)
    assert runner.get("ref1")["body"] == {"ok": True}
    assert _Webhook.received == [{"mode": "novelty", "idea_text": "x"}]
    assert runner.get("unknown") is None
    runner.close()


def test_runner_records_a_connection_error():
    runner = WebhookRunner("http://127.0.0.1:9/webhook/paperpilot", timeout_s=2)
    runner.start("ref2", {})
    assert wait_for(lambda: runner.get("ref2")["error"] is not None)
    runner.close()


# ---- routes ----------------------------------------------------------------------------------------


@pytest.fixture
def app_client(test_db_url, fake_qdrant, webhook):
    settings = Settings(APP_DATABASE_URL=test_db_url, QDRANT_URL=fake_qdrant, LOG_LEVEL="WARNING",
                        HEALTH_TIMEOUT_S=1.0, STARTUP_DB_WAIT_S=5.0, N8N_WEBHOOK_URL=webhook)
    with TestClient(create_app(settings)) as c:
        c.app.state.db.ensure_schema()
        with c.app.state.db.pool.connection() as conn:
            conn.execute("DELETE FROM runs")
        yield c


def test_page_and_redirect(app_client):
    assert app_client.get("/", follow_redirects=False).headers["location"] == "/app"
    page = app_client.get("/app")
    assert page.status_code == 200 and "PaperPilot" in page.text
    assert app_client.get("/app/static/fonts/LICENSE.md").status_code == 200


def test_invalid_request_is_rejected_before_calling_n8n(app_client):
    resp = app_client.post("/app/api/runs", json={"mode": "audit"})
    assert resp.status_code == 422 and resp.json()["errors"]
    assert _Webhook.received == []


def test_start_then_follow_a_run_by_its_ref(app_client):
    resp = app_client.post("/app/api/runs", json={"mode": "novelty", "idea_text": "Retrieve only when needed.", "extra": "dropped"})
    assert resp.status_code == 202
    ref = resp.json()["ref"]
    assert wait_for(lambda: _Webhook.received)
    sent = _Webhook.received[0]
    assert sent == {"mode": "novelty", "idea_text": "Retrieve only when needed.", "client_ref": ref}

    # the stub webhook does not create a run: the workflow would, through POST /runs
    assert app_client.get(f"/app/api/runs/{ref}").json()["state"] in ("starting", "failed")
    run_id = app_client.post("/runs", json={"request": sent, "n8n_execution_id": "e1"}).json()["run_id"]
    assert app_client.get(f"/runs/{run_id}").json()["input"]["client_ref"] == ref
    app_client.app.state.db.add_step(run_id, "decompose", "done", {"output": {"facets": {"task": "t"}, "queries": []}})
    status = app_client.get(f"/app/api/runs/{ref}").json()
    assert status["state"] == "running" and status["mode"] == "novelty"
    assert status["stages"][0]["state"] == "done" and status["stages"][1]["state"] == "active"


def test_unknown_or_malformed_ref_is_404(app_client):
    assert app_client.get("/app/api/runs/abcdefghijkmnpqrstuv").status_code == 404
    assert app_client.get("/app/api/runs/bad-ref").status_code == 404


def test_client_ref_must_be_a_plain_token(app_client):
    resp = app_client.post("/runs", json={"request": {"mode": "novelty", "idea_text": "x y z", "client_ref": "x'; drop"}})
    assert resp.status_code == 422
