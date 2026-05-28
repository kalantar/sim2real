#!/usr/bin/env python3
"""sim2real prepare — 6-phase state machine for algorithm transfer.

Phases:
  1. Init       — load manifest v3, validate prerequisites
  2. Context    — assemble + cache context document
  3. Translate  — checkpoint: write skill_input.json, check for translation_output.json
  4. Assembly   — assemble resolved scenarios, generate PipelineRuns
  5. Summary    — generate run_summary.md
  6. Gate       — human review: [d]eploy / [e]dit / [q]uit

Re-running skips completed phases (tracked in .state.json).
"""

import argparse
import json
import subprocess
import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path

# Ensure repo root is on sys.path when run as a script (python pipeline/prepare.py)
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.lib.manifest import load_manifest, ManifestError
from pipeline.lib.state_machine import StateMachine
from pipeline.lib.context_builder import build_context
from pipeline.lib.tekton import make_pipelinerun_scenario
from pipeline.lib.assemble import assemble_packages, AssemblyError, inject_hf_secret_name
from pipeline.lib.epp import inject_epp_image

# ── Repo layout ──────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent

# Overridden in main() via --experiment-root (defaults to cwd).
EXPERIMENT_ROOT = REPO_ROOT




def _resolve_manifest_default(experiment_root: Path) -> Path:
    """Resolve default manifest path: transfer.yaml first, then config/transfer.yaml."""
    direct = experiment_root / "transfer.yaml"
    if direct.exists():
        return direct
    return experiment_root / "config" / "transfer.yaml"




def _display_path(p: Path) -> str:
    """Return p relative to EXPERIMENT_ROOT if possible, else REPO_ROOT, else absolute."""
    try:
        return str(p.relative_to(EXPERIMENT_ROOT))
    except ValueError:
        pass
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


# ── Color helpers ────────────────────────────────────────────────────────────
_tty = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text


def info(msg: str)  -> None: print(_c("34", "[INFO]  ") + msg)
def ok(msg: str)    -> None: print(_c("32", "[OK]    ") + msg)
def warn(msg: str)  -> None: print(_c("33", "[WARN]  ") + msg)
def err(msg: str)   -> None: print(_c("31", "[ERROR] ") + msg, file=sys.stderr)


def step(n, title: str) -> None:
    print("\n" + _c("36", f"━━━ Phase {n}: {title} ━━━"))


# ── Subprocess helper ────────────────────────────────────────────────────────

def run(cmd: list[str], *, check: bool = True, capture: bool = False,
        cwd: "Path | None" = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=capture, cwd=cwd)


# ── Config resolution ────────────────────────────────────────────────────────

def _default_run_name() -> str:
    return f"sim2real-{datetime.now().strftime('%Y-%m-%d')}"


def _load_setup_config() -> dict:
    path = EXPERIMENT_ROOT / "workspace" / "setup_config.json"
    if not path.exists():
        path = REPO_ROOT / "workspace" / "setup_config.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _load_resolved_config(manifest: dict) -> dict:
    """Build resolved config from manifest component section."""
    return dict(manifest.get("component", {}))


def _get_submodule_shas(component_path: str = "") -> dict[str, str]:
    """Get HEAD commit SHAs for submodules.

    Args:
        component_path: Relative path to the component submodule (from manifest).
                        Resolved relative to EXPERIMENT_ROOT (with REPO_ROOT fallback).
    """
    shas = {}
    # Framework submodules (always at REPO_ROOT)
    for name in ("inference-sim", "llm-d-benchmark"):
        sub = REPO_ROOT / name
        if sub.exists() and (sub / ".git").exists():
            try:
                result = run(["git", "rev-parse", "HEAD"], capture=True, cwd=sub)
                shas[name] = result.stdout.strip()
            except subprocess.CalledProcessError:
                shas[name] = "unknown"
        else:
            shas[name] = "unknown"

    # Component submodule (from manifest, EXPERIMENT_ROOT with fallback)
    if component_path:
        sub = EXPERIMENT_ROOT / component_path
        if not sub.exists():
            sub = REPO_ROOT / component_path
        if sub.exists() and (sub / ".git").exists():
            try:
                result = run(["git", "rev-parse", "HEAD"], capture=True, cwd=sub)
                shas["component"] = result.stdout.strip()
            except subprocess.CalledProcessError:
                shas["component"] = "unknown"
        else:
            shas["component"] = "unknown"
    else:
        shas["component"] = "unknown"

    return shas


# ── Phase 1: Init ────────────────────────────────────────────────────────────

