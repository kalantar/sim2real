# Pipeline Self-Contained Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `pipeline/`'s subprocess dependency on `tools/transfer_cli.py` by copying the required logic directly into `pipeline/lib/`.

**Architecture:** Two new modules — `pipeline/lib/values.py` (deep-merge logic, copy-pasted verbatim from `tools/transfer_cli.py` lines 2089–2316 plus a new `merge_values()` wrapper) and `pipeline/lib/tekton.py` (compile-pipeline shim, converted from `cmd_compile_pipeline`). Both `prepare.py` and `deploy.py` then import directly instead of subprocess-calling the CLI. `tools/transfer_cli.py` is untouched.

**Tech Stack:** Python 3.10+, PyYAML, pytest, unittest.mock

---

## Chunk 1: `pipeline/lib/values.py`

**Files:**
- Create: `pipeline/lib/values.py`
- Create: `pipeline/tests/test_values.py`

---

### Task 1: Write failing tests for `values.py`

- [ ] **Step 1: Create `pipeline/tests/test_values.py`**

```python
"""Tests for pipeline/lib/values.py — deep-merge logic."""
import copy
import tempfile
from pathlib import Path

import pytest
import yaml

from pipeline.lib.values import (
    _apply_request_multiplier,
    _apply_vllm_image_override,
    _deep_merge,
    _flatten_gaie_shared,
    _merge_lists,
    merge_values,
)


# ── _merge_lists ──────────────────────────────────────────────────────────────

class TestMergeLists:
    def test_scalar_list_replaced(self):
        assert _merge_lists(["a", "b"], ["c"]) == ["c"]

    def test_scalar_overlay_replaces_dict_base(self):
        assert _merge_lists([{"name": "x"}], ["c"]) == ["c"]

    def test_explicit_clear_returns_empty(self):
        assert _merge_lists([{"name": "x"}], []) == []

    def test_named_key_merge_by_name(self):
        base = [{"name": "x", "value": 1}, {"name": "y", "value": 2}]
        overlay = [{"name": "x", "value": 99}]
        result = _merge_lists(base, overlay)
        assert result == [{"name": "x", "value": 99}, {"name": "y", "value": 2}]

    def test_named_key_merge_adds_new_entry(self):
        base = [{"name": "x", "v": 1}]
        overlay = [{"name": "x", "v": 1}, {"name": "z", "v": 3}]
        result = _merge_lists(base, overlay)
        assert len(result) == 2
        assert any(item["name"] == "z" for item in result)

    def test_positional_merge_no_common_key(self):
        base = [{"a": 1, "b": 2}]
        overlay = [{"a": 99}]
        result = _merge_lists(base, overlay)
        assert result == [{"a": 99, "b": 2}]

    def test_positional_preserves_surplus_from_base(self):
        base = [{"a": 1}, {"a": 2}]
        overlay = [{"a": 9}]
        result = _merge_lists(base, overlay)
        assert len(result) == 2
        assert result[0]["a"] == 9
        assert result[1]["a"] == 2


# ── _deep_merge ───────────────────────────────────────────────────────────────

class TestDeepMerge:
    def test_nested_dict_merge(self):
        base = {"a": {"b": 1, "c": 2}}
        overlay = {"a": {"b": 99}}
        result = _deep_merge(base, overlay)
        assert result == {"a": {"b": 99, "c": 2}}

    def test_overlay_adds_new_key(self):
        result = _deep_merge({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_does_not_mutate_base(self):
        base = {"a": {"b": 1}}
        overlay = {"a": {"b": 2}}
        _deep_merge(base, overlay)
        assert base == {"a": {"b": 1}}

    def test_does_not_mutate_overlay(self):
        base = {"a": {"b": 1}}
        overlay = {"a": {"b": 2}}
        _deep_merge(base, overlay)
        assert overlay == {"a": {"b": 2}}

    def test_list_delegated_to_merge_lists(self):
        base = {"items": [{"name": "x", "v": 1}]}
        overlay = {"items": [{"name": "x", "v": 99}]}
        result = _deep_merge(base, overlay)
        assert result["items"] == [{"name": "x", "v": 99}]


# ── _flatten_gaie_shared ──────────────────────────────────────────────────────

class TestFlattenGaieShared:
    def test_shared_helmvalues_flattened_into_phases(self):
        data = {
            "stack": {
                "gaie": {
                    "shared": {"helmValues": {"conn": 10}},
                    "baseline": {"helmValues": {"foo": "bar"}},
                    "treatment": {"helmValues": {"baz": "qux"}},
                }
            }
        }
        result = _flatten_gaie_shared(data)
        gaie = result["stack"]["gaie"]
        assert gaie["baseline"]["helmValues"] == {"conn": 10, "foo": "bar"}
        assert gaie["treatment"]["helmValues"] == {"conn": 10, "baz": "qux"}

    def test_shared_key_removed(self):
        data = {
            "stack": {
                "gaie": {
                    "shared": {"helmValues": {}},
                    "baseline": {},
                    "treatment": {},
                }
            }
        }
        result = _flatten_gaie_shared(data)
        assert "shared" not in result["stack"]["gaie"]

    def test_epp_image_key_removed(self):
        data = {
            "stack": {
                "gaie": {
                    "shared": {},
                    "epp_image": {
                        "upstream": {"hub": "ghcr.io", "name": "epp", "tag": "v1"},
                    },
                    "baseline": {},
                    "treatment": {},
                }
            }
        }
        result = _flatten_gaie_shared(data)
        assert "epp_image" not in result["stack"]["gaie"]

    def test_treatment_uses_build_image(self):
        data = {
            "stack": {
                "gaie": {
                    "shared": {},
                    "epp_image": {
                        "upstream": {"hub": "upstream.io", "name": "epp", "tag": "v1"},
                        "build": {"hub": "registry.io", "name": "custom", "tag": "abc123"},
                    },
                    "baseline": {},
                    "treatment": {},
                }
            }
        }
        result = _flatten_gaie_shared(data)
        gaie = result["stack"]["gaie"]
        t_img = gaie["treatment"]["helmValues"]["inferenceExtension"]["image"]
        b_img = gaie["baseline"]["helmValues"]["inferenceExtension"]["image"]
        assert t_img["hub"] == "registry.io"
        assert t_img["tag"] == "abc123"
        assert b_img["hub"] == "upstream.io"

    def test_noop_when_no_gaie(self):
        data = {"stack": {"model": {"name": "llama"}}}
        result = _flatten_gaie_shared(data)
        assert result == {"stack": {"model": {"name": "llama"}}}


# ── _apply_vllm_image_override ────────────────────────────────────────────────

class TestApplyVllmImageOverride:
    def test_replaces_container_image(self):
        data = {
            "stack": {
                "model": {
                    "vllm_image": "custom/vllm:latest",
                    "helmValues": {
                        "decode": {"containers": [{"image": "old/vllm:v1"}]}
                    },
                }
            }
        }
        result = _apply_vllm_image_override(data)
        containers = result["stack"]["model"]["helmValues"]["decode"]["containers"]
        assert containers[0]["image"] == "custom/vllm:latest"

    def test_strips_vllm_image_key(self):
        data = {
            "stack": {
                "model": {
                    "vllm_image": "custom/vllm:latest",
                    "helmValues": {"decode": {"containers": [{"image": "old"}]}},
                }
            }
        }
        result = _apply_vllm_image_override(data)
        assert "vllm_image" not in result["stack"]["model"]

    def test_noop_when_key_absent(self):
        data = {"stack": {"model": {"modelName": "llama"}}}
        result = _apply_vllm_image_override(data)
        assert result == {"stack": {"model": {"modelName": "llama"}}}


# ── _apply_request_multiplier ─────────────────────────────────────────────────

class TestApplyRequestMultiplier:
    def test_scales_num_requests(self):
        data = {
            "observe": {
                "request_multiplier": 2,
                "workloads": [{"name": "w1", "spec": "num_requests: 100\n"}],
            }
        }
        result = _apply_request_multiplier(data)
        spec = yaml.safe_load(result["observe"]["workloads"][0]["spec"])
        assert spec["num_requests"] == 200

    def test_strips_multiplier_key(self):
        data = {
            "observe": {
                "request_multiplier": 2,
                "workloads": [{"name": "w1", "spec": "num_requests: 10\n"}],
            }
        }
        result = _apply_request_multiplier(data)
        assert "request_multiplier" not in result["observe"]

    def test_multiplier_lte_one_no_scaling(self):
        data = {
            "observe": {
                "request_multiplier": 1,
                "workloads": [{"name": "w1", "spec": "num_requests: 100\n"}],
            }
        }
        result = _apply_request_multiplier(data)
        spec = yaml.safe_load(result["observe"]["workloads"][0]["spec"])
        assert spec["num_requests"] == 100

    def test_missing_multiplier_noop(self):
        data = {"observe": {"workloads": []}}
        result = _apply_request_multiplier(data)
        assert result == {"observe": {"workloads": []}}

    def test_non_numeric_spec_left_unchanged(self, capsys):
        data = {
            "observe": {
                "request_multiplier": 2,
                "workloads": [{"name": "w1", "spec": "{invalid yaml: ["}],
            }
        }
        result = _apply_request_multiplier(data)
        assert result["observe"]["workloads"][0]["spec"] == "{invalid yaml: ["


# ── merge_values() end-to-end ─────────────────────────────────────────────────

class TestMergeValues:
    def test_basic_merge_with_scenario(self, tmp_path):
        env = tmp_path / "env.yaml"
        env.write_text(yaml.dump({
            "common": {"x": 1},
            "scenarios": {"s1": {"y": 2}},
        }))
        alg = tmp_path / "alg.yaml"
        alg.write_text(yaml.dump({"z": 3}))
        out = tmp_path / "values.yaml"

        merge_values(env, alg, out, scenario="s1")

        result = yaml.safe_load(out.read_text())
        assert result["x"] == 1
        assert result["y"] == 2
        assert result["z"] == 3

    def test_scenario_strips_pipeline_keys(self, tmp_path):
        env = tmp_path / "env.yaml"
        env.write_text(yaml.dump({
            "common": {},
            "scenarios": {
                "s1": {
                    "target": "x",
                    "build": "y",
                    "config": "z",
                    "stack": {"a": 1},
                }
            },
        }))
        alg = tmp_path / "alg.yaml"
        alg.write_text("{}")
        out = tmp_path / "values.yaml"

        merge_values(env, alg, out, scenario="s1")

        result = yaml.safe_load(out.read_text())
        assert "target" not in result
        assert "build" not in result
        assert "config" not in result
        assert result["stack"]["a"] == 1

    def test_strips_fast_iteration_from_pipeline(self, tmp_path):
        env = tmp_path / "env.yaml"
        env.write_text(yaml.dump({
            "common": {"pipeline": {"fast_iteration": True, "sleepDuration": "30s"}},
            "scenarios": {"s1": {}},
        }))
        alg = tmp_path / "alg.yaml"
        alg.write_text("{}")
        out = tmp_path / "values.yaml"

        merge_values(env, alg, out, scenario="s1")

        result = yaml.safe_load(out.read_text())
        # fast_iteration stripped; sleepDuration kept
        assert result.get("pipeline", {}).get("fast_iteration") is None
        assert result.get("pipeline", {}).get("sleepDuration") == "30s"

    def test_raises_file_not_found_on_missing_env(self, tmp_path):
        alg = tmp_path / "alg.yaml"
        alg.write_text("{}")
        with pytest.raises(FileNotFoundError):
            merge_values(tmp_path / "missing.yaml", alg, tmp_path / "out.yaml")

    def test_raises_file_not_found_on_missing_alg(self, tmp_path):
        env = tmp_path / "env.yaml"
        env.write_text(yaml.dump({"common": {}, "scenarios": {}}))
        with pytest.raises(FileNotFoundError):
            merge_values(env, tmp_path / "missing.yaml", tmp_path / "out.yaml")

    def test_raises_value_error_on_unknown_scenario(self, tmp_path):
        env = tmp_path / "env.yaml"
        env.write_text(yaml.dump({"common": {}, "scenarios": {"s1": {}}}))
        alg = tmp_path / "alg.yaml"
        alg.write_text("{}")
        with pytest.raises(ValueError, match="s999"):
            merge_values(env, alg, tmp_path / "out.yaml", scenario="s999")

    def test_creates_output_parent_dirs(self, tmp_path):
        env = tmp_path / "env.yaml"
        env.write_text(yaml.dump({"common": {}, "scenarios": {"s1": {}}}))
        alg = tmp_path / "alg.yaml"
        alg.write_text("{}")
        out = tmp_path / "subdir" / "values.yaml"

        merge_values(env, alg, out, scenario="s1")
        assert out.exists()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /path/to/sim2real
python -m pytest pipeline/tests/test_values.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'pipeline.lib.values'`

