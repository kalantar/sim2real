# Translate Autonomy Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the sim2real-translate skill fully autonomous about what files to create/modify, driven by user hints in `transfer.yaml` rather than pre-specified target config in `env_defaults.yaml`.

**Architecture:** Hints (inline text + files from `transfer.yaml`) flow through `manifest.py` → `skill_input.json` → skill session dynamically. The cached context doc stays pure codebase state. `env_defaults.yaml` target shrinks to `repo` only. Assembly gains `treatment_config_generated` semantics and PipelineRun-per-workload output. `pipeline/lib/values.py` and `pipeline/lib/tekton.py` internalize merge/compile logic so `prepare.py` no longer calls `transfer_cli.py` via subprocess.

**Tech Stack:** Python 3.10+ stdlib + PyYAML, pytest, existing `pipeline/lib/` patterns.

**Spec:** `docs/superpowers/specs/2026-04-09-sim2real-translate-autonomy-design.md`
**Self-contained design:** `docs/superpowers/specs/2026-04-09-pipeline-self-contained-design.md`

---

## File Map

### New files
| File | Responsibility |
|------|----------------|
| `pipeline/lib/values.py` | Deep-merge, gaie-shared flatten, vllm/multiplier overrides, `merge_values()` public API |
| `pipeline/lib/tekton.py` | `compile_pipeline()` + `make_pipelinerun()` — no subprocess to transfer_cli.py |
| `pipeline/tests/test_values.py` | Unit tests for all functions in values.py |
| `pipeline/tests/test_tekton.py` | Unit tests for tekton.py |

### Modified files
| File | Changes |
|------|---------|
| `pipeline/lib/manifest.py` | Parse `hints` section; read hint file contents; warn on `context.notes` |
| `pipeline/prepare.py` | Remove `CLI`/`VENV_PYTHON`; Phase 3 writes hints + shrinks target; Phase 4 uses `treatment_config_generated`, `register_file`, PipelineRuns; remove `_deep_merge`/`_run_merge_values` |
| `pipeline/tests/test_manifest.py` | Add hints parsing tests |
| `pipeline/tests/test_prepare.py` | Update `MINIMAL_ENV_DEFAULTS` fixture; update validate_assembly tests for new Check 1/2/3; update skill_input.json assertions; remove `mod.CLI` assignment |
| `config/env_defaults.yaml` | Remove `target.plugin_dir/register_file/rewrite_file/package`, remove `build.test_scope` |
| `config/transfer.yaml` | Add `hints` section |
| `.claude/skills/sim2real-translate/SKILL.md` | Autonomous Step 2; hints loading; remove REWRITE_FILE branching; update review.py invocation |
| `.claude/skills/sim2real-translate/scripts/review.py` | Add `--hints-json` argument |

---

## Chunk 1: pipeline/lib/values.py

Port merge-values logic from `tools/transfer_cli.py` into `pipeline/lib/values.py`. Functions at lines: `_detect_list_key` (2092), `_merge_lists` (2103), `_deep_merge` (2154), `_flatten_gaie_shared` (2172), `_apply_vllm_image_override` (2222), `_apply_request_multiplier` (2245). Copy verbatim — do not refactor.

### Task 1: Failing tests for values.py

**Files:**
- Create: `pipeline/tests/test_values.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for pipeline/lib/values.py — merge logic."""
import copy
import io
import pytest
import yaml
from pathlib import Path
from pipeline.lib.values import (
    _detect_list_key, _merge_lists, _deep_merge,
    _flatten_gaie_shared, _apply_vllm_image_override,
    _apply_request_multiplier, merge_values,
)


# ── _detect_list_key ─────────────────────────────────────────────────────────

def test_detect_list_key_by_name():
    base = [{"name": "a", "val": 1}]
    overlay = [{"name": "b", "val": 2}]
    assert _detect_list_key(base, overlay) == "name"


def test_detect_list_key_returns_none_for_scalars():
    assert _detect_list_key([1, 2], [3, 4]) is None


def test_detect_list_key_returns_none_when_no_common_key():
    base = [{"x": 1}]
    overlay = [{"y": 2}]
    assert _detect_list_key(base, overlay) is None


# ── _merge_lists ─────────────────────────────────────────────────────────────

def test_merge_lists_scalar_replacement():
    assert _merge_lists([1, 2], [3, 4]) == [3, 4]


def test_merge_lists_explicit_clear():
    assert _merge_lists([1, 2, 3], []) == []


def test_merge_lists_named_key_merge():
    base = [{"name": "a", "val": 1}, {"name": "b", "val": 2}]
    overlay = [{"name": "a", "val": 99}, {"name": "c", "val": 3}]
    result = _merge_lists(base, overlay)
    by_name = {r["name"]: r for r in result}
    assert by_name["a"]["val"] == 99   # merged
    assert by_name["b"]["val"] == 2    # preserved
    assert by_name["c"]["val"] == 3    # added


def test_merge_lists_positional_fallback():
    base = [{"x": 1}, {"x": 2}]
    overlay = [{"x": 10}]
    result = _merge_lists(base, overlay)
    assert result[0]["x"] == 10   # positional merge
    assert result[1]["x"] == 2    # base preserved


# ── _deep_merge ───────────────────────────────────────────────────────────────

def test_deep_merge_simple():
    result = _deep_merge({"a": 1, "nested": {"x": 1, "y": 2}},
                         {"b": 2, "nested": {"y": 3, "z": 4}})
    assert result == {"a": 1, "b": 2, "nested": {"x": 1, "y": 3, "z": 4}}


def test_deep_merge_does_not_mutate():
    base = {"nested": {"x": 1}}
    overlay = {"nested": {"x": 2}}
    result = _deep_merge(base, overlay)
    assert base["nested"]["x"] == 1
    assert result["nested"]["x"] == 2


def test_deep_merge_delegates_lists():
    base = {"items": [{"name": "a", "v": 1}]}
    overlay = {"items": [{"name": "a", "v": 99}]}
    result = _deep_merge(base, overlay)
    assert result["items"][0]["v"] == 99


# ── _flatten_gaie_shared ──────────────────────────────────────────────────────

def test_flatten_gaie_shared_distributes_to_phases():
    merged = {
        "stack": {"gaie": {
            "shared": {"helmValues": {"flags": {"v": 5}}},
            "baseline": {"helmValues": {"other": "x"}},
            "treatment": {"helmValues": {}},
        }}
    }
    result = _flatten_gaie_shared(merged)
    gaie = result["stack"]["gaie"]
    assert gaie["baseline"]["helmValues"]["flags"] == {"v": 5}
    assert gaie["treatment"]["helmValues"]["flags"] == {"v": 5}
    assert "shared" not in gaie


def test_flatten_gaie_shared_no_shared_is_noop():
    merged = {"stack": {"gaie": {"baseline": {"helmValues": {"a": 1}}}}}
    result = _flatten_gaie_shared(copy.deepcopy(merged))
    assert result["stack"]["gaie"]["baseline"]["helmValues"]["a"] == 1


# ── _apply_vllm_image_override ────────────────────────────────────────────────

def test_apply_vllm_image_override():
    merged = {
        "stack": {
            "model": {
                "vllm_image": "ghcr.io/llm-d/llm-d-cuda:v0.5.1",
                "helmValues": {"decode": {"containers": [{"image": "old"}]}},
            }
        }
    }
    result = _apply_vllm_image_override(merged)
    assert result["stack"]["model"]["helmValues"]["decode"]["containers"][0]["image"] == \
        "ghcr.io/llm-d/llm-d-cuda:v0.5.1"
    assert "vllm_image" not in result["stack"]["model"]


def test_apply_vllm_image_override_noop_when_absent():
    merged = {"stack": {"model": {"helmValues": {}}}}
    result = _apply_vllm_image_override(copy.deepcopy(merged))
    assert "vllm_image" not in result["stack"]["model"]


# ── _apply_request_multiplier ─────────────────────────────────────────────────

def test_apply_request_multiplier_scales():
    merged = {
        "observe": {
            "request_multiplier": 10,
            "workloads": [{"name": "wl1", "num_requests": 100}],
        }
    }
    result = _apply_request_multiplier(merged)
    assert result["observe"]["workloads"][0]["num_requests"] == 1000
    assert "request_multiplier" not in result["observe"]


def test_apply_request_multiplier_absent_is_noop():
    merged = {"observe": {"workloads": [{"name": "wl1", "num_requests": 100}]}}
    result = _apply_request_multiplier(copy.deepcopy(merged))
    assert result["observe"]["workloads"][0]["num_requests"] == 100


def test_apply_request_multiplier_one_is_noop():
    merged = {
        "observe": {
            "request_multiplier": 1,
            "workloads": [{"name": "wl1", "num_requests": 50}],
        }
    }
    result = _apply_request_multiplier(merged)
    assert result["observe"]["workloads"][0]["num_requests"] == 50


# ── merge_values end-to-end ───────────────────────────────────────────────────

def _make_env(tmp_path, data):
    p = tmp_path / "env.yaml"
    p.write_text(yaml.dump(data))
    return p


def _make_alg(tmp_path, data):
    p = tmp_path / "alg.yaml"
    p.write_text(yaml.dump(data))
    return p


def test_merge_values_end_to_end(tmp_path):
    env_path = _make_env(tmp_path, {
        "common": {"observe": {"request_multiplier": 1}},
        "scenarios": {
            "routing": {
                "target": {"repo": "llm-d-inference-scheduler"},
                "config": {"kind": "EndpointPickerConfig"},
                "build": {"commands": [["go", "build", "./..."]]},
                "gaie": {"baseline": {"helmValues": {}}},
            }
        }
    })
    alg_path = _make_alg(tmp_path, {
        "stack": {"model": {"modelName": "test-model"}},
        "observe": {"workloads": [{"name": "wl1", "num_requests": 10}]},
    })
    out_path = tmp_path / "values.yaml"
    merge_values(env_path, alg_path, out_path, scenario="routing")
    result = yaml.safe_load(out_path.read_text())
    assert result["stack"]["model"]["modelName"] == "test-model"
    # pipeline-only keys stripped
    assert "target" not in result
    assert "build" not in result


def test_merge_values_raises_on_missing_env(tmp_path):
    from pipeline.lib.values import merge_values
    with pytest.raises(FileNotFoundError):
        merge_values(tmp_path / "missing.yaml", tmp_path / "alg.yaml",
                     tmp_path / "out.yaml")


def test_merge_values_raises_on_unknown_scenario(tmp_path):
    env_path = _make_env(tmp_path, {"common": {}, "scenarios": {}})
    alg_path = _make_alg(tmp_path, {})
    with pytest.raises(ValueError, match="unknown_scenario"):
        merge_values(env_path, alg_path, tmp_path / "out.yaml",
                     scenario="unknown_scenario")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/jchen/go/src/inference-sim/sim2real
python -m pytest pipeline/tests/test_values.py -v 2>&1 | head -30
```

