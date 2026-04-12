"""Tests for review.py — consensus logic, JSON extraction, prompt building."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import review as rv


# ── check_consensus ────────────────────────────────────────────────────────────

def _r(verdict, model="m1"):
    return {"model": model, "verdict": verdict, "issues": [], "summary": ""}


def test_consensus_all_approve():
    reviews = [_r("APPROVE", "m1"), _r("APPROVE", "m2"), _r("APPROVE", "m3")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is True
    assert approve == 3
    assert total == 3


def test_consensus_majority_approve():
    reviews = [_r("APPROVE", "m1"), _r("APPROVE", "m2"), _r("NEEDS_CHANGES", "m3")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is True
    assert approve == 2
    assert total == 3


def test_consensus_approve_with_error():
    """2 APPROVE + 1 ERROR → 2/2 successful = consensus."""
    reviews = [_r("APPROVE", "m1"), _r("APPROVE", "m2"), _r("ERROR", "m3")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is True
    assert approve == 2
    assert total == 2


def test_no_consensus_split_with_error():
    """1 APPROVE + 1 NEEDS_CHANGES + 1 ERROR → 1/2 successful = no consensus."""
    reviews = [_r("APPROVE", "m1"), _r("NEEDS_CHANGES", "m2"), _r("ERROR", "m3")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is False
    assert approve == 1
    assert total == 2


def test_no_consensus_all_needs_changes_with_error():
    """2 NEEDS_CHANGES + 1 ERROR → 0/2 successful = no consensus."""
    reviews = [_r("NEEDS_CHANGES", "m1"), _r("NEEDS_CHANGES", "m2"), _r("ERROR", "m3")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is False
    assert approve == 0
    assert total == 2


def test_no_consensus_all_errors():
    """All errors → 0 successful reviews."""
    reviews = [_r("ERROR", "m1"), _r("ERROR", "m2"), _r("ERROR", "m3")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is False
    assert approve == 0
    assert total == 0


def test_dev_mode_single_approve():
    """1/1 in dev mode → consensus."""
    reviews = [_r("APPROVE")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is True


def test_dev_mode_single_needs_changes():
    reviews = [_r("NEEDS_CHANGES")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is False


# ── extract_json_from_content ──────────────────────────────────────────────────

def test_extract_plain_json():
    content = '{"verdict": "APPROVE", "issues": [], "summary": "ok"}'
    result = rv.extract_json_from_content(content)
    assert result is not None
    assert result["verdict"] == "APPROVE"


def test_extract_json_in_code_fence():
    content = '```json\n{"verdict": "NEEDS_CHANGES", "issues": [{"category": "fidelity", "description": "x", "file": "f.go", "fix": "y"}], "summary": "needs work"}\n```'
    result = rv.extract_json_from_content(content)
    assert result is not None
    assert result["verdict"] == "NEEDS_CHANGES"
    assert len(result["issues"]) == 1


def test_extract_json_with_prose():
    content = 'Here is my review:\n{"verdict": "APPROVE", "issues": [], "summary": "good"}\nDone.'
    result = rv.extract_json_from_content(content)
    assert result is not None
    assert result["verdict"] == "APPROVE"


def test_extract_json_trailing_comma():
    """Gemini sometimes produces trailing commas."""
    content = '{"verdict": "APPROVE", "issues": [], "summary": "ok",}'
    result = rv.extract_json_from_content(content)
    assert result is not None


def test_extract_json_returns_none_on_garbage():
    result = rv.extract_json_from_content("no json here at all")
    assert result is None


# ── build_system_prompt ────────────────────────────────────────────────────────

def test_build_system_prompt_includes_json_schema(tmp_path, monkeypatch):
    """build_system_prompt reads review.md and appends JSON format section."""
    fake_prompt = tmp_path / "review.md"
    fake_prompt.write_text("# Review Criteria\nCheck fidelity.\n")
    monkeypatch.setattr(rv, "REVIEW_PROMPT", fake_prompt)

    prompt = rv.build_system_prompt()
    assert "# Review Criteria" in prompt
    assert "verdict" in prompt
    assert "NEEDS_CHANGES" in prompt


def test_build_system_prompt_file_missing(tmp_path, monkeypatch):
    """Missing review.md raises FileNotFoundError."""
    monkeypatch.setattr(rv, "REVIEW_PROMPT", tmp_path / "nonexistent.md")
    with pytest.raises(FileNotFoundError):
        rv.build_system_prompt()


# ── build_user_message ─────────────────────────────────────────────────────────

def test_build_user_message_round_number():
    msg = rv.build_user_message("package p", "func algo(){}", "thresh: 0.5",
                                 "# Context", "kind: Scorer", 3)
    assert "Round 3" in msg


def test_build_user_message_includes_all_sections():
    msg = rv.build_user_message("package p", "func algo(){}", "thresh: 0.5",
                                 "# Context", "kind: Scorer", 1)
    assert "Generated Plugin Code" in msg
    assert "Algorithm Source" in msg
    assert "Algorithm Config" in msg
    assert "Translation Context" in msg
    assert "Treatment Config" in msg


# ── collect_issues ─────────────────────────────────────────────────────────────

def test_collect_issues_from_needs_changes():
    reviews = [
        {"verdict": "NEEDS_CHANGES", "issues": [
            {"category": "fidelity", "description": "issue A", "file": "f.go", "fix": "fix A"}
        ]},
        {"verdict": "APPROVE", "issues": []},
    ]
    issues = rv.collect_issues(reviews)
    assert len(issues) == 1
    assert issues[0]["description"] == "issue A"


def test_collect_issues_string_coercion():
    """Models that return string issues are coerced to dicts."""
    reviews = [{"verdict": "NEEDS_CHANGES", "issues": ["simple string issue"]}]
    issues = rv.collect_issues(reviews)
    assert len(issues) == 1
    assert issues[0]["description"] == "simple string issue"


def test_collect_issues_ignores_errors():
    reviews = [{"verdict": "ERROR", "issues": []}]
    assert rv.collect_issues(reviews) == []
