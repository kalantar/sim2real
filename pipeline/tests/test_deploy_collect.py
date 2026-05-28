"""Tests for deploy.py _cmd_collect phase selection logic."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from pipeline.lib.progress import ConfigMapProgressStore


def _mock_cm(monkeypatch, data):
    """Monkeypatch ConfigMapProgressStore to return *data* on load and no-op on save."""
    monkeypatch.setattr(ConfigMapProgressStore, "load",
                        lambda self: json.loads(json.dumps(data)))
    monkeypatch.setattr(ConfigMapProgressStore, "save", lambda self, d: None)


def test_collect_with_progress_default(tmp_path, monkeypatch):
    """Without --package, collect uses all unique packages from progress."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "wl-smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "wl-smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline": {"workload": "wl-load", "package": "baseline", "status": "pending"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    collected_phases = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        collected_phases.extend(phases)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert collected_phases == ["baseline", "treatment"]


def test_collect_fallback_no_progress(tmp_path, monkeypatch):
    """Without --package and empty progress, discovers phases from cluster/ with warning."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    _mock_cm(monkeypatch, {})

    class Args:
        package = None
        skip_logs = False

    collected_phases = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        collected_phases.extend(phases)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract), \
         patch.object(deploy, "warn") as mock_warn:
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert collected_phases == ["baseline", "treatment"]
    mock_warn.assert_called()


def test_collect_single_package_from_progress(tmp_path, monkeypatch):
    """With --package treatment and progress containing it, collects only treatment."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "wl-smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "wl-smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = ["treatment"]
        skip_logs = False

    collected_phases = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        collected_phases.extend(phases)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert collected_phases == ["treatment"]


def test_collect_experiment_expands_to_all_progress_phases(tmp_path, monkeypatch):
    """With --package experiment, expands to all known phases from progress."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "wl-smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "wl-smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-canary": {"workload": "wl-smoke", "package": "canary", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = ["experiment"]
        skip_logs = False

    collected_phases = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        collected_phases.extend(phases)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    # Should expand to all phases from progress (sorted)
    assert collected_phases == ["baseline", "canary", "treatment"]


def test_collect_unknown_package_exits(tmp_path, monkeypatch):
    """With --package foo (not in progress), collect exits with error."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "wl-smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "wl-smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = ["foo"]
        skip_logs = False

    with pytest.raises(SystemExit):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})


def test_collect_custom_package_in_progress(tmp_path, monkeypatch):
    """With --package canary where canary IS in progress, succeeds."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "wl-smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-canary": {"workload": "wl-smoke", "package": "canary", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = ["canary"]
        skip_logs = False

    collected_phases = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        collected_phases.extend(phases)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert collected_phases == ["canary"]


def test_collect_corrupt_configmap_raises(tmp_path, monkeypatch):
    """ValueError from ConfigMap with invalid JSON propagates."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    def _raise_on_load(self):
        raise ValueError("Corrupt ConfigMap sim2real-progress in ns-0")

    monkeypatch.setattr(ConfigMapProgressStore, "load", _raise_on_load)
    monkeypatch.setattr(ConfigMapProgressStore, "save", lambda self, d: None)

    class Args:
        package = None
        skip_logs = False

    collected_phases = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        collected_phases.extend(phases)
        return {p: None for p in phases}

    # The ValueError is caught by _cmd_collect and treated as no progress
    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract), \
         patch.object(deploy, "warn") as mock_warn:
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert collected_phases == ["baseline", "treatment"]
    assert any("Corrupt" in str(c) or "Failed" in str(c) for c in mock_warn.call_args_list)


