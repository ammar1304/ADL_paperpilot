"""Step 6, the citation check (D37): marker -> reference -> library paper, retrieval of the cited
paper's passages, the P6 request and its validation, the loop through the API, the n8n wiring.
The cases come from the five papers audited on the team PC."""

import json
import time
from pathlib import Path

import pytest

import stubs
from app.audit import citations as cit

REPO = Path(__file__).resolve().parents[3]
AUDIT_WF = json.loads((REPO / "n8n" / "workflows" / "30-audit.json").read_text(encoding="utf-8"))

REFS = [
    "Akari Asai, Zeqiu Wu, Yizhong Wang, Avirup Sil, and Hannaneh Hajishirzi. 2024. Self-rag: Learning to retrieve, generate, "
    "and critique through self-reflection. In ICLR.",
    "Nitish Shirish Keskar, Bryan McCann, Lav R Varshney, Caiming Xiong, and Richard Socher. Ctrl: A conditional transformer "
    "language model for controllable generation. arXiv preprint arXiv:1909.05858 , 2019.",
    "Long Ouyang, Jeffrey Wu, Xu Jiang, Diogo Almeida, Carroll Wainwright, Pamela Mishkin, Chong Zhang, and",  # split by the parser
    "Ryan Lowe. Training language models to follow instructions with human feedback. In NeurIPS , 2022.",
    "Tom B. Brown, Benjamin Mann, Nick Ryder, and Dario Amodei. 2020. Language models are few-shot learners. In NeurIPS.",
    "Alex Mallen, Akari Asai, Victor Zhong, Rajarshi Das, Daniel Khashabi, and Hannaneh Hajishirzi. 2023a. When not to trust "
    "language models: Investigating effectiveness of parametric and non-parametric memories. In ACL.",
    "Alex Mallen, Someone Else. 2023b. Another paper. In EMNLP.",
]
LIBRARY = cit.Library([
    cit.LibraryPaper("arxiv:2310.11511", "2310.11511", "Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection", True),
    cit.LibraryPaper("arxiv:2212.10511", "2212.10511", "When Not to Trust Language Models: Investigating Effectiveness of "
                     "Parametric and Non-Parametric Memories", False),
    cit.LibraryPaper("arxiv:1909.05858", "1909.05858", "CTRL", False),
])


def test_markers_parse_in_the_styles_the_papers_use():
    assert cit.parse_marker("Asai et al., 2024").author == "asai"
    assert cit.parse_marker("Mallen et al. (2023)").year == "2023"
    assert cit.parse_marker("Zhang et al., 2023b").year == "2023b"
    assert (cit.parse_marker("Lewis and Fan, 2019").second, cit.parse_marker("[12]").number) == ("fan", 12)
    assert cit.parse_marker("see the appendix") is None


def test_first_author_and_years_from_bibliography_entries():
    assert [cit.first_author(r) for r in (REFS[0], REFS[4], "Asai, A., and Wu, Z. 2023.", "A. Author. 2020. Title.")] == \
        ["asai", "brown", "asai", "author"]  # "Tom B. Brown" is not cut at "B."
    assert cit.years(REFS[1]) == {"2019"}  # not 1909 from the arXiv id


def test_fragments_split_by_the_parser_are_joined():
    merged = cit.merge_fragments(REFS)
    assert len(merged) == len(REFS) - 1 and merged[2].startswith("Long Ouyang") and "Ryan Lowe" in merged[2]


def test_markers_resolve_to_one_entry():
    refs = cit.merge_fragments(REFS)
    assert cit.resolve("Ouyang et al., 2022", refs)["index"] == 2  # year in the joined fragment
    assert cit.resolve("Keskar et al., 2019", refs)["status"] == "found"
    assert cit.resolve("Brown et al., 2020", refs)["index"] == 3
    assert cit.resolve("Mallen et al., 2023b", refs)["reference"].endswith("EMNLP.")  # the letter decides
    assert cit.resolve("Mallen et al., 2023", refs)["status"] == "ambiguous"
    assert cit.resolve("Smith et al., 2021", refs)["status"] == "not_found"
    assert cit.resolve("[2]", refs)["reference"].startswith("Nitish") and cit.resolve("[40]", refs)["status"] == "not_found"


