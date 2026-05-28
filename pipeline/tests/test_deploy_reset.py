"""Tests for deploy.py reset subcommand and _reset_pair helper."""

from pipeline.lib.progress import ConfigMapProgressStore


_PROGRESS = {
    "wl-smoke-baseline":   {"workload": "wl-smoke",  "package": "baseline",  "status": "done",      "namespace": "sim2real-0", "retries": 0},
    "wl-smoke-treatment":  {"workload": "wl-smoke",  "package": "treatment", "status": "running",   "namespace": "sim2real-1", "retries": 0},
    "wl-load-baseline":    {"workload": "wl-load",   "package": "baseline",  "status": "pending",   "namespace": None,         "retries": 0},
    "wl-load-treatment":   {"workload": "wl-load",   "package": "treatment", "status": "timed-out", "namespace": "sim2real-2", "retries": 1},
    "wl-heavy-baseline":   {"workload": "wl-heavy",  "package": "baseline",  "status": "failed",    "namespace": "sim2real-0", "retries": 0},
}

_DISCOVERED = {
    "wl-smoke-baseline":  {"pr_name": "baseline-smoke-run1",  "workload": "wl-smoke", "package": "baseline"},
    "wl-smoke-treatment": {"pr_name": "treatment-smoke-run1", "workload": "wl-smoke", "package": "treatment"},
    "wl-load-baseline":   {"pr_name": "baseline-load-run1",   "workload": "wl-load",  "package": "baseline"},
    "wl-load-treatment":  {"pr_name": "treatment-load-run1",  "workload": "wl-load",  "package": "treatment"},
    "wl-heavy-baseline":  {"pr_name": "baseline-heavy-run1",  "workload": "wl-heavy", "package": "baseline"},
}


def _mock_cm(monkeypatch, data, capture_saves=None):
    """Monkeypatch ConfigMapProgressStore to return a deep copy of *data* on load.

    If *capture_saves* is a dict, saves are captured into it.
    Otherwise saves are no-ops.
    """
    import json as _json
    monkeypatch.setattr(ConfigMapProgressStore, "load",
                        lambda self: _json.loads(_json.dumps(data)))
    if capture_saves is not None:
        def _save(self, d):
            capture_saves.clear()
            capture_saves.update(d)
        monkeypatch.setattr(ConfigMapProgressStore, "save", _save)
    else:
        monkeypatch.setattr(ConfigMapProgressStore, "save", lambda self, d: None)


def test_reset_pair_failed_deletes_pr_and_helm(monkeypatch):
    """A failed pair gets its PipelineRun deleted and Helm releases uninstalled."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-heavy", "package": "baseline", "status": "failed",
             "namespace": "sim2real-0", "retries": 0}
    calls = []

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        calls.append(cmd)
        class _R:
            returncode = 0
            stdout = "release-a\nrelease-b\n"
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    mod._reset_pair("wl-heavy-baseline", entry, _DISCOVERED)

    assert entry["status"] == "pending"
    assert entry["namespace"] is None
    assert entry["retries"] == 0

    # Should have: kubectl delete pipelinerun, helm list, helm uninstall x2
    kubectl_deletes = [c for c in calls if c[:2] == ["kubectl", "delete"]]
    assert len(kubectl_deletes) == 1
    assert "baseline-heavy-run1" in kubectl_deletes[0]

    helm_lists = [c for c in calls if c[:2] == ["helm", "list"]]
    assert len(helm_lists) == 1

    helm_uninstalls = [c for c in calls if c[:2] == ["helm", "uninstall"]]
    assert len(helm_uninstalls) == 2


def test_reset_pair_running_cancels_first(monkeypatch):
    """A running pair gets cancelled before deletion."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-smoke", "package": "treatment", "status": "running",
             "namespace": "sim2real-1", "retries": 0}
    cancelled = []

    def fake_cancel(pr_name, ns):
        cancelled.append((pr_name, ns))

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = ""
        return _R()

    monkeypatch.setattr(mod, "_cancel_and_delete_pipelinerun", fake_cancel)
    monkeypatch.setattr(mod, "run", fake_run)

    mod._reset_pair("wl-smoke-treatment", entry, _DISCOVERED)

    assert cancelled == [("treatment-smoke-run1", "sim2real-1")]
    assert entry["status"] == "pending"
    assert entry["namespace"] is None


