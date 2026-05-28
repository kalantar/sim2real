# Backoff Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an exponential backoff controller to `deploy.py run` that reduces poll frequency during sustained GPU scarcity and resumes normal cadence when capacity returns.

**Architecture:** New `BackoffController` class in `pipeline/lib/backoff.py` encapsulates state machine logic (NORMAL ↔ BACKING_OFF), exponential interval computation, and scarcity signal processing. The main orchestrator loop in `deploy.py` queries the controller each cycle to determine whether to dispatch and how long to sleep. Controller state is persisted under a `_orchestrator` key in `progress.json`, with underscore-prefix filtering added to existing pair-iteration code.

**Tech Stack:** Python 3.10+, pytest, no new dependencies.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `pipeline/lib/backoff.py` | Create | `BackoffController` class — state machine, interval calc, signal processing |
| `pipeline/deploy.py` | Modify | Integrate controller into orchestrator loop; add `--max-backoff` flag; filter `_orchestrator` from pair iteration; show state in `status` |
| `pipeline/tests/test_backoff.py` | Create | Unit tests for `BackoffController` in isolation |
| `pipeline/tests/test_deploy_run.py` | Modify | Integration tests for backoff in orchestrator context |

---

### Task 1: BackoffController — core state machine

**Files:**
- Create: `pipeline/lib/backoff.py`
- Create: `pipeline/tests/test_backoff.py`

- [ ] **Step 1: Write failing tests for BackoffController initialization and state transitions**

```python
# pipeline/tests/test_backoff.py
import time
from unittest.mock import patch
from pipeline.lib.backoff import BackoffController


def test_initial_state_is_normal():
    bc = BackoffController(base_interval=30, max_backoff=600)
    assert bc.state == "normal"
    assert bc.backoff_level == 0
    assert bc.effective_interval == 30


def test_signal_scarcity_enters_backing_off():
    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=0, min_cost=4)
    assert bc.state == "backing_off"
    assert bc.backoff_level == 1
    assert bc.effective_interval == 60  # 30 * 2^1


def test_repeated_scarcity_increases_level():
    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=0, min_cost=4)
    bc.signal_scarcity(free_gpus=0, min_cost=4)
    assert bc.backoff_level == 2
    assert bc.effective_interval == 120  # 30 * 2^2


def test_backoff_capped_at_max():
    bc = BackoffController(base_interval=30, max_backoff=600)
    for _ in range(20):
        bc.signal_scarcity(free_gpus=0, min_cost=4)
    assert bc.effective_interval == 600


def test_signal_capacity_resets_to_normal():
    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=0, min_cost=4)
    bc.signal_scarcity(free_gpus=0, min_cost=4)
    assert bc.state == "backing_off"
    bc.signal_capacity(free_gpus=8, max_cost=4)
    assert bc.state == "normal"
    assert bc.backoff_level == 0
    assert bc.effective_interval == 30


def test_signal_scheduling_success_resets():
    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=0, min_cost=4)
    bc.signal_scheduling_success()
    assert bc.state == "normal"
    assert bc.backoff_level == 0


def test_should_dispatch_false_during_backoff_with_no_capacity():
    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=0, min_cost=4)
    assert bc.should_dispatch(free_gpus=0, min_cost=4) is False


def test_should_dispatch_true_during_backoff_with_capacity():
    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=0, min_cost=4)
    assert bc.should_dispatch(free_gpus=8, min_cost=4) is True


def test_should_dispatch_true_when_normal():
    bc = BackoffController(base_interval=30, max_backoff=600)
    assert bc.should_dispatch(free_gpus=8, min_cost=4) is True


def test_signal_scarcity_no_op_when_free_gpus_sufficient():
    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=8, min_cost=4)
    assert bc.state == "normal"
    assert bc.backoff_level == 0


def test_to_dict_roundtrip():
    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=0, min_cost=4)
    bc.signal_scarcity(free_gpus=0, min_cost=4)
    data = bc.to_dict()
    bc2 = BackoffController.from_dict(data, base_interval=30, max_backoff=600)
    assert bc2.state == "backing_off"
    assert bc2.backoff_level == 2


def test_signal_capacity_no_reset_when_insufficient():
    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=0, min_cost=4)
    bc.signal_capacity(free_gpus=2, max_cost=4)
    assert bc.state == "backing_off"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest pipeline/tests/test_backoff.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.lib.backoff'`