---

### Task 2: Create `pipeline/lib/values.py`

- [ ] **Step 1: Create `pipeline/lib/values.py`**

Copy the following from `tools/transfer_cli.py`, then add the `merge_values()` function below:

```python
"""YAML deep-merge logic for pipeline/ — ported from tools/transfer_cli.py.

Copy-pasted verbatim. tools/ will be removed in a future cleanup;
this is the authoritative copy for pipeline use.
"""
import copy
from pathlib import Path

import yaml

# ── Ported verbatim from tools/transfer_cli.py lines 2089–2316 ───────────────
#    _LIST_KEY_CANDIDATES, _detect_list_key, _merge_lists, _deep_merge,
#    _flatten_gaie_shared, _apply_vllm_image_override, _apply_request_multiplier
# ─────────────────────────────────────────────────────────────────────────────
```

Then paste lines **2089–2316** from `tools/transfer_cli.py` verbatim (starting at `_LIST_KEY_CANDIDATES = ...` through the end of `_apply_request_multiplier`).

Move `import copy` and `import yaml` calls inside those functions up to the module level (they are already declared at module level above — just delete the in-function imports when they appear).

Then append the `merge_values()` public API at the bottom:

```python
# ── Public API ────────────────────────────────────────────────────────────────

def merge_values(
    env_path: Path,
    alg_path: Path,
    out_path: Path,
    scenario: str | None = None,
) -> None:
    """Deep-merge env_defaults and algorithm_values into values.yaml.

    In pipeline usage, scenario is always provided (both prepare.py and
    deploy.py always pass a scenario). The parameter is technically optional
    to allow standalone use without scenario resolution.

    Raises:
        FileNotFoundError: if env_path or alg_path does not exist
        yaml.YAMLError: if either input file fails to parse
        ValueError: if scenario is specified but not found in env_defaults
        OSError: if writing out_path fails
    """
    env_data = yaml.safe_load(env_path.read_text()) or {}
    alg_data = yaml.safe_load(alg_path.read_text()) or {}

    if scenario is not None:
        common = env_data.get("common", {})
        scenarios = env_data.get("scenarios", {})
        if scenario not in scenarios:
            raise ValueError(
                f"scenario '{scenario}' not found in env_defaults. "
                f"Available: {list(scenarios.keys())}"
            )
        env_data = _deep_merge(common, scenarios[scenario])
        for key in ("target", "build", "config"):
            env_data.pop(key, None)

    merged = _deep_merge(env_data, alg_data)

    # Strip pipeline-only keys except sleepDuration (consumed by Tekton templates)
    pipeline_config = merged.pop("pipeline", {})
    template_keys = {k: v for k, v in pipeline_config.items() if k == "sleepDuration"}
    if template_keys:
        merged["pipeline"] = template_keys

    merged = _flatten_gaie_shared(merged)
    merged = _apply_vllm_image_override(merged)
    merged = _apply_request_multiplier(merged)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.dump(merged, default_flow_style=False, sort_keys=False))
```

