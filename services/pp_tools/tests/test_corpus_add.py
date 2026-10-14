"""Adding hand-picked papers to the manifest (app.corpus.add), against the stub arXiv."""

import json

import pytest

import stubs
from app.corpus import add
from app.corpus.arxiv import ArxivClient
from app.corpus.manifest import ManifestRow, read_manifest, write_manifest
from test_build_search_eval import make_settings

TITLES = {p["id"]: p["title"] for p in stubs.PAPERS}


@pytest.fixture(scope="module")
def stub_url():
    server = stubs.serve()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def settings(tmp_path, stub_url):
    s = make_settings(tmp_path, stub_url)
    # The first selection: two of the three stub papers, neither with full text.
    write_manifest(
        s.manifest_path,
        [ManifestRow(i, 1, 2021, "cs.CL", False, TITLES[i]) for i in ("2101.00001", "2203.00002")],
    )
    (s.corpus_dir / "selection.json").write_text(json.dumps({"kept": 2}), encoding="utf-8")
    return s


def summary_of(stdout: str) -> dict:
    """The command prints JSON log lines (httpx), then the pretty-printed summary."""
    return json.loads(stdout[stdout.index("{\n") :])


def write_additions(path, rows):
    lines = ["arxiv_id,fulltext,expected_title,reason"]
    lines += [f'{i},{f},"{t}",{r}' for i, f, t, r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_fetch_latest_returns_known_ids_only(stub_url):
    client = ArxivClient(f"{stub_url}/api/query", f"{stub_url}/pdf", delay_s=0)
    found = client.fetch_latest(["2405.00003", "2999.99999"])
    assert list(found) == ["2405.00003"]
    assert found["2405.00003"].version == 3  # latest version, not v1


def test_title_similarity_tolerates_case_and_punctuation():
    assert add.title_similarity("Self-RAG: Learning to Retrieve", "self rag learning to retrieve") == 1.0
    assert add.title_similarity("Dense Passage Retrieval for Open-Domain QA", "Atlas: Few-shot Learning") < add.TITLE_MATCH


def test_add_reports_every_outcome_and_writes_only_good_rows(settings, monkeypatch, capsys):
    monkeypatch.setattr(add, "get_settings", lambda: settings)
    write_additions(
        settings.corpus_dir / "additions.csv",
        [
            ("2405.00003", 1, TITLES["2405.00003"], "new paper"),
            ("2101.00001", 1, TITLES["2101.00001"], "already there; now full text"),
            ("2203.00002", 0, TITLES["2203.00002"].upper(), "already there; case does not matter"),
            ("2101.00009", 0, "A paper that does not exist", "typo"),
        ],
    )
    before = settings.manifest_path.read_text()

    assert add.main(["--dry-run"]) == 1  # one id not found
    assert settings.manifest_path.read_text() == before  # dry run writes nothing
    capsys.readouterr()

    assert add.main([]) == 1
    out = summary_of(capsys.readouterr().out)
    assert out["added"] == ["2405.00003"]
    assert out["upgraded_to_fulltext"] == ["2101.00001"]
    assert out["already_present"] == ["2203.00002"]
    assert out["not_found"] == ["2101.00009"]
    assert out["written"] is True

    rows = {r.arxiv_id: r for r in read_manifest(settings.manifest_path)}
    assert set(rows) == {"2101.00001", "2203.00002", "2405.00003"}
    assert rows["2405.00003"].version == 3 and rows["2405.00003"].fulltext is True and rows["2405.00003"].year == 2024
    assert rows["2101.00001"].fulltext is True
    selection = json.loads((settings.corpus_dir / "selection.json").read_text())
    assert selection["kept"] == 2 and selection["added_by_hand"]["added"] == ["2405.00003"]


def test_wrong_title_is_refused(settings, monkeypatch, capsys):
    """A mistyped id that exists on arXiv must not add the wrong paper."""
    monkeypatch.setattr(add, "get_settings", lambda: settings)
    write_additions(settings.corpus_dir / "additions.csv", [("2405.00003", 1, "Dense Passage Retrieval for Open-Domain QA", "typo")])
    assert add.main([]) == 1
    out = summary_of(capsys.readouterr().out)
    assert out["added"] == [] and out["title_mismatch"][0]["arxiv_id"] == "2405.00003"
    assert out["written"] is False
    assert "2405.00003" not in {r.arxiv_id for r in read_manifest(settings.manifest_path)}


def test_malformed_additions_file_is_rejected(settings, monkeypatch, capsys):
    monkeypatch.setattr(add, "get_settings", lambda: settings)
    write_additions(settings.corpus_dir / "additions.csv", [("2405.00003v2", 1, "t", "versioned ids are not allowed")])
    assert add.main([]) == 2
    assert "not a bare arXiv id" in capsys.readouterr().err