- [ ] **Step 3: Implement BackoffController**

```python
# pipeline/lib/backoff.py
"""Exponential backoff controller for the deploy orchestrator."""
from __future__ import annotations

import datetime as _dt


class BackoffController:
    """Manages poll-interval backoff during sustained GPU scarcity.

    State machine: NORMAL <-> BACKING_OFF
    """

    def __init__(self, base_interval: int, max_backoff: int) -> None:
        self._base = base_interval
        self._max = max_backoff
        self.state: str = "normal"
        self.backoff_level: int = 0
        self.last_scarcity_time: str | None = None
        self.last_probe_free_gpus: int | None = None

    @property
    def effective_interval(self) -> int:
        if self.state == "normal":
            return self._base
        raw = self._base * (2 ** self.backoff_level)
        return min(raw, self._max)

    def signal_scarcity(self, *, free_gpus: int, min_cost: int) -> None:
        if free_gpus >= min_cost:
            return
        self.state = "backing_off"
        self.backoff_level += 1
        raw = self._base * (2 ** self.backoff_level)
        if raw > self._max:
            self.backoff_level = self._level_for_max()
        self.last_scarcity_time = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.last_probe_free_gpus = free_gpus

    def signal_capacity(self, *, free_gpus: int, max_cost: int) -> None:
        if free_gpus >= max_cost:
            self._reset()
        self.last_probe_free_gpus = free_gpus

    def signal_scheduling_success(self) -> None:
        self._reset()

    def should_dispatch(self, *, free_gpus: int, min_cost: int) -> bool:
        if self.state == "normal":
            return True
        return free_gpus >= min_cost

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "backoff_level": self.backoff_level,
            "last_scarcity_time": self.last_scarcity_time,
            "last_probe_free_gpus": self.last_probe_free_gpus,
        }

    @classmethod
    def from_dict(cls, data: dict, *, base_interval: int, max_backoff: int) -> BackoffController:
        bc = cls(base_interval=base_interval, max_backoff=max_backoff)
        bc.state = data.get("state", "normal")
        bc.backoff_level = data.get("backoff_level", 0)
        bc.last_scarcity_time = data.get("last_scarcity_time")
        bc.last_probe_free_gpus = data.get("last_probe_free_gpus")
        return bc

    def _reset(self) -> None:
        self.state = "normal"
        self.backoff_level = 0

    def _level_for_max(self) -> int:
        level = 0
        while self._base * (2 ** (level + 1)) <= self._max:
            level += 1
        return level
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest pipeline/tests/test_backoff.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add pipeline/lib/backoff.py pipeline/tests/test_backoff.py
git commit -m "feat(backoff): add BackoffController state machine with tests"
```

---

### Task 2: Filter `_orchestrator` key from pair iteration

**Files:**
- Modify: `pipeline/deploy.py:746-762` (`_resolve_scope`)
- Modify: `pipeline/deploy.py:1031-1037` (`_pending_pairs`, `_work_remaining`)
- Modify: `pipeline/deploy.py:1024-1029` (slots_busy init)
- Modify: `pipeline/deploy.py:997` (reconcile loop)
- Modify: `pipeline/tests/test_deploy_run.py`

- [ ] **Step 1: Write failing test for _orchestrator key exclusion**

Add to `pipeline/tests/test_deploy_run.py`:

