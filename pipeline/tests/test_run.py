"""Tests for pipeline/run.py CLI argument parsing and experiment-root path routing."""
import json
import sys
from unittest.mock import patch, MagicMock


class TestBuildParser:
    def test_accepts_experiment_root_before_subcommand(self):
        import pipeline.run as mod
        parser = mod.build_parser()
        args = parser.parse_args(["--experiment-root", "/tmp/exp", "switch", "run1"])
        assert args.experiment_root == "/tmp/exp"
        assert args.command == "switch"
        assert args.name == "run1"

    def test_experiment_root_defaults_to_none_when_absent(self):
        import pipeline.run as mod
        parser = mod.build_parser()
        args = parser.parse_args(["switch", "run1"])
        assert getattr(args, "experiment_root", None) is None


class TestExperimentRootPaths:
    """main() must derive workspace/submodule paths from --experiment-root."""

    def test_switch_derives_paths_from_experiment_root(self, tmp_path):
        import pipeline.run as mod

        exp = tmp_path / "my-experiment"
        exp.mkdir()

        captured = {}

        def fake_switch(run_name, workspace_dir, submodule_dir, setup_config, confirm_fn):
            captured["workspace_dir"] = workspace_dir
            captured["submodule_dir"] = submodule_dir
            captured["setup_config"] = setup_config
            return MagicMock(active_run=run_name, files_written=[])

        with patch.object(mod, "switch_run", side_effect=fake_switch), \
             patch.object(sys, "argv",
                          ["run.py", "--experiment-root", str(exp), "switch", "run1"]):
            mod.main()

        assert captured["workspace_dir"] == exp / "workspace"
        assert captured["submodule_dir"] == exp / "llm-d-inference-scheduler"
        assert captured["setup_config"] == exp / "workspace" / "setup_config.json"

    def test_list_derives_workspace_from_experiment_root(self, tmp_path):
        import pipeline.run as mod

        exp = tmp_path / "my-experiment"
        (exp / "workspace" / "runs").mkdir(parents=True)
        (exp / "workspace" / "setup_config.json").write_text(
            json.dumps({"current_run": ""})
        )

        captured = {}

        def fake_list(workspace_dir, setup_config):
            captured["workspace_dir"] = workspace_dir
            return []

        with patch.object(mod, "list_runs", side_effect=fake_list), \
             patch.object(sys, "argv",
                          ["run.py", "--experiment-root", str(exp), "list"]):
            mod.main()

        assert captured["workspace_dir"] == exp / "workspace"

    def test_inspect_derives_workspace_from_experiment_root(self, tmp_path):
        import pipeline.run as mod

        exp = tmp_path / "my-experiment"
        run_dir = exp / "workspace" / "runs" / "run1"
        run_dir.mkdir(parents=True)
        (run_dir / ".state.json").write_text(
            json.dumps({"run_name": "run1", "scenario": "test", "phases": {}})
        )
        (run_dir / "run_metadata.json").write_text(
            json.dumps({"version": 1, "stages": {}})
        )
        (exp / "workspace" / "setup_config.json").write_text(
            json.dumps({"current_run": ""})
        )

        captured = {}

        def fake_inspect(run_dir, active_run=""):
            captured["run_dir"] = run_dir
            return MagicMock(
                name="run1", scenario="test", active=False,
                phases=[], files_created=[], files_modified=[],
                deploy_stages={}, deploy_last_step="",
            )

        with patch.object(mod, "inspect_run", side_effect=fake_inspect), \
             patch.object(sys, "argv",
                          ["run.py", "--experiment-root", str(exp), "inspect", "run1"]):
            mod.main()

        assert captured["run_dir"] == exp / "workspace" / "runs" / "run1"

    def test_without_experiment_root_defaults_to_cwd(self, tmp_path, monkeypatch):
        import pipeline.run as mod

        monkeypatch.chdir(tmp_path)

        captured = {}

        def fake_list(workspace_dir, setup_config):
            captured["workspace_dir"] = workspace_dir
            return []

        with patch.object(mod, "list_runs", side_effect=fake_list), \
             patch.object(sys, "argv", ["run.py", "list"]):
            mod.main()

        assert captured["workspace_dir"] == tmp_path / "workspace"
