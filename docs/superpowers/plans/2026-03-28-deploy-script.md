# sim2real-deploy Script Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the `sim2real-deploy` skill into `scripts/deploy.py`, a deterministic Python script matching the style of `scripts/setup.py` and `scripts/prepare.py`.

**Architecture:** Single `scripts/deploy.py` file with argparse CLI, step-labeled output, and stage functions covering: prerequisite verification, EPP image build (via build-epp.sh), Tekton cluster benchmarks (fast and full mode), and PR creation. PR creation is opt-in (`--pr` flag; default is to skip). During pipeline waits, the script prints a monitoring hint (`tkn pr logs -f`) so the user can tail logs in a second terminal. Pure data-manipulation helpers are extracted and unit-tested; cluster-interaction code follows the same pattern as `prepare.py` (no unit tests).

**Tech Stack:** Python 3.10+, stdlib only, subprocess for `kubectl`/`tkn`/`gh`/`git`, PyYAML for config reads, `tools/transfer_cli.py` for schema validation and data pipeline ops.

---

## File map

| Path | Create/Modify | Purpose |
|------|--------------|---------|
| `scripts/deploy.py` | Create | Main script — all stage logic |
| `tests/test_deploy.py` | Create | Unit tests for pure helper functions |

---

## Chunk 1: Scaffold + pure helpers + tests

### Task 1: Write failing tests for pure data helpers

**Files:**
- Create: `tests/test_deploy.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_deploy.py
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import importlib

deploy = importlib.import_module("deploy")

# ── _inject_image_reference ─────────────────────────────────────────
def test_inject_image_sets_hub_and_tag():
    alg = {"stack": {"gaie": {"treatment": {"helmValues": {}}}}}
    result = deploy._inject_image_reference(alg, "quay.io/me", "run-2026-03-28")
    hv = result["stack"]["gaie"]["treatment"]["helmValues"]
    ie = hv["inferenceExtension"]["image"]
    assert ie["hub"] == "quay.io/me"
    assert ie["tag"] == "run-2026-03-28"

def test_inject_image_preserves_other_keys():
    alg = {"stack": {"gaie": {"treatment": {"helmValues": {"foo": "bar"}}}}}
    result = deploy._inject_image_reference(alg, "hub", "tag")
    assert result["stack"]["gaie"]["treatment"]["helmValues"]["foo"] == "bar"

# ── _construct_validation_results ───────────────────────────────────
def _make_equiv(suite_a_passed=True, suite_c_passed=True):
    return {
        "suite_a": {"passed": suite_a_passed, "kendall_tau": 0.9},
        "suite_b": {"passed": True},
        "suite_c": {"passed": suite_c_passed},
    }

def test_construct_fast_mode_pass():
    val = deploy._construct_validation_results(_make_equiv(), fast_iter=True)
    assert val["overall_verdict"] == "PASS"
    assert val["suite_a"]["kendall_tau"] == 0.9

def test_construct_fast_mode_fail_suite_a():
    val = deploy._construct_validation_results(_make_equiv(suite_a_passed=False), fast_iter=True)
    assert val["overall_verdict"] == "FAIL"

def test_construct_fast_mode_fail_suite_c():
    val = deploy._construct_validation_results(_make_equiv(suite_c_passed=False), fast_iter=True)
    assert val["overall_verdict"] == "FAIL"

def test_construct_full_mode_no_overall_verdict():
    val = deploy._construct_validation_results(_make_equiv(), fast_iter=False)
    assert "overall_verdict" not in val

# ── _merge_benchmark_into_validation ────────────────────────────────
def _make_bench(verdict="PASS", noise_cv=0.05):
    return {
        "mechanism_check_verdict": verdict,
        "noise_cv": noise_cv,
        "workload_classification": [],
    }

def _make_val(suite_a_passed=True, suite_c_passed=True):
    return {
        "suite_a": {"passed": suite_a_passed},
        "suite_b": {"passed": True},
        "suite_c": {"passed": suite_c_passed},
    }

def test_merge_benchmark_pass():
    val = deploy._merge_benchmark_into_validation(_make_val(), _make_bench("PASS"))
    assert val["overall_verdict"] == "PASS"
    assert val["noise_cv"] == 0.05
    assert "noise_cv" not in val["benchmark"]
    assert val["benchmark"]["mechanism_check_verdict"] == "PASS"

def test_merge_benchmark_inconclusive():
    val = deploy._merge_benchmark_into_validation(_make_val(), _make_bench("INCONCLUSIVE"))
    assert val["overall_verdict"] == "INCONCLUSIVE"

def test_merge_benchmark_fail_verdict():
    val = deploy._merge_benchmark_into_validation(_make_val(), _make_bench("FAIL"))
    assert val["overall_verdict"] == "FAIL"

def test_merge_benchmark_fail_suite_a():
    val = deploy._merge_benchmark_into_validation(
        _make_val(suite_a_passed=False), _make_bench("PASS")
    )
    assert val["overall_verdict"] == "FAIL"
```