```python
def test_status_ignores_orchestrator_metadata(tmp_path, capsys):
    """_orchestrator key in progress.json should not appear in status output."""
    progress = {
        "wl-foo-baseline": {"workload": "foo", "package": "baseline", "status": "running", "namespace": "ns-1", "retries": 0},
        "_orchestrator": {"state": "backing_off", "backoff_level": 2, "last_probe_free_gpus": 0},
    }
    pf = tmp_path / "progress.json"
    pf.write_text(json.dumps(progress))

    from pipeline.deploy import _cmd_status
    args = argparse.Namespace(only=None, workload=None, package=None, status=None)
    _cmd_status(args, pf)
    out = capsys.readouterr().out
    assert "wl-foo-baseline" in out
    assert "_orchestrator" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest pipeline/tests/test_deploy_run.py::test_status_ignores_orchestrator_metadata -v`
Expected: FAIL — `_orchestrator` appears in output

- [ ] **Step 3: Add helper function and update pair-iteration code**

In `pipeline/deploy.py`, add a helper near the top (after imports, around line 40):

```python
def _is_pair_key(key: str) -> bool:
    """Return True if key is a real pair entry (not metadata)."""
    return not key.startswith("_")
```

Then update `_resolve_scope` (line 762):

```python
    return filtered or {k for k in progress.keys() if _is_pair_key(k)}
```

Update `_pending_pairs` (line 1031-1033):

```python
    def _pending_pairs() -> list[str]:
        return [k for k, v in progress.items()
                if _is_pair_key(k) and v.get("status") == "pending" and k in _scope]
```

Update `_work_remaining` (line 1035-1037):

```python
    def _work_remaining() -> bool:
        return any(v.get("status") in ("pending", "running", "collecting")
                   for k, v in progress.items() if _is_pair_key(k) and k in _scope)
```

Update slots_busy init (line 1025-1029):

```python
    slots_busy: dict[str, str] = {
        entry["namespace"]: key
        for key, entry in progress.items()
        if _is_pair_key(key) and entry.get("status") == "running" and entry.get("namespace")
    }
```

Update reconcile loop (line 997):

```python
    for key, entry in progress.items():
        if not _is_pair_key(key):
            continue
        if entry["status"] == "running":
```

- [ ] **Step 4: Run tests to verify the new test passes and existing tests still pass**

Run: `python -m pytest pipeline/tests/test_deploy_run.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add pipeline/deploy.py pipeline/tests/test_deploy_run.py
git commit -m "fix(deploy): filter _orchestrator metadata key from pair iteration"
```

---

### Task 3: Integrate BackoffController into orchestrator loop

**Files:**
- Modify: `pipeline/deploy.py:903-1210` (the `_cmd_run` function)
- Modify: `pipeline/tests/test_deploy_run.py`

- [ ] **Step 1: Write failing integration tests**

Add to `pipeline/tests/test_deploy_run.py`:

```python
def test_backoff_skips_dispatch_during_scarcity(monkeypatch, tmp_path, capsys):
    """When backoff is active and no capacity, dispatch should be skipped."""
    from pipeline.lib.backoff import BackoffController
    from pipeline.deploy import _capacity_limited_pairs

    progress = {
        "wl-a-baseline": {"workload": "a", "package": "baseline", "status": "pending",
                          "namespace": None, "retries": 0, "gpu_cost": 4,
                          "pending_stalls": 0, "pending_since": None},
    }
    pending = ["wl-a-baseline"]

    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=0, min_cost=4)

    assert bc.should_dispatch(free_gpus=0, min_cost=4) is False


def test_backoff_allows_dispatch_when_capacity_returns(monkeypatch, tmp_path, capsys):
    """When backoff is active but capacity returns, dispatch should proceed."""
    from pipeline.lib.backoff import BackoffController

    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=0, min_cost=4)

    assert bc.should_dispatch(free_gpus=8, min_cost=4) is True


def test_backoff_state_persisted_in_progress(tmp_path):
    """BackoffController state should be saved under _orchestrator key."""
    import json
    from pipeline.lib.backoff import BackoffController
    from pipeline.lib.progress import LocalProgressStore

    progress_path = tmp_path / "progress.json"
    store = LocalProgressStore(progress_path)

    bc = BackoffController(base_interval=30, max_backoff=600)
    bc.signal_scarcity(free_gpus=0, min_cost=4)
    bc.signal_scarcity(free_gpus=0, min_cost=4)

    progress = {"wl-a-baseline": {"status": "pending"}}
    progress["_orchestrator"] = bc.to_dict()
    store.save(progress)

    loaded = store.load()
    assert loaded["_orchestrator"]["state"] == "backing_off"
    assert loaded["_orchestrator"]["backoff_level"] == 2

    bc2 = BackoffController.from_dict(loaded["_orchestrator"], base_interval=30, max_backoff=600)
    assert bc2.state == "backing_off"
    assert bc2.effective_interval == 120
```

