# `deploy.py stop` Subcommand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `deploy.py stop` subcommand that deletes the `sim2real-orchestrator` Kubernetes Job (remote orchestrator) with cascading pod deletion.

**Architecture:** A new `_cmd_stop` function in `pipeline/deploy.py` uses `kubectl delete job sim2real-orchestrator` in the primary namespace (`namespaces[0]` from `setup_config.json`). It first checks whether the Job exists, then deletes it with `--cascade=foreground`. If no Job exists, it prints "no remote orchestrator started" and returns. It does NOT touch `progress.json` — pair state is left as-is.

**Tech Stack:** Python 3.10+, kubectl CLI, argparse (existing patterns in deploy.py)

**Namespace resolution:** `setup_config.json` contains `namespaces` (list of slot namespaces) and `namespace` (the primary/first namespace). The remote orchestrator Job lives in `namespaces[0]`, same as where `_cmd_run` builds the EPP image (`_build_epp_image(run_dir, run_dir.name, namespaces[0])`). The `stop` subcommand reads namespaces the same way: `namespaces = setup_config.get("namespaces") or [setup_config.get("namespace", "")]`.

---

### Task 1: Add `_cmd_stop` function and CLI wiring

**Files:**
- Modify: `pipeline/deploy.py:1515` (add `stop` parser after `run_p` block)
- Modify: `pipeline/deploy.py:1558-1581` (add `stop` dispatch in `main()`)
- Modify: `pipeline/deploy.py:1460-1470` (add `stop` to epilog examples)
- Test: `pipeline/tests/test_deploy_stop.py` (new file)

- [ ] **Step 1: Write the test — Job exists and is deleted**

Create `pipeline/tests/test_deploy_stop.py`:

```python
"""Tests for deploy.py stop subcommand."""

import pipeline.deploy as mod


def test_stop_deletes_orchestrator_job(monkeypatch, capsys):
    """When the orchestrator Job exists, stop deletes it."""
    calls = []

    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        calls.append(cmd)
        class _R:
            returncode = 0
            stdout = ""
        return _R()

    monkeypatch.setattr(mod, "run", fake_run)

    mod._cmd_stop(namespace="sim2real-dev")

    # Should check existence then delete
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
```

- [ ] **Step 2: Write the test — Job does not exist (no-op)**

Append to the same test file:

```python
import subprocess


def test_stop_no_job_prints_message(monkeypatch, capsys):
    """When no orchestrator Job exists, print message and return."""
    def fake_run(cmd, *, check=True, capture=False, cwd=None):
        if cmd[:2] == ["kubectl", "get"]:
            raise subprocess.CalledProcessError(1, cmd)
        raise AssertionError(f"Unexpected call: {cmd}")

    monkeypatch.setattr(mod, "run", fake_run)

    mod._cmd_stop(namespace="sim2real-dev")

    out = capsys.readouterr().out
    assert "no remote orchestrator started" in out.lower()
```

- [ ] **Step 3: Write the test — main() dispatches stop correctly**

Append to the same test file:

```python
import json
from unittest.mock import patch


def test_main_dispatches_stop(tmp_path, monkeypatch):
    """main() routes 'stop' to _cmd_stop with the primary namespace."""
    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    ws = tmp_path / "workspace"
    (ws / "setup_config.json").write_text(json.dumps({
        "current_run": "test-run",
        "namespace": "sim2real-0",
        "namespaces": ["sim2real-0", "sim2real-1"],
    }))

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
```

- [ ] **Step 4: Write the test — stop with no namespaces configured**

Append to the same test file:

```python
def test_main_stop_no_namespace_exits(tmp_path, monkeypatch):
    """stop exits with error when no namespaces configured."""
    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    run_dir.mkdir(parents=True)

    ws = tmp_path / "workspace"
    (ws / "setup_config.json").write_text(json.dumps({
        "current_run": "test-run",
    }))

    monkeypatch.setattr("sys.argv", [
        "deploy.py", "--experiment-root", str(tmp_path), "stop",
    ])
    monkeypatch.setattr(mod, "EXPERIMENT_ROOT", tmp_path)

    import pytest
    with patch.object(mod, "_load_setup_config", return_value={
        "current_run": "test-run",
    }):
        with pytest.raises(SystemExit):
            mod.main()
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `python -m pytest pipeline/tests/test_deploy_stop.py -v`
Expected: All 4 tests FAIL (function not defined / attribute error)

- [ ] **Step 6: Implement `_cmd_stop`**

Add to `pipeline/deploy.py` before the `# ── CLI ──` section (around line 1452):

```python
JOB_NAME = "sim2real-orchestrator"


def _cmd_stop(namespace: str) -> None:
    """Stop the remote orchestrator Job."""
    try:
        run(["kubectl", "get", "job", JOB_NAME, "-n", namespace],
            check=True, capture=True)
    except subprocess.CalledProcessError:
        info(f"No remote orchestrator started in {namespace}")
        return

    run(["kubectl", "delete", "job", JOB_NAME, "-n", namespace,
         "--cascade=foreground"])
    ok(f"Stopped {JOB_NAME} in {namespace}")
```

- [ ] **Step 7: Wire up CLI parser**

In `build_parser()`, after the `reset_p` block and before `pairs_p`, add:

```python
    sub.add_parser("stop", help="Stop the remote orchestrator Job")
```

- [ ] **Step 8: Wire up main() dispatch**

In `main()`, add a new `elif` branch before the `pairs` branch:

```python
    elif cmd == "stop":
        namespaces = [ns for ns in (setup_config.get("namespaces") or
                      [setup_config.get("namespace", "")]) if ns]
        if not namespaces:
            err("No namespaces configured. Run setup.py first.")
            sys.exit(1)
        _cmd_stop(namespace=namespaces[0])
```

Update the error message in the `else` branch:
```python
        err("No subcommand specified. Use: deploy.py run | status | collect | reset | stop | pairs")
```

- [ ] **Step 9: Add example to epilog**

In `build_parser()`, add to the epilog examples:

```
  python pipeline/deploy.py stop                         # Stop remote orchestrator Job
```

- [ ] **Step 10: Run tests to verify they pass**

Run: `python -m pytest pipeline/tests/test_deploy_stop.py -v`
Expected: All 4 tests PASS

- [ ] **Step 11: Run full test suite and lint**

Run: `ruff check pipeline/ --select F`
Run: `python -m pytest pipeline/ -v`
Expected: No lint errors, all tests pass

- [ ] **Step 12: Commit**

```bash
git add pipeline/deploy.py pipeline/tests/test_deploy_stop.py
git commit -m "Add deploy.py stop subcommand for remote orchestrator (#125)"
```

---

### Design Decisions

1. **Namespace = `namespaces[0]`**: The remote orchestrator Job (per #127) will live in the primary namespace — same slot where `_cmd_run` builds the EPP image. `stop` uses the same resolution pattern.

2. **No `progress.json` mutation**: The issue explicitly says "progress.json is left as-is." A subsequent `run` reconciles on startup. This keeps `stop` simple and stateless.

3. **`--cascade=foreground`**: Ensures the Job's pods are fully deleted before the command returns. The user gets a clean signal that the orchestrator is stopped.

4. **No `--namespace` flag on `stop`**: It reads from `setup_config.json` like every other subcommand. Adding a per-subcommand namespace override is out of scope.

5. **Message on no-op**: "No remote orchestrator started in {namespace}" — per user request, this wording avoids implying there's no orchestrator at all (a local one might be running).