def test_reset_pair_none_namespace_resets_state(monkeypatch):
    """Pairs with no namespace still get reset (e.g. failed without namespace)."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-load", "package": "baseline", "status": "failed",
             "namespace": None, "retries": 2}

    result = mod._reset_pair("wl-load-baseline", entry, _DISCOVERED)
    assert result is True
    assert entry["status"] == "pending"
    assert entry["retries"] == 0


def test_reset_pair_kubectl_delete_failure_does_not_reset(monkeypatch):
    """When kubectl delete pipelinerun fails, state is NOT reset."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-heavy", "package": "baseline", "status": "failed",
             "namespace": "sim2real-0", "retries": 0}

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 1 if "pipelinerun" in cmd else 0
            stdout = ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    result = mod._reset_pair("wl-heavy-baseline", entry, _DISCOVERED)
    assert result is False
    assert entry["status"] == "failed"
    assert entry["namespace"] == "sim2real-0"


def test_reset_pair_helm_list_failure_does_not_reset(monkeypatch):
    """When helm list fails, state is NOT reset — operator needs manual intervention."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-heavy", "package": "baseline", "status": "failed",
             "namespace": "sim2real-0", "retries": 0}

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 1 if cmd[:2] == ["helm", "list"] else 0
            stdout = ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    result = mod._reset_pair("wl-heavy-baseline", entry, _DISCOVERED)
    assert result is False
    assert entry["status"] == "failed"
    assert entry["namespace"] == "sim2real-0"


def test_reset_pair_helm_uninstall_failure_does_not_reset(monkeypatch):
    """When helm uninstall fails, state is NOT reset."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-heavy", "package": "baseline", "status": "failed",
             "namespace": "sim2real-0", "retries": 0}

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 1 if cmd[:2] == ["helm", "uninstall"] else 0
            stdout = "stuck-release\n" if cmd[:2] == ["helm", "list"] else ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    result = mod._reset_pair("wl-heavy-baseline", entry, _DISCOVERED)
    assert result is False
    assert entry["status"] == "failed"
    assert entry["namespace"] == "sim2real-0"


def test_reset_pair_missing_pr_name_warns(monkeypatch, capsys):
    """When pr_name is not in discovered, a warning is emitted."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-unknown", "package": "baseline", "status": "failed",
             "namespace": "sim2real-0", "retries": 0}

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    result = mod._reset_pair("wl-unknown-baseline", entry, {})
    assert result is True
    assert entry["status"] == "pending"
    err = capsys.readouterr().err
    assert "no PipelineRun name found" in err


def test_cmd_reset_continues_on_exception(tmp_path, monkeypatch, capsys):
    """One pair raising an exception does not abort reset of remaining pairs."""
    import pipeline.deploy as mod

    progress = {
        "wl-a-baseline": {"workload": "wl-a", "package": "baseline", "status": "failed",
                          "namespace": "sim2real-0", "retries": 0},
        "wl-b-baseline": {"workload": "wl-b", "package": "baseline", "status": "failed",
                          "namespace": "sim2real-1", "retries": 0},
    }
    saved_data = {}
    _mock_cm(monkeypatch, progress, capture_saves=saved_data)

    call_count = []

    def exploding_reset(key, entry, disc, dry_run=False, namespaces=None, preserve_done_status=False):
        call_count.append(key)
        if key == "wl-a-baseline":
            raise RuntimeError("kubectl not found")
        entry["status"] = "pending"
        entry["namespace"] = None
        entry["retries"] = 0
        return True

    monkeypatch.setattr(mod, "_reset_pair", exploding_reset)

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    class _Args:
        only = None; workload = None; package = None; status = None; dry_run = False
        preserve_done_status = False

    mod._cmd_reset(_Args(), run_dir, _DISCOVERED,
                   setup_config={"namespace": "sim2real-ns"})

    # Both pairs should have been attempted
    assert "wl-a-baseline" in call_count
    assert "wl-b-baseline" in call_count
    # Progress should still be saved (wl-b was reset)
    assert saved_data["wl-b-baseline"]["status"] == "pending"


def test_reset_pair_done_resets_state_by_default(monkeypatch):
    """Done pairs get PipelineRun deleted AND state reset to pending."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-smoke", "package": "baseline", "status": "done",
             "namespace": None, "retries": 0}
    calls = []

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        calls.append(cmd)
        class _R:
            returncode = 0
            stdout = ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    result = mod._reset_pair("wl-smoke-baseline", entry, _DISCOVERED,
                             namespaces=["sim2real-0", "sim2real-1"])

    assert result is True
    # State reset to pending
    assert entry["status"] == "pending"
    assert entry["namespace"] is None
    # PipelineRun deleted across all namespace slots
    kubectl_deletes = [c for c in calls if "delete" in c and "pipelinerun" in c]
    assert len(kubectl_deletes) == 2


