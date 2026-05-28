"""Tests for scenario assembly (baseline/treatment merge logic)."""
import pytest
import yaml

from pipeline.lib.assemble import assemble_scenarios
from pipeline.lib.assemble import assemble_packages, AssemblyError, inject_hf_secret_name


BASELINE = {
    "scenario": [{
        "name": "admission-control",
        "model": {"name": "Qwen/Qwen3-14B", "maxModelLen": 40960},
        "images": {
            "vllm": {"repository": "ghcr.io/llm-d/llm-d-cuda", "tag": "v0.6.0"},
            "inferenceScheduler": {"repository": "ghcr.io/llm-d/llm-d-inference-scheduler", "tag": "v0.7.1"},
        },
        "inferenceExtension": {
            "verbosity": "3",
            "inferencePoolProviderConfig": {"destinationRule": {"trafficPolicy": {}}},
        },
        "decode": {"replicas": 4},
    }],
}

BASELINE_OVERLAY = {
    "scenario": [{
        "name": "admission-control",
        "inferenceExtension": {
            "pluginsConfigFile": "custom-plugins.yaml",
            "pluginsCustomConfig": {
                "custom-plugins.yaml": "kind: EndpointPickerConfig\nplugins:\n- type: random-picker\n",
            },
        },
        "extraObjects": [
            {"apiVersion": "v1alpha2", "kind": "InferenceObjective", "metadata": {"name": "critical"}, "spec": {"priority": 100}},
        ],
    }],
}

TREATMENT_DIFFS = {
    "scenario": [{
        "name": "admission-control",
        "images": {
            "inferenceScheduler": {"repository": "ghcr.io/kalantar/llm-d-inference-scheduler", "tag": "ac"},
        },
    }],
}

TREATMENT_OVERLAY = {
    "scenario": [{
        "name": "admission-control",
        "inferenceExtension": {
            "pluginsConfigFile": "custom-plugins.yaml",
            "pluginsCustomConfig": {
                "custom-plugins.yaml": "kind: EndpointPickerConfig\nplugins:\n- type: quintic-shed\n",
            },
        },
    }],
}

BASELINE_ALT = {
    "scenario": [{
        "name": "admission-control",
        "model": {"name": "Qwen/Qwen3-14B", "maxModelLen": 40960},
        "images": {
            "vllm": {"repository": "ghcr.io/llm-d/llm-d-cuda", "tag": "v0.6.0"},
            "inferenceScheduler": {"repository": "ghcr.io/llm-d/llm-d-inference-scheduler", "tag": "v0.7.1"},
        },
        "inferenceExtension": {"verbosity": "3"},
        "decode": {"replicas": 8},
    }],
}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False))


def test_baseline_merge(tmp_path):
    """Baseline + baseline_overlay produces merged scenario with plugin config and extraObjects."""
    _write(tmp_path / "baseline.yaml", BASELINE)
    _write(tmp_path / "generated" / "baseline_config.yaml", BASELINE_OVERLAY)
    _write(tmp_path / "generated" / "treatment_config.yaml", {})

    bl, _ = assemble_scenarios(
        baseline_path=tmp_path / "baseline.yaml",
        treatment_path=None,
        baseline_overlay_path=tmp_path / "generated" / "baseline_config.yaml",
        treatment_overlay_path=tmp_path / "generated" / "treatment_config.yaml",
    )

    sc = bl["scenario"][0]
    assert sc["inferenceExtension"]["pluginsConfigFile"] == "custom-plugins.yaml"
    assert "custom-plugins.yaml" in sc["inferenceExtension"]["pluginsCustomConfig"]
    assert sc["extraObjects"][0]["kind"] == "InferenceObjective"
    assert sc["model"]["name"] == "Qwen/Qwen3-14B"


def test_treatment_merge(tmp_path):
    """Treatment = baseline + treatment diffs + treatment overlay."""
    _write(tmp_path / "baseline.yaml", BASELINE)
    _write(tmp_path / "treatment.yaml", TREATMENT_DIFFS)
    _write(tmp_path / "generated" / "baseline_config.yaml", BASELINE_OVERLAY)
    _write(tmp_path / "generated" / "treatment_config.yaml", TREATMENT_OVERLAY)

    bl, tr = assemble_scenarios(
        baseline_path=tmp_path / "baseline.yaml",
        treatment_path=tmp_path / "treatment.yaml",
        baseline_overlay_path=tmp_path / "generated" / "baseline_config.yaml",
        treatment_overlay_path=tmp_path / "generated" / "treatment_config.yaml",
    )

    bl_sc = bl["scenario"][0]
    tr_sc = tr["scenario"][0]

    assert bl_sc["images"]["inferenceScheduler"]["tag"] == "v0.7.1"
    assert tr_sc["images"]["inferenceScheduler"]["tag"] == "ac"
    assert tr_sc["images"]["inferenceScheduler"]["repository"] == "ghcr.io/kalantar/llm-d-inference-scheduler"

    assert "random-picker" in bl_sc["inferenceExtension"]["pluginsCustomConfig"]["custom-plugins.yaml"]
    assert "quintic-shed" in tr_sc["inferenceExtension"]["pluginsCustomConfig"]["custom-plugins.yaml"]

    assert tr_sc["model"]["name"] == "Qwen/Qwen3-14B"
    assert tr_sc["decode"]["replicas"] == 4