def test_references_are_found_in_the_library_by_arxiv_id_or_title():
    assert LIBRARY.match(REFS[1]) == (LIBRARY.by_id["1909.05858"], "arxiv_id")  # "CTRL" is too short for a title match
    paper, how = LIBRARY.match(REFS[0])
    assert (paper.paper_id, how) == ("arxiv:2310.11511", "title")
    assert LIBRARY.match(REFS[4]) == (None, "")


def test_citation_jobs_and_findings():
    claims = [
        {"claim": "Self-RAG trains an LM to critique its own generations.", "cites": ["Asai et al., 2024", "Asai et al., 2024"]},
        {"claim": "GPT-3 learns from a few examples.", "cites": ["Brown et al., 2020"]},
        {"claim": "Popularity matters.", "cites": ["Mallen et al., 2023", "Nobody et al., 2021"]},
    ]
    jobs, findings = cit.citation_jobs(claims, REFS, LIBRARY)
    assert [(j["claim"], j["paper_id"], j["fulltext"]) for j in jobs] == [(0, "arxiv:2310.11511", True)]  # cited twice, one job
    assert [(f["claim"], f["verdict"]) for f in findings] == [(1, "not_in_library"), (2, "unresolved_reference"), (2, "unresolved_reference")]
    assert cit.query_for({"claim": "As Asai et al., 2024 show, X holds.", "cites": ["Asai et al., 2024"]}) == "As show, X holds."


def _job(passages=3):
    return {"claim": 0, "marker": "Asai et al., 2024", "paper_id": "arxiv:2310.11511", "title": "Self-RAG", "fulltext": True,
            "passages": [{"id": f"E{k}", "chunk_id": f"c{k}", "text": "x " * 300, "section": "3 Method"} for k in range(1, passages + 1)]}


def test_pairs_are_batched_under_the_token_budget_and_fit_groqs_limit():
    from app.prompts import PromptLibrary
    from app.textutil import count_tokens

    claims = [{"claim": "c " * 150, "sentence": "s " * 200}]
    jobs = [_job() for _ in range(9)]
    groups = cit.batches(jobs, claims)
    assert len(groups) > 1 and sum(len(g) for g in groups) == 9
    prompt = PromptLibrary(REPO / "prompts").load("P6-citation-checker")
    for g in groups:
        user = prompt.render({"pairs_block": "\n\n".join(cit.pair_block(k, claims[0], jobs[j]) for k, j in enumerate(g, 1))})
        tokens = count_tokens(user.system + user.user + json.dumps(cit.CITATION_SCHEMA)) * 1.4  # the counter undercounts
        assert tokens + cit.CITATION_MAX_OUTPUT <= 7600


def test_validation_drops_unseen_evidence_and_unfounded_verdicts():
    jobs = [_job(), _job(2), _job()]
    out = {"checks": [
        {"pair": 1, "verdict": "supported", "evidence": ["E2", "E9"], "reason": "E2 says so"},
        {"pair": 2, "verdict": "contradicted", "evidence": ["E3"], "reason": "no E3 was given"},
        {"pair": 2, "verdict": "supported", "evidence": ["E1"], "reason": "duplicate"},
        {"pair": 5, "verdict": "supported", "evidence": ["E1"], "reason": "no such pair"}]}
    checks = cit.validate(json.dumps(out), [0, 1, 2], jobs)
    assert [(c["job"], c["verdict"], c["evidence"]) for c in checks] == \
        [(0, "supported", ["E2"]), (1, "not_found", []), (2, "not_answered", [])]
    with pytest.raises(ValueError):
        cit.validate({"checks": []}, [0], jobs)


def test_request_is_groq_only_with_a_strict_schema():
    body = cit.citation_request("S", "U", "openai/gpt-oss-20b")
    assert body["reasoning_effort"] == "low" and body["response_format"]["json_schema"]["strict"] is True
    item = body["response_format"]["json_schema"]["schema"]["properties"]["checks"]["items"]
    assert set(item["required"]) == set(item["properties"]) and item["additionalProperties"] is False