- [ ] **Step 2: Run tests to confirm they pass**

```bash
python -m pytest pipeline/tests/test_values.py -v
```

Expected: all tests pass. If any fail, fix `values.py` (do not modify the tests).

- [ ] **Step 3: Commit**

```bash
git add pipeline/lib/values.py pipeline/tests/test_values.py
git commit -m "feat(pipeline): add lib/values.py with deep-merge logic ported from transfer_cli.py"
```

---

## Chunk 2: `pipeline/lib/tekton.py`

**Files:**
- Create: `pipeline/lib/tekton.py`
- Create: `pipeline/tests/test_tekton.py`

---

### Task 3: Write failing tests for `tekton.py`

- [ ] **Step 1: Create `pipeline/tests/test_tekton.py`**

```python
"""Tests for pipeline/lib/tekton.py — compile-pipeline shim."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

import pipeline.lib.tekton as tekton_mod
from pipeline.lib.tekton import compile_pipeline


def _make_template_dir(tmp_path: Path, phase: str) -> Path:
    """Create a minimal template directory with a phase template."""
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / f"{phase}-pipeline.yaml.j2").write_text("# template")
    return template_dir


def _make_values_file(tmp_path: Path) -> Path:
    """Create a minimal values.yaml."""
    values = tmp_path / "values.yaml"
    values.write_text(yaml.dump({"stack": {"gaie": {}}, "observe": {}}))
    return values


def _make_fake_tektonc(tmp_path: Path) -> Path:
    """Create a fake tektonc.py at the expected submodule path."""
    tektonc_dir = tmp_path / "tektonc-data-collection" / "tektonc"
    tektonc_dir.mkdir(parents=True)
    tektonc = tektonc_dir / "tektonc.py"
    tektonc.write_text("# fake tektonc")
    return tektonc


class TestCompilePipeline:
    def test_returns_false_when_tektonc_absent(self, tmp_path):
        template_dir = _make_template_dir(tmp_path, "baseline")
        values = _make_values_file(tmp_path)
        out_dir = tmp_path / "out"
        # REPO_ROOT has no tektonc-data-collection submodule
        with patch.object(tekton_mod, "REPO_ROOT", tmp_path):
            result = compile_pipeline(template_dir, values, "baseline", out_dir)
        assert result is False

    def test_returns_true_on_subprocess_success(self, tmp_path):
        template_dir = _make_template_dir(tmp_path, "baseline")
        values = _make_values_file(tmp_path)
        out_dir = tmp_path / "out"
        _make_fake_tektonc(tmp_path)

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""

        with patch.object(tekton_mod, "REPO_ROOT", tmp_path):
            with patch("subprocess.run", return_value=mock_proc):
                result = compile_pipeline(template_dir, values, "baseline", out_dir)
        assert result is True

    def test_returns_false_on_subprocess_failure(self, tmp_path):
        template_dir = _make_template_dir(tmp_path, "treatment")
        values = _make_values_file(tmp_path)
        out_dir = tmp_path / "out"
        _make_fake_tektonc(tmp_path)

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "compilation failed"

        with patch.object(tekton_mod, "REPO_ROOT", tmp_path):
            with patch("subprocess.run", return_value=mock_proc):
                result = compile_pipeline(template_dir, values, "treatment", out_dir)
        assert result is False

    def test_uses_unified_template_when_present(self, tmp_path):
        """Prefers pipeline.yaml.j2 over {phase}-pipeline.yaml.j2."""
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "pipeline.yaml.j2").write_text("# unified")
        (template_dir / "baseline-pipeline.yaml.j2").write_text("# per-phase")
        values = _make_values_file(tmp_path)
        out_dir = tmp_path / "out"
        _make_fake_tektonc(tmp_path)

        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stderr = ""
            return m

        with patch.object(tekton_mod, "REPO_ROOT", tmp_path):
            with patch("subprocess.run", side_effect=fake_run):
                compile_pipeline(template_dir, values, "baseline", out_dir)

        # The template passed to tektonc should be pipeline.yaml.j2
        template_args = [c for c in captured_cmd if "pipeline.yaml.j2" in str(c)]
        assert any("pipeline.yaml.j2" in str(a) and "baseline-" not in str(a)
                   for a in template_args)
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest pipeline/tests/test_tekton.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'pipeline.lib.tekton'`