def test_collect_only_done_phases(tmp_path, monkeypatch):
    """Only phases with status done are included."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-a-baseline": {"workload": "wl-a", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-a-treatment": {"workload": "wl-a", "package": "treatment", "status": "pending"},
        "wl-a-canary": {"workload": "wl-a", "package": "canary", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    collected_phases = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        collected_phases.extend(phases)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    # treatment is pending — excluded; baseline and canary are done — included
    assert sorted(collected_phases) == ["baseline", "canary"]


def test_collect_missing_package_key_skipped(tmp_path, monkeypatch):
    """Progress entries without a 'package' key are gracefully skipped."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-a-baseline": {"workload": "wl-a", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-b-broken": {"workload": "wl-b", "status": "done"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    collected_phases = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        collected_phases.extend(phases)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert collected_phases == ["baseline"]


def test_collect_with_multi_baseline_progress(tmp_path, monkeypatch):
    """Collect discovers arbitrary phase names from progress."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-b1": {"workload": "wl-smoke", "package": "b1", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-b2": {"workload": "wl-smoke", "package": "b2", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-ac1": {"workload": "wl-smoke", "package": "ac1", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    collected_phases = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        collected_phases.extend(phases)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert collected_phases == ["ac1", "b1", "b2"]


def test_collect_fallback_discovers_from_pipelinerun_files(tmp_path, monkeypatch):
    """Without progress data, falls back to discovering phases from pipelinerun YAMLs."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    cluster_dir = run_dir / "cluster"
    cluster_dir.mkdir(parents=True)
    (cluster_dir / "pipelinerun-wl-smoke-b1.yaml").write_text("apiVersion: tekton.dev/v1")
    (cluster_dir / "pipelinerun-wl-smoke-b2.yaml").write_text("apiVersion: tekton.dev/v1")
    _mock_cm(monkeypatch, {})

    class Args:
        package = None
        skip_logs = False

    collected_phases = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        collected_phases.extend(phases)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert collected_phases == ["b1", "b2"]


def test_collect_with_workload_scope(tmp_path, monkeypatch):
    """--workload scopes extraction to matching workloads only."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline": {"workload": "load", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-treatment": {"workload": "load", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = None
        workload = "smoke"
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert extract_calls[0]["workload"] == "smoke"
    assert sorted(extract_calls[0]["phases"]) == ["baseline", "treatment"]


def test_collect_with_only_scope(tmp_path, monkeypatch):
    """--only scopes extraction to one pair's workload and package."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline": {"workload": "load", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = "wl-smoke-baseline"
        workload = None
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert extract_calls[0]["workload"] == "smoke"
    assert extract_calls[0]["phases"] == ["baseline"]


def test_collect_only_without_prefix(tmp_path, monkeypatch):
    """--only resolves wl- prefix automatically."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = "smoke-baseline"
        workload = None
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert extract_calls[0]["workload"] == "smoke"


def test_collect_workload_with_package_filter(tmp_path, monkeypatch):
    """--workload + --package compose: workload scopes within specified phases."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline": {"workload": "load", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = None
        workload = "smoke"
        package = ["baseline"]
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert extract_calls[0]["workload"] == "smoke"
    assert extract_calls[0]["phases"] == ["baseline"]


def test_collect_workload_no_match_exits(tmp_path, monkeypatch):
    """--workload with no matching pairs exits with error."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = None
        workload = "nonexistent"
        package = None
        skip_logs = False

    with pytest.raises(SystemExit):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})


def test_collect_warns_nondone_scoped_pairs(tmp_path, monkeypatch):
    """When scoped pairs include non-done entries, warn but continue with done ones."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "running"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = None
        workload = "smoke"
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract), \
         patch.object(deploy, "warn") as mock_warn:
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert extract_calls[0]["phases"] == ["baseline"]
    assert any("running" in str(c) for c in mock_warn.call_args_list)


def test_collect_unscoped_unchanged(tmp_path, monkeypatch):
    """Without --only/--workload, collect behaves exactly as before (no workload param)."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = None
        workload = None
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert extract_calls[0]["workload"] is None
    assert sorted(extract_calls[0]["phases"]) == ["baseline", "treatment"]


def test_collect_scoped_without_progress_exits(tmp_path, monkeypatch):
    """--workload without progress data exits with error."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    _mock_cm(monkeypatch, {})

    class Args:
        only = None
        workload = "smoke"
        package = None
        skip_logs = False

    with pytest.raises(SystemExit):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})


def test_collect_scoped_all_nondone_no_extraction(tmp_path, monkeypatch):
    """When all scoped pairs are non-done, no extraction is attempted and summary prints 0/0."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "running"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "pending"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = None
        workload = "smoke"
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract), \
         patch.object(deploy, "warn") as mock_warn:
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 0
    assert any("running" in str(c) or "pending" in str(c) for c in mock_warn.call_args_list)


def test_collect_scoped_runtime_error(tmp_path, monkeypatch):
    """RuntimeError from extractor pod is caught and reported."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = None
        workload = "smoke"
        package = None
        skip_logs = False

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        raise RuntimeError("pod failed")

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract), \
         patch.object(deploy, "warn") as mock_warn:
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert any("pod failed" in str(c) for c in mock_warn.call_args_list)


