"""Audit step 3: candidate sentences, P4 batches, and validation of the extractor's output."""

import time
from pathlib import Path

import pytest

import stubs
from app.audit.claims import batches, candidate_sentences, signals, split_sentences, validate
from app.audit.paper import build_paper
from audit_fixtures import PDF_CAPTIONS, synthetic_doc

REPO = Path(__file__).resolve().parents[3]


def test_sentence_split_keeps_abbreviations_and_decimals():
    s = split_sentences("Asai et al. (2023) use tokens, e.g. retrieval. Fig. 2 shows 54.9 vs. 50.5. Then we stop.")
    assert s == ["Asai et al. (2023) use tokens, e.g. retrieval.", "Fig. 2 shows 54.9 vs. 50.5.", "Then we stop."]


def test_signals():
    assert signals("CRAG reaches 54.9 on PopQA.") == ["number"]
    assert signals("As shown in Table 3, it helps.") == ["table_figure"]
    assert signals("Reflection tokens (Asai et al., 2023) are used.") == ["citation"]
    assert signals("We use [12, 14] for this.") == ["citation"]
    assert signals("We train for 3 epochs on 2 GPUs.") == []  # integers alone are not claims


def paper():
    return build_paper(synthetic_doc(), None, PDF_CAPTIONS).as_dict()


def test_candidates_skip_references_and_batches_respect_the_budget():
    cands, total = candidate_sentences(paper())
    assert [c.id for c in cands] == ["b:1/s:0"] and total == 1  # the reference entry is not a sentence to check
    many = cands * 50
    groups = batches(many, max_tokens=200)
    assert len(groups) > 1 and sum(len(g) for g in groups) == 50


SENTENCE = "Our method improves EM from 41.2 to 47.8 on NQ (Table 1), see also Figure 1."


def _claim(**kw):
    base = {"sentence_id": "b:1/s:0", "type": "comparison", "claim": "The method beats BM25 on NQ EM.",
            "values": [{"value": "47.8", "metric": "EM", "dataset": "NQ", "system": "our method", "setting": ""}],
            "comparison": {"system": "our method", "baseline": "BM25", "difference": "", "kind": "unspecified", "direction": "better"},
            "refers_to": ["table:1"], "cites": []}
    return base | kw


def test_validation_drops_invented_numbers_and_flags_unknown_tables():
    ids, sents = {"b:1/s:0"}, {"b:1/s:0": SENTENCE}
    good = _claim()
    invented = _claim(values=[{"value": "48.0", "metric": "EM", "dataset": "NQ", "system": "x", "setting": ""}])
    diff = _claim(comparison={"system": "a", "baseline": "b", "difference": "6.6", "kind": "absolute", "direction": "better"})
    ghost = _claim(refers_to=["table:7", "figure:1"])
    wrong_id = _claim(sentence_id="b:99/s:0")
    kept, dropped = validate({"claims": [good, invented, diff, ghost, wrong_id]}, ids, sents, paper())
    assert len(kept) == 2 and kept[0]["refers_to"] == ["table:1", "figure:1"] and kept[0]["sentence"] == SENTENCE  # figure:1 from the sentence
    assert kept[1]["refers_to"] == ["figure:1", "table:1"] and kept[1]["unknown_refs"] == ["table:7"]
    reasons = [d["reason"] for d in dropped]
    assert reasons[0] == "values not in the sentence: ['48.0']"
    assert reasons[1] == "values not in the sentence: ['6.6']"  # a computed difference the sentence never states
    assert reasons[2] == "unknown sentence id"
    with pytest.raises(ValueError):
        validate({"claims": [{"sentence_id": "b:1/s:0"}]}, ids, sents, paper())


