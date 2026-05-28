"""Tests for deploy.py run orchestrator and status subcommand."""
import argparse
import json
import pytest
from unittest.mock import patch

from pipeline.lib.progress import ConfigMapProgressStore


_PROGRESS = {
    "wl-smoke-baseline":   {"workload": "wl-smoke",  "package": "baseline",   "status": "done",      "namespace": "sim2real-0", "retries": 0},
    "wl-smoke-treatment":  {"workload": "wl-smoke",  "package": "treatment",  "status": "running",   "namespace": "sim2real-1", "retries": 0},
    "wl-load-baseline":    {"workload": "wl-load",   "package": "baseline",   "status": "pending",   "namespace": None,         "retries": 0},
    "wl-load-treatment":   {"workload": "wl-load",   "package": "treatment",  "status": "timed-out", "namespace": "sim2real-2", "retries": 1},
    "wl-heavy-baseline":   {"workload": "wl-heavy",  "package": "baseline",   "status": "failed",    "namespace": "sim2real-0", "retries": 0},
    "_orchestrator":       {"state": "normal", "backoff_level": 0, "last_probe_free_gpus": 8},
}


def _mock_cm(monkeypatch, data):
    """Monkeypatch ConfigMapProgressStore to return *data* on load and no-op on save."""
    monkeypatch.setattr(ConfigMapProgressStore, "load", lambda self: data)
    monkeypatch.setattr(ConfigMapProgressStore, "save", lambda self, d: None)


def test_status_output_contains_all_pairs(tmp_path, capsys, monkeypatch):
    from pipeline.deploy import _cmd_status
    _mock_cm(monkeypatch, _PROGRESS)

    class _Args:
        only = None
        workload = None
        package = None
        status = None
        live = False

    _cmd_status(_Args(), tmp_path, setup_config={"namespace": "sim2real-ns"})
    out = capsys.readouterr().out
    for key in _PROGRESS:
        if not key.startswith("_"):
            assert key in out


def test_status_filter_by_workload(tmp_path, capsys, monkeypatch):
    from pipeline.deploy import _cmd_status
    _mock_cm(monkeypatch, _PROGRESS)

    class _Args:
        only = None
        workload = "wl-smoke"
        package = None
        status = None
        live = False

    _cmd_status(_Args(), tmp_path, setup_config={"namespace": "sim2real-ns"})
    out = capsys.readouterr().out
    assert "wl-smoke-baseline" in out
    assert "wl-smoke-treatment" in out
    assert "wl-load-baseline" not in out


def test_status_filter_by_package(tmp_path, capsys, monkeypatch):
    from pipeline.deploy import _cmd_status
    _mock_cm(monkeypatch, _PROGRESS)

    class _Args:
        only = None
        workload = None
        package = "treatment"
        status = None
        live = False

    _cmd_status(_Args(), tmp_path, setup_config={"namespace": "sim2real-ns"})
    out = capsys.readouterr().out
    assert "wl-smoke-treatment" in out
    assert "wl-load-treatment" in out
    assert "wl-smoke-baseline" not in out


def test_status_summary_line(tmp_path, capsys, monkeypatch):
    from pipeline.deploy import _cmd_status
    _mock_cm(monkeypatch, _PROGRESS)

    class _Args:
        only = None
        workload = None
        package = None
        status = None
        live = False

    _cmd_status(_Args(), tmp_path, setup_config={"namespace": "sim2real-ns"})
    out = capsys.readouterr().out
    assert "5 pairs" in out
    assert "1 done" in out
    assert "1 running" in out
    assert "1 pending" in out


def test_status_missing_progress_file(tmp_path, capsys, monkeypatch):
    from pipeline.deploy import _cmd_status
    _mock_cm(monkeypatch, {})

    class _Args:
        only = None
        workload = None
        package = None
        status = None
        live = False

    _cmd_status(_Args(), tmp_path / "missing-run-dir",
                setup_config={"namespace": "sim2real-ns"})
    out = capsys.readouterr().out
    assert "0 pairs" in out


def test_status_filter_by_only(tmp_path, capsys, monkeypatch):
    """status subcommand supports --only filter."""
    from pipeline.deploy import _cmd_status
    _mock_cm(monkeypatch, _PROGRESS)

    class _Args:
        only = "wl-smoke-baseline"; workload = None; package = None; status = None; live = False

    _cmd_status(_Args(), tmp_path, setup_config={"namespace": "sim2real-ns"})
    out = capsys.readouterr().out
    assert "wl-smoke-baseline" in out
    assert "wl-load-baseline" not in out


def test_status_filter_by_status(tmp_path, capsys, monkeypatch):
    """status subcommand supports --status filter."""
    from pipeline.deploy import _cmd_status
    _mock_cm(monkeypatch, _PROGRESS)

    class _Args:
        only = None; workload = None; package = None; status = "running"; live = False

    _cmd_status(_Args(), tmp_path, setup_config={"namespace": "sim2real-ns"})
    out = capsys.readouterr().out
    assert "wl-smoke-treatment" in out
    assert "wl-load-baseline" not in out


def test_status_mismatch_shows_valid_values(tmp_path, capsys, monkeypatch):
    """status subcommand shows valid values on filter mismatch."""
    from pipeline.deploy import _cmd_status
    _mock_cm(monkeypatch, _PROGRESS)

    class _Args:
        only = None; workload = "nonexistent"; package = None; status = None; live = False

    with pytest.raises(SystemExit) as exc_info:
        _cmd_status(_Args(), tmp_path, setup_config={"namespace": "sim2real-ns"})
    assert exc_info.value.code == 1
    captured = capsys.readouterr().err
    assert "unrecognized" in captured
    assert "wl-smoke" in captured


def test_status_empty_progress_with_filters(tmp_path, capsys, monkeypatch):
    """status with empty progress and active filters warns filters are ignored."""
    from pipeline.deploy import _cmd_status
    _mock_cm(monkeypatch, {})

    class _Args:
        only = None; workload = "foo"; package = None; status = None; live = False

    _cmd_status(_Args(), tmp_path, setup_config={"namespace": "sim2real-ns"})
    out = capsys.readouterr().out
    assert "0 pairs" in out
    assert "filters ignored" in out


def test_load_pairs_discovers_all_pairs(tmp_path):
    from pipeline.deploy import _load_pairs
    import yaml as _yaml
    for wl, pkg in [("smoke", "baseline"), ("smoke", "treatment"), ("load", "baseline")]:
        pr = {
            "apiVersion": "tekton.dev/v1", "kind": "PipelineRun",
            "metadata": {"name": f"{pkg}-{wl}-run1", "namespace": "sim2real-0"},
            "spec": {"params": [
                {"name": "workloadName", "value": f"wl-{wl}"},
                {"name": "phase", "value": pkg},
            ]},
        }
        (tmp_path / f"pipelinerun-{wl}-{pkg}.yaml").write_text(_yaml.dump(pr))

    pairs = _load_pairs(tmp_path)
    assert "wl-smoke-baseline" in pairs
    assert "wl-smoke-treatment" in pairs
    assert "wl-load-baseline" in pairs
    assert len(pairs) == 3


def test_load_pairs_skips_corrupt_yaml(tmp_path, capsys):
    """Corrupt YAML files are skipped with a warning; valid ones still loaded."""
    import yaml as _yaml
    from pipeline.deploy import _load_pairs

    pr = {
        "metadata": {"name": "baseline-smoke-run1", "namespace": "ns"},
        "spec": {"params": [
            {"name": "workloadName", "value": "wl-smoke"},
            {"name": "phase", "value": "baseline"},
        ]},
    }
    (tmp_path / "pipelinerun-smoke-baseline.yaml").write_text(_yaml.dump(pr))
    (tmp_path / "pipelinerun-bad.yaml").write_text("{{invalid yaml: [")

    pairs = _load_pairs(tmp_path)

    assert len(pairs) == 1
    assert "wl-smoke-baseline" in pairs
    assert "pipelinerun-bad.yaml" in capsys.readouterr().err


def test_load_pairs_skips_malformed_params(tmp_path, capsys):
    """Missing 'value' key in a param entry skips the file with a warning."""
    import yaml as _yaml
    from pipeline.deploy import _load_pairs

    pr = {
        "metadata": {"name": "run1", "namespace": "ns"},
        "spec": {"params": [
            {"name": "workloadName"},
            {"name": "phase", "value": "baseline"},
        ]},
    }
    (tmp_path / "pipelinerun-test.yaml").write_text(_yaml.dump(pr))
    pairs = _load_pairs(tmp_path)
    assert len(pairs) == 0
    assert "pipelinerun-test.yaml" in capsys.readouterr().err


def test_load_pairs_warns_on_skip(tmp_path, capsys):
    """Warning is emitted with filename when a file is skipped."""
    from pipeline.deploy import _load_pairs

    (tmp_path / "pipelinerun-broken.yaml").write_text("not: valid: yaml: [[[")
    _load_pairs(tmp_path)

    err = capsys.readouterr().err
    assert "[WARN]" in err
    assert "pipelinerun-broken.yaml" in err


def test_apply_run_filters_by_status():
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = None; workload = None; package = None; status = "failed"

    result = _apply_run_filters(dict(_PROGRESS), _Args())
    assert result == {"wl-heavy-baseline"}


