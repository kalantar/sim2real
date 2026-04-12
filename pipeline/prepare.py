#!/usr/bin/env python3
"""sim2real prepare — 6-phase state machine for algorithm transfer.

Phases:
  1. Init       — load manifest v2, resolve scenario config, validate prerequisites
  2. Context    — assemble + cache context document
  3. Translate  — checkpoint: write skill_input.json, check for translation_output.json
  4. Assembly   — treatment config, algorithm values, merge-values, cluster packages
  5. Summary    — generate run_summary.md
  6. Gate       — human review: [d]eploy / [e]dit / [q]uit

Re-running skips completed phases (tracked in .state.json).
"""

import argparse
import json
import subprocess
import sys
import warnings
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
from pipeline.lib.values import _deep_merge, merge_values
from pipeline.lib.tekton import compile_pipeline, make_experiment_pipeline, make_phase_pipeline

# ── Repo layout ──────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent

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
    path = REPO_ROOT / "workspace" / "setup_config.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _load_resolved_config(manifest: dict) -> dict:
    """Load env_defaults, merge common + scenario, return resolved config."""
    env_path = REPO_ROOT / "config" / "env_defaults.yaml"
    env_data = yaml.safe_load(env_path.read_text())
    common = env_data.get("common", {})
    scenarios = env_data.get("scenarios", {})
    scenario = manifest["scenario"]
    if scenario not in scenarios:
        err(f"Scenario '{scenario}' not found in env_defaults.yaml. "
            f"Available: {list(scenarios.keys())}")
        sys.exit(1)
    return _deep_merge(common, scenarios[scenario])


def _get_submodule_shas() -> dict[str, str]:
    """Get HEAD commit SHAs for submodules."""
    shas = {}
    for name, path in [("inference-sim", "inference-sim"),
                       ("llm-d-inference-scheduler", "llm-d-inference-scheduler")]:
        sub = REPO_ROOT / path
        if sub.exists() and (sub / ".git").exists():
            result = run(["git", "rev-parse", "HEAD"], capture=True, cwd=sub)
            shas[name] = result.stdout.strip()
        else:
            shas[name] = "unknown"
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

    # Validate prerequisites
    for label, path_str in [
        ("algorithm.source", manifest["algorithm"]["source"]),
        ("algorithm.config", manifest["algorithm"]["config"]),
        ("baseline.sim.config", manifest["baseline"]["sim"]["config"]),
    ]:
        if not (REPO_ROOT / path_str).exists():
            err(f"{label} not found: {path_str}")
            sys.exit(1)

    # Validate baseline.real.config if present
    baseline_real_config = manifest["baseline"]["real"].get("config")
    if baseline_real_config is not None:
        if not (REPO_ROOT / baseline_real_config).exists():
            err(f"baseline.real.config not found: {baseline_real_config}")
            sys.exit(1)

    for wl in manifest["workloads"]:
        if not (REPO_ROOT / wl).exists():
            err(f"Workload not found: {wl}")
            sys.exit(1)

    if not (REPO_ROOT / manifest["llm_config"]).exists():
        err(f"llm_config not found: {manifest['llm_config']}")
        sys.exit(1)

    # Validate target repo exists
    target = resolved.get("target", {})
    target_repo = target.get("repo", "")
    if target_repo and not (REPO_ROOT / target_repo).exists():
        err(f"Target repo not found: {target_repo}")
        sys.exit(1)

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
        full = REPO_ROOT / f
        if not full.exists():
            err(f"Context file not found: {f}")
            sys.exit(1)
        context_files.append(full)

    shas = _get_submodule_shas()

    path, cached = build_context(
        context_files=context_files,
        submodule_shas=shas,
        scenario=manifest["scenario"],
        cache_dir=REPO_ROOT / "workspace" / "context",
    )

    state.mark_done("context", hash=path.stem, cached=cached, path=str(path),
                    context_file_prepared=True, context_file_populated=False)
    ok(f"Context {'cached' if cached else 'built'}: {path}")
    return path


# ── Phase 3: Translation Checkpoint ──────────────────────────────────────────

