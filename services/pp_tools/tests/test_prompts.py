"""The prompt library and its consistency with the n8n workflow and the pp-tools contracts."""

import json
import re
import typing
from pathlib import Path

import pytest

from app.novelty import DecomposerOutput, JudgeOutput, Label
from app.prompts import PromptError, PromptLibrary, parse_prompt_file

REPO = Path(__file__).resolve().parents[3]
LIBRARY = PromptLibrary(REPO / "prompts")
WORKFLOW = json.loads((REPO / "n8n" / "workflows" / "20-novelty.json").read_text())

GOOD = """---
name: P9-test
version: v2
technique: few-shot
---
Notes that are not sent.
## System
You answer about {{topic}}. JSON like {"a": 1} stays as is.
## User
Question: {{ question }}
"""


def test_parse_and_render():
    p = parse_prompt_file(GOOD)
    assert (p.name, p.version, p.meta["technique"]) == ("P9-test", "v2", "few-shot")
    assert p.variables == {"topic", "question"}
    r = p.render({"topic": "RAG", "question": "why?"})
    assert r.system == 'You answer about RAG. JSON like {"a": 1} stays as is.'
    assert r.user == "Question: why?"
    assert "Notes" not in r.system


def test_missing_variable_is_an_error():
    with pytest.raises(PromptError, match="missing variables"):
        parse_prompt_file(GOOD).render({"topic": "RAG"})


@pytest.mark.parametrize(
    "text,error",
    [
        ("no front matter", "front-matter"),
        ("---\nname: x\n---\n## System\na\n## User\nb", "needs 'version'"),
        ("---\nname: P1-x\nversion: v1\n---\n## User\nb\n## System\na", "in that order"),
        ("---\nname: P1-x\nversion: v1\n---\n## System\n\n## User\nb", "must not be empty"),
    ],
)
def test_malformed_files_are_rejected(text, error):
    with pytest.raises(PromptError, match=error):
        parse_prompt_file(text)


def test_library_rejects_path_tricks():
    with pytest.raises(PromptError):
        LIBRARY.load("../secrets")
    with pytest.raises(PromptError):
        LIBRARY.load("P1-decomposer", "v1/../../x")


# ---- the real library -----------------------------------------------------------------------


def test_every_active_prompt_loads():
    assert LIBRARY.check_all() == []
    assert set(LIBRARY.active_versions()) == {"P1-decomposer", "P2-search-planner", "P3-novelty-judge", "P4-claim-extractor", "P5-figure-checker"}


def test_prompt_variables_are_the_ones_pp_tools_provides():
    assert LIBRARY.load("P1-decomposer").variables == {"idea_text"}
    assert LIBRARY.load("P1-decomposer", "v1").variables == {"idea_text"}
    assert LIBRARY.load("P2-search-planner").variables == {
        "idea_block", "facets_block", "round_block", "candidates_block", "history_block",
    }
    assert LIBRARY.load("P3-novelty-judge").variables == {"library_block", "facets_block", "candidates_block", "feedback_block"}
    # v1 stays loadable for before/after comparisons
    assert LIBRARY.load("P3-novelty-judge", "v1").variables == {"facets_block", "candidates_block", "feedback_block"}


def test_decomposer_example_satisfies_its_own_contract():
    """The worked example inside P1 must be valid output, or it teaches the model the wrong format."""
    system = LIBRARY.load("P1-decomposer").system
    example = system.split("Answer:", 1)[1].strip().splitlines()[0]
    wrapped = json.loads(example)
    assert set(wrapped) == {"output"}  # n8n's Structured Output Parser expects this wrapper
    parsed = DecomposerOutput.model_validate(wrapped["output"])
    # The example must also obey the prompt's own rules, or it teaches the opposite (v2: 4 to 6 queries).
    assert 4 <= len(parsed.queries) <= 6


# ---- workflow schemas match pp-tools contracts ------------------------------------------------