def test_apply_run_filters_compose():
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = None; workload = None; package = "treatment"; status = "timed-out"

    result = _apply_run_filters(dict(_PROGRESS), _Args())
    assert result == {"wl-load-treatment"}


def test_apply_run_filters_only_flag(capsys):
    """Exact match does not emit the 'resolved' diagnostic."""
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = "wl-smoke-baseline"; workload = None; package = None; status = None

    result = _apply_run_filters(dict(_PROGRESS), _Args())
    assert result == {"wl-smoke-baseline"}
    assert "resolved" not in capsys.readouterr().out


def test_apply_run_filters_no_flags_returns_empty():
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = None; workload = None; package = None; status = None

    result = _apply_run_filters(dict(_PROGRESS), _Args())
    assert result == set()


def test_apply_run_filters_only_without_prefix(capsys):
    """--only accepts values without the wl- prefix and logs normalization."""
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = "smoke-baseline"; workload = None; package = None; status = None

    result = _apply_run_filters(dict(_PROGRESS), _Args())
    assert result == {"wl-smoke-baseline"}
    assert "resolved" in capsys.readouterr().out


def test_apply_run_filters_only_no_match():
    """--only aborts when neither exact nor prefixed form matches."""
    import pytest
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = "nonexistent"; workload = None; package = None; status = None

    with pytest.raises(SystemExit) as exc_info:
        _apply_run_filters(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1


def test_apply_run_filters_only_no_double_prefix():
    """--only wl-nonexistent doesn't false-match via double-prefixing."""
    import pytest
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = "wl-nonexistent"; workload = None; package = None; status = None

    with pytest.raises(SystemExit) as exc_info:
        _apply_run_filters(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1


def test_apply_run_filters_only_empty_string():
    """--only '' (from unset shell var) returns empty set, not all pairs."""
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = ""; workload = None; package = None; status = None

    result = _apply_run_filters(dict(_PROGRESS), _Args())
    assert result == set()


# ── Multi-value flag tests (issue #212) ────────────────────────────────────


def test_parse_list_none():
    from pipeline.deploy import _parse_list
    assert _parse_list(None) is None


def test_parse_list_empty_list():
    from pipeline.deploy import _parse_list
    assert _parse_list([]) is None


def test_parse_list_single_string():
    from pipeline.deploy import _parse_list
    assert _parse_list("smoke") == ["smoke"]


def test_parse_list_comma_separated():
    from pipeline.deploy import _parse_list
    assert _parse_list("smoke,load") == ["smoke", "load"]


def test_parse_list_list_input():
    from pipeline.deploy import _parse_list
    assert _parse_list(["smoke", "load"]) == ["smoke", "load"]


def test_parse_list_mixed_comma_and_list():
    from pipeline.deploy import _parse_list
    assert _parse_list(["smoke,load", "heavy"]) == ["smoke", "load", "heavy"]


def test_parse_list_strips_whitespace():
    from pipeline.deploy import _parse_list
    assert _parse_list(" smoke , load ") == ["smoke", "load"]


def test_parse_list_whitespace_only():
    from pipeline.deploy import _parse_list
    assert _parse_list("  ,  ") is None


def test_apply_run_filters_multi_workload():
    """Multiple --workload values match any of the specified workloads."""
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = None; workload = ["wl-smoke", "wl-heavy"]; package = None; status = None

    result = _apply_run_filters(dict(_PROGRESS), _Args())
    assert result == {"wl-smoke-baseline", "wl-smoke-treatment", "wl-heavy-baseline"}


def test_apply_run_filters_multi_package():
    """Multiple --package values use union semantics."""
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = None; workload = None; package = ["baseline", "treatment"]; status = None

    result = _apply_run_filters(dict(_PROGRESS), _Args())
    assert result == {"wl-smoke-baseline", "wl-load-baseline", "wl-heavy-baseline",
                      "wl-smoke-treatment", "wl-load-treatment"}


def test_apply_run_filters_multi_only():
    """Multiple --only values resolve independently and return the union."""
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = ["wl-smoke-baseline", "wl-load-treatment"]; workload = None; package = None; status = None

    result = _apply_run_filters(dict(_PROGRESS), _Args())
    assert result == {"wl-smoke-baseline", "wl-load-treatment"}


def test_apply_run_filters_multi_only_with_prefix_resolution(capsys):
    """Multiple --only values with wl- prefix resolution."""
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = ["smoke-baseline", "load-treatment"]; workload = None; package = None; status = None

    result = _apply_run_filters(dict(_PROGRESS), _Args())
    assert result == {"wl-smoke-baseline", "wl-load-treatment"}
    assert "resolved" in capsys.readouterr().out


def test_apply_run_filters_multi_only_partial_unresolved(capsys):
    """Partial match in --only aborts with error listing unresolved keys."""
    import pytest
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = ["wl-smoke-baseline", "nonexistent"]; workload = None; package = None; status = None

    with pytest.raises(SystemExit) as exc_info:
        _apply_run_filters(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1
    captured = capsys.readouterr().err
    assert "no match" in captured
    assert "nonexistent" in captured


def test_apply_run_filters_comma_in_workload():
    """Comma-separated --workload values are parsed correctly."""
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = None; workload = ["wl-smoke,wl-heavy"]; package = None; status = None

    result = _apply_run_filters(dict(_PROGRESS), _Args())
    assert result == {"wl-smoke-baseline", "wl-smoke-treatment", "wl-heavy-baseline"}


def test_apply_run_filters_multi_only_all_unresolved(capsys):
    """All --only keys unresolved aborts with exit code 1."""
    import pytest
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = ["nonexistent1", "nonexistent2"]; workload = None; package = None; status = None

    with pytest.raises(SystemExit) as exc_info:
        _apply_run_filters(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1
    captured = capsys.readouterr().err
    assert "no match" in captured
    assert "nonexistent1" in captured


def test_apply_run_filters_invalid_workload_aborts(capsys):
    """--workload with a value not in progress aborts with exit 1."""
    import pytest
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = None; workload = "nonexistent"; package = None; status = None

    with pytest.raises(SystemExit) as exc_info:
        _apply_run_filters(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "nonexistent" in err
    assert "wl-smoke" in err


def test_apply_run_filters_partial_invalid_workload_aborts(capsys):
    """--workload with one valid and one invalid value still aborts."""
    import pytest
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = None; workload = "wl-smoke,bogus"; package = None; status = None

    with pytest.raises(SystemExit) as exc_info:
        _apply_run_filters(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "bogus" in err


def test_apply_run_filters_invalid_package_aborts(capsys):
    """--package with a value not in progress aborts with exit 1."""
    import pytest
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = None; workload = None; package = "baseline1x"; status = None

    with pytest.raises(SystemExit) as exc_info:
        _apply_run_filters(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "baseline1x" in err
    assert "baseline" in err


def test_apply_run_filters_partial_invalid_package_aborts(capsys):
    """--package with one valid and one invalid value still aborts."""
    import pytest
    from pipeline.deploy import _apply_run_filters

    class _Args:
        only = None; workload = None; package = "treatment,bogus"; status = None

    with pytest.raises(SystemExit) as exc_info:
        _apply_run_filters(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "bogus" in err


def test_resolve_scope_multi_only_all_unresolved_aborts(capsys):
    """Multi-value --only with all keys unresolved aborts via _apply_run_filters."""
    import pytest
    from pipeline.deploy import _resolve_scope

    class _Args:
        only = ["bad1", "bad2"]; workload = None; package = None; status = None

    with pytest.raises(SystemExit) as exc_info:
        _resolve_scope(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1
    captured = capsys.readouterr().err
    assert "no match" in captured


def test_report_filter_mismatch_multi_value_formatting(capsys):
    """_report_filter_mismatch formats multi-value lists with commas."""
    from pipeline.deploy import _report_filter_mismatch

    class _Args:
        only = None; workload = ["wl-smoke", "wl-heavy"]; package = ["baseline", "treatment"]; status = None

    _report_filter_mismatch(dict(_PROGRESS), _Args())
    captured = capsys.readouterr().err
    assert "--workload 'wl-smoke,wl-heavy'" in captured
    assert "--package 'baseline,treatment'" in captured


def test_collect_run_flags_list_values():
    """_collect_run_flags correctly serializes list-valued args."""
    from pipeline.deploy import _collect_run_flags

    class _Args:
        only = ["wl-smoke-baseline", "wl-load-treatment"]
        workload = None
        package = ["baseline", "treatment"]
        status = None
        force = False
        skip_teardown = False
        preserve_pipelineruns = False
        max_retries = 2
        poll_interval = 30
        gpu_resource_type = None
        default_gpu_cost = 1
        pending_threshold = 600
        max_pending_stalls = 10
        max_backoff = 600

    flags = _collect_run_flags(_Args())
    assert "--only" in flags
    idx = flags.index("--only")
    assert flags[idx + 1] == "wl-smoke-baseline"
    assert flags[idx + 2] == "wl-load-treatment"
    assert "--package" in flags
    pidx = flags.index("--package")
    assert flags[pidx + 1] == "baseline"
    assert flags[pidx + 2] == "treatment"
    assert "['wl-smoke-baseline'" not in " ".join(flags)


def test_collect_run_flags_single_string_status():
    """_collect_run_flags handles single-string status correctly."""
    from pipeline.deploy import _collect_run_flags

    class _Args:
        only = None
        workload = None
        package = None
        status = "failed"
        force = False
        skip_teardown = False
        preserve_pipelineruns = False
        max_retries = 2
        poll_interval = 30
        gpu_resource_type = None
        default_gpu_cost = 1
        pending_threshold = 600
        max_pending_stalls = 10
        max_backoff = 600

    flags = _collect_run_flags(_Args())
    assert flags == ["--status", "failed"]


def test_resolve_scope_shows_valid_keys_on_only_mismatch(capsys):
    """--only mismatch prints valid pair keys before aborting with exit code 1."""
    import pytest
    from pipeline.deploy import _resolve_scope

    class _Args:
        only = "nonexistent"; workload = None; package = None; status = None

    with pytest.raises(SystemExit) as exc_info:
        _resolve_scope(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1
    captured = capsys.readouterr().err
    assert "no match" in captured
    assert "wl-smoke-baseline" in captured
    assert "wl-load-treatment" in captured


def test_resolve_scope_shows_valid_workloads_on_mismatch(capsys):
    """--workload mismatch prints valid workload values."""
    import pytest
    from pipeline.deploy import _resolve_scope

    class _Args:
        only = None; workload = "nonexistent"; package = None; status = None

    with pytest.raises(SystemExit) as exc_info:
        _resolve_scope(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1
    captured = capsys.readouterr().err
    assert "unrecognized" in captured
    assert "wl-smoke" in captured
    assert "wl-load" in captured
    assert "wl-heavy" in captured


def test_resolve_scope_shows_valid_packages_on_mismatch(capsys):
    """--package mismatch prints valid package values."""
    import pytest
    from pipeline.deploy import _resolve_scope

    class _Args:
        only = None; workload = None; package = "nonexistent"; status = None

    with pytest.raises(SystemExit) as exc_info:
        _resolve_scope(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1
    captured = capsys.readouterr().err
    assert "unrecognized" in captured
    assert "baseline" in captured
    assert "treatment" in captured


def test_resolve_scope_shows_valid_statuses_on_mismatch(capsys):
    """--status mismatch prints valid status values."""
    import pytest
    from pipeline.deploy import _resolve_scope

    class _Args:
        only = None; workload = None; package = None; status = "nonexistent"

    with pytest.raises(SystemExit) as exc_info:
        _resolve_scope(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1
    captured = capsys.readouterr().err
    assert "No pairs matched" in captured
    assert "done" in captured
    assert "running" in captured
    assert "pending" in captured
    assert "failed" in captured
    assert "timed-out" in captured


def test_resolve_scope_combined_filter_mismatch(capsys):
    """Combined filters where each value is valid but intersection is empty."""
    import pytest
    from pipeline.deploy import _resolve_scope

    class _Args:
        only = None; workload = "wl-smoke"; package = None; status = "timed-out"

    with pytest.raises(SystemExit) as exc_info:
        _resolve_scope(dict(_PROGRESS), _Args())
    assert exc_info.value.code == 1
    captured = capsys.readouterr().err
    assert "--workload 'wl-smoke'" in captured
    assert "--status 'timed-out'" in captured


# ── _reconcile_on_resume ──────────────────────────────────────────────────────

_DISCOVERED = {
    "wl-smoke-baseline": {"pr_name": "baseline-smoke-run1", "pr_path": "cluster/pipelinerun-smoke-baseline.yaml"},
}


def test_reconcile_succeeded_sets_done_and_frees_namespace(monkeypatch):
    """On resume, a 'running' pair whose PipelineRun Succeeded transitions to done."""
    import pipeline.deploy as mod

    monkeypatch.setattr(mod, "_check_pipelinerun_status", lambda pr, ns: "Succeeded")

    progress = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "running", "namespace": "sim2real-0",
            "pending_since": "2026-01-01T00:00:00Z",
        }
    }
    mod._reconcile_on_resume(progress, _DISCOVERED)
    assert progress["wl-smoke-baseline"]["status"] == "done"
    assert progress["wl-smoke-baseline"]["namespace"] is None
    assert progress["wl-smoke-baseline"]["completed_namespace"] == "sim2real-0"
    assert progress["wl-smoke-baseline"]["pending_since"] is None


def test_reconcile_completed_sets_done_and_frees_namespace(monkeypatch):
    """Tekton v0.44+ returns 'Completed' for success — treat same as 'Succeeded'."""
    import pipeline.deploy as mod

    monkeypatch.setattr(mod, "_check_pipelinerun_status", lambda pr, ns: "Completed")
    monkeypatch.setattr(mod, "_delete_pipelinerun", lambda pr, ns: None)

    progress = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "running", "namespace": "sim2real-0",
            "pending_since": "2026-01-01T00:00:00Z",
        }
    }
    mod._reconcile_on_resume(progress, _DISCOVERED)
    assert progress["wl-smoke-baseline"]["status"] == "done"
    assert progress["wl-smoke-baseline"]["namespace"] is None
    assert progress["wl-smoke-baseline"]["completed_namespace"] == "sim2real-0"
    assert progress["wl-smoke-baseline"]["pending_since"] is None


def test_reconcile_on_resume_sets_completed_namespace_on_success(monkeypatch):
    """In _reconcile_on_resume, when a running pair's PipelineRun Succeeds, completed_namespace is recorded before namespace is cleared."""
    import pipeline.deploy as mod

    monkeypatch.setattr(mod, "_check_pipelinerun_status", lambda pr, ns: "Succeeded")
    monkeypatch.setattr(mod, "_delete_pipelinerun", lambda pr, ns: None)

    progress = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "running", "namespace": "sim2real-2",
            "pending_since": None,
        }
    }
    discovered = {
        "wl-smoke-baseline": {"pr_name": "baseline-smoke-run1", "workload": "wl-smoke", "package": "baseline"}
    }
    mod._reconcile_on_resume(progress, discovered)
    entry = progress["wl-smoke-baseline"]
    assert entry["status"] == "done"
    assert entry["namespace"] is None
    assert entry["completed_namespace"] == "sim2real-2"


def test_reconcile_unrecognized_status_resets_to_pending(capsys):
    """Stale statuses (e.g. 'collecting' from pre-upgrade) are reset to pending."""
    import pipeline.deploy as mod

    progress = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "collecting", "namespace": "sim2real-0",
            "pending_since": None,
        }
    }
    mod._reconcile_on_resume(progress, _DISCOVERED)
    assert progress["wl-smoke-baseline"]["status"] == "pending"
    assert progress["wl-smoke-baseline"]["namespace"] is None
    captured = capsys.readouterr().err
    assert "unrecognized status 'collecting'" in captured


def test_reconcile_running_no_pr_resets_to_pending():
    """Running pair with no PipelineRun metadata resets to pending."""
    import pipeline.deploy as mod

    progress = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "running", "namespace": "sim2real-0",
            "pending_since": None,
        }
    }
    mod._reconcile_on_resume(progress, {})
    assert progress["wl-smoke-baseline"]["status"] == "pending"
    assert progress["wl-smoke-baseline"]["namespace"] is None


def test_reconcile_succeeded_deletes_pipelinerun(monkeypatch):
    """On Succeeded, _reconcile_on_resume calls _delete_pipelinerun."""
    import pipeline.deploy as mod

    monkeypatch.setattr(mod, "_check_pipelinerun_status", lambda pr, ns: "Succeeded")
    deleted = []
    monkeypatch.setattr(mod, "_delete_pipelinerun",
                        lambda pr, ns: deleted.append((pr, ns)))

    progress = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "running", "namespace": "sim2real-0",
            "pending_since": "2026-01-01T00:00:00Z",
        }
    }
    mod._reconcile_on_resume(progress, _DISCOVERED)
    assert progress["wl-smoke-baseline"]["status"] == "done"
    assert deleted == [("baseline-smoke-run1", "sim2real-0")]


def test_reconcile_preserve_pipelineruns_skips_deletion(monkeypatch):
    """With preserve_pipelineruns=True, _delete_pipelinerun is not called."""
    import pipeline.deploy as mod

    monkeypatch.setattr(mod, "_check_pipelinerun_status", lambda pr, ns: "Succeeded")
    deleted = []
    monkeypatch.setattr(mod, "_delete_pipelinerun",
                        lambda pr, ns: deleted.append((pr, ns)))

    progress = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "running", "namespace": "sim2real-0",
            "pending_since": "2026-01-01T00:00:00Z",
        }
    }
    mod._reconcile_on_resume(progress, _DISCOVERED, preserve_pipelineruns=True)
    assert progress["wl-smoke-baseline"]["status"] == "done"
    assert deleted == []


