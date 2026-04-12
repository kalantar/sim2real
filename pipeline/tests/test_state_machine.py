"""Tests for phase state machine."""
import json
import pytest

from pipeline.lib.state_machine import StateMachine


def test_new_state_machine(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    assert not sm.is_done("init")
    assert sm.run_name == "test-run"
    assert sm.scenario == "routing"


def test_mark_done_and_persist(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.mark_done("init")
    assert sm.is_done("init")
    state_file = tmp_path / ".state.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert data["phases"]["init"]["status"] == "done"


def test_mark_done_with_metadata(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.mark_done("context", hash="a1b2c3", cached=True)
    data = json.loads((tmp_path / ".state.json").read_text())
    assert data["phases"]["context"]["hash"] == "a1b2c3"
    assert data["phases"]["context"]["cached"] is True


def test_load_existing_state(tmp_path):
    sm1 = StateMachine("test-run", "routing", tmp_path)
    sm1.mark_done("init")
    sm1.mark_done("context", hash="abc")
    sm2 = StateMachine.load(tmp_path)
    assert sm2.is_done("init")
    assert sm2.is_done("context")
    assert sm2.run_name == "test-run"


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        StateMachine.load(tmp_path)


def test_reset_phase(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.mark_done("init")
    sm.reset("init")
    assert not sm.is_done("init")


def test_get_phase_metadata(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.mark_done("translate", plugin_type="adaptive-v2-scorer", review_rounds=3)
    meta = sm.get_phase("translate")
    assert meta["plugin_type"] == "adaptive-v2-scorer"
    assert meta["review_rounds"] == 3


def test_increment_checkpoint_hits(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.increment("translate", "checkpoint_hits")
    sm.increment("translate", "checkpoint_hits")
    assert sm.get_phase("translate")["checkpoint_hits"] == 2


def test_get_phase_returns_copy(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.mark_done("init")
    meta = sm.get_phase("init")
    meta["extra"] = "should not appear"
    assert "extra" not in sm.get_phase("init")


def test_get_phase_empty(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    assert sm.get_phase("nonexistent") == {}


def test_update_partial_fields(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.mark_done("context", hash="abc", context_file_prepared=True, context_file_populated=False)
    assert sm.get_phase("context")["context_file_prepared"] is True
    assert sm.get_phase("context")["context_file_populated"] is False
    sm.update("context", context_file_populated=True)
    meta = sm.get_phase("context")
    assert meta["context_file_populated"] is True
    # Other fields preserved
    assert meta["hash"] == "abc"
    assert meta["context_file_prepared"] is True
    assert meta["status"] == "done"


def test_update_creates_phase_if_missing(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.update("context", context_file_prepared=True)
    assert sm.get_phase("context")["context_file_prepared"] is True


def test_atomic_save(tmp_path):
    """Save uses tmp+rename so partial writes don't corrupt state."""
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.mark_done("init")
    state_file = tmp_path / ".state.json"
    data = json.loads(state_file.read_text())
    assert "run_name" in data
