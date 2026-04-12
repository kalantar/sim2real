"""Tekton pipeline compilation for sim2real.

Extracted from tools/transfer_cli.py compile-pipeline subcommand.
"""
import copy
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

    tektonc = REPO_ROOT / "tektonc-data-collection" / "tektonc" / "tektonc.py"
    if not tektonc.exists():
        return False

    # Load values and inject phase-specific synthetic keys
    try:
        values = yaml.safe_load(values_path.read_text()) or {}
    except Exception:
        return False

    values["phase"] = phase
    gaie_key = "treatment" if phase == "treatment" else "baseline"
    values["gaie_config"] = (
        values.get("stack", {}).get("gaie", {}).get(gaie_key, {}).get("helmValues", {})
    )
    values["inference_objectives"] = (
        values.get("stack", {}).get("gaie", {}).get("inferenceObjectives", [])
    )

    # Write augmented values to a temp file for tektonc
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
                capture_output=True, text=True, shell=False, timeout=120
            )
        except subprocess.TimeoutExpired:
            return False
        except OSError:
            return False
        if r.returncode != 0:
            return False
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    return True


def _apply_workspace_bindings(ws_names: list, bindings: dict) -> list:
    """Map workspace names to their PVC/secret bindings.

    Falls back to a PVC claim named after the workspace for any unmapped name.
    """
    return [
        {"name": name, **bindings.get(name, {"persistentVolumeClaim": {"claimName": name}})}
        for name in ws_names
    ]


def make_pipelinerun(phase: str, workload: dict, run_name: str, namespace: str,
                     pipeline_name: str = "",
                     compiled_pipeline: dict | None = None,
                     workspace_bindings: dict | None = None) -> dict:
    """Generate a Tekton PipelineRun resource for a single workload."""
    wl_name = workload.get("name", workload.get("workload_name", "unknown"))
    safe_name = wl_name.replace("_", "-")
    pr_name = f"{phase}-{safe_name}-{run_name}"

    # Exclude injected housekeeping key from the workload spec passed to the pipeline
    wl_spec = {k: v for k, v in workload.items() if k != "workload_name"}
    wl_spec_str = yaml.dump(wl_spec, default_flow_style=True).strip()

    spec: dict = {
        "pipelineRef": {
            "name": pipeline_name or f"{phase}-pipeline"
        },
        "params": [
            {"name": "experimentId", "value": run_name},
            {"name": "runName", "value": run_name},
            {"name": "namespace", "value": namespace},
            {"name": "workloadName", "value": wl_name},
            {"name": "workloadSpec", "value": wl_spec_str},
        ],
    }
    if workspace_bindings is not None:
        # Derive workspace names from the compiled pipeline; fall back to binding keys
        if compiled_pipeline:
            ws_names = [ws["name"] for ws in compiled_pipeline.get("spec", {}).get("workspaces", [])]
        else:
            ws_names = list(workspace_bindings.keys())
        spec["workspaces"] = _apply_workspace_bindings(ws_names, workspace_bindings)

    return {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": {
            "name": pr_name,
            "namespace": namespace,
        },
        "spec": spec,
    }


def _prefix_task(
    task: dict,
    prefix: str,
    phase_task_names: set,
    extra_before: list,
    wload_param: str,
    wload_spec_param: str,
) -> dict:
    """Deep-copy a task, prefixing its name and updating all internal references.

    - Task name gets prefix prepended.
    - runAfter entries that name phase-internal tasks get prefixed; entries
      that reference external tasks (e.g. workspace names) are left alone.
    - extra_before is prepended to runAfter (used to chain sequential groups).
    - $(params.workloadName) and $(params.workloadSpec) are rewritten to the
      per-workload param names for this group.
    - $(tasks.ORIG.) result references are updated to $(tasks.PREFIX+ORIG.).
    """
    t = copy.deepcopy(task)
    t["name"] = prefix + t["name"]

    ra = [prefix + r if r in phase_task_names else r
          for r in t.get("runAfter", [])]
    ra = extra_before + ra
    if ra:
        t["runAfter"] = ra
    else:
        t.pop("runAfter", None)

    def _update_str(v):
        if not isinstance(v, str):
            return v
        for tn in phase_task_names:
            v = v.replace(f"$(tasks.{tn}.", f"$(tasks.{prefix}{tn}.")
        v = v.replace("$(params.workloadName)", f"$(params.{wload_param})")
        v = v.replace("$(params.workloadSpec)", f"$(params.{wload_spec_param})")
        return v

    for param in t.get("params", []):
        param["value"] = _update_str(param.get("value", ""))

    return t


