"""Tests for deploy.py stop subcommand."""

from unittest.mock import patch

import pytest

import pipeline.deploy as mod


def _fake_run_ok(cmd, *, check=True, capture=False, cwd=None):
    class _R:
        returncode = 0
        stdout = ""
        stderr = ""
    return _R()


def test_stop_deletes_orchestrator_job(monkeypatch, capsys):
    """When the orchestrator Job exists, stop deletes it."""
    calls = []

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        calls.append(cmd)
        return _fake_run_ok(cmd)

    monkeypatch.setattr(mod, "run", fake_run)

    mod._cmd_stop(namespace="sim2real-dev")

    assert len(calls) == 2
    assert calls[0] == [
        "kubectl", "get", "job", "sim2real-orchestrator",
        "-n", "sim2real-dev",
    ]
    assert calls[1] == [
        "kubectl", "delete", "job", "sim2real-orchestrator",
        "-n", "sim2real-dev", "--cascade=foreground",
    ]
    out = capsys.readouterr().out
    assert "sim2real-orchestrator" in out
    assert "sim2real-dev" in out


def test_stop_no_job_prints_message(monkeypatch, capsys):
    """When no orchestrator Job exists, print info message and return."""
    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 1
            stdout = ""
            stderr = 'Error from server (NotFound): jobs.batch "sim2real-orchestrator" not found\n'
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    mod._cmd_stop(namespace="sim2real-dev")

    out = capsys.readouterr().out
    assert "no remote orchestrator started" in out.lower()


def test_stop_kubectl_get_error_exits(monkeypatch, capsys):
    """Non-NotFound kubectl get errors exit with code 1."""
    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "error: You must be logged in to the server"
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        mod._cmd_stop(namespace="sim2real-dev")
    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "logged in" in captured.err.lower()


def test_stop_kubectl_delete_error_exits(monkeypatch, capsys):
    """When Job exists but delete fails, exit with code 1."""
    call_count = [0]

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return _fake_run_ok(cmd)
        class _R:
            returncode = 1
            stdout = ""
            stderr = "error: forbidden"
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        mod._cmd_stop(namespace="sim2real-dev")
    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "forbidden" in captured.err.lower()


def test_main_dispatches_stop(tmp_path, monkeypatch):
    """main() routes 'stop' to _cmd_stop with the primary namespace."""
    monkeypatch.setattr("sys.argv", [
        "deploy.py", "--experiment-root", str(tmp_path), "stop",
    ])
    monkeypatch.setattr(mod, "EXPERIMENT_ROOT", tmp_path)

    stop_calls = []

    def mock_stop(namespace):
        stop_calls.append(namespace)

    with patch.object(mod, "_cmd_stop", mock_stop):
        with patch.object(mod, "_load_setup_config", return_value={
            "current_run": "test-run",
            "namespace": "sim2real-0",
            "namespaces": ["sim2real-0", "sim2real-1"],
        }):
            mod.main()

    assert stop_calls == ["sim2real-0"]


def test_main_dispatches_stop_singular_namespace(tmp_path, monkeypatch):
    """main() falls back to singular 'namespace' key when 'namespaces' is absent."""
    monkeypatch.setattr("sys.argv", [
        "deploy.py", "--experiment-root", str(tmp_path), "stop",
    ])
    monkeypatch.setattr(mod, "EXPERIMENT_ROOT", tmp_path)

    stop_calls = []

    def mock_stop(namespace):
        stop_calls.append(namespace)

    with patch.object(mod, "_cmd_stop", mock_stop):
        with patch.object(mod, "_load_setup_config", return_value={
            "current_run": "test-run",
            "namespace": "sim2real-legacy",
        }):
            mod.main()

    assert stop_calls == ["sim2real-legacy"]


def test_main_stop_no_namespace_exits(tmp_path, monkeypatch):
    """stop exits with code 1 when no namespaces configured."""
    monkeypatch.setattr("sys.argv", [
        "deploy.py", "--experiment-root", str(tmp_path), "stop",
    ])
    monkeypatch.setattr(mod, "EXPERIMENT_ROOT", tmp_path)

    with patch.object(mod, "_load_setup_config", return_value={
        "current_run": "test-run",
    }):
        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 1


def test_main_stop_does_not_require_run_dir(tmp_path, monkeypatch):
    """stop works without a run directory or run name configured."""
    monkeypatch.setattr("sys.argv", [
        "deploy.py", "--experiment-root", str(tmp_path), "stop",
    ])
    monkeypatch.setattr(mod, "EXPERIMENT_ROOT", tmp_path)

    stop_calls = []

    def mock_stop(namespace):
        stop_calls.append(namespace)

    with patch.object(mod, "_cmd_stop", mock_stop):
        with patch.object(mod, "_load_setup_config", return_value={
            "namespace": "sim2real-0",
        }):
            mod.main()

    assert stop_calls == ["sim2real-0"]