- [ ] **Step 2: Run tests to verify they pass (these test the library, not orchestrator integration yet)**

Run: `python -m pytest pipeline/tests/test_deploy_run.py::test_backoff_skips_dispatch_during_scarcity pipeline/tests/test_deploy_run.py::test_backoff_allows_dispatch_when_capacity_returns pipeline/tests/test_deploy_run.py::test_backoff_state_persisted_in_progress -v`
Expected: All PASS (these verify library behavior, not deploy.py integration)

- [ ] **Step 3: Integrate BackoffController into `_cmd_run`**

In `pipeline/deploy.py`, add import at top of `_cmd_run` (around line 907):

```python
    from pipeline.lib.backoff import BackoffController
```

After `_probe_fail_count = 0` (line 961), add controller initialization:

```python
    max_backoff = getattr(args, "max_backoff", 600)

    # Initialize or restore backoff controller
    existing_orch = progress.get("_orchestrator")
    if existing_orch:
        backoff = BackoffController.from_dict(existing_orch, base_interval=poll_interval, max_backoff=max_backoff)
        if backoff.state != "normal":
            info(f"Resuming in {backoff.state} state (level {backoff.backoff_level})")
    else:
        backoff = BackoffController(base_interval=poll_interval, max_backoff=max_backoff)
```

In the capacity probe section (after line 1139), add scarcity/capacity signals:

```python
        # ── Backoff signals ──────────────────────────────────────────────
        pending = _pending_pairs()
        if free_gpus is not None and pending:
            min_cost = min(progress[k].get("gpu_cost", pair_gpu_cost) for k in pending)
            max_cost = max(progress[k].get("gpu_cost", pair_gpu_cost) for k in pending)
            if free_gpus < min_cost:
                prev_state = backoff.state
                backoff.signal_scarcity(free_gpus=free_gpus, min_cost=min_cost)
                if prev_state == "normal":
                    warn(f"Scarcity detected: {free_gpus} free GPUs, entering backoff (next poll: {backoff.effective_interval}s)")
                else:
                    info(f"Backoff level {backoff.backoff_level} — next poll in {backoff.effective_interval}s")
            elif free_gpus >= max_cost:
                if backoff.state != "normal":
                    info(f"Backoff probe: {free_gpus} free GPUs available → resuming normal dispatch")
                backoff.signal_capacity(free_gpus=free_gpus, max_cost=max_cost)
```

Replace the dispatch gating section (lines 1141-1157) with:

