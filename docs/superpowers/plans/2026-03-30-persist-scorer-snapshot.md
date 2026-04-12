# Persist Generated Scorer Snapshot on Pipeline Success

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Copy the generated scorer and test `.go` files into `run_dir` when `stage_final_review` passes all gates, so each run has a durable snapshot of the code it produced.

**Architecture:** Add `persist_scorer_snapshot(run_dir, stage3_path)` to `scripts/prepare.py` that reads scorer/test paths from `prepare_stage3_output.json` and copies them into `run_dir` as `prepare_scorer.go` / `prepare_scorer_test.go`. Call it from `write_outputs()`, which already executes only on successful pipeline completion.

**Tech Stack:** Python 3.10+, stdlib only (`shutil`, `pathlib`, `json`)

---

## Chunk 1: Implementation + Tests

### Task 1: Write the failing tests

**Files:**
- Create: `scripts/test_prepare_snapshot.py`

- [ ] **Step 1: Write the test file**

```python
"""Tests for persist_scorer_snapshot in prepare.py."""
import json
import sys
import types
from pathlib import Path

import pytest

# ── Load only the function under test, stubbing heavy top-level imports ───────

def _load_prepare():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "prepare", Path(__file__).resolve().parent / "prepare.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("yaml", types.ModuleType("yaml"))
    spec.loader.exec_module(mod)
    return mod


_mod = _load_prepare()
persist_scorer_snapshot = _mod.persist_scorer_snapshot


# ── Helpers ───────────────────────────────────────────────────────────────────

def _setup(tmp: Path):
    """Minimal run_dir + stage3 artifact + fake scorer source files."""
    run_dir = tmp / "run"
    run_dir.mkdir()

    scorer_dir = tmp / "scorer"
    scorer_dir.mkdir()
    scorer = scorer_dir / "evolved_scorer.go"
    scorer.write_text("package scorer\n// impl\n")
    test_f = scorer_dir / "evolved_scorer_test.go"
    test_f.write_text("package scorer_test\n// tests\n")

    stage3 = run_dir / "prepare_stage3_output.json"
    stage3.write_text(json.dumps({
        "scorer_file": str(scorer.relative_to(tmp)),
        "test_file": str(test_f.relative_to(tmp)),
        "register_file": "llm-d-inference-scheduler/pkg/plugins/register.go",
        "scorer_type": "evolved-scorer",
        "tekton_artifacts": {"values_yaml": ""},
    }))

    return run_dir, stage3, scorer, test_f, tmp


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_copies_scorer_and_test(tmp_path, monkeypatch):
    run_dir, stage3, scorer, test_f, root = _setup(tmp_path)
    monkeypatch.setattr(_mod, "REPO_ROOT", root)

    scorer_dest, test_dest = persist_scorer_snapshot(run_dir, stage3)

    assert scorer_dest == run_dir / "prepare_scorer.go"
    assert test_dest == run_dir / "prepare_scorer_test.go"
    assert scorer_dest.read_text() == scorer.read_text()
    assert test_dest.read_text() == test_f.read_text()


def test_missing_test_file_returns_none(tmp_path, monkeypatch):
    run_dir, stage3, scorer, test_f, root = _setup(tmp_path)
    test_f.unlink()
    data = json.loads(stage3.read_text())
    data["test_file"] = ""
    stage3.write_text(json.dumps(data))
    monkeypatch.setattr(_mod, "REPO_ROOT", root)

    scorer_dest, test_dest = persist_scorer_snapshot(run_dir, stage3)

    assert scorer_dest.exists()
    assert test_dest is None


def test_overwrites_stale_snapshot(tmp_path, monkeypatch):
    run_dir, stage3, scorer, test_f, root = _setup(tmp_path)
    (run_dir / "prepare_scorer.go").write_text("stale content")
    monkeypatch.setattr(_mod, "REPO_ROOT", root)

    persist_scorer_snapshot(run_dir, stage3)

    assert (run_dir / "prepare_scorer.go").read_text() == scorer.read_text()
```

- [ ] **Step 2: Run tests — expect failure** (`persist_scorer_snapshot` not yet defined)

