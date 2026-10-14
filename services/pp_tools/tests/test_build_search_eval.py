"""Integration: build the knowledge base from a manifest, search it, evaluate it.

Uses the offline stubs (fake arXiv and fake embedder) and an in-process Qdrant.
This checks the plumbing and the logic; it says nothing about retrieval
quality, which needs the real embedding model and real papers.
"""

import json

import pytest

import stubs
from app.config import Settings
from app.corpus.arxiv import ArxivClient
from app.corpus.build import CorpusBuilder
from app.corpus.manifest import ManifestRow, write_manifest
from app.eval.retrieval import QueryFileError, evaluate, load_queries, markdown_table
from app.retrieval.embedder import Embedder
from app.retrieval.search import Retriever
from app.retrieval.store import Store, make_client


@pytest.fixture(scope="module")
def stub_url():
    server = stubs.serve()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def make_settings(tmp_path, stub_url: str) -> Settings:
    return Settings(
        APP_DATABASE_URL="postgresql://unused@127.0.0.1:1/unused",
        QDRANT_URL="local::memory:",
        EMBEDDER_URL=stub_url,
        CORPUS_DIR=tmp_path / "corpus",
        CACHE_DIR=tmp_path / "cache",
        ARXIV_API_URL=f"{stub_url}/api/query",
        ARXIV_PDF_URL=f"{stub_url}/pdf",
        ARXIV_DELAY_S=0,
        CHUNK_TOKENS=120,
        CHUNK_OVERLAP=20,
    )


def write_stub_manifest(settings: Settings, fulltext_ids: set[str]) -> None:
    rows = [
        ManifestRow(p["id"], p["version"], int(p["published"][:4]), "cs.CL", p["id"] in fulltext_ids, p["title"])
        for p in stubs.PAPERS
    ]
    write_manifest(settings.manifest_path, rows)


@pytest.fixture
def built(tmp_path, stub_url):
    settings = make_settings(tmp_path, stub_url)
    write_stub_manifest(settings, {"2101.00001", "2405.00003"})
    arxiv = ArxivClient(settings.arxiv_api_url, settings.arxiv_pdf_url, delay_s=0)
    embedder = Embedder(settings.embedder_url, batch_size=8)
    store = Store(make_client(settings.qdrant_url))
    progress: list[dict] = []
    report = CorpusBuilder(settings, arxiv, embedder, store, progress.append).run(recreate=True)
    yield settings, report, Retriever(embedder, store), store, progress
    arxiv.close()
    embedder.close()


def test_build_indexes_every_paper_in_both_strategies(built):
    settings, report, _, store, progress = built
    assert report.papers_in_manifest == 3 and report.papers_indexed == 3
    assert report.fulltext_requested == 2 and report.fulltext_indexed == 2
    assert report.pdf_failures == {} and report.missing_metadata == []
    assert report.tables_found == 2 and report.figures_found == 2
    assert report.chunks["structured"]["abstract"] == 3
    assert report.chunks["structured"]["table"] == 2
    assert "window" in report.chunks["fixed"]

    stats = store.stats()
    assert stats["fixed"]["papers"] == 3 and stats["structured"]["papers"] == 3
    assert stats["structured"]["chunk_types"]["table"] == 2

    assert (settings.cache_dir / "build_report.json").exists()
    assert {p["stage"] for p in progress} >= {"metadata", "pdf", "index:fixed", "index:structured"}


def test_rebuild_is_idempotent_and_uses_the_cache(built, stub_url):
    settings, first, retriever, store, _ = built
    before = store.stats()
    # Point arXiv at a dead address: everything must come from the cache now.
    dead = ArxivClient("http://127.0.0.1:9/api", "http://127.0.0.1:9/pdf", delay_s=0, retries=1)
    second = CorpusBuilder(settings, dead, retriever.embedder, store).run(recreate=False)
    assert second.chunks == first.chunks
    assert store.stats() == before  # same deterministic point ids: no duplicates


def test_rebuild_reuses_vectors_and_embeds_only_what_changed(built):
    """Adding papers must not re-embed the whole library (88 minutes on a laptop CPU)."""
    settings, first, retriever, store, _ = built
    assert first.embedding_model == "stub/hashed-bow"
    total = {k: sum(v.values()) for k, v in first.chunks.items()}
    assert first.chunks_embedded == total and first.chunks_reused == {"fixed": 0, "structured": 0}

    arxiv = ArxivClient(settings.arxiv_api_url, settings.arxiv_pdf_url, delay_s=0)
    same = CorpusBuilder(settings, arxiv, retriever.embedder, store).run(recreate=True)
    assert same.chunks_embedded == {"fixed": 0, "structured": 0}
    assert same.chunks_reused == total
    assert store.stats()["structured"]["chunks"] == total["structured"]  # recreated, nothing lost

    # One more paper gets its full text: only that paper's new chunks are embedded.
    write_stub_manifest(settings, {"2101.00001", "2405.00003", "2203.00002"})
    more = CorpusBuilder(settings, arxiv, retriever.embedder, store).run(recreate=True)
    new_structured = sum(more.chunks["structured"].values())
    assert 0 < more.chunks_embedded["structured"] < new_structured
    assert more.chunks_reused["structured"] + more.chunks_embedded["structured"] == new_structured
    hits = retriever.chunks("rewriting multi-hop hotpotqa musique iterative", "structured", 5)
    assert hits[0].paper_id == "arxiv:2203.00002"  # the new vectors are searchable
    arxiv.close()


