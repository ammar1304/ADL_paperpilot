"""Reranking the novelty candidate pool (D28), against the stub TEI reranker."""

import pytest

import stubs
from app.novelty import RetrievalConfig, rank_candidates
from app.retrieval.reranker import Reranker, RerankerError
from app.retrieval.store import Hit

IDEA = "train a small classifier to decide when retrieval is needed for each question"


@pytest.fixture(scope="module")
def stub_url():
    server = stubs.serve()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def hit(pid: str, cid: str, text: str, kind: str = "section") -> Hit:
    return Hit(f"{pid}#{cid}", pid, 0.5, kind, "3 Method" if kind == "section" else "Abstract", f"Title {pid}", 2024, text)


class FakeRetriever:
    """Vector search that ranks the off-topic paper first, the way bi-encoders sometimes do."""

    def __init__(self, abstracts: dict[str, str] | None = None):
        self.queries: list[str] = []
        self.abstracts = abstracts or {}

    def by_ids(self, chunk_ids, strategy):
        return [hit(cid.split("#")[0], "abs", self.abstracts[cid.split("#")[0]], "abstract")
                for cid in chunk_ids if cid.endswith("#abs") and cid.split("#")[0] in self.abstracts]

    def chunks(self, query, strategy, k):
        self.queries.append(query)
        return [
            hit("arxiv:A", "sec:1", "retrieval augmented question answering always retrieves passages for every question"),
            hit("arxiv:A", "abs", "we study retrieval augmented generation pipelines", "abstract"),
            hit("arxiv:B", "sec:4", "unrelated introduction about datasets"),
            hit("arxiv:B", "sec:3", "we train a small classifier to decide when retrieval is needed for each question"),
            hit("arxiv:C", "abs", "a survey of dense retrievers", "abstract"),
        ][:k]


def test_reranker_scores_come_back_in_input_order(stub_url):
    reranker = Reranker(stub_url, batch_size=2)  # forces two batches
    texts = ["nothing in common", "train a small classifier", "decide when retrieval is needed"]
    scores = reranker.scores(IDEA, texts)
    assert len(scores) == 3
    assert scores[0] == 0.0 and scores[1] > 0 and scores[2] > scores[1]  # TEI returns them sorted; we restore order
    assert reranker.healthy()


def test_reranker_rejects_malformed_answers(stub_url):
    with pytest.raises(RerankerError):
        Reranker(stub_url, retries=1).scores("RERANK_FAIL " + IDEA, ["a", "b"])


def test_fused_order_without_reranker():
    out = rank_candidates(FakeRetriever(), None, IDEA, ["q1", "q2"], RetrievalConfig(top_papers=2, pool_papers=6))
    assert [c["paper_id"] for c in out["candidates"]] == ["arxiv:A", "arxiv:B"]
    assert out["ranking_method"] == "fused" and out["ranking_note"] == "no reranker configured, fused order kept"
    assert all(e["rerank_score"] is None for e in out["pool"])


def test_reranker_reorders_pool_and_picks_evidence(stub_url):
    out = rank_candidates(FakeRetriever(), Reranker(stub_url), IDEA, ["q1", "q2"], RetrievalConfig(top_papers=2, pool_papers=6, order="rerank"))
    assert out["ranking_method"] == "reranked" and out["ranking_note"] is None
    ranked = [c["paper_id"] for c in out["candidates"]]
    assert ranked == ["arxiv:B", "arxiv:A"]  # the paper that does the idea now comes first
    b = out["candidates"][0]
    assert b["fused_rank"] == 2 and b["rerank_score"] > out["candidates"][1]["rerank_score"]
    assert b["evidence"][0]["chunk_id"] == "arxiv:B#sec:3"  # its most relevant passage first, not its best vector match
    assert [e["paper_id"] for e in out["pool"]] == ["arxiv:B", "arxiv:A", "arxiv:C"]


