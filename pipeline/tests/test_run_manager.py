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


class TestListRuns:
    def _setup(self, tmp_path, runs, active_run=""):
        """Create workspace with given runs. Each run: (name, scenario, phases, stages)."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        for name, scenario, phases, stages in runs:
            run_dir = ws / "runs" / name
            _write_state(run_dir, name, scenario, phases)
            _write_meta(run_dir, stages)
        _write_setup(ws, active_run)
        return ws, ws / "setup_config.json"

    def test_returns_conforming_runs_only(self, tmp_path):
        from pipeline.lib.run_manager import list_runs
        ws, cfg = self._setup(tmp_path, [
            ("run1", "routing", {"gate": {"status": "done", "verdict": "READY TO DEPLOY"}},
             {"setup": {"status": "completed"}}),
        ], active_run="run1")
        # add a non-conforming run dir (missing run_name key)
        bad_dir = ws / "runs" / "oldrun"
        bad_dir.mkdir(parents=True)
        (bad_dir / ".state.json").write_text(json.dumps({"scenario": "x", "phases": {}}))
        (bad_dir / "run_metadata.json").write_text(json.dumps({"version": 1, "stages": {}}))

        results = list_runs(ws, cfg)
        assert len(results) == 1
        assert results[0].name == "run1"

    def test_active_flag_set_correctly(self, tmp_path):
        from pipeline.lib.run_manager import list_runs
        ws, cfg = self._setup(tmp_path, [
            ("run1", "routing", {"gate": {"status": "done", "verdict": "PASS"}},
             {"setup": {"status": "completed"}}),
            ("run2", "routing", {"gate": {"status": "done"}},
             {"setup": {"status": "completed"}}),
        ], active_run="run2")

        results = list_runs(ws, cfg)
        by_name = {r.name: r for r in results}
        assert not by_name["run1"].active
        assert by_name["run2"].active

    def test_verdict_extracted_from_gate_phase(self, tmp_path):
        from pipeline.lib.run_manager import list_runs
        ws, cfg = self._setup(tmp_path, [
            ("run1", "routing", {
                "init": {"status": "done"},
                "gate": {"status": "done", "verdict": "READY TO DEPLOY"},
             }, {"setup": {"status": "completed"}}),
        ], active_run="")
        results = list_runs(ws, cfg)
        assert results[0].verdict == "READY TO DEPLOY"
        assert results[0].last_phase == "gate"

    def test_empty_workspace_returns_empty_list(self, tmp_path):
        from pipeline.lib.run_manager import list_runs
        ws = tmp_path / "workspace"
        (ws / "runs").mkdir(parents=True)
        _write_setup(ws, "")
        assert list_runs(ws, ws / "setup_config.json") == []

    def test_missing_runs_dir_returns_empty_list(self, tmp_path):
        from pipeline.lib.run_manager import list_runs
        ws = tmp_path / "workspace"
        ws.mkdir()
        _write_setup(ws, "")
        assert list_runs(ws, ws / "setup_config.json") == []

    def test_skips_non_directory_entries(self, tmp_path):
        from pipeline.lib.run_manager import list_runs
        ws, cfg = self._setup(tmp_path, [
            ("run1", "routing", {"gate": {"status": "done"}}, {"setup": {"status": "completed"}}),
        ])
        (ws / "runs" / "stray_file.txt").write_text("not a run")
        results = list_runs(ws, cfg)
        assert len(results) == 1

    def test_results_sorted_by_name(self, tmp_path):
        from pipeline.lib.run_manager import list_runs
        ws, cfg = self._setup(tmp_path, [
            ("zebra", "routing", {"gate": {"status": "done"}}, {"setup": {"status": "completed"}}),
            ("apple", "routing", {"gate": {"status": "done"}}, {"setup": {"status": "completed"}}),
            ("mango", "routing", {"gate": {"status": "done"}}, {"setup": {"status": "completed"}}),
        ])
        results = list_runs(ws, cfg)
        assert [r.name for r in results] == ["apple", "mango", "zebra"]