def test_vectors_from_another_model_are_not_reused(built):
    settings, first, retriever, store, _ = built
    from app.retrieval.store import collection_name

    name = collection_name("structured")
    points, _ = store.client.scroll(name, limit=1000, with_payload=True, with_vectors=True)
    texts = {p.payload["text"] for p in points}
    assert all(p.payload.get("embedding_model") == "stub/hashed-bow" for p in points)
    store.client.set_payload(name, payload={"embedding_model": "other/model"}, points=[p.id for p in points])
    arxiv = ArxivClient(settings.arxiv_api_url, settings.arxiv_pdf_url, delay_s=0)
    again = CorpusBuilder(settings, arxiv, retriever.embedder, store).run(recreate=True)
    assert again.chunks_reused["structured"] == 0
    assert again.chunks_embedded["structured"] == len(texts)
    arxiv.close()


def test_pdf_failure_falls_back_to_abstract(tmp_path, stub_url):
    settings = make_settings(tmp_path, stub_url)
    write_stub_manifest(settings, {"2101.00001"})
    # A cached PDF that is not a PDF: extraction fails, the paper keeps its abstract.
    bad = settings.cache_dir / "pdf" / "2101.00001v2.pdf"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"%PDF-1.4 broken")
    store = Store(make_client("local::memory:"))
    embedder = Embedder(stub_url)
    report = CorpusBuilder(settings, ArxivClient(settings.arxiv_api_url, settings.arxiv_pdf_url, 0), embedder, store).run()
    assert "arxiv:2101.00001" in report.pdf_failures
    assert report.papers_indexed == 3 and report.fulltext_indexed == 0


def test_search_finds_the_right_paper_and_tables(built):
    _, _, retriever, _, _ = built
    for strategy in ("fixed", "structured"):
        top = retriever.papers("citation faithfulness attribution metric", strategy, k=3)
        assert top[0].paper_id == "arxiv:2405.00003"
        assert len({h.paper_id for h in top}) == len(top)  # distinct papers
    tables = retriever.chunks("exact match F1 results", "structured", 5, {"chunk_type": "table"})
    assert tables and all(h.chunk_type == "table" for h in tables)
    assert tables[0].chunk_id.endswith("#tbl:1")


def test_evaluate_and_query_files(built, tmp_path):
    _, _, retriever, _, _ = built
    qfile = tmp_path / "q.jsonl"
    qfile.write_text(
        "\n".join(
            json.dumps(q)
            for q in [
                {"id": "q1", "query": "rewriting search queries for multi-hop hotpotqa", "relevant_papers": ["arxiv:2203.00002"], "type": "paraphrase", "split": "dev"},
                {"id": "q2", "query": "cross-encoder reranker on natural questions", "relevant_papers": ["arxiv:2101.00001"], "type": "section", "split": "test"},
            ]
        )
        + "\n// comments and blank lines are ignored\n\n"
    )
    queries = load_queries(qfile)
    summary = evaluate(retriever, queries, ["fixed", "structured"], [1, 3])
    assert summary["overall"]["n"] == 2
    for strategy in ("fixed", "structured"):
        assert summary["overall"][strategy]["recall@1"] == 1.0
        assert summary["overall"][strategy]["mrr@3"] == 1.0
    assert set(summary["by_type"]) == {"paraphrase", "section"}
    assert "| all | 2 | fixed |" in markdown_table(summary, ["fixed", "structured"])

    worksheet = tmp_path / "w.csv"
    worksheet.write_text(
        "id,split,type,relevant_papers,query,context\n"
        "a,dev,table,arxiv:2101.00001,exact match of the reranker,ctx\n"
        "b,test,section,arxiv:2203.00002,,not written yet\n"
    )
    assert [q.id for q in load_queries(worksheet)] == ["a"]

    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"id": "x", "query": "q", "relevant_papers": ["p"], "type": "vibes"}) + "\n")
    with pytest.raises(QueryFileError, match="type must be one of"):
        load_queries(bad)


def test_select_writes_manifest_and_refuses_to_overwrite(tmp_path, stub_url, monkeypatch, capsys):
    from app.config import get_settings
    from app.corpus import select
    from app.corpus.manifest import read_manifest

    for key, value in {
        "APP_DATABASE_URL": "postgresql://unused@127.0.0.1:1/unused",
        "QDRANT_URL": "local::memory:",
        "CORPUS_DIR": str(tmp_path / "corpus"),
        "ARXIV_API_URL": f"{stub_url}/api/query",
        "ARXIV_PDF_URL": f"{stub_url}/pdf",
        "ARXIV_DELAY_S": "0",
        "LOG_LEVEL": "WARNING",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        assert select.main(["--max-papers", "3", "--fulltext", "2"]) == 0
        rows = read_manifest(tmp_path / "corpus" / "manifest.csv")
        assert [r.arxiv_id for r in rows] == [p["id"] for p in stubs.PAPERS]
        assert sum(r.fulltext for r in rows) == 2
        summary = json.loads((tmp_path / "corpus" / "selection.json").read_text())
        assert summary["kept"] == 3 and summary["papers_per_year"] == {"2021": 1, "2022": 1, "2024": 1}
        assert select.main(["--max-papers", "3"]) == 1  # exists, no --force
        assert "already exists" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()