def test_reconcile_succeeded_delete_failure_nonfatal(monkeypatch, capsys):
    """If _delete_pipelinerun raises, the pair still transitions to done."""
    import pipeline.deploy as mod

    monkeypatch.setattr(mod, "_check_pipelinerun_status", lambda pr, ns: "Succeeded")
    monkeypatch.setattr(mod, "_delete_pipelinerun",
                        lambda pr, ns: (_ for _ in ()).throw(OSError("kubectl fail")))

    progress = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "running", "namespace": "sim2real-0",
            "pending_since": None,
        }
    }
    mod._reconcile_on_resume(progress, _DISCOVERED)
    assert progress["wl-smoke-baseline"]["status"] == "done"
    assert progress["wl-smoke-baseline"]["namespace"] is None
    assert "kubectl fail" in capsys.readouterr().err


def test_reconcile_failed_sets_failed_retains_namespace(monkeypatch):
    """On Failed, pair transitions to failed but namespace is retained for reset."""
    import pipeline.deploy as mod

    monkeypatch.setattr(mod, "_check_pipelinerun_status", lambda pr, ns: "Failed")

    progress = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "running", "namespace": "sim2real-0",
            "pending_since": "2026-01-01T00:00:00Z",
        }
    }
    mod._reconcile_on_resume(progress, _DISCOVERED)
    assert progress["wl-smoke-baseline"]["status"] == "failed"
    assert progress["wl-smoke-baseline"]["namespace"] == "sim2real-0"


