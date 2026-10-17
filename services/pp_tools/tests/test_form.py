"""The form (D39): PDF uploads, audits of an uploaded PDF, the result page, and the 05-form workflow."""

import json
import time
from pathlib import Path

import httpx
import pytest

import stubs
from app.audit.uploads import UploadError, Uploads
from app.report import audit_page, novelty_page, result_page

REPO = Path(__file__).resolve().parents[3]
FORM_WF = json.loads((REPO / "n8n" / "workflows" / "05-form.json").read_text(encoding="utf-8"))
RESULTS = REPO / "eval" / "results"


# ---- uploads ----------------------------------------------------------------------------


def test_uploads_are_named_by_content_and_must_be_pdfs(tmp_path):
    up = Uploads(tmp_path / "uploads", max_bytes=1_000)
    pdf = b"%PDF-1.7\nhello"
    first = up.save(pdf)
    assert len(first) == 64 and up.save(pdf) == first and up.read(first) == pdf
    assert up.exists(first) and not up.exists("b" * 64) and not up.exists("../x")
    with pytest.raises(UploadError, match="not a PDF"):
        up.save(b"<html>")
    with pytest.raises(UploadError, match="larger than"):
        up.save(b"%PDF-" + b"x" * 2_000)
    with pytest.raises(UploadError, match="empty"):
        up.save(b"")
    with pytest.raises(LookupError):
        up.path("../../etc/passwd")
    assert not list((tmp_path / "uploads").glob("*.part"))


@pytest.fixture(scope="module")
def stub_url():
    server = stubs.serve()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _app(tmp_path, test_db_url, stub_url, **extra):
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        APP_DATABASE_URL=test_db_url, QDRANT_URL="local::memory:", EMBEDDER_URL=stub_url, PARSER_URL=stub_url,
        CACHE_DIR=tmp_path / "cache", CORPUS_DIR=tmp_path / "corpus", PROMPTS_DIR=REPO / "prompts",
        LOG_LEVEL="WARNING", **extra,
    )
    return create_app(settings)


def test_an_uploaded_pdf_is_audited_without_any_download(tmp_path, test_db_url, stub_url):
    """The stub's address is private and private URLs are not allowed here: only the upload path can work."""
    from fastapi.testclient import TestClient

    pdf = httpx.get(f"{stub_url}/pdf/2101.00001v2").content
    with TestClient(_app(tmp_path, test_db_url, stub_url)) as client:
        assert client.post("/uploads", content=b"not a pdf").status_code == 415
        up = client.post("/uploads", content=pdf, headers={"content-type": "application/pdf"})
        assert up.status_code == 201 and up.json()["bytes"] == len(pdf)
        upload_id = up.json()["upload_id"]

        missing = client.post("/runs", json={"request": {"mode": "audit", "draft_pdf_upload": "c" * 64}})
        assert missing.status_code == 422 and missing.json()["errors"][0]["field"] == "draft_pdf_upload"

        run_id = client.post("/runs", json={"request": {"mode": "audit", "draft_pdf_upload": upload_id}}).json()["run_id"]
        job_id = client.post(f"/audit/{run_id}/parse").json()["job_id"]
        for _ in range(100):
            job = client.get(f"/jobs/{job_id}").json()
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        assert job["status"] == "done", job["error"]
        assert job["params"]["upload"] == upload_id and "url" not in job["params"]
        assert job["result"]["sha256"] == upload_id  # the same hash names the upload and the parsed paper
        assert client.get(f"/audit/{run_id}/paper").json()["title"] == "Corrective Retrieval for Testing"


def test_upload_size_limit(tmp_path, test_db_url, stub_url):
    from fastapi.testclient import TestClient

    with TestClient(_app(tmp_path, test_db_url, stub_url, AUDIT_MAX_PDF_MB=1)) as client:
        assert client.post("/uploads", content=b"%PDF-" + b"x" * 1_100_000).status_code == 413


# ---- the result page --------------------------------------------------------------------


def _novelty_sample():
    line = (RESULTS / "20261016-000420-agent-on" / "runs.jsonl").read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)["result"]


def _audit_sample():
    return json.loads((RESULTS / "20261016-235320-audit-citations" / "2401.15884v3.json").read_text(encoding="utf-8"))["result"]


