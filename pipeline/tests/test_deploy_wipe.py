"""Tests for deploy.py wipe subcommand."""

import json
from pathlib import Path

from pipeline.lib.progress import ConfigMapProgressStore

_PROGRESS = {
    "wl-smoke-baseline":   {"workload": "wl-smoke",  "package": "baseline",  "status": "done",      "namespace": "sim2real-0", "retries": 0, "pending_stalls": 0, "pending_since": None},
    "wl-smoke-treatment":  {"workload": "wl-smoke",  "package": "treatment", "status": "done",      "namespace": "sim2real-1", "retries": 0, "pending_stalls": 0, "pending_since": None},
    "wl-load-baseline":    {"workload": "wl-load",   "package": "baseline",  "status": "pending",   "namespace": None,         "retries": 0, "pending_stalls": 0, "pending_since": None},
    "wl-load-treatment":   {"workload": "wl-load",   "package": "treatment", "status": "failed",    "namespace": "sim2real-2", "retries": 1, "pending_stalls": 2, "pending_since": "2026-05-14T10:00:00Z"},
    "wl-heavy-baseline":   {"workload": "wl-heavy",  "package": "baseline",  "status": "timed-out", "namespace": "sim2real-0", "retries": 1, "pending_stalls": 3, "pending_since": "2026-05-14T11:00:00Z"},
}


def _mock_cm(monkeypatch, data):
    """Monkeypatch ConfigMapProgressStore to return a deep copy of *data* on load."""
    monkeypatch.setattr(ConfigMapProgressStore, "load",
                        lambda self: json.loads(json.dumps(data)))
    monkeypatch.setattr(ConfigMapProgressStore, "save", lambda self, d: None)


def _setup_results(run_dir: Path) -> None:
    """Create a realistic results directory tree under run_dir."""
    for pkg in ("baseline", "treatment"):
        for wl in ("wl-smoke", "wl-load", "wl-heavy"):
            d = run_dir / "results" / pkg / wl
            d.mkdir(parents=True, exist_ok=True)
            (d / "trace_header.yaml").write_text("header")
            (d / "trace_data.csv").write_text("data")


def test_wipe_all_deletes_results(tmp_path, monkeypatch):
    """Unscoped wipe deletes results for all pairs regardless of status."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _mock_cm(monkeypatch, _PROGRESS)
    _setup_results(run_dir)

    class _Args:
        only = None; workload = None; package = None; dry_run = False; yes = True

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    # All pairs wiped regardless of status (including pending)
    assert not (run_dir / "results" / "baseline" / "wl-smoke").exists()
    assert not (run_dir / "results" / "baseline" / "wl-heavy").exists()
    assert not (run_dir / "results" / "baseline" / "wl-load").exists()
    assert not (run_dir / "results" / "treatment" / "wl-smoke").exists()
    assert not (run_dir / "results" / "treatment" / "wl-load").exists()


def test_wipe_scoped_by_workload(tmp_path, monkeypatch):
    """--workload scopes wipe to matching pairs only."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _mock_cm(monkeypatch, _PROGRESS)
    _setup_results(run_dir)

    class _Args:
        only = None; workload = "wl-smoke"; package = None; dry_run = False; yes = True

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    # Only wl-smoke directories deleted
    assert not (run_dir / "results" / "baseline" / "wl-smoke").exists()
    assert not (run_dir / "results" / "treatment" / "wl-smoke").exists()
    # Other workloads untouched
    assert (run_dir / "results" / "baseline" / "wl-load").exists()


def test_wipe_scoped_by_only(tmp_path, monkeypatch):
    """--only scopes wipe to a single pair."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _mock_cm(monkeypatch, _PROGRESS)
    _setup_results(run_dir)

    class _Args:
        only = "wl-heavy-baseline"; workload = None; package = None; dry_run = False; yes = True

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    # Only wl-heavy/baseline deleted
    assert not (run_dir / "results" / "baseline" / "wl-heavy").exists()
    assert (run_dir / "results" / "baseline" / "wl-smoke").exists()


def test_wipe_scoped_by_package(tmp_path, monkeypatch):
    """--package scopes wipe to pairs matching that package."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _mock_cm(monkeypatch, _PROGRESS)
    _setup_results(run_dir)

    class _Args:
        only = None; workload = None; package = "treatment"; dry_run = False; yes = True

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    # Only treatment directories deleted
    assert not (run_dir / "results" / "treatment" / "wl-smoke").exists()
    assert (run_dir / "results" / "baseline" / "wl-smoke").exists()