def test_absent_treatment_uses_baseline(tmp_path):
    """When treatment.yaml is absent, treatment == baseline + treatment overlay."""
    _write(tmp_path / "baseline.yaml", BASELINE)
    _write(tmp_path / "generated" / "baseline_config.yaml", BASELINE_OVERLAY)
    _write(tmp_path / "generated" / "treatment_config.yaml", TREATMENT_OVERLAY)

    bl, tr = assemble_scenarios(
        baseline_path=tmp_path / "baseline.yaml",
        treatment_path=None,
        baseline_overlay_path=tmp_path / "generated" / "baseline_config.yaml",
        treatment_overlay_path=tmp_path / "generated" / "treatment_config.yaml",
    )

    assert bl["scenario"][0]["images"]["inferenceScheduler"]["tag"] == "v0.7.1"
    assert tr["scenario"][0]["images"]["inferenceScheduler"]["tag"] == "v0.7.1"
    assert "quintic-shed" in tr["scenario"][0]["inferenceExtension"]["pluginsCustomConfig"]["custom-plugins.yaml"]


def test_missing_overlays_passthrough(tmp_path):
    """When overlay files don't exist, baseline passes through unchanged."""
    _write(tmp_path / "baseline.yaml", BASELINE)

    bl, tr = assemble_scenarios(
        baseline_path=tmp_path / "baseline.yaml",
        treatment_path=None,
        baseline_overlay_path=tmp_path / "generated" / "baseline_config.yaml",
        treatment_overlay_path=tmp_path / "generated" / "treatment_config.yaml",
    )

    assert bl["scenario"][0]["model"]["name"] == "Qwen/Qwen3-14B"
    assert bl == tr


def test_overlay_preserves_existing_fields(tmp_path):
    """Overlay adds fields without clobbering unrelated existing fields."""
    _write(tmp_path / "baseline.yaml", BASELINE)
    _write(tmp_path / "generated" / "baseline_config.yaml", BASELINE_OVERLAY)
    _write(tmp_path / "generated" / "treatment_config.yaml", {})

    bl, _ = assemble_scenarios(
        baseline_path=tmp_path / "baseline.yaml",
        treatment_path=None,
        baseline_overlay_path=tmp_path / "generated" / "baseline_config.yaml",
        treatment_overlay_path=tmp_path / "generated" / "treatment_config.yaml",
    )

    sc = bl["scenario"][0]
    assert sc["inferenceExtension"]["verbosity"] == "3"
    assert sc["inferenceExtension"]["inferencePoolProviderConfig"] == {"destinationRule": {"trafficPolicy": {}}}
    assert sc["images"]["vllm"]["tag"] == "v0.6.0"


# ── assemble_packages tests ────────────────────────────────────────────────

def test_assemble_packages_single_baseline(tmp_path):
    """Single baseline with no algorithms produces one package."""
    _write(tmp_path / "b1.yaml", BASELINE)
    pkgs = assemble_packages(
        baselines=[{"name": "b1", "scenario_path": tmp_path / "b1.yaml"}],
        algorithms=[],
        generated_dir=tmp_path / "generated",
    )
    assert len(pkgs) == 1
    assert pkgs[0].name == "b1"
    assert pkgs[0].kind == "baseline"
    assert pkgs[0].resolved["scenario"][0]["model"]["name"] == "Qwen/Qwen3-14B"


def test_assemble_packages_multi_baseline(tmp_path):
    """Two baselines produce two packages in declaration order."""
    _write(tmp_path / "b1.yaml", BASELINE)
    _write(tmp_path / "b2.yaml", BASELINE_ALT)
    pkgs = assemble_packages(
        baselines=[
            {"name": "b1", "scenario_path": tmp_path / "b1.yaml"},
            {"name": "b2", "scenario_path": tmp_path / "b2.yaml"},
        ],
        algorithms=[],
        generated_dir=tmp_path / "generated",
    )
    assert len(pkgs) == 2
    assert pkgs[0].name == "b1"
    assert pkgs[1].name == "b2"
    assert pkgs[0].resolved["scenario"][0]["decode"]["replicas"] == 4
    assert pkgs[1].resolved["scenario"][0]["decode"]["replicas"] == 8


