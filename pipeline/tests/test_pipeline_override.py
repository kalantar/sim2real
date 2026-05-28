"""Tests for pipeline manifest override in prepare.py and deploy.py."""
import pytest
import yaml

from pipeline.lib.manifest import load_manifest, ManifestError
from pipeline.lib.tekton import make_pipelinerun_scenario
from pipeline.tests.test_manifest import MINIMAL_V3


def test_pipelinerun_uses_custom_pipeline_name():
    """make_pipelinerun_scenario embeds the pipeline_name in pipelineRef."""
    pr = make_pipelinerun_scenario(
        phase="baseline",
        workload={"name": "share-gpt", "num_requests": 10},
        run_name="run-001",
        namespace="sim2real-slot-0",
        pipeline_name="custom-pipeline",
        scenario_content="kind: scenario",
    )
    assert pr["spec"]["pipelineRef"]["name"] == "custom-pipeline"


def test_pipelinerun_uses_default_pipeline_name():
    """Default pipeline name is 'sim2real'."""
    pr = make_pipelinerun_scenario(
        phase="treatment",
        workload={"name": "share-gpt", "num_requests": 10},
        run_name="run-001",
        namespace="sim2real-slot-0",
        pipeline_name="sim2real",
        scenario_content="kind: scenario",
    )
    assert pr["spec"]["pipelineRef"]["name"] == "sim2real"


def test_manifest_rejects_absolute_pipeline_yaml(tmp_path):
    """Absolute pipeline.yaml path is rejected at manifest load time."""
    data = {**MINIMAL_V3, "pipeline": {"yaml": "/etc/passwd"}}
    p = tmp_path / "transfer.yaml"
    p.write_text(yaml.dump(data))
    with pytest.raises(ManifestError, match="relative path"):
        load_manifest(p)


def test_manifest_rejects_empty_pipeline_name(tmp_path):
    """Empty pipeline.name is rejected."""
    data = {**MINIMAL_V3, "pipeline": {"name": "", "yaml": "pipeline/pipeline.yaml"}}
    p = tmp_path / "transfer.yaml"
    p.write_text(yaml.dump(data))
    with pytest.raises(ManifestError, match="pipeline.name.*non-empty"):
        load_manifest(p)


def test_manifest_rejects_non_string_pipeline_name(tmp_path):
    """Non-string pipeline.name (e.g. YAML boolean) is rejected."""
    data = {**MINIMAL_V3, "pipeline": {"name": True, "yaml": "pipeline/pipeline.yaml"}}
    p = tmp_path / "transfer.yaml"
    p.write_text(yaml.dump(data))
    with pytest.raises(ManifestError, match="pipeline.name.*non-empty"):
        load_manifest(p)


def test_manifest_rejects_non_string_pipeline_yaml(tmp_path):
    """Non-string pipeline.yaml (e.g. YAML integer) is rejected."""
    data = {**MINIMAL_V3, "pipeline": {"name": "sim2real", "yaml": 42}}
    p = tmp_path / "transfer.yaml"
    p.write_text(yaml.dump(data))
    with pytest.raises(ManifestError, match="pipeline.yaml.*non-empty"):
        load_manifest(p)


def test_deploy_rejects_traversal_path(tmp_path):
    """deploy.py rejects pipeline.yaml that resolves outside REPO_ROOT."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    manifest = {"pipeline": {"yaml": "../../etc/passwd"}}
    pipeline_yaml_rel = manifest.get("pipeline", {}).get("yaml", "pipeline/pipeline.yaml")
    pipeline_yaml = (repo_root / pipeline_yaml_rel).resolve()
    assert not pipeline_yaml.is_relative_to(repo_root.resolve())