def test_reset_pair_done_preserves_status_with_flag(monkeypatch):
    """Done pairs with preserve_done_status=True get cleanup but stay done."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-smoke", "package": "baseline", "status": "done",
             "namespace": None, "retries": 0}
    calls = []

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        calls.append(cmd)
        class _R:
            returncode = 0
            stdout = ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    result = mod._reset_pair("wl-smoke-baseline", entry, _DISCOVERED,
                             namespaces=["sim2real-0", "sim2real-1"],
                             preserve_done_status=True)

    assert result is True
    # State stays done
    assert entry["status"] == "done"
    # PipelineRun still deleted
    kubectl_deletes = [c for c in calls if "delete" in c and "pipelinerun" in c]
    assert len(kubectl_deletes) == 2


def test_cmd_reset_skips_pending_only(tmp_path, monkeypatch, capsys):
    """reset acts on all non-pending pairs, including done."""
    import pipeline.deploy as mod

    _mock_cm(monkeypatch, _PROGRESS)

    cleaned = []
    monkeypatch.setattr(mod, "_reset_pair",
                        lambda k, e, d, dry_run=False, namespaces=None, preserve_done_status=False: cleaned.append(k) or True)

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    class _Args:
        only = None; workload = None; package = None; status = None; dry_run = False
        preserve_done_status = False

    mod._cmd_reset(_Args(), run_dir, _DISCOVERED,
                   setup_config={"namespace": "sim2real-ns"})

    # pending (wl-load-baseline) should be skipped
    assert "wl-load-baseline" not in cleaned
    # done, running, timed-out, failed should all be reset
    assert "wl-smoke-baseline" in cleaned
    assert "wl-smoke-treatment" in cleaned
    assert "wl-load-treatment" in cleaned
    assert "wl-heavy-baseline" in cleaned


def test_cmd_reset_respects_only_filter(tmp_path, monkeypatch, capsys):
    """--only scopes reset to a single pair."""
    import pipeline.deploy as mod

    _mock_cm(monkeypatch, _PROGRESS)

    cleaned = []
    monkeypatch.setattr(mod, "_reset_pair",
                        lambda k, e, d, dry_run=False, namespaces=None, preserve_done_status=False: cleaned.append(k) or True)

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    class _Args:
        only = "wl-heavy-baseline"; workload = None; package = None; status = None; dry_run = False
        preserve_done_status = False

    mod._cmd_reset(_Args(), run_dir, _DISCOVERED,
                   setup_config={"namespace": "sim2real-ns"})

    assert cleaned == ["wl-heavy-baseline"]


def test_cmd_reset_dry_run_does_not_save(tmp_path, monkeypatch, capsys):
    """--dry-run does not persist to ConfigMap."""
    import pipeline.deploy as mod

    saved_data = {}
    _mock_cm(monkeypatch, _PROGRESS, capture_saves=saved_data)

    monkeypatch.setattr(mod, "_reset_pair",
                        lambda k, e, d, dry_run=False, namespaces=None, preserve_done_status=False: True)

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    class _Args:
        only = None; workload = None; package = None; status = None; dry_run = True
        preserve_done_status = False

    mod._cmd_reset(_Args(), run_dir, _DISCOVERED,
                   setup_config={"namespace": "sim2real-ns"})

    # Progress should not be saved (capture_saves stays empty)
    assert saved_data == {}


def test_cmd_reset_saves_progress_on_success(tmp_path, monkeypatch, capsys):
    """After reset, progress is saved via ConfigMapProgressStore."""
    import pipeline.deploy as mod

    saved_data = {}
    _mock_cm(monkeypatch, _PROGRESS, capture_saves=saved_data)

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 0
            stdout = ""
        return _R()

    def fake_cancel(pr_name, ns):
        pass

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "_cancel_and_delete_pipelinerun", fake_cancel)

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    class _Args:
        only = None; workload = None; package = None; status = None; dry_run = False
        preserve_done_status = False

    mod._cmd_reset(_Args(), run_dir, _DISCOVERED,
                   namespaces=["sim2real-0", "sim2real-1", "sim2real-2"],
                   setup_config={"namespace": "sim2real-ns"})

    # All non-pending pairs reset to pending (including done)
    assert saved_data["wl-smoke-baseline"]["status"] == "pending"
    assert saved_data["wl-smoke-treatment"]["status"] == "pending"
    assert saved_data["wl-load-treatment"]["status"] == "pending"
    assert saved_data["wl-heavy-baseline"]["status"] == "pending"
    # Pending pair unchanged
    assert saved_data["wl-load-baseline"]["status"] == "pending"


def test_force_reset_resets_all_non_pending_including_done(monkeypatch):
    """_force_reset resets all non-pending pairs including done."""
    import pipeline.deploy as mod

    progress = {
        "wl-a-baseline": {"workload": "wl-a", "package": "baseline", "status": "failed",
                          "namespace": "sim2real-0", "retries": 0},
        "wl-a-treatment": {"workload": "wl-a", "package": "treatment", "status": "pending",
                           "namespace": None, "retries": 0},
        "wl-b-baseline": {"workload": "wl-b", "package": "baseline", "status": "timed-out",
                          "namespace": "sim2real-1", "retries": 2},
        "wl-c-baseline": {"workload": "wl-c", "package": "baseline", "status": "done",
                          "namespace": None, "retries": 0},
    }
    discovered = {
        "wl-a-baseline": {"pr_name": "baseline-a-run1", "workload": "wl-a", "package": "baseline"},
        "wl-a-treatment": {"pr_name": "treatment-a-run1", "workload": "wl-a", "package": "treatment"},
        "wl-b-baseline": {"pr_name": "baseline-b-run1", "workload": "wl-b", "package": "baseline"},
        "wl-c-baseline": {"pr_name": "baseline-c-run1", "workload": "wl-c", "package": "baseline"},
    }

    cleaned = []

    def fake_reset(key, entry, disc, dry_run=False, namespaces=None):
        cleaned.append(key)
        entry["status"] = "pending"
        entry["namespace"] = None
        entry["retries"] = 0
        return True

    monkeypatch.setattr(mod, "_reset_pair", fake_reset)

    scope = set(progress.keys())
    n = mod._force_reset(progress, scope, discovered,
                         namespaces=["sim2real-0", "sim2real-1"])

    # Only pending should be skipped
    assert "wl-a-treatment" not in cleaned
    # Failed, timed-out, AND done should all be reset
    assert "wl-a-baseline" in cleaned
    assert "wl-b-baseline" in cleaned
    assert "wl-c-baseline" in cleaned
    assert n == 3


def test_force_reset_skips_count_on_reset_failure(monkeypatch):
    """_force_reset does not count pairs where _reset_pair returned False."""
    import pipeline.deploy as mod

    progress = {
        "wl-a-baseline": {"workload": "wl-a", "package": "baseline", "status": "failed",
                          "namespace": "sim2real-0", "retries": 0},
    }
    discovered = {"wl-a-baseline": {"pr_name": "pr1", "workload": "wl-a", "package": "baseline"}}

    monkeypatch.setattr(mod, "_reset_pair", lambda *a, **kw: False)

    n = mod._force_reset(progress, {"wl-a-baseline"}, discovered)
    assert n == 0
    assert progress["wl-a-baseline"]["status"] == "failed"


def test_force_reset_continues_on_exception(monkeypatch):
    """_force_reset handles exceptions without aborting remaining pairs."""
    import pipeline.deploy as mod

    progress = {
        "wl-a-baseline": {"workload": "wl-a", "package": "baseline", "status": "failed",
                          "namespace": "sim2real-0", "retries": 0},
        "wl-b-baseline": {"workload": "wl-b", "package": "baseline", "status": "failed",
                          "namespace": "sim2real-1", "retries": 0},
    }
    discovered = {
        "wl-a-baseline": {"pr_name": "pr1", "workload": "wl-a", "package": "baseline"},
        "wl-b-baseline": {"pr_name": "pr2", "workload": "wl-b", "package": "baseline"},
    }

    call_count = []

    def sometimes_fails(key, entry, disc, dry_run=False, namespaces=None):
        call_count.append(key)
        if key == "wl-a-baseline":
            raise RuntimeError("network error")
        entry["status"] = "pending"
        entry["namespace"] = None
        entry["retries"] = 0
        return True

    monkeypatch.setattr(mod, "_reset_pair", sometimes_fails)

    n = mod._force_reset(progress, set(progress.keys()), discovered)

    assert len(call_count) == 2
    assert n == 1  # only wl-b succeeded
    assert progress["wl-a-baseline"]["status"] == "failed"  # unchanged
    assert progress["wl-b-baseline"]["status"] == "pending"


def test_cmd_reset_aborts_on_filter_mismatch(tmp_path, monkeypatch, capsys):
    """reset exits with error when --only doesn't match any pair."""
    import pipeline.deploy as mod

    _mock_cm(monkeypatch, _PROGRESS)

    run_dir = tmp_path / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    class _Args:
        only = "nonexistent"; workload = None; package = None; status = None; dry_run = False
        preserve_done_status = False

    with __import__("pytest").raises(SystemExit) as exc_info:
        mod._cmd_reset(_Args(), run_dir, _DISCOVERED,
                       setup_config={"namespace": "sim2real-ns"})
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "no match" in captured.out + captured.err