- [ ] **Step 2: Run tests — expect ImportError (deploy.py doesn't exist yet)**

```
python -m pytest tests/test_deploy.py -v 2>&1 | head -30
```
Expected: `ModuleNotFoundError` or import failure

---

### Task 2: Scaffold deploy.py with helpers + argparse

**Files:**
- Create: `scripts/deploy.py`

- [ ] **Step 1: Create scaffold with helpers**

```python
#!/usr/bin/env python3
"""sim2real deploy — Build EPP, Cluster Benchmarks, PR."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Repo layout ───────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = str(REPO_ROOT / ".venv/bin/python")
CLI = str(REPO_ROOT / "tools/transfer_cli.py")

# ── Color helpers ─────────────────────────────────────────────────────────────
_tty = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text

def info(msg: str) -> None: print(_c("34", "[INFO]  ") + msg)
def ok(msg: str)   -> None: print(_c("32", "[OK]    ") + msg)
def warn(msg: str) -> None: print(_c("33", "[WARN]  ") + msg)
def err(msg: str)  -> None: print(_c("31", "[ERROR] ") + msg, file=sys.stderr)

def step(n, title: str) -> None:
    print("\n" + _c("36", f"━━━ Step {n}: {title} ━━━"))

# ── Subprocess helper ─────────────────────────────────────────────────────────
def run(cmd: list[str], *, check: bool = True, capture: bool = False,
        input: str | None = None, cwd: Path | None = None,
        env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, check=check, text=True,
        capture_output=capture, input=input,
        cwd=cwd, env=env,
    )

# ── Argument parser ───────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deploy.py",
        description="sim2real deploy: Build EPP → Cluster Benchmarks → PR",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/deploy.py
  python scripts/deploy.py --skip-build-epp    # skip EPP build (already built)
  python scripts/deploy.py --no-pr             # skip PR creation
""",
    )
    p.add_argument("--skip-build-epp", action="store_true",
                   help="Skip EPP image build (use if already built this run)")
    p.add_argument("--pr", action="store_true",
                   help="Create PR after benchmarks pass (default: skip PR, review results first)")
    return p

# ── Pure data helpers (unit-tested) ─────────────────────────────────────────

def _inject_image_reference(alg_values: dict, hub: str, tag: str) -> dict:
    """Inject EPP image hub+tag into algorithm_values dict. Returns modified dict."""
    (alg_values
        .setdefault("stack", {})
        .setdefault("gaie", {})
        .setdefault("treatment", {})
        .setdefault("helmValues", {})
        .setdefault("inferenceExtension", {})
        .setdefault("image", {}))
    alg_values["stack"]["gaie"]["treatment"]["helmValues"]["inferenceExtension"]["image"].update(
        {"hub": hub, "tag": tag}
    )
    return alg_values


def _construct_validation_results(equiv: dict, fast_iter: bool) -> dict:
    """Build partial validation_results dict from equivalence gate output.

    In fast mode, adds overall_verdict. In full mode, leaves overall_verdict
    absent (set later by _merge_benchmark_into_validation).
    """
    val = {
        "suite_a": equiv["suite_a"],
        "suite_b": equiv["suite_b"],
        "suite_c": equiv["suite_c"],
    }
    if fast_iter:
        passed = (
            val.get("suite_a", {}).get("passed")
            and val.get("suite_c", {}).get("passed")
        )
        val["overall_verdict"] = "PASS" if passed else "FAIL"
    return val


def _merge_benchmark_into_validation(val: dict, bench: dict) -> dict:
    """Merge benchmark_output dict into validation_results dict.

    noise_cv goes to top-level; all other bench keys go under val['benchmark'].
    overall_verdict is derived from mechanism_check_verdict and suite results.
    """
    val["benchmark"] = {k: v for k, v in bench.items() if k != "noise_cv"}
    val["noise_cv"] = bench["noise_cv"]

    mech = bench.get("mechanism_check_verdict", "ERROR")
    if (mech == "PASS"
            and val.get("suite_a", {}).get("passed")
            and val.get("suite_c", {}).get("passed")):
        val["overall_verdict"] = "PASS"
    elif mech == "INCONCLUSIVE":
        val["overall_verdict"] = "INCONCLUSIVE"
    else:
        val["overall_verdict"] = "FAIL"
    return val


# ── Run metadata ──────────────────────────────────────────────────────────────
def update_run_metadata(run_dir: Path, stage: str = "deploy", **fields) -> None:
    meta_path = run_dir / "run_metadata.json"
    if not meta_path.exists():
        return
    meta = json.loads(meta_path.read_text())
    meta["stages"].setdefault(stage, {}).update(fields)
    meta_path.write_text(json.dumps(meta, indent=2))


def main() -> int:
    args = build_parser().parse_args()
    print(_c("36", "\n━━━ sim2real-deploy ━━━\n"))
    # TODO: implement stages
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run tests — expect failures only on missing imports**

```
python -m pytest tests/test_deploy.py -v
```
Expected: all tests PASS (helpers are now defined)

- [ ] **Step 3: Commit**

```bash
git add scripts/deploy.py tests/test_deploy.py
git commit -m "feat(deploy): scaffold deploy.py with pure helpers + tests"
```

---

## Chunk 2: Prerequisites + EPP build

### Task 3: Implement `load_setup_config` and `check_prerequisites`

**Files:**
- Modify: `scripts/deploy.py` (add `load_setup_config`, `check_prerequisites`)

- [ ] **Step 1: Add `load_setup_config`**

```python
def load_setup_config() -> tuple[dict, str, Path]:
    """Load workspace/setup_config.json. Returns (cfg, run_name, run_dir)."""
    cfg_path = REPO_ROOT / "workspace" / "setup_config.json"
    if not cfg_path.exists():
        err("workspace/setup_config.json not found — run python scripts/setup.py first")
        sys.exit(1)
    cfg = json.loads(cfg_path.read_text())
    run_name = cfg["run_name"]
    run_dir = REPO_ROOT / "workspace" / "runs" / run_name
    ok(f"Run: {run_name}  ({run_dir})")
    return cfg, run_name, run_dir
```

- [ ] **Step 2: Add `check_prerequisites` after `load_setup_config`**

```python
def check_prerequisites(run_dir: Path) -> tuple[str, bool]:
    """Verify Phase 1 artifacts and cluster readiness.

    Returns (scorer_file_path, fast_iter).
    Exits 1 on any failure.
    """
    step(0, "Checking prerequisites")

    # Phase 1 artifact files
    required = [
        run_dir / "prepare_algorithm_summary.json",
        run_dir / "prepare_signal_coverage.json",
        run_dir / "prepare_stage3_output.json",
        run_dir / "prepare_tekton" / "values.yaml",
        run_dir / "prepare_translation_reviews.json",
        run_dir / "prepare_equivalence_results.json",
    ]
    missing = [str(f) for f in required if not f.exists()]
    if missing:
        err("Missing Phase 1 artifacts (run python scripts/prepare.py first):")
        for m in missing:
            print(f"  • {m}")
        sys.exit(1)

    # Schema validation for JSON artifacts
    for art in ["prepare_algorithm_summary.json", "prepare_signal_coverage.json",
                "prepare_stage3_output.json"]:
        result = run([VENV_PYTHON, CLI, "validate-schema", str(run_dir / art)],
                     check=False, capture=True)
        if result.returncode != 0:
            err(f"Schema validation failed: {art}")
            print(result.stderr)
            sys.exit(1)

    # AI review verdict check
    reviews = json.loads((run_dir / "prepare_translation_reviews.json").read_text())
    if reviews.get("final_verdict") != "consistent":
        err("AI review verdict is not 'consistent' — re-run prepare or investigate reviews")
        sys.exit(1)

    # Equivalence gate check (Suite A + C required)
    equiv = json.loads((run_dir / "prepare_equivalence_results.json").read_text())
    if not equiv.get("suite_a", {}).get("passed"):
        err("Suite A did not pass — re-run prepare equivalence gate")
        sys.exit(1)
    if not equiv.get("suite_c", {}).get("passed"):
        err("Suite C did not pass — re-run prepare equivalence gate")
        sys.exit(1)

    # Scorer file exists and builds
    stage3 = json.loads((run_dir / "prepare_stage3_output.json").read_text())
    scorer_file = stage3["scorer_file"]
    if not Path(scorer_file).exists():
        err(f"Scorer file missing: {scorer_file}")
        sys.exit(1)
    result = run(
        ["go", "build", "./..."],
        check=False, capture=True,
        cwd=REPO_ROOT / "llm-d-inference-scheduler",
        env={**os.environ, "GOWORK": "off"},
    )
    if result.returncode != 0:
        err("Scorer build failed:")
        print(result.stderr)
        sys.exit(1)

    # Registry configuration check
    try:
        import yaml
        cfg = yaml.safe_load((REPO_ROOT / "config" / "env_defaults.yaml").read_text())
        hub = cfg.get("stack", {}).get("gaie", {}).get("epp_image", {}).get("build", {}).get("hub", "")
        if not hub or "REPLACE_ME" in hub:
            err("Set epp_image.build.hub in config/env_defaults.yaml before deploying")
            sys.exit(1)
        fast_iter = bool(cfg.get("pipeline", {}).get("fast_iteration", True))
    except Exception as e:
        err(f"Cannot read config/env_defaults.yaml: {e}")
        sys.exit(1)

    ok("All prerequisites satisfied")
    info(f"Fast iteration mode: {fast_iter}")
    return scorer_file, fast_iter
```

- [ ] **Step 3: Wire into `main()`**

```python
def main() -> int:
    args = build_parser().parse_args()
    print(_c("36", "\n━━━ sim2real-deploy ━━━\n"))

    cfg, run_name, run_dir = load_setup_config()
    namespace = cfg["namespace"]

    update_run_metadata(run_dir, status="in_progress",
                        started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    scorer_file, fast_iter = check_prerequisites(run_dir)

    # TODO: remaining stages
    return 0
```

- [ ] **Step 4: Smoke test (no cluster needed)**

```bash
# Should print "[ERROR] workspace/setup_config.json not found" and exit 1
python scripts/deploy.py 2>&1 | head -5
```

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy.py
git commit -m "feat(deploy): add load_setup_config and check_prerequisites"
```

---

### Task 4: Implement `stage_build_epp`

**Files:**
- Modify: `scripts/deploy.py` (add `stage_build_epp`)

The EPP build stage calls the existing `build-epp.sh`, then injects the image reference
into `prepare_tekton/algorithm_values.yaml`, re-merges values, and compiles+applies
Tekton pipeline YAMLs to the cluster.

- [ ] **Step 1: Implement `stage_build_epp`**

```python
def stage_build_epp(run_dir: Path, run_name: str, namespace: str) -> str:
    """Build EPP image on-cluster, update algorithm_values, re-merge, compile+apply pipelines.

    Returns the full image reference (e.g. quay.io/me/llm-d:run-name).
    """
    step(1, "Build EPP Image (in-cluster via BuildKit)")

    # Locate build-epp.sh (inside the skill directory)
    build_script_candidates = list(REPO_ROOT.glob("**/.claude/skills/sim2real-deploy/scripts/build-epp.sh"))
    if not build_script_candidates:
        err("build-epp.sh not found — expected at .claude/skills/sim2real-deploy/scripts/build-epp.sh")
        sys.exit(1)
    build_script = build_script_candidates[0]

    result = run(
        ["bash", str(build_script),
         "--run-dir", str(run_dir),
         "--run-name", run_name,
         "--namespace", namespace],
        check=False,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        err("EPP build failed — see output above")
        sys.exit(1)

    # Read image reference written by build-epp.sh into run_metadata.json
    meta = json.loads((run_dir / "run_metadata.json").read_text())
    full_image = meta.get("epp_image", "")
    if not full_image:
        err("build-epp.sh completed but epp_image not set in run_metadata.json")
        sys.exit(1)
    ok(f"EPP image: {full_image}")

    # Inject image reference into algorithm_values.yaml
    step("1b", "Injecting image reference into algorithm_values.yaml")
    import yaml
    alg_values_path = run_dir / "prepare_tekton" / "algorithm_values.yaml"
    alg_values = yaml.safe_load(alg_values_path.read_text())

    # hub is everything before the last ':', tag is the last ':'
    if ":" in full_image:
        hub, tag = full_image.rsplit(":", 1)
    else:
        hub, tag = full_image, run_name
    alg_values = _inject_image_reference(alg_values, hub, tag)
    alg_values_path.write_text(yaml.dump(alg_values, default_flow_style=False, sort_keys=False))
    ok("algorithm_values.yaml updated")

    # Re-merge values
    step("1c", "Re-merging values")
    values_out = run_dir / "prepare_tekton" / "values.yaml"
    run([VENV_PYTHON, CLI, "merge-values",
         "--env", str(REPO_ROOT / "config" / "env_defaults.yaml"),
         "--algorithm", str(alg_values_path),
         "--out", str(values_out)],
        cwd=REPO_ROOT)
    ok("values.yaml re-merged")

    # Compile and apply Tekton pipeline YAMLs
    step("1d", "Compiling and applying Tekton pipelines")
    pipelines_dir = run_dir / "prepare_tekton" / "pipelines"
    pipelines_dir.mkdir(parents=True, exist_ok=True)
    for phase in ["noise", "baseline", "treatment"]:
        run([VENV_PYTHON, CLI, "compile-pipeline",
             "--template-dir", str(REPO_ROOT / "tektonc-data-collection/tektoncsample/sim2real"),
             "--values", str(values_out),
             "--phase", phase,
             "--out", str(pipelines_dir)],
            cwd=REPO_ROOT)
    for yaml_file in sorted(pipelines_dir.glob("*.yaml")):
        run(["kubectl", "apply", "-f", str(yaml_file), f"-n={namespace}"])
    ok("Tekton pipelines applied to cluster")

    update_run_metadata(run_dir, last_completed_step="build_epp")
    return full_image
```

- [ ] **Step 2: Wire into `main()`**

```python
    if not args.skip_build_epp:
        full_image = stage_build_epp(run_dir, run_name, namespace)
    else:
        meta = json.loads((run_dir / "run_metadata.json").read_text())
        full_image = meta.get("epp_image", "")
        if not full_image:
            err("--skip-build-epp set but no epp_image in run_metadata.json")
            sys.exit(1)
        info(f"Skipping EPP build. Using image: {full_image}")
```

- [ ] **Step 3: Commit**

```bash
git add scripts/deploy.py
git commit -m "feat(deploy): add stage_build_epp"
```

---

## Chunk 3: Cluster benchmarks

### Task 5: Implement pipeline phase runner helpers

**Files:**
- Modify: `scripts/deploy.py` (add `_run_pipeline_phase`, `_extract_phase_results`)

These are reused for noise (repeated), baseline, and treatment phases.

- [ ] **Step 1: Add `_run_pipeline_phase`**

```python
PIPELINE_TIMEOUT_SECS = 14400  # 4 hours

def _run_pipeline_phase(phase: str, pipelinerun_name: str, namespace: str,
                        run_dir: Path, run_index: int = 0) -> None:
    """Submit a Tekton PipelineRun and wait for it to complete.

    Updates benchmark-state on success/failure. Exits 1 on failure or timeout.
    """
    pipelines_dir = run_dir / "prepare_tekton" / "pipelines"
    pipeline_yaml = pipelines_dir / f"{phase}-pipeline.yaml"
    values_path = run_dir / "prepare_tekton" / "values.yaml"

    # Apply pipeline definition
    result = run(["kubectl", "apply", "-f", str(pipeline_yaml), f"-n={namespace}"],
                 check=False, capture=True)
    if result.returncode != 0:
        err(f"kubectl apply failed for {phase} pipeline: {result.stderr}")
        sys.exit(1)

    # Render PipelineRun
    pipelinerun_yaml = f"/tmp/pipelinerun-{phase}-{run_index}.yaml"
    result = run(
        [VENV_PYTHON, CLI, "render-pipelinerun",
         "--template", str(run_dir / "prepare_tekton" / f"pipelinerun-{phase}.yaml"),
         "--vars", f"PIPELINERUN_NAME={pipelinerun_name}",
                   f"NAMESPACE={namespace}",
                   f"PHASE={phase}",
                   f"RUN_INDEX={run_index}",
         "--out", pipelinerun_yaml],
        check=False, capture=True, cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        err(f"render-pipelinerun failed for {phase}: {result.stderr}")
        sys.exit(1)

    # Submit PipelineRun
    result = run(["kubectl", "apply", "-f", pipelinerun_yaml, f"-n={namespace}"],
                 check=False, capture=True)
    if result.returncode != 0:
        err(f"kubectl apply pipelinerun failed for {phase}: {result.stderr}")
        sys.exit(1)

    run([VENV_PYTHON, CLI, "benchmark-state",
         "--workspace", str(run_dir.parent.parent),  # workspace/
         "--set-phase", phase, "--status", "running",
         "--pipelinerun", pipelinerun_name],
        check=False, cwd=REPO_ROOT)

    # Poll until terminal state
    info(f"Waiting for {phase} PipelineRun: {pipelinerun_name} (timeout {PIPELINE_TIMEOUT_SECS}s)...")
    info(f"  To tail logs: tkn pr logs {pipelinerun_name} -n {namespace} -f")
    import time
    elapsed = 0
    while True:
        result = run(
            ["tkn", "pr", "describe", pipelinerun_name,
             "-o", "jsonpath={.status.conditions[0].reason}",
             "-n", namespace],
            check=False, capture=True,
        )
        reason = result.stdout.strip()
        if any(t in reason for t in ["Succeeded", "Failed", "PipelineRunCancelled", "CouldntGetTask"]):
            break
        time.sleep(30)
        elapsed += 30
        if elapsed >= PIPELINE_TIMEOUT_SECS:
            run([VENV_PYTHON, CLI, "benchmark-state",
                 "--workspace", str(run_dir.parent.parent),
                 "--set-phase", phase, "--status", "failed",
                 "--failure-reason", f"Polling timeout after {PIPELINE_TIMEOUT_SECS}s on run {run_index}"],
                check=False, cwd=REPO_ROOT)
            err(f"{phase} run {run_index} timed out after {PIPELINE_TIMEOUT_SECS}s")
            sys.exit(1)

    if "Succeeded" not in reason:
        fail_result = run(
            ["tkn", "pr", "describe", pipelinerun_name,
             "-o", "jsonpath={.status.conditions[0].message}",
             "-n", namespace],
            check=False, capture=True,
        )
        fail_reason = fail_result.stdout.strip() or "PipelineRun failed"
        run([VENV_PYTHON, CLI, "benchmark-state",
             "--workspace", str(run_dir.parent.parent),
             "--set-phase", phase, "--status", "failed",
             "--failure-reason", fail_reason],
            check=False, cwd=REPO_ROOT)
        err(f"{phase} run {run_index} failed: {fail_reason}")
        sys.exit(1)

    ok(f"{phase} PipelineRun succeeded: {pipelinerun_name}")
```

- [ ] **Step 2: Add `_extract_phase_results`**

```python
def _extract_phase_results(phase: str, namespace: str, run_dir: Path) -> Path:
    """Extract results from cluster data-pvc via extractor pod.

    Returns path to validated results JSON.
    """
    import time
    pod_name = f"sim2real-extract-{phase}"

    # Clean up any leftover pod
    run(["kubectl", "delete", "pod", pod_name, "-n", namespace,
         "--ignore-not-found", "--force", "--grace-period=0"],
        check=False, capture=True)

    overrides = json.dumps({
        "spec": {
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data-pvc"}}],
            "containers": [{
                "name": "e", "image": "alpine:3.19",
                "command": ["sleep", "600"],
                "volumeMounts": [{"name": "data", "mountPath": "/data"}],
            }],
        }
    })
    run(["kubectl", "run", pod_name, "--image=alpine:3.19", "--restart=Never",
         "--overrides", overrides, "-n", namespace])
    result = run(
        ["kubectl", "wait", f"pod/{pod_name}", "--for=condition=Ready",
         "--timeout=60s", f"-n={namespace}"],
        check=False, capture=True,
    )
    if result.returncode != 0:
        err(f"Extractor pod {pod_name} not ready")
        sys.exit(1)

    raw_dir = run_dir / f"deploy_{phase}_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    result = run(
        ["kubectl", "cp", f"{namespace}/{pod_name}:/data/{phase}/", str(raw_dir), "--retries=3"],
        check=False, capture=True,
    )
    run(["kubectl", "delete", "pod", pod_name, "-n", namespace,
         "--ignore-not-found", "--force", "--grace-period=0"],
        check=False, capture=True)
    if result.returncode != 0:
        err(f"kubectl cp failed for {phase}")
        sys.exit(1)

    results_path = run_dir / f"deploy_{phase}_results.json"
    result = run(
        [VENV_PYTHON, CLI, "convert-trace",
         "--input-dir", str(raw_dir),
         "--output", str(results_path)],
        check=False, capture=True, cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        err(f"convert-trace failed for {phase}")
        sys.exit(1)

    result = run([VENV_PYTHON, CLI, "validate-schema", str(results_path)],
                 check=False, capture=True, cwd=REPO_ROOT)
    if result.returncode != 0:
        err(f"Schema validation failed for {phase}_results.json — do not mark phase done")
        sys.exit(1)

    run([VENV_PYTHON, CLI, "benchmark-state",
         "--workspace", str(run_dir.parent.parent),
         "--set-phase", phase, "--status", "done",
         "--results", str(results_path)],
        check=False, cwd=REPO_ROOT)

    ok(f"{phase} results extracted: {results_path}")
    return results_path
```

- [ ] **Step 3: Commit**

```bash
git add scripts/deploy.py
git commit -m "feat(deploy): add pipeline phase runner and result extractor helpers"
```

---

### Task 6: Implement `stage_benchmarks` (fast + full mode)

**Files:**
- Modify: `scripts/deploy.py` (add `stage_benchmarks`)

- [ ] **Step 1: Implement `stage_benchmarks`**

```python
def stage_benchmarks(run_dir: Path, namespace: str, fast_iter: bool) -> str:
    """Run cluster benchmarks. Returns overall_verdict string."""
    step(2, f"Cluster Benchmarks (fast_iteration={fast_iter})")

    equiv_path = run_dir / "prepare_equivalence_results.json"
    equiv = json.loads(equiv_path.read_text())
    workspace_dir = run_dir.parent.parent  # sim2real/workspace/

    if fast_iter:
        info("FAST MODE: Skipping noise gate and mechanism check (pipeline.fast_iteration=true)")
        val = _construct_validation_results(equiv, fast_iter=True)
        val_path = run_dir / "deploy_validation_results.json"
        val_path.write_text(json.dumps(val, indent=2))
        ok(f"Wrote deploy_validation_results.json (fast mode, overall_verdict={val['overall_verdict']})")
    else:
        # Full mode: write partial validation_results without overall_verdict
        val = _construct_validation_results(equiv, fast_iter=False)
        val_path = run_dir / "deploy_validation_results.json"
        val_path.write_text(json.dumps(val, indent=2))

    # ── Run phases ──────────────────────────────────────────────────────────
    # Check benchmark-state to determine which phases still need to run
    import time

    phases_to_run = [] if fast_iter else ["noise"]
    phases_to_run += ["baseline", "treatment"]

    if not fast_iter:
        # Initialize benchmark state
        result = run(
            [VENV_PYTHON, CLI, "benchmark-state",
             "--workspace", str(workspace_dir), "--namespace", namespace],
            check=False, capture=True, cwd=REPO_ROOT,
        )
        if result.returncode == 2:
            err("benchmark-state failed — missing workspace/algorithm_summary.json")
            sys.exit(1)
        elif result.returncode != 0:
            err(f"benchmark-state failed (exit {result.returncode})")
            sys.exit(1)

    for phase in phases_to_run:
        # Check if already done
        bench_state_file = workspace_dir / "benchmark_state.json"
        if bench_state_file.exists():
            state = json.loads(bench_state_file.read_text())
            if state.get("phases", {}).get(phase, {}).get("status") == "done":
                info(f"Phase {phase} already done — skipping")
                if phase not in ("noise",):
                    results_path = run_dir / f"deploy_{phase}_results.json"
                continue

        if phase == "noise":
            _run_noise_phase(run_dir, namespace, workspace_dir)
        else:
            # Pre-flight
            result = run(
                [VENV_PYTHON, CLI, "preflight",
                 "--phase", phase,
                 "--values", str(run_dir / "prepare_tekton" / "values.yaml"),
                 "--namespace", namespace],
                check=False, capture=True, cwd=REPO_ROOT,
            )
            if result.returncode != 0:
                err(f"Preflight failed for {phase}")
                sys.exit(1)

            pipelinerun_name = f"sim2real-{phase}-{int(time.time())}"
            _run_pipeline_phase(phase, pipelinerun_name, namespace, run_dir)
            _extract_phase_results(phase, namespace, run_dir)

    # ── Mechanism check (full mode only) ────────────────────────────────────
    if not fast_iter:
        step("2b", "Mechanism check")
        bench_out = run_dir / "deploy_benchmark_output.json"
        result = run(
            [VENV_PYTHON, CLI, "benchmark",
             "--noise", str(run_dir / "deploy_noise_results.json"),
             "--baseline", str(run_dir / "deploy_baseline_results.json"),
             "--treatment", str(run_dir / "deploy_treatment_results.json"),
             "--signal-coverage", str(run_dir / "prepare_signal_coverage.json"),
             "--workloads-dir", str(REPO_ROOT / "blis_router/workloads/"),
             "--out", str(bench_out)],
            check=False, capture=True, cwd=REPO_ROOT,
        )
        if result.returncode == 1:
            err("Mechanism check FAIL — see deploy_benchmark_output.json")
            sys.exit(1)
        if result.returncode == 2:
            err("Mechanism check infrastructure error")
            sys.exit(1)

        bench = json.loads(bench_out.read_text())
        mech_verdict = bench.get("mechanism_check_verdict", "ERROR")
        if mech_verdict == "INCONCLUSIVE":
            warn("Mechanism check verdict is INCONCLUSIVE.")
            warn("Options: 1) Re-run later  2) Inspect workloads  3) Accept as soft-pass")
            warn("For option 3: set operator_notes in deploy_validation_results.json manually,")
            warn("then re-run with --skip-build-epp to proceed to PR creation.")
            sys.exit(1)

        # Merge benchmark into validation_results
        val = json.loads(val_path.read_text())
        val = _merge_benchmark_into_validation(val, bench)
        val_path.write_text(json.dumps(val, indent=2))
        ok(f"Mechanism check: {mech_verdict}")

    # ── Comparison table ────────────────────────────────────────────────────
    step("2c", "Comparison table")
    table_path = run_dir / "deploy_comparison_table.txt"
    result = run(
        [VENV_PYTHON, CLI, "compare",
         "--baseline", str(run_dir / "deploy_baseline_results.json"),
         "--treatment", str(run_dir / "deploy_treatment_results.json"),
         "--out", str(table_path)],
        check=False, capture=True, cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        err("compare failed")
        sys.exit(1)
    if table_path.exists():
        print(table_path.read_text())

    update_run_metadata(run_dir, last_completed_step="benchmarks")

    verdict = json.loads(val_path.read_text()).get("overall_verdict", "UNKNOWN")
    ok(f"Benchmarks complete. overall_verdict: {verdict}")
    return verdict
```

- [ ] **Step 2: Add `_run_noise_phase` helper**

```python
def _run_noise_phase(run_dir: Path, namespace: str, workspace_dir: Path) -> None:
    """Run the sequential noise characterization loop."""
    import time
    import yaml

    values_path = run_dir / "prepare_tekton" / "values.yaml"
    values = yaml.safe_load(values_path.read_text())
    noise_runs = values.get("observe", {}).get("noise_runs", 3)
    info(f"Running {noise_runs} noise characterization runs...")

    for i in range(noise_runs):
        pipelinerun_name = f"sim2real-noise-run{i}-{int(time.time())}"
        info(f"Noise run {i} of {noise_runs - 1}: {pipelinerun_name}")

        # Pre-flight (retry once if transitient infra teardown)
        for attempt in range(2):
            result = run(
                [VENV_PYTHON, CLI, "preflight",
                 "--phase", "noise",
                 "--values", str(values_path),
                 "--namespace", namespace],
                check=False, capture=True, cwd=REPO_ROOT,
            )
            if result.returncode == 0:
                break
            if attempt == 0:
                warn("Preflight failed, retrying in 30s...")
                time.sleep(30)
            else:
                err("Preflight failed for noise phase")
                sys.exit(1)

        _run_pipeline_phase("noise", pipelinerun_name, namespace, run_dir, run_index=i)

    # Extract all noise runs at once via extractor pod
    _extract_phase_results("noise", namespace, run_dir)
    # Rename deploy path to align naming: deploy_noise_results.json
    raw_out = run_dir / "deploy_noise_results.json"
    extracted = run_dir / "deploy_noise_results.json"
    ok(f"Noise characterization complete: {extracted}")
```

- [ ] **Step 3: Wire `stage_benchmarks` into `main()`**

```python
    verdict = stage_benchmarks(run_dir, namespace, fast_iter)
```

- [ ] **Step 4: Commit**

```bash
git add scripts/deploy.py
git commit -m "feat(deploy): add stage_benchmarks (fast + full mode)"
```

---

## Chunk 4: PR creation + completion

### Task 7: Implement `stage_pr` and complete `main()`

**Files:**
- Modify: `scripts/deploy.py` (add `stage_pr`, complete `main()`)

- [ ] **Step 1: Implement `stage_pr`**

```python
def stage_pr(run_dir: Path) -> str | None:
    """Create PR in llm-d-inference-scheduler. Returns PR URL or None (fast mode / skipped)."""
    step(3, "PR Creation")

    # Fast-iteration check
    try:
        import yaml
        cfg = yaml.safe_load((REPO_ROOT / "config" / "env_defaults.yaml").read_text())
        fast_iter = bool(cfg.get("pipeline", {}).get("fast_iteration", True))
    except Exception as e:
        err(f"Cannot read config/env_defaults.yaml: {e}"); sys.exit(1)

    if fast_iter:
        info("Fast-iteration mode: PR creation skipped.")
        info("Review deploy_comparison_table.txt and set pipeline.fast_iteration=false when ready.")
        return None

    # Prerequisite artifacts
    val_path = run_dir / "deploy_validation_results.json"
    result = run([VENV_PYTHON, CLI, "validate-schema", str(val_path)],
                 check=False, capture=True, cwd=REPO_ROOT)
    if result.returncode != 0:
        err("deploy_validation_results.json missing or invalid"); sys.exit(1)

    evidence_path = run_dir / "deploy_transfer_evidence.md"
    if not evidence_path.exists() or not evidence_path.read_text().strip():
        # Generate evidence if missing
        result = run(
            [VENV_PYTHON, CLI, "generate-evidence",
             "--workspace", str(run_dir.parent.parent),
             "--out", str(evidence_path)],
            check=False, capture=True, cwd=REPO_ROOT,
        )
        if result.returncode != 0:
            err("generate-evidence failed"); sys.exit(1)

    val = json.loads(val_path.read_text())
    verdict = val.get("overall_verdict", "")

    if verdict == "FAIL":
        err("overall_verdict is FAIL — do not create PR"); sys.exit(1)
    if verdict == "INCONCLUSIVE":
        if not val.get("operator_notes", "").strip():
            err("overall_verdict is INCONCLUSIVE but operator_notes is absent or empty.")
            err("Set operator_notes in deploy_validation_results.json before re-running.")
            sys.exit(1)
        warn(f"Proceeding with INCONCLUSIVE verdict under operator sign-off: {val['operator_notes']}")
    elif verdict != "PASS":
        err(f"Unexpected overall_verdict: '{verdict}'"); sys.exit(1)

    # gh auth check
    result = run(["gh", "auth", "status"], check=False, capture=True)
    if result.returncode != 0:
        err("gh auth check failed — run 'gh auth login' and retry"); sys.exit(1)

    # Push branch
    alg_name = json.loads((run_dir / "prepare_algorithm_summary.json").read_text())["algorithm_name"]
    branch = f"transfer/{alg_name}"
    scheduler_dir = REPO_ROOT / "llm-d-inference-scheduler"

    # Check if branch already exists on remote
    result = run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
        check=False, capture=True, cwd=scheduler_dir,
    )
    if result.returncode == 0:
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        branch = f"{branch}-{timestamp}"
        warn(f"Branch already exists — using timestamped branch: {branch}")

    run(["git", "checkout", "-b", branch], check=False, cwd=scheduler_dir)
    result = run(["git", "push", "origin", branch], check=False, capture=True, cwd=scheduler_dir)
    if result.returncode != 0:
        err(f"git push failed for branch {branch}"); sys.exit(1)
    ok(f"Pushed branch: {branch}")

    # Append calibration log
    result = run(
        [VENV_PYTHON, CLI, "append-calibration-log",
         "--workspace", str(run_dir.parent.parent),
         "--calibration-log", str(REPO_ROOT / "docs/transfer/calibration_log.md")],
        check=False, capture=True, cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        err("append-calibration-log failed — inspect calibration_log.md before continuing")
        sys.exit(1)
    ok("Calibration log appended")

    # Create PR
    suite_a_tau = val.get("suite_a", {}).get("kendall_tau", "N/A")
    suite_c_pass = str(val.get("suite_c", {}).get("passed", False)).lower()
    mech = val.get("benchmark", {}).get("mechanism_check_verdict", "N/A")
    evidence = evidence_path.read_text()

    pr_body = f"""## Summary

Sim-to-production transfer: `{alg_name}`

**Validation:**
- Suite A Kendall-tau: `{suite_a_tau}` (threshold: 0.8)
- Suite C concurrent safety: `{suite_c_pass}`
- Mechanism check: `{mech}`
- Overall verdict: `{verdict}`

## Evidence

{evidence}

## Rollback

To disable: in EndpointPickerConfig, set `parameters.enabled: false` on the blis-weighted-scorer plugin entry.
"""

    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(pr_body)
        body_file = f.name

    try:
        result = run(
            ["gh", "pr", "create",
             "--title", f"feat(scorer): add {alg_name} sim-to-production scorer plugin",
             "--base", "main",
             "--head", branch,
             "--body-file", body_file],
            check=False, capture=True, cwd=scheduler_dir,
        )
        if result.returncode != 0:
            err(f"gh pr create failed. Branch '{branch}' is already pushed — create PR manually.")
            sys.exit(1)
    finally:
        Path(body_file).unlink(missing_ok=True)

    pr_url_result = run(["gh", "pr", "view", "--json", "url", "-q", ".url"],
                        check=False, capture=True, cwd=scheduler_dir)
    pr_url = pr_url_result.stdout.strip()
    ok(f"PR created: {pr_url}")
    return pr_url
```

- [ ] **Step 2: Complete `main()`**

```python
def main() -> int:
    args = build_parser().parse_args()
    print(_c("36", "\n━━━ sim2real-deploy ━━━\n"))

    cfg, run_name, run_dir = load_setup_config()
    namespace = cfg["namespace"]

    update_run_metadata(run_dir, status="in_progress",
                        started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    scorer_file, fast_iter = check_prerequisites(run_dir)

    if not args.skip_build_epp:
        full_image = stage_build_epp(run_dir, run_name, namespace)
    else:
        meta = json.loads((run_dir / "run_metadata.json").read_text())
        full_image = meta.get("epp_image", "")
        if not full_image:
            err("--skip-build-epp set but no epp_image in run_metadata.json"); sys.exit(1)
        info(f"Skipping EPP build. Using image: {full_image}")

    verdict = stage_benchmarks(run_dir, namespace, fast_iter)

    pr_url = None
    if args.pr:
        pr_url = stage_pr(run_dir)

    update_run_metadata(run_dir,
                        status="completed",
                        completed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        summary="Build EPP, benchmarks completed",
                        artifacts=["deploy_validation_results.json",
                                   "deploy_comparison_table.txt"])

    # ── Final summary ─────────────────────────────────────────────────────
    print()
    print(_c("32", "━━━ /sim2real-deploy complete ━━━"))
    print()
    print(f"Verdict:   {verdict}")
    print(f"EPP Image: {full_image}")
    print()
    print("Artifacts:")
    for art in ["deploy_validation_results.json", "deploy_comparison_table.txt"]:
        path = run_dir / art
        status = "✓" if path.exists() else "✗ (missing)"
        print(f"  {run_dir}/{art}  {status}")
    print()
    if not args.pr:
        print("PR: skipped (use --pr to create)")
    else:
        print(f"PR: {pr_url or 'skipped (fast_iteration mode)'}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run full test suite**

```
python -m pytest tests/test_deploy.py -v
```
Expected: all tests PASS

- [ ] **Step 4: Verify argparse help**

```bash
python scripts/deploy.py --help
```
Expected: clean usage message with `--skip-build-epp` and `--pr` (PR is opt-in)

- [ ] **Step 5: Verify error path (no setup_config.json)**

```bash
# In a temp workspace without setup_config.json:
python scripts/deploy.py 2>&1 | grep "setup_config.json"
```
Expected: `[ERROR] workspace/setup_config.json not found`

- [ ] **Step 6: Final commit**

```bash
git add scripts/deploy.py
git commit -m "feat(deploy): add stage_pr and complete main() — deploy script complete"
```

---

## Summary

After all tasks complete, `scripts/deploy.py` provides:
- `python scripts/deploy.py` — EPP build + benchmarks (no PR; review results first)
- `python scripts/deploy.py --pr` — EPP build + benchmarks + PR creation
- `python scripts/deploy.py --skip-build-epp` — resume benchmarks after EPP already built
- `python scripts/deploy.py --skip-build-epp --pr` — resume and create PR

During each pipeline wait, the script prints:
```
[INFO]   To tail logs: tkn pr logs <pipelinerun-name> -n <namespace> -f
```
so the user can open a second terminal and follow progress.

The skill (`/sim2real-deploy`) remains as a reference for AI agents; the script is the deterministic equivalent for direct invocation.