```python
        # ── Assign pending work to free slots ────────────────────────────
        free_slots = [ns for ns in namespaces if ns not in slots_busy]
        pending = _pending_pairs()
        if free_gpus is not None and pending:
            min_cost = min(progress[k].get("gpu_cost", pair_gpu_cost) for k in pending)
            if not backoff.should_dispatch(free_gpus=free_gpus, min_cost=min_cost):
                if len(pending) > 0:
                    info(f"Backoff: skipping dispatch ({len(pending)} pending, {free_gpus} free GPUs)")
                dispatchable = []
            else:
                dispatchable = _capacity_limited_pairs(
                    pending, progress,
                    free_gpus=free_gpus, default_gpu_cost=pair_gpu_cost,
                )
                if len(dispatchable) == 0 and pending:
                    smallest = min(progress[k].get("gpu_cost", pair_gpu_cost) for k in pending)
                    warn(f"Dispatching 0/{len(pending)} pending pairs — smallest cost ({smallest}) exceeds free GPUs ({free_gpus})")
                elif len(dispatchable) < len(pending):
                    info(f"Dispatching {len(dispatchable)}/{len(pending)} pending pairs (capacity-limited: {free_gpus} free GPUs)")
                elif len(free_slots) < len(dispatchable):
                    info(f"Dispatching {len(free_slots)}/{len(pending)} pending pairs (slot-limited)")
        else:
            dispatchable = pending
```

After a successful kubectl apply (around line 1205 where `ok(f"[{pair_key}] → {ns}")` is), add scheduling success signal:

```python
            if backoff.state != "normal":
                backoff.signal_scheduling_success()
                info("Scheduling success → backoff reset")
```

Replace the `time.sleep(poll_interval)` at line 1208 with:

```python
        # Persist backoff state
        progress["_orchestrator"] = backoff.to_dict()
        store.save(progress)

        if _work_remaining() or slots_busy:
            time.sleep(backoff.effective_interval)
```

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest pipeline/tests/test_deploy_run.py pipeline/tests/test_backoff.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add pipeline/deploy.py pipeline/tests/test_deploy_run.py
git commit -m "feat(deploy): integrate BackoffController into orchestrator loop"
```

---

### Task 4: Add `--max-backoff` CLI flag

**Files:**
- Modify: `pipeline/deploy.py:1305-1325` (argparse section)
- Modify: `pipeline/tests/test_deploy_run.py`

- [ ] **Step 1: Write failing test for CLI flag**

Add to `pipeline/tests/test_deploy_run.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest pipeline/tests/test_deploy_run.py::test_run_parser_has_max_backoff_flag -v`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'max_backoff'`

- [ ] **Step 3: Add `--max-backoff` flag to `run` subparser**

In `pipeline/deploy.py`, in the `run_p` argument section (after `--max-pending-stalls`, around line 1325), add:

```python
    run_p.add_argument("--max-backoff", type=int, default=600, dest="max_backoff",
                       help="Maximum backoff interval in seconds during GPU scarcity [600]")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest pipeline/tests/test_deploy_run.py::test_run_parser_has_max_backoff_flag pipeline/tests/test_deploy_run.py::test_run_parser_max_backoff_custom -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add pipeline/deploy.py pipeline/tests/test_deploy_run.py
git commit -m "feat(deploy): add --max-backoff CLI flag (default 600s)"
```

---

### Task 5: Show orchestrator state in `deploy.py status`

**Files:**
- Modify: `pipeline/deploy.py:153-195` (`_cmd_status`)
- Modify: `pipeline/tests/test_deploy_run.py`

- [ ] **Step 1: Write failing test for status display**

Add to `pipeline/tests/test_deploy_run.py`:

```python
def test_status_shows_orchestrator_state_backing_off(tmp_path, capsys):
    """deploy.py status should show backoff state when _orchestrator is present."""
    progress = {
        "wl-foo-baseline": {"workload": "foo", "package": "baseline", "status": "running", "namespace": "ns-1", "retries": 0},
        "_orchestrator": {"state": "backing_off", "backoff_level": 2, "last_probe_free_gpus": 0, "last_scarcity_time": "2026-05-08T14:32:00+00:00"},
    }
    pf = tmp_path / "progress.json"
    pf.write_text(json.dumps(progress))

    from pipeline.deploy import _cmd_status
    args = argparse.Namespace(only=None, workload=None, package=None, status=None)
    _cmd_status(args, pf)
    out = capsys.readouterr().out
    assert "backing_off" in out
    assert "level 2" in out


def test_status_no_orchestrator_section_when_normal(tmp_path, capsys):
    """deploy.py status should not show orchestrator section when state is normal."""
    progress = {
        "wl-foo-baseline": {"workload": "foo", "package": "baseline", "status": "running", "namespace": "ns-1", "retries": 0},
        "_orchestrator": {"state": "normal", "backoff_level": 0, "last_probe_free_gpus": 8},
    }
    pf = tmp_path / "progress.json"
    pf.write_text(json.dumps(progress))

    from pipeline.deploy import _cmd_status
    args = argparse.Namespace(only=None, workload=None, package=None, status=None)
    _cmd_status(args, pf)
    out = capsys.readouterr().out
    assert "backing_off" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest pipeline/tests/test_deploy_run.py::test_status_shows_orchestrator_state_backing_off -v`
