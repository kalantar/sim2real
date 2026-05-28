"""Tests that deploy.py operates without a manifest (transfer.yaml)."""

import json
from unittest.mock import patch

from pipeline.lib.progress import ConfigMapProgressStore


def test_build_parser_no_manifest_flag():
    """build_parser() does NOT expose a --manifest argument."""
    from pipeline.deploy import build_parser

    parser = build_parser()
    # Collect all option strings from all actions
    all_options = set()
    for action in parser._actions:
        all_options.update(action.option_strings)

    assert "--manifest" not in all_options


def test_main_works_without_transfer_yaml(tmp_path, monkeypatch):
    """main() dispatches 'status' without needing transfer.yaml anywhere."""
    import pipeline.deploy as deploy

    # Set up minimal workspace structure
    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    # setup_config.json with current_run
    ws = tmp_path / "workspace"
    (ws / "setup_config.json").write_text(json.dumps({"current_run": "test-run", "namespace": "ns-0"}))

    # Mock ConfigMapProgressStore to return progress data
    progress = {"wl-a-baseline": {"workload": "wl-a", "package": "baseline", "status": "pending", "namespace": None, "retries": 0}}
    monkeypatch.setattr(ConfigMapProgressStore, "load", lambda self: progress)
    monkeypatch.setattr(ConfigMapProgressStore, "save", lambda self, d: None)

    # Patch sys.argv to simulate CLI invocation
    monkeypatch.setattr("sys.argv", ["deploy.py", "--experiment-root", str(tmp_path), "status"])

    # Patch EXPERIMENT_ROOT and _load_setup_config to use our tmp_path
    monkeypatch.setattr(deploy, "EXPERIMENT_ROOT", tmp_path)

    status_called = []

    def mock_status(args, run_dir, setup_config=None):
        status_called.append(run_dir)

    with patch.object(deploy, "_cmd_status", mock_status):
        with patch.object(deploy, "_load_setup_config", return_value={"current_run": "test-run", "namespace": "ns-0"}):
            deploy.main()

    assert len(status_called) == 1
    assert status_called[0] == run_dir