# ---- through the steps and the API ------------------------------------------------------------


@pytest.fixture(scope="module")
def stub_url():
    server = stubs.serve()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _settings(tmp_path, test_db_url, stub_url):
    from app.config import Settings

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "manifest.csv").write_text("arxiv_id,version,year,primary_category,fulltext,title\n"
                                         "2101.00002,1,2021,cs.CL,1,Some cited paper\n", encoding="utf-8")
    return Settings(
        APP_DATABASE_URL=test_db_url, QDRANT_URL="local::memory:", EMBEDDER_URL=stub_url, PARSER_URL=stub_url,
        AUDIT_ALLOW_PRIVATE_URLS=True, CACHE_DIR=tmp_path / "cache", CORPUS_DIR=corpus,
        PROMPTS_DIR=REPO / "prompts", LOG_LEVEL="WARNING",
    )


def _parsed_run_with_claim(client, stub_url):
    run_id = client.post("/runs", json={"request": {"mode": "audit", "draft_pdf_url": f"{stub_url}/pdf/2101.00001v2"}}).json()["run_id"]
    job = client.post(f"/audit/{run_id}/parse").json()["job_id"]
    for _ in range(100):
        if client.get(f"/jobs/{job}").json()["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    client.post(f"/audit/{run_id}/claims/next")
    claim = {"sentence_id": "b:1/s:0", "type": "citation", "claim": "Author (2020) show that our method improves EM on NQ.",
             "values": [], "comparison": None, "refers_to": [], "cites": ["Author, 2020"]}
    assert client.post(f"/audit/{run_id}/claims", json={"batch": 0, "output": {"claims": [claim]}}).status_code == 200
    return run_id


class _Hit:
    def __init__(self, i, score):
        self.chunk_id, self.paper_id, self.score, self.chunk_type = f"arxiv:2101.00002#w{i}", "arxiv:2101.00002", score, "window"
        self.section, self.title, self.year, self.text = "4 Results", "Some cited paper", 2021, f"passage {i} about EM on NQ"


class _Retriever:
    def __init__(self):
        self.calls = []

    def chunks(self, query, strategy, k, filters):
        self.calls.append((query, strategy, k, filters))
        return [_Hit(i, 0.5 + i / 100) for i in range(5)]


class _Reranker:
    def scores(self, query, texts):
        return [0.1, 0.9, 0.3, 0.8, 0.2]  # the reranker reorders the dense hits


def test_plan_retrieves_the_cited_papers_passages_and_reranks_them(tmp_path, test_db_url, stub_url):
    from fastapi.testclient import TestClient

    from app.audit import steps
    from app.db import Database
    from app.main import create_app
    from app.prompts import PromptLibrary
    from app.audit.store import PaperStore

    settings = _settings(tmp_path, test_db_url, stub_url)
    with TestClient(create_app(settings)) as client:
        run_id = _parsed_run_with_claim(client, stub_url)
    db = Database(test_db_url, 2.0)
    db.open()
    library = cit.Library([cit.LibraryPaper("arxiv:2101.00002", "2101.00002", "Some cited paper", True)])
    retriever = _Retriever()
    from uuid import UUID

    out = steps.citation_plan(db, PromptLibrary(REPO / "prompts"), PaperStore(settings.cache_dir / "audit"), UUID(run_id),
                              library, retriever, _Reranker())
    assert out == {"jobs": 1, "findings": 0, "batches": 1}
    query, strategy, k, filters = retriever.calls[0]
    assert filters == {"paper_id": "arxiv:2101.00002"} and "Author, 2020" not in query and k == cit.RETRIEVE_K
    job = steps._citation_plan(db, UUID(run_id))["jobs"][0]
    assert [p["chunk_id"] for p in job["passages"]] == ["arxiv:2101.00002#w1", "arxiv:2101.00002#w3", "arxiv:2101.00002#w2"]
    assert [p["id"] for p in job["passages"]] == ["E1", "E2", "E3"]
    db.close()


def test_citation_loop_through_the_api(tmp_path, test_db_url, stub_url):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(_settings(tmp_path, test_db_url, stub_url))) as client:
        run_id = _parsed_run_with_claim(client, stub_url)
        first = client.post(f"/audit/{run_id}/citations/next").json()
        # no corpus was built in this test: retrieval fails and is recorded, the pair is still judged
        assert (first["done"], first["pairs"], first["model"]) == (False, 1, "openai/gpt-oss-120b")
        user = first["request"]["messages"][1]["content"]
        assert "PAIR 1" in user and "Cited as Author, 2020: Some cited paper" in user and "(no passage was found" in user
        client.post(f"/audit/{run_id}/citation-checks/error", json={"batch": 0, "status": 429, "retry_after": "20", "model": "openai/gpt-oss-120b"})
        assert client.post(f"/audit/{run_id}/citations/next").json()["attempt"] == 1
        saved = client.post(f"/audit/{run_id}/citation-checks", json={"batch": 0, "output_text": json.dumps(
            {"checks": [{"pair": 1, "verdict": "not_found", "evidence": [], "reason": "no passage"}]})}).json()
        assert saved == {"batch": 0, "not_found": 1}
        assert client.post(f"/audit/{run_id}/citations/next").json()["done"] is True
        result = client.post(f"/audit/{run_id}/result").json()
        cc = result["citation_checks"]
        assert result["implemented"].endswith("+citations") and result["prompt_versions"]["P6-citation-checker"] == "v1"
        assert (cc["summary"]["in_library"], cc["summary"]["not_found"], cc["summary"]["retrieval_errors"]) == (1, 1, 1)
        assert cc["checks"][0]["cited_paper"] == "arxiv:2101.00002" and cc["checks"][0]["marker"] == "Author, 2020"


