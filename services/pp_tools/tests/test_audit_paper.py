"""app/audit/paper.py: DoclingDocument -> blocks, numbered tables and figures (D31)."""

from audit_fixtures import PDF_CAPTIONS, PNG_1PX, synthetic_doc

from app.audit.paper import build_paper, caption_kind, picture_images, strip_images


def test_caption_kind():
    assert caption_kind("Table 3: Results") == ("table", 3)
    assert caption_kind("Fig. 2. Accuracy") == ("figure", 2)
    assert caption_kind("Figure 10: x") == ("figure", 10)
    assert caption_kind("As shown in Table 3, ...") is None


def test_tables_are_numbered_from_captions_split_tables_merged_and_missing_captions_repaired():
    doc = synthetic_doc()
    paper = build_paper(doc, picture_images(doc), PDF_CAPTIONS)
    by_id = {t.id: t for t in paper.tables}
    assert list(by_id) == ["table:1", "table:2"]
    t1 = by_id["table:1"]
    assert t1.caption_source == "docling" and t1.header_rows == 1
    assert t1.grid[-1] == ["Ours + web", "49.0", "57.1"] and t1.merged_from == ["#/tables/1"]  # the continuation
    t2 = by_id["table:2"]
    assert t2.caption_source == "pdf-text" and t2.caption.startswith("Table 2:") and t2.page == 2


def test_figures_ignore_a_wrong_kind_caption_and_find_their_own():
    doc = synthetic_doc()
    paper = build_paper(doc, picture_images(doc), PDF_CAPTIONS)
    f1, f2 = paper.figures
    assert (f1.id, f1.caption_source, f1.image_png) == ("figure:1", "docling", PNG_1PX)
    assert (f2.id, f2.caption_source) == ("figure:2", "repaired")  # "Table 9" caption rejected, own caption found inside


def test_without_the_pdf_text_layer_the_item_stays_unnumbered_rather_than_guessed():
    paper = build_paper(synthetic_doc(), None, None)
    assert [t.id for t in paper.tables] == ["table:1", "table:u1"]


def test_blocks_sections_and_references():
    paper = build_paper(synthetic_doc())
    assert paper.title == "Corrective Retrieval for Testing" and paper.pages == 3
    claim = next(b for b in paper.blocks if "47.8" in b.text)
    assert claim.section == "1 Introduction" and claim.id.startswith("b:")
    assert paper.references == ["A. Author. 2020. Some cited paper. In ACL."]
    assert all(b.label != "page_footer" for b in paper.blocks)


def test_as_dict_and_strip_images_keep_storage_small():
    doc = synthetic_doc()
    d = build_paper(doc, picture_images(doc), PDF_CAPTIONS).as_dict()
    assert d["figures"][0]["has_image"] is True and "image_png" not in d["figures"][0]
    assert strip_images(doc)["pictures"][0]["image"] is None