def _phase_init(args, manifest: dict, run_dir: Path) -> StateMachine:
    """Phase 1: Load manifest, resolve scenario config, validate prerequisites."""
    step(1, "Init")

    # Check for existing state
    if (run_dir / ".state.json").exists() and not args.force:
        state = StateMachine.load(run_dir)
        if state.is_done("init"):
            info("[skip] Init phase already complete")
            return state

    scenario = manifest["scenario"]
    resolved = _load_resolved_config(manifest)

    # Validate prerequisites — per-algorithm files
    for algo in manifest.get("algorithms", []):
        src = algo.get("source")
        if src and not (EXPERIMENT_ROOT / src).exists():
            err(f"algorithm '{algo['name']}' source not found: {src}")
            sys.exit(1)
        algo_config = algo.get("config")
        if algo_config and not (EXPERIMENT_ROOT / algo_config).exists():
            err(f"algorithm '{algo['name']}' config not found: {algo_config}")
            sys.exit(1)

    # Validate prerequisites — per-baseline files
    for bl in manifest.get("baselines", []):
        sim_cfg = bl.get("sim", {}).get("config")
        if sim_cfg and not (EXPERIMENT_ROOT / sim_cfg).exists():
            err(f"baseline '{bl['name']}' sim.config not found: {sim_cfg}")
            sys.exit(1)
        real_cfg = bl.get("real", {}).get("config")
        if real_cfg and not (EXPERIMENT_ROOT / real_cfg).exists():
            err(f"baseline '{bl['name']}' real.config not found: {real_cfg}")
            sys.exit(1)
        scenario_file = bl.get("scenario")
        if scenario_file and not (EXPERIMENT_ROOT / scenario_file).exists():
            err(f"baseline '{bl['name']}' scenario not found: {scenario_file}")
            sys.exit(1)

    for wl in manifest["workloads"]:
        if not (EXPERIMENT_ROOT / wl).exists():
            err(f"Workload not found: {wl}")
            sys.exit(1)

    # Validate component submodule
    target_path = resolved.get("path", "")
    component_ref = manifest.get("component", {}).get("ref")
    if target_path:
        comp_dir = EXPERIMENT_ROOT / target_path
        if not comp_dir.exists():
            if component_ref:
                err(f"Component repo not found: {target_path}\n"
                    f"  Initialize with: git submodule update --init {target_path}")
            else:
                err(f"Component repo not found: {target_path}")
            sys.exit(1)
        elif component_ref:
            if not (comp_dir / ".git").exists():
                err(f"component.ref is set but {target_path} is not a git repository.\n"
                    f"  Cannot verify ref {component_ref}.\n"
                    f"  Initialize with: git submodule update --init {target_path}")
                sys.exit(1)
            try:
                result = run(["git", "rev-parse", "HEAD"], capture=True, cwd=comp_dir)
            except subprocess.CalledProcessError:
                err(f"Cannot determine HEAD in {target_path} (git rev-parse failed).\n"
                    f"  The .git directory may be corrupted.")
                sys.exit(1)
            actual_sha = result.stdout.strip()
            try:
                result_ref = run(["git", "rev-parse", component_ref], capture=True, cwd=comp_dir)
                expected_sha = result_ref.stdout.strip()
            except subprocess.CalledProcessError:
                err(f"Cannot resolve component.ref '{component_ref}' in {target_path}.\n"
                    f"  Ensure the ref exists: cd {target_path} && git fetch")
                sys.exit(1)
            if actual_sha != expected_sha:
                warn(f"Component ref mismatch in {target_path}:\n"
                     f"  manifest component.ref: {component_ref}\n"
                     f"  checked-out HEAD:       {actual_sha}\n"
                     f"  Update with: cd {target_path} && git checkout {component_ref}")

    run_name = args.run or _load_setup_config().get("current_run", _default_run_name())
    state = StateMachine(run_name, scenario, run_dir)
    state.mark_done("init")
    ok(f"Init complete: run={run_name} scenario={scenario}")
    return state


# ── Phase 2: Context ─────────────────────────────────────────────────────────