Expected: `ImportError` or `ModuleNotFoundError` — `pipeline.lib.values` doesn't exist yet.

### Task 2: Implement values.py

**Files:**
- Create: `pipeline/lib/values.py`

- [ ] **Step 3: Create values.py by porting from transfer_cli.py**

Copy verbatim (no refactoring) from `tools/transfer_cli.py`:
- Lines 2092–2102: `_detect_list_key`
- Lines 2103–2153: `_merge_lists`
- Lines 2154–2171: `_deep_merge`
- Lines 2172–2221: `_flatten_gaie_shared`
- Lines 2222–2244: `_apply_vllm_image_override`
- Lines 2245–2280: `_apply_request_multiplier`

Then add the `merge_values` public function:

```python
"""Deep-merge logic for sim2real pipeline values assembly.

Ported from tools/transfer_cli.py. The two copies evolve independently:
this module is internal pipeline logic; transfer_cli.py is public CLI.
"""
import copy
import yaml
from pathlib import Path

_LIST_KEY_CANDIDATES = ("name", "mountPath", "containerPort")

# ... (copy _detect_list_key, _merge_lists, _deep_merge,
#      _flatten_gaie_shared, _apply_vllm_image_override,
#      _apply_request_multiplier verbatim) ...

_PIPELINE_ONLY_KEYS = {"target", "build", "config"}


def merge_values(
    env_path: Path,
    alg_path: Path,
    out_path: Path,
    scenario: str | None = None,
) -> None:
    """Deep-merge env_defaults and algorithm_values into values.yaml.

    Raises:
        FileNotFoundError: if env_path or alg_path does not exist
        yaml.YAMLError: if either input file fails to parse
        ValueError: if scenario is specified but not found in env_defaults
        OSError: if writing out_path fails
    """
    if not Path(env_path).exists():
        raise FileNotFoundError(f"env_defaults not found: {env_path}")
    if not Path(alg_path).exists():
        raise FileNotFoundError(f"algorithm_values not found: {alg_path}")

    env_data = yaml.safe_load(Path(env_path).read_text()) or {}
    alg_data = yaml.safe_load(Path(alg_path).read_text()) or {}

    if scenario is not None:
        common = env_data.get("common", {})
        scenarios = env_data.get("scenarios", {})
        if scenario not in scenarios:
            raise ValueError(
                f"Scenario '{scenario}' not found in env_defaults. "
                f"Available: {list(scenarios.keys())}"
            )
        env_data = _deep_merge(common, scenarios[scenario])
        # Strip pipeline-only keys (not needed in values.yaml)
        for key in _PIPELINE_ONLY_KEYS:
            env_data.pop(key, None)

    merged = _deep_merge(env_data, alg_data)
    # Strip pipeline.* except sleepDuration
    pipeline = merged.get("pipeline", {})
    if pipeline:
        sleep = pipeline.get("sleepDuration")
        merged.pop("pipeline", None)
        if sleep is not None:
            merged.setdefault("pipeline", {})["sleepDuration"] = sleep

    _flatten_gaie_shared(merged)
    _apply_vllm_image_override(merged)
    _apply_request_multiplier(merged)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(yaml.dump(merged, default_flow_style=False,
                                         allow_unicode=True, sort_keys=False))
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest pipeline/tests/test_values.py -v
```

Expected: all green.

- [ ] **Step 5: Update prepare.py to use values.merge_values**

In `pipeline/prepare.py`:

1. Add import at top: `from pipeline.lib.values import merge_values, _deep_merge`
2. Delete `_deep_merge` function (local list-unaware copy — replaced by imported version)
3. Delete `_run_merge_values` function
4. Delete `CLI = ...` and `VENV_PYTHON = ...` constants
5. Replace `_run_merge_values(...)` call in `_phase_assembly` with:

```python
try:
    merge_values(REPO_ROOT / "config" / "env_defaults.yaml",
                 alg_values_path, values_path, scenario=manifest["scenario"])
except (FileNotFoundError, yaml.YAMLError, ValueError, OSError) as e:
    err(f"merge-values failed: {e}")
    sys.exit(1)
```

6. In `test_prepare.py`, remove line `mod.CLI = str(repo_root / "tools" / "transfer_cli.py")`
   and `mod.VENV_PYTHON = "python"` from `_import_prepare_with_root`.

- [ ] **Step 5b: Update deploy.py to use values.merge_values**