def test_reranker_failure_keeps_fused_order(stub_url):
    out = rank_candidates(
        FakeRetriever(), Reranker(stub_url, retries=1), "RERANK_FAIL " + IDEA, ["q1"], RetrievalConfig(top_papers=2)
    )
    assert out["ranking_method"] == "fused"
    assert out["ranking_note"].startswith("reranker failed, fused order kept")
    assert [c["paper_id"] for c in out["candidates"]] == ["arxiv:A", "arxiv:B"]


def test_reranking_can_be_switched_off(stub_url):
    out = rank_candidates(FakeRetriever(), Reranker(stub_url), IDEA, ["q1"], RetrievalConfig(rerank=False, top_papers=2))
    assert out["ranking_method"] == "fused" and out["ranking_note"] is None


def test_idea_text_as_an_extra_query():
    retriever = FakeRetriever()
    out = rank_candidates(retriever, None, IDEA, ["q1", "q2"], RetrievalConfig(idea_as_query=True))
    assert retriever.queries == ["q1", "q2", IDEA]
    assert out["queries"] == ["q1", "q2"] and out["idea_as_query"] is True


def test_abstract_scoring_rescues_a_paper_whose_best_chunks_are_off_topic(stub_url):
    """E8 run 1: IRCoT's best vector matches were results sections, so the reranker sank it."""
    abstracts = {"arxiv:C": "we train a small classifier to decide when retrieval is needed for each question"}
    cfg = RetrievalConfig(top_papers=3, pool_papers=6, rerank_chunks_per_paper=1, order="rerank")
    without = rank_candidates(FakeRetriever(abstracts), Reranker(stub_url), IDEA, ["q1"], cfg)
    assert [c["paper_id"] for c in without["candidates"]][0] != "arxiv:C"

    cfg.rerank_abstract = True
    out = rank_candidates(FakeRetriever(abstracts), Reranker(stub_url), IDEA, ["q1"], cfg)
    assert out["rerank_abstract"] is True
    top = out["candidates"][0]
    assert top["paper_id"] == "arxiv:C" and top["evidence"][0]["chunk_id"] == "arxiv:C#abs"


def test_hybrid_order_combines_both_rankings(stub_url):
    cfg = RetrievalConfig(top_papers=3, pool_papers=6, order="hybrid")
    out = rank_candidates(FakeRetriever(), Reranker(stub_url), IDEA, ["q1"], cfg)
    assert out["ranking_method"] == "hybrid"
    pool = {e["paper_id"]: e for e in out["pool"]}
    assert all(e["rerank_rank"] is not None for e in pool.values())

    def hybrid(e):
        return 1 / (60 + e["fused_rank"]) + 1 / (60 + e["rerank_rank"])

    scores = [hybrid(e) for e in out["pool"]]
    assert scores == sorted(scores, reverse=True)


class ManyQueriesRetriever(FakeRetriever):
    """Every local query hits the same 40 library papers, as when the decomposer's queries agree."""

    def chunks(self, query, strategy, k):
        self.queries.append(query)
        return [hit(f"arxiv:L{i:02d}", "sec:1", f"library passage number {i} about retrieval") for i in range(40)][:k]


def _web_paper():
    from app.external.semantic_scholar import ExternalPaper

    return ExternalPaper("s2:web1", "web1", "Learning When to Retrieve", "we train a small classifier to decide when retrieval is needed for each question",
                         2023, "", "", 10)


def test_web_papers_reach_the_reranker_only_when_always_ranked_f009(stub_url):
    reranker = Reranker(stub_url)
    cfg = RetrievalConfig(pool_papers=30, top_papers=6, order="hybrid")
    queries = [f"query {i}" for i in range(5)]
    without = rank_candidates(ManyQueriesRetriever(), reranker, IDEA, queries, cfg, external=[[_web_paper()]])
    assert "s2:web1" not in {p["paper_id"] for p in without["pool"]}  # one list cannot beat five
    with_ = rank_candidates(ManyQueriesRetriever(), reranker, IDEA, queries, cfg, external=[[_web_paper()]], always_rank={"s2:web1"})
    web = next(p for p in with_["pool"] if p["paper_id"] == "s2:web1")
    assert web["rerank_rank"] == 1 and web["source"] == "semantic_scholar"