def _phase_context(args, state: StateMachine, manifest: dict, run_dir: Path) -> Path:
    """Phase 2: Build context document with caching."""
    step(2, "Context")

    if state.is_done("context") and not getattr(args, "rebuild_context", False) and not args.force:
        cached_path = Path(state.get_phase("context").get("path", ""))
        if cached_path.exists():
            info(f"[skip] Context cached: {cached_path}")
            return cached_path

    # Resolve context files from manifest
    context_files = []
    for f in manifest.get("context", {}).get("files", []):
        full = EXPERIMENT_ROOT / f
        if not full.exists():
            err(f"Context file not found: {f}")
            sys.exit(1)
        context_files.append(full)

    shas = _get_submodule_shas(manifest.get("component", {}).get("path", ""))

    path, cached = build_context(
        context_files=context_files,
        submodule_shas=shas,
        scenario=manifest["scenario"],
        cache_dir=EXPERIMENT_ROOT / "workspace" / "context",
    )

    state.mark_done("context", hash=path.stem, cached=cached, path=str(path),
                    context_file_prepared=True, context_file_populated=False)
    ok(f"Context {'cached' if cached else 'built'}: {path}")
    return path


# ── Phase 3: Translation Checkpoint ──────────────────────────────────────────

def _phase_translate(args, state: StateMachine, manifest: dict, run_dir: Path,
                     resolved: dict, context_path: Path):
    """Phase 3: Write skill_input.json (or skip if no algorithm in manifest)."""
    step(3, "Translation Checkpoint")

    if state.is_done("translate") and not args.force:
        info("[skip] Translation already complete")
        return

    if not manifest.get("algorithms"):
        info("[skip] No algorithm in manifest — baseline-only mode")
        state.mark_done("translate", mode="baseline-only")
        return

    build_cfg = resolved.get("build", {})

    # Build commands: common commands (skill determines test scope)
    commands = [" ".join(c) if isinstance(c, list) else c for c in build_cfg.get("commands", [])]

    # Write skill_input.json
    first_bl = manifest.get("baselines", [{}])[0]
    first_algo = manifest.get("algorithms", [{}])[0]

    skill_input = {
        "run_name": state.run_name,
        "run_dir": _display_path(run_dir),
        "scenario": manifest["scenario"],
        "context_path": _display_path(context_path),
        "manifest_path": str(getattr(args, "manifest", None) or "transfer.yaml"),
        "baselines": [
            {
                "name": bl["name"],
                "sim_config": bl.get("sim", {}).get("config"),
                "real_config": bl.get("real", {}).get("config"),
                "real_notes": bl.get("real", {}).get("notes", ""),
            }
            for bl in manifest.get("baselines", [])
        ],
        "algorithms": [
            {
                "name": algo["name"],
                "source": algo["source"],
                "config": algo.get("config"),
                "defaults": algo["defaults"],
            }
            for algo in manifest.get("algorithms", [])
        ],
        # Legacy fields for backward compat with existing skill
        "algorithm_source": first_algo.get("source", ""),
        "algorithm_config": first_algo.get("config"),
        "baseline_sim_config": first_bl.get("sim", {}).get("config"),
        "baseline_real_config": first_bl.get("real", {}).get("config"),
        "baseline_real_notes": first_bl.get("real", {}).get("notes", ""),
        "target": {"repo": resolved.get("repo", "")},
        "build_commands": commands,
        "config_kind": resolved.get("kind", ""),
        "context": {"text": manifest.get("context", {}).get("text", "")},
    }
    skill_input_path = run_dir / "skill_input.json"
    skill_input_path.write_text(json.dumps(skill_input, indent=2))

    # Check for translation output
    output_path = run_dir / "translation_output.json"
    if output_path.exists():
        output = json.loads(output_path.read_text())
        # Validate required fields
        for f in ["plugin_type", "files_created", "files_modified",
                  "package", "test_commands", "config_kind",
                  "treatment_config_generated", "description"]:
            if f not in output:
                err(f"translation_output.json missing required field: {f}")
                sys.exit(1)
        if "register_file" not in output:
            err("translation_output.json missing required field: register_file")
            sys.exit(1)

        state.mark_done("translate",
                        plugin_type=output["plugin_type"],
                        files_created=output["files_created"],
                        register_file=output.get("register_file"),
                        treatment_config_generated=output.get("treatment_config_generated", False))
        ok(f"Translation found: {output['plugin_type']}")
        return

    # No translation output yet — checkpoint
    state.increment("translate", "checkpoint_hits")
    hits = state.get_phase("translate").get("checkpoint_hits", 1)

    print(f"\n{'='*60}")
    print("  TRANSLATION CHECKPOINT")
    print(f"{'='*60}")
    print(f"\n  skill_input.json written to: {_display_path(skill_input_path)}")
    print("\n  Next step: run the /sim2real-translate skill in Claude Code,")
    print("  then re-run: python pipeline/prepare.py")
    if hits >= 3:
        warn(f"Checkpoint hit {hits} times. Have you run the translation skill?")
    print(f"\n{'='*60}\n")
    sys.exit(0)