def test_wipe_dry_run_does_not_delete(tmp_path, monkeypatch, capsys):
    """--dry-run prints what would be deleted but does not mutate anything."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _mock_cm(monkeypatch, _PROGRESS)
    _setup_results(run_dir)

    class _Args:
        only = None; workload = None; package = None; dry_run = True; yes = False

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    # Nothing deleted
    assert (run_dir / "results" / "baseline" / "wl-smoke").exists()
    # DRY-RUN mentioned in output
    captured = capsys.readouterr()
    assert "DRY-RUN" in captured.out + captured.err


def test_wipe_includes_pending_pairs(tmp_path, monkeypatch):
    """Pending pairs with results on disk are wiped like any other pair."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    progress = {
        "wl-a-baseline": {"workload": "wl-a", "package": "baseline", "status": "pending",
                          "namespace": None, "retries": 0, "pending_stalls": 0, "pending_since": None},
    }
    _mock_cm(monkeypatch, progress)
    d = run_dir / "results" / "baseline" / "wl-a"
    d.mkdir(parents=True)
    (d / "trace.csv").write_text("data")

    class _Args:
        only = None; workload = None; package = None; dry_run = False; yes = True

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    assert not (run_dir / "results" / "baseline" / "wl-a").exists()


def test_wipe_no_progress_reports_nothing(tmp_path, monkeypatch, capsys):
    """When ConfigMap has no progress data, wipe reports nothing to do."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _mock_cm(monkeypatch, {})

    class _Args:
        only = None; workload = None; package = None; dry_run = False; yes = True

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    captured = capsys.readouterr()
    assert "nothing" in (captured.out + captured.err).lower()


def test_wipe_confirmation_abort(tmp_path, monkeypatch, capsys):
    """User declining confirmation aborts without changes."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _mock_cm(monkeypatch, _PROGRESS)
    _setup_results(run_dir)

    monkeypatch.setattr("builtins.input", lambda _: "n")

    class _Args:
        only = None; workload = None; package = None; dry_run = False; yes = False

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    # Nothing deleted
    assert (run_dir / "results" / "baseline" / "wl-smoke").exists()


def test_wipe_confirmation_accept(tmp_path, monkeypatch):
    """User confirming with 'y' proceeds with wipe."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _mock_cm(monkeypatch, _PROGRESS)
    _setup_results(run_dir)

    monkeypatch.setattr("builtins.input", lambda _: "y")

    class _Args:
        only = None; workload = None; package = None; dry_run = False; yes = False

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    assert not (run_dir / "results" / "baseline" / "wl-smoke").exists()


def test_wipe_eof_on_input_aborts(tmp_path, monkeypatch, capsys):
    """EOFError from input() (non-interactive) aborts without changes."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _mock_cm(monkeypatch, _PROGRESS)
    _setup_results(run_dir)

    def raise_eof(_):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    class _Args:
        only = None; workload = None; package = None; dry_run = False; yes = False

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    assert (run_dir / "results" / "baseline" / "wl-smoke").exists()
    captured = capsys.readouterr()
    assert "--yes" in captured.out + captured.err


def test_wipe_filter_mismatch_aborts(tmp_path, monkeypatch, capsys):
    """--only with non-existent pair aborts with error."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _mock_cm(monkeypatch, _PROGRESS)

    class _Args:
        only = "nonexistent"; workload = None; package = None; dry_run = False; yes = True

    with __import__("pytest").raises(SystemExit) as exc_info:
        mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})
    assert exc_info.value.code == 1


def test_wipe_cleans_empty_parent_dirs(tmp_path, monkeypatch):
    """After wiping all workloads under a package, the package dir is removed."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    progress = {
        "wl-only-baseline": {"workload": "wl-only", "package": "baseline", "status": "done",
                             "namespace": None, "retries": 0, "pending_stalls": 0, "pending_since": None},
    }
    _mock_cm(monkeypatch, progress)
    d = run_dir / "results" / "baseline" / "wl-only"
    d.mkdir(parents=True)
    (d / "trace.csv").write_text("data")

    class _Args:
        only = None; workload = None; package = None; dry_run = False; yes = True

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    assert not (run_dir / "results" / "baseline").exists()