---

### Task 4: Create `pipeline/lib/tekton.py`

- [ ] **Step 1: Create `pipeline/lib/tekton.py`**

The content is `cmd_compile_pipeline` from `tools/transfer_cli.py` (lines 1598–1677) converted to a callable function. Key changes from the original:
- Parameters replace `args.template_dir`, `args.values`, `args.phase`, `args.out`
- Returns `bool` instead of `int` exit codes (0 → True, non-0 → False)
- Tektonc path built from `REPO_ROOT` constant (not `__file__`, since this file lives in `pipeline/lib/`)
- Imports moved to module level

```python
"""Tekton compile-pipeline shim for pipeline/ — ported from tools/transfer_cli.py.

Copy-pasted verbatim from cmd_compile_pipeline and converted to a callable.
tools/ will be removed in a future cleanup; this is the authoritative copy.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def compile_pipeline(
    template_dir: Path,
    values_path: Path,
    phase: str,
    out_dir: Path,
) -> bool:
    """Compile a Tekton PipelineRun YAML for the given phase.

    Augments values with synthetic keys (phase, gaie_config,
    inference_objectives), writes to a tempfile, and invokes
    tektonc.py as a subprocess.

    Returns True on success, False on any failure (missing tektonc,
    subprocess exit non-zero, timeout, values parse error).
    Caller is responsible for writing a stub file on False.
    """
    tektonc = REPO_ROOT / "tektonc-data-collection" / "tektonc" / "tektonc.py"
    if not tektonc.exists():
        return False

    if not template_dir.is_dir():
        return False
    if not values_path.exists():
        return False

    # Prefer unified pipeline.yaml.j2; fall back to per-phase {phase}-pipeline.yaml.j2
    unified_template = template_dir / "pipeline.yaml.j2"
    phase_template = template_dir / f"{phase}-pipeline.yaml.j2"
    if unified_template.exists():
        template_file = unified_template
    elif phase_template.exists():
        template_file = phase_template
    else:
        return False

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{phase}-pipeline.yaml"

    try:
        values = yaml.safe_load(values_path.read_text()) or {}
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
        except subprocess.TimeoutExpired:
            return False
        except OSError:
            return False

        return r.returncode == 0
    finally:
        Path(tmp.name).unlink(missing_ok=True)
```