# ── Phase 4: Assembly ────────────────────────────────────────────────────────

def _phase_assembly(args, state: StateMachine, manifest: dict, run_dir: Path,
                    resolved: dict):
    """Phase 4: Assemble resolved scenarios from baseline/treatment + overlays."""
    step(4, "Assembly")

    if state.is_done("assembly") and not args.force:
        info("[skip] Assembly already complete")
        return

    # 4b: Build baseline and algorithm specs from manifest
    baselines_spec = []
    for bl in manifest.get("baselines", []):
        scenario_path = bl.get("scenario")
        if scenario_path:
            scenario_path = EXPERIMENT_ROOT / scenario_path
        else:
            scenario_path = EXPERIMENT_ROOT / "baseline.yaml"
        if not scenario_path.exists():
            err(f"Baseline scenario not found: {scenario_path}")
            sys.exit(1)
        spec = {"name": bl["name"], "scenario_path": scenario_path}
        defaults_path = bl.get("defaults")
        if defaults_path:
            dp = EXPERIMENT_ROOT / defaults_path
            if not dp.exists():
                err(f"Baseline defaults not found: {dp}")
                sys.exit(1)
            spec["defaults_path"] = dp
        baselines_spec.append(spec)

    algorithms_spec = []
    for algo in manifest.get("algorithms", []):
        scenario_path = algo.get("scenario")
        if scenario_path:
            scenario_path = EXPERIMENT_ROOT / scenario_path
        else:
            fallback = EXPERIMENT_ROOT / "treatment.yaml"
            scenario_path = fallback if fallback.exists() else None
        algorithms_spec.append({
            "name": algo["name"],
            "scenario_path": scenario_path,
            "defaults": algo["defaults"],
        })

    translation_output_path = run_dir / "translation_output.json"
    translation_happened = translation_output_path.exists()
    generated_dir = run_dir / "generated"

    try:
        packages = assemble_packages(
            baselines=baselines_spec,
            algorithms=algorithms_spec if translation_happened else [],
            generated_dir=generated_dir,
            overlays_expected=translation_happened,
        )
    except AssemblyError as e:
        err(str(e))
        sys.exit(1)

    # 4b.5: Inject EPP images into packages (only when translation occurred)
    if translation_happened:
        meta_path = run_dir / "run_metadata.json"
        if not meta_path.exists():
            err("run_metadata.json absent — cannot inject EPP image. Re-run setup.py.")
            sys.exit(1)
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError as e:
            err(f"run_metadata.json is not valid JSON: {e}. Re-run setup.py.")
            sys.exit(1)
        registry = meta.get("registry", "")
        repo_name = meta.get("repo_name", "llm-d-inference-scheduler")
        run_name_tag = run_dir.name
        if not registry:
            err("run_metadata.json has no registry — cannot determine EPP image. Re-run setup.py.")
            sys.exit(1)
        for pkg in packages:
            if pkg.kind == "algorithm":
                injected = inject_epp_image(pkg.resolved, registry, repo_name, run_name_tag)
                if injected:
                    ok(f"EPP image injected into {pkg.name}: {registry}/{repo_name}:{run_name_tag}")
                else:
                    err(f"{pkg.name} has no 'scenario' entries — EPP image cannot be injected.")
                    sys.exit(1)

        # Inject baseline EPP image into baseline packages
        component_path = manifest.get("component", {}).get("path", "")
        if component_path:
            comp_dir = EXPERIMENT_ROOT / component_path
            if comp_dir.exists() and (comp_dir / ".git").exists():
                from pipeline.lib.ensure_image import compute_source_hash
                baseline_tag = compute_source_hash(comp_dir)[:8]
                from pipeline.lib.epp import inject_image_ref
                for pkg in packages:
                    if pkg.kind == "baseline":
                        injected = inject_image_ref(
                            pkg.resolved, f"{registry}/{repo_name}", baseline_tag
                        )
                        if injected:
                            ok(f"Baseline EPP image: {registry}/{repo_name}:{baseline_tag} → {pkg.name}")
            else:
                info(f"Skipping baseline EPP injection: {component_path} not found or not a git repo")
        else:
            info("Skipping baseline EPP injection: no component.path in manifest")

    # 4b.6: Inject huggingface.secretName into all packages
    setup_config = _load_setup_config()
    if not setup_config:
        err("setup_config.json not found. Run setup.py first to bootstrap cluster resources.")
        sys.exit(1)
    hf_secret_name = setup_config.get("hf_secret_name", "hf-secret")
    for pkg in packages:
        if not inject_hf_secret_name(pkg.resolved, hf_secret_name):
            err(f"{pkg.name} has no 'scenario' entries — huggingface.secretName cannot be injected.")
            sys.exit(1)

    # 4c: Write resolved scenarios
    cluster_dir = run_dir / "cluster"
    cluster_dir.mkdir(parents=True, exist_ok=True)

    for pkg in packages:
        out = cluster_dir / f"{pkg.name}.yaml"
        out.write_text(yaml.dump(pkg.resolved, default_flow_style=False, allow_unicode=True))

    ok(f"Resolved scenarios: {_display_path(cluster_dir)}")

    # 4d: Load workloads
    workloads = []
    for wl_path_str in manifest.get("workloads", []):
        wl_path = EXPERIMENT_ROOT / wl_path_str
        if not wl_path.exists():
            err(f"Workload file not found: {wl_path}")
            sys.exit(1)
        try:
            wl_data = yaml.safe_load(wl_path.read_text())
        except yaml.YAMLError as e:
            err(f"Invalid YAML in workload file {wl_path}: {e}")
            sys.exit(1)
        if not isinstance(wl_data, dict):
            err(f"Workload file is not a YAML mapping: {wl_path}")
            sys.exit(1)
        if "name" not in wl_data and "workload_name" not in wl_data:
            wl_data["workload_name"] = Path(wl_path_str).stem
        workloads.append(wl_data)

    if not workloads:
        warn("No workloads defined — cannot generate PipelineRuns")
        done_packages = [pkg.name for pkg in packages]
        state.mark_done("assembly", packages=done_packages)
        return

    # 4e: Pipeline resource
    run_name = run_dir.name
    pipeline_name = manifest.get("pipeline", {}).get("name", "sim2real")

    # 4f: Generate PipelineRuns
    namespace = setup_config.get("namespace", "default")
    ws_bindings = setup_config.get("workspaces") or {}
    shas = _get_submodule_shas(manifest.get("component", {}).get("path", ""))
    benchmark_commit = shas.get("llm-d-benchmark", "")
    blis_commit = shas.get("inference-sim", "")
    benchmark_sub = REPO_ROOT / "llm-d-benchmark"
    benchmark_repo_url = ""
    if benchmark_sub.exists() and (benchmark_sub / ".git").exists():
        try:
            result = run(["git", "remote", "get-url", "origin"], capture=True, cwd=benchmark_sub)
            benchmark_repo_url = result.stdout.strip()
        except subprocess.CalledProcessError as e:
            warn(f"git remote get-url origin failed in {benchmark_sub}: {e}")
    blis_sub = REPO_ROOT / "inference-sim"
    blis_repo_url = ""
    if blis_sub.exists() and (blis_sub / ".git").exists():
        try:
            result = run(["git", "remote", "get-url", "origin"], capture=True, cwd=blis_sub)
            blis_repo_url = result.stdout.strip()
        except subprocess.CalledProcessError as e:
            warn(f"git remote get-url origin failed in {blis_sub}: {e}")

    missing_params = []
    if not benchmark_repo_url:
        missing_params.append("benchmark_repo_url (llm-d-benchmark submodule)")
    if not blis_repo_url:
        missing_params.append("blis_repo_url (inference-sim submodule)")
    if missing_params:
        if translation_happened:
            err(f"Critical PipelineRun params resolved to empty: {', '.join(missing_params)}. "
                "Initialize submodules with: git submodule update --init")
            sys.exit(1)
        else:
            warn(f"Submodule params not available: {', '.join(missing_params)}. "
                 "Generated PipelineRuns will FAIL on cluster until submodules are initialized: "
                 "git submodule update --init")
    first_baseline = next((p for p in packages if p.kind == "baseline"), packages[0])
    scenarios_list = first_baseline.resolved.get("scenario", [])
    model_name = scenarios_list[0].get("model", {}).get("name", "") if scenarios_list else ""

    for pkg in packages:
        scenario_content = yaml.dump(pkg.resolved, default_flow_style=False, allow_unicode=True)
        for wl in workloads:
            wl_name = wl.get("name", wl.get("workload_name", "unknown"))
            safe_wl = wl_name.replace("_", "-")

            pr = make_pipelinerun_scenario(
                phase=pkg.name,
                workload=wl,
                run_name=run_name,
                namespace=namespace,
                pipeline_name=pipeline_name,
                scenario_content=scenario_content,
                workspace_bindings=ws_bindings if ws_bindings else None,
                benchmark_git_commit=benchmark_commit,
                benchmark_git_repo_url=benchmark_repo_url,
                blis_git_commit=blis_commit,
                blis_git_repo_url=blis_repo_url,
                model=model_name,
            )
            pr_path = cluster_dir / f"pipelinerun-{safe_wl}-{pkg.name}.yaml"
            pr_path.write_text(yaml.dump(pr, default_flow_style=False, allow_unicode=True))

    ok(f"PipelineRuns: {len(workloads) * len(packages)} generated")

    # 4g: Verify generated dir (only when translation produced files)
    if translation_happened:
        _verify_generated_dir(run_dir)

    # 4h: validate-assembly
    algo_names = [pkg.name for pkg in packages if pkg.kind == "algorithm"] if translation_happened else None
    _validate_assembly(run_dir, resolved, algorithm_packages=algo_names)

    done_packages = [pkg.name for pkg in packages]
    state.mark_done("assembly", packages=done_packages)
    ok("Assembly complete")


