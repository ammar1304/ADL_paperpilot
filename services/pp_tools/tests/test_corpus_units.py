"""Unit tests: token windows, arXiv parsing, headings, chunkers, metrics. No services needed."""

import pytest

from app.corpus.arxiv import ArxivError, parse_feed, split_id
from app.corpus.chunking import PaperText, chunk_paper, fixed_chunks, structured_chunks
from app.corpus.manifest import ManifestError, ManifestRow, read_manifest, write_manifest
from app.corpus.structure import Block, StructuredPaper, is_tablelike, parse_heading
from app.eval.metrics import recall_at_k, reciprocal_rank
from app.textutil import count_tokens, windows

import stubs

# ---- token windows -----------------------------------------------------------------


def test_count_tokens_counts_words_and_punctuation():
    assert count_tokens("RAG improves EM by 4.2 points.") == 9  # RAG improves EM by 4 . 2 points .


def test_windows_overlap_and_cover_the_text():
    text = " ".join(f"w{i}" for i in range(25))
    parts = list(windows(text, size=10, overlap=3))
    # 25 tokens, windows of 10 stepping by 7: starts at 0, 7, 14, then 21 to reach the end.
    assert [p.split()[0] for p in parts] == ["w0", "w7", "w14", "w21"]
    assert parts[-1].endswith("w24")
    assert all(count_tokens(p) <= 10 for p in parts)


def test_windows_short_text_is_one_window():
    assert list(windows("only a few words", 512, 64)) == ["only a few words"]


@pytest.mark.parametrize("size,overlap", [(0, 0), (10, 10), (10, -1)])
def test_windows_rejects_bad_parameters(size, overlap):
    with pytest.raises(ValueError):
        list(windows("a b c", size, overlap))


# ---- arXiv ---------------------------------------------------------------------------


def test_split_id_handles_urls_versions_and_old_ids():
    assert split_id("http://arxiv.org/abs/2005.11401v4") == ("2005.11401", 4)
    assert split_id("arxiv:2310.11511") == ("2310.11511", None)
    assert split_id("hep-th/9901001v2") == ("hep-th/9901001", 2)
    with pytest.raises(ArxivError):
        split_id("not-an-id")


def test_parse_feed_reads_metadata():
    total, papers = parse_feed(stubs.atom_feed(stubs.PAPERS, 3))
    assert total == 3
    p = papers[0]
    assert (p.arxiv_id, p.version, p.year) == ("2101.00001", 2, 2021)
    assert p.paper_id == "arxiv:2101.00001"
    assert p.abstract.startswith("We study reranking")  # whitespace normalized
    assert p.primary_category == "cs.CL" and p.categories == ["cs.CL", "cs.IR"]
    assert p.authors == ["Ada Example", "Alan Sample"]


