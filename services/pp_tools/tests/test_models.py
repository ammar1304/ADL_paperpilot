"""Unit tests for the request contract. No database needed."""

import pytest
from pydantic import ValidationError

from app.models import IDEA_MAX_CHARS, RunRequest

IDEA = "Use retrieval-augmented claim verification on result tables in ML papers."


def test_valid_novelty_request_applies_defaults():
    req = RunRequest.model_validate({"mode": "novelty", "idea_text": f"  {IDEA}  "})
    assert req.mode == "novelty"
    assert req.idea_text == IDEA  # whitespace stripped
    assert req.query_language == "en"
    assert req.draft_pdf_url is None


def test_valid_audit_request():
    req = RunRequest.model_validate(
        {"mode": "audit", "draft_pdf_url": "https://arxiv.org/pdf/1706.03762", "venue": "NeurIPS", "query_language": "fr"}
    )
    assert str(req.draft_pdf_url) == "https://arxiv.org/pdf/1706.03762"
    assert req.query_language == "fr"


def test_novelty_without_idea_text_is_rejected():
    with pytest.raises(ValidationError, match="idea_text is required"):
        RunRequest.model_validate({"mode": "novelty"})


def test_novelty_with_blank_idea_text_is_rejected():
    with pytest.raises(ValidationError, match="idea_text is required"):
        RunRequest.model_validate({"mode": "novelty", "idea_text": "   "})


def test_audit_without_pdf_url_is_rejected():
    with pytest.raises(ValidationError, match="draft_pdf_url is required"):
        RunRequest.model_validate({"mode": "audit", "idea_text": IDEA})


def test_unknown_mode_is_rejected():
    with pytest.raises(ValidationError) as exc:
        RunRequest.model_validate({"mode": "writing", "idea_text": IDEA})
    assert exc.value.errors()[0]["loc"] == ("mode",)


@pytest.mark.parametrize("text", ["too short", "x" * (IDEA_MAX_CHARS + 1)])
def test_idea_text_length_bounds(text):
    with pytest.raises(ValidationError) as exc:
        RunRequest.model_validate({"mode": "novelty", "idea_text": text})
    assert exc.value.errors()[0]["loc"] == ("idea_text",)


def test_invalid_url_is_rejected():
    with pytest.raises(ValidationError) as exc:
        RunRequest.model_validate({"mode": "audit", "draft_pdf_url": "not a url"})
    assert exc.value.errors()[0]["loc"] == ("draft_pdf_url",)


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError) as exc:
        RunRequest.model_validate({"mode": "novelty", "idea_text": IDEA, "temperature": 2})
    assert exc.value.errors()[0]["loc"] == ("temperature",)