In `pipeline/deploy.py` (pipeline-self-contained spec, "Modified Files"):

1. Delete `CLI = str(REPO_ROOT / "tools/transfer_cli.py")` constant
2. Delete `VENV_PYTHON = str(REPO_ROOT / ".venv/bin/python")` constant
3. Add import: `from pipeline.lib.values import merge_values`
4. Replace `_inject_image_into_values()` subprocess block with a direct `merge_values(...)` call:

```python
try:
    merge_values(REPO_ROOT / "config" / "env_defaults.yaml",
                 alg_values_path, values_path, scenario=manifest["scenario"])
except (FileNotFoundError, yaml.YAMLError, ValueError, OSError) as e:
    err(f"merge-values failed: {e}")
    sys.exit(1)
```

(Read `pipeline/deploy.py` first to locate `_inject_image_into_values` and confirm the exact call sites.)

- [ ] **Step 6: Run full pipeline test suite**

```bash
python -m pytest pipeline/ -v
```

Expected: all existing tests pass (no regressions).

- [ ] **Step 7: Commit**

```bash
git add pipeline/lib/values.py pipeline/tests/test_values.py pipeline/prepare.py pipeline/tests/test_prepare.py
git commit -m "feat: add pipeline/lib/values.py, remove transfer_cli.py subprocess for merge-values"
```

---

## Chunk 2: pipeline/lib/tekton.py

Port `compile_pipeline` from `transfer_cli.py:cmd_compile_pipeline` (line 1598) and add new `make_pipelinerun` function.

### Task 3: Failing tests for tekton.py

**Files:**
- Create: `pipeline/tests/test_tekton.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for pipeline/lib/tekton.py."""
import json
import yaml
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from pipeline.lib.tekton import compile_pipeline, make_pipelinerun


# ── compile_pipeline ──────────────────────────────────────────────────────────

def test_compile_pipeline_returns_false_when_tektonc_missing(tmp_path):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "pipeline.yaml.j2").write_text("kind: Pipeline")
    values_path = tmp_path / "values.yaml"
    values_path.write_text("stack: {}")

    # tektonc submodule path won't exist in tmp_path
    result = compile_pipeline(template_dir, values_path, "baseline", tmp_path / "out",
                               tektonc_root=tmp_path)
    assert result is False


def test_compile_pipeline_returns_true_on_success(tmp_path):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "pipeline.yaml.j2").write_text("kind: Pipeline")
    values_path = tmp_path / "values.yaml"
    values_path.write_text("stack: {}")
    out_dir = tmp_path / "out"

    mock_result = MagicMock()
    mock_result.returncode = 0

    tektonc_root = tmp_path
    (tektonc_root / "tektonc-data-collection" / "tektonc").mkdir(parents=True)
    tektonc_py = tektonc_root / "tektonc-data-collection" / "tektonc" / "tektonc.py"
    tektonc_py.write_text("")

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        result = compile_pipeline(template_dir, values_path, "baseline", out_dir,
                                   tektonc_root=tektonc_root)
    assert result is True
    # Verify tektonc.py was invoked with the correct -t / -f / -o flags
    call_args = mock_run.call_args[0][0]  # first positional arg = command list
    assert str(tektonc_py) in call_args
    assert "-t" in call_args
    assert "-f" in call_args
    assert "-o" in call_args
    expected_out = str(out_dir / "baseline-pipeline.yaml")
    assert expected_out in call_args


def test_compile_pipeline_returns_false_on_subprocess_failure(tmp_path):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "pipeline.yaml.j2").write_text("kind: Pipeline")
    values_path = tmp_path / "values.yaml"
    values_path.write_text("stack: {}")

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "compilation error"

    tektonc_root = tmp_path
    (tektonc_root / "tektonc-data-collection" / "tektonc").mkdir(parents=True)
    (tektonc_root / "tektonc-data-collection" / "tektonc" / "tektonc.py").write_text("")

    with patch("subprocess.run", return_value=mock_result):
        result = compile_pipeline(template_dir, values_path, "baseline", tmp_path / "out",
                                   tektonc_root=tektonc_root)
    assert result is False


# ── make_pipelinerun ──────────────────────────────────────────────────────────

def test_make_pipelinerun_returns_valid_k8s_resource():
    workload = {"name": "wl-short", "num_requests": 100}
    pr = make_pipelinerun("baseline", workload, run_name="sim2real-2026-04-09",
                           namespace="sim2real")
    assert pr["apiVersion"] == "tekton.dev/v1"
    assert pr["kind"] == "PipelineRun"
    assert pr["metadata"]["namespace"] == "sim2real"
    params = {p["name"]: p["value"] for p in pr["spec"]["params"]}
    assert params["namespace"] == "sim2real"
    assert params["runName"] == "sim2real-2026-04-09"
    assert params["workloadName"] == "wl-short"
    assert "sim2real-baseline" in pr["spec"]["pipelineRef"]["name"]


def test_make_pipelinerun_workload_name_truncated():
    workload = {"name": "very-long-workload-name-exceeding-twenty-chars", "num_requests": 10}
    pr = make_pipelinerun("treatment", workload, run_name="run", namespace="ns")
    # metadata.name must be valid k8s name (no truncation issues)
    assert len(pr["metadata"]["name"]) <= 63


def test_make_pipelinerun_includes_required_workspaces():
    workload = {"name": "wl1", "num_requests": 5}
    pr = make_pipelinerun("baseline", workload, run_name="r", namespace="n")
    workspace_names = {w["name"] for w in pr["spec"]["workspaces"]}
    assert "model-cache" in workspace_names
    assert "hf-credentials" in workspace_names
    assert "data-storage" in workspace_names


def test_make_pipelinerun_workload_spec_serialized():
    workload = {"name": "wl1", "num_requests": 50, "rate": 2.5}
    pr = make_pipelinerun("baseline", workload, run_name="r", namespace="n")
    params = {p["name"]: p["value"] for p in pr["spec"]["params"]}
    # workloadSpec should be a YAML string
    parsed = yaml.safe_load(params["workloadSpec"])
    assert parsed["num_requests"] == 50
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest pipeline/tests/test_tekton.py -v 2>&1 | head -20
```

Expected: `ImportError` — `pipeline.lib.tekton` doesn't exist yet.

### Task 4: Implement tekton.py

**Files:**
- Create: `pipeline/lib/tekton.py`

- [ ] **Step 3: Create tekton.py**

Port `compile_pipeline` from `transfer_cli.py:cmd_compile_pipeline` (lines 1598–1677), converting from CLI handler to callable function. Add `make_pipelinerun`:

```python
"""Tekton pipeline compilation and PipelineRun generation for sim2real.

compile_pipeline: ported from tools/transfer_cli.py:cmd_compile_pipeline.
make_pipelinerun: new — generates PipelineRun YAML per workload.
"""
import subprocess
import sys
import tempfile
import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def compile_pipeline(
    template_dir: Path,
    values_path: Path,
    phase: str,
    out_dir: Path,
    tektonc_root: Path | None = None,
) -> bool:
    """Compile a Tekton Pipeline YAML for the given phase.

    Augments values with synthetic keys (phase, gaie_config,
    inference_objectives), writes to a tempfile, invokes tektonc.py.

    Returns True on success, False on any failure.
    Caller writes a stub file on False.
    """
    root = tektonc_root or REPO_ROOT
    tektonc = root / "tektonc-data-collection" / "tektonc" / "tektonc.py"
    if not tektonc.exists():
        return False

    if not Path(template_dir).is_dir():
        return False

    unified = Path(template_dir) / "pipeline.yaml.j2"
    phase_tmpl = Path(template_dir) / f"{phase}-pipeline.yaml.j2"
    if unified.exists():
        template_file = unified
    elif phase_tmpl.exists():
        template_file = phase_tmpl
    else:
        return False

    try:
        values = yaml.safe_load(Path(values_path).read_text()) or {}
    except Exception:
        return False

    values["phase"] = phase
    gaie_key = "treatment" if phase == "treatment" else "baseline"
    values["gaie_config"] = (
        values.get("gaie", {}).get(gaie_key, {}).get("helmValues", {})
    )
    values["inference_objectives"] = (
        values.get("stack", {}).get("gaie", {}).get("inferenceObjectives", [])
    )

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    out_file = Path(out_dir) / f"{phase}-pipeline.yaml"

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    try:
        yaml.dump(values, tmp, default_flow_style=False, allow_unicode=True)
        tmp.flush()
        tmp.close()
        try:
            r = subprocess.run(
                [sys.executable, str(tektonc),
                 "-t", str(template_file),
                 "-f", tmp.name,
                 "-o", str(out_file)],
                capture_output=True, text=True, shell=False, timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        return r.returncode == 0
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def make_pipelinerun(
    phase: str,
    workload: dict,
    run_name: str,
    namespace: str,
) -> dict:
    """Generate a Tekton PipelineRun dict for one workload.

    Args:
        phase: "baseline" or "treatment"
        workload: workload spec dict (must have "name" key)
        run_name: experiment/run identifier
        namespace: Kubernetes namespace

    Returns:
        Dict suitable for yaml.dump → kubectl apply.
    """
    wl_name = workload.get("name", workload.get("workload_name", "unknown"))
    # k8s name: lowercase, max 63 chars
    safe_name = f"sim2real-{phase}-{wl_name}"[:63].rstrip("-").lower()

    return {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": {
            "name": safe_name,
            "namespace": namespace,
        },
        "spec": {
            "pipelineRef": {"name": f"sim2real-{phase}"},
            "params": [
                {"name": "experimentId",  "value": run_name},
                {"name": "namespace",     "value": namespace},
                {"name": "runName",       "value": run_name},
                {"name": "workloadName",  "value": wl_name},
                {"name": "workloadSpec",  "value": yaml.dump(workload,
                                                              default_flow_style=True).strip()},
                {"name": "sleepDuration", "value": "30s"},
            ],
            "workspaces": [
                {"name": "model-cache",
                 "persistentVolumeClaim": {"claimName": "model-cache"}},
                {"name": "hf-credentials",
                 "secret": {"secretName": "hf-secret"}},
                {"name": "data-storage",
                 "persistentVolumeClaim": {"claimName": "data-storage"}},
            ],
        },
    }
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest pipeline/tests/test_tekton.py -v
```

Expected: all green.

- [ ] **Step 5: Update prepare.py _compile_cluster_packages**

Replace the existing `_compile_cluster_packages` function in `pipeline/prepare.py`:

1. Add imports at top: `from pipeline.lib.tekton import compile_pipeline, make_pipelinerun`
2. Replace the function body:

```python
def _compile_cluster_packages(run_dir: Path, resolved: dict, values_path: Path,
                               setup_config: dict):
    """Compile cluster YAMLs organized by package (baseline, treatment)."""
    cluster_dir = run_dir / "cluster"
    namespace = setup_config.get("namespace", "default")
    if not setup_config.get("namespace"):
        warn("namespace not found in setup_config.json; using 'default'")

    values = yaml.safe_load(values_path.read_text())
    workloads = values.get("observe", {}).get("workloads", [])

    tektonc_dir = REPO_ROOT / "tektonc-data-collection" / "tektoncsample" / "sim2real"

    for package in ["baseline", "treatment"]:
        pkg_dir = cluster_dir / package
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # Compile Tekton Pipeline YAML
        if tektonc_dir.exists():
            ok_flag = compile_pipeline(tektonc_dir, values_path, package, pkg_dir)
            if not ok_flag:
                warn(f"compile_pipeline failed for {package}; writing stub")
                (pkg_dir / f"{package}-pipeline.yaml").write_text(
                    f"# compile_pipeline failed for {package}\n")
        else:
            (pkg_dir / f"{package}-pipeline.yaml").write_text(
                f"# PipelineRun stub for {package}\n"
                f"# tektonc-data-collection not available\n")

        # Write per-workload PipelineRun YAMLs
        for wl in workloads:
            wl_name = wl.get("name", wl.get("workload_name", "unknown"))
            pr = make_pipelinerun(package, wl, run_name=run_dir.parent.parent.name,
                                   namespace=namespace)
            safe = wl_name.replace("_", "-")
            (pkg_dir / f"pipelinerun-{safe}.yaml").write_text(
                yaml.dump(pr, default_flow_style=False, allow_unicode=True))

    ok(f"Cluster packages: {cluster_dir.relative_to(REPO_ROOT)}")
```

3. Update `_phase_assembly` to pass `setup_config` to `_compile_cluster_packages`:
```python
setup_config = _load_setup_config()
_compile_cluster_packages(run_dir, resolved, values_path, setup_config)
```

- [ ] **Step 5b: Add failing tests for _compile_cluster_packages new behavior**

Add to `TestCompileClusterPackages` (or create class) in `pipeline/tests/test_prepare.py`:

```python
class TestCompileClusterPackages:
    def test_no_epp_yaml_generated(self, repo):
        """epp.yaml must NOT be generated — EPP config lives inside pipeline YAML."""
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        values_path = run_dir / "values.yaml"
        values_path.write_text(yaml.dump({
            "observe": {"workloads": [{"name": "wl1", "num_requests": 10}]},
            "gaie": {"baseline": {}, "treatment": {}},
        }))
        resolved = {}
        setup_config = {"namespace": "sim2real"}

        mod._compile_cluster_packages(run_dir, resolved, values_path, setup_config)

        assert not (run_dir / "cluster" / "baseline" / "epp.yaml").exists()
        assert not (run_dir / "cluster" / "treatment" / "epp.yaml").exists()

    def test_pipelinerun_yaml_per_workload(self, repo):
        """One pipelinerun-{name}.yaml per workload per package."""
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        values_path = run_dir / "values.yaml"
        values_path.write_text(yaml.dump({
            "observe": {
                "workloads": [
                    {"name": "bursty", "num_requests": 50},
                    {"name": "steady", "num_requests": 100},
                ]
            },
        }))
        setup_config = {"namespace": "test-ns"}

        mod._compile_cluster_packages(run_dir, {}, values_path, setup_config)

        for phase in ["baseline", "treatment"]:
            assert (run_dir / "cluster" / phase / "pipelinerun-bursty.yaml").exists()
            assert (run_dir / "cluster" / phase / "pipelinerun-steady.yaml").exists()

    def test_pipelinerun_content_is_valid_k8s(self, repo):
        """PipelineRun YAML is a valid Tekton PipelineRun resource."""
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "my-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        values_path = run_dir / "values.yaml"
        values_path.write_text(yaml.dump({
            "observe": {"workloads": [{"name": "wl1", "num_requests": 5}]},
        }))
        setup_config = {"namespace": "myns"}

        mod._compile_cluster_packages(run_dir, {}, values_path, setup_config)

        pr_path = run_dir / "cluster" / "baseline" / "pipelinerun-wl1.yaml"
        pr = yaml.safe_load(pr_path.read_text())
        assert pr["apiVersion"] == "tekton.dev/v1"
        assert pr["kind"] == "PipelineRun"
        assert pr["metadata"]["namespace"] == "myns"
        params = {p["name"]: p["value"] for p in pr["spec"]["params"]}
        assert params["workloadName"] == "wl1"
        assert params["namespace"] == "myns"
```

