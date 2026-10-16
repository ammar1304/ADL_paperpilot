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
    assert len(kept) == 2 and kept[0]["refers_to"] == ["table:1"] and kept[0]["sentence"] == SENTENCE
    assert kept[1]["refers_to"] == ["figure:1"] and kept[1]["unknown_refs"] == ["table:7"]
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
        assert len(out["claims"]) == 1 and out["coverage"]["batches_done"] == 1
        assert out["prompt_versions"] == {"P4-claim-extractor": "v1"}


# ---- the 30-audit workflow matches the contracts ---------------------------------------------

import json  # noqa: E402

from app.audit.claims import Claim, ClaimValue, ClaimsOutput, Comparison  # noqa: E402

AUDIT_WF = json.loads((REPO / "n8n" / "workflows" / "30-audit.json").read_text(encoding="utf-8"))


def _node(name):
    return next(n for n in AUDIT_WF["nodes"] if n["name"] == name)


def test_claims_schema_matches_the_pydantic_contract():
    schema = json.loads(_node("Claims schema")["parameters"]["inputSchema"])
    item = schema["properties"]["claims"]["items"]
    assert set(item["properties"]) == set(Claim.model_fields)
    assert item["properties"]["type"]["enum"] == ["result", "comparison", "figure", "citation", "other"]
    assert set(item["properties"]["values"]["items"]["properties"]) == set(ClaimValue.model_fields)
    assert set(item["properties"]["comparison"]["properties"]) == set(Comparison.model_fields)
    assert set(schema["properties"]) == set(ClaimsOutput.model_fields)


def test_p4_example_satisfies_the_contract():
    from app.prompts import PromptLibrary

    system = PromptLibrary(REPO / "prompts").load("P4-claim-extractor").system
    example = system.split("Answer:", 1)[1].split("The third sentence", 1)[0].strip()
    ClaimsOutput.model_validate(json.loads(example)["output"])


def test_audit_llm_nodes_never_use_gemini():
    """D12: a draft is never sent to the Gemini free tier."""
    types = {n["type"] for n in AUDIT_WF["nodes"]}
    assert "@n8n/n8n-nodes-langchain.lmChatGoogleGemini" not in types
    assert _node("Extract claims")["parameters"]["needsFallback"] is False
    assert _node("Groq gpt-oss-20b (claims)")["parameters"]["model"] == "openai/gpt-oss-20b"


def test_audit_workflow_loops_over_batches_and_polls_the_parse_job():
    conns = AUDIT_WF["connections"]
    assert [o[0]["node"] for o in conns["Loop over batches"]["main"]] == ["Assemble result", "Extract claims"]
    assert conns["Pause 40 s"]["main"][0][0]["node"] == "Loop over batches"
    assert [o[0]["node"] for o in conns["Parse status"]["main"]] == ["Render claim prompts", "Parse failed", "Polled too long?"]
    assert _node("Assemble result")["executeOnce"] is True


def test_one_claims_request_fits_groqs_per_request_limit():
    """Groq refused every first-version request: input + max output came to 8,069-8,213 > 8,000 tokens."""
    from app.audit.steps import BATCH_TOKENS

    max_out = _node("Groq gpt-oss-20b (claims)")["parameters"]["options"]["maxTokensToSample"]
    prompt_overhead = 2600  # system prompt + n8n format instructions + tables list, in real tokens
    assert BATCH_TOKENS * 1.4 + prompt_overhead + max_out <= 7000  # 1.4: our counter undercounts real tokens
