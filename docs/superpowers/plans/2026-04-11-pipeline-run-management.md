# Pipeline Run Management Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `pipeline/run.py` CLI with `list`, `inspect`, and `switch` subcommands to manage sim2real runs, including syncing generated artifacts to the `llm-d-inference-scheduler` submodule on switch.

**Architecture:** Core logic lives in `pipeline/lib/run_manager.py` (three public functions + data classes + exceptions). `pipeline/run.py` is a thin CLI wrapper that reads repo layout constants and delegates to `run_manager`. The `confirm_fn` and `_check_dirty` parameters on `switch_run` are injected callables, making all paths unit-testable without subprocess or stdin interaction.

**Tech Stack:** Python 3.10+, stdlib only (`json`, `pathlib`, `shutil`, `subprocess`, `dataclasses`, `argparse`). Tests use `pytest` with `tmp_path` fixture.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `pipeline/lib/run_manager.py` | Create | Data types, `list_runs`, `inspect_run`, `switch_run`, exceptions |
| `pipeline/run.py` | Create | CLI entry point: argparse, color helpers, calls run_manager |
| `pipeline/tests/test_run_manager.py` | Create | Unit tests for all run_manager functions |

---

## Chunk 1: `run_manager.py` — data types, `list_runs`, `inspect_run`

### Task 1: Data types and conformance helpers

**Files:**
- Create: `pipeline/lib/run_manager.py`
- Create: `pipeline/tests/test_run_manager.py`

- [ ] **Step 1: Write the failing tests for conformance helpers**