def _verify_generated_dir(run_dir: Path):
    """Verify the generated/ directory exists with expected files."""
    generated_dir = run_dir / "generated"
    if not generated_dir.exists():
        warn("generated/ directory not found — translation skill should create this.")
        warn("Continuing without generated file copies.")
        return

    output_path = run_dir / "translation_output.json"
    if not output_path.exists():
        warn("translation_output.json not found — skipping generated file verification")
        return

    try:
        output = json.loads(output_path.read_text())
    except json.JSONDecodeError:
        err("translation_output.json is not valid JSON — translation may have failed. "
            "Re-run the /sim2real-translate skill.")
        sys.exit(1)

    for f in output.get("files_created", []) + output.get("files_modified", []):
        if not (generated_dir / Path(f).name).exists():
            warn(f"generated/ missing: {Path(f).name}")


def _validate_assembly(run_dir: Path, resolved: dict, algorithm_packages: list[str] | None = None):
    """Phase 4g: Deterministic consistency checks."""
    output_path = run_dir / "translation_output.json"
    if not output_path.exists():
        return
    try:
        output = json.loads(output_path.read_text())
    except json.JSONDecodeError:
        warn("translation_output.json is not valid JSON — skipping validation")
        return
    plugin_type = output["plugin_type"]
    target_path = resolved.get("path", "")
    treatment_config_generated = output.get("treatment_config_generated", True)

    errors = []

    # Check 1: plugin_type literal exists in register_file's directory OR in files_created
    register_file = output.get("register_file")
    if register_file is not None:
        register_path = EXPERIMENT_ROOT / target_path / register_file
        if register_path.exists():
            search_files = list(register_path.parent.rglob("*.go"))
            for f in output.get("files_created", []):
                p = EXPERIMENT_ROOT / target_path / f
                if p.exists() and p.suffix == ".go":
                    search_files.append(p)
            found = any(
                plugin_type in f.read_text()
                for f in search_files
            )
            if not found:
                errors.append(
                    f"plugin_type '{plugin_type}' not found in {register_file} or plugin files")
        else:
            errors.append(f"register_file not found on disk: {register_file}")

    # Check 2: plugin_type string present inside algorithm scenario YAMLs
    # Skip when treatment_config_generated=False: baseline config is copied instead,
    # and plugin_type may not appear in the treatment YAML.
    if treatment_config_generated:
        check_names = algorithm_packages or ["treatment"]
        for pkg_name in check_names:
            pkg_yaml = run_dir / "cluster" / f"{pkg_name}.yaml"
            if pkg_yaml.exists():
                if plugin_type not in pkg_yaml.read_text():
                    errors.append(
                        f"plugin_type '{plugin_type}' not found in {pkg_name}.yaml")

    # Check 3: all files_created exist in target repo
    for f in output.get("files_created", []):
        if target_path and not (EXPERIMENT_ROOT / target_path / f).exists():
            errors.append(f"files_created entry missing on disk: {f}")

    if errors:
        err("validate-assembly FAILED:")
        for e in errors:
            err(f"  - {e}")
        sys.exit(1)
    ok("validate-assembly: all checks passed")


