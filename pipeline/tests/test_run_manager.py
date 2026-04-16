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


class TestInspectRun:
    def _make_run(self, tmp_path, name="adaptive6", scenario="adaptive-routing",
                  phases=None, stages=None, files_created=None, files_modified=None):
        run_dir = tmp_path / "runs" / name
        if phases is None:
            phases = {
                "init": {"status": "done"},
                "translate": {"status": "done", "review_rounds": 2, "consensus": "1/1"},
                "baseline_derivation": {"status": "done", "user_approved": True},
                "assembly": {"status": "done", "packages": ["baseline", "treatment"]},
                "gate": {"status": "done", "verdict": "READY TO DEPLOY"},
            }
        if stages is None:
            stages = {
                "setup": {"status": "completed"},
                "deploy": {"status": "pending", "last_completed_step": "build_epp"},
            }
        _write_state(run_dir, name, scenario, phases)
        _write_meta(run_dir, stages)
        if files_created is not None or files_modified is not None:
            _write_translation_output(
                run_dir,
                files_created or [],
                files_modified or ["pkg/plugins/scorer/adaptive_v2.go"],
            )
        return run_dir

    def test_raises_for_nonexistent_run(self, tmp_path):
        from pipeline.lib.run_manager import inspect_run, RunNotFoundError
        with pytest.raises(RunNotFoundError, match="not found"):
            inspect_run(tmp_path / "runs" / "nope")

    def test_raises_for_invalid_state(self, tmp_path):
        from pipeline.lib.run_manager import inspect_run, RunNotFoundError
        run_dir = tmp_path / "runs" / "bad"
        run_dir.mkdir(parents=True)
        (run_dir / ".state.json").write_text("not json")
        with pytest.raises(RunNotFoundError):
            inspect_run(run_dir)

    def test_returns_run_detail(self, tmp_path):
        from pipeline.lib.run_manager import inspect_run
        run_dir = self._make_run(tmp_path, files_modified=["pkg/plugins/scorer/adaptive_v2.go"])
        detail = inspect_run(run_dir)
        assert detail.name == "adaptive6"
        assert detail.scenario == "adaptive-routing"
        assert not detail.active  # active_run not passed

    def test_active_flag_via_param(self, tmp_path):
        from pipeline.lib.run_manager import inspect_run
        run_dir = self._make_run(tmp_path)
        detail = inspect_run(run_dir, active_run="adaptive6")
        assert detail.active

    def test_phases_populated(self, tmp_path):
        from pipeline.lib.run_manager import inspect_run
        run_dir = self._make_run(tmp_path)
        detail = inspect_run(run_dir)
        phase_names = [p.name for p in detail.phases]
        assert "translate" in phase_names
        assert "gate" in phase_names

    def test_translate_notes(self, tmp_path):
        from pipeline.lib.run_manager import inspect_run
        run_dir = self._make_run(tmp_path)
        detail = inspect_run(run_dir)
        translate = next(p for p in detail.phases if p.name == "translate")
        assert "2 review rounds" in translate.notes
        assert "1/1" in translate.notes

    def test_gate_verdict(self, tmp_path):
        from pipeline.lib.run_manager import inspect_run
        run_dir = self._make_run(tmp_path)
        detail = inspect_run(run_dir)
        gate = next(p for p in detail.phases if p.name == "gate")
        assert gate.verdict == "READY TO DEPLOY"

    def test_assembly_notes(self, tmp_path):
        from pipeline.lib.run_manager import inspect_run
        run_dir = self._make_run(tmp_path)
        detail = inspect_run(run_dir)
        assembly = next(p for p in detail.phases if p.name == "assembly")
        assert "baseline" in assembly.notes
        assert "treatment" in assembly.notes

    def test_generated_files_from_translation_output(self, tmp_path):
        from pipeline.lib.run_manager import inspect_run
        run_dir = self._make_run(tmp_path,
                                  files_created=["pkg/plugins/scorer/adaptive_v2_test.go"],
                                  files_modified=["pkg/plugins/scorer/adaptive_v2.go"])
        detail = inspect_run(run_dir)
        assert "pkg/plugins/scorer/adaptive_v2.go" in detail.files_modified
        assert "pkg/plugins/scorer/adaptive_v2_test.go" in detail.files_created

    def test_deploy_stages_populated(self, tmp_path):
        from pipeline.lib.run_manager import inspect_run
        run_dir = self._make_run(tmp_path)
        detail = inspect_run(run_dir)
        assert "setup" in detail.deploy_stages
        assert detail.deploy_stages["setup"] == "completed"
        assert detail.deploy_last_step == "build_epp"

    def test_missing_translation_output_ok(self, tmp_path):
        """inspect_run should not fail if translation_output.json is absent."""
        from pipeline.lib.run_manager import inspect_run
        run_dir = self._make_run(tmp_path)  # no files_created/modified written
        detail = inspect_run(run_dir)
        assert detail.files_created == []
        assert detail.files_modified == []

    def test_baseline_derivation_notes(self, tmp_path):
        from pipeline.lib.run_manager import inspect_run
        run_dir = self._make_run(tmp_path, phases={
            "baseline_derivation": {"status": "done", "user_approved": True},
        })
        detail = inspect_run(run_dir)
        bd = next(p for p in detail.phases if p.name == "baseline_derivation")
        assert "user approved" in bd.notes

    def test_empty_phases_returns_empty_list(self, tmp_path):
        from pipeline.lib.run_manager import inspect_run
        run_dir = self._make_run(tmp_path, phases={})
        detail = inspect_run(run_dir)
        assert detail.phases == []

    def test_missing_metadata_deploy_stages_empty(self, tmp_path):
        """inspect_run should not fail if run_metadata.json is absent."""
        from pipeline.lib.run_manager import inspect_run
        run_dir = self._make_run(tmp_path, stages={})
        # Remove metadata file entirely
        (run_dir / "run_metadata.json").unlink()
        detail = inspect_run(run_dir)
        assert detail.deploy_stages == {}