@pytest.fixture(scope="module")
def stub_url():
    server = stubs.serve()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_raw_model_text_is_parsed_and_llm_errors_are_recorded(tmp_path, test_db_url, stub_url):
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
        assert client.post(f"/audit/{run_id}/claims/error", json={"batch": 0, "error": "429 rate limit"}).json()["recorded"] is True
        assert client.post(f"/audit/{run_id}/claims", json={"batch": 0, "output_text": "{not json"}).status_code == 409
        text = json.dumps({"claims": [_claim()]})
        saved = client.post(f"/audit/{run_id}/claims", json={"batch": 0, "output_text": text, "usage": {"total_tokens": 4321}}).json()
        assert saved == {"batch": 0, "kept": 1, "dropped": 0}
        result = client.post(f"/audit/{run_id}/result").json()
        assert result["coverage"]["llm_errors"] == 1 and len(result["claims"]) == 1


def test_claims_through_the_api(tmp_path, test_db_url, stub_url):
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
        assert client.post(f"/audit/{run_id}/claim-prompts").status_code == 409  # not parsed yet
        job = client.post(f"/audit/{run_id}/parse").json()["job_id"]
        for _ in range(100):
            if client.get(f"/jobs/{job}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        rendered = client.post(f"/audit/{run_id}/claim-prompts").json()
        assert rendered["candidates"] == 1 and len(rendered["batches"]) == 1
        b = rendered["batches"][0]
        assert b["system"].startswith("You extract checkable claims") and "[b:1/s:0]" in b["user"] and "table:1: Table 1" in b["user"]
        assert client.post(f"/audit/{run_id}/claims", json={"batch": 0, "output": {"claims": [{"bad": 1}]}}).status_code == 409
        saved = client.post(f"/audit/{run_id}/claims", json={"batch": 0, "output": {"claims": [_claim()]}}).json()
        assert saved == {"batch": 0, "kept": 1, "dropped": 0}
        result = client.get(f"/audit/{run_id}/claims").json()
        assert result["by_type"] == {"comparison": 1} and result["batches_done"] == 1 and result["prompt_version"] == "v1"
        assert client.post(f"/audit/{run_id}/claims", json={"batch": 5, "output": {"claims": []}}).status_code == 409
        out = client.post(f"/audit/{run_id}/result").json()
        assert out["implemented"] == "parse+claims" and out["paper"]["tables"] == ["table:1", "table:u1"]  # the stub PDF has no "Table 2" caption
        assert len(out["claims"]) == 1 and out["coverage"]["batches_done"] == 1 and out["number_checks"] is None
        # step 4: "our method ... 47.8 EM on NQ" is the "Ours" row of Table 1
        summary = client.post(f"/audit/{run_id}/check-numbers").json()
        assert (summary["checks"], summary["supported"], summary["contradicted"]) == (1, 1, 0)
        out = client.post(f"/audit/{run_id}/result").json()
        assert out["implemented"] == "parse+claims+numbers"
        assert out["number_checks"]["checks"][0]["cell"]["row"] == "Ours"
        assert out["prompt_versions"] == {"P4-claim-extractor": "v1"}


# ---- the 30-audit workflow matches the contracts ---------------------------------------------

import json  # noqa: E402

from app.audit.claims import Claim, ClaimValue, ClaimsOutput, Comparison  # noqa: E402

AUDIT_WF = json.loads((REPO / "n8n" / "workflows" / "30-audit.json").read_text(encoding="utf-8"))


def _node(name):
    return next(n for n in AUDIT_WF["nodes"] if n["name"] == name)


def _groq_body():
    from app.audit.claims import claims_request

    body = claims_request("system", "user")
    return body, body["response_format"]["json_schema"]["schema"]


def test_claims_schema_matches_the_pydantic_contract_and_groq_strict_rules():
    body, schema = _groq_body()
    item = schema["properties"]["claims"]["items"]
    assert set(item["properties"]) == set(Claim.model_fields) == set(item["required"])  # strict: all required
    assert item["properties"]["type"]["enum"] == ["result", "comparison", "figure", "citation", "other"]
    assert set(item["properties"]["values"]["items"]["properties"]) == set(ClaimValue.model_fields)
    assert set(item["properties"]["comparison"]["properties"]) == set(Comparison.model_fields)
    assert item["properties"]["comparison"]["type"] == ["object", "null"]
    assert set(schema["properties"]) == set(ClaimsOutput.model_fields)

    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or node.get("type") == ["object", "null"]:
                yield node
            for v in node.values():
                yield from objects(v)
        elif isinstance(node, list):
            for v in node:
                yield from objects(v)

    assert all(o.get("additionalProperties") is False for o in objects(schema))
    assert body["reasoning_effort"] == "low" and body["include_reasoning"] is False  # F-013
    assert body["response_format"]["json_schema"]["strict"] is True


def test_p4_example_satisfies_the_contract():
    from app.prompts import PromptLibrary

    system = PromptLibrary(REPO / "prompts").load("P4-claim-extractor").system
    example = system.split("Answer:", 1)[1].split("The third sentence", 1)[0].strip()
    ClaimsOutput.model_validate(json.loads(example))


def test_audit_llm_calls_never_use_gemini():
    """D12: a draft is never sent to the Gemini free tier."""
    text = json.dumps(AUDIT_WF)
    assert "lmChatGoogleGemini" not in text and "generativelanguage.googleapis.com" not in text
    node = _node("Extract claims")
    assert node["parameters"]["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert node["parameters"]["genericAuthType"] == "httpHeaderAuth"
    assert node["parameters"]["jsonBody"] == "={{ JSON.stringify($json.request) }}"  # built by pp-tools (F-016)
    assert _groq_body()[0]["model"].startswith("openai/gpt-oss")
    assert node["credentials"]["httpHeaderAuth"]["id"] == "ppGroqHeader0001"  # created by n8n/entrypoint.sh (F-014)
    assert "ppGroqHeader0001" in (REPO / "n8n" / "entrypoint.sh").read_text(encoding="utf-8")


def test_audit_workflow_asks_pp_tools_for_each_batch_and_recovers_from_llm_errors():
    """F-012: n8n's retries wait at most 5 s; the loop's state lives in pp-tools instead."""
    conns = AUDIT_WF["connections"]
    assert [o[0]["node"] for o in conns["Parse status"]["main"]] == ["Next claim batch", "Parse failed", "Polled too long?"]
    assert [o[0]["node"] for o in conns["More batches?"]["main"]] == ["Pause before call", "Check numbers"]
    assert conns["Check numbers"]["main"][0][0]["node"] == "Next figure"  # step 5 follows (test_audit_figures.py)
    assert _node("Check numbers")["parameters"]["url"].endswith("/check-numbers")
    assert conns["Pause before call"]["main"][0][0]["node"] == "Extract claims"
    assert _node("Pause before call")["parameters"]["amount"] == "={{ $json.wait_s }}"  # pp-tools decides (F-017)
    error_body = _node("Record LLM error")["parameters"]["jsonBody"]
    assert all(k in error_body for k in ("status:", "retry_after:", "'retry-after'", "model:"))
    assert [o[0]["node"] for o in conns["Extract claims"]["main"]] == ["Save claims", "Record LLM error"]  # success, error
    assert conns["Record LLM error"]["main"][0][0]["node"] == "Next claim batch"
    assert _node("Extract claims")["onError"] == "continueErrorOutput"
    assert conns["Save claims"]["main"][0][0]["node"] == "Next claim batch"
    assert _node("Save claims")["parameters"]["options"]["response"]["response"]["neverError"] is True


def test_one_claims_request_fits_groqs_per_request_limit():
    """F-011: Groq refused every first-version request: input + max output came to 8,069-8,213 > 8,000 tokens."""

    from app.audit.steps import BATCH_TOKENS
    from app.prompts import PromptLibrary
    from app.textutil import count_tokens

    max_out = _groq_body()[0]["max_completion_tokens"]
    p4 = PromptLibrary(REPO / "prompts").load("P4-claim-extractor")
    schema_tokens = count_tokens(json.dumps(_groq_body()[1]))
    tables_and_title = 500
    real = 1.4  # our counter undercounts real tokens by 20-40%
    assert (count_tokens(p4.system) + schema_tokens + BATCH_TOKENS) * real + tables_and_title + max_out <= 7600


def test_next_batch_serves_each_batch_until_saved_or_three_attempts(tmp_path, test_db_url, stub_url):
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
        first = client.post(f"/audit/{run_id}/claims/next").json()
        assert (first["done"], first["batch"], first["attempt"]) == (False, 0, 1) and "[b:1/s:0]" in first["user"]
        # the LLM call failed (rate limit): n8n asks again and gets the same batch
        again = client.post(f"/audit/{run_id}/claims/next").json()
        assert (again["batch"], again["attempt"]) == (0, 2)
        client.post(f"/audit/{run_id}/claims", json={"batch": 0, "output": {"claims": [_claim()]}})
        assert client.post(f"/audit/{run_id}/claims/next").json() == {"done": True, "batches": 1, "finished": 1}

        # a batch that never gets a valid output is skipped after 3 attempts
        run2 = client.post("/runs", json={"request": {"mode": "audit", "draft_pdf_url": f"{stub_url}/pdf/2101.00001v2"}}).json()["run_id"]
        job = client.post(f"/audit/{run2}/parse").json()["job_id"]
        for _ in range(100):
            if client.get(f"/jobs/{job}").json()["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        for attempt in (1, 2, 3):
            assert client.post(f"/audit/{run2}/claims/next").json()["attempt"] == attempt
            assert client.post(f"/audit/{run2}/claims", json={"batch": 0, "output": {"claims": [{"bad": 1}]}}).status_code == 409
        assert client.post(f"/audit/{run2}/claims/next").json()["done"] is True
        result = client.post(f"/audit/{run2}/result").json()
        assert result["coverage"]["batches_skipped"] == [0] and result["claims"] == []


def test_no_n8n_expression_hides_a_closing_brace_pair():
    """F-016: n8n ends an expression at the first "}}"; an inline JSON schema broke every Groq call."""
    import re

    bad = []
    for wf in sorted((REPO / "n8n" / "workflows").glob("*.json")):
        def walk(value, where):
            if isinstance(value, dict):
                for k, v in value.items():
                    walk(v, f"{where}.{k}")
            elif isinstance(value, list):
                for i, v in enumerate(value):
                    walk(v, f"{where}[{i}]")
            elif isinstance(value, str) and value.startswith("="):
                for expr in re.findall(r"\{\{(.*?)\}\}", value, flags=re.S):
                    if "{{" in expr:
                        bad.append(where)
                if value.count("}}") != value.count("{{"):
                    bad.append(where)
        for node in json.loads(wf.read_text(encoding="utf-8"))["nodes"]:
            walk(node["parameters"], f"{wf.name}:{node['name']}")
    assert bad == []


def test_next_batch_hands_n8n_a_ready_groq_request():
    from app.audit.claims import CLAIMS_SCHEMA, claims_request

    body = claims_request("S", "U")
    assert body["messages"] == [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    assert body["response_format"]["json_schema"]["schema"] is CLAIMS_SCHEMA
    json.dumps(body)  # serialisable as is


def test_validation_repairs_what_the_model_gets_slightly_wrong():
    """F-018, seen in the first full run: units, a typo in the id, a lowercase table reference, a margin read as a score."""
    sid = "b:53/s:1"
    sentence = "As shown in table 1, CRAG outperformed RAG by margins of 7.0% accuracy on PopQA; the evaluator is 0.77B parameters vs 7B."
    ids, sents = {sid, "b:53/s:2"}, {sid: sentence, "b:53/s:2": "Other."}
    margin = _claim(sentence_id=sid, refers_to=[],
                    values=[{"value": "7.0%", "metric": "accuracy", "dataset": "PopQA", "system": "CRAG", "setting": ""}],
                    comparison={"system": "CRAG", "baseline": "RAG", "difference": "7.0%", "kind": "absolute", "direction": "better"})
    units = _claim(sentence_id="b:53/s/1", comparison=None, refers_to=[],
                   values=[{"value": "0.77B vs 7B", "metric": "parameters", "dataset": "", "system": "CRAG", "setting": ""}])
    ambiguous = _claim(sentence_id="b:53", comparison=None, values=[])
    kept, dropped = validate({"claims": [margin, units, ambiguous]}, ids, sents, paper())
    assert [d["reason"] for d in dropped] == ["unknown sentence id"]
    assert kept[0]["values"][0]["role"] == "difference" and kept[0]["refers_to"] == ["table:1"]
    assert kept[1]["sentence_id"] == sid and kept[1]["repaired_id"] == "b:53/s/1"
    assert kept[1]["values"][0]["role"] == "value"


def _parsed_run(client, stub_url):
    run_id = client.post("/runs", json={"request": {"mode": "audit", "draft_pdf_url": f"{stub_url}/pdf/2101.00001v2"}}).json()["run_id"]
    job = client.post(f"/audit/{run_id}/parse").json()["job_id"]
    for _ in range(100):
        if client.get(f"/jobs/{job}").json()["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    return run_id


def test_rate_limits_wait_switch_model_then_stop_without_burning_attempts(tmp_path, test_db_url, stub_url):
    """F-017: a 429 cost one of three attempts, so a spent daily quota skipped every batch in 90 s."""
    from fastapi.testclient import TestClient

    from app.audit.claims import CLAIMS_FALLBACK_MODELS, CLAIMS_MODEL
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        APP_DATABASE_URL=test_db_url, QDRANT_URL="local::memory:", EMBEDDER_URL=stub_url, PARSER_URL=stub_url,
        AUDIT_ALLOW_PRIVATE_URLS=True, CACHE_DIR=tmp_path / "cache", CORPUS_DIR=tmp_path / "corpus",
        PROMPTS_DIR=REPO / "prompts", LOG_LEVEL="WARNING",
    )
    with TestClient(create_app(settings)) as client:
        run_id = _parsed_run(client, stub_url)
        first = client.post(f"/audit/{run_id}/claims/next").json()
        assert (first["model"], first["wait_s"], first["request"]["model"]) == (CLAIMS_MODEL, 30, CLAIMS_MODEL)

        # per-minute limit: same model, wait for retry-after, the attempt is given back
        client.post(f"/audit/{run_id}/claims/error", json={"batch": 0, "status": 429, "retry_after": "70", "model": CLAIMS_MODEL, "error": "429"})
        nxt = client.post(f"/audit/{run_id}/claims/next").json()
        assert (nxt["batch"], nxt["attempt"], nxt["model"]) == (0, 1, CLAIMS_MODEL) and 70 <= nxt["wait_s"] <= 72

        # daily limit on the main model: the fallback takes over at once
        client.post(f"/audit/{run_id}/claims/error", json={"batch": 0, "status": 429, "retry_after": "1009", "model": CLAIMS_MODEL, "error": "429"})
        nxt = client.post(f"/audit/{run_id}/claims/next").json()
        assert (nxt["attempt"], nxt["model"], nxt["wait_s"]) == (1, CLAIMS_FALLBACK_MODELS[0], 30)
        assert nxt["request"]["model"] == CLAIMS_FALLBACK_MODELS[0]

        # every model spent: the run stops and says why, instead of skipping batch after batch
        client.post(f"/audit/{run_id}/claims/error", json={"batch": 0, "status": 429, "retry_after": "2000", "model": CLAIMS_FALLBACK_MODELS[0], "error": "429"})
        done = client.post(f"/audit/{run_id}/claims/next").json()
        assert done["done"] is True and "daily token limit" in done["stopped"]
        cov = client.post(f"/audit/{run_id}/result").json()["coverage"]
        assert cov["batches_skipped"] == [0] and cov["rate_limited"] == 3 and "daily token limit" in cov["stopped"]
        assert cov["models"] == sorted([CLAIMS_MODEL, CLAIMS_FALLBACK_MODELS[0]])

        # other errors still use attempts
        run2 = _parsed_run(client, stub_url)
        for attempt in (1, 2, 3):
            assert client.post(f"/audit/{run2}/claims/next").json()["attempt"] == attempt
            client.post(f"/audit/{run2}/claims/error", json={"batch": 0, "status": 500, "error": "server error"})
        assert client.post(f"/audit/{run2}/claims/next").json()["done"] is True