# ── Phase 5: Summary ─────────────────────────────────────────────────────────

def _phase_summary(state: StateMachine, manifest: dict, run_dir: Path, resolved: dict):
    """Phase 5: Generate run_summary.md."""
    step(5, "Summary")

    if state.is_done("summary") and not getattr(state, '_force', False):
        info("[skip] Summary already complete")
        return

    translation_output_path = run_dir / "translation_output.json"

    if translation_output_path.exists():
        output = json.loads(translation_output_path.read_text())
        translate_meta = state.get_phase("translate")

        lines = [
            f"**Run Summary: `{state.run_name}`**",
            f"Generated: {datetime.now(timezone.utc).isoformat()} | Scenario: {manifest['scenario']}",
            "",
            "**Algorithm**",
            f"- Source: `{(manifest.get('algorithms') or [{}])[0].get('source', 'N/A')}`",
            f"- Description: {output.get('description', 'N/A')}",
            "",
            "**Translation**",
            f"- Plugin type: `{output['plugin_type']}`",
            f"- Files created: {', '.join(f'`{f}`' for f in output.get('files_created', []))}",
            f"- Files modified: {', '.join(f'`{f}`' for f in output.get('files_modified', []))}",
        ]

        if translate_meta.get("review_rounds"):
            lines.append(f"- Review: {translate_meta.get('consensus', 'N/A')} "
                         f"after {translate_meta['review_rounds']} rounds")
    else:
        baselines_str = ", ".join(bl["name"] for bl in manifest.get("baselines", []))
        lines = [
            f"**Run Summary: `{state.run_name}`**",
            f"Generated: {datetime.now(timezone.utc).isoformat()} | Scenario: {manifest['scenario']}",
            "",
            f"**Mode:** Baseline-only ({baselines_str})" if baselines_str else "**Mode:** Baseline-only (no translation)",
        ]

    # Baseline vs Treatment config comparison
    lines.extend(["", "**Packages**", ""])
    cluster_dir = run_dir / "cluster"
    if cluster_dir.exists():
        for p in sorted(cluster_dir.glob("pipelinerun-*.yaml")):
            lines.append(f"- `{p}`")

    # Workloads
    lines.extend(["", "**Workloads**", ""])
    for wl in manifest["workloads"]:
        wl_name = Path(wl).stem
        lines.append(f"- {wl_name}")

    # Checklist
    if translation_output_path.exists():
        lines.extend([
            "", "**Checklist**",
            "- [x] Translation complete",
            "- [x] Assembly complete",
            "- [x] validate-assembly passed",
            "",
        ])
    else:
        lines.extend([
            "", "**Checklist**",
            "- [-] Translation skipped (baseline-only)",
            "- [x] Assembly complete",
            "",
        ])

    summary_path = run_dir / "run_summary.md"
    summary_path.write_text("\n".join(lines))
    state.mark_done("summary")
    ok(f"Summary: {_display_path(summary_path)}")