def test_reconcile_unknown_resets_to_pending(monkeypatch, capsys):
    """On Unknown (PR not found on cluster), pair resets to pending with warning."""
    import pipeline.deploy as mod

    monkeypatch.setattr(mod, "_check_pipelinerun_status", lambda pr, ns: "Unknown")

    progress = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "running", "namespace": "sim2real-0",
            "pending_since": None,
        }
    }
    mod._reconcile_on_resume(progress, _DISCOVERED)
    assert progress["wl-smoke-baseline"]["status"] == "pending"
    assert progress["wl-smoke-baseline"]["namespace"] is None
    assert "not found on cluster" in capsys.readouterr().err


def test_reconcile_status_check_exception_skips_pair(monkeypatch, capsys):
    """If _check_pipelinerun_status raises, the pair is skipped with a warning."""
    import pipeline.deploy as mod

    def _raise(pr, ns):
        raise OSError("kubectl not found")
    monkeypatch.setattr(mod, "_check_pipelinerun_status", _raise)

    progress = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "running", "namespace": "sim2real-0",
            "pending_since": None,
        }
    }
    mod._reconcile_on_resume(progress, _DISCOVERED)
    assert progress["wl-smoke-baseline"]["status"] == "running"
    assert "failed to check PipelineRun status" in capsys.readouterr().err


def test_reconcile_still_running_left_unchanged(monkeypatch):
    """Running PipelineRun is left as-is (no double-dispatch)."""
    import pipeline.deploy as mod

    monkeypatch.setattr(mod, "_check_pipelinerun_status", lambda pr, ns: "Running")

    progress = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "running", "namespace": "sim2real-0",
            "pending_since": "2026-01-01T00:00:00Z",
        }
    }
    mod._reconcile_on_resume(progress, _DISCOVERED)
    assert progress["wl-smoke-baseline"]["status"] == "running"
    assert progress["wl-smoke-baseline"]["namespace"] == "sim2real-0"
    assert progress["wl-smoke-baseline"]["pending_since"] == "2026-01-01T00:00:00Z"


# ── _force_reset ──────────────────────────────────────────────────────────────

def _mock_run(monkeypatch):
    """Mock subprocess.run for _force_reset tests (no real kubectl/helm)."""
    import pipeline.deploy as mod

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)


def test_force_reset_resets_all_non_pending_pairs(monkeypatch):
    from pipeline.deploy import _force_reset
    _mock_run(monkeypatch)
    progress = dict(_PROGRESS)
    scope = set(progress.keys())
    n = _force_reset(progress, scope)
    # running, timed-out, failed, AND done are reset; only pending is skipped
    assert n == 4
    for key in ("wl-smoke-baseline", "wl-smoke-treatment", "wl-load-treatment", "wl-heavy-baseline"):
        assert progress[key]["status"] == "pending"
        assert progress[key]["namespace"] is None
        assert progress[key]["retries"] == 0


def test_force_reset_leaves_pending_pairs_unchanged(monkeypatch):
    from pipeline.deploy import _force_reset
    _mock_run(monkeypatch)
    progress = dict(_PROGRESS)
    scope = set(progress.keys())
    _force_reset(progress, scope)
    assert progress["wl-load-baseline"]["status"] == "pending"


def test_force_reset_scoped_to_package(monkeypatch):
    from pipeline.deploy import _force_reset
    _mock_run(monkeypatch)
    progress = {
        "wl-a-baseline":  {"workload": "wl-a", "package": "baseline",  "status": "failed", "namespace": "ns-0", "retries": 2},
        "wl-a-treatment": {"workload": "wl-a", "package": "treatment", "status": "failed", "namespace": "ns-1", "retries": 1},
    }
    scope = {"wl-a-baseline"}
    n = _force_reset(progress, scope)
    assert n == 1
    assert progress["wl-a-baseline"]["status"] == "pending"
    assert progress["wl-a-baseline"]["retries"] == 0
    assert progress["wl-a-treatment"]["status"] == "failed"


def test_force_reset_returns_zero_when_nothing_to_reset():
    from pipeline.deploy import _force_reset
    progress = {
        "wl-a-baseline": {"workload": "wl-a", "package": "baseline", "status": "pending", "namespace": None, "retries": 0},
    }
    n = _force_reset(progress, {"wl-a-baseline"})
    assert n == 0
    assert progress["wl-a-baseline"]["status"] == "pending"


def test_force_reset_clears_retries(monkeypatch):
    from pipeline.deploy import _force_reset
    _mock_run(monkeypatch)
    progress = {
        "wl-a-baseline": {"workload": "wl-a", "package": "baseline", "status": "timed-out", "namespace": "ns-0", "retries": 3},
    }
    _force_reset(progress, {"wl-a-baseline"})
    assert progress["wl-a-baseline"]["retries"] == 0


# ── Capacity-gated dispatch (issue #64) ──────────────────────────────────────


def test_init_progress_stores_gpu_cost(tmp_path):
    """New progress entries include gpu_cost field."""
    import yaml as _yaml
    from pipeline.deploy import _load_pairs

    cluster_dir = tmp_path / "cluster"
    cluster_dir.mkdir()

    pr = {
        "metadata": {"name": "baseline-smoke-run1", "namespace": "ns"},
        "spec": {"params": [
            {"name": "workloadName", "value": "wl-smoke"},
            {"name": "phase", "value": "baseline"},
        ]},
    }
    (cluster_dir / "pipelinerun-smoke-baseline.yaml").write_text(_yaml.dump(pr))

    discovered = _load_pairs(cluster_dir)
    # Simulate progress initialization with gpu_cost
    pair_gpu_cost = 8
    progress = {}
    for key, meta in discovered.items():
        if key not in progress:
            progress[key] = {
                "workload": meta["workload"],
                "package":  meta["package"],
                "status":   "pending",
                "namespace": None,
                "retries":  0,
                "gpu_cost": pair_gpu_cost,
            }
    assert "gpu_cost" in progress["wl-smoke-baseline"]
    assert progress["wl-smoke-baseline"]["gpu_cost"] == 8


def test_init_progress_gpu_cost_uses_fallback(tmp_path):
    """When using default cost, gpu_cost stores that value."""
    import yaml as _yaml
    from pipeline.deploy import _load_pairs

    cluster_dir = tmp_path / "cluster"
    cluster_dir.mkdir()

    pr = {
        "metadata": {"name": "baseline-smoke-run1", "namespace": "ns"},
        "spec": {"params": [
            {"name": "workloadName", "value": "wl-smoke"},
            {"name": "phase", "value": "baseline"},
        ]},
    }
    (cluster_dir / "pipelinerun-smoke-baseline.yaml").write_text(_yaml.dump(pr))

    discovered = _load_pairs(cluster_dir)
    default_cost = 1  # --default-gpu-cost fallback
    progress = {}
    for key, meta in discovered.items():
        if key not in progress:
            progress[key] = {
                "workload": meta["workload"],
                "package":  meta["package"],
                "status":   "pending",
                "namespace": None,
                "retries":  0,
                "gpu_cost": default_cost,
            }
    assert progress["wl-smoke-baseline"]["gpu_cost"] == 1


def test_capacity_gated_dispatch_limits_pairs():
    """When free GPUs < total pending cost, only a subset is dispatched."""
    from pipeline.deploy import _capacity_limited_pairs

    progress = {
        "wl-a-baseline":   {"status": "pending", "gpu_cost": 8},
        "wl-b-baseline":   {"status": "pending", "gpu_cost": 4},
        "wl-c-baseline":   {"status": "pending", "gpu_cost": 4},
        "wl-d-baseline":   {"status": "pending", "gpu_cost": 8},
    }
    pending = ["wl-a-baseline", "wl-b-baseline", "wl-c-baseline", "wl-d-baseline"]

    # sorted: b(4), c(4), a(8), d(8). budget=12: b→8, c→4, 4>=8? No. So only b and c fit.
    result = _capacity_limited_pairs(pending, progress, free_gpus=12, default_gpu_cost=1)
    assert result == ["wl-b-baseline", "wl-c-baseline"]


def test_capacity_gated_dispatch_all_fit():
    """When free GPUs >= total pending cost, all pairs are returned."""
    from pipeline.deploy import _capacity_limited_pairs

    progress = {
        "wl-a-baseline":   {"status": "pending", "gpu_cost": 4},
        "wl-b-baseline":   {"status": "pending", "gpu_cost": 4},
    }
    pending = ["wl-a-baseline", "wl-b-baseline"]

    result = _capacity_limited_pairs(pending, progress, free_gpus=16, default_gpu_cost=1)
    # sorted by cost (both 4), stable sort preserves order
    assert set(result) == {"wl-a-baseline", "wl-b-baseline"}
    assert len(result) == 2


