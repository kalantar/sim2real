"""GPU capacity probe for deploy.py orchestrator."""

import json
import subprocess
from pathlib import Path
from typing import Union

import yaml

from pipeline.lib.values import deep_merge


# ── Cluster probe ──────────────────────────────────────────────────────────────


def probe_free_gpus(
    gpu_resource_type: str = "nvidia.com/gpu",
) -> Union[tuple[int, int, int], str]:
    """Return (free_gpus, total_allocatable, total_requested) or error string.

    Queries kubectl for node allocatable resources and pod requests,
    computes the delta clamped to zero.

    Skips pods without spec.nodeName — these are Pending (unscheduled) and
    have not been allocated any node resources.

    Assumes only spec.containers request GPUs (initContainers are excluded —
    llm-d workloads do not use GPU-requesting init containers).

    Note: the two kubectl calls are not atomic — cluster state may change
    between them. Acceptable for logging; consumers that gate on capacity
    (#64) should account for this.
    """
    try:
        nodes_result = subprocess.run(
            ["kubectl", "get", "nodes", "-o", "json"],
            check=False, text=True, capture_output=True,
        )
        if nodes_result.returncode != 0:
            return nodes_result.stderr.strip() or "kubectl get nodes failed"

        pods_result = subprocess.run(
            ["kubectl", "get", "pods", "--all-namespaces",
             "--field-selector=status.phase!=Succeeded,status.phase!=Failed",
             "-o", "json"],
            check=False, text=True, capture_output=True,
        )
        if pods_result.returncode != 0:
            return pods_result.stderr.strip() or "kubectl get pods failed"

    except OSError as e:
        return str(e)

    try:
        nodes = json.loads(nodes_result.stdout)
        pods = json.loads(pods_result.stdout)
    except json.JSONDecodeError as e:
        return f"JSON parse error: {e}"

    total_allocatable = 0
    for node in nodes.get("items", []):
        alloc = node.get("status", {}).get("allocatable", {})
        count = alloc.get(gpu_resource_type)
        if count is not None:
            try:
                total_allocatable += int(count)
            except ValueError:
                return f"non-integer allocatable value {count!r} on node {node.get('metadata', {}).get('name', '?')}"

    total_requested = 0
    for pod in pods.get("items", []):
        if pod.get("spec", {}).get("nodeName") is None:
            continue
        for container in pod.get("spec", {}).get("containers", []):
            requests = container.get("resources", {}).get("requests", {})
            count = requests.get(gpu_resource_type)
            if count is not None:
                try:
                    total_requested += int(count)
                except ValueError:
                    return f"non-integer request value {count!r} in pod {pod.get('metadata', {}).get('name', '?')}"

    free = max(0, total_allocatable - total_requested)
    return (free, total_allocatable, total_requested)


# ── GPU cost derivation ────────────────────────────────────────────────────────


def derive_gpu_resource_type(resolved_scenario: dict, defaults: dict) -> str:
    """Derive the Kubernetes GPU resource name from scenario + defaults.

    Merges the first scenario entry over defaults, then reads
    accelerator.resource. Falls back to "nvidia.com/gpu".
    """
    scenario_entry = {}
    scenarios = resolved_scenario.get("scenario", [])
    if scenarios:
        scenario_entry = scenarios[0]

    merged = deep_merge(defaults, scenario_entry)
    return merged.get("accelerator", {}).get("resource", "nvidia.com/gpu")


def gpu_cost_per_pair(resolved_scenario: dict, defaults: dict) -> Union[int, str]:
    """Compute total GPU cost for one baseline/treatment pair.

    Merges the first scenario entry over defaults, then sums GPU cost
    across enabled roles using 3-level precedence:
      role.accelerator.count > accelerator.count > tensor * dataLocal

    The middle tier (accelerator.count as per-role fallback) extends the
    Jinja template's 2-level logic — kept intentionally so that top-level
    accelerator.count propagates to roles that don't override it.

    Returns int on success, or error string describing the problematic field.
    """
    scenario_entry = {}
    scenarios = resolved_scenario.get("scenario", [])
    if scenarios:
        scenario_entry = scenarios[0]

    merged = deep_merge(defaults, scenario_entry)

    top_accel = merged.get("accelerator", {})
    top_count_raw = top_accel.get("count")
    if top_count_raw is not None:
        try:
            if int(top_count_raw) == 0:
                return 0
        except (ValueError, TypeError):
            return f"accelerator.count={top_count_raw!r} is not a valid integer"

    gpu_cost = 0
    for role_name, default_enabled, default_replicas in [
        ("decode", True, 1),
        ("prefill", False, 0),
    ]:
        role_cfg = merged.get(role_name, {})
        if not role_cfg.get("enabled", default_enabled):
            continue

        replicas = role_cfg.get("replicas", default_replicas)
        parallelism = role_cfg.get("parallelism", {})

        role_accel = role_cfg.get("accelerator", {})
        try:
            if "count" in role_accel:
                gpus_per_pod = int(role_accel["count"])
            elif top_count_raw is not None:
                gpus_per_pod = int(top_count_raw)
            else:
                gpus_per_pod = parallelism.get("tensor", 1) * parallelism.get("dataLocal", 1)
        except (ValueError, TypeError):
            field = f"{role_name}.accelerator.count" if "count" in role_accel else "accelerator.count"
            val = role_accel.get("count") if "count" in role_accel else top_count_raw
            return f"{field}={val!r} is not a valid integer"

        gpu_cost += replicas * gpus_per_pod

    return gpu_cost


def load_defaults(repo_root: Path) -> Union[dict, str, None]:
    """Load llm-d-benchmark defaults.yaml.

    Returns:
        dict: parsed defaults on success.
        None: file not found (expected when submodule not initialized).
        str: error message when file exists but can't be parsed.
    """
    defaults_path = repo_root / "llm-d-benchmark" / "config" / "templates" / "values" / "defaults.yaml"
    if not defaults_path.exists():
        return None
    try:
        return yaml.safe_load(defaults_path.read_text()) or {}
    except yaml.YAMLError as e:
        return f"defaults.yaml parse error: {e}"
    except OSError as e:
        return f"defaults.yaml read error: {e}"