def test_reset_pair_done_uninstalls_helm_in_completed_namespace(monkeypatch):
    """Done pairs get orphaned helm releases uninstalled in completed_namespace."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-smoke", "package": "baseline", "status": "done",
             "namespace": None, "completed_namespace": "sim2real-0", "retries": 0}
    calls = []

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        calls.append(cmd)
        class _R:
            returncode = 0
            stdout = "orphaned-release\n" if cmd[:2] == ["helm", "list"] else ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    result = mod._reset_pair("wl-smoke-baseline", entry, _DISCOVERED,
                             namespaces=["sim2real-0", "sim2real-1"])

    assert result is True
    assert entry["status"] == "pending"

    helm_lists = [c for c in calls if c[:2] == ["helm", "list"]]
    assert len(helm_lists) == 1
    assert "-n" in helm_lists[0] and "sim2real-0" in helm_lists[0]

    helm_uninstalls = [c for c in calls if c[:2] == ["helm", "uninstall"]]
    assert len(helm_uninstalls) == 1
    assert "orphaned-release" in helm_uninstalls[0]


def test_reset_pair_done_no_completed_ns_skips_helm(monkeypatch):
    """Done pairs without completed_namespace skip helm cleanup gracefully."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-smoke", "package": "baseline", "status": "done",
             "namespace": None, "retries": 0}
    calls = []

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        calls.append(cmd)
        class _R:
            returncode = 0
            stdout = ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    result = mod._reset_pair("wl-smoke-baseline", entry, _DISCOVERED,
                             namespaces=["sim2real-0", "sim2real-1"])

    assert result is True
    helm_calls = [c for c in calls if c[0] == "helm"]
    assert helm_calls == []