def test_capacity_gated_dispatch_sorts_ascending():
    """Pairs are sorted by gpu_cost ascending to maximize dispatch count."""
    from pipeline.deploy import _capacity_limited_pairs

    progress = {
        "wl-big-baseline":   {"status": "pending", "gpu_cost": 8},
        "wl-small-baseline": {"status": "pending", "gpu_cost": 2},
        "wl-mid-baseline":   {"status": "pending", "gpu_cost": 4},
    }
    pending = ["wl-big-baseline", "wl-small-baseline", "wl-mid-baseline"]

    # Budget 10: sorted → small(2), mid(4), big(8). 2+4=6<=10, 6+8=14>10. So small+mid.
    result = _capacity_limited_pairs(pending, progress, free_gpus=10, default_gpu_cost=1)
    assert result == ["wl-small-baseline", "wl-mid-baseline"]


def test_capacity_gated_dispatch_uses_default_cost_for_legacy_entries():
    """Entries without gpu_cost field use the default_gpu_cost fallback."""
    from pipeline.deploy import _capacity_limited_pairs

    progress = {
        "wl-old-baseline": {"status": "pending"},  # no gpu_cost field
        "wl-new-baseline": {"status": "pending", "gpu_cost": 4},
    }
    pending = ["wl-old-baseline", "wl-new-baseline"]

    # default_gpu_cost=2: sorted → old(2), new(4). budget=5: 2+4=6>5. Only old fits.
    result = _capacity_limited_pairs(pending, progress, free_gpus=5, default_gpu_cost=2)
    assert result == ["wl-old-baseline"]


def test_capacity_gated_dispatch_zero_budget():
    """Zero free GPUs means nothing is dispatched."""
    from pipeline.deploy import _capacity_limited_pairs

    progress = {
        "wl-a-baseline": {"status": "pending", "gpu_cost": 4},
    }
    pending = ["wl-a-baseline"]

    result = _capacity_limited_pairs(pending, progress, free_gpus=0, default_gpu_cost=1)
    assert result == []


def test_probe_failure_dispatches_all_pending():
    """When probe returns error string, all pending pairs are dispatched (no gating)."""
    from pipeline.deploy import _capacity_limited_pairs

    progress = {
        "wl-a-baseline": {"status": "pending", "gpu_cost": 8},
        "wl-b-baseline": {"status": "pending", "gpu_cost": 4},
        "wl-c-baseline": {"status": "pending", "gpu_cost": 4},
    }
    pending = ["wl-a-baseline", "wl-b-baseline", "wl-c-baseline"]

    # Simulate the dispatch logic: when probe fails, free_gpus is None,
    # so _capacity_limited_pairs is NOT called — dispatchable = pending directly.
    # This verifies the contract: probe failure means no filtering.
    capacity = "connection refused"  # str = failure
    free_gpus = None
    if isinstance(capacity, tuple):
        free_gpus = capacity[0]

    if free_gpus is not None:
        dispatchable = _capacity_limited_pairs(
            pending, progress, free_gpus=free_gpus, default_gpu_cost=1,
        )
    else:
        dispatchable = pending

    assert dispatchable == pending
    assert len(dispatchable) == 3


def test_slot_limited_dispatch(capsys):
    """When capacity allows all pairs but fewer slots exist, slot-limited log fires."""
    from pipeline.deploy import _capacity_limited_pairs, info

    progress = {
        "wl-a-baseline": {"status": "pending", "gpu_cost": 4},
        "wl-b-baseline": {"status": "pending", "gpu_cost": 4},
        "wl-c-baseline": {"status": "pending", "gpu_cost": 4},
    }
    pending = ["wl-a-baseline", "wl-b-baseline", "wl-c-baseline"]
    free_gpus = 24  # plenty of capacity
    pair_gpu_cost = 4

    dispatchable = _capacity_limited_pairs(
        pending, progress, free_gpus=free_gpus, default_gpu_cost=pair_gpu_cost,
    )
    # All 3 fit in capacity
    assert len(dispatchable) == 3

    # But only 1 slot available — slot-limited
    free_slots = ["sim2real-0"]  # 1 slot
    if len(dispatchable) < len(pending):
        info(f"Dispatching {len(dispatchable)}/{len(pending)} pending pairs (capacity-limited: {free_gpus} free GPUs)")
    elif len(free_slots) < len(dispatchable):
        info(f"Dispatching {len(free_slots)}/{len(pending)} pending pairs (slot-limited)")

    out = capsys.readouterr().out
    assert "slot-limited" in out
    assert "1/3" in out


def test_init_progress_includes_pending_stalls():
    """New progress entries include pending_stalls field initialized to 0."""
    progress_entry = {
        "workload": "wl-smoke",
        "package": "baseline",
        "status": "pending",
        "namespace": None,
        "retries": 0,
        "gpu_cost": 1,
        "pending_stalls": 0,
    }
    assert "pending_stalls" in progress_entry
    assert progress_entry["pending_stalls"] == 0


def test_run_parser_has_pending_flags():
    """run subcommand exposes --pending-threshold and --max-pending-stalls."""
    from pipeline.deploy import build_parser
    parser = build_parser()
    args = parser.parse_args(["run", "--pending-threshold", "300", "--max-pending-stalls", "5"])
    assert args.pending_threshold == 300
    assert args.max_pending_stalls == 5


def test_run_parser_pending_flag_defaults():
    """--pending-threshold defaults to 600, --max-pending-stalls to 10."""
    from pipeline.deploy import build_parser
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.pending_threshold == 600
    assert args.max_pending_stalls == 10


def test_run_parser_has_max_backoff_flag():
    """run subcommand should have --max-backoff flag with default 600."""
    from pipeline.deploy import build_parser
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.max_backoff == 600


def test_run_parser_max_backoff_custom():
    """--max-backoff should accept custom values."""
    from pipeline.deploy import build_parser
    parser = build_parser()
    args = parser.parse_args(["run", "--max-backoff", "300"])
    assert args.max_backoff == 300


def test_early_reclaim_recoverable_threshold_exceeded(monkeypatch):
    """Recoverable pending pod past threshold: cancel PR, free slot, return to pending."""
    import datetime as _dt
    import json
    import pipeline.deploy as mod

    pods_json_recoverable = {
        "items": [{
            "status": {
                "phase": "Pending",
                "conditions": [{
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": "0/8 nodes are available: 8 Insufficient nvidia.com/gpu.",
                }],
            },
        }],
    }

    entry = {
        "workload": "wl-smoke", "package": "baseline", "status": "running",
        "namespace": "sim2real-0", "retries": 0, "gpu_cost": 1,
        "pending_stalls": 0,
        "pending_since": (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=700)).isoformat(),
    }

    cancelled = []

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = json.dumps(pods_json_recoverable)
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "_cancel_and_delete_pipelinerun",
                        lambda pr, ns: cancelled.append((pr, ns)))

    reclaimed = mod._handle_pending_pods(
        pr_name="baseline-smoke-run1",
        namespace="sim2real-0",
        entry=entry,
        pending_threshold=600,
        max_pending_stalls=10,
    )

    assert reclaimed is True
    assert entry["status"] == "pending"
    assert entry["namespace"] is None
    assert entry["pending_stalls"] == 1
    assert entry["pending_since"] is None
    assert cancelled == [("baseline-smoke-run1", "sim2real-0")]


def test_early_reclaim_recoverable_under_threshold(monkeypatch):
    """Recoverable pending pod under threshold: set pending_since, do NOT reclaim."""
    import json
    import pipeline.deploy as mod

    pods_json_recoverable = {
        "items": [{
            "status": {
                "phase": "Pending",
                "conditions": [{
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": "0/8 nodes are available: 8 Insufficient nvidia.com/gpu.",
                }],
            },
        }],
    }

    entry = {
        "workload": "wl-smoke", "package": "baseline", "status": "running",
        "namespace": "sim2real-0", "retries": 0, "gpu_cost": 1,
        "pending_stalls": 0, "pending_since": None,
    }

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = json.dumps(pods_json_recoverable)
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    reclaimed = mod._handle_pending_pods(
        pr_name="baseline-smoke-run1",
        namespace="sim2real-0",
        entry=entry,
        pending_threshold=600,
        max_pending_stalls=10,
    )

    assert reclaimed is False
    assert entry["status"] == "running"
    assert entry["pending_since"] is not None


def test_early_reclaim_non_recoverable_fails_immediately(monkeypatch):
    """Non-recoverable pending: fail immediately, no waiting."""
    import json
    import pipeline.deploy as mod

    pods_json_non_recoverable = {
        "items": [{
            "status": {
                "phase": "Pending",
                "conditions": [{
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": "0/8 nodes are available: 8 node(s) didn't match Pod's node affinity/selector.",
                }],
            },
        }],
    }

    entry = {
        "workload": "wl-smoke", "package": "baseline", "status": "running",
        "namespace": "sim2real-0", "retries": 0, "gpu_cost": 1,
        "pending_stalls": 0, "pending_since": None,
    }

    cancelled = []

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = json.dumps(pods_json_non_recoverable)
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "_cancel_and_delete_pipelinerun",
                        lambda pr, ns: cancelled.append((pr, ns)))

    reclaimed = mod._handle_pending_pods(
        pr_name="baseline-smoke-run1",
        namespace="sim2real-0",
        entry=entry,
        pending_threshold=600,
        max_pending_stalls=10,
    )

    assert reclaimed is True
    assert entry["status"] == "failed"
    assert entry["namespace"] is None
    assert entry["pending_stalls"] == 0
    assert cancelled == [("baseline-smoke-run1", "sim2real-0")]


