"""Step 5, the figure check (D36): grouping claims per figure, the multimodal request, validation,
the loop through the API, and the n8n wiring."""

import base64
import json
import time
from pathlib import Path

import pytest

import stubs
from app.audit import figures as fig

REPO = Path(__file__).resolve().parents[3]
AUDIT_WF = json.loads((REPO / "n8n" / "workflows" / "30-audit.json").read_text(encoding="utf-8"))
PAPER = {"title": "T", "figures": [{"id": "figure:1", "caption": "Figure 1: Accuracy vs retrieval quality."},
                                   {"id": "figure:2", "caption": "Figure 2: Overview."},
                                   {"id": "figure:u1", "caption": ""}]}


def _c(refs, text="Accuracy drops as retrieval gets worse.", unknown=()):
    return {"claim": text, "sentence": text + " (Figure 1)", "refers_to": list(refs), "unknown_refs": list(unknown)}


def test_claims_are_grouped_per_figure_and_split_at_eight():
    claims = [_c(["figure:1"]) for _ in range(10)] + [_c(["figure:2", "table:1"]), _c(["figure:9"]), _c([], unknown=["figure:7"])]
    jobs, findings = fig.figure_jobs(PAPER, claims, has_image=lambda f: f != "figure:2")
    assert [(j["figure"], len(j["claims"])) for j in jobs] == [("figure:1", 8), ("figure:1", 2)]
    assert [(f["claim"], f["verdict"]) for f in findings] == [(10, "not_shown"), (12, "missing_figure")]
    assert "Figure 7" in findings[1]["evidence"]


def test_claims_block_numbers_claims_and_keeps_the_original_sentence():
    block = fig.claims_block([_c(["figure:1"]), {"claim": "Same.", "sentence": "Same."}])
    assert block.startswith("1. Accuracy drops") and 'Sentence in the paper: "Accuracy drops' in block
    assert block.endswith("2. Same.")  # no repeated sentence