def test_wipe_no_results_on_disk(tmp_path, monkeypatch, capsys):
    """Wipe reports skip when results directories don't exist on disk."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    progress = {
        "wl-a-baseline": {"workload": "wl-a", "package": "baseline", "status": "done",
                          "namespace": None, "retries": 0, "pending_stalls": 0, "pending_since": None},
    }
    _mock_cm(monkeypatch, progress)

    class _Args:
        only = None; workload = None; package = None; dry_run = False; yes = True

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    captured = capsys.readouterr()
    assert "no results on disk" in (captured.out + captured.err).lower()


def test_wipe_parent_not_removed_when_siblings_remain(tmp_path, monkeypatch):
    """Package dir is kept when out-of-scope workloads remain under it."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    progress = {
        "wl-a-baseline": {"workload": "wl-a", "package": "baseline", "status": "done",
                          "namespace": None, "retries": 0, "pending_stalls": 0, "pending_since": None},
        "wl-b-baseline": {"workload": "wl-b", "package": "baseline", "status": "done",
                          "namespace": None, "retries": 0, "pending_stalls": 0, "pending_since": None},
    }
    _mock_cm(monkeypatch, progress)
    for wl in ("wl-a", "wl-b"):
        d = run_dir / "results" / "baseline" / wl
        d.mkdir(parents=True)
        (d / "trace.csv").write_text("data")

    class _Args:
        only = "wl-a-baseline"; workload = None; package = None; dry_run = False; yes = True

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    # wl-a deleted, wl-b untouched, parent kept
    assert not (run_dir / "results" / "baseline" / "wl-a").exists()
    assert (run_dir / "results" / "baseline" / "wl-b").exists()
    assert (run_dir / "results" / "baseline").exists()


def test_wipe_rmtree_failure_skips_pair(tmp_path, monkeypatch, capsys):
    """When rmtree fails for one pair, that pair is skipped and exit code is 1."""
    import shutil
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    progress = {
        "wl-a-baseline": {"workload": "wl-a", "package": "baseline", "status": "done",
                          "namespace": None, "retries": 0, "pending_stalls": 0, "pending_since": None},
        "wl-b-baseline": {"workload": "wl-b", "package": "baseline", "status": "done",
                          "namespace": None, "retries": 0, "pending_stalls": 0, "pending_since": None},
    }
    _mock_cm(monkeypatch, progress)
    for wl in ("wl-a", "wl-b"):
        d = run_dir / "results" / "baseline" / wl
        d.mkdir(parents=True)
        (d / "trace.csv").write_text("data")

    orig_rmtree = shutil.rmtree

    def failing_rmtree(path, *a, **kw):
        if "wl-a" in str(path):
            raise OSError("permission denied")
        return orig_rmtree(path, *a, **kw)

    monkeypatch.setattr("shutil.rmtree", failing_rmtree)

    class _Args:
        only = None; workload = None; package = None; dry_run = False; yes = True

    with __import__("pytest").raises(SystemExit) as exc_info:
        mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})
    assert exc_info.value.code == 1

    # wl-a still on disk (failed), wl-b deleted
    assert (run_dir / "results" / "baseline" / "wl-a").exists()
    assert not (run_dir / "results" / "baseline" / "wl-b").exists()
    # Error count in summary
    captured = capsys.readouterr()
    assert "1 failed" in captured.out + captured.err


def test_wipe_warns_on_missing_package_workload(tmp_path, monkeypatch, capsys):
    """Entries missing package/workload fields emit a warning."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    progress = {
        "wl-broken": {"status": "done", "namespace": None, "retries": 0,
                      "pending_stalls": 0, "pending_since": None},
        "wl-ok-baseline": {"workload": "wl-ok", "package": "baseline", "status": "done",
                           "namespace": None, "retries": 0, "pending_stalls": 0, "pending_since": None},
    }
    _mock_cm(monkeypatch, progress)

    class _Args:
        only = None; workload = None; package = None; dry_run = False; yes = True

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    captured = capsys.readouterr()
    assert "missing package/workload" in (captured.out + captured.err).lower()


def test_wipe_does_not_save_progress(tmp_path, monkeypatch):
    """Wipe must never persist progress changes — it only deletes files."""
    import pipeline.deploy as mod

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    save_called = []
    monkeypatch.setattr(ConfigMapProgressStore, "load",
                        lambda self: json.loads(json.dumps(_PROGRESS)))
    monkeypatch.setattr(ConfigMapProgressStore, "save",
                        lambda self, d: save_called.append(True))
    _setup_results(run_dir)

    class _Args:
        only = None; workload = None; package = None; dry_run = False; yes = True

    mod._cmd_wipe(_Args(), run_dir, setup_config={"namespace": "ns-0"})

    assert save_called == [], "wipe must not call store.save()"