def make_experiment_pipeline(
    phase_workloads: list,
    compiled_pipelines: dict,
    run_name: str,
    namespace: str,
    workspace_bindings: dict | None = None,
) -> tuple:
    """Generate one combined Pipeline + PipelineRun for all phase/workload combos.

    The pipeline runs each (phase, workload) group fully in sequence: the entry
    tasks of each group wait on the converted-finally tasks of the previous group.
    Tekton `finally` blocks are converted to regular tasks so they participate in
    the sequential chain.

    Args:
        phase_workloads: Ordered [(phase, workload_name, workload_dict), ...]
        compiled_pipelines: {phase: pipeline_yaml_dict} keyed by phase name
        run_name: Experiment run name (becomes experimentId and is used in naming)
        namespace: Kubernetes namespace

    Returns:
        (pipeline_dict, pipelinerun_dict)
    """
    safe_run = run_name.replace("_", "-")
    pipeline_name = f"sim2real-experiment-{safe_run}"
    pr_name = f"experiment-{safe_run}"

    # Shared params (identical across all workload groups)
    shared_params = [
        {"name": "namespace", "type": "string"},
        {"name": "experimentId", "type": "string"},
        {"name": "runName", "type": "string"},
        {"name": "sleepDuration", "type": "string", "default": "30s"},
    ]
    pr_params = [
        {"name": "namespace", "value": namespace},
        {"name": "experimentId", "value": run_name},
        {"name": "runName", "value": run_name},
    ]

    # Collect workspaces from all compiled pipelines (deduplicated by name)
    workspace_names_seen: set = set()
    all_workspaces: list = []
    for phase_pipeline in compiled_pipelines.values():
        for ws in phase_pipeline.get("spec", {}).get("workspaces", []):
            if ws["name"] not in workspace_names_seen:
                all_workspaces.append(ws)
                workspace_names_seen.add(ws["name"])

    all_tasks: list = []
    all_finally_tasks: list = []  # cleanup tasks for spec.finally (run on cancel/failure)
    anchor_tasks: list = []  # prefixed names of the last tasks of the previous group

    for phase, wload_name, wload_data in phase_workloads:
        safe_wl = wload_name.replace("_", "-")

        # Truncate safe_wl so that prefix + longest_task_name <= 63 (Tekton/k8s limit).
        # Compute max original task name length from the compiled phase pipeline.
        phase_all_tasks = (compiled_pipelines[phase].get("spec", {}).get("tasks", [])
                           + compiled_pipelines[phase].get("spec", {}).get("finally", []))
        max_orig = max((len(t["name"]) for t in phase_all_tasks), default=0)
        # prefix = "{phase}-{safe_wl}-"  → budget for safe_wl = 63 - len(phase) - 2 - max_orig
        budget = max(1, 63 - len(phase) - 2 - max_orig)
        safe_wl = safe_wl[:budget].rstrip("-")

        prefix = f"{phase}-{safe_wl}-"

        wload_param = f"workloadName-{phase}-{safe_wl}"
        wload_spec_param = f"workloadSpec-{phase}-{safe_wl}"

        # Exclude injected housekeeping key from the workload spec
        wl_spec = {k: v for k, v in wload_data.items() if k != "workload_name"}
        wl_spec_str = yaml.dump(wl_spec, default_flow_style=True).strip()

        shared_params.extend([
            {"name": wload_param, "type": "string"},
            {"name": wload_spec_param, "type": "string"},
        ])
        pr_params.extend([
            {"name": wload_param, "value": wload_name},
            {"name": wload_spec_param, "value": wl_spec_str},
        ])

        phase_spec = compiled_pipelines[phase].get("spec", {})
        phase_tasks = phase_spec.get("tasks", [])
        phase_finally = phase_spec.get("finally", [])
        phase_task_names = {t["name"] for t in phase_tasks + phase_finally}

        # Entry tasks: those with no runAfter — they wait on the previous group's anchor
        entry_names = {t["name"] for t in phase_tasks if not t.get("runAfter")}

        # Leaf tasks: no other regular task lists them in runAfter
        has_successor: set = set()
        for t in phase_tasks:
            for ra in t.get("runAfter", []):
                has_successor.add(ra)
        leaf_prefixed = [prefix + t["name"]
                         for t in phase_tasks if t["name"] not in has_successor]

        # Regular tasks
        for task in phase_tasks:
            extra = anchor_tasks if task["name"] in entry_names else []
            all_tasks.append(
                _prefix_task(task, prefix, phase_task_names, extra,
                             wload_param, wload_spec_param)
            )

        # Finally tasks → regular tasks, running after the leaf tasks of this group.
        # Also added to all_finally_tasks (no runAfter) so the combined pipeline's
        # spec.finally runs them on cancel or failure.
        finally_prefixed = []
        for task in phase_finally:
            all_tasks.append(
                _prefix_task(task, prefix, phase_task_names, leaf_prefixed,
                             wload_param, wload_spec_param)
            )
            finally_prefixed.append(prefix + task["name"])

            cleanup = _prefix_task(task, prefix, phase_task_names, [],
                                   wload_param, wload_spec_param)
            cleanup.pop("runAfter", None)
            # spec.finally names must be globally unique (across tasks + finally).
            # Append "-c" suffix (truncate base to 61 chars to stay within 63 limit).
            base = cleanup["name"]
            cleanup["name"] = (base[:61] if len(base) > 61 else base) + "-c"
            all_finally_tasks.append(cleanup)

        # Next group's anchor: finally tasks (or leaves if no finally block)
        anchor_tasks = finally_prefixed if finally_prefixed else leaf_prefixed

    pipeline_spec: dict = {
        "params": shared_params,
        "workspaces": all_workspaces,
        "tasks": all_tasks,
    }
    if all_finally_tasks:
        pipeline_spec["finally"] = all_finally_tasks

    pipeline = {
        "apiVersion": "tekton.dev/v1",
        "kind": "Pipeline",
        "metadata": {"name": pipeline_name},
        "spec": pipeline_spec,
    }

    pr_workspaces = _apply_workspace_bindings(
        [ws["name"] for ws in all_workspaces], workspace_bindings or {}
    )

    pipelinerun = {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": {"name": pr_name, "namespace": namespace},
        "spec": {
            "pipelineRef": {"name": pipeline_name},
            "params": pr_params,
            "workspaces": pr_workspaces,
        },
    }

    return pipeline, pipelinerun


def make_phase_pipeline(
    phase: str,
    workloads: list,
    compiled_pipeline: dict,
    run_name: str,
    namespace: str,
    workspace_bindings: dict | None = None,
) -> tuple:
    """Generate a sequential Pipeline + PipelineRun for all workloads in one phase.

    Runs workloads one after another (same sequential-chaining logic as
    make_experiment_pipeline, but scoped to a single phase).

    Returns:
        (pipeline_dict, pipelinerun_dict)
    """
    phase_workloads = [
        (phase, wl.get("name", wl.get("workload_name", f"workload-{i}")), wl)
        for i, wl in enumerate(workloads)
    ]
    pipeline, pr = make_experiment_pipeline(
        phase_workloads,
        {phase: compiled_pipeline},
        run_name,
        namespace,
        workspace_bindings=workspace_bindings,
    )
    # Rename so the pipeline and pipelinerun are clearly scoped to this phase
    safe_run = run_name.replace("_", "-")
    pipeline["metadata"]["name"] = f"sim2real-{phase}-{safe_run}"
    pr["metadata"]["name"] = f"{phase}-{safe_run}"
    pr["spec"]["pipelineRef"]["name"] = f"sim2real-{phase}-{safe_run}"
    return pipeline, pr