def test_novelty_page_from_a_real_result():
    result = _novelty_sample()
    html = novelty_page({"input": {"idea_text": "Diffusion <b>for</b> proteins"}}, result, "/form/paperpilot-form")
    assert "No overlapping paper found" in html and "not proof of novelty" in html
    assert "Diffusion &lt;b&gt;for&lt;/b&gt; proteins" in html and "<b>for</b>" not in html
    assert result["summary"].split(";")[0][:40] in html.replace("&#x27;", "'")
    assert "LLM-written, not verified" in html and 'href="/form/paperpilot-form"' in html


def test_novelty_page_lists_overlapping_papers_with_facet_labels():
    result = _novelty_sample()
    paper = {**result["checked_not_close"][0], "overlap": {"task": "same", "method": "partial", "data": "unknown", "result": "different"}}
    html = novelty_page({"input": {}}, {**result, "closest_work": [paper]})
    assert "1 related paper overlap" in html and "task: same" in html and "b-bad" in html
    assert 'href="https://arxiv.org/abs/2403.14197"' in html


def test_audit_page_from_a_real_result_puts_problems_first():
    result = _audit_sample()
    html = audit_page({"input": {"draft_pdf_url": "https://arxiv.org/pdf/2401.15884v3"}}, result)
    assert "Corrective Retrieval Augmented Generation" in html
    assert "No claim was contradicted" in html and "Partial audit" in html and "daily token limit" in html
    assert html.index("unverifiable") < html.index("Figures, read by a vision model")  # the one unverifiable number is listed before the supported ones
    assert "<summary>Supported (13)</summary>" in html and "<summary>Cited paper not in the library (8)</summary>" in html
    for section in ("Numbers against", "Figures, read", "Citations, against"):
        assert section in html

    bad = json.loads(json.dumps(result))
    bad["number_checks"]["checks"][3]["verdict"] = "contradicted"
    bad["number_checks"]["summary"]["contradicted"] = 1
    bad["citation_checks"]["checks"][0]["text"] = '<script>alert("x")</script>'
    html = audit_page({"input": {}}, bad)
    assert "1 claim contradicted" in html and "<script>" not in html and "&lt;script&gt;" in html
    numbers = html[html.index("Numbers against"):html.index("Figures, read")]
    assert numbers.index("contradicted</span>") < numbers.index("supported</span>")


def test_result_page_for_rejected_and_failed_runs():
    html = result_page(422, {"errors": [{"field": "idea_text", "message": "String should have at least 20 characters"}]}, None)
    assert "Please fix the form" in html and "Idea: String should have at least 20 characters" in html
    html = result_page(200, {"run_id": "x"}, {"status": "failed", "mode": "audit", "error": {"message": "parser down"}})
    assert "The run failed" in html and "parser down" in html
    assert "HTTP 500" in result_page(500, {"message": "Error in workflow"}, None)
    run = {"status": "done", "mode": "audit", "input": {}, "result": _audit_sample()}
    assert "Draft audit" in result_page(200, {"run_id": "x"}, run) or "Corrective" in result_page(200, {"run_id": "x"}, run)


def test_form_result_endpoint_reads_the_stored_run(client):
    run_id = client.post("/runs", json={"request": {"mode": "novelty", "idea_text": "An idea that is long enough to pass."}}).json()["run_id"]
    client.patch(f"/runs/{run_id}", json={"status": "done", "result": _novelty_sample()})
    html = client.post("/form/result", json={"status_code": 200, "body": {"run_id": run_id}, "form_url": "/form/paperpilot-form"}).json()["html"]
    assert "Novelty check" in html and "An idea that is long enough to pass." in html
    html = client.post("/form/result", json={"status_code": "422", "body": {"errors": [{"field": "mode", "message": "bad"}]}}).json()["html"]
    assert "What to do: bad" in html


# ---- the n8n workflow -------------------------------------------------------------------


def _node(name):
    return next(n for n in FORM_WF["nodes"] if n["name"] == name)