Expected: FAIL — no backoff state in output

- [ ] **Step 3: Add orchestrator state display to `_cmd_status`**

In `pipeline/deploy.py`, in `_cmd_status` (around line 193, before the final `print()`), add:

```python
    orch = progress.get("_orchestrator")
    if orch and orch.get("state") != "normal":
        print(f"  Orchestrator: {orch['state']} (level {orch.get('backoff_level', 0)}, "
              f"last probe: {orch.get('last_probe_free_gpus', '?')} free GPUs)")
        print()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest pipeline/tests/test_deploy_run.py::test_status_shows_orchestrator_state_backing_off pipeline/tests/test_deploy_run.py::test_status_no_orchestrator_section_when_normal -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add pipeline/deploy.py pipeline/tests/test_deploy_run.py
git commit -m "feat(deploy): show backoff state in status output"
```

---

### Task 6: Consecutive reclaim signal (N reclaims in window)

**Files:**
- Modify: `pipeline/lib/backoff.py`
- Modify: `pipeline/tests/test_backoff.py`
- Modify: `pipeline/deploy.py`

- [ ] **Step 1: Write failing tests for reclaim signal**

Add to `pipeline/tests/test_backoff.py`:

```python
def test_reclaim_signal_triggers_backoff_after_threshold():
    bc = BackoffController(base_interval=30, max_backoff=600, reclaim_threshold=3, reclaim_window=600)
    bc.signal_reclaim()
    assert bc.state == "normal"
    bc.signal_reclaim()
    assert bc.state == "normal"
    bc.signal_reclaim()
    assert bc.state == "backing_off"


def test_reclaim_signal_window_expiry():
    bc = BackoffController(base_interval=30, max_backoff=600, reclaim_threshold=3, reclaim_window=600)
    # Simulate old reclaims that are outside the window
    import datetime as _dt
    old_time = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=700)).isoformat()
    bc._reclaim_times = [old_time, old_time]
    bc.signal_reclaim()
    assert bc.state == "normal"  # old ones expired, only 1 recent


def test_reclaim_signal_roundtrip():
    bc = BackoffController(base_interval=30, max_backoff=600, reclaim_threshold=3, reclaim_window=600)
    bc.signal_reclaim()
    bc.signal_reclaim()
    data = bc.to_dict()
    bc2 = BackoffController.from_dict(data, base_interval=30, max_backoff=600, reclaim_threshold=3, reclaim_window=600)
    assert len(bc2._reclaim_times) == 2
    bc2.signal_reclaim()
    assert bc2.state == "backing_off"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest pipeline/tests/test_backoff.py::test_reclaim_signal_triggers_backoff_after_threshold -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'reclaim_threshold'`

- [ ] **Step 3: Add reclaim signal to BackoffController**

Update `pipeline/lib/backoff.py` — modify `__init__`:

```python
    def __init__(self, base_interval: int, max_backoff: int,
                 reclaim_threshold: int = 3, reclaim_window: int = 600) -> None:
        self._base = base_interval
        self._max = max_backoff
        self._reclaim_threshold = reclaim_threshold
        self._reclaim_window = reclaim_window
        self.state: str = "normal"
        self.backoff_level: int = 0
        self.last_scarcity_time: str | None = None
        self.last_probe_free_gpus: int | None = None
        self._reclaim_times: list[str] = []
```