def test_assemble_packages_baseline_with_defaults(tmp_path):
    """Baseline with defaults merges defaults under scenario."""
    defaults = {"scenario": [{"name": "admission-control", "decode": {"replicas": 2}, "model": {"name": "default-model"}}]}
    override = {"scenario": [{"name": "admission-control", "decode": {"replicas": 8}}]}
    _write(tmp_path / "defaults.yaml", defaults)
    _write(tmp_path / "b1.yaml", override)
    pkgs = assemble_packages(
        baselines=[{"name": "b1", "scenario_path": tmp_path / "b1.yaml", "defaults_path": tmp_path / "defaults.yaml"}],
        algorithms=[],
        generated_dir=tmp_path / "generated",
    )
    sc = pkgs[0].resolved["scenario"][0]
    assert sc["decode"]["replicas"] == 8
    assert sc["model"]["name"] == "default-model"


def test_assemble_packages_with_algorithm(tmp_path):
    """Algorithm derives from its default baseline's resolved config."""
    _write(tmp_path / "b1.yaml", BASELINE)
    _write(tmp_path / "treatment.yaml", TREATMENT_DIFFS)
    _write(tmp_path / "generated" / "b1_config.yaml", BASELINE_OVERLAY)
    _write(tmp_path / "generated" / "ac1_config.yaml", TREATMENT_OVERLAY)
    pkgs = assemble_packages(
        baselines=[{"name": "b1", "scenario_path": tmp_path / "b1.yaml"}],
        algorithms=[{"name": "ac1", "scenario_path": tmp_path / "treatment.yaml", "defaults": "b1"}],
        generated_dir=tmp_path / "generated",
        overlays_expected=True,
    )
    assert len(pkgs) == 2
    assert pkgs[0].kind == "baseline"
    assert pkgs[1].kind == "algorithm"
    assert pkgs[1].resolved["scenario"][0]["images"]["inferenceScheduler"]["tag"] == "ac"
    assert "quintic-shed" in pkgs[1].resolved["scenario"][0]["inferenceExtension"]["pluginsCustomConfig"]["custom-plugins.yaml"]


def test_assemble_packages_shared_baseline_overlay(tmp_path):
    """When per-baseline overlay missing, shared baseline_config.yaml applies to all."""
    _write(tmp_path / "b1.yaml", BASELINE)
    _write(tmp_path / "b2.yaml", BASELINE_ALT)
    _write(tmp_path / "generated" / "baseline_config.yaml", BASELINE_OVERLAY)
    pkgs = assemble_packages(
        baselines=[
            {"name": "b1", "scenario_path": tmp_path / "b1.yaml"},
            {"name": "b2", "scenario_path": tmp_path / "b2.yaml"},
        ],
        algorithms=[],
        generated_dir=tmp_path / "generated",
    )
    for pkg in pkgs:
        assert pkg.resolved["scenario"][0]["inferenceExtension"]["pluginsConfigFile"] == "custom-plugins.yaml"


def test_assemble_packages_algorithm_unknown_baseline_raises(tmp_path):
    """Algorithm referencing unknown baseline name raises AssemblyError."""
    _write(tmp_path / "b1.yaml", BASELINE)
    with pytest.raises(AssemblyError, match="unknown baseline.*nonexistent"):
        assemble_packages(
            baselines=[{"name": "b1", "scenario_path": tmp_path / "b1.yaml"}],
            algorithms=[{"name": "ac1", "scenario_path": tmp_path / "t.yaml", "defaults": "nonexistent"}],
            generated_dir=tmp_path / "generated",
        )


class TestInjectHfSecretName:
    """inject_hf_secret_name sets huggingface.secretName on all scenario entries."""

    def test_injects_into_scenario_entries(self):
        scenario_dict = {"scenario": [
            {"name": "baseline", "model": {"name": "Qwen/Qwen3-14B"}},
        ]}
        result = inject_hf_secret_name(scenario_dict, "hf-secret")
        assert result is True
        assert scenario_dict["scenario"][0]["huggingface"] == {"secretName": "hf-secret"}

    def test_preserves_existing_huggingface_fields(self):
        scenario_dict = {"scenario": [
            {"name": "baseline", "huggingface": {"existingField": "value"}},
        ]}
        inject_hf_secret_name(scenario_dict, "my-token")
        assert scenario_dict["scenario"][0]["huggingface"] == {
            "existingField": "value",
            "secretName": "my-token",
        }

    def test_returns_false_for_empty_scenarios(self):
        assert inject_hf_secret_name({"scenario": []}, "hf-secret") is False
        assert inject_hf_secret_name({}, "hf-secret") is False

    def test_does_not_overwrite_explicit_secret_name(self):
        scenario_dict = {"scenario": [
            {"name": "baseline", "huggingface": {"secretName": "explicit-override"}},
        ]}
        inject_hf_secret_name(scenario_dict, "hf-secret")
        assert scenario_dict["scenario"][0]["huggingface"]["secretName"] == "explicit-override"