def _phase_translate(args, state: StateMachine, manifest: dict, run_dir: Path,
                     resolved: dict, context_path: Path):
    """Phase 3: Write skill_input.json, check for translation_output.json."""
    step(3, "Translation Checkpoint")

    if state.is_done("translate") and not args.force:
        info("[skip] Translation already complete")
        return

    target = resolved.get("target", {})
    build_cfg = resolved.get("build", {})
    config_cfg = resolved.get("config", {})

    # Build commands: common commands (skill determines test scope)
    commands = [list(c) for c in build_cfg.get("commands", [])]

    # Write skill_input.json
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
        "baseline_sim_config": manifest["baseline"]["sim"]["config"],
        "baseline_real_config": manifest["baseline"]["real"].get("config"),
        "baseline_real_notes": manifest["baseline"]["real"].get("notes", ""),
        "target": {"repo": target.get("repo", "")},
        "build_commands": commands,
        "config_kind": config_cfg.get("kind", ""),
        "hints": manifest.get("hints", {"text": "", "files": []}),
    }
    skill_input_path = run_dir / "skill_input.json"
    skill_input_path.write_text(json.dumps(skill_input, indent=2))

    # Check for translation output
    output_path = run_dir / "translation_output.json"
    if output_path.exists():
        output = json.loads(output_path.read_text())
        # Validate required fields
        for f in ["plugin_type", "files_created", "files_modified",
                  "package", "test_commands", "config_kind", "helm_path",
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
    print(f"\n  skill_input.json written to: {skill_input_path.relative_to(REPO_ROOT)}")
    print("\n  Next step: run the /sim2real-translate skill in Claude Code,")
    print("  then re-run: python pipeline/prepare.py")
    if hits >= 3:
        warn(f"Checkpoint hit {hits} times. Have you run the translation skill?")
    print(f"\n{'='*60}\n")
    sys.exit(0)


# ── Phase 4: Assembly ────────────────────────────────────────────────────────

def _phase_assembly(args, state: StateMachine, manifest: dict, run_dir: Path,
                    resolved: dict):
    """Phase 4: Assemble cluster artifacts from translation output."""
    step(4, "Assembly")

    if state.is_done("assembly") and not args.force:
        info("[skip] Assembly already complete")
        return

    # 4a: Validate treatment config
    tc_path = run_dir / "treatment_config.yaml"
    if tc_path.exists():
        tc = yaml.safe_load(tc_path.read_text())
        expected_kind = resolved.get("config", {}).get("kind")
        if expected_kind and isinstance(tc, dict) and tc.get("kind") != expected_kind:
            err(f"treatment_config kind mismatch: got '{tc.get('kind')}', expected '{expected_kind}'")
            sys.exit(1)
        ok("Treatment config validated")

    # 4b: Baseline config — from baseline_config.yaml in run dir (if Phase 2 ran),
    # otherwise falls back to env_defaults (scenarios.<scenario>.gaie.baseline)

    # 4c: Generate algorithm_values.yaml
    alg_values_path = run_dir / "algorithm_values.yaml"
    _generate_algorithm_values(manifest, resolved, alg_values_path)
    ok(f"Algorithm values: {alg_values_path.relative_to(REPO_ROOT)}")

    # 4c.5: Re-inject EPP image if one was already built for this run
    meta_path = run_dir / "run_metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        epp_image = meta.get("epp_image", "")
        if epp_image:
            # epp_image is "hub/name:tag" — split on last colon for tag, then last slash for hub
            tag = epp_image.rsplit(":", 1)[-1] if ":" in epp_image else ""
            repo = epp_image.rsplit(":", 1)[0] if ":" in epp_image else epp_image
            name = repo.rsplit("/", 1)[-1] if "/" in repo else repo
            hub = repo.rsplit("/", 1)[0] if "/" in repo else ""
            alg_values = yaml.safe_load(alg_values_path.read_text())
            (alg_values
                .setdefault("stack", {})
                .setdefault("gaie", {})
                .setdefault("treatment", {})
                .setdefault("helmValues", {})
                .setdefault("inferenceExtension", {})
                ["image"]) = {"hub": hub, "name": name, "tag": tag, "pullPolicy": "Always"}
            alg_values_path.write_text(yaml.dump(alg_values, default_flow_style=False, allow_unicode=True))
            ok(f"EPP image re-injected: {epp_image}")

    # 4d: Merge values
    values_path = run_dir / "values.yaml"
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
    ok(f"Values merged: {values_path.relative_to(REPO_ROOT)}")

    # 4e: Compile cluster YAMLs per package
    setup_config = _load_setup_config()
    _compile_cluster_packages(run_dir, resolved, values_path, setup_config)

    # 4f: Verify generated/ directory (created by translation skill)
    _verify_generated_dir(run_dir)

    # 4g: validate-assembly
    _validate_assembly(run_dir, resolved)

    state.mark_done("assembly", packages=["baseline", "treatment"])
    ok("Assembly complete")


def _generate_algorithm_values(manifest: dict, resolved: dict, out_path: Path):
    """Generate algorithm_values.yaml from manifest + resolved config."""
    llm_config_path = REPO_ROOT / manifest["llm_config"]
    llm_config = yaml.safe_load(llm_config_path.read_text())

    # Support both flat format (model_name/model_uri) and nested format (model.hf_repo/model.id)
    _model_block = llm_config.get("model", {})
    if "model_name" not in llm_config:
        llm_config["model_name"] = _model_block.get("hf_repo") or _model_block.get("id", "")
    if "model_uri" not in llm_config:
        hf_repo = _model_block.get("hf_repo") or _model_block.get("id", "")
        llm_config["model_uri"] = f"hf://{hf_repo}" if hf_repo else ""
    if "vllm_args" not in llm_config:
        llm_config["vllm_args"] = llm_config.get("vllm_config", {})

    # Parse workloads
    workloads = []
    multiplier = resolved.get("observe", {}).get("request_multiplier", 1)
    for wl_path_str in manifest["workloads"]:
        wl_path = REPO_ROOT / wl_path_str
        wl_data = yaml.safe_load(wl_path.read_text())
        if "name" not in wl_data and "workload_name" not in wl_data:
            wl_data["workload_name"] = Path(wl_path_str).stem
        if multiplier > 1 and "num_requests" in wl_data:
            wl_data["num_requests"] = int(wl_data["num_requests"] * multiplier)
        workloads.append(wl_data)

    # Resolve inference-sim image tag for blis observe container
    inference_sim_dir = REPO_ROOT / "inference-sim"
    blis_tag_result = run(
        ["git", "describe", "--tags"],
        check=False, capture=True, cwd=inference_sim_dir,
    )
    blis_tag = blis_tag_result.stdout.strip() if blis_tag_result.returncode == 0 else ""

    # Build algorithm values
    vllm_image = resolved.get("stack", {}).get("model", {}).get("vllm_image", "")
    observe = {"workloads": workloads}
    if blis_tag:
        observe["image"] = f"ghcr.io/inference-sim/blis:{blis_tag}"
    alg_values = {
        "stack": {
            "model": {
                "modelName": llm_config.get("model_name", ""),
                "modelArtifacts": {"uri": llm_config.get("model_uri", "")},
            },
        },
        "observe": observe,
    }
    if vllm_image:
        alg_values["stack"]["model"]["vllmImage"] = vllm_image

    # Translate BLIS vllm_config params to vLLM CLI flags for the helm chart.
    # The chart expects containers[].args as a list of "--flag=value" strings.
    # Mapping per blis_router/CLUSTER.md and the deployment comment in llm_config.yaml.
    # total_kv_blocks is a calibration reference only — not a vLLM CLI flag.
    # max_model_len: 0 means "unlimited (model native)" in BLIS — omit the flag.
    _BLIS_TO_VLLM_FLAG = {
        "gpu_memory_utilization":   "--gpu-memory-utilization",
        "block_size_in_tokens":     "--block-size",
        "max_num_running_reqs":     "--max-num-seqs",
        "max_num_scheduled_tokens": "--max-num-batched-tokens",
        "max_model_len":            "--max-model-len",
        "kv_events_config":         "--kv-events-config",
    }
    vllm_config_raw = llm_config.get("vllm_args", {})
    tp = llm_config.get("serving", {}).get("tensor_parallelism", 1)
    replicas = llm_config.get("cluster", {}).get("num_instances", 1)
    vllm_args_list: list[str] = []
    if tp is not None and int(tp) > 1:
        vllm_args_list.append(f"--tensor-parallel-size={int(tp)}")
    for blis_key, cli_flag in _BLIS_TO_VLLM_FLAG.items():
        val = vllm_config_raw.get(blis_key)
        if val is None:
            continue
        if blis_key == "max_model_len" and val == 0:
            continue
        vllm_args_list.append(f"{cli_flag}={val}")
    decode_block: dict = {"replicas": replicas}
    # Set GAIE_RELEASE_NAME_POSTFIX so the kv-events-config endpoint resolves correctly.
    # The EPP service is named sim2real-{run_name}-gaie-epp by the Tekton deploy-gaie task.
    run_name = out_path.parent.name
    container_entry: dict = {"env": [{"name": "GAIE_RELEASE_NAME_POSTFIX",
                                      "value": f"sim2real-{run_name}"}]}
    if vllm_args_list:
        container_entry["args"] = vllm_args_list
    decode_block["containers"] = [container_entry]
    alg_values["stack"]["model"]["helmValues"] = {"decode": decode_block}

    # Embed treatment EPP config based on treatment_config_generated flag
    output_path = out_path.parent / "translation_output.json"
    if output_path.exists():
        translation_output = json.loads(output_path.read_text())
        treatment_config_generated = translation_output.get("treatment_config_generated", False)
    else:
        treatment_config_generated = False

    if treatment_config_generated:
        tc_path = out_path.parent / "treatment_config.yaml"
        if not tc_path.exists():
            raise RuntimeError(
                f"treatment_config_generated=true but treatment_config.yaml "
                f"not found at {tc_path}")
        tc_content = tc_path.read_text()
        (alg_values["stack"]
         .setdefault("gaie", {})
         .setdefault("treatment", {})
         .setdefault("helmValues", {})
         .setdefault("inferenceExtension", {})
         ["pluginsCustomConfig"]) = {"custom-plugins.yaml": tc_content}
    else:
        # treatment_config_generated=false — copy baseline EPP config to treatment slot
        baseline_cfg = (resolved
                        .get("stack", {})
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
            warnings.warn(
                "treatment_config_generated=false and baseline has no EPP config; "
                "treatment pluginsCustomConfig will be empty",
                UserWarning,
                stacklevel=2,
            )

    # Embed baseline EPP config if derived in Phase 2 (overrides env_defaults static value)
    baseline_cfg_path = out_path.parent / "baseline_config.yaml"
    if baseline_cfg_path.exists():
        bc_content = baseline_cfg_path.read_text()
        (alg_values["stack"]
         .setdefault("gaie", {})
         .setdefault("baseline", {})
         .setdefault("helmValues", {})
         .setdefault("inferenceExtension", {})
         ["pluginsCustomConfig"]) = {"custom-plugins.yaml": bc_content}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.dump(alg_values, default_flow_style=False, sort_keys=False))


def _compile_cluster_packages(run_dir: Path, resolved: dict, values_path: Path,
                               setup_config: dict):
    """Compile cluster YAMLs organized by package (baseline, treatment)."""
    cluster_dir = run_dir / "cluster"
    namespace = setup_config.get("namespace", "default")
    if not setup_config.get("namespace"):
        warn("namespace not found in setup_config.json; using 'default'")

    values = yaml.safe_load(values_path.read_text())
    workloads = values.get("observe", {}).get("workloads", [])

    # Inject workload_name from manifest file stems when workload dicts lack a name.
    # values.yaml embeds workload data without preserving source filenames, so we
    # cross-reference the manifest by index to get a stable, human-readable name.
    si_path = run_dir / "skill_input.json"
    if si_path.exists():
        try:
            si = json.loads(si_path.read_text())
            manifest = yaml.safe_load(Path(si["manifest_path"]).read_text()) or {}
            wl_paths = manifest.get("workloads", [])
            for i, wl in enumerate(workloads):
                if "name" not in wl and "workload_name" not in wl and i < len(wl_paths):
                    wl["workload_name"] = Path(wl_paths[i]).stem
        except Exception:
            pass

    tektonc_dir = REPO_ROOT / "tektonc-data-collection" / "tektoncsample" / "sim2real"
    run_name = run_dir.name
    compiled_pipelines: dict = {}

    for package in ["baseline", "treatment"]:
        pkg_dir = cluster_dir / package
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # Compile Tekton Pipeline YAML for this phase
        pr_path = pkg_dir / f"{package}-pipeline.yaml"
        if tektonc_dir.exists():
            ok_flag = compile_pipeline(tektonc_dir, values_path, package, pkg_dir)
            if not ok_flag:
                warn(f"compile_pipeline failed for {package}; writing stub")
                pr_path.write_text(f"# compile_pipeline failed for {package}\n")
        else:
            pr_path.write_text(f"# PipelineRun stub for {package}\n"
                               f"# tektonc-data-collection not available\n")

        # Load compiled pipeline for the experiment combiner and per-package pipelineruns
        if pr_path.exists() and not pr_path.read_text().startswith("#"):
            try:
                compiled_pipelines[package] = yaml.safe_load(pr_path.read_text())
            except Exception:
                pass

        # Remove any stale per-workload pipelineruns from earlier pipeline versions
        for stale in pkg_dir.glob("pipelinerun-workload-*.yaml"):
            stale.unlink()

        # Generate a single sequential Pipeline + PipelineRun for standalone package execution
        if package in compiled_pipelines:
            phase_pipeline, phase_pr = make_phase_pipeline(
                package, workloads, compiled_pipelines[package],
                run_name, namespace,
                workspace_bindings=setup_config.get("workspaces"),
            )
            (pkg_dir / f"sim2real-{package}-pipeline.yaml").write_text(
                yaml.dump(phase_pipeline, default_flow_style=False, allow_unicode=True)
            )
            (pkg_dir / f"pipelinerun-{package}.yaml").write_text(
                yaml.dump(phase_pr, default_flow_style=False, allow_unicode=True)
            )

    # Generate the single sequential experiment pipeline (all baselines then all
    # treatments, one after another). This replaces the per-workload PipelineRuns.
    if len(compiled_pipelines) == 2:
        phase_workloads = (
            [("baseline", wl.get("name", wl.get("workload_name", f"workload-{i}")), wl)
             for i, wl in enumerate(workloads)]
            + [("treatment", wl.get("name", wl.get("workload_name", f"workload-{i}")), wl)
               for i, wl in enumerate(workloads)]
        )
        experiment_pipeline, experiment_pr = make_experiment_pipeline(
            phase_workloads, compiled_pipelines, run_name, namespace,
            workspace_bindings=setup_config.get("workspaces"),
        )
        exp_dir = cluster_dir / "experiment"
        exp_dir.mkdir(parents=True, exist_ok=True)
        (exp_dir / "experiment-pipeline.yaml").write_text(
            yaml.dump(experiment_pipeline, default_flow_style=False, allow_unicode=True))
        (exp_dir / "pipelinerun-experiment.yaml").write_text(
            yaml.dump(experiment_pr, default_flow_style=False, allow_unicode=True))
        ok(f"Experiment pipeline: {exp_dir.relative_to(REPO_ROOT)}/")
    else:
        warn("Could not generate experiment pipeline: missing compiled phase pipelines")

    ok(f"Cluster packages: {cluster_dir.relative_to(REPO_ROOT)}")


def _verify_generated_dir(run_dir: Path):
    """Verify the generated/ directory exists with expected files."""
    generated_dir = run_dir / "generated"
    if not generated_dir.exists():
        warn("generated/ directory not found — translation skill should create this.")
        warn("Continuing without generated file copies.")
        return

    output = json.loads((run_dir / "translation_output.json").read_text())
    for f in output.get("files_created", []) + output.get("files_modified", []):
        if not (generated_dir / Path(f).name).exists():
            warn(f"generated/ missing: {Path(f).name}")


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
            # Search register_file's directory recursively: Go plugins use constants
            # defined in sibling files (e.g. AdaptiveV2Type = "adaptive-v2-scorer" in
            # adaptive_v2.go, referenced as scorer.AdaptiveV2Type in register.go).
            plugins_dir = register_path.parent
            found = any(
                plugin_type in f.read_text()
                for f in plugins_dir.rglob("*.go")
            )
            if not found:
                errors.append(
                    f"plugin_type '{plugin_type}' not found in {register_file} or adjacent plugin files")
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


# ── Phase 5: Summary ─────────────────────────────────────────────────────────

def _phase_summary(state: StateMachine, manifest: dict, run_dir: Path, resolved: dict):
    """Phase 5: Generate run_summary.md."""
    step(5, "Summary")

    if state.is_done("summary") and not getattr(state, '_force', False):
        info("[skip] Summary already complete")
        return

    output = json.loads((run_dir / "translation_output.json").read_text())
    translate_meta = state.get_phase("translate")

    lines = [
        f"**Run Summary: `{state.run_name}`**",
        f"Generated: {datetime.now(timezone.utc).isoformat()} | Scenario: {manifest['scenario']}",
        "",
        "**Algorithm**",
        f"- Source: `{manifest['algorithm']['source']}`",
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

    # Baseline vs Treatment config comparison
    lines.extend(["", "**Packages**", ""])
    cluster_dir = run_dir / "cluster"
    exp_pr = cluster_dir / "experiment" / "pipelinerun-experiment.yaml"
    if exp_pr.exists():
        lines.append("- `experiment/` — pipelinerun-experiment.yaml (sequential)")
    elif cluster_dir.exists():
        for pkg_dir in sorted(cluster_dir.iterdir()):
            if pkg_dir.is_dir() and any(pkg_dir.glob("pipelinerun-*.yaml")):
                pr_names = ", ".join(p.name for p in sorted(pkg_dir.glob("pipelinerun-*.yaml")))
                lines.append(f"- `{pkg_dir.name}/` — {pr_names}")

    # Workloads
    lines.extend(["", "**Workloads**", ""])
    multiplier = resolved.get("observe", {}).get("request_multiplier", 1)
    for wl in manifest["workloads"]:
        wl_name = Path(wl).stem
        lines.append(f"- {wl_name} (x{multiplier})")

    # Checklist
    lines.extend([
        "", "**Checklist**",
        "- [x] Translation complete",
        "- [x] Assembly complete",
        "- [x] validate-assembly passed",
        "",
    ])

    summary_path = run_dir / "run_summary.md"
    summary_path.write_text("\n".join(lines))
    state.mark_done("summary")
    ok(f"Summary: {summary_path.relative_to(REPO_ROOT)}")


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
    ok("Pipeline complete. Deploy with: python pipeline/deploy.py")


def _cmd_context(args, manifest, run_dir):
    """Rebuild context cache only."""
    state = _phase_init(args, manifest, run_dir)
    args.rebuild_context = True
    _phase_context(args, state, manifest, run_dir)


def _cmd_assemble(args, manifest, run_dir):
    """Re-run assembly from existing translation output."""
    try:
        state = StateMachine.load(run_dir)
    except FileNotFoundError:
        err("No state file. Run prepare.py first.")
        sys.exit(1)

    if not state.is_done("translate"):
        err("Cannot assemble: translation not complete. Run /sim2real-translate first.")
        sys.exit(1)

    resolved = _load_resolved_config(manifest)
    state.reset("assembly")
    state.reset("summary")
    state.reset("gate")
    _phase_assembly(args, state, manifest, run_dir, resolved)
    _phase_summary(state, manifest, run_dir, resolved)
    _phase_gate(state, run_dir)


def _cmd_validate_assembly(args, manifest, run_dir):
    """Run validate-assembly checks standalone."""
    required = ["translation_output.json"]
    for name in required:
        if not (run_dir / name).exists():
            err(f"Required file missing: {name}. Run translation skill first.")
            sys.exit(1)
    resolved = _load_resolved_config(manifest)
    _validate_assembly(run_dir, resolved)


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
                   help="Path to transfer.yaml (default: config/transfer.yaml)")
    p.add_argument("--run", metavar="NAME",
                   help="Override run name")

    sub = p.add_subparsers(dest="command")
    sub.add_parser("context", help="Rebuild context cache only")
    sub.add_parser("assemble", help="Reproduce cluster YAMLs from existing translation")
    sub.add_parser("validate-assembly", help="Validate assembly consistency (standalone)")
    sub.add_parser("status", help="Show current run state")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    manifest_path = args.manifest or str(REPO_ROOT / "config" / "transfer.yaml")
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as e:
        err(str(e))
        sys.exit(1)

    setup_config = _load_setup_config()
    run_name = args.run or setup_config.get("current_run", _default_run_name())
    run_dir = REPO_ROOT / "workspace" / "runs" / run_name

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