# ── Phase 6: Gate ────────────────────────────────────────────────────────────

def _phase_gate(state: StateMachine, run_dir: Path):
    """Phase 6: Human review gate."""
    step(6, "Gate")

    if state.is_done("gate"):
        verdict = state.get_phase("gate").get("verdict", "")
        info(f"[skip] Gate already complete: {verdict}")
        return

    summary_path = run_dir / "run_summary.md"
    print("\n" + summary_path.read_text())

    while True:
        choice = input("\n  [d]eploy / [e]dit / [q]uit: ").strip().lower()
        if choice in ("d", "deploy"):
            with open(summary_path, "a") as f:
                f.write("\n**Verdict: READY TO DEPLOY**\n")
            state.mark_done("gate", verdict="READY TO DEPLOY")
            ok("Gate: READY TO DEPLOY")
            return
        elif choice in ("e", "edit"):
            info(f"Edit files in {run_dir}, then press Enter to re-display.")
            input("  Press Enter when done editing...")
            print("\n" + summary_path.read_text())
        elif choice in ("q", "quit"):
            state.mark_done("gate", verdict="abandoned")
            warn("Gate: abandoned")
            sys.exit(0)
        else:
            print("  Enter 'd' to deploy, 'e' to edit, or 'q' to quit.")


# ── Subcommands ──────────────────────────────────────────────────────────────

def _cmd_run(args, manifest, run_dir):
    state = _phase_init(args, manifest, run_dir)
    resolved = _load_resolved_config(manifest)
    context_path = _phase_context(args, state, manifest, run_dir)
    _phase_translate(args, state, manifest, run_dir, resolved, context_path)
    _phase_assembly(args, state, manifest, run_dir, resolved)
    _phase_summary(state, manifest, run_dir, resolved)
    _phase_gate(state, run_dir)
    ok("Pipeline complete. Deploy with: python <repo-root>/pipeline/deploy.py")