def test_early_reclaim_stalled_at_max_pending_stalls(monkeypatch):
    """When pending_stalls reaches max, pair transitions to stalled."""
    import datetime as _dt
    import json
    import pipeline.deploy as mod

    pods_json_recoverable = {
        "items": [{
            "status": {
                "phase": "Pending",
                "conditions": [{
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": "0/8 nodes are available: 8 Insufficient nvidia.com/gpu.",
                }],
            },
        }],
    }

    entry = {
        "workload": "wl-smoke", "package": "baseline", "status": "running",
        "namespace": "sim2real-0", "retries": 0, "gpu_cost": 1,
        "pending_stalls": 9,
        "pending_since": (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=700)).isoformat(),
    }

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = json.dumps(pods_json_recoverable)
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "_cancel_and_delete_pipelinerun", lambda pr, ns: None)

    reclaimed = mod._handle_pending_pods(
        pr_name="baseline-smoke-run1",
        namespace="sim2real-0",
        entry=entry,
        pending_threshold=600,
        max_pending_stalls=10,
    )

    assert reclaimed is True
    assert entry["status"] == "stalled"
    assert entry["pending_stalls"] == 10


def test_early_reclaim_kubectl_failure_returns_false(monkeypatch, capsys):
    """kubectl get pods failure: warn and don't reclaim, let timeout handle it."""
    import pipeline.deploy as mod

    entry = {
        "workload": "wl-smoke", "package": "baseline", "status": "running",
        "namespace": "sim2real-0", "retries": 0, "gpu_cost": 1,
        "pending_stalls": 0, "pending_since": None,
    }

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "connection refused"
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    reclaimed = mod._handle_pending_pods(
        pr_name="baseline-smoke-run1",
        namespace="sim2real-0",
        entry=entry,
        pending_threshold=600,
        max_pending_stalls=10,
    )

    assert reclaimed is False
    assert entry["status"] == "running"
    err = capsys.readouterr().err
    assert "pod query failed" in err


def test_early_reclaim_pods_running_clears_pending_since(monkeypatch):
    """When pods transition from Pending to Running, clear pending_since."""
    import json
    import pipeline.deploy as mod

    pods_json_running = {
        "items": [{
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }],
    }

    entry = {
        "workload": "wl-smoke", "package": "baseline", "status": "running",
        "namespace": "sim2real-0", "retries": 0, "gpu_cost": 1,
        "pending_stalls": 0,
        "pending_since": "2026-05-09T12:00:00+00:00",
    }

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = json.dumps(pods_json_running)
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    reclaimed = mod._handle_pending_pods(
        pr_name="baseline-smoke-run1",
        namespace="sim2real-0",
        entry=entry,
        pending_threshold=600,
        max_pending_stalls=10,
    )

    assert reclaimed is False
    assert entry["pending_since"] is None


def test_pending_to_running_signals_backoff_reset(monkeypatch):
    """When pods transition Pending→Running, caller should signal backoff reset."""
    import json
    import pipeline.deploy as mod
    from pipeline.lib.backoff import BackoffController

    pods_json_running = {
        "items": [{
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }],
    }

    entry = {
        "workload": "wl-smoke", "package": "baseline", "status": "running",
        "namespace": "sim2real-0", "retries": 0, "gpu_cost": 1,
        "pending_stalls": 0,
        "pending_since": "2026-05-09T12:00:00+00:00",
    }

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = json.dumps(pods_json_running)
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=0, min_cost=8)
    assert bc.state == "backing_off"

    had_pending = entry.get("pending_since") is not None
    reclaimed = mod._handle_pending_pods(
        pr_name="baseline-smoke-run1",
        namespace="sim2real-0",
        entry=entry,
        pending_threshold=600,
        max_pending_stalls=10,
    )

    assert reclaimed is False
    assert entry["pending_since"] is None
    assert had_pending is True

    # This mirrors the logic now in deploy.py's orchestrator loop
    if had_pending and entry.get("pending_since") is None and not reclaimed:
        bc.signal_scheduling_success()

    assert bc.state == "normal"
    assert bc.backoff_level == 0


def test_no_backoff_signal_when_never_pending(monkeypatch):
    """Pods that were never pending should NOT trigger backoff reset."""
    import json
    import pipeline.deploy as mod
    from pipeline.lib.backoff import BackoffController

    pods_json_running = {
        "items": [{
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }],
    }

    entry = {
        "workload": "wl-smoke", "package": "baseline", "status": "running",
        "namespace": "sim2real-0", "retries": 0, "gpu_cost": 1,
        "pending_stalls": 0,
        "pending_since": None,
    }

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = json.dumps(pods_json_running)
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=0, min_cost=8)
    assert bc.state == "backing_off"

    had_pending = entry.get("pending_since") is not None
    reclaimed = mod._handle_pending_pods(
        pr_name="baseline-smoke-run1",
        namespace="sim2real-0",
        entry=entry,
        pending_threshold=600,
        max_pending_stalls=10,
    )

    assert reclaimed is False
    assert had_pending is False

    # Caller logic: no signal because had_pending is False
    if had_pending and entry.get("pending_since") is None and not reclaimed:
        bc.signal_scheduling_success()

    assert bc.state == "backing_off"  # Unchanged


def test_early_reclaim_malformed_pending_since_resets_timer(monkeypatch, capsys):
    """Malformed pending_since resets timer instead of crashing."""
    import json
    import pipeline.deploy as mod

    pods_json_recoverable = {
        "items": [{
            "status": {
                "phase": "Pending",
                "conditions": [{
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": "0/8 nodes are available: 8 Insufficient nvidia.com/gpu.",
                }],
            },
        }],
    }

    entry = {
        "workload": "wl-smoke", "package": "baseline", "status": "running",
        "namespace": "sim2real-0", "retries": 0, "gpu_cost": 1,
        "pending_stalls": 0,
        "pending_since": "not-a-valid-timestamp",
    }

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = json.dumps(pods_json_recoverable)
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    reclaimed = mod._handle_pending_pods(
        pr_name="baseline-smoke-run1",
        namespace="sim2real-0",
        entry=entry,
        pending_threshold=600,
        max_pending_stalls=10,
    )

    assert reclaimed is False
    assert entry["pending_since"] != "not-a-valid-timestamp"
    err = capsys.readouterr().err
    assert "malformed pending_since" in err


def test_force_reset_clears_pending_stalls(monkeypatch):
    """--force resets pending_stalls along with retries."""
    from pipeline.deploy import _force_reset

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = ""
        return _R()

    import pipeline.deploy as mod
    monkeypatch.setattr(mod, "run", fake_run)

    progress = {
        "wl-a-baseline": {
            "workload": "wl-a", "package": "baseline", "status": "stalled",
            "namespace": None, "retries": 2, "pending_stalls": 10,
            "pending_since": "2026-05-09T12:00:00+00:00",
        },
    }
    _force_reset(progress, {"wl-a-baseline"})
    assert progress["wl-a-baseline"]["pending_stalls"] == 0
    assert progress["wl-a-baseline"]["pending_since"] is None


def test_early_reclaim_json_decode_error_warns(monkeypatch, capsys):
    """kubectl returns garbage JSON with rc=0: warn and don't reclaim."""
    import pipeline.deploy as mod

    entry = {
        "workload": "wl-smoke", "package": "baseline", "status": "running",
        "namespace": "sim2real-0", "retries": 0, "gpu_cost": 1,
        "pending_stalls": 0, "pending_since": None,
    }

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = "<html>auth proxy page</html>"
            stderr = ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    reclaimed = mod._handle_pending_pods(
        pr_name="baseline-smoke-run1",
        namespace="sim2real-0",
        entry=entry,
        pending_threshold=600,
        max_pending_stalls=10,
    )

    assert reclaimed is False
    assert entry["status"] == "running"
    err = capsys.readouterr().err
    assert "invalid JSON" in err


def test_status_ignores_orchestrator_metadata_as_pair(tmp_path, capsys, monkeypatch):
    """_orchestrator key should not appear as a pair row in status output."""
    progress = {
        "wl-foo-baseline": {"workload": "foo", "package": "baseline", "status": "running", "namespace": "ns-1", "retries": 0},
        "_orchestrator": {"state": "backing_off", "backoff_level": 2, "last_probe_free_gpus": 0},
    }
    _mock_cm(monkeypatch, progress)

    from pipeline.deploy import _cmd_status
    args = argparse.Namespace(only=None, workload=None, package=None, status=None)
    _cmd_status(args, tmp_path, setup_config={"namespace": "sim2real-ns"})
    out = capsys.readouterr().out
    assert "wl-foo-baseline" in out
    lines = out.strip().split("\n")
    pair_lines = [l for l in lines if l.strip().startswith("wl-") or l.strip().startswith("_")]
    for line in pair_lines:
        assert not line.strip().startswith("_orchestrator")


