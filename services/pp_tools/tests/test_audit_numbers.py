"""Step 4, the number check (D35). Each case below is a layout or a mistake met on real papers;
the numbers are invented."""

from app.audit.numbers import check_claims, claimed_number, index_table, parse_number, similarity

# CRAG-like: a full-width row names the backbone of the rows below it; ablation rows ("w/o.")
# belong to the method above them.
MAIN = {"id": "table:1", "grid": [
    ["Method", "PopQA (Accuracy)", "Bio (FactScore)", "Time"],
    ["LLaMA2-hf-7b", "LLaMA2-hf-7b", "LLaMA2-hf-7b", ""],
    ["RAG", "50.5", "44.9", "1.00"],
    ["CRAG", "54.9", "47.7", "1.40"],
    ["Self-RAG*", "29.0", "32.2", "1.10"],
    ["Self-CRAG", "49.0", "69.1", "1.60"],
    ["SelfRAG-LLaMA2-7b", "SelfRAG-LLaMA2-7b", "SelfRAG-LLaMA2-7b", ""],
    ["RAG", "52.8", "59.2", "1.00"],
    ["CRAG", "59.8", "74.1", "1.30"],
]}
ABLATION = {"id": "table:2", "grid": [
    ["", "LLaMA2-hf-7b"],
    ["CRAG", "54.9"],
    ["w/o. refinement", "49.8"],
    ["Self-CRAG", "49.0"],
    ["w/o. refinement", "35.9"],
]}
# Adaptive-RAG-like: two header rows (model over metric), a group column written once,
# and a second sub-table stacked below with its own headers.
STACKED = {"id": "table:3", "grid": [
    ["", "", "SQuAD", "SQuAD"],
    ["Types", "Methods", "EM", "Step"],
    ["Simple", "Single-step Approach", "27.80", "1.00"],
    ["", "No Retrieval", "3.60", "0.00"],
    ["Adaptive", "Adaptive-RAG (Ours)", "26.80", "1.37"],
    ["", "", "MuSiQue", "MuSiQue"],
    ["Types", "Methods", "EM", "Step"],
    ["Simple", "Single-step Approach", "13.80", "1.00"],
    ["", "No Retrieval", "2.40", "0.00"],
]}
PAPER = {"tables": [MAIN, ABLATION, STACKED]}


def _margin(system, baseline, diff, text, kind="absolute", refs=("table:1",), direction="better"):
    return {"sentence_id": "b:1/s:0", "claim": text, "refers_to": list(refs), "values": [],
            "comparison": {"system": system, "baseline": baseline, "difference": diff, "kind": kind, "direction": direction}}


def _value(system, value, text, dataset="", metric="", setting="", refs=("table:1",)):
    return {"sentence_id": "b:1/s:0", "claim": text, "refers_to": list(refs), "comparison": None,
            "values": [{"value": value, "system": system, "dataset": dataset, "metric": metric, "setting": setting, "role": "value"}]}


def _one(claim):
    checks = check_claims(PAPER, [claim])["checks"]
    assert len(checks) == 1
    return checks[0]


def test_numbers_in_cells_and_claims():
    assert parse_number("54.9") == (54.9, 1, False) and parse_number("**61.8**") == (61.8, 1, False)
    assert parse_number("1,084") == (1084.0, 0, False) and parse_number("92.5 (3.1)") == (92.5, 1, False)
    assert parse_number("-") is None and parse_number("Small (60M)") is None
    assert claimed_number("7.0%") == (7.0, 1, True) and claimed_number("0.77B vs 7B")[0] == 0.77


def test_similarity_handles_abbreviations_without_confusing_methods():
    assert similarity("Biography", "Bio") == 0.9 and similarity("Arc-Challenge", "ARC") == 0.9
    assert similarity("SelfRAG-LLaMA27b", "SelfRAG-LLaMA2-7b") == 1.0
    assert similarity("CRAG", "Self-CRAG") < 0.75 and similarity("RAG", "CRAG") < 0.75


def test_index_keeps_sections_groups_ablations_and_stacked_blocks():
    main = {(c.row_ctx[-1], c.section, c.col): c for c in index_table(MAIN)}
    assert main[("CRAG", "SelfRAG-LLaMA2-7b", 1)].text == "59.8" and main[("CRAG", "LLaMA2-hf-7b", 1)].text == "54.9"
    assert main[("CRAG", "LLaMA2-hf-7b", 1)].col_ctx == ["Method", "PopQA (Accuracy)"] or \
        main[("CRAG", "LLaMA2-hf-7b", 1)].col_ctx == ["PopQA (Accuracy)"]
    ablation = [c for c in index_table(ABLATION) if c.row_ctx[-1] == "w/o. refinement"]
    assert [c.row_ctx for c in ablation] == [["CRAG", "w/o. refinement"], ["Self-CRAG", "w/o. refinement"]]
    stacked = index_table(STACKED)
    no_ret = [c for c in stacked if c.row_ctx[-1] == "No Retrieval" and c.col == 2]
    assert [(c.row_ctx, c.col_ctx, c.block) for c in no_ret] == [
        (["Simple", "No Retrieval"], ["SQuAD", "EM"], 0), (["Simple", "No Retrieval"], ["MuSiQue", "EM"], 1)]