def test_the_request_carries_the_image_and_a_strict_schema():
    png = b"\x89PNG\r\n\x1a\nfake"
    body = fig.figure_request("SYS", "USER", png)
    assert body["model"] == "qwen/qwen3.8-27b" and body["reasoning_effort"] == "low" and body["reasoning_format"] == "hidden"
    user = body["messages"][1]["content"]
    assert user[0] == {"type": "text", "text": "USER"}
    assert user[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(png).decode()
    schema = body["response_format"]["json_schema"]
    assert schema["strict"] is True and schema["schema"]["additionalProperties"] is False
    item = schema["schema"]["properties"]["checks"]["items"]
    assert set(item["required"]) == set(item["properties"]) and item["additionalProperties"] is False
    json.dumps(body)


def test_one_figure_request_fits_groqs_per_minute_limit():
    """Groq counts input + output budget against 8,000 tokens a minute; an image is a flat 2,048."""
    from app.prompts import PromptLibrary
    from app.textutil import count_tokens

    worst = "x " * 400  # one token per character pair: the counter's worst case
    claims = [{"claim": worst, "sentence": worst + " longer", "refers_to": ["figure:1"]} for _ in range(20)]
    jobs, _ = fig.figure_jobs(PAPER, claims, has_image=lambda f: True)
    assert len(jobs) > 1 and sum(len(j["claims"]) for j in jobs) == 20  # split by tokens, nothing lost
    for job in jobs:
        rendered = PromptLibrary(REPO / "prompts").load("P5-figure-checker").render({
            "title": "t " * 60, "figure_id": "Figure 3", "caption": "c " * 300,
            "claims_block": fig.claims_block([claims[i] for i in job["claims"]])})
        text_tokens = count_tokens(rendered.system + rendered.user + json.dumps(fig.FIGURE_SCHEMA)) * 1.4  # our counter undercounts
        assert text_tokens + 2048 + fig.FIGURE_MAX_OUTPUT <= 7600


def test_validation_keeps_one_verdict_per_claim():
    out = {"figure": "A line plot.", "checks": [
        {"claim": 2, "verdict": "contradicted", "evidence": "the line goes up", "read_values": ["61.8"]},
        {"claim": 2, "verdict": "supported", "evidence": "duplicate", "read_values": []},
        {"claim": 7, "verdict": "supported", "evidence": "no such claim", "read_values": []},
        {"claim": 1, "verdict": "maybe", "evidence": "", "read_values": []}]}
    description, checks = fig.validate(json.dumps(out), [40, 41, 42])
    assert description == "A line plot."
    assert [(c["claim"], c["verdict"]) for c in checks] == [(40, "not_shown"), (41, "contradicted"), (42, "not_answered")]
    with pytest.raises(ValueError):
        fig.validate({"nope": 1}, [1])


# ---- through the API --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stub_url():
    server = stubs.serve()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _claim_about_figure_1():
    return {"sentence_id": "b:1/s:0", "type": "figure", "claim": "Figure 1 shows our method beating BM25 on NQ.",
            "values": [], "comparison": None, "refers_to": ["figure:1"], "cites": []}


def test_figure_loop_through_the_api(tmp_path, test_db_url, stub_url):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        APP_DATABASE_URL=test_db_url, QDRANT_URL="local::memory:", EMBEDDER_URL=stub_url, PARSER_URL=stub_url,
        AUDIT_ALLOW_PRIVATE_URLS=True, CACHE_DIR=tmp_path / "cache", CORPUS_DIR=tmp_path / "corpus",
        PROMPTS_DIR=REPO / "prompts", LOG_LEVEL="WARNING",
    )
    with TestClient(create_app(settings)) as client:
        run_id = client.post("/runs", json={"request": {"mode": "audit", "draft_pdf_url": f"{stub_url}/pdf/2101.00001v2"}}).json()["run_id"]
        job = client.post(f"/audit/{run_id}/parse").json()["job_id"]
        for _ in range(100):
            if client.get(f"/jobs/{job}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        client.post(f"/audit/{run_id}/claims/next")
        assert client.post(f"/audit/{run_id}/claims", json={"batch": 0, "output": {"claims": [_claim_about_figure_1()]}}).status_code == 200

        first = client.post(f"/audit/{run_id}/figures/next").json()
        assert (first["done"], first["figure"], first["claims"], first["model"], first["wait_s"]) == (False, "figure:1", 1, fig.FIGURE_MODEL, 45)
        user = first["request"]["messages"][1]["content"]
        assert "Figure 1: Accuracy as passages grow." in user[0]["text"] and "1. Figure 1 shows our method" in user[0]["text"]
        assert user[1]["image_url"]["url"].startswith("data:image/png;base64,")

        # a per-minute 429 gives the attempt back and sets the wait
        client.post(f"/audit/{run_id}/figure-checks/error", json={"batch": 0, "status": "429", "retry_after": "70", "model": fig.FIGURE_MODEL})
        again = client.post(f"/audit/{run_id}/figures/next").json()
        assert again["attempt"] == 1 and 70 <= again["wait_s"] <= 72

        bad = client.post(f"/audit/{run_id}/figure-checks", json={"batch": 0, "output_text": "not json"})
        assert bad.status_code == 409
        saved = client.post(f"/audit/{run_id}/figure-checks", json={"batch": 0, "output": {
            "figure": "A bar chart of EM for BM25 and ours.",
            "checks": [{"claim": 1, "verdict": "supported", "evidence": "the 'Ours' bar is higher", "read_values": ["47.8"]}]}}).json()
        assert saved == {"batch": 0, "figure": "figure:1", "supported": 1}
        assert client.post(f"/audit/{run_id}/figures/next").json()["done"] is True

        result = client.post(f"/audit/{run_id}/result").json()
        assert result["implemented"] == "parse+claims+figures"
        fc = result["figure_checks"]
        assert fc["summary"]["supported"] == 1 and fc["summary"]["rate_limited"] == 1 and fc["summary"]["jobs_done"] == 1
        assert fc["figures_read"] == {"figure:1": "A bar chart of EM for BM25 and ours."}
        assert fc["checks"][0]["sentence_id"] == "b:1/s:0" and result["prompt_versions"]["P5-figure-checker"] == "v1"


# ---- the n8n workflow -------------------------------------------------------------------------


def _node(name):
    return next(n for n in AUDIT_WF["nodes"] if n["name"] == name)


def test_audit_workflow_runs_the_figure_loop_after_the_number_check():
    conns = AUDIT_WF["connections"]
    assert conns["Check numbers"]["main"][0][0]["node"] == "Next figure"
    assert [o[0]["node"] for o in conns["More figures?"]["main"]] == ["Pause before figure", "Assemble result"]
    assert conns["Pause before figure"]["main"][0][0]["node"] == "Check figure"
    assert [o[0]["node"] for o in conns["Check figure"]["main"]] == ["Save figure check", "Record figure error"]
    assert conns["Save figure check"]["main"][0][0]["node"] == "Next figure"
    assert conns["Record figure error"]["main"][0][0]["node"] == "Next figure"
    check = _node("Check figure")
    assert check["onError"] == "continueErrorOutput" and check["parameters"]["jsonBody"] == "={{ JSON.stringify($json.request) }}"
    assert check["parameters"]["url"] == "https://api.groq.com/openai/v1/chat/completions"  # Groq only (D12)
    assert check["credentials"]["httpHeaderAuth"]["id"] == "ppGroqHeader0001"
    assert _node("Pause before figure")["parameters"]["amount"] == "={{ $json.wait_s }}"
    assert "$('Next figure')" in _node("Save figure check")["parameters"]["jsonBody"]
    assert "$('Next figure')" in _node("Record figure error")["parameters"]["jsonBody"]
    assert _node("Next figure")["parameters"]["url"].endswith("/figures/next")