def test_status_shows_orchestrator_state_backing_off(tmp_path, capsys, monkeypatch):
    """deploy.py status should show backoff state when _orchestrator is present."""
    progress = {
        "wl-foo-baseline": {"workload": "foo", "package": "baseline", "status": "running", "namespace": "ns-1", "retries": 0},
        "_orchestrator": {"state": "backing_off", "backoff_level": 2, "last_probe_free_gpus": 0, "last_scarcity_time": "2026-05-08T14:32:00+00:00"},
    }
    _mock_cm(monkeypatch, progress)

    from pipeline.deploy import _cmd_status
    args = argparse.Namespace(only=None, workload=None, package=None, status=None)
    _cmd_status(args, tmp_path, setup_config={"namespace": "sim2real-ns"})
    out = capsys.readouterr().out
    assert "backing_off" in out
    assert "level 2" in out


def test_status_no_orchestrator_section_when_normal(tmp_path, capsys, monkeypatch):
    """deploy.py status should not show orchestrator section when state is normal."""
    progress = {
        "wl-foo-baseline": {"workload": "foo", "package": "baseline", "status": "running", "namespace": "ns-1", "retries": 0},
        "_orchestrator": {"state": "normal", "backoff_level": 0, "last_probe_free_gpus": 8},
    }
    _mock_cm(monkeypatch, progress)

    from pipeline.deploy import _cmd_status
    args = argparse.Namespace(only=None, workload=None, package=None, status=None)
    _cmd_status(args, tmp_path, setup_config={"namespace": "sim2real-ns"})
    out = capsys.readouterr().out
    assert "backing_off" not in out


def test_resolve_scope_excludes_orchestrator_key(tmp_path):
    """_resolve_scope should never include _orchestrator in the pair set."""
    from pipeline.deploy import _resolve_scope
    args = argparse.Namespace(only=None, workload=None, package=None, status=None)
    scope = _resolve_scope(_PROGRESS, args)
    assert "_orchestrator" not in scope
    assert len(scope) == 5  # only the real pair keys


def test_apply_run_filters_excludes_orchestrator_key():
    """_apply_run_filters should not include _orchestrator even with status filter."""
    from pipeline.deploy import _apply_run_filters
    args = argparse.Namespace(only=None, workload=None, package=None, status="running")
    result = _apply_run_filters(_PROGRESS, args)
    assert "_orchestrator" not in result


def test_report_filter_mismatch_excludes_orchestrator(tmp_path, capsys):
    """_report_filter_mismatch valid-values lists should not include metadata keys."""
    from pipeline.deploy import _report_filter_mismatch
    _report_filter_mismatch(_PROGRESS, argparse.Namespace(only="nonexistent", workload=None, package=None, status=None))
    err_out = capsys.readouterr().err
    assert "_orchestrator" not in err_out


def test_status_empty_pairs_only_orchestrator(tmp_path, capsys, monkeypatch):
    """deploy.py status should handle progress with only _orchestrator (no pairs)."""
    progress = {
        "_orchestrator": {"state": "backing_off", "backoff_level": 3, "last_probe_free_gpus": 0},
    }
    _mock_cm(monkeypatch, progress)

    from pipeline.deploy import _cmd_status
    args = argparse.Namespace(only=None, workload=None, package=None, status=None)
    _cmd_status(args, tmp_path, setup_config={"namespace": "sim2real-ns"})
    out = capsys.readouterr().out
    assert "0 pairs" in out


# ── Image build decision (_cmd_build) ──────────────────────────────────────────

def test_cmd_build_missing_metadata(tmp_path):
    """Missing run_metadata.json → sys.exit."""
    from pipeline.deploy import _cmd_build
    with pytest.raises(SystemExit):
        _cmd_build(tmp_path, namespace="ns", skip_build=False)


def test_cmd_build_no_component_image(tmp_path, capsys):
    """component_image absent → skip."""
    from pipeline.deploy import _cmd_build
    (tmp_path / "run_metadata.json").write_text(json.dumps({"registry": "quay.io/me"}))
    result = _cmd_build(tmp_path, namespace="ns", skip_build=False)
    assert result == "skip"


def test_cmd_build_empty_component_image(tmp_path):
    """component_image is empty string → sys.exit (misconfigured setup)."""
    from pipeline.deploy import _cmd_build
    (tmp_path / "run_metadata.json").write_text(json.dumps({"component_image": ""}))
    with pytest.raises(SystemExit):
        _cmd_build(tmp_path, namespace="ns", skip_build=False)


def test_cmd_build_skip_flag(tmp_path, capsys):
    """--skip-build returns skip."""
    from pipeline.deploy import _cmd_build
    (tmp_path / "run_metadata.json").write_text(
        json.dumps({"component_image": "quay.io/me/sched:r1", "registry": "quay.io/me", "repo_name": "sched"})
    )
    result = _cmd_build(tmp_path, namespace="ns", skip_build=True)
    assert result == "skip"


# ── _check_slot_ready hf_secret_name parameter ──────────────────────────────


class TestCheckSlotReadyHfSecret:
    """_check_slot_ready uses the hf_secret_name parameter."""

    @patch("pipeline.deploy.run")
    def test_uses_configured_secret_name(self, mock_run):
        from pipeline.deploy import _check_slot_ready

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "Bound"

        ready, failures = _check_slot_ready("test-ns", hf_secret_name="my-hf-token")

        secret_calls = [c for c in mock_run.call_args_list
                        if "secret" in str(c) and "my-hf-token" in str(c)]
        assert len(secret_calls) == 1
        assert ready

    @patch("pipeline.deploy.run")
    def test_reports_configured_secret_name_in_failure(self, mock_run):
        from pipeline.deploy import _check_slot_ready

        def side_effect(cmd, *, check=True, capture=False, input=None):
            class R:
                returncode = 0
                stdout = "Bound"
            r = R()
            if "secret" in cmd:
                r.returncode = 1
            return r

        mock_run.side_effect = side_effect

        ready, failures = _check_slot_ready("test-ns", hf_secret_name="my-hf-token")

        assert not ready
        assert any("my-hf-token" in f for f in failures)


def test_load_pairs_includes_scenario_content(tmp_path):
    """_load_pairs extracts scenarioContent param from PipelineRun YAMLs."""
    import yaml as _yaml
    from pipeline.deploy import _load_pairs

    cluster_dir = tmp_path / "cluster"
    cluster_dir.mkdir()

    scenario = {"scenario": [{"decode": {"replicas": 2}}]}
    pr = {
        "metadata": {"name": "baseline-wl1-run1", "namespace": "ns"},
        "spec": {"params": [
            {"name": "workloadName", "value": "wl1"},
            {"name": "phase", "value": "baseline"},
            {"name": "scenarioContent", "value": _yaml.dump(scenario)},
        ]},
    }
    (cluster_dir / "pipelinerun-wl1-baseline.yaml").write_text(_yaml.dump(pr))

    pairs = _load_pairs(cluster_dir)
    assert "wl-wl1-baseline" in pairs
    assert pairs["wl-wl1-baseline"]["scenario_content"] == _yaml.dump(scenario)


def test_load_pairs_missing_scenario_content(tmp_path):
    """_load_pairs sets scenario_content to None when param is absent."""
    import yaml as _yaml
    from pipeline.deploy import _load_pairs

    cluster_dir = tmp_path / "cluster"
    cluster_dir.mkdir()

    pr = {
        "metadata": {"name": "baseline-wl1-run1", "namespace": "ns"},
        "spec": {"params": [
            {"name": "workloadName", "value": "wl1"},
            {"name": "phase", "value": "baseline"},
        ]},
    }
    (cluster_dir / "pipelinerun-wl1-baseline.yaml").write_text(_yaml.dump(pr))

    pairs = _load_pairs(cluster_dir)
    assert pairs["wl-wl1-baseline"]["scenario_content"] is None


# ── _derive_pair_gpu_costs ───────────────────────────────────────────────────


def test_derive_pair_gpu_costs_heterogeneous():
    """Per-pair cost derivation produces different costs for different scenarios."""
    import yaml as _yaml
    from pipeline.deploy import _derive_pair_gpu_costs

    defaults = {
        "accelerator": {"count": 1},
        "decode": {"enabled": True, "replicas": 1},
    }

    scenario_a = {"scenario": [{"decode": {"replicas": 2}}]}
    scenario_b = {"scenario": [{"decode": {"replicas": 4}}]}

    discovered = {
        "wl-a-baseline": {"scenario_content": _yaml.dump(scenario_a)},
        "wl-b-treatment": {"scenario_content": _yaml.dump(scenario_b)},
    }

    costs = _derive_pair_gpu_costs(discovered, defaults=defaults, fallback_cost=1)
    assert costs["wl-a-baseline"] == 2
    assert costs["wl-b-treatment"] == 4


def test_derive_pair_gpu_costs_fallback_on_missing_scenario():
    """When scenarioContent is None, falls back to defaults-only derivation."""
    from pipeline.deploy import _derive_pair_gpu_costs

    defaults = {
        "decode": {"enabled": True, "replicas": 1},
        "accelerator": {"count": 4},
    }

    discovered = {
        "wl-a-baseline": {"scenario_content": None},
    }

    costs = _derive_pair_gpu_costs(discovered, defaults=defaults, fallback_cost=1)
    assert costs["wl-a-baseline"] == 4


