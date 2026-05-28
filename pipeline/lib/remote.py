"""Remote run support — Kubernetes resource generation for --remote mode."""

from pathlib import Path

CONFIGMAP_NAME = "sim2real-run-inputs"
JOB_NAME = "sim2real-orchestrator"
SERVICE_ACCOUNT = "sim2real-runner"
MOUNT_BASE = "/data"


def build_run_inputs_configmap(
    *, run_dir: Path, workspace_dir: Path, namespace: str, run_name: str
) -> dict:
    setup_path = workspace_dir / "setup_config.json"
    if not setup_path.exists():
        raise FileNotFoundError(f"setup_config.json not found: {setup_path}")

    metadata_path = run_dir / "run_metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"run_metadata.json not found: {metadata_path}")

    data = {
        "setup_config.json": setup_path.read_text(),
        "run_metadata.json": metadata_path.read_text(),
    }

    cluster_dir = run_dir / "cluster"
    yaml_files = sorted(cluster_dir.glob("*.yaml")) if cluster_dir.is_dir() else []
    if not yaml_files:
        raise FileNotFoundError(
            f"No cluster YAML files in {cluster_dir} — run prepare.py first"
        )
    for yaml_file in yaml_files:
        data[f"cluster--{yaml_file.name}"] = yaml_file.read_text()

    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": CONFIGMAP_NAME, "namespace": namespace},
        "data": data,
    }


def _configmap_items(data: dict, run_name: str) -> list[dict]:
    """Build the items list that maps ConfigMap keys to filesystem paths."""
    items = []
    for key in sorted(data):
        if key == "setup_config.json":
            items.append({"key": key, "path": key})
        elif key == "run_metadata.json":
            items.append({"key": key, "path": f"runs/{run_name}/{key}"})
        elif key.startswith("cluster--"):
            filename = key[len("cluster--"):]
            items.append({"key": key, "path": f"runs/{run_name}/cluster/{filename}"})
        else:
            raise ValueError(
                f"Unrecognized ConfigMap key '{key}' — update _configmap_items "
                f"to handle this key or remove it from build_run_inputs_configmap"
            )
    return items


def build_orchestrator_job(
    *, namespace: str, image: str, run_name: str, run_flags: list[str],
    configmap_data: dict,
) -> dict:
    workspace_path = f"{MOUNT_BASE}/workspace"
    config_path = f"{MOUNT_BASE}/config"
    items = _configmap_items(configmap_data, run_name)

    args = [
        "--experiment-root", MOUNT_BASE,
        "--run", run_name,
        "run", "--skip-build",
    ] + run_flags

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": JOB_NAME, "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 18000,
            "template": {
                "spec": {
                    "serviceAccountName": SERVICE_ACCOUNT,
                    "restartPolicy": "Never",
                    "initContainers": [{
                        "name": "copy-inputs",
                        "image": image,
                        "command": ["cp", "-r", f"{config_path}/.", workspace_path],
                        "volumeMounts": [
                            {"name": "config", "mountPath": config_path, "readOnly": True},
                            {"name": "workspace", "mountPath": workspace_path},
                        ],
                    }],
                    "containers": [{
                        "name": "orchestrator",
                        "image": image,
                        "args": args,
                        "env": [{"name": "PYTHONUNBUFFERED", "value": "1"}],
                        "volumeMounts": [
                            {"name": "workspace", "mountPath": workspace_path},
                        ],
                    }],
                    "volumes": [
                        {
                            "name": "config",
                            "configMap": {
                                "name": CONFIGMAP_NAME,
                                "items": items,
                            },
                        },
                        {
                            "name": "workspace",
                            "emptyDir": {},
                        },
                    ],
                },
            },
        },
    }