def test_form_workflow_wiring():
    conns = FORM_WF["connections"]
    trigger = _node("PaperPilot form")
    assert trigger["type"] == "n8n-nodes-base.formTrigger" and trigger["typeVersion"] == 2.4
    assert trigger["parameters"]["options"]["path"] == "paperpilot-form"
    fields = {f["fieldName"]: f for f in trigger["parameters"]["formFields"]["values"]}
    assert set(fields) == {"task", "idea_text", "draft_pdf", "draft_pdf_url"} and fields["task"]["requiredField"] is True
    assert fields["draft_pdf"]["fieldType"] == "file" and fields["draft_pdf"]["multipleFiles"] is False
    assert fields["draft_pdf"]["acceptFileTypes"] == ".pdf"

    assert [o[0]["node"] for o in conns["PDF uploaded?"]["main"]] == ["Store PDF", "Build request"]
    assert conns["PaperPilot form"]["main"][0][0]["node"] == "PDF uploaded?"
    assert [o[0]["node"] for o in conns["Store PDF"]["main"]] == ["Build request", "Upload rejected"]
    assert _node("Store PDF")["onError"] == "continueErrorOutput"
    assert conns["Upload rejected"]["main"][0][0]["node"] == "Result page"
    assert conns["Build request"]["main"][0][0]["node"] == "Run PaperPilot"
    assert conns["Run PaperPilot"]["main"][0][0]["node"] == "Result page"
    assert conns["Result page"]["main"][0][0]["node"] == "Show result"

    store = _node("Store PDF")["parameters"]
    assert store["url"] == "={{ $env.PP_TOOLS_URL }}/uploads" and store["contentType"] == "binaryData"
    assert store["inputDataFieldName"] == "draft_pdf"
    run = _node("Run PaperPilot")
    assert run["parameters"]["url"] == "http://localhost:5678/webhook/paperpilot"  # the same entry point as every client
    assert run["parameters"]["options"]["response"]["response"] == {"fullResponse": True, "neverError": True}
    assert run["parameters"]["options"]["timeout"] >= 30 * 60 * 1000
    show = _node("Show result")
    assert show["type"] == "n8n-nodes-base.form" and show["parameters"]["operation"] == "completion"
    assert show["parameters"]["respondWith"] == "showText" and show["parameters"]["responseText"] == "={{ $json.html }}"
    assert FORM_WF["id"] == "pp05Form00000001" and FORM_WF["settings"]["errorWorkflow"] == "pp90ErrorHandler"


def test_build_request_expression_maps_the_form_to_the_run_request():
    """Evaluate the node's expression with the form's values, as n8n would, through node if available."""
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    expr = _node("Build request")["parameters"]["jsonOutput"]
    assert expr.startswith("={{") and expr.endswith("}}")
    body = expr[3:-2]
    script = (
        "const cases = JSON.parse(process.argv[1]); const out = cases.map(([form, uploaded]) => {"
        " const $ = (name) => name === 'PaperPilot form' ? { first: () => ({ json: form }) }"
        " : { isExecuted: uploaded !== null, first: () => ({ json: { upload_id: uploaded } }) };"
        f" return ({body}); }}); console.log(JSON.stringify(out));"
    )
    cases = [
        [{"task": "Check how novel an idea is", "idea_text": " my idea ", "draft_pdf_url": ""}, None],
        [{"task": "Audit a draft paper", "idea_text": "", "draft_pdf_url": "https://arxiv.org/pdf/1"}, None],
        [{"task": "Audit a draft paper", "idea_text": "", "draft_pdf_url": "https://arxiv.org/pdf/1"}, "f" * 64],
    ]
    raw = json.loads(subprocess.run(["node", "-e", script, json.dumps(cases)], capture_output=True, text=True, check=True).stdout)
    out = [json.loads(r) for r in raw]  # the node's output is a JSON string, as the Set node's raw mode expects
    assert out[0] == {"mode": "novelty", "idea_text": "my idea"}
    assert out[1] == {"mode": "audit", "draft_pdf_url": "https://arxiv.org/pdf/1"}
    assert out[2] == {"mode": "audit", "draft_pdf_upload": "f" * 64}  # a file wins over a link


def test_no_two_workflows_register_the_same_webhook():
    """n8n keys production webhooks by path and method only. The form trigger's POST once used
    the router's path ("paperpilot"), so n8n skipped it at publish time ("some triggers failed to
    activate") and every submission ended on "Problem loading form" (F-027)."""
    seen = {}
    for f in sorted((REPO / "n8n" / "workflows").glob("*.json")):
        wf = json.loads(f.read_text(encoding="utf-8"))
        for n in wf["nodes"]:
            p = n["parameters"]
            if n["type"] == "n8n-nodes-base.webhook":
                keys = [(p["path"], p.get("httpMethod", "GET"))]
            elif n["type"] == "n8n-nodes-base.formTrigger":
                path = p.get("options", {}).get("path") or p.get("path") or n.get("webhookId")
                keys = [(path, "GET"), (path, "POST")]
            else:
                continue
            for key in keys:
                assert key not in seen, f"{key} registered by {seen[key]} and {wf['name']}/{n['name']}"
                seen[key] = f"{wf['name']}/{n['name']}"