def test_a_true_margin_is_supported_in_the_right_column_and_section():
    c = _one(_margin("CRAG", "RAG", "7.0%", "CRAG outperformed RAG by 7.0% accuracy on PopQA when based on SelfRAG-LLaMA27b."))
    assert c["verdict"] == "supported" and c["cell"]["text"] == "59.8" and c["baseline_cell"]["text"] == "52.8"
    c = _one(_margin("CRAG", "RAG", "4.4", "CRAG beats RAG by 4.4 on PopQA with LLaMA2-hf-7b."))
    assert c["verdict"] == "supported" and c["cell"]["section"] == "LLaMA2-hf-7b"


def test_a_wrong_margin_is_contradicted_with_the_table_values():
    c = _one(_margin("CRAG", "RAG", "7.4%", "CRAG outperformed RAG by 7.4% accuracy on PopQA when based on SelfRAG-LLaMA27b."))
    assert c["verdict"] == "contradicted" and "59.8 vs 52.8" in c["note"]


def test_an_absolute_claim_cannot_pass_as_a_relative_difference():
    """E3: 0.5 points planted as 0.8 passed because 59.8 vs 59.3 is 0.84% relative."""
    rel = round((59.8 - 52.8) / 52.8 * 100, 1)  # 13.3
    assert _one(_margin("CRAG", "RAG", f"{rel}", "CRAG beats RAG by 13.3 on PopQA SelfRAG-LLaMA2-7b."))["verdict"] == "contradicted"
    c = _one(_margin("CRAG", "RAG", f"{rel}%", "CRAG beats RAG by 13.3% relative on PopQA SelfRAG-LLaMA2-7b.", kind="relative"))
    assert c["verdict"] == "supported" and c["matched_as"] == "relative"


def test_direction_and_lower_is_better_columns():
    c = _one(_margin("RAG", "CRAG", "7.0", "RAG beats CRAG by 7.0 on PopQA SelfRAG-LLaMA2-7b."))
    assert c["verdict"] == "contradicted" and c["note"] == "the direction is reversed"
    c = _one(_margin("RAG", "CRAG", "0.30", "RAG is faster than CRAG by 0.30 in Time SelfRAG-LLaMA2-7b."))
    assert c["verdict"] == "supported"  # Time: lower is better


def test_ablation_rows_belong_to_the_method_the_text_names():
    """E3: "CRAG w/o. refinement" must not be read under Self-CRAG because the baseline is Self-CRAG."""
    c = _one(_margin("w/o. refinement", "Self-CRAG", "0.8", "CRAG w/o. refinement beats Self-CRAG by 0.8 with LLaMA2-hf-7b.", refs=("table:2",)))
    assert c["verdict"] == "supported" and c["cell"]["text"] == "49.8"
    c = _one(_margin("CRAG", "w/o. refinement", "5.1", "CRAG beats CRAG w/o. refinement by 5.1.", refs=("table:2",)))
    assert c["verdict"] == "supported" and c["baseline_cell"]["text"] == "49.8"


def test_row_names_do_not_pick_columns_and_blocks_do_not_mix():
    """E3: "Single-step Approach" picked the "Step" column; SQuAD rows were paired with MuSiQue rows."""
    c = _one(_margin("Single-step Approach", "No Retrieval", "11.40", "Single-step Approach beats No Retrieval by 11.40 EM on MuSiQue.",
                     refs=("table:3",)))
    assert c["verdict"] == "supported" and c["cell"]["column"] == "MuSiQue / EM"
    c = _one(_margin("Adaptive-RAG (Ours)", "No Retrieval", "23.20", "Our method beats No Retrieval by 23.20 EM on SQuAD.", refs=("table:3",)))
    assert c["verdict"] == "supported"


def test_values_supported_contradicted_or_unverifiable():
    assert _one(_value("CRAG", "59.8", "CRAG reaches 59.8 on PopQA.", dataset="PopQA", setting="SelfRAG-LLaMA2-7b"))["verdict"] == "supported"
    c = _one(_value("CRAG", "58.9", "CRAG reaches 58.9 on PopQA.", dataset="PopQA", setting="SelfRAG-LLaMA2-7b"))
    assert c["verdict"] == "contradicted" and c["table_says"] == "59.8"
    assert _one(_value("critic", "90%", "The critic agrees 90% of the time."))["verdict"] == "unverifiable"
    assert _one(_value("our method", "26.80", "Our method reaches 26.80 EM on SQuAD.", refs=("table:3",)))["verdict"] == "supported"


def test_values_without_a_system_and_margins_already_checked_are_skipped():
    claim = _margin("CRAG", "RAG", "7.0", "CRAG beats RAG by 7.0 on PopQA SelfRAG-LLaMA2-7b.")
    claim["values"] = [{"value": "7.0", "system": "CRAG", "dataset": "PopQA", "metric": "", "setting": "", "role": "difference"},
                       {"value": "14k", "system": "", "dataset": "PopQA", "metric": "samples", "setting": "", "role": "value"}]
    out = check_claims(PAPER, [claim])
    assert [c["kind"] for c in out["checks"]] == ["difference"] and out["summary"]["supported"] == 1