Create `pipeline/tests/test_run_manager.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jchen/go/src/inference-sim/sim2real
python -m pytest pipeline/tests/test_run_manager.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError` or `ImportError` (module doesn't exist yet).

- [ ] **Step 3: Create `pipeline/lib/run_manager.py` with data types and helpers**

```python
"""Run management: list, inspect, and switch sim2real pipeline runs."""
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# ── Exceptions ───────────────────────────────────────────────────────────────

class RunNotFoundError(Exception):
    pass

class TranslationOutputError(Exception):
    pass

class SwitchAborted(Exception):
    """User declined to overwrite uncommitted changes."""


# ── Data types ───────────────────────────────────────────────────────────────

@dataclass
class RunSummary:
    name: str
    scenario: str
    last_phase: str
    verdict: str
    active: bool

@dataclass
class PhaseInfo:
    name: str
    status: str
    notes: str = ""
    verdict: str = ""

@dataclass
class RunDetail:
    name: str
    scenario: str
    active: bool
    phases: list   # list[PhaseInfo]
    files_created: list   # list[str]
    files_modified: list  # list[str]
    deploy_stages: dict   # dict[str, str]
    deploy_last_step: str = ""

@dataclass
class SwitchResult:
    files_written: list  # list[str]
    active_run: str


# ── Conformance helpers ───────────────────────────────────────────────────────

def _load_state(run_dir: Path) -> "dict | None":
    """Load and validate .state.json; return None if nonconforming."""
    path = run_dir / ".state.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not all(k in data for k in ("run_name", "scenario", "phases")):
        return None
    return data

def _load_metadata(run_dir: Path) -> "dict | None":
    """Load and validate run_metadata.json; return None if nonconforming."""
    path = run_dir / "run_metadata.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not all(k in data for k in ("version", "stages")):
        return None
    return data

def _last_done_phase(phases: dict) -> str:
    """Return the name of the last phase with status 'done'."""
    last = ""
    for name, info in phases.items():
        if isinstance(info, dict) and info.get("status") == "done":
            last = name
    return last

def _get_verdict(phases: dict) -> str:
    """Extract verdict string from phases (typically from gate phase)."""
    for info in phases.values():
        if isinstance(info, dict) and "verdict" in info:
            return info["verdict"]
    return ""
```

- [ ] **Step 4: Run conformance tests to verify they pass**

```bash
python -m pytest pipeline/tests/test_run_manager.py::TestLoadState pipeline/tests/test_run_manager.py::TestLoadMetadata -v
```

Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/lib/run_manager.py pipeline/tests/test_run_manager.py
git commit -m "feat(run): add run_manager data types and conformance helpers"
```

---

### Task 2: `list_runs`

**Files:**
- Modify: `pipeline/lib/run_manager.py`
- Modify: `pipeline/tests/test_run_manager.py`

- [ ] **Step 1: Write failing tests for `list_runs`**

Append to `pipeline/tests/test_run_manager.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest pipeline/tests/test_run_manager.py::TestListRuns -v 2>&1 | head -20
```

Expected: `ImportError: cannot import name 'list_runs'`

- [ ] **Step 3: Implement `list_runs`**

Append to `pipeline/lib/run_manager.py`:

```python
def list_runs(workspace_dir: Path, setup_config_path: Path) -> "list[RunSummary]":
    """Return RunSummary for each conforming run, sorted by name. Non-conforming runs are silently skipped."""
    runs_dir = workspace_dir / "runs"
    if not runs_dir.exists():
        return []

    active_run = ""
    if setup_config_path.exists():
        try:
            cfg = json.loads(setup_config_path.read_text())
            active_run = cfg.get("current_run", "")
        except (json.JSONDecodeError, OSError):
            pass

    results = []
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        state = _load_state(run_dir)
        meta = _load_metadata(run_dir)
        if state is None or meta is None:
            continue  # silently skip non-conforming
        name = state["run_name"]
        phases = state.get("phases", {})
        results.append(RunSummary(
            name=name,
            scenario=state["scenario"],
            last_phase=_last_done_phase(phases),
            verdict=_get_verdict(phases),
            active=(name == active_run),
        ))
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest pipeline/tests/test_run_manager.py::TestListRuns -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/lib/run_manager.py pipeline/tests/test_run_manager.py
git commit -m "feat(run): implement list_runs"
```

---

### Task 3: `inspect_run`

**Files:**
- Modify: `pipeline/lib/run_manager.py`
- Modify: `pipeline/tests/test_run_manager.py`

- [ ] **Step 1: Write failing tests for `inspect_run`**

Append to `pipeline/tests/test_run_manager.py`:

```python
class TestInspectRun:
    def _make_run(self, tmp_path, name="adaptive6", scenario="adaptive-routing",
                  phases=None, stages=None, files_created=None, files_modified=None):
        run_dir = tmp_path / "runs" / name
        phases = phases or {
            "init": {"status": "done"},
            "translate": {"status": "done", "review_rounds": 2, "consensus": "1/1"},
            "baseline_derivation": {"status": "done", "user_approved": True},
            "assembly": {"status": "done", "packages": ["baseline", "treatment"]},
            "gate": {"status": "done", "verdict": "READY TO DEPLOY"},
        }
        stages = stages or {
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest pipeline/tests/test_run_manager.py::TestInspectRun -v 2>&1 | head -20
```

Expected: `ImportError: cannot import name 'inspect_run'`

- [ ] **Step 3: Implement `_phase_notes` and `inspect_run`**

Append to `pipeline/lib/run_manager.py`:

```python
def _phase_notes(name: str, info: dict) -> str:
    """Extract human-readable notes from a phase info dict."""
    if name == "translate":
        parts = []
        if "review_rounds" in info:
            parts.append(f"{info['review_rounds']} review rounds")
        if "consensus" in info:
            parts.append(f"consensus {info['consensus']}")
        return ", ".join(parts)
    if name == "baseline_derivation":
        if info.get("user_approved"):
            return "user approved"
    if name == "assembly":
        pkgs = info.get("packages", [])
        if pkgs:
            return f"packages: {', '.join(pkgs)}"
    return ""


def inspect_run(run_dir: Path, active_run: str = "") -> RunDetail:
    """Load full run detail. Raises RunNotFoundError if run_dir doesn't exist or is invalid."""
    if not run_dir.exists():
        raise RunNotFoundError(f"Error: run '{run_dir.name}' not found in workspace/runs/")
    state = _load_state(run_dir)
    if state is None:
        raise RunNotFoundError(f"Error: run '{run_dir.name}' has no valid .state.json")

    meta = _load_metadata(run_dir)

    phases = []
    for name, info in state.get("phases", {}).items():
        if not isinstance(info, dict):
            continue
        phases.append(PhaseInfo(
            name=name,
            status=info.get("status", ""),
            notes=_phase_notes(name, info),
            verdict=info.get("verdict", ""),
        ))

    files_created: list[str] = []
    files_modified: list[str] = []
    to_path = run_dir / "translation_output.json"
    if to_path.exists():
        try:
            to = json.loads(to_path.read_text())
            files_created = to.get("files_created") or []
            files_modified = to.get("files_modified") or []
        except (json.JSONDecodeError, OSError):
            pass

    deploy_stages: dict[str, str] = {}
    deploy_last_step = ""
    if meta:
        for stage_name, stage_info in meta.get("stages", {}).items():
            if isinstance(stage_info, dict):
                deploy_stages[stage_name] = stage_info.get("status", "")
                if stage_name == "deploy":
                    deploy_last_step = stage_info.get("last_completed_step", "")
            else:
                deploy_stages[stage_name] = str(stage_info)

    return RunDetail(
        name=state["run_name"],
        scenario=state["scenario"],
        active=(state["run_name"] == active_run),
        phases=phases,
        files_created=files_created,
        files_modified=files_modified,
        deploy_stages=deploy_stages,
        deploy_last_step=deploy_last_step,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest pipeline/tests/test_run_manager.py::TestInspectRun -v
```

Expected: all 11 tests PASS.

- [ ] **Step 5: Run all run_manager tests so far**

```bash
python -m pytest pipeline/tests/test_run_manager.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add pipeline/lib/run_manager.py pipeline/tests/test_run_manager.py
git commit -m "feat(run): implement inspect_run"
```

---

## Chunk 2: `run_manager.py` — `switch_run`

### Task 4: `switch_run`

**Files:**
- Modify: `pipeline/lib/run_manager.py`
- Modify: `pipeline/tests/test_run_manager.py`

- [ ] **Step 1: Write failing tests for `switch_run`**

Append to `pipeline/tests/test_run_manager.py`:

```python
class TestSwitchRun:
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

        # Submodule dir (not a real git repo — dirty check is injected)
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
                            _check_dirty=lambda d, ps: [])

        assert isinstance(result, SwitchResult)
        assert "pkg/plugins/scorer/adaptive_v2.go" in result.files_written
        assert result.active_run == "adaptive6"

        dst = sub_dir / "pkg/plugins/scorer/adaptive_v2.go"
        assert dst.read_text() == "// content of adaptive_v2.go"

        cfg_data = json.loads(cfg.read_text())
        assert cfg_data["current_run"] == "adaptive6"

    def test_setup_config_only_updated_after_all_copies_succeed(self, tmp_path):
        """setup_config.json must NOT be updated if a copy fails."""
        import shutil as _shutil
        from unittest.mock import patch
        from pipeline.lib.run_manager import switch_run
        ws, sub_dir, cfg, run_dir = self._setup(tmp_path,
            files_modified=["pkg/plugins/scorer/adaptive_v2.go"])

        with patch("pipeline.lib.run_manager.shutil.copy2", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                switch_run("adaptive6", ws, sub_dir, cfg,
                           confirm_fn=lambda _: True,
                           _check_dirty=lambda d, ps: [])

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
                           _check_dirty=lambda d, ps: [])

        # First file written must still be present (not rolled back)
        written = list((sub_dir / "pkg/plugins/scorer").glob("*.go"))
        assert len(written) >= 1

    def test_dirty_check_receives_submodule_relative_paths(self, tmp_path):
        """_check_dirty must be called with the submodule-relative paths, not basenames."""
        from pipeline.lib.run_manager import switch_run
        ws, sub_dir, cfg, _ = self._setup(tmp_path,
            files_created=["pkg/plugins/scorer/adaptive_v2_test.go"],
            files_modified=["pkg/plugins/scorer/adaptive_v2.go"])

        received = {}
        def capture_dirty(d, ps):
            received["submodule_dir"] = d
            received["paths"] = list(ps)
            return []

        switch_run("adaptive6", ws, sub_dir, cfg,
                   confirm_fn=lambda _: True,
                   _check_dirty=capture_dirty)

        assert received["submodule_dir"] == sub_dir
        assert "pkg/plugins/scorer/adaptive_v2_test.go" in received["paths"]
        assert "pkg/plugins/scorer/adaptive_v2.go" in received["paths"]

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
                       _check_dirty=lambda d, ps: [])

    # ── Validation: translation_output.json ──────────────────────────────────

    def test_raises_if_translation_output_missing(self, tmp_path):
        from pipeline.lib.run_manager import switch_run, TranslationOutputError
        ws, sub_dir, cfg, run_dir = self._setup(tmp_path)
        (run_dir / "translation_output.json").unlink()
        with pytest.raises(TranslationOutputError, match="Phase 3"):
            switch_run("adaptive6", ws, sub_dir, cfg,
                       confirm_fn=lambda _: True,
                       _check_dirty=lambda d, ps: [])

    def test_raises_if_translation_output_malformed(self, tmp_path):
        from pipeline.lib.run_manager import switch_run, TranslationOutputError
        ws, sub_dir, cfg, run_dir = self._setup(tmp_path)
        (run_dir / "translation_output.json").write_text(json.dumps({"files_created": "not a list"}))
        with pytest.raises(TranslationOutputError, match="malformed"):
            switch_run("adaptive6", ws, sub_dir, cfg,
                       confirm_fn=lambda _: True,
                       _check_dirty=lambda d, ps: [])

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
                       _check_dirty=lambda d, ps: [])

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
                       _check_dirty=lambda d, ps: [])

    # ── Validation: submodule not found ──────────────────────────────────────

    def test_raises_if_submodule_missing(self, tmp_path):
        from pipeline.lib.run_manager import switch_run, RunNotFoundError
        ws, sub_dir, cfg, run_dir = self._setup(tmp_path)
        import shutil
        shutil.rmtree(sub_dir)
        with pytest.raises(RunNotFoundError, match="submodule"):
            switch_run("adaptive6", ws, sub_dir, cfg,
                       confirm_fn=lambda _: True,
                       _check_dirty=lambda d, ps: [])

    # ── Dirty file handling ───────────────────────────────────────────────────

    def test_dirty_files_confirmed_proceeds(self, tmp_path):
        from pipeline.lib.run_manager import switch_run
        ws, sub_dir, cfg, _ = self._setup(tmp_path)
        confirmed = []
        def confirm(dirty):
            confirmed.extend(dirty)
            return True

        result = switch_run("adaptive6", ws, sub_dir, cfg,
                            confirm_fn=confirm,
                            _check_dirty=lambda d, ps: ["pkg/plugins/scorer/adaptive_v2.go"])
        assert len(confirmed) == 1
        assert result is not None

    def test_dirty_files_declined_raises_switch_aborted(self, tmp_path):
        from pipeline.lib.run_manager import switch_run, SwitchAborted
        ws, sub_dir, cfg, _ = self._setup(tmp_path)
        with pytest.raises(SwitchAborted):
            switch_run("adaptive6", ws, sub_dir, cfg,
                       confirm_fn=lambda _: False,
                       _check_dirty=lambda d, ps: ["pkg/plugins/scorer/adaptive_v2.go"])

    def test_dirty_declined_does_not_modify_setup_config(self, tmp_path):
        from pipeline.lib.run_manager import switch_run, SwitchAborted
        ws, sub_dir, cfg, _ = self._setup(tmp_path, active_run="other")
        with pytest.raises(SwitchAborted):
            switch_run("adaptive6", ws, sub_dir, cfg,
                       confirm_fn=lambda _: False,
                       _check_dirty=lambda d, ps: ["pkg/plugins/scorer/adaptive_v2.go"])
        assert json.loads(cfg.read_text())["current_run"] == "other"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest pipeline/tests/test_run_manager.py::TestSwitchRun -v 2>&1 | head -20
