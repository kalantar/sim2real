"""Tests for remote run support (ConfigMap packing + Job generation)."""

import json

import pytest

from pipeline.lib.remote import (
    CONFIGMAP_NAME,
    JOB_NAME,
    build_orchestrator_job,
    build_run_inputs_configmap,
    _configmap_items,
)


@pytest.fixture()
def workspace(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    run_dir = workspace_dir / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    (run_dir / "cluster").mkdir()
    return workspace_dir, run_dir


def _write_defaults(workspace_dir, run_dir):
    setup = {"namespace": "ns", "pipeline_name": "p"}
    (workspace_dir / "setup_config.json").write_text(json.dumps(setup))
    meta = {"run_name": "test-run"}
    (run_dir / "run_metadata.json").write_text(json.dumps(meta))
    (run_dir / "cluster" / "pipelinerun-smoke-baseline.yaml").write_text("kind: PipelineRun")


def test_setup_config_packed(workspace):
    workspace_dir, run_dir = workspace
    _write_defaults(workspace_dir, run_dir)
    cm = build_run_inputs_configmap(
        run_dir=run_dir, workspace_dir=workspace_dir,
        namespace="ns", run_name="test-run",
    )
    assert cm["apiVersion"] == "v1"
    assert cm["kind"] == "ConfigMap"
    assert cm["metadata"]["name"] == CONFIGMAP_NAME
    assert cm["metadata"]["namespace"] == "ns"
    assert json.loads(cm["data"]["setup_config.json"]) == {
        "namespace": "ns", "pipeline_name": "p",
    }


def test_run_metadata_packed(workspace):
    workspace_dir, run_dir = workspace
    _write_defaults(workspace_dir, run_dir)
    cm = build_run_inputs_configmap(
        run_dir=run_dir, workspace_dir=workspace_dir,
        namespace="ns", run_name="test-run",
    )
    assert json.loads(cm["data"]["run_metadata.json"]) == {"run_name": "test-run"}


def test_cluster_yamls_packed(workspace):
    workspace_dir, run_dir = workspace
    _write_defaults(workspace_dir, run_dir)
    (run_dir / "cluster" / "baseline.yaml").write_text("base: true\n")
    (run_dir / "cluster" / "treatment.yaml").write_text("treat: true\n")
    cm = build_run_inputs_configmap(
        run_dir=run_dir, workspace_dir=workspace_dir,
        namespace="ns", run_name="test-run",
    )
    assert cm["data"]["cluster--baseline.yaml"] == "base: true\n"
    assert cm["data"]["cluster--treatment.yaml"] == "treat: true\n"


def test_missing_setup_config(workspace):
    workspace_dir, run_dir = workspace
    (run_dir / "run_metadata.json").write_text("{}")
    with pytest.raises(FileNotFoundError, match="setup_config.json"):
        build_run_inputs_configmap(
            run_dir=run_dir, workspace_dir=workspace_dir,
            namespace="ns", run_name="test-run",
        )


def test_missing_run_metadata(workspace):
    workspace_dir, run_dir = workspace
    (workspace_dir / "setup_config.json").write_text("{}")
    with pytest.raises(FileNotFoundError, match="run_metadata.json"):
        build_run_inputs_configmap(
            run_dir=run_dir, workspace_dir=workspace_dir,
            namespace="ns", run_name="test-run",
        )


def test_empty_cluster_dir_raises(workspace):
    """Empty cluster/ directory raises FileNotFoundError."""
    workspace_dir, run_dir = workspace
    (workspace_dir / "setup_config.json").write_text("{}")
    (run_dir / "run_metadata.json").write_text("{}")
    with pytest.raises(FileNotFoundError, match="No cluster YAML"):
        build_run_inputs_configmap(
            run_dir=run_dir, workspace_dir=workspace_dir,
            namespace="ns", run_name="test-run",
        )


def test_missing_cluster_dir_raises(tmp_path):
    """Missing cluster/ directory raises FileNotFoundError."""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    run_dir = workspace_dir / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    (workspace_dir / "setup_config.json").write_text("{}")
    (run_dir / "run_metadata.json").write_text("{}")
    with pytest.raises(FileNotFoundError, match="No cluster YAML"):
        build_run_inputs_configmap(
            run_dir=run_dir, workspace_dir=workspace_dir,
            namespace="ns", run_name="test-run",
        )


# --- _configmap_items tests ---


def test_configmap_items_setup_config_at_root():
    """setup_config.json maps to workspace root."""
    data = {"setup_config.json": "{}"}
    items = _configmap_items(data, "run1")
    assert {"key": "setup_config.json", "path": "setup_config.json"} in items


def test_configmap_items_run_metadata_under_run():
    """run_metadata.json maps under runs/<name>/."""
    data = {"run_metadata.json": "{}"}
    items = _configmap_items(data, "run1")
    assert {"key": "run_metadata.json", "path": "runs/run1/run_metadata.json"} in items


def test_configmap_items_cluster_yaml_nested():
    """cluster--foo.yaml maps to runs/<name>/cluster/foo.yaml."""
    data = {"cluster--baseline.yaml": "x", "cluster--pipelinerun-a.yaml": "y"}
    items = _configmap_items(data, "exp-1")
    paths = {i["path"] for i in items}
    assert "runs/exp-1/cluster/baseline.yaml" in paths
    assert "runs/exp-1/cluster/pipelinerun-a.yaml" in paths


def test_configmap_items_raises_on_unrecognized_key():
    """Unrecognized keys raise ValueError (producer/consumer mismatch)."""
    data = {"setup_config.json": "{}", "unknown_file.txt": "data"}
    with pytest.raises(ValueError, match="Unrecognized"):
        _configmap_items(data, "run1")


# --- build_orchestrator_job tests ---

RUN_NAME = "exp-001"
NAMESPACE = "sim2real"
IMAGE = "ghcr.io/inference-sim/sim2real-orchestrator:latest"
SAMPLE_DATA = {
    "setup_config.json": "{}",
    "run_metadata.json": "{}",
    "cluster--baseline.yaml": "x",
}


def _build_job(**overrides):
    defaults = dict(
        namespace=NAMESPACE, image=IMAGE, run_name=RUN_NAME,
        run_flags=["--dry-run"], configmap_data=SAMPLE_DATA,
    )
    defaults.update(overrides)
    return build_orchestrator_job(**defaults)


def test_job_name_matches_constant():
    assert JOB_NAME == "sim2real-orchestrator"


def test_job_structure():
    job = _build_job()
    assert job["kind"] == "Job"
    assert job["metadata"]["name"] == JOB_NAME
    assert job["metadata"]["namespace"] == NAMESPACE

    spec = job["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "sim2real-runner"
    assert spec["restartPolicy"] == "Never"

    container = spec["containers"][0]
    assert container["image"] == IMAGE
    args = container["args"]
    assert "--experiment-root" in args
    assert "run" in args
    assert "--skip-build" in args
    assert "--dry-run" in args


def test_job_orchestrator_unbuffered():
    job = _build_job()
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["PYTHONUNBUFFERED"] == "1"


def test_job_workspace_is_writable_emptydir():
    """Orchestrator mounts a writable emptyDir at /data/workspace."""
    job = _build_job()
    spec = job["spec"]["template"]["spec"]
    mount = spec["containers"][0]["volumeMounts"][0]
    assert mount["name"] == "workspace"
    assert mount["mountPath"] == "/data/workspace"
    ws_vol = next(v for v in spec["volumes"] if v["name"] == "workspace")
    assert ws_vol == {"name": "workspace", "emptyDir": {}}


def test_job_configmap_volume_is_read_only():
    """ConfigMap is mounted read-only at /data/config with items spec."""
    job = _build_job()
    spec = job["spec"]["template"]["spec"]
    cfg_vol = next(v for v in spec["volumes"] if v["name"] == "config")
    assert cfg_vol["configMap"]["name"] == CONFIGMAP_NAME
    items = cfg_vol["configMap"]["items"]
    paths = {i["path"] for i in items}
    assert "setup_config.json" in paths
    assert f"runs/{RUN_NAME}/run_metadata.json" in paths
    assert f"runs/{RUN_NAME}/cluster/baseline.yaml" in paths


def test_job_has_init_container_that_copies_inputs():
    """initContainer copies ConfigMap contents to the writable workspace."""
    job = _build_job()
    spec = job["spec"]["template"]["spec"]
    init = spec["initContainers"][0]
    assert init["name"] == "copy-inputs"
    assert init["command"] == ["cp", "-r", "/data/config/.", "/data/workspace"]
    mounts = {m["name"]: m for m in init["volumeMounts"]}
    assert mounts["config"]["readOnly"] is True
    assert "workspace" in mounts


def test_job_experiment_root_matches_mount():
    job = _build_job()
    args = job["spec"]["template"]["spec"]["containers"][0]["args"]
    idx = args.index("--experiment-root")
    assert args[idx + 1] == "/data"


def test_job_backoff_limit_zero():
    job = _build_job()
    assert job["spec"]["backoffLimit"] == 0


def test_job_active_deadline():
    job = _build_job()
    assert job["spec"]["activeDeadlineSeconds"] == 18000