- [ ] **Step 2: Run tests to confirm they pass**

```bash
python -m pytest pipeline/tests/test_tekton.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Run all pipeline tests to check for regressions**

```bash
python -m pytest pipeline/tests/ -v
```

Expected: all existing tests still pass.

- [ ] **Step 4: Commit**

```bash
git add pipeline/lib/tekton.py pipeline/tests/test_tekton.py
git commit -m "feat(pipeline): add lib/tekton.py with compile-pipeline shim ported from transfer_cli.py"
```

---

## Chunk 3: Update `prepare.py`, `deploy.py`, and cleanup

**Files:**
- Modify: `pipeline/prepare.py`
- Modify: `pipeline/deploy.py`
- Modify: `pipeline/tests/test_prepare.py:99`

---

### Task 5: Update `pipeline/prepare.py`

Four changes: (a) remove `CLI`/`VENV_PYTHON`, (b) delete `_deep_merge` and update `_load_resolved_config`, (c) replace `_run_merge_values` with direct call, (d) replace subprocess in `_compile_cluster_packages`.

- [ ] **Step 1: Update imports at the top of `prepare.py`**

In the `from pipeline.lib.*` import block (around line 29), add:

```python
from pipeline.lib.values import _deep_merge, merge_values
from pipeline.lib.tekton import compile_pipeline
```

- [ ] **Step 2: Delete `CLI` and `VENV_PYTHON` constants (lines 35–36)**

Remove these two lines:
```python
VENV_PYTHON = str(REPO_ROOT / ".venv/bin/python")
CLI = str(REPO_ROOT / "tools/transfer_cli.py")
```

- [ ] **Step 3: Delete the local `_deep_merge` function (lines 65–73)**

Remove the entire local function:
```python
def _deep_merge(base: dict, overlay: dict) -> dict:
    """Deep-merge overlay onto base. Returns new dict."""
    result = copy.deepcopy(base)
    for key, oval in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(oval, dict):
            result[key] = _deep_merge(result[key], oval)
        else:
            result[key] = copy.deepcopy(oval)
    return result