def test_collect_scoped_per_phase_failure(tmp_path, monkeypatch):
    """Per-phase extraction failure in scoped path populates failed list."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = None
        workload = "smoke"
        package = None
        skip_logs = False

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        return {
            "baseline": None,
            "treatment": RuntimeError("tar failed"),
        }

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract), \
         patch.object(deploy, "warn") as mock_warn:
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert any("tar failed" in str(c) for c in mock_warn.call_args_list)


def test_collect_only_takes_precedence_over_workload(tmp_path, monkeypatch):
    """When both --only and --workload are given, --only takes precedence."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline": {"workload": "load", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = "wl-smoke-baseline"
        workload = "load"
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert extract_calls[0]["workload"] == "smoke"
    assert extract_calls[0]["phases"] == ["baseline"]


def test_collect_unscoped_multi_namespace_dispatch(tmp_path, monkeypatch):
    """Unscoped collect dispatches one extract call per distinct completed_namespace."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline":   {"workload": "smoke", "package": "baseline",  "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment":  {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline":    {"workload": "load",  "package": "baseline",  "status": "done", "completed_namespace": "ns-1"},
        "wl-load-treatment":   {"workload": "load",  "package": "treatment", "status": "done", "completed_namespace": "ns-1"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({"namespace": namespace, "phases": sorted(phases), "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 2
    by_ns = {c["namespace"]: c for c in extract_calls}
    assert set(by_ns.keys()) == {"ns-0", "ns-1"}
    assert by_ns["ns-0"]["phases"] == ["baseline", "treatment"]
    assert by_ns["ns-1"]["phases"] == ["baseline", "treatment"]
    # Unscoped path passes no workload restriction
    assert all(c["workload"] is None for c in extract_calls)


def test_collect_unscoped_missing_completed_namespace_warns_and_skips(tmp_path, monkeypatch):
    """Done entries without completed_namespace emit a warning and are skipped."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append(phases)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract), \
         patch.object(deploy, "warn") as mock_warn:
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    # No extraction — all entries missing completed_namespace
    assert extract_calls == []
    warnings = [str(c) for c in mock_warn.call_args_list]
    assert any("completed_namespace" in w for w in warnings)


def test_collect_scoped_multi_namespace_dispatch(tmp_path, monkeypatch):
    """Scoped collect uses each workload's completed_namespace, not primary."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline":  {"workload": "smoke", "package": "baseline",  "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline":   {"workload": "load",  "package": "baseline",  "status": "done", "completed_namespace": "ns-1"},
        "wl-load-treatment":  {"workload": "load",  "package": "treatment", "status": "done", "completed_namespace": "ns-1"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = None
        workload = "smoke"
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({"namespace": namespace, "phases": sorted(phases), "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-primary"})

    assert len(extract_calls) == 1
    # Must use smoke's completed_namespace (ns-0), NOT the primary namespace
    assert extract_calls[0]["namespace"] == "ns-0"
    assert extract_calls[0]["workload"] == "smoke"
    assert sorted(extract_calls[0]["phases"]) == ["baseline", "treatment"]


def test_collect_scoped_missing_completed_namespace_warns_and_skips(tmp_path, monkeypatch):
    """Scoped collect warns and skips workloads whose done pairs lack completed_namespace."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline":  {"workload": "smoke", "package": "baseline",  "status": "done"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = None
        workload = "smoke"
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append(phases)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract), \
         patch.object(deploy, "warn") as mock_warn:
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert extract_calls == []
    warnings = [str(c) for c in mock_warn.call_args_list]
    assert any("completed_namespace" in w for w in warnings)


def test_collect_reads_from_configmap(tmp_path, monkeypatch):
    """collect reads progress from ConfigMapProgressStore."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)

    cm_data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, cm_data)

    class Args:
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert sorted(extract_calls[0]["phases"]) == ["baseline", "treatment"]



# ── Incremental collect helpers ──────────────────────────────────────────────

def test_probe_remote_mtimes_parses_output():
    """_probe_remote_mtimes parses stat output into {workload: mtime} dict."""
    from pipeline.deploy import _probe_remote_mtimes

    stat_output = (
        "1715800000 /data/run1/baseline/wl-smoke/trace_data.csv\n"
        "1715800100 /data/run1/baseline/wl-load/trace_data.csv\n"
    )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout=stat_output, stderr="")
        result = _probe_remote_mtimes("pod", "/data/run1/baseline", "ns-0")

    assert result == {"wl-smoke": 1715800000.0, "wl-load": 1715800100.0}


def test_probe_remote_mtimes_returns_empty_on_failure():
    """_probe_remote_mtimes returns {} and warns when kubectl exec fails."""
    from pipeline import deploy
    from pipeline.deploy import _probe_remote_mtimes

    with patch("subprocess.run") as mock_run, \
         patch.object(deploy, "warn") as mock_warn:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="exec failed")
        result = _probe_remote_mtimes("pod", "/data/run1/baseline", "ns-0")

    assert result == {}
    assert any("mtime probe failed" in str(c) for c in mock_warn.call_args_list)


def test_probe_remote_mtimes_logs_info_on_empty_stdout():
    """_probe_remote_mtimes logs info when find succeeds but finds nothing."""
    from pipeline import deploy
    from pipeline.deploy import _probe_remote_mtimes

    with patch("subprocess.run") as mock_run, \
         patch.object(deploy, "info") as mock_info:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = _probe_remote_mtimes("pod", "/data/run1/baseline", "ns-0")

    assert result == {}
    assert any("no trace_data.csv" in str(c) for c in mock_info.call_args_list)


def test_probe_remote_mtimes_warns_on_stderr():
    """_probe_remote_mtimes warns when stderr has content but command succeeds."""
    from pipeline import deploy
    from pipeline.deploy import _probe_remote_mtimes

    stat_output = "1715800000 /data/run1/baseline/wl-smoke/trace_data.csv\n"

    with patch("subprocess.run") as mock_run, \
         patch.object(deploy, "warn") as mock_warn:
        mock_run.return_value = MagicMock(
            returncode=0, stdout=stat_output,
            stderr="stat: /data/run1/baseline/wl-broken/trace_data.csv: No such file")
        result = _probe_remote_mtimes("pod", "/data/run1/baseline", "ns-0")

    assert result == {"wl-smoke": 1715800000.0}
    assert any("mtime probe had errors" in str(c) for c in mock_warn.call_args_list)


def test_probe_remote_mtimes_warns_on_unparseable_line():
    """_probe_remote_mtimes warns when float() fails on the mtime token."""
    from pipeline import deploy
    from pipeline.deploy import _probe_remote_mtimes

    stat_output = "garbage /data/run1/baseline/wl-bad/trace_data.csv\n1715800000 /data/run1/baseline/wl-smoke/trace_data.csv\n"

    with patch("subprocess.run") as mock_run, \
         patch.object(deploy, "warn") as mock_warn:
        mock_run.return_value = MagicMock(
            returncode=0, stdout=stat_output, stderr="")
        result = _probe_remote_mtimes("pod", "/data/run1/baseline", "ns-0")

    assert result == {"wl-smoke": 1715800000.0}
    assert any("unparseable" in str(c) for c in mock_warn.call_args_list)


def test_probe_remote_mtimes_warns_on_single_token_line():
    """_probe_remote_mtimes warns on lines with fewer than 2 tokens."""
    from pipeline import deploy
    from pipeline.deploy import _probe_remote_mtimes

    stat_output = "onlyone\n1715800000 /data/run1/baseline/wl-smoke/trace_data.csv\n"

    with patch("subprocess.run") as mock_run, \
         patch.object(deploy, "warn") as mock_warn:
        mock_run.return_value = MagicMock(
            returncode=0, stdout=stat_output, stderr="")
        result = _probe_remote_mtimes("pod", "/data/run1/baseline", "ns-0")

    assert result == {"wl-smoke": 1715800000.0}
    assert any("unparseable" in str(c) for c in mock_warn.call_args_list)


def test_is_up_to_date_true_when_local_newer(tmp_path):
    """_is_up_to_date returns True when local file is at least as new as remote."""
    from pipeline.deploy import _is_up_to_date

    local_csv = tmp_path / "trace_data.csv"
    local_csv.write_text("data")
    local_mtime = local_csv.stat().st_mtime

    assert _is_up_to_date(local_csv, local_mtime - 100) is True
    assert _is_up_to_date(local_csv, local_mtime) is True


def test_is_up_to_date_false_when_no_local(tmp_path):
    """_is_up_to_date returns False when local file does not exist."""
    from pipeline.deploy import _is_up_to_date

    assert _is_up_to_date(tmp_path / "trace_data.csv", 1715800000.0) is False


def test_is_up_to_date_false_when_remote_newer(tmp_path):
    """_is_up_to_date returns False when remote mtime is newer than local."""
    from pipeline.deploy import _is_up_to_date
    import os

    local_csv = tmp_path / "trace_data.csv"
    local_csv.write_text("data")
    old_time = 1000000000.0
    os.utime(local_csv, (old_time, old_time))

    assert _is_up_to_date(local_csv, old_time + 100) is False


def test_is_up_to_date_false_when_remote_mtime_none(tmp_path):
    """_is_up_to_date returns False when remote_mtime is None."""
    from pipeline.deploy import _is_up_to_date

    local_csv = tmp_path / "trace_data.csv"
    local_csv.write_text("data")

    assert _is_up_to_date(local_csv, None) is False


def test_is_up_to_date_false_on_os_error(tmp_path):
    """_is_up_to_date returns False and warns when stat raises OSError."""
    from pipeline import deploy
    from pipeline.deploy import _is_up_to_date

    local_csv = tmp_path / "trace_data.csv"
    local_csv.write_text("data")

    with patch.object(Path, "stat", side_effect=OSError("permission denied")), \
         patch.object(deploy, "warn") as mock_warn:
        assert _is_up_to_date(local_csv, 1715800000.0) is False

    assert any("stat failed" in str(c) for c in mock_warn.call_args_list)


def test_collect_unscoped_reports_per_pair_with_namespace(tmp_path, monkeypatch, capsys):
    """Unscoped collect reports each phase/workload with namespace context."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline": {"workload": "load", "package": "baseline", "status": "done", "completed_namespace": "ns-1"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        if on_workload_done and allowed_workloads:
            for phase in phases:
                for wl in allowed_workloads.get(phase, set()):
                    on_workload_done(phase, wl, namespace, None)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    captured = capsys.readouterr()
    output = captured.out + captured.err
    # Per-pair lines include phase/workload and namespace
    assert "baseline/smoke" in output
    assert "treatment/smoke" in output
    assert "baseline/load" in output
    assert "(ns-0)" in output
    assert "(ns-1)" in output
    # Summary shows pair count and root path
    assert "3/3 pairs" in captured.out
    assert str(run_dir / "results") in captured.out
    # Old format should NOT appear
    assert "Collected: baseline" not in captured.out
    assert "2/2 phases" not in captured.out


def test_collect_unscoped_failure_reports_pair_count(tmp_path, monkeypatch, capsys):
    """When extraction fails for a phase, summary shows correct pair counts."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        if on_workload_done:
            on_workload_done("baseline", "smoke", namespace, None)
            on_workload_done("treatment", "smoke", namespace, RuntimeError("disk full"))
        return {"baseline": None, "treatment": RuntimeError("disk full")}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    captured = capsys.readouterr()
    # 1 pair collected (baseline/smoke), 1 failed (treatment/smoke)
    assert "1/2 pairs" in captured.out
    assert "Failed:    1 pairs" in captured.out


def test_collect_scoped_reports_namespace_context(tmp_path, monkeypatch, capsys):
    """Scoped collect reports phase/workload with namespace."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-slot-0"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-slot-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        only = None
        workload = "smoke"
        package = None
        skip_logs = False

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-primary"})

    captured = capsys.readouterr()
    output = captured.out
    assert "baseline/smoke" in output
    assert "treatment/smoke" in output
    assert "(ns-slot-0)" in output
    assert "2/2 pairs" in output


def test_collect_unscoped_no_cross_product_on_slot_reuse(tmp_path, monkeypatch, capsys):
    """When a namespace slot is reused for different workloads, only actual pairs are reported."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    # ns-0 ran baseline/smoke and treatment/smoke, then was reused for baseline/load
    # treatment/load does NOT exist in ns-0
    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline": {"workload": "load", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        if on_workload_done and allowed_workloads:
            for phase in phases:
                for wl in allowed_workloads.get(phase, set()):
                    on_workload_done(phase, wl, namespace, None)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    captured = capsys.readouterr()
    output = captured.out + captured.err
    # Should report exactly 3 pairs, not 4 (no phantom treatment/load)
    assert "3/3 pairs" in captured.out
    assert "treatment/load" not in output
    assert "baseline/smoke" in output
    assert "baseline/load" in output
    assert "treatment/smoke" in output


# ── Parallel extraction tests ────────────────────────────────────────────────


def test_collect_unscoped_parallel_multi_ns(tmp_path, monkeypatch):
    """With multiple namespace slots, extraction runs via ThreadPoolExecutor."""
    from pipeline import deploy
    import concurrent.futures

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline":   {"workload": "smoke", "package": "baseline",  "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment":  {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline":    {"workload": "load",  "package": "baseline",  "status": "done", "completed_namespace": "ns-1"},
        "wl-load-treatment":   {"workload": "load",  "package": "treatment", "status": "done", "completed_namespace": "ns-1"},
        "wl-heavy-baseline":   {"workload": "heavy", "package": "baseline",  "status": "done", "completed_namespace": "ns-2"},
        "wl-heavy-treatment":  {"workload": "heavy", "package": "treatment", "status": "done", "completed_namespace": "ns-2"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({"namespace": namespace, "phases": sorted(phases)})
        return {p: None for p in phases}

    executor_used = []
    OrigExecutor = concurrent.futures.ThreadPoolExecutor

    class TrackingExecutor(OrigExecutor):
        def __init__(self, *a, **kw):
            executor_used.append(True)
            super().__init__(*a, **kw)

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract), \
         patch("concurrent.futures.ThreadPoolExecutor", TrackingExecutor):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 3
    ns_set = {c["namespace"] for c in extract_calls}
    assert ns_set == {"ns-0", "ns-1", "ns-2"}
    assert len(executor_used) == 1


def test_collect_unscoped_single_ns_no_threading(tmp_path, monkeypatch):
    """With a single namespace slot, extraction runs sequentially (no ThreadPoolExecutor)."""
    from pipeline import deploy
    import concurrent.futures

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline":   {"workload": "smoke", "package": "baseline",  "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment":  {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        return {p: None for p in phases}

    executor_used = []
    OrigExecutor = concurrent.futures.ThreadPoolExecutor

    class TrackingExecutor(OrigExecutor):
        def __init__(self, *a, **kw):
            executor_used.append(True)
            super().__init__(*a, **kw)

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract), \
         patch("concurrent.futures.ThreadPoolExecutor", TrackingExecutor):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(executor_used) == 0


def test_collect_unscoped_parallel_one_slot_fails(tmp_path, monkeypatch, capsys):
    """When one slot fails in parallel mode, other slots still succeed."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline":   {"workload": "smoke", "package": "baseline",  "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment":  {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline":    {"workload": "load",  "package": "baseline",  "status": "done", "completed_namespace": "ns-1"},
        "wl-load-treatment":   {"workload": "load",  "package": "treatment", "status": "done", "completed_namespace": "ns-1"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        if namespace == "ns-1":
            raise RuntimeError("pod not ready")
        if on_workload_done and allowed_workloads:
            for phase in phases:
                for wl in allowed_workloads.get(phase, set()):
                    on_workload_done(phase, wl, namespace, None)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "baseline/smoke" in out
    assert "treatment/smoke" in out
    assert "2/" in captured.out


def test_collect_unscoped_parallel_step_header(tmp_path, monkeypatch, capsys):
    """Step header announces slot count when multiple slots exist."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline":   {"workload": "smoke", "package": "baseline",  "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline":    {"workload": "load",  "package": "baseline",  "status": "done", "completed_namespace": "ns-1"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    out = capsys.readouterr().out
    assert "2 slots in parallel" in out


def test_collect_unscoped_parallel_non_runtime_error(tmp_path, monkeypatch, capsys):
    """Non-RuntimeError from one slot doesn't crash the loop — other slots still succeed."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline":   {"workload": "smoke", "package": "baseline",  "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment":  {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline":    {"workload": "load",  "package": "baseline",  "status": "done", "completed_namespace": "ns-1"},
        "wl-load-treatment":   {"workload": "load",  "package": "treatment", "status": "done", "completed_namespace": "ns-1"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        if namespace == "ns-1":
            raise OSError("kubectl binary not found")
        if on_workload_done and allowed_workloads:
            for phase in phases:
                for wl in allowed_workloads.get(phase, set()):
                    on_workload_done(phase, wl, namespace, None)
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "baseline/smoke" in out
    assert "treatment/smoke" in out
    assert "2/" in captured.out


def test_collect_unscoped_parallel_all_slots_fail(tmp_path, monkeypatch, capsys):
    """All slots failing produces correct summary with zero collected."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    data = {
        "wl-smoke-baseline":   {"workload": "smoke", "package": "baseline",  "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment":  {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline":    {"workload": "load",  "package": "baseline",  "status": "done", "completed_namespace": "ns-1"},
        "wl-load-treatment":   {"workload": "load",  "package": "treatment", "status": "done", "completed_namespace": "ns-1"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        raise RuntimeError(f"pod failed in {namespace}")

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    out = capsys.readouterr().out
    assert "0/" in out
    assert "Failed:" in out


# ── Stale file clearing tests ───────────────────────────────────────────────


def test_collect_full_copy_clears_stale_files(tmp_path, monkeypatch):
    """Full-copy path removes stale local files before kubectl cp."""
    from pipeline import deploy
    import subprocess

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    results_dir = run_dir / "results" / "baseline" / "wl-smoke"
    results_dir.mkdir(parents=True)

    stale_file = results_dir / "server_logs" / "stale.log"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text("stale")

    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    def mock_run(cmd, **kwargs):
        mock = MagicMock(returncode=0, stdout="", stderr="")
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "exec" in cmd_str and "ls" in cmd_str:
            mock.stdout = "wl-smoke"
        if "exec" in cmd_str and "stat" in cmd_str:
            mock.stdout = ""
        return mock

    monkeypatch.setattr(subprocess, "run", mock_run)

    deploy._extract_phases_from_pvc(
        ["baseline"], "test-run", "ns-0", run_dir, skip_logs=False)

    assert not stale_file.exists()


def test_collect_per_workload_clears_stale_files(tmp_path, monkeypatch):
    """Per-workload (scoped) path removes stale local files before kubectl cp."""
    from pipeline import deploy
    import subprocess

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    results_dir = run_dir / "results" / "baseline" / "wl-smoke"
    results_dir.mkdir(parents=True)

    stale_file = results_dir / "server_logs" / "stale.log"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text("stale")

    data = {
        "wl-smoke-baseline": {"workload": "wl-smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    def mock_run(cmd, **kwargs):
        mock = MagicMock(returncode=0, stdout="", stderr="")
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "exec" in cmd_str and "stat" in cmd_str:
            mock.stdout = ""
        return mock

    monkeypatch.setattr(subprocess, "run", mock_run)

    deploy._extract_phases_from_pvc(
        ["baseline"], "test-run", "ns-0", run_dir,
        skip_logs=False, workload="wl-smoke")

    assert not stale_file.exists()


def test_collect_skip_logs_clears_stale_log_dirs(tmp_path, monkeypatch):
    """--skip-logs path removes stale server_logs/ and epp_logs/ before copying."""
    from pipeline import deploy
    import subprocess

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    results_dir = run_dir / "results" / "baseline" / "wl-smoke"
    results_dir.mkdir(parents=True)

    stale_server = results_dir / "server_logs" / "stale.log"
    stale_server.parent.mkdir(parents=True)
    stale_server.write_text("stale server log")

    stale_epp = results_dir / "epp_logs" / "stale.log"
    stale_epp.parent.mkdir(parents=True)
    stale_epp.write_text("stale epp log")

    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    def mock_run(cmd, **kwargs):
        mock = MagicMock(returncode=0, stdout="", stderr="")
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "exec" in cmd_str and "ls" in cmd_str:
            mock.stdout = "wl-smoke"
        if "exec" in cmd_str and "stat" in cmd_str:
            mock.stdout = ""
        return mock

    monkeypatch.setattr(subprocess, "run", mock_run)

    deploy._extract_phases_from_pvc(
        ["baseline"], "test-run", "ns-0", run_dir, skip_logs=True)

    assert not stale_server.exists()
    assert not (results_dir / "server_logs").exists()
    assert not stale_epp.exists()


def test_collect_idempotent_no_stale_accumulation(tmp_path, monkeypatch):
    """Running collect twice produces same result as once (no accumulation)."""
    from pipeline import deploy
    import subprocess

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)

    data = {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    call_count = [0]

    def mock_run(cmd, **kwargs):
        mock = MagicMock(returncode=0, stdout="", stderr="")
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "exec" in cmd_str and "ls" in cmd_str:
            mock.stdout = "wl-smoke"
        elif "exec" in cmd_str and "stat" in cmd_str:
            mock.stdout = ""
        elif "cp" in cmd_str:
            call_count[0] += 1
            dest = run_dir / "results" / "baseline" / "wl-smoke"
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "trace_data.csv").write_text(f"data-{call_count[0]}")
            (dest / "server_logs").mkdir(exist_ok=True)
            (dest / "server_logs" / f"pod-{call_count[0]}.log").write_text("log")
        return mock

    monkeypatch.setattr(subprocess, "run", mock_run)

    deploy._extract_phases_from_pvc(
        ["baseline"], "test-run", "ns-0", run_dir, skip_logs=False)

    deploy._extract_phases_from_pvc(
        ["baseline"], "test-run", "ns-0", run_dir, skip_logs=False)

    results_dir = run_dir / "results" / "baseline" / "wl-smoke" / "server_logs"
    log_files = list(results_dir.glob("*.log"))
    assert len(log_files) == 1


# ── Per-slot workload filtering tests (issue #213) ───────────────────────────


def test_collect_parallel_filters_workloads_per_slot(tmp_path, monkeypatch):
    """In parallel mode (multiple namespace slots), each slot's extraction
    receives an allowed_workloads set containing ONLY the workloads that
    progress assigns to that slot."""
    from pipeline import deploy
    import concurrent.futures

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    # Two slots: ns-0 has smoke, ns-1 has load+heavy
    data = {
        "wl-smoke-baseline":   {"workload": "smoke", "package": "baseline",  "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment":  {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline":    {"workload": "load",  "package": "baseline",  "status": "done", "completed_namespace": "ns-1"},
        "wl-load-treatment":   {"workload": "load",  "package": "treatment", "status": "done", "completed_namespace": "ns-1"},
        "wl-heavy-baseline":   {"workload": "heavy", "package": "baseline",  "status": "done", "completed_namespace": "ns-1"},
        "wl-heavy-treatment":  {"workload": "heavy", "package": "treatment", "status": "done", "completed_namespace": "ns-1"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({
            "namespace": namespace,
            "phases": sorted(phases),
            "allowed_workloads": allowed_workloads,
        })
        return {p: None for p in phases}

    OrigExecutor = concurrent.futures.ThreadPoolExecutor

    class TrackingExecutor(OrigExecutor):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract), \
         patch("concurrent.futures.ThreadPoolExecutor", TrackingExecutor):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    # Should have 2 extract calls (one per slot)
    assert len(extract_calls) == 2

    # Build a map of namespace -> allowed_workloads for verification
    ns_to_allowed = {c["namespace"]: c["allowed_workloads"] for c in extract_calls}

    # ns-0 should allow smoke in both phases
    assert ns_to_allowed["ns-0"] == {"baseline": {"smoke"}, "treatment": {"smoke"}}, (
        f"Expected ns-0 per-phase allowed_workloads, got {ns_to_allowed['ns-0']}")

    # ns-1 should allow load+heavy in both phases
    assert ns_to_allowed["ns-1"] == {"baseline": {"load", "heavy"}, "treatment": {"load", "heavy"}}, (
        f"Expected ns-1 per-phase allowed_workloads, got {ns_to_allowed['ns-1']}")


def test_collect_sequential_filters_workloads_per_slot(tmp_path, monkeypatch):
    """In sequential mode (single slot), extraction still receives
    allowed_workloads containing the workloads assigned to that slot."""
    from pipeline import deploy
    import concurrent.futures

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    # Single slot: ns-0 has both workloads
    data = {
        "wl-smoke-baseline":   {"workload": "smoke", "package": "baseline",  "status": "done", "completed_namespace": "ns-0"},
        "wl-smoke-treatment":  {"workload": "smoke", "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
        "wl-load-baseline":    {"workload": "load",  "package": "baseline",  "status": "done", "completed_namespace": "ns-0"},
        "wl-load-treatment":   {"workload": "load",  "package": "treatment", "status": "done", "completed_namespace": "ns-0"},
    }
    _mock_cm(monkeypatch, data)

    class Args:
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None, allowed_workloads=None, on_workload_done=None):
        extract_calls.append({
            "namespace": namespace,
            "phases": sorted(phases),
            "allowed_workloads": allowed_workloads,
        })
        return {p: None for p in phases}

    executor_used = []
    OrigExecutor = concurrent.futures.ThreadPoolExecutor

    class TrackingExecutor(OrigExecutor):
        def __init__(self, *a, **kw):
            executor_used.append(True)
            super().__init__(*a, **kw)

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract), \
         patch("concurrent.futures.ThreadPoolExecutor", TrackingExecutor):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    # Sequential mode: no ThreadPoolExecutor used
    assert len(executor_used) == 0

    # Should have exactly 1 extract call
    assert len(extract_calls) == 1

    # The single slot should receive per-phase allowed_workloads
    call = extract_calls[0]
    assert call["namespace"] == "ns-0"
    assert call["allowed_workloads"] == {"baseline": {"smoke", "load"}, "treatment": {"smoke", "load"}}, (
        f"Expected per-phase allowed_workloads, got {call['allowed_workloads']}")


def test_extract_phases_filters_by_allowed_workloads(tmp_path, monkeypatch):
    """Inside _extract_phases_from_pvc, when allowed_workloads is set,
    only those workloads are copied even if ls discovers more on the PVC."""
    from pipeline import deploy
    import subprocess

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)

    copied_workloads = []

    def mock_run(cmd, **kwargs):
        mock = MagicMock(returncode=0, stdout="", stderr="")
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "exec" in cmd_str and "ls" in cmd_str:
            # PVC has three workloads
            mock.stdout = "wl-smoke\nwl-load\nwl-heavy"
        elif "exec" in cmd_str and "stat" in cmd_str:
            # No mtimes — force copy
            mock.stdout = ""
        elif "cp" in cmd_str:
            # Track which workloads get copied
            for wl in ("wl-smoke", "wl-load", "wl-heavy"):
                if wl in cmd_str:
                    copied_workloads.append(wl)
                    # Create the destination file so the code doesn't error
                    dest = run_dir / "results" / "baseline" / wl
                    dest.mkdir(parents=True, exist_ok=True)
                    break
        return mock

    monkeypatch.setattr(subprocess, "run", mock_run)

    # Also mock helper functions that would trip up without a real pod
    monkeypatch.setattr(deploy, "_probe_phase_sizes",
                        lambda pod, rn, phases, ns: {p: 100 for p in phases})
    monkeypatch.setattr(deploy, "_probe_remote_mtimes",
                        lambda pod, path, ns: {})
    monkeypatch.setattr(deploy, "_is_up_to_date", lambda local, remote: False)

    # Call with allowed_workloads limiting baseline phase to only smoke and heavy
    deploy._extract_phases_from_pvc(
        ["baseline"], "test-run", "ns-0", run_dir,
        skip_logs=False, allowed_workloads={"baseline": {"wl-smoke", "wl-heavy"}})

    # Only wl-smoke and wl-heavy should have been copied, NOT wl-load
    assert "wl-smoke" in copied_workloads, "wl-smoke should have been copied"
    assert "wl-heavy" in copied_workloads, "wl-heavy should have been copied"
    assert "wl-load" not in copied_workloads, (
        "wl-load should NOT have been copied when allowed_workloads excludes it")


def test_extract_phases_filters_by_allowed_workloads_skip_logs(tmp_path, monkeypatch):
    """Same as above but with skip_logs=True — the --skip-logs discovery path
    also respects allowed_workloads filtering."""
    from pipeline import deploy
    import subprocess

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)

    copied_workloads = []

    def mock_run(cmd, **kwargs):
        mock = MagicMock(returncode=0, stdout="", stderr="")
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "exec" in cmd_str and "ls" in cmd_str:
            mock.stdout = "wl-smoke\nwl-load\nwl-heavy"
        elif "exec" in cmd_str and "stat" in cmd_str:
            mock.stdout = ""
        elif "cp" in cmd_str:
            for wl in ("wl-smoke", "wl-load", "wl-heavy"):
                if wl in cmd_str:
                    copied_workloads.append(wl)
                    dest = run_dir / "results" / "baseline" / wl
                    dest.mkdir(parents=True, exist_ok=True)
                    break
        return mock

    monkeypatch.setattr(subprocess, "run", mock_run)

    monkeypatch.setattr(deploy, "_probe_phase_sizes",
                        lambda pod, rn, phases, ns: {p: 100 for p in phases})
    monkeypatch.setattr(deploy, "_probe_remote_mtimes",
                        lambda pod, path, ns: {})
    monkeypatch.setattr(deploy, "_is_up_to_date", lambda local, remote: False)

    deploy._extract_phases_from_pvc(
        ["baseline"], "test-run", "ns-0", run_dir,
        skip_logs=True, allowed_workloads={"baseline": {"wl-smoke", "wl-heavy"}})

    assert "wl-smoke" in copied_workloads, "wl-smoke should have been copied"
    assert "wl-heavy" in copied_workloads, "wl-heavy should have been copied"
    assert "wl-load" not in copied_workloads, (
        "wl-load should NOT have been copied in skip-logs mode when excluded")


def test_extract_phases_per_phase_filter_prevents_cross_phase_leak(tmp_path, monkeypatch):
    """Regression test for #216: a workload assigned to treatment on this slot
    must NOT be collected under baseline even if it exists on the PVC there."""
    from pipeline import deploy
    import subprocess

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)

    copied_pairs = []

    def mock_run(cmd, **kwargs):
        mock = MagicMock(returncode=0, stdout="", stderr="")
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "exec" in cmd_str and "ls" in cmd_str:
            # Both phases have chatbot and balanced on PVC (stale from prior run)
            mock.stdout = "chatbot\nbalanced"
        elif "exec" in cmd_str and "stat" in cmd_str:
            mock.stdout = ""
        elif "cp" in cmd_str:
            for phase in ("baseline", "treatment"):
                for wl in ("chatbot", "balanced"):
                    if f"/{phase}/{wl}/" in cmd_str:
                        copied_pairs.append(f"{phase}/{wl}")
                        dest = run_dir / "results" / phase / wl
                        dest.mkdir(parents=True, exist_ok=True)
                        break
        return mock

    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr(deploy, "_probe_phase_sizes",
                        lambda pod, rn, phases, ns: {p: 100 for p in phases})
    monkeypatch.setattr(deploy, "_probe_remote_mtimes",
                        lambda pod, path, ns: {})
    monkeypatch.setattr(deploy, "_is_up_to_date", lambda local, remote: False)

    # This slot is assigned: baseline/chatbot + treatment/balanced
    # PVC has both workloads under both phases (stale)
    deploy._extract_phases_from_pvc(
        ["baseline", "treatment"], "test-run", "ns-0", run_dir,
        skip_logs=False,
        allowed_workloads={"baseline": {"chatbot"}, "treatment": {"balanced"}})

    assert "baseline/chatbot" in copied_pairs
    assert "treatment/balanced" in copied_pairs
    assert "baseline/balanced" not in copied_pairs, (
        "baseline/balanced is stale — should NOT be collected (cross-phase leak)")
    assert "treatment/chatbot" not in copied_pairs, (
        "treatment/chatbot is stale — should NOT be collected (cross-phase leak)")