Run to confirm they fail:
```bash
python -m pytest pipeline/tests/test_prepare.py::TestCompileClusterPackages -v 2>&1 | tail -20
```

- [ ] **Step 6: Run pipeline tests**

```bash
python -m pytest pipeline/ -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add pipeline/lib/tekton.py pipeline/tests/test_tekton.py pipeline/prepare.py
git commit -m "feat: add pipeline/lib/tekton.py with compile_pipeline + make_pipelinerun; drop epp.yaml"
```

---

## Chunk 3: manifest.py hints + config files

### Task 5: Failing tests for hints parsing

**Files:**
- Modify: `pipeline/tests/test_manifest.py`

- [ ] **Step 1: Add failing tests**

Append to `pipeline/tests/test_manifest.py`:

```python
# ── Hints section ─────────────────────────────────────────────────────────────

def test_hints_section_optional(tmp_path):
    """Manifest without hints loads cleanly; hints defaults to empty."""
    path = _write_manifest(tmp_path, MINIMAL_V2)
    m = load_manifest(path)
    hints = m.get("hints", {})
    assert hints.get("text", "") == ""
    assert hints.get("files", []) == []


def test_hints_text_loaded(tmp_path):
    data = {**MINIMAL_V2, "hints": {"text": "Modify precise_prefix_cache.go"}}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["hints"]["text"] == "Modify precise_prefix_cache.go"


def test_hints_files_contents_embedded(tmp_path):
    hint_file = tmp_path / "hint.md"
    hint_file.write_text("# Transfer hint\nRewrite scorer")
    data = {**MINIMAL_V2, "hints": {"files": [str(hint_file)]}}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert len(m["hints"]["files"]) == 1
    assert m["hints"]["files"][0]["path"] == str(hint_file)
    assert "Rewrite scorer" in m["hints"]["files"][0]["content"]


def test_hints_file_not_found_raises(tmp_path):
    data = {**MINIMAL_V2, "hints": {"files": ["/nonexistent/hint.md"]}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="hints.files"):
        load_manifest(path)


def test_context_notes_deprecated_warns(tmp_path, recwarn):
    data = {**MINIMAL_V2, "context": {"notes": "old style note", "files": []}}
    path = _write_manifest(tmp_path, data)
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        m = load_manifest(path)
    texts = [str(warning.message) for warning in w]
    assert any("context.notes" in t and "deprecated" in t for t in texts)
    # Value is ignored (not migrated)
    assert "notes" not in m.get("hints", {}).get("text", "")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest pipeline/tests/test_manifest.py -v -k "hints or notes" 2>&1 | tail -20
```

Expected: multiple FAILs.

### Task 6: Implement hints in manifest.py

**Files:**
- Modify: `pipeline/lib/manifest.py`

- [ ] **Step 3: Update load_manifest**

```python
import warnings  # add to imports

def load_manifest(path: "Path | str") -> dict:
    # ... existing validation unchanged until end ...

    # Hints section (optional)
    hints_raw = data.get("hints", {}) or {}
    hints_text = hints_raw.get("text", "") or ""
    hints_files_raw = hints_raw.get("files", []) or []
    hints_files = []
    for fpath in hints_files_raw:
        fp = Path(fpath)
        if not fp.exists():
            raise ManifestError(f"hints.files entry not found: {fpath}")
        hints_files.append({"path": str(fp), "content": fp.read_text()})
    data["hints"] = {"text": hints_text, "files": hints_files}

    # Deprecation warning for context.notes
    if data.get("context", {}).get("notes"):
        warnings.warn(
            "context.notes is deprecated; use hints.text instead. "
            "The value is currently ignored.",
            DeprecationWarning,
            stacklevel=2,
        )

    return data
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest pipeline/tests/test_manifest.py -v
```

Expected: all green including new hints tests.

### Task 7: Update config files

**Files:**
- Modify: `config/env_defaults.yaml`
- Modify: `config/transfer.yaml`

- [ ] **Step 5: Update env_defaults.yaml**

Read `config/env_defaults.yaml` first. For each scenario (`routing`, `admission_control`, `adaptive-routing`) **and the `common` section if applicable**, remove from `target`: `plugin_dir`, `register_file`, `rewrite_file`, `package`. Keep only `repo`. Remove `build.test_scope` from each section's `build` block.

After change, each scenario's target should look like:
```yaml
target:
  repo: llm-d-inference-scheduler
```

If `common` has no `target` block, no change needed there.

- [ ] **Step 6: Update transfer.yaml**

Add a `hints` section reflecting existing intent (currently encoded in `target.rewrite_file`):

```yaml
hints:
  text: |
    Modify the existing scorer in the target repo to implement the new
    adaptive routing algorithm. Review the context files for guidance on
    what to change.
  files:
    - sim2real_golden_correct/transfer_hint.md  # if it exists
```

Remove any `context.notes` if present.

- [ ] **Step 7: Run full test suite**

```bash
python -m pytest pipeline/ -v
```

