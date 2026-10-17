"""E5 (citations): the gold set, the passages endpoint, P6 v2 and the script's scoring."""

import importlib.util
import json
from pathlib import Path

import pytest

import stubs
from app.audit import citations as cit

REPO = Path(__file__).resolve().parents[3]
GOLD = json.loads((REPO / "eval" / "e5" / "citation_claims.json").read_text(encoding="utf-8"))["items"]


def _script():
    spec = importlib.util.spec_from_file_location("e5", REPO / "scripts" / "e5_citation_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gold_set_is_balanced_and_well_formed():
    assert len(GOLD) == 40 and len({i["id"] for i in GOLD}) == 40
    assert sum(i["gold"] == "supported" for i in GOLD) == 25 and sum(i["gold"] == "unsupported" for i in GOLD) == 15
    assert sum(i["kind"] == "several citations" for i in GOLD) == 8
    for i in GOLD:
        assert i["paper_id"].startswith("arxiv:") and i["marker"] and i["evidence"] and i["title"]
        assert i["marker"] in i["sentence"] and 3 <= len(i["evidence"].split()) or len(i["evidence"]) >= 9


@pytest.fixture(scope="module")
def stub_url():
    server = stubs.serve()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_passages_endpoint_uses_the_audits_retrieval(tmp_path, test_db_url, stub_url):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(APP_DATABASE_URL=test_db_url, QDRANT_URL="local::memory:", EMBEDDER_URL=stub_url,
                        CACHE_DIR=tmp_path / "cache", CORPUS_DIR=tmp_path / "corpus", PROMPTS_DIR=REPO / "prompts", LOG_LEVEL="WARNING")
    with TestClient(create_app(settings)) as client:
        assert client.post("/citations/passages", json={"paper_id": "arxiv:2101.00002"}).status_code == 422
        out = client.post("/citations/passages", json={"paper_id": "arxiv:2101.00002", "claim": "Author (2020) show X (Author, 2020).",
                                                       "cites": ["Author, 2020"]})
        assert out.status_code == 409 and "corpus build" in out.json()["detail"]  # no library yet: a clear refusal, not a crash


def test_shared_function_ranks_by_reranker_and_cuts_windows():
    from app.audit.steps import citation_passages

    class Hit:
        def __init__(self, i):
            self.chunk_id, self.chunk_type, self.section, self.score = f"c{i}", "section", "3", 0.5
            self.text = "Title. Section 3.\n" + " ".join(f"Sentence {k} is filler text." for k in range(60)) + " The answer is here."

    class R:
        def chunks(self, q, s, k, f):
            assert f == {"paper_id": "p"} and k == cit.RETRIEVE_K
            return [Hit(i) for i in range(4)]

    class RR:
        def scores(self, q, texts):
            return [0.1, 0.9, 0.5, 0.2]

    out = citation_passages(R(), RR(), "the answer is here", "p")
    assert [p["chunk_id"] for p in out] == ["c1", "c2", "c3"] and all(len(p["text"]) <= cit.PASSAGE_CHARS for p in out)
    assert "The answer is here." in out[0]["text"]


def test_p6_v2_scopes_the_judgement_to_the_named_citation():
    from app.prompts import PromptLibrary

    lib = PromptLibrary(REPO / "prompts")
    v1, v2 = lib.load("P6-citation-checker", "v1"), lib.load("P6-citation-checker", "v2")
    assert "attributes to the citation named in the pair" in v2.system and "attributes to the citation named" not in v1.system
    assert json.loads((REPO / "prompts" / "active.json").read_text())["P6-citation-checker"] == "v1"  # until E5 says otherwise


def test_script_scores_and_retrieval_hits(monkeypatch, tmp_path):
    e5 = _script()
    items = [g for g in GOLD if g["id"] in ("e5-03", "e5-04")]  # DPR, true and changed number

    def fake_post(url, body, headers=None, timeout=120):
        if url.endswith("/citations/passages"):
            return 200, {"query": "q", "passages": [{"id": "E1", "text": "Our dense retriever outperforms BM25 by 9%- 19% absolute.", "chunk_id": "x"}]}, {}
        user = body["messages"][1]["content"]
        assert "Cited as Karpukhin et al., 2020" in user and body["model"] == e5.CLAIMS_MODEL
        checks = [{"pair": 1, "verdict": "supported", "evidence": ["E1"], "reason": "r"},
                  {"pair": 2, "verdict": "contradicted", "evidence": ["E1"], "reason": "9-19, not 30-40"}]
        return 200, {"choices": [{"message": {"content": json.dumps({"checks": checks})}}], "usage": {}}, {}

    monkeypatch.setattr(e5, "post", fake_post)
    monkeypatch.setattr(e5, "PACE_S", 0)
    jobs = e5.retrieve(items, "http://x")
    assert [j["hit_rank"] for j in jobs] == [1, 1]  # "9%- 19%" across a line break still matches
    with (tmp_path / "calls.jsonl").open("w") as out:
        results = e5.judge(items, jobs, "v1", "key", out)
    s = e5.score(items, results)
    assert s["supported_recognised"] == "1/1" and s["errors_accepted"] == "0/1" and s["errors_contradicted"] == "1/1"
