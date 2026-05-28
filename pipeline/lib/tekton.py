"""Tekton PipelineRun generation for sim2real."""
import yaml

_SPEC_BASE_DIR = "/workspace/source/llm-d-benchmark"
_SCENARIO_FILE_PATH = "/tmp/llmdbench-config/scenario.yaml"


def _default_spec_content(base_dir: str = _SPEC_BASE_DIR,
                          scenario_file: str = _SCENARIO_FILE_PATH) -> str:
    """Return the llmdbenchmark spec content string with PVC paths."""
    return (
        f"base_dir: {base_dir}\n"
        f"\n"
        f"values_file:\n"
        f"  path: {base_dir}/config/templates/values/defaults.yaml\n"
        f"\n"
        f"template_dir:\n"
        f"  path: {base_dir}/config/templates/jinja\n"
        f"\n"
        f"scenario_file:\n"
        f"  path: {scenario_file}\n"
    )


def _apply_workspace_bindings(ws_names: list, bindings: dict) -> list:
    """Map workspace names to their PVC/secret bindings.

    Falls back to a PVC claim named after the workspace for any unmapped name.
    """
    return [
        {"name": name, **bindings.get(name, {"persistentVolumeClaim": {"claimName": name}})}
        for name in ws_names
    ]


def make_pipelinerun_scenario(
    phase: str,
    workload: dict,
    run_name: str,
    namespace: str,
    pipeline_name: str,
    scenario_content: str,
    workspace_bindings: dict | None = None,
    spec_content: str | None = None,
    benchmark_git_commit: str = "",
    benchmark_git_repo_url: str = "",
    blis_git_commit: str = "",
    blis_git_repo_url: str = "",
    model: str = "",
) -> dict:
    """Generate a PipelineRun with resolved scenario content."""
    if spec_content is None:
        spec_content = _default_spec_content()
    wl_name = workload.get("name", workload.get("workload_name", "unknown"))
    safe_name = wl_name.replace("_", "-")
    safe_phase = phase.replace("_", "-")
    pr_name = f"{safe_phase}-{safe_name}-{run_name}"

    wl_spec = {k: v for k, v in workload.items() if k != "workload_name"}
    wl_spec_str = yaml.dump(wl_spec, default_flow_style=True).strip()

    spec: dict = {
        "pipelineRef": {"name": pipeline_name},
        "params": [
            {"name": "experimentId",      "value": run_name},
            {"name": "runName",           "value": run_name},
            {"name": "namespace",         "value": namespace},
            {"name": "phase",             "value": phase},
            {"name": "scenarioContent",   "value": scenario_content},
            {"name": "specContent",       "value": spec_content},
            {"name": "workloadName",      "value": wl_name},
            {"name": "workloadSpec",      "value": wl_spec_str},
            {"name": "benchmarkGitRepoUrl", "value": benchmark_git_repo_url},
            {"name": "benchmarkGitCommit", "value": benchmark_git_commit},
            {"name": "blisGitRepoUrl",   "value": blis_git_repo_url},
            {"name": "blisGitCommit",     "value": blis_git_commit},
            {"name": "model",            "value": model},
        ],
        "timeouts": {"pipeline": "4h"},
    }

    if workspace_bindings is not None:
        ws_names = list(workspace_bindings.keys())
        spec["workspaces"] = _apply_workspace_bindings(ws_names, workspace_bindings)

    return {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": {"name": pr_name, "namespace": namespace},
        "spec": spec,
    }