def _parser_schema(node_name: str) -> dict:
    node = next(n for n in WORKFLOW["nodes"] if n["name"] == node_name)
    return json.loads(node["parameters"]["inputSchema"])


def test_facets_schema_matches_decomposer_contract():
    schema = _parser_schema("Facets schema")
    assert set(schema["required"]) == set(DecomposerOutput.model_fields)
    facets = schema["properties"]["facets"]
    assert set(facets["required"]) == {"task", "method", "data", "result"}
    assert schema["properties"]["queries"]["items"]["properties"]["facets"]["items"]["enum"] == ["task", "method", "data", "result"]


def test_judgement_schema_matches_judge_contract():
    schema = _parser_schema("Judgement schema")
    assert set(schema["required"]) == set(JudgeOutput.model_fields)
    item = schema["properties"]["matches"]["items"]
    assert set(item["required"]) == {"paper_id", "overlap", "evidence", "rationale"}
    for facet in ("task", "method", "data", "result"):
        assert item["properties"]["overlap"]["properties"][facet]["enum"] == list(typing.get_args(Label))


def test_plan_schema_matches_planner_contract():
    from app.novelty import PlanOutput, PlanQuery

    schema = _parser_schema("Plan schema")
    assert set(schema["properties"]) == set(PlanOutput.model_fields)
    assert schema["properties"]["decision"]["enum"] == ["search", "stop"]
    item = schema["properties"]["queries"]["items"]
    assert set(item["required"]) == set(PlanQuery.model_fields)
    assert item["properties"]["where"]["enum"] == ["local", "web"]
    assert schema["properties"]["queries"]["maxItems"] == 3


def test_search_loop_is_wired_with_both_exits():
    conns = WORKFLOW["connections"]
    assert conns["Retrieve candidates"]["main"] == [[{"node": "Render planner prompt", "type": "main", "index": 0}]]
    assert [o[0]["node"] for o in conns["Agent should plan?"]["main"]] == ["Plan search", "Render judge prompt"]
    assert [o[0]["node"] for o in conns["Search again?"]["main"]] == ["Render planner prompt", "Render judge prompt"]


def test_workflow_llm_nodes_use_the_documented_models():
    models = {n["name"]: n["parameters"].get("model") or n["parameters"].get("modelName") for n in WORKFLOW["nodes"] if "lmChat" in n["type"]}
    assert models == {
        "Groq gpt-oss-20b": "openai/gpt-oss-20b",
        "Groq gpt-oss-20b (planner)": "openai/gpt-oss-20b",
        "Groq gpt-oss-120b": "openai/gpt-oss-120b",
        "Gemini fallback (decomposer)": "models/gemini-3.5-flash-lite",
        "Gemini fallback (planner)": "models/gemini-3.5-flash-lite",
        "Gemini fallback (judge)": "models/gemini-3.5-flash-lite",
    }
    for n in WORKFLOW["nodes"]:
        if "lmChat" in n["type"]:
            assert n["parameters"]["options"]["temperature"] == 0


def test_workflow_prompts_come_from_the_library():
    """Prompts are never typed into the LLM nodes: they come from the rendered library output."""
    for n in WORKFLOW["nodes"]:
        if n["type"].endswith("chainLlm"):
            assert n["parameters"]["text"] == "={{ $json.user }}"
            assert n["parameters"]["messages"]["messageValues"][0]["message"] == "={{ $json.system }}"
    assert re.search(r"/prompts/P1-decomposer/render", json.dumps(WORKFLOW))
    assert re.search(r"/plan-prompt", json.dumps(WORKFLOW))


def test_planner_example_satisfies_its_own_contract():
    from app.novelty import PlanOutput

    system = LIBRARY.load("P2-search-planner").system
    example = system.split("Answer:", 1)[1].strip().splitlines()[0]
    wrapped = json.loads(example)
    assert set(wrapped) == {"output"}
    plan = PlanOutput.model_validate(wrapped["output"])
    assert plan.decision == "search" and {q.where for q in plan.queries} == {"local", "web"}