```

Expected: `ImportError: cannot import name 'switch_run'`

- [ ] **Step 3: Implement `_load_translation_output`, `_git_dirty_default`, and `switch_run`**

Append to `pipeline/lib/run_manager.py`:

```python
def _load_translation_output(run_dir: Path, run_name: str) -> "tuple[list[str], list[str]]":
    """Load and validate translation_output.json. Returns (files_created, files_modified)."""
    to_path = run_dir / "translation_output.json"
    if not to_path.exists():
        raise TranslationOutputError(
            f"Error: run '{run_name}' has no translation_output.json — was Phase 3 completed?"
        )
    try:
        data = json.loads(to_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise TranslationOutputError(
            f"Error: translation_output.json is malformed — {e}"
        )
    fc = data.get("files_created")
    fm = data.get("files_modified")
    if (not isinstance(fc, list) or not isinstance(fm, list)
            or not all(isinstance(x, str) for x in fc)
            or not all(isinstance(x, str) for x in fm)):
        raise TranslationOutputError(
            "Error: translation_output.json is malformed — expected 'files_created' and "
            "'files_modified' as lists of strings"
        )
    return fc, fm


def _git_dirty_default(submodule_dir: Path, paths: "list[str]") -> "list[str]":
    """Return paths that have uncommitted tracked changes in the submodule."""
    dirty = []
    for rel_path in paths:
        result = subprocess.run(
            ["git", "-C", str(submodule_dir), "status", "--porcelain", rel_path],
            capture_output=True, text=True,
        )
        line = result.stdout.strip()
        # '??' prefix = untracked; not considered dirty for our purposes
        if line and not line.startswith("??"):
            dirty.append(rel_path)
    return dirty


def switch_run(
    run_name: str,
    workspace_dir: Path,
    submodule_dir: Path,
    setup_config_path: Path,
    confirm_fn: "callable",
    _check_dirty: "callable | None" = None,
) -> SwitchResult:
    """
    Switch the active run: validate, copy generated files to submodule, update setup_config.

    confirm_fn(dirty_files: list[str]) -> bool  — called when dirty files found; return True to proceed.
    _check_dirty(submodule_dir, paths) -> list[str]  — injectable for tests; defaults to git status.

    Raises: RunNotFoundError, TranslationOutputError, ValueError, SwitchAborted, OSError.
    """
    if _check_dirty is None:
        _check_dirty = _git_dirty_default

    run_dir = workspace_dir / "runs" / run_name

    # Step 1: validate and load
    if not run_dir.exists():
        raise RunNotFoundError(f"Error: run '{run_name}' not found in workspace/runs/")
    if not setup_config_path.exists():
        raise RunNotFoundError("Error: workspace/setup_config.json not found")

    files_created, files_modified = _load_translation_output(run_dir, run_name)
    target_files = files_created + files_modified

    # Step 2: basename collision check
    seen: set[str] = set()
    for rel_path in target_files:
        basename = Path(rel_path).name
        if basename in seen:
            raise ValueError(
                f"Error: basename collision in translation_output.json: "
                f"'{basename}' maps to multiple paths"
            )
        seen.add(basename)

    # Step 3: pre-validate all source files
    generated_dir = run_dir / "generated"
    missing = [Path(f).name for f in target_files
               if not (generated_dir / Path(f).name).exists()]
    if missing:
        raise ValueError(
            f"Error: missing source files in workspace/runs/{run_name}/generated/: "
            + ", ".join(missing)
        )

    # Step 4: check submodule exists
    if not submodule_dir.exists():
        raise RunNotFoundError(
            "Error: submodule directory llm-d-inference-scheduler not found"
        )

    # Step 5: dirty check (immediately before copy)
    dirty = _check_dirty(submodule_dir, target_files)
    if dirty and not confirm_fn(dirty):
        raise SwitchAborted()

    # Step 6: copy files
    files_written: list[str] = []
    for rel_path in target_files:
        src = generated_dir / Path(rel_path).name
        dst = submodule_dir / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            files_written.append(rel_path)
        except OSError as e:
            raise OSError(f"Error: failed to copy {rel_path}: {e}") from e

    # Step 7: update setup_config only after all copies succeed
    cfg = json.loads(setup_config_path.read_text())
    cfg["current_run"] = run_name
    setup_config_path.write_text(json.dumps(cfg, indent=2))

    return SwitchResult(files_written=files_written, active_run=run_name)
```

- [ ] **Step 4: Run switch_run tests**

```bash
python -m pytest pipeline/tests/test_run_manager.py::TestSwitchRun -v
```

Expected: all 11 tests PASS.

- [ ] **Step 5: Run all run_manager tests**

```bash
python -m pytest pipeline/tests/test_run_manager.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add pipeline/lib/run_manager.py pipeline/tests/test_run_manager.py
git commit -m "feat(run): implement switch_run"
```

---

## Chunk 3: `pipeline/run.py` CLI

### Task 5: CLI wiring

**Files:**
- Create: `pipeline/run.py`

- [ ] **Step 1: Create `pipeline/run.py`**

```python
#!/usr/bin/env python3
"""sim2real run — list, inspect, and switch pipeline runs."""

import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when run as a script (python pipeline/run.py)
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.lib.run_manager import (
    list_runs, inspect_run, switch_run,
    RunNotFoundError, TranslationOutputError, SwitchAborted,
)

# ── Repo layout ───────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = REPO_ROOT / "workspace"
SETUP_CONFIG = WORKSPACE_DIR / "setup_config.json"
SUBMODULE_DIR = REPO_ROOT / "llm-d-inference-scheduler"

# ── Color helpers ─────────────────────────────────────────────────────────────
_tty = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text

def info(msg: str) -> None: print(_c("34", "[INFO]  ") + msg)
def ok(msg: str)   -> None: print(_c("32", "[OK]    ") + msg)
def warn(msg: str) -> None: print(_c("33", "[WARN]  ") + msg)
def err(msg: str)  -> None: print(_c("31", "[ERROR] ") + msg, file=sys.stderr)


# ── Subcommand handlers ───────────────────────────────────────────────────────

def cmd_list(_args) -> None:
    runs = list_runs(WORKSPACE_DIR, SETUP_CONFIG)
    fmt = "{:<14} {:<28} {:<12} {:<22} {}"
    print(fmt.format("NAME", "SCENARIO", "PHASE", "VERDICT", "ACTIVE"))
    for r in runs:
        print(fmt.format(r.name, r.scenario, r.last_phase, r.verdict,
                         "*" if r.active else ""))


def cmd_inspect(args) -> None:
    active_run = ""
    if SETUP_CONFIG.exists():
        try:
            active_run = json.loads(SETUP_CONFIG.read_text()).get("current_run", "")
        except (json.JSONDecodeError, OSError):
            pass

    run_dir = WORKSPACE_DIR / "runs" / args.name
    try:
        detail = inspect_run(run_dir, active_run=active_run)
    except RunNotFoundError as e:
        err(str(e))
        sys.exit(1)

    active_marker = "  [ACTIVE]" if detail.active else ""
    print(f"Run: {detail.name}{active_marker}")
    print(f"Scenario: {detail.scenario}")
    print()
    print("Phases:")
    for p in detail.phases:
        notes_part = f"  ({p.notes})" if p.notes else ""
        verdict_part = f"  \u2192 {p.verdict}" if p.verdict else ""
        print(f"  {p.name:<20} {p.status}{notes_part}{verdict_part}")

    if detail.files_created or detail.files_modified:
        print()
        print("Generated files:")
        for f in detail.files_modified:
            print(f"  {f}  (modified)")
        for f in detail.files_created:
            print(f"  {f}  (created)")

    if detail.deploy_stages:
        print()
        print("Deploy:")
        for stage, status in detail.deploy_stages.items():
            extra = (f"  (last: {detail.deploy_last_step})"
                     if stage == "deploy" and detail.deploy_last_step else "")
            print(f"  {stage:<10} {status}{extra}")


def cmd_switch(args) -> None:
    def confirm_fn(dirty_files):
        warn("The following files in llm-d-inference-scheduler have uncommitted changes:")
        for f in dirty_files:
            print(f"  {f}")
        answer = input("Overwrite uncommitted changes? [y/N] ").strip().lower()
        return answer == "y"

    try:
        result = switch_run(
            args.name, WORKSPACE_DIR, SUBMODULE_DIR, SETUP_CONFIG, confirm_fn
        )
    except (RunNotFoundError, TranslationOutputError, ValueError) as e:
        err(str(e))
        sys.exit(1)
    except SwitchAborted:
        info("Switch aborted — no changes made.")
        sys.exit(0)
    except OSError as e:
        err(str(e))
        sys.exit(1)

    ok(f"Switched to run: {result.active_run}")
    for f in result.files_written:
        info(f"  wrote {f}")


# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="sim2real run — list, inspect, and switch pipeline runs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pipeline/run.py list              # show all runs with status
  python pipeline/run.py inspect adaptive6 # show run details
  python pipeline/run.py switch admin5     # switch active run, sync submodule
""",
    )
    sub = p.add_subparsers(dest="command")

    sub.add_parser("list", help="List all conforming runs")

    inspect_p = sub.add_parser("inspect", help="Show full details of a run")
    inspect_p.add_argument("name", metavar="NAME", help="Run name")

    switch_p = sub.add_parser("switch", help="Switch active run and sync submodule artifacts")
    switch_p.add_argument("name", metavar="NAME", help="Run name to switch to")

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "inspect":
        cmd_inspect(args)
    elif args.command == "switch":
        cmd_switch(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test `list` against real workspace**

```bash
python pipeline/run.py list
```

Expected: table of conforming runs, `adaptive6` marked `*` in ACTIVE column. Non-conforming older runs are not shown.

- [ ] **Step 3: Smoke-test `inspect` against real workspace**

```bash
python pipeline/run.py inspect adaptive6
```

Expected: phases, generated files, deploy stages printed. No errors.

- [ ] **Step 4: Verify `inspect` exits 1 for unknown run**

```bash
python pipeline/run.py inspect nonexistent_run_xyz; echo "exit: $?"
```

Expected: `[ERROR] Error: run 'nonexistent_run_xyz' not found in workspace/runs/` and `exit: 1`.

- [ ] **Step 5: Smoke-test `switch` with the active run (round-trip)**

```bash
# Read current active run, switch to it (no-op), confirm exit 0
python pipeline/run.py switch adaptive6; echo "exit: $?"
```

Expected: `[OK] Switched to run: adaptive6` followed by `wrote` lines and `exit: 0`. If there are no dirty files in the submodule, no prompt should appear.

- [ ] **Step 6: Run all pipeline tests to verify nothing broken**

```bash
python -m pytest pipeline/tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add pipeline/run.py
git commit -m "feat(run): add pipeline/run.py CLI (list, inspect, switch)"
```