def test_parse_feed_raises_on_api_error_entry():
    xml = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>http://arxiv.org/api/errors#incorrect_id_format_for_1234</id>
      <title>Error</title><summary>incorrect id format for 1234</summary></entry></feed>"""
    with pytest.raises(ArxivError, match="incorrect id format"):
        parse_feed(xml)


# ---- manifest ------------------------------------------------------------------------


def test_manifest_round_trip_and_duplicate_check(tmp_path):
    path = tmp_path / "manifest.csv"
    rows = [ManifestRow("2101.00001", 2, 2021, "cs.CL", True, 'A title, with "quotes"')]
    write_manifest(path, rows)
    assert read_manifest(path) == rows
    path.write_text(path.read_text() + "2101.00001,1,2021,cs.CL,0,dup\n")
    with pytest.raises(ManifestError, match="duplicate"):
        read_manifest(path)


def test_missing_manifest_explains_what_to_do(tmp_path):
    with pytest.raises(ManifestError, match="run corpus selection first"):
        read_manifest(tmp_path / "manifest.csv")


# ---- headings and tables -------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("1 Introduction", ("1", "Introduction")),
        ("3.2 Retrieval Module", ("3.2", "Retrieval Module")),
        ("4. Experiments", ("4", "Experiments")),
        ("Related Work", (None, "Related Work")),
        ("References", (None, "References")),
        ("1 BM25 41.2 38.7 45.0", None),  # a table row
        ("2 This sentence is a long paragraph that ends with a period.", None),
        ("We propose a new method", None),
    ],
)
def test_parse_heading(line, expected):
    assert parse_heading(line) == expected


def test_is_tablelike():
    assert is_tablelike("41.2 38.7 45.0")
    assert is_tablelike("BM25")
    assert not is_tablelike("This sentence is ordinary running text that ends here.")


# ---- chunkers --------------------------------------------------------------------------


def _paper(blocks=None) -> PaperText:
    structured = None
    if blocks is not None:
        structured = StructuredPaper(blocks=blocks, body_text="\n\n".join(b.text for b in blocks))
    return PaperText("arxiv:1", "A Title", "An abstract about retrieval.", 2024, structured)


def test_abstract_only_paper_gives_one_chunk_per_strategy():
    paper = _paper()
    fixed = fixed_chunks(paper, 512, 64)
    structured = structured_chunks(paper, 512)
    assert [c.chunk_id for c in fixed] == ["arxiv:1#w0"]
    assert [c.chunk_id for c in structured] == ["arxiv:1#abs"]
    assert "A Title" in fixed[0].text and "An abstract" in fixed[0].text


def test_structured_chunks_ids_types_and_packing():
    long_para = " ".join(["word"] * 30)
    blocks = [
        Block("paragraph", long_para, 1, "1", "Introduction"),
        Block("paragraph", long_para, 1, "1", "Introduction"),
        Block("paragraph", "Short method text.", 1, "3.2", "Retrieval"),
        Block("table", "Table 2: Results.\n41.2 | 38.7", 2, "4", "Experiments", "2"),
        Block("table", "Table 2: Appendix results.\n1.0", 3, "A", "Appendix", "2"),
        Block("figure", "Figure 1: A plot.", 2, "4", "Experiments", "1"),
    ]
    chunks = structured_chunks(_paper(blocks), size=40)
    ids = [c.chunk_id for c in chunks]
    assert ids == [
        "arxiv:1#abs",
        "arxiv:1#sec:1/p1",  # two 30-token paragraphs do not fit in 40: split between them
        "arxiv:1#sec:1/p2",
        "arxiv:1#sec:3.2",
        "arxiv:1#tbl:2",
        "arxiv:1#tbl:2-2",  # duplicate label gets a suffix
        "arxiv:1#fig:1",
    ]
    assert len(set(ids)) == len(ids)
    assert chunks[3].text.startswith("A Title. Section 3.2 Retrieval.")
    assert {c.chunk_type for c in chunks} == {"abstract", "section", "table", "figure"}


def test_both_strategies_cover_the_same_body_text():
    blocks = [Block("paragraph", "Unique sentence about rewriting queries.", 1, "2", "Method")]
    paper = _paper(blocks)
    for strategy in ("fixed", "structured"):
        joined = " ".join(c.text for c in chunk_paper(paper, strategy, 512, 64))
        assert "Unique sentence about rewriting queries." in joined


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError):
        chunk_paper(_paper(), "semantic", 512, 64)


# ---- metrics -------------------------------------------------------------------------------


def test_recall_and_reciprocal_rank():
    ranked = ["a", "b", "c", "d"]
    assert recall_at_k(ranked, {"c"}, 2) == 0.0
    assert recall_at_k(ranked, {"c"}, 3) == 1.0
    assert recall_at_k(ranked, {"a", "x"}, 4) == 0.5
    assert reciprocal_rank(ranked, {"c"}, 4) == pytest.approx(1 / 3)
    assert reciprocal_rank(ranked, {"c"}, 2) == 0.0
    with pytest.raises(ValueError):
        recall_at_k(ranked, set(), 1)