```bash
cd /Users/jchen/go/src/inference-sim/sim2real
python -m pytest scripts/test_prepare_snapshot.py -v
```

Expected: `AttributeError: module 'prepare' has no attribute 'persist_scorer_snapshot'`

---

### Task 2: Implement `persist_scorer_snapshot`

**Files:**
- Modify: `scripts/prepare.py` — insert function before `write_outputs` (around line 1400)

- [ ] **Step 3: Insert the function**

Add immediately before the `# ── Completion ──` section (the line `def write_outputs`):

```python
# ── Scorer snapshot ───────────────────────────────────────────────────────────

def persist_scorer_snapshot(run_dir: Path, stage3_path: Path) -> tuple[Path, Path | None]:
    """Copy generated scorer + test files into run_dir as durable run artifacts.

    Called only after all gates pass. Returns (scorer_dest, test_dest).
    test_dest is None when no test file was generated.
    """
    stage3 = json.loads(stage3_path.read_text())

    scorer_src = REPO_ROOT / stage3["scorer_file"]
    test_src_str = stage3.get("test_file", "")
    test_src = (REPO_ROOT / test_src_str) if test_src_str else None

    scorer_dest = run_dir / "prepare_scorer.go"
    shutil.copy(scorer_src, scorer_dest)
    ok(f"Scorer snapshot → {scorer_dest.relative_to(REPO_ROOT)}")

    test_dest: Path | None = None
    if test_src and test_src.exists():
        test_dest = run_dir / "prepare_scorer_test.go"
        shutil.copy(test_src, test_dest)
        ok(f"Test snapshot   → {test_dest.relative_to(REPO_ROOT)}")

    return scorer_dest, test_dest
```

- [ ] **Step 4: Run tests — expect pass**

```bash
python -m pytest scripts/test_prepare_snapshot.py -v
```

Expected: 3 tests pass.

---

### Task 3: Wire into `write_outputs`

**Files:**
- Modify: `scripts/prepare.py` — `write_outputs` function (around line 1402)

- [ ] **Step 5: Call `persist_scorer_snapshot` at the top of `write_outputs`**

The current `write_outputs` opens with:
```python
def write_outputs(run_dir: Path, cfg: dict, stage3_path: Path) -> None:
    stage3 = json.loads(stage3_path.read_text())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    update_run_metadata(
```

Change to:
```python
def write_outputs(run_dir: Path, cfg: dict, stage3_path: Path) -> None:
    stage3 = json.loads(stage3_path.read_text())
    persist_scorer_snapshot(run_dir, stage3_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    update_run_metadata(
        run_dir, "prepare",
        status="completed",
        completed_at=now,
        summary="Extract, translate, generate, build/test, and AI review completed",
        artifacts=[
            "prepare_algorithm_summary.json",
            "prepare_signal_coverage.json",
            "prepare_stage3_output.json",
            "prepare_translation_reviews.json",
            "prepare_scorer.go",
            "prepare_scorer_test.go",
        ],
    )
```

- [ ] **Step 6: Add the new artifacts to the printed list in `write_outputs`**

Find the `print("Artifacts produced:")` loop that lists artifact names, currently:
```python
    for name in [
        "prepare_algorithm_summary.json",
        "prepare_signal_coverage.json",
        "prepare_stage3_output.json",
        "prepare_reviewer_output.json",
        "prepare_equivalence_results.json",
        "prepare_translation_reviews.json",
    ]:
```

Change to:
```python
    for name in [
        "prepare_algorithm_summary.json",
        "prepare_signal_coverage.json",
        "prepare_stage3_output.json",
        "prepare_reviewer_output.json",
        "prepare_equivalence_results.json",
        "prepare_translation_reviews.json",
        "prepare_scorer.go",
        "prepare_scorer_test.go",
    ]:
```

- [ ] **Step 7: Run the full unit test suite — expect no regressions**

```bash
python -m pytest tools/ scripts/test_prepare_snapshot.py -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add scripts/prepare.py scripts/test_prepare_snapshot.py
git commit -m "feat(prepare): snapshot scorer + test files into run_dir on pipeline success"
```