```

Also remove the `import copy` line (line 17) since it was only used by the local `_deep_merge`.

- [ ] **Step 4: Delete `_run_merge_values` (lines 379–391) and replace its call site**

Remove the entire function:
```python
def _run_merge_values(scenario: str, alg_values_path: Path, out_path: Path):
    """Run transfer_cli.py merge-values --scenario."""
    result = run([
        VENV_PYTHON, CLI, "merge-values",
        ...
    ], check=False, capture=True)
    if result.returncode != 0:
        err(f"merge-values failed (exit {result.returncode}):")
        err(result.stderr)
        sys.exit(1)
```

In `_phase_assembly` (around line 310), replace:
```python
_run_merge_values(manifest["scenario"], alg_values_path, values_path)
```
with:
```python
try:
    merge_values(
        REPO_ROOT / "config" / "env_defaults.yaml",
        alg_values_path,
        values_path,
        scenario=manifest["scenario"],
    )
except (FileNotFoundError, yaml.YAMLError, ValueError, OSError) as e:
    err(f"merge-values failed: {e}")
    sys.exit(1)
```

- [ ] **Step 5: Replace subprocess block in `_compile_cluster_packages`**

`tektonc_dir` (line 421) is the Jinja2 **template** directory (`tektoncsample/sim2real`), not
the tektonc binary. `compile_pipeline()` accepts it as `template_dir` and locates the tektonc
binary internally via its own `REPO_ROOT`. Keep `tektonc_dir` as-is; only change the call.

Variable mapping: `tektonc_dir` → `template_dir`, `values_path` → `values_path`,
`package` → `phase`, `pkg_dir` → `out_dir`.

In `_compile_cluster_packages` (around line 420), find the `if tektonc_dir.exists():` block:

```python
        tektonc_dir = REPO_ROOT / "tektonc-data-collection" / "tektoncsample" / "sim2real"
        if tektonc_dir.exists():
            result = run([
                VENV_PYTHON, CLI, "compile-pipeline",
                "--template-dir", str(tektonc_dir),
                "--values", str(values_path),
                "--phase", package,
                "--out", str(pkg_dir),
            ], check=False, capture=True)
            if result.returncode != 0:
                warn(f"compile-pipeline failed for {package}: {result.stderr}")
                pr_path.write_text(f"# compile-pipeline failed for {package}\n")