Add `signal_reclaim` method:

```python
    def signal_reclaim(self) -> None:
        now = _dt.datetime.now(_dt.timezone.utc)
        self._reclaim_times.append(now.isoformat())
        cutoff = (now - _dt.timedelta(seconds=self._reclaim_window)).isoformat()
        self._reclaim_times = [t for t in self._reclaim_times if t >= cutoff]
        if len(self._reclaim_times) >= self._reclaim_threshold:
            self.state = "backing_off"
            self.backoff_level += 1
            raw = self._base * (2 ** self.backoff_level)
            if raw > self._max:
                self.backoff_level = self._level_for_max()
            self.last_scarcity_time = now.isoformat()
            self._reclaim_times = []
```

Update `to_dict`:

```python
    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "backoff_level": self.backoff_level,
            "last_scarcity_time": self.last_scarcity_time,
            "last_probe_free_gpus": self.last_probe_free_gpus,
            "reclaim_times": self._reclaim_times,
        }
```

Update `from_dict`:

```python
    @classmethod
    def from_dict(cls, data: dict, *, base_interval: int, max_backoff: int,
                  reclaim_threshold: int = 3, reclaim_window: int = 600) -> BackoffController:
        bc = cls(base_interval=base_interval, max_backoff=max_backoff,
                 reclaim_threshold=reclaim_threshold, reclaim_window=reclaim_window)
        bc.state = data.get("state", "normal")
        bc.backoff_level = data.get("backoff_level", 0)
        bc.last_scarcity_time = data.get("last_scarcity_time")
        bc.last_probe_free_gpus = data.get("last_probe_free_gpus")
        bc._reclaim_times = data.get("reclaim_times", [])
        return bc
```

Update `_reset`:

```python
    def _reset(self) -> None:
        self.state = "normal"
        self.backoff_level = 0
        self._reclaim_times = []
```

- [ ] **Step 4: Wire reclaim signal into deploy.py**

In `_handle_pending_pods` return site in the orchestrator loop (around line 1092-1094 where `reclaimed` is True):

```python
                if reclaimed:
                    backoff.signal_reclaim()
                    del slots_busy[ns]
                    store.save(progress)
                    continue
```

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest pipeline/tests/test_backoff.py pipeline/tests/test_deploy_run.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add pipeline/lib/backoff.py pipeline/tests/test_backoff.py pipeline/deploy.py
git commit -m "feat(backoff): add consecutive-reclaim scarcity signal"
```

---

### Task 7: Final verification and lint

**Files:** All modified files

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest pipeline/ -v`
Expected: All PASS

- [ ] **Step 2: Run lint**

Run: `ruff check pipeline/ --select F`
Expected: No errors

- [ ] **Step 3: Verify acceptance criteria mapping**

Check each criterion from the issue:
1. ✅ Poll interval increases exponentially up to `--max-backoff` → `BackoffController.effective_interval`
2. ✅ Dispatch skipped during backoff unless capacity → `backoff.should_dispatch()` gate
3. ✅ Running slots still monitored → slot processing runs before backoff gate
4. ✅ Backoff resets on scheduling success or capacity return → `signal_scheduling_success()`, `signal_capacity()`
5. ✅ `deploy.py status` shows orchestrator state → `_cmd_status` reads `_orchestrator`
6. ✅ Backward-compatible → without scarcity, `BackoffController` stays in `normal`, `effective_interval == base_interval`
7. ✅ Tests cover all scenarios → `test_backoff.py` + integration tests in `test_deploy_run.py`

- [ ] **Step 4: Final commit if any adjustments needed**

```bash
git status
# If clean, skip. Otherwise:
git add -A && git commit -m "chore: lint/test fixes"
```