def _cmd_context(args, manifest, run_dir):
    """Rebuild context cache only."""
    state = _phase_init(args, manifest, run_dir)
    args.rebuild_context = True
    _phase_context(args, state, manifest, run_dir)


def _cmd_assemble(args, manifest, run_dir):
    """Re-run assembly (baseline-only if no translation output exists)."""
    try:
        state = StateMachine.load(run_dir)
    except FileNotFoundError:
        err("No state file. Run prepare.py first.")
        sys.exit(1)

    baseline_only = not (run_dir / "translation_output.json").exists()
    if baseline_only:
        translate_phase = state.get_phase("translate")
        if translate_phase.get("checkpoint_hits"):
            err("Translation was attempted but translation_output.json is missing. "
                "Re-run /sim2real-translate or remove the translate phase from state "
                "to proceed in baseline-only mode.")
            sys.exit(1)
        warn("No translation output — producing baseline-only PipelineRuns")

    resolved = _load_resolved_config(manifest)
    state.reset("assembly")
    state.reset("summary")
    state.reset("gate")
    _phase_assembly(args, state, manifest, run_dir, resolved)
    _phase_summary(state, manifest, run_dir, resolved)
    if not baseline_only:
        _phase_gate(state, run_dir)


def _cmd_validate_assembly(args, manifest, run_dir):
    """Run validate-assembly checks standalone."""
    if not (run_dir / "translation_output.json").exists():
        info("Baseline-only run — no treatment validation needed")
        return
    resolved = _load_resolved_config(manifest)
    algo_names = [a["name"] for a in manifest.get("algorithms", []) if a]
    _validate_assembly(run_dir, resolved, algorithm_packages=algo_names or None)


def _cmd_status(run_dir):
    """Print current state."""
    try:
        state = StateMachine.load(run_dir)
    except FileNotFoundError:
        print("No active run.")
        return
    print(f"Run: {state.run_name} | Scenario: {state.scenario}")
    for phase in ["init", "context", "translate", "assembly", "summary", "gate"]:
        meta = state.get_phase(phase)
        status = meta.get("status", "pending")
        extras = ""
        if phase == "context" and meta.get("cached"):
            extras = " (cached)"
        if phase == "translate" and meta.get("plugin_type"):
            extras = f" ({meta['plugin_type']})"
        if phase == "gate" and meta.get("verdict"):
            extras = f" ({meta['verdict']})"
        print(f"  {phase:12s} {status}{extras}")


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="prepare.py",
        description="sim2real prepare — 6-phase state machine for algorithm transfer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--force", action="store_true",
                   help="Regenerate all phases (ignore .state.json)")
    p.add_argument("--rebuild-context", action="store_true", dest="rebuild_context",
                   help="Force context cache rebuild")
    p.add_argument("--manifest", metavar="PATH",
                   help="Path to transfer.yaml (default: transfer.yaml)")
    p.add_argument("--run", metavar="NAME",
                   help="Override run name")
    p.add_argument("--experiment-root", metavar="PATH", dest="experiment_root",
                   help="Root of the experiment repo (default: current directory)")

    sub = p.add_subparsers(dest="command")
    sub.add_parser("context", help="Rebuild context cache only")
    sub.add_parser("assemble", help="Reproduce cluster YAMLs from existing translation")
    sub.add_parser("validate-assembly", help="Validate assembly consistency (standalone)")
    sub.add_parser("status", help="Show current run state")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    global EXPERIMENT_ROOT
    EXPERIMENT_ROOT = Path(args.experiment_root).resolve() if args.experiment_root else Path.cwd()

    manifest_path = args.manifest or str(_resolve_manifest_default(EXPERIMENT_ROOT))
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as e:
        err(str(e))
        sys.exit(1)

    setup_config = _load_setup_config()
    run_name = args.run or setup_config.get("current_run", _default_run_name())
    run_dir = EXPERIMENT_ROOT / "workspace" / "runs" / run_name

    cmd = args.command
    if cmd == "status":
        _cmd_status(run_dir)
    elif cmd == "context":
        _cmd_context(args, manifest, run_dir)
    elif cmd == "assemble":
        _cmd_assemble(args, manifest, run_dir)
    elif cmd == "validate-assembly":
        _cmd_validate_assembly(args, manifest, run_dir)
    else:
        _cmd_run(args, manifest, run_dir)


if __name__ == "__main__":
    main()