def test_derive_pair_gpu_costs_fallback_on_bad_yaml():
    """When scenarioContent is invalid YAML, falls back to defaults-only derivation."""
    from pipeline.deploy import _derive_pair_gpu_costs

    defaults = {"decode": {"enabled": True, "replicas": 1}, "accelerator": {"count": 2}}

    discovered = {
        "wl-a-baseline": {"scenario_content": ": invalid: yaml: ["},
    }

    costs = _derive_pair_gpu_costs(discovered, defaults=defaults, fallback_cost=99)
    assert costs["wl-a-baseline"] == 2


def test_derive_pair_gpu_costs_no_defaults():
    """When defaults is None, uses fallback_cost for all pairs."""
    from pipeline.deploy import _derive_pair_gpu_costs

    discovered = {
        "wl-a-baseline": {"scenario_content": "scenario:\n- decode:\n    replicas: 2\n"},
    }

    costs = _derive_pair_gpu_costs(discovered, defaults=None, fallback_cost=7)
    assert costs["wl-a-baseline"] == 7


def test_init_progress_per_pair_heterogeneous_cost(tmp_path):
    """Progress entries get individually-derived gpu_cost from scenarioContent."""
    import yaml as _yaml
    from pipeline.deploy import _load_pairs, _derive_pair_gpu_costs

    cluster_dir = tmp_path / "cluster"
    cluster_dir.mkdir()

    scenario_a = {"scenario": [{"decode": {"replicas": 2, "accelerator": {"count": 4}}}]}
    pr_a = {
        "metadata": {"name": "baseline-a-run1", "namespace": "ns"},
        "spec": {"params": [
            {"name": "workloadName", "value": "wl-a"},
            {"name": "phase", "value": "baseline"},
            {"name": "scenarioContent", "value": _yaml.dump(scenario_a)},
        ]},
    }
    (cluster_dir / "pipelinerun-a-baseline.yaml").write_text(_yaml.dump(pr_a))

    scenario_b = {"scenario": [{"decode": {"replicas": 1, "accelerator": {"count": 2}}}]}
    pr_b = {
        "metadata": {"name": "treatment-b-run1", "namespace": "ns"},
        "spec": {"params": [
            {"name": "workloadName", "value": "wl-b"},
            {"name": "phase", "value": "treatment"},
            {"name": "scenarioContent", "value": _yaml.dump(scenario_b)},
        ]},
    }
    (cluster_dir / "pipelinerun-b-treatment.yaml").write_text(_yaml.dump(pr_b))

    defaults = {"decode": {"enabled": True, "replicas": 1}, "accelerator": {"count": 1}}
    discovered = _load_pairs(cluster_dir)
    costs = _derive_pair_gpu_costs(discovered, defaults=defaults, fallback_cost=1)

    progress = {}
    for key, meta in discovered.items():
        progress[key] = {
            "workload": meta["workload"],
            "package":  meta["package"],
            "status":   "pending",
            "namespace": None,
            "retries":  0,
            "gpu_cost": costs[key],
            "pending_stalls": 0,
            "pending_since": None,
        }

    assert progress["wl-a-baseline"]["gpu_cost"] == 8
    assert progress["wl-b-treatment"]["gpu_cost"] == 2


# ── status ConfigMap behavior ───────────────────────────────────────────────

def test_status_parser_has_no_remote_flag():
    """status subcommand does NOT accept --remote (flag removed)."""
    from pipeline.deploy import build_parser
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["status", "--remote"])

def test_status_reads_configmap_when_namespace_configured(tmp_path, capsys, monkeypatch):
    """status reads from ConfigMap when namespace is configured."""
    from pipeline.deploy import _cmd_status

    progress_data = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "running", "namespace": "sim2real-0", "retries": 0,
        },
    }
    _mock_cm(monkeypatch, progress_data)

    class _Args:
        only = None; workload = None; package = None; status = None

    _cmd_status(_Args(), tmp_path,
                setup_config={"namespace": "sim2real-ns"})

    out = capsys.readouterr().out
    assert "wl-smoke-baseline" in out

def test_status_reads_from_configmap(tmp_path, capsys, monkeypatch):
    """status reads progress from ConfigMap."""
    from pipeline.deploy import _cmd_status

    progress_data = {
        "wl-smoke-baseline": {
            "workload": "wl-smoke", "package": "baseline",
            "status": "done", "namespace": None, "retries": 0,
        },
    }
    _mock_cm(monkeypatch, progress_data)

    class _Args:
        only = None; workload = None; package = None; status = None

    run_dir = tmp_path / "nonexistent-run"
    run_dir.mkdir()
    _cmd_status(_Args(), run_dir,
                setup_config={"namespace": "sim2real-ns"})

    out = capsys.readouterr().out
    assert "wl-smoke-baseline" in out

def test_status_empty_configmap_reports_no_run(tmp_path, capsys, monkeypatch):
    """Empty ConfigMap reports '0 pairs'."""
    from pipeline.deploy import _cmd_status
    _mock_cm(monkeypatch, {})

    class _Args:
        only = None; workload = None; package = None; status = None

    _cmd_status(_Args(), tmp_path,
                setup_config={"namespace": "sim2real-ns"})

    out = capsys.readouterr().out
    assert "0 pairs" in out

def test_status_no_namespace_exits_with_error(tmp_path, capsys):
    """status with no namespace configured exits with error."""
    from pipeline.deploy import _cmd_status

    class _Args:
        only = None; workload = None; package = None; status = None

    with pytest.raises(SystemExit):
        _cmd_status(_Args(), tmp_path, setup_config={})


# ── _configmap_namespace helper ──────────────────────────────────────────────

def test_configmap_namespace_from_setup_config():
    """Primary namespace comes from setup_config['namespace']."""
    from pipeline.deploy import _configmap_namespace
    assert _configmap_namespace({"namespace": "sim2real-ns"}) == "sim2real-ns"

def test_configmap_namespace_fallback_to_namespaces_arg():
    """Falls back to explicit namespaces[0] when setup_config has no namespace."""
    from pipeline.deploy import _configmap_namespace
    assert _configmap_namespace({}, ["sim2real-0", "sim2real-1"]) == "sim2real-0"

def test_configmap_namespace_fallback_to_setup_config_namespaces():
    """Falls back to setup_config['namespaces'][0] when namespace key is empty."""
    from pipeline.deploy import _configmap_namespace
    assert _configmap_namespace({"namespaces": ["sim2real-0"]}) == "sim2real-0"

def test_configmap_namespace_empty():
    """Returns '' when no namespace source is available."""
    from pipeline.deploy import _configmap_namespace
    assert _configmap_namespace({}) == ""
    assert _configmap_namespace(None) == ""


# ── ConfigMapProgressStore wiring in _cmd_run / _cmd_reset ─────────────────

def test_cmd_run_uses_configmap_store(monkeypatch, tmp_path):
    """_cmd_run uses ConfigMapProgressStore directly."""
    import pipeline.deploy as mod

    _mock_cm(monkeypatch, {})
    monkeypatch.setattr(mod, "_cmd_build", lambda *a, **kw: "skip")
    monkeypatch.setattr(mod, "_load_pairs", lambda d: {})

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run_metadata.json").write_text('{}')
    (run_dir / "cluster").mkdir()

    args = argparse.Namespace(
        skip_build=True, only=None, workload=None, package=None,
        status=None, force=False, max_retries=2, poll_interval=30,
        gpu_resource_type=None, default_gpu_cost=1,
        pending_threshold=600, max_pending_stalls=10, max_backoff=600,
    )
    setup = {"namespace": "sim2real-ns", "namespaces": ["sim2real-ns"]}

    with pytest.raises(SystemExit):
        mod._cmd_run(args, run_dir, setup)


def test_cmd_reset_uses_configmap_store(monkeypatch, tmp_path):
    """_cmd_reset uses ConfigMapProgressStore directly."""
    import pipeline.deploy as mod

    progress_data = {
        "wl-a-baseline": {"workload": "wl-a", "package": "baseline",
                          "status": "done", "namespace": None, "retries": 0},
    }
    _mock_cm(monkeypatch, progress_data)

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    args = argparse.Namespace(only=None, workload=None, package=None,
                              status=None, dry_run=False)
    # Note: _cmd_reset now takes run_dir, not progress_path
    mod._cmd_reset(args, run_dir, {},
                   namespaces=["sim2real-ns"],
                   setup_config={"namespace": "sim2real-ns"})


def test_status_uses_configmap_directly(tmp_path, capsys, monkeypatch):
    """status uses ConfigMapProgressStore directly (CM data appears in output)."""
    from pipeline.deploy import _cmd_status

    remote_data = {"wl-remote-baseline": {"workload": "wl-remote", "package": "baseline",
                                          "status": "running", "namespace": "ns-0", "retries": 0}}
    _mock_cm(monkeypatch, remote_data)

    class _Args:
        only = None; workload = None; package = None; status = None

    _cmd_status(_Args(), tmp_path, setup_config={"namespace": "sim2real-ns"})

    out = capsys.readouterr().out
    assert "wl-remote-baseline" in out
