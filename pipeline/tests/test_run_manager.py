"""Tests for pipeline/lib/run_manager.py."""
import json
import pytest
from pathlib import Path

# ── Helpers ──────────────────────────────────────────────────────────────────

def _write_state(run_dir, name, scenario, phases):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ".state.json").write_text(json.dumps({
        "run_name": name, "scenario": scenario, "phases": phases
    }))

def _write_meta(run_dir, stages, version=1):
    (run_dir / "run_metadata.json").write_text(json.dumps({
        "version": version, "stages": stages
    }))

def _write_setup(workspace, current_run):
    (workspace / "setup_config.json").write_text(json.dumps({"current_run": current_run}))

def _write_translation_output(run_dir, files_created, files_modified):
    (run_dir / "translation_output.json").write_text(json.dumps({
        "files_created": files_created,
        "files_modified": files_modified,
    }))

# ── Conformance helpers ───────────────────────────────────────────────────────

class TestLoadState:
    def test_valid(self, tmp_path):
        from pipeline.lib.run_manager import _load_state
        run_dir = tmp_path / "runs" / "r1"
        _write_state(run_dir, "r1", "routing", {"init": {"status": "done"}})
        data = _load_state(run_dir)
        assert data is not None
        assert data["run_name"] == "r1"

    def test_missing_file_returns_none(self, tmp_path):
        from pipeline.lib.run_manager import _load_state
        run_dir = tmp_path / "runs" / "r1"
        run_dir.mkdir(parents=True)
        assert _load_state(run_dir) is None

    def test_missing_required_key_returns_none(self, tmp_path):
        from pipeline.lib.run_manager import _load_state
        run_dir = tmp_path / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (run_dir / ".state.json").write_text(json.dumps({"run_name": "r1"}))  # missing scenario, phases
        assert _load_state(run_dir) is None

    def test_invalid_json_returns_none(self, tmp_path):
        from pipeline.lib.run_manager import _load_state
        run_dir = tmp_path / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (run_dir / ".state.json").write_text("not json")
        assert _load_state(run_dir) is None


class TestLoadMetadata:
    def test_valid(self, tmp_path):
        from pipeline.lib.run_manager import _load_metadata
        run_dir = tmp_path / "runs" / "r1"
        run_dir.mkdir(parents=True)
        _write_meta(run_dir, {"setup": {"status": "completed"}})
        data = _load_metadata(run_dir)
        assert data is not None
        assert "stages" in data

    def test_missing_file_returns_none(self, tmp_path):
        from pipeline.lib.run_manager import _load_metadata
        run_dir = tmp_path / "runs" / "r1"
        run_dir.mkdir(parents=True)
        assert _load_metadata(run_dir) is None

    def test_missing_required_key_returns_none(self, tmp_path):
        from pipeline.lib.run_manager import _load_metadata
        run_dir = tmp_path / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (run_dir / "run_metadata.json").write_text(json.dumps({"version": 1}))  # missing stages
        assert _load_metadata(run_dir) is None


class TestPhaseHelpers:
    def test_last_done_phase_returns_last_done(self, tmp_path):
        from pipeline.lib.run_manager import _last_done_phase
        phases = {
            "init": {"status": "done"},
            "context": {"status": "done"},
            "gate": {"status": "done"},
        }
        assert _last_done_phase(phases) == "gate"

    def test_last_done_phase_empty_returns_empty(self, tmp_path):
        from pipeline.lib.run_manager import _last_done_phase
        assert _last_done_phase({}) == ""

    def test_last_done_phase_skips_non_done(self, tmp_path):
        from pipeline.lib.run_manager import _last_done_phase
        phases = {
            "init": {"status": "done"},
            "translate": {"status": "in_progress"},
        }
        assert _last_done_phase(phases) == "init"

    def test_get_verdict_from_gate(self, tmp_path):
        from pipeline.lib.run_manager import _get_verdict
        phases = {
            "gate": {"status": "done", "verdict": "READY TO DEPLOY"},
        }
        assert _get_verdict(phases) == "READY TO DEPLOY"

    def test_get_verdict_missing_returns_empty(self, tmp_path):
        from pipeline.lib.run_manager import _get_verdict
        assert _get_verdict({}) == ""

    def test_get_verdict_no_verdict_key(self, tmp_path):
        from pipeline.lib.run_manager import _get_verdict
        phases = {"gate": {"status": "done"}}
        assert _get_verdict(phases) == ""