Expected: all pass. (Config changes don't break tests since test fixtures use their own YAML.)

- [ ] **Step 8: Commit**

```bash
git add pipeline/lib/manifest.py pipeline/tests/test_manifest.py \
        config/env_defaults.yaml config/transfer.yaml
git commit -m "feat: add hints to manifest; shrink env_defaults target; update transfer.yaml"
```

---

## Chunk 4: prepare.py — Phase 3 + Phase 4 assembly

### Task 8: Update Phase 3 (skill_input.json)

**Files:**
- Modify: `pipeline/prepare.py`
- Modify: `pipeline/tests/test_prepare.py`

- [ ] **Step 1: Write failing tests for new skill_input.json shape**

In `pipeline/tests/test_prepare.py`, update `MINIMAL_ENV_DEFAULTS` fixture to remove old target fields and add a test for hints:

```python
MINIMAL_ENV_DEFAULTS = {
    "common": {
        "observe": {"request_multiplier": 10},
        "build": {
            "commands": [["go", "build", "./..."]],
        },
    },
    "scenarios": {
        "routing": {
            "target": {
                "repo": "llm-d-inference-scheduler",
            },
            "build": {},   # no test_scope
            "config": {
                "kind": "EndpointPickerConfig",
                "helm_path": "gaie.treatment.helmValues.inferenceExtension.pluginsCustomConfig.custom-plugins.yaml",
            },
            "gaie": {"baseline": {"helmValues": {}}},
        },
    },
}
```

Add to `TestPhaseTranslate`:

```python
def test_skill_input_contains_hints_empty(self, repo):
    mod = _import_prepare_with_root(repo)
    manifest = dict(MINIMAL_MANIFEST)
    run_dir = repo / "workspace" / "runs" / "test-run"
    run_dir.mkdir(parents=True, exist_ok=True)

    resolved = mod._load_resolved_config(manifest)
    state = StateMachine("test-run", "routing", run_dir)
    state.mark_done("init")
    context_path = run_dir / "context.md"
    context_path.write_text("# Context")

    class Args:
        force = False
        manifest = None

    with pytest.raises(SystemExit):
        mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)

    si = json.loads((run_dir / "skill_input.json").read_text())
    # New: hints field present
    assert "hints" in si
    assert si["hints"]["text"] == ""
    assert si["hints"]["files"] == []
    # New: target has only repo
    assert si["target"] == {"repo": "llm-d-inference-scheduler"}
    # Old fields removed
    assert "plugin_dir" not in si.get("target", {})
    assert "register_file" not in si.get("target", {})
    assert "context_notes" not in si


def test_skill_input_contains_hints_from_manifest(self, repo):
    mod = _import_prepare_with_root(repo)
    manifest = dict(MINIMAL_MANIFEST)
    manifest["hints"] = {
        "text": "Modify scorer X",
        "files": [{"path": "hint.md", "content": "# Hint content"}],
    }
    run_dir = repo / "workspace" / "runs" / "test-run"
    run_dir.mkdir(parents=True, exist_ok=True)

    resolved = mod._load_resolved_config(manifest)
    state = StateMachine("test-run", "routing", run_dir)
    state.mark_done("init")
    context_path = run_dir / "context.md"
    context_path.write_text("# Context")

    class Args:
        force = False
        manifest = None

    with pytest.raises(SystemExit):
        mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)

    si = json.loads((run_dir / "skill_input.json").read_text())
    assert si["hints"]["text"] == "Modify scorer X"
    assert si["hints"]["files"][0]["content"] == "# Hint content"
```

- [ ] **Step 2: Run new tests to confirm they fail**

```bash
python -m pytest pipeline/tests/test_prepare.py::TestPhaseTranslate -v -k "hints" 2>&1 | tail -20
```

- [ ] **Step 3: Update _phase_translate in prepare.py**

Replace the `skill_input` dict construction:

```python
skill_input = {
    "run_name": state.run_name,
    "run_dir": str(run_dir.relative_to(REPO_ROOT)),
    "scenario": manifest["scenario"],
    "context_path": str(context_path.relative_to(REPO_ROOT)
                       if context_path.is_relative_to(REPO_ROOT)
                       else context_path),
    "manifest_path": str(getattr(args, "manifest", None) or "config/transfer.yaml"),
    "algorithm_source": manifest["algorithm"]["source"],
    "algorithm_config": manifest["algorithm"]["config"],
    "target": {"repo": target.get("repo", "")},   # only repo
    "build_commands": commands,
    "config_kind": config_cfg.get("kind", ""),
    "hints": manifest.get("hints", {"text": "", "files": []}),
}
```

Remove from `skill_input`: `context_notes` (no longer written).
Remove `test_scope` appending from `commands` (lines that call `build_cfg.get("test_scope")`).

Also update validation of `translation_output.json` required fields in `_phase_translate`:

```python
for f in ["plugin_type", "files_created", "files_modified",
          "package", "test_commands", "config_kind", "helm_path",
          "treatment_config_generated", "description"]:  # full 10-field schema
    if f not in output:
        err(f"translation_output.json missing required field: {f}")
        sys.exit(1)
# register_file may be null (rewrite mode) but must be present
if "register_file" not in output:
    err("translation_output.json missing required field: register_file")
    sys.exit(1)
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest pipeline/tests/test_prepare.py -v
```

Expected: all pass including new hints tests.

### Task 9: Update Phase 4 assembly + validate_assembly

**Files:**
- Modify: `pipeline/prepare.py`
- Modify: `pipeline/tests/test_prepare.py`

- [ ] **Step 5: Write failing validate_assembly tests for new behavior**

Add to `TestValidateAssembly` in `test_prepare.py`:

```python
def test_check1_skipped_when_register_file_null(self, repo):
    """register_file=null (rewrite mode) — Check 1 skipped."""
    mod = _import_prepare_with_root(repo)
    run_dir = repo / "workspace" / "runs" / "test-run"
    run_dir.mkdir(parents=True, exist_ok=True)

    resolved = MINIMAL_ENV_DEFAULTS["scenarios"]["routing"]
    output = {
        "plugin_type": "test-scorer",
        "files_created": [],
        "files_modified": ["pkg/plugins/scorer/precise_prefix_cache.go"],
        "register_file": None,
        "treatment_config_generated": True,
    }
    (run_dir / "translation_output.json").write_text(json.dumps(output))

    # treatment pipeline YAML contains plugin_type
    pkg_dir = run_dir / "cluster" / "treatment"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "treatment-pipeline.yaml").write_text("type: test-scorer\n")

    _write_yaml(run_dir / "treatment_config.yaml", {"kind": "EndpointPickerConfig"})

    # register.go does NOT contain plugin_type — but should be ignored
    register = repo / "llm-d-inference-scheduler" / "pkg" / "plugins" / "register.go"
    register.write_text("no scorer here")

    mod._validate_assembly(run_dir, resolved)  # should not raise


def test_check2_uses_pipeline_yaml_not_epp(self, repo):
    """Check 2 now reads treatment-pipeline.yaml, not epp.yaml."""
    mod = _import_prepare_with_root(repo)
    run_dir = repo / "workspace" / "runs" / "test-run"
    run_dir.mkdir(parents=True, exist_ok=True)

    resolved = MINIMAL_ENV_DEFAULTS["scenarios"]["routing"]
    output = {
        "plugin_type": "test-scorer",
        "files_created": [],
        "files_modified": [],
        "register_file": "pkg/plugins/register.go",
        "treatment_config_generated": True,
    }
    (run_dir / "translation_output.json").write_text(json.dumps(output))

    register = repo / "llm-d-inference-scheduler" / "pkg" / "plugins" / "register.go"
    register.write_text('Register("test-scorer", NewTestScorer)')

    pkg_dir = run_dir / "cluster" / "treatment"
    pkg_dir.mkdir(parents=True)
    # Pipeline YAML contains plugin_type (no epp.yaml needed)
    (pkg_dir / "treatment-pipeline.yaml").write_text("type: test-scorer\n")

    _write_yaml(run_dir / "treatment_config.yaml", {"kind": "EndpointPickerConfig"})
    mod._validate_assembly(run_dir, resolved)


def test_check3_skipped_when_no_custom_config(self, repo):
    """treatment_config_generated=False — Check 3 (kind match) skipped."""
    mod = _import_prepare_with_root(repo)
    run_dir = repo / "workspace" / "runs" / "test-run"
    run_dir.mkdir(parents=True, exist_ok=True)

    resolved = MINIMAL_ENV_DEFAULTS["scenarios"]["routing"]
    output = {
        "plugin_type": "test-scorer",
        "files_created": [],
        "files_modified": ["pkg/plugins/scorer/precise_prefix_cache.go"],
        "register_file": "pkg/plugins/register.go",
        "treatment_config_generated": False,
    }
    (run_dir / "translation_output.json").write_text(json.dumps(output))

    register = repo / "llm-d-inference-scheduler" / "pkg" / "plugins" / "register.go"
    register.write_text('Register("test-scorer", NewTestScorer)')

    pkg_dir = run_dir / "cluster" / "treatment"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "treatment-pipeline.yaml").write_text("type: test-scorer\n")
    # No treatment_config.yaml — and treatment_config_generated=False

    mod._validate_assembly(run_dir, resolved)  # should not raise
```

- [ ] **Step 6: Run new tests to confirm they fail**

```bash
python -m pytest pipeline/tests/test_prepare.py::TestValidateAssembly -v -k "null or pipeline or skipped" 2>&1 | tail -20
```

- [ ] **Step 7: Update _validate_assembly in prepare.py**

Replace the entire `_validate_assembly` function:

```python
def _validate_assembly(run_dir: Path, resolved: dict):
    """Phase 4g: Deterministic consistency checks."""
    output = json.loads((run_dir / "translation_output.json").read_text())
    plugin_type = output["plugin_type"]
    config_cfg = resolved.get("config", {})
    target = resolved.get("target", {})
    treatment_config_generated = output.get("treatment_config_generated", True)

    errors = []

    # Check 1: plugin_type in register_file (skip if null — rewrite mode)
    register_file = output.get("register_file")
    if register_file is not None:
        register_path = REPO_ROOT / target.get("repo", "") / register_file
        if register_path.exists():
            if plugin_type not in register_path.read_text():
                errors.append(
                    f"plugin_type '{plugin_type}' not found in {register_file}")
        else:
            errors.append(f"register_file not found on disk: {register_file}")

    # Check 2: plugin_type string present inside treatment-pipeline.yaml
    # (EPP config is embedded in the compiled Pipeline YAML — no separate epp.yaml)
    # Skip when treatment_config_generated=False: baseline config is copied instead,
    # and plugin_type may not appear in the treatment pipeline YAML.
    if treatment_config_generated:
        pipeline_yaml = run_dir / "cluster" / "treatment" / "treatment-pipeline.yaml"
        if pipeline_yaml.exists():
            if plugin_type not in pipeline_yaml.read_text():
                errors.append(
                    f"plugin_type '{plugin_type}' not found in treatment-pipeline.yaml")

    # Check 3: treatment_config kind matches scenario (only if custom config generated)
    if treatment_config_generated and config_cfg.get("kind"):
        tc_path = run_dir / "treatment_config.yaml"
        if tc_path.exists():
            tc = yaml.safe_load(tc_path.read_text())
            if isinstance(tc, dict) and tc.get("kind") != config_cfg["kind"]:
                errors.append(
                    f"treatment_config kind '{tc.get('kind')}' != expected "
                    f"'{config_cfg['kind']}'")

    # Check 4: all files_created exist in target repo
    target_repo = target.get("repo", "")
    for f in output.get("files_created", []):
        if target_repo and not (REPO_ROOT / target_repo / f).exists():
            errors.append(f"files_created entry missing on disk: {f}")

    if errors:
        err("validate-assembly FAILED:")
        for e in errors:
            err(f"  - {e}")
        sys.exit(1)
    ok("validate-assembly: all checks passed")
```

- [ ] **Step 8: Update _generate_algorithm_values in prepare.py**

Replace the treatment config embedding block:

```python
# Embed treatment EPP config from treatment_config.yaml (or copy baseline)
output = json.loads((out_path.parent / "translation_output.json").read_text()) \
    if (out_path.parent / "translation_output.json").exists() else {}
treatment_config_generated = output.get("treatment_config_generated", False)

if treatment_config_generated:
    tc_path = out_path.parent / "treatment_config.yaml"
    if not tc_path.exists():
        raise RuntimeError(
            f"treatment_config_generated=true but treatment_config.yaml not found at {tc_path}")
    tc_content = tc_path.read_text()
    (alg_values["stack"]
     .setdefault("gaie", {})
     .setdefault("treatment", {})
     .setdefault("helmValues", {})
     .setdefault("inferenceExtension", {})
     ["pluginsCustomConfig"]) = {"custom-plugins.yaml": tc_content}
else:
    baseline_cfg = (resolved
                    .get("gaie", {})
                    .get("baseline", {})
                    .get("helmValues", {})
                    .get("inferenceExtension", {})
                    .get("pluginsCustomConfig", {}))
    if baseline_cfg:
        (alg_values["stack"]
         .setdefault("gaie", {})
         .setdefault("treatment", {})
         .setdefault("helmValues", {})
         .setdefault("inferenceExtension", {})
         ["pluginsCustomConfig"]) = baseline_cfg
    else:
        warn("treatment_config_generated=false and baseline has no EPP config; "
             "treatment pluginsCustomConfig will be empty")
```

- [ ] **Step 8b: Write failing tests for _generate_algorithm_values treatment_config_generated logic**

Add to `TestGenerateAlgorithmValues` (or create a new class) in `test_prepare.py`:

```python
class TestGenerateAlgorithmValues:
    def test_embeds_treatment_config_when_generated(self, repo):
        """treatment_config_generated=true → treatment slot gets custom EPP config."""
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "translation_output.json").write_text(json.dumps({
            "plugin_type": "test-scorer",
            "treatment_config_generated": True,
        }))
        tc_yaml = "kind: EndpointPickerConfig\npluginType: test-scorer\n"
        (run_dir / "treatment_config.yaml").write_text(tc_yaml)
        alg_path = run_dir / "algorithm_values.yaml"
        alg_path.write_text(yaml.dump({"stack": {"model": {}}}))
        out_path = run_dir / "alg_values_merged.yaml"
        resolved = {"gaie": {"baseline": {"helmValues": {}}}}

        mod._generate_algorithm_values(alg_path, out_path, resolved)
        result = yaml.safe_load(out_path.read_text())
        custom = (result["stack"]["gaie"]["treatment"]["helmValues"]
                  ["inferenceExtension"]["pluginsCustomConfig"])
        assert custom["custom-plugins.yaml"] == tc_yaml

    def test_copies_baseline_config_when_not_generated(self, repo):
        """treatment_config_generated=false → baseline EPP config copied to treatment."""
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "translation_output.json").write_text(json.dumps({
            "plugin_type": "test-scorer",
            "treatment_config_generated": False,
        }))
        baseline_cfg = {"custom-plugins.yaml": "baseline epp config"}
        alg_path = run_dir / "algorithm_values.yaml"
        alg_path.write_text(yaml.dump({"stack": {"model": {}}}))
        out_path = run_dir / "alg_values_merged.yaml"
        resolved = {
            "gaie": {
                "baseline": {
                    "helmValues": {
                        "inferenceExtension": {
                            "pluginsCustomConfig": baseline_cfg
                        }
                    }
                }
            }
        }

        mod._generate_algorithm_values(alg_path, out_path, resolved)
        result = yaml.safe_load(out_path.read_text())
        custom = (result["stack"]["gaie"]["treatment"]["helmValues"]
                  ["inferenceExtension"]["pluginsCustomConfig"])
        assert custom == baseline_cfg

    def test_raises_when_treatment_config_missing(self, repo):
        """treatment_config_generated=true but no treatment_config.yaml → RuntimeError."""
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "translation_output.json").write_text(json.dumps({
            "plugin_type": "test-scorer",
            "treatment_config_generated": True,
        }))
        alg_path = run_dir / "algorithm_values.yaml"
        alg_path.write_text(yaml.dump({"stack": {}}))
        out_path = run_dir / "alg_values_merged.yaml"
        resolved = {}

        with pytest.raises(RuntimeError, match="treatment_config.yaml"):
            mod._generate_algorithm_values(alg_path, out_path, resolved)

    def test_warns_when_no_baseline_and_not_generated(self, repo):
        """treatment_config_generated=false, baseline has no EPP config → warning."""
        import warnings
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "translation_output.json").write_text(json.dumps({
            "plugin_type": "test-scorer",
            "treatment_config_generated": False,
        }))
        alg_path = run_dir / "algorithm_values.yaml"
        alg_path.write_text(yaml.dump({"stack": {}}))
        out_path = run_dir / "alg_values_merged.yaml"
        resolved = {"gaie": {"baseline": {"helmValues": {}}}}  # no pluginsCustomConfig

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            mod._generate_algorithm_values(alg_path, out_path, resolved)
        texts = [str(warning.message) for warning in w]
        assert any("treatment pluginsCustomConfig" in t for t in texts)
```

Run to confirm they fail:

```bash
python -m pytest pipeline/tests/test_prepare.py::TestGenerateAlgorithmValues -v 2>&1 | tail -20
```

- [ ] **Step 9: Update existing validate_assembly tests**

First, find all existing tests to update:
```bash
grep -n "def test.*validate.*assembly\|epp.yaml\|register_file\|needs_custom_config" \
  pipeline/tests/test_prepare.py
```

Update each test that creates `epp.yaml` or uses old output schema:
1. Add `"register_file": "pkg/plugins/register.go"` and `"treatment_config_generated": True` to output dict
2. Create `cluster/treatment/treatment-pipeline.yaml` containing plugin_type instead of `epp.yaml`
3. Remove `epp.yaml` creation
4. Remove `needs_custom_config` / `suggested_config` from output dicts

- [ ] **Step 10: Run full test suite**

```bash
python -m pytest pipeline/ -v
```

Expected: all pass.

- [ ] **Step 11: Commit**

```bash
git add pipeline/prepare.py pipeline/tests/test_prepare.py
git commit -m "feat: update prepare.py Phase 3/4 for autonomous translate output schema"
```

---

## Chunk 5: SKILL.md + review.py

### Task 10: Update review.py for --hints-json

**Files:**
- Modify: `.claude/skills/sim2real-translate/scripts/review.py`

- [ ] **Step 1: Add --hints-json argument**

Read the current `review.py` to understand its argument parsing. Add:

```python
parser.add_argument("--hints-json", dest="hints_json", default=None,
                    help="JSON string with hints {text, files[{path, content}]}")
```

When building the review prompt, append hints **after** algorithm context but **before** the "Evaluate" instruction (so the reviewer knows the mandate before judging the translation). Find the `prompt_parts.append(...)` block and append after algorithm content:

```python
if args.hints_json:
    try:
        hints = json.loads(args.hints_json)
        hints_text = hints.get("text", "")
        hints_files = hints.get("files", [])
        if hints_text or hints_files:
            prompt_parts.append("\n\n## Transfer Hints (user's mandate for this run)\n")
            if hints_text:
                prompt_parts.append(hints_text)
            for f in hints_files:
                prompt_parts.append(
                    f"\n### {f.get('path', 'hint')}\n{f.get('content', '')}")
    except (json.JSONDecodeError, AttributeError):
        pass  # hints are optional; don't fail review if malformed
```

(Read `review.py` first to locate `prompt_parts` and find the correct insertion point.)

- [ ] **Step 2: Run existing review.py tests**

```bash
python -m pytest .claude/skills/sim2real-translate/tests/ -v
```

Expected: all pass (--hints-json is additive, no existing behavior broken).

### Task 11: Update SKILL.md

**Files:**
- Modify: `.claude/skills/sim2real-translate/SKILL.md`

- [ ] **Step 3: Update SKILL.md**

Make the following changes:

**Prerequisites section — validation block**: remove `plugin_dir`, `register_file`, `package`, `rewrite_file` from required target fields. New check:

```python
required = ['run_name', 'run_dir', 'scenario', 'context_path', 'manifest_path',
            'algorithm_source', 'algorithm_config', 'target', 'build_commands',
            'config_kind', 'hints']
missing = [f for f in required if f not in si]
if missing:
    print(f'HALT: skill_input.json missing fields: {missing}')
    sys.exit(1)
if 'repo' not in si['target']:
    print('HALT: skill_input.json target.repo missing')
    sys.exit(1)
```

**Shell variable loading**: remove `PLUGIN_DIR`, `REGISTER_FILE`, `PACKAGE`, `REWRITE_FILE`, `CONTEXT_NOTES`. Add:

```bash
HINTS_TEXT=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json')).get('hints', {}).get('text', ''))")
HINTS_FILES_CONTENT=$(python3 -c "
import json
hints = json.load(open('$RUN_DIR/skill_input.json')).get('hints', {}).get('files', [])
for f in hints:
    print(f'### {f[\"path\"]}')
    print(f['content'])
    print()
")
```

**Step 2 (Translate)**: Replace the "Create mode / Rewrite mode" branching with autonomous flow:

```
## Step 2: Translate

Use TaskCreate: "Step 2: Translate Algorithm" → TaskUpdate in_progress

Read these files (use the Read tool):
1. $CONTEXT_PATH — cached context document (production interfaces, examples)
2. $ALGO_SOURCE — simulated algorithm source (Go)
3. $ALGO_CONFIG — algorithm policy config (YAML with weights/thresholds)
4. prompts/prepare/translate.md — writer guidance
5. Registration files in $TARGET_REPO — discover during exploration (path unknown upfront)

Hold in mind (NOT written to disk):
- $HINTS_TEXT — user's inline transfer mandate
- $HINTS_FILES_CONTENT — contents of user hint files

Based on hints + codebase exploration, autonomously decide what to create/modify:
- Read any files in $TARGET_REPO needed to understand current structure
- Hints take priority as the mandate for what to do
- Write all changes directly to $TARGET_REPO (no restricted directories)

Write translation_output.json with all 10 fields:
{
  "plugin_type": "<kebab-case type name>",
  "files_created": [...],        // paths relative to target repo
  "files_modified": [...],       // paths relative to target repo
  "package": "<Go package>",
  "register_file": "<path or null>",
  "test_commands": [...],
  "config_kind": "<value of $CONFIG_KIND>",
  "helm_path": "gaie.treatment.helmValues...",
  "treatment_config_generated": true/false,
  "description": "<one-line summary>"
}

register_file: path to the file where the plugin was registered (relative to target repo),
  or null if no new registration was needed (rewrite mode).
treatment_config_generated: true if you wrote $RUN_DIR/treatment_config.yaml, false otherwise.
  When false, assembly will copy baseline's EPP config to the treatment slot.
```

**Step 6 (Output)**: The `generated/` copy behavior is **unchanged** — files are copied by basename (`Path(f).name`), producing `generated/adaptive.go`, `generated/adaptive_test.go`, etc. `translation_output.json` is the provenance record of full paths. No edits needed to Step 6.

**Step 5 (Review)**: Update `review.py` invocation to include `--hints-json`:

```bash
HINTS_JSON=$(python3 -c "
import json
si = json.load(open('$RUN_DIR/skill_input.json'))
print(json.dumps(si.get('hints', {'text': '', 'files': []})))
")

python3 .claude/skills/sim2real-translate/scripts/review.py \
    --plugin-files $PLUGIN_FILES \
    --algorithm "$ALGO_SOURCE" \
    --algorithm-config "$ALGO_CONFIG" \
    --context "$CONTEXT_PATH" \
    --treatment-config "$RUN_DIR/treatment_config.yaml" \
    --hints-json "$HINTS_JSON" \
    --round $ROUND_NUM \
    --out "$RUN_DIR/review/round_${ROUND_NUM}.json" \
    $([ "$DEV_MODE" = "true" ] && echo "--dev")
```

- [ ] **Step 4: Run review.py tests**

```bash
python -m pytest .claude/skills/sim2real-translate/tests/ -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/sim2real-translate/SKILL.md \
        .claude/skills/sim2real-translate/scripts/review.py
git commit -m "feat: update sim2real-translate skill for autonomous translation with hints"
```

---

## Final verification

- [ ] **Run full test suite one last time**

```bash
python -m pytest pipeline/ .claude/skills/sim2real-translate/tests/ -v
```

Expected: all green.

- [ ] **Smoke check config files parse correctly**

```bash
python3 -c "
import yaml
from pathlib import Path
env = yaml.safe_load(Path('config/env_defaults.yaml').read_text())
for scenario, cfg in env.get('scenarios', {}).items():
    target = cfg.get('target', {})
    assert 'repo' in target, f'{scenario}: missing target.repo'
    assert 'plugin_dir' not in target, f'{scenario}: stale plugin_dir still present'
    assert 'register_file' not in target, f'{scenario}: stale register_file still present'
    print(f'{scenario}: OK (target.repo={target[\"repo\"]})')
"
```

- [ ] **Smoke check transfer.yaml loads via manifest**

```bash
python3 -c "
from pipeline.lib.manifest import load_manifest
m = load_manifest('config/transfer.yaml')
assert 'text' in m['hints'] and 'files' in m['hints'], 'hints structure missing keys'
print('hints.text:', m['hints']['text'][:60] if m['hints']['text'] else '(empty)')
print('hints.files:', len(m['hints']['files']), 'file(s)')
print('scenario:', m['scenario'])
print('OK')
"
```