def test_reset_pair_done_no_releases_is_noop(monkeypatch):
    """Done pairs with no helm releases found skip uninstall."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-smoke", "package": "baseline", "status": "done",
             "namespace": None, "completed_namespace": "sim2real-0", "retries": 0}
    calls = []

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        calls.append(cmd)
        class _R:
            returncode = 0
            stdout = ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    result = mod._reset_pair("wl-smoke-baseline", entry, _DISCOVERED,
                             namespaces=["sim2real-0", "sim2real-1"])

    assert result is True
    helm_lists = [c for c in calls if c[:2] == ["helm", "list"]]
    assert len(helm_lists) == 1
    helm_uninstalls = [c for c in calls if c[:2] == ["helm", "uninstall"]]
    assert helm_uninstalls == []


def test_reset_pair_done_helm_list_failure_warns(monkeypatch, capsys):
    """Done pair with helm list failure still returns True but warns."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-smoke", "package": "baseline", "status": "done",
             "namespace": None, "completed_namespace": "sim2real-0", "retries": 0}

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 1 if cmd[:2] == ["helm", "list"] else 0
            stdout = ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    result = mod._reset_pair("wl-smoke-baseline", entry, _DISCOVERED,
                             namespaces=["sim2real-0", "sim2real-1"])

    assert result is True
    assert entry["status"] == "pending"
    err = capsys.readouterr().err
    assert "helm list failed" in err