```

Replace with (keep `tektonc_dir` assignment, replace only the subprocess call):

```python
        tektonc_dir = REPO_ROOT / "tektonc-data-collection" / "tektoncsample" / "sim2real"
        if tektonc_dir.exists():
            success = compile_pipeline(
                template_dir=tektonc_dir,
                values_path=values_path,
                phase=package,
                out_dir=pkg_dir,
            )
            if not success:
                warn(f"compile-pipeline failed for {package}")
                pr_path.write_text(f"# compile-pipeline failed for {package}\n")
```

- [ ] **Step 6: Run existing prepare tests**

```bash
python -m pytest pipeline/tests/test_prepare.py -v
```

Expected: all tests pass. If `_deep_merge` was used in tests, update those imports too.

---

### Task 6: Update `pipeline/deploy.py`

- [ ] **Step 1: Add import at top of `deploy.py`**

In the `from pipeline.lib.*` imports (around line 17), add:

```python
from pipeline.lib.values import merge_values
```

- [ ] **Step 2: Delete `CLI` and `VENV_PYTHON` constants (lines 22–23)**

Remove:
```python
VENV_PYTHON = str(REPO_ROOT / ".venv/bin/python")
CLI = str(REPO_ROOT / "tools/transfer_cli.py")
```

- [ ] **Step 3: Replace subprocess block in `_inject_image_into_values`**

In `_inject_image_into_values` (around line 140), find:

```python
    result = run([
        VENV_PYTHON, CLI, "merge-values",
        "--env", str(env_path),
        "--algorithm", str(alg_values_path),
        "--out", str(values_path),
        "--scenario", scenario,
    ], check=False, capture=True)
    if result.returncode != 0:
        err(f"merge-values failed: {result.stderr}")
        sys.exit(1)
    ok("values.yaml re-merged with EPP image")
```

Replace with:

```python
    try:
        merge_values(env_path, alg_values_path, values_path, scenario=scenario)
    except (FileNotFoundError, yaml.YAMLError, ValueError, OSError) as e:
        err(f"merge-values failed: {e}")
        sys.exit(1)
    ok("values.yaml re-merged with EPP image")
```

Also add `import yaml` at the top of `deploy.py` if not already present (check line 13).

- [ ] **Step 4: Run all pipeline tests**

```bash
python -m pytest pipeline/tests/ -v
```

Expected: all tests pass.

---

### Task 7: Clean up `test_prepare.py` dead assignment

- [ ] **Step 1: Remove dead attribute assignments (lines 98–99)**

In `pipeline/tests/test_prepare.py`, in `_import_prepare_with_root`, remove both lines:

```python
    mod.VENV_PYTHON = "python"                              # line 98
    mod.CLI = str(repo_root / "tools" / "transfer_cli.py") # line 99
```

Both are dead assignments since `prepare.py` no longer has these attributes.

- [ ] **Step 2: Run all pipeline tests one final time**

```bash
python -m pytest pipeline/tests/ -v
```

Expected: all tests pass, no warnings about missing attributes.

- [ ] **Step 3: Commit**

```bash
git add pipeline/prepare.py pipeline/deploy.py pipeline/tests/test_prepare.py
git commit -m "refactor(pipeline): remove transfer_cli.py subprocess dependency

prepare.py and deploy.py now import merge_values/compile_pipeline
directly from pipeline/lib/ instead of subprocess-calling transfer_cli.py.
CLI and VENV_PYTHON constants removed."
```