class TestSwitchRun:
    # Shared no-op injectables (submodule is a plain dir, no real git)
    _no_dirty = staticmethod(lambda d: False)
    _no_reset = staticmethod(lambda d: None)

    def _setup(self, tmp_path, run_name="adaptive6",
               files_created=None, files_modified=None,
               generated_files=None, active_run="other"):
        """
        Set up a minimal workspace + submodule for switch_run tests.

        generated_files: list of basenames to create in workspace/runs/<run>/generated/.
                         Defaults to basenames of files_created + files_modified.
        """
        ws = tmp_path / "workspace"
        run_dir = ws / "runs" / run_name
        run_dir.mkdir(parents=True)

        fc = files_created or []
        fm = files_modified or ["pkg/plugins/scorer/adaptive_v2.go"]
        _write_state(run_dir, run_name, "routing",
                     {"gate": {"status": "done", "verdict": "READY TO DEPLOY"}})
        _write_meta(run_dir, {"setup": {"status": "completed"}})
        _write_translation_output(run_dir, fc, fm)

        # Create source files in generated/
        gen_dir = run_dir / "generated"
        gen_dir.mkdir()
        all_targets = fc + fm
        for rel_path in (generated_files if generated_files is not None
                         else [Path(p).name for p in all_targets]):
            (gen_dir / rel_path).write_text(f"// content of {rel_path}")

        # Submodule dir (not a real git repo — dirty/reset are injected)
        sub_dir = tmp_path / "llm-d-inference-scheduler"
        sub_dir.mkdir()
        for rel_path in all_targets:
            dst = sub_dir / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text("// old content")

        _write_setup(ws, active_run)
        cfg = ws / "setup_config.json"
        return ws, sub_dir, cfg, run_dir

    # ── Happy path ────────────────────────────────────────────────────────────

    def test_copies_files_and_updates_setup_config(self, tmp_path):
        from pipeline.lib.run_manager import switch_run, SwitchResult
        ws, sub_dir, cfg, run_dir = self._setup(tmp_path,
            files_modified=["pkg/plugins/scorer/adaptive_v2.go"])

        result = switch_run("adaptive6", ws, sub_dir, cfg,
                            confirm_fn=lambda _: True,
                            _is_dirty=self._no_dirty, _reset=self._no_reset)

        assert isinstance(result, SwitchResult)
        assert "pkg/plugins/scorer/adaptive_v2.go" in result.files_written
        assert result.active_run == "adaptive6"

        dst = sub_dir / "pkg/plugins/scorer/adaptive_v2.go"
        assert dst.read_text() == "// content of adaptive_v2.go"

        cfg_data = json.loads(cfg.read_text())
        assert cfg_data["current_run"] == "adaptive6"

    def test_reset_called_before_copy(self, tmp_path):
        """_reset must be called once, before any file copies."""
        from pipeline.lib.run_manager import switch_run
        ws, sub_dir, cfg, _ = self._setup(tmp_path,
            files_modified=["pkg/plugins/scorer/adaptive_v2.go"])

        call_order = []
        import shutil as _shutil
        real_copy2 = _shutil.copy2
        from unittest.mock import patch

        def track_reset(d):
            call_order.append("reset")

        def track_copy(src, dst):
            call_order.append("copy")
            return real_copy2(src, dst)

        with patch("pipeline.lib.run_manager.shutil.copy2", side_effect=track_copy):
            switch_run("adaptive6", ws, sub_dir, cfg,
                       confirm_fn=lambda _: True,
                       _is_dirty=self._no_dirty, _reset=track_reset)

        assert call_order[0] == "reset"
        assert "copy" in call_order

    def test_stale_files_from_previous_run_deleted(self, tmp_path):
        """Files from the previous run not present in the target run must be removed."""
        from pipeline.lib.run_manager import switch_run
        ws = tmp_path / "workspace"
        ws.mkdir()

        # Previous run wrote a file not present in the target run
        prev_dir = ws / "runs" / "prev-run"
        prev_dir.mkdir(parents=True)
        _write_state(prev_dir, "prev-run", "routing", {})
        _write_meta(prev_dir, {})
        _write_translation_output(prev_dir, ["pkg/plugins/admitter/stale.go"], [])

        # Target run does not include the stale file
        target_dir = ws / "runs" / "adaptive6"
        target_dir.mkdir(parents=True)
        _write_state(target_dir, "adaptive6", "routing", {})
        _write_meta(target_dir, {})
        _write_translation_output(target_dir, [], ["pkg/plugins/scorer/adaptive_v2.go"])
        gen_dir = target_dir / "generated"
        gen_dir.mkdir()
        (gen_dir / "adaptive_v2.go").write_text("// new content")

        sub_dir = tmp_path / "llm-d-inference-scheduler"
        (sub_dir / "pkg/plugins/admitter").mkdir(parents=True)
        (sub_dir / "pkg/plugins/admitter/stale.go").write_text("// stale")
        (sub_dir / "pkg/plugins/scorer").mkdir(parents=True)
        (sub_dir / "pkg/plugins/scorer/adaptive_v2.go").write_text("// old")

        _write_setup(ws, "prev-run")
        cfg = ws / "setup_config.json"

        switch_run("adaptive6", ws, sub_dir, cfg,
                   confirm_fn=lambda _: True,
                   _is_dirty=self._no_dirty, _reset=self._no_reset)

        assert not (sub_dir / "pkg/plugins/admitter/stale.go").exists()
        assert (sub_dir / "pkg/plugins/scorer/adaptive_v2.go").read_text() == "// new content"

    def test_stale_cleanup_skipped_if_no_previous_run(self, tmp_path):
        """No error when setup_config has no previous run or previous run dir is missing."""
        from pipeline.lib.run_manager import switch_run
        ws, sub_dir, cfg, _ = self._setup(tmp_path, active_run="")

        result = switch_run("adaptive6", ws, sub_dir, cfg,
                            confirm_fn=lambda _: True,
                            _is_dirty=self._no_dirty, _reset=self._no_reset)
        assert result.active_run == "adaptive6"

    def test_setup_config_only_updated_after_all_copies_succeed(self, tmp_path):
        """setup_config.json must NOT be updated if a copy fails."""
        from unittest.mock import patch
        from pipeline.lib.run_manager import switch_run
        ws, sub_dir, cfg, run_dir = self._setup(tmp_path,
            files_modified=["pkg/plugins/scorer/adaptive_v2.go"])

        with patch("pipeline.lib.run_manager.shutil.copy2", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                switch_run("adaptive6", ws, sub_dir, cfg,
                           confirm_fn=lambda _: True,
                           _is_dirty=self._no_dirty, _reset=self._no_reset)

        cfg_data = json.loads(cfg.read_text())
        assert cfg_data["current_run"] == "other"

    def test_partial_copy_failure_leaves_written_files_in_place(self, tmp_path):
        """Files written before a mid-flight failure must not be rolled back."""
        import shutil as _shutil
        from unittest.mock import patch
        from pipeline.lib.run_manager import switch_run
        ws, sub_dir, cfg, run_dir = self._setup(tmp_path,
            files_created=["pkg/plugins/scorer/adaptive_v2_test.go"],
            files_modified=["pkg/plugins/scorer/adaptive_v2.go"])

        call_count = [0]
        real_copy2 = _shutil.copy2
        def copy2_fail_on_second(src, dst):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise OSError("disk full")
            return real_copy2(src, dst)

        with patch("pipeline.lib.run_manager.shutil.copy2", side_effect=copy2_fail_on_second):
            with pytest.raises(OSError):
                switch_run("adaptive6", ws, sub_dir, cfg,
                           confirm_fn=lambda _: True,
                           _is_dirty=self._no_dirty, _reset=self._no_reset)

        # First file written must still be present (not rolled back)
        written = list((sub_dir / "pkg/plugins/scorer").glob("*.go"))
        assert len(written) >= 1

    # ── Validation: run not found ─────────────────────────────────────────────

    def test_raises_run_not_found(self, tmp_path):
        from pipeline.lib.run_manager import switch_run, RunNotFoundError
        ws = tmp_path / "workspace"
        ws.mkdir()
        _write_setup(ws, "")
        sub_dir = tmp_path / "llm-d-inference-scheduler"
        sub_dir.mkdir()
        cfg = ws / "setup_config.json"
        with pytest.raises(RunNotFoundError, match="not found"):
            switch_run("nope", ws, sub_dir, cfg,
                       confirm_fn=lambda _: True,
                       _is_dirty=self._no_dirty, _reset=self._no_reset)

    # ── Validation: translation_output.json ──────────────────────────────────

    def test_raises_if_translation_output_missing(self, tmp_path):
        from pipeline.lib.run_manager import switch_run, TranslationOutputError
        ws, sub_dir, cfg, run_dir = self._setup(tmp_path)
        (run_dir / "translation_output.json").unlink()
        with pytest.raises(TranslationOutputError, match="Phase 3"):
            switch_run("adaptive6", ws, sub_dir, cfg,
                       confirm_fn=lambda _: True,
                       _is_dirty=self._no_dirty, _reset=self._no_reset)

    def test_raises_if_translation_output_malformed(self, tmp_path):
        from pipeline.lib.run_manager import switch_run, TranslationOutputError
        ws, sub_dir, cfg, run_dir = self._setup(tmp_path)
        (run_dir / "translation_output.json").write_text(json.dumps({"files_created": "not a list"}))
        with pytest.raises(TranslationOutputError, match="malformed"):
            switch_run("adaptive6", ws, sub_dir, cfg,
                       confirm_fn=lambda _: True,
                       _is_dirty=self._no_dirty, _reset=self._no_reset)

    # ── Validation: basename collision ────────────────────────────────────────

    def test_raises_on_basename_collision(self, tmp_path):
        from pipeline.lib.run_manager import switch_run
        ws, sub_dir, cfg, run_dir = self._setup(
            tmp_path,
            files_created=["pkg/a/foo.go"],
            files_modified=["pkg/b/foo.go"],  # same basename 'foo.go'
            generated_files=["foo.go"],
        )
        with pytest.raises(ValueError, match="basename collision"):
            switch_run("adaptive6", ws, sub_dir, cfg,
                       confirm_fn=lambda _: True,
                       _is_dirty=self._no_dirty, _reset=self._no_reset)

    # ── Validation: missing source file ──────────────────────────────────────

    def test_raises_if_source_file_missing(self, tmp_path):
        from pipeline.lib.run_manager import switch_run
        ws, sub_dir, cfg, run_dir = self._setup(
            tmp_path,
            files_modified=["pkg/plugins/scorer/adaptive_v2.go"],
            generated_files=[],  # deliberately empty
        )
        with pytest.raises(ValueError, match="missing source files"):
            switch_run("adaptive6", ws, sub_dir, cfg,
                       confirm_fn=lambda _: True,
                       _is_dirty=self._no_dirty, _reset=self._no_reset)

    # ── Validation: submodule not found ──────────────────────────────────────

    def test_raises_if_submodule_missing(self, tmp_path):
        from pipeline.lib.run_manager import switch_run, RunNotFoundError
        ws, sub_dir, cfg, run_dir = self._setup(tmp_path)
        import shutil
        shutil.rmtree(sub_dir)
        with pytest.raises(RunNotFoundError, match="submodule"):
            switch_run("adaptive6", ws, sub_dir, cfg,
                       confirm_fn=lambda _: True,
                       _is_dirty=self._no_dirty, _reset=self._no_reset)

    # ── Dirty handling ────────────────────────────────────────────────────────

    def test_dirty_confirmed_proceeds(self, tmp_path):
        from pipeline.lib.run_manager import switch_run
        ws, sub_dir, cfg, _ = self._setup(tmp_path)
        confirm_called = [False]
        def confirm(_):
            confirm_called[0] = True
            return True

        result = switch_run("adaptive6", ws, sub_dir, cfg,
                            confirm_fn=confirm,
                            _is_dirty=lambda d: True, _reset=self._no_reset)
        assert confirm_called[0]
        assert result is not None

    def test_dirty_declined_raises_switch_aborted(self, tmp_path):
        from pipeline.lib.run_manager import switch_run, SwitchAborted
        ws, sub_dir, cfg, _ = self._setup(tmp_path)
        with pytest.raises(SwitchAborted):
            switch_run("adaptive6", ws, sub_dir, cfg,
                       confirm_fn=lambda _: False,
                       _is_dirty=lambda d: True, _reset=self._no_reset)

    def test_dirty_declined_does_not_modify_setup_config(self, tmp_path):
        from pipeline.lib.run_manager import switch_run, SwitchAborted
        ws, sub_dir, cfg, _ = self._setup(tmp_path, active_run="other")
        with pytest.raises(SwitchAborted):
            switch_run("adaptive6", ws, sub_dir, cfg,
                       confirm_fn=lambda _: False,
                       _is_dirty=lambda d: True, _reset=self._no_reset)
        assert json.loads(cfg.read_text())["current_run"] == "other"

    def test_clean_submodule_skips_confirm(self, tmp_path):
        """confirm_fn must NOT be called when submodule is clean."""
        from pipeline.lib.run_manager import switch_run
        ws, sub_dir, cfg, _ = self._setup(tmp_path)
        confirm_called = [False]
        def confirm(_):
            confirm_called[0] = True
            return True

        switch_run("adaptive6", ws, sub_dir, cfg,
                   confirm_fn=confirm,
                   _is_dirty=lambda d: False, _reset=self._no_reset)
        assert not confirm_called[0]