def test_reset_pair_done_helm_uninstall_failure_warns(monkeypatch, capsys):
    """Done pair with helm uninstall failure still returns True but warns."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-smoke", "package": "baseline", "status": "done",
             "namespace": None, "completed_namespace": "sim2real-0", "retries": 0}

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 1 if cmd[:2] == ["helm", "uninstall"] else 0
            stdout = "stuck-release\n" if cmd[:2] == ["helm", "list"] else ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    result = mod._reset_pair("wl-smoke-baseline", entry, _DISCOVERED,
                             namespaces=["sim2real-0", "sim2real-1"])

    assert result is True
    assert entry["status"] == "pending"
    err = capsys.readouterr().err
    assert "Failed to uninstall" in err


def test_reset_pair_done_no_pr_name_still_cleans_helm(monkeypatch):
    """Done pair with no pr_name in discovered still checks completed_namespace for orphans."""
    import pipeline.deploy as mod

    entry = {"workload": "wl-smoke", "package": "baseline", "status": "done",
             "namespace": None, "completed_namespace": "sim2real-0", "retries": 0}
    calls = []

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        calls.append(cmd)
        class _R:
            returncode = 0
            stdout = "orphaned-release\n" if cmd[:2] == ["helm", "list"] else ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    # Empty discovered — simulates PipelineRun already deleted
    result = mod._reset_pair("wl-smoke-baseline", entry, {})

    assert result is True
    helm_lists = [c for c in calls if c[:2] == ["helm", "list"]]
    assert len(helm_lists) == 1
    assert "sim2real-0" in helm_lists[0]
    helm_uninstalls = [c for c in calls if c[:2] == ["helm", "uninstall"]]
    assert len(helm_uninstalls) == 1