# ---- the n8n workflow -------------------------------------------------------------------------


def _node(name):
    return next(n for n in AUDIT_WF["nodes"] if n["name"] == name)


def test_audit_workflow_runs_the_citation_loop_after_the_figures():
    conns = AUDIT_WF["connections"]
    assert [o[0]["node"] for o in conns["More figures?"]["main"]] == ["Pause before figure", "Next citations"]
    assert [o[0]["node"] for o in conns["More citations?"]["main"]] == ["Pause before citations", "Assemble result"]
    assert conns["Pause before citations"]["main"][0][0]["node"] == "Check citations"
    assert [o[0]["node"] for o in conns["Check citations"]["main"]] == ["Save citation check", "Record citation error"]
    assert conns["Save citation check"]["main"][0][0]["node"] == "Next citations"
    assert conns["Record citation error"]["main"][0][0]["node"] == "Next citations"
    check = _node("Check citations")
    assert check["parameters"]["url"] == "https://api.groq.com/openai/v1/chat/completions" and check["onError"] == "continueErrorOutput"
    assert check["parameters"]["jsonBody"] == "={{ JSON.stringify($json.request) }}"
    assert "$('Next citations')" in _node("Save citation check")["parameters"]["jsonBody"]
    assert "$('Next citations')" in _node("Record citation error")["parameters"]["jsonBody"]
    assert _node("Next citations")["parameters"]["url"].endswith("/citations/next")


def test_passages_keep_the_part_that_matches_the_claim():
    """F-022: the first 700 characters of an abstract stopped before the method the claim was about."""
    filler = " ".join(f"Background sentence {i} about language models and retrieval in general." for i in range(12))
    text = "Robust Retrieval. Abstract.\n" + filler + " We filter retrieved passages with a natural language inference model. More text follows here."
    window = cit.best_window(text, "designed an NLI model, a natural language inference filter", 700)
    assert window.startswith("Robust Retrieval. Abstract.\n") and "natural language inference model" in window and len(window) <= 700
    assert cit.best_window("short text", "anything", 700) == "short text"


def test_queries_drop_citations_in_any_form():
    q = cit.query_for({"claim": "Yoran et al. (2024) designed an NLI filter, unlike [3, 4] and Lewis and Fan, 2019.",
                       "cites": ["Yoran et al., 2024"]})
    assert "Yoran" not in q and "[3" not in q and "2019" not in q and "NLI filter" in q
