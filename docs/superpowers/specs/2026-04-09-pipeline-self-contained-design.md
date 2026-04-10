# Design: Self-Contained Pipeline (Remove transfer_cli.py Dependency)

**Date:** 2026-04-09
**Status:** Approved

## Problem

`pipeline/prepare.py` and `pipeline/deploy.py` invoke `tools/transfer_cli.py` via subprocess
for two subcommands: `merge-values` and `compile-pipeline`. This creates a cross-boundary
dependency — the pipeline scripts cannot run without `tools/` being present and callable, and
changes to `transfer_cli.py`'s CLI interface can silently break the pipeline.

`transfer_cli.py` is a public-facing CLI used by CI scripts and prompt stages 1–6; it is not
going away. The fix is to duplicate the relevant logic into `pipeline/lib/` so the pipeline
is self-contained, and let the two copies evolve independently.

## New Modules

### `pipeline/lib/values.py`

Owns all YAML deep-merge logic. Ported verbatim from `tools/transfer_cli.py`.

**Private functions (ported from transfer_cli.py):**

- `_detect_list_key(base_list, overlay_list) -> str | None` — detect shared key field (`name`, `mountPath`, `containerPort`) for named-key list merge
- `_merge_lists(base_list, overlay_list) -> list` — three-tier strategy: scalar lists replaced; all-dict with common key merged by key; all-dict without common key merged positionally
- `_deep_merge(base, overlay) -> dict` — recursive dict merge; delegates list handling to `_merge_lists`
- `_flatten_gaie_shared(merged) -> dict` — flatten `gaie.shared.helmValues` into each phase, inject EPP image refs, remove `gaie.shared` and `gaie.epp_image`
- `_apply_vllm_image_override(merged) -> dict` — apply `stack.model.vllm_image` to `decode.containers[0].image`; strip key from output
- `_apply_request_multiplier(merged) -> dict` — scale `num_requests` in workload specs; strip `observe.request_multiplier` from output

**Public API:**

```python
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
```

Calling sequence inside `merge_values`:
1. Load both YAML files (raise on missing / parse failure)
2. If `scenario` provided: resolve `_deep_merge(common, scenarios[scenario])`; strip pipeline-only keys (`target`, `build`, `config`)
3. `merged = _deep_merge(env_data, alg_data)`
4. Strip `pipeline.*` except `sleepDuration`
5. `_flatten_gaie_shared(merged)`
6. `_apply_vllm_image_override(merged)`
7. `_apply_request_multiplier(merged)`
8. Write `merged` to `out_path`

**Error contract:** raises Python exceptions (not `sys.exit`). Callers catch and handle.

### `pipeline/lib/tekton.py`

Thin shim ported from `tools/transfer_cli.py:cmd_compile_pipeline`.

**Public API:**

```python
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

    Returns True on success, False on failure (caller writes stub).
    tektonc.py lives in the tektonc-data-collection submodule; if it
    is absent this function returns False immediately.
    """
```

The subprocess call to `tektonc.py` is retained — tektonc is an external submodule tool,
not part of `transfer_cli.py`. Only the `transfer_cli.py` indirection is removed.

## Modified Files

### `pipeline/prepare.py`

| Before | After |
|--------|-------|
| `CLI = str(REPO_ROOT / "tools/transfer_cli.py")` | deleted |
| `VENV_PYTHON = str(REPO_ROOT / ".venv/bin/python")` | deleted |
| `_deep_merge(base, overlay)` (local, list-unaware) | deleted; import from `values.py` |
| `_load_resolved_config()` calls local `_deep_merge` | import `_deep_merge` from `values.py` |
| `_run_merge_values(scenario, alg_values_path, out_path)` | deleted; replaced by `merge_values(...)` |
| `_compile_cluster_packages()` subprocess block | replaced by `compile_pipeline(...)` call |

`_run_merge_values` is deleted entirely — its call site in `_phase_assembly` is replaced with
a direct `merge_values(...)` call wrapped in try/except.

### `pipeline/deploy.py`

| Before | After |
|--------|-------|
| `CLI = str(REPO_ROOT / "tools/transfer_cli.py")` | deleted |
| `VENV_PYTHON = str(REPO_ROOT / ".venv/bin/python")` | deleted |
| `_inject_image_into_values()` subprocess block | replaced by `merge_values(...)` call |

### `pipeline/tests/test_prepare.py`

Remove the `mod.CLI = ...` patch — `_run_merge_values` no longer exists. Any test that
previously mocked the subprocess call should instead mock `merge_values` directly (or use
a real temp-file call).

## New Tests

### `pipeline/tests/test_values.py`

Unit tests covering:

- `_merge_lists`: scalar replacement, named-key merge, positional merge, explicit-clear (`[]`)
- `_deep_merge`: nested dict merge, list delegation, independence (no mutation of inputs)
- `_flatten_gaie_shared`: shared helm flattened into both phases; `gaie.shared` removed; EPP image injection (build tag takes priority for treatment)
- `_apply_vllm_image_override`: override applied and key stripped; no-op when key absent
- `_apply_request_multiplier`: `num_requests` scaled; key stripped; non-numeric workload spec left unchanged
- `merge_values()` end-to-end: two temp YAML files merged to a third; scenario resolution; raises on missing file; raises on unknown scenario

### `pipeline/tests/test_tekton.py`

- `compile_pipeline()` returns False immediately when tektonc submodule is absent
- `compile_pipeline()` returns True and writes output file when tektonc is present (mocked subprocess returning exit 0)
- `compile_pipeline()` returns False on subprocess failure (mocked exit 1)

## What Stays in transfer_cli.py

Everything. `tools/transfer_cli.py` retains its own implementations of
`_merge_lists`, `_deep_merge`, etc. for CLI/CI use. The two copies are independent by design:
`pipeline/lib/values.py` is internal pipeline logic; `tools/transfer_cli.py` is a public-facing
CLI. They may diverge over time.

## Out of Scope

- No changes to `tools/transfer_cli.py`
- No changes to the `transfer_cli.py` subcommand interface
- No other pipeline scripts beyond `prepare.py` and `deploy.py`
- `pipeline/setup.py` (separate redesign underway)
