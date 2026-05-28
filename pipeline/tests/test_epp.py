"""Tests for EPP image injection (pipeline/lib/epp.py)."""

import yaml

from pipeline.lib.assemble import assemble_scenarios
from pipeline.lib.epp import inject_epp_image, inject_image_ref
from pipeline.lib.tekton import make_pipelinerun_scenario


class TestInjectEppImage:
    """Unit tests for inject_epp_image."""

    def test_treatment_gets_epp_image(self):
        """BC-1: Treatment scenario entries get the EPP image injected."""
        scenario = {
            "scenario": [
                {"name": "test-scenario", "images": {"vllm": {"repository": "r", "tag": "t"}}},
            ]
        }
        result = inject_epp_image(scenario, "ghcr.io/me", "llm-d-inference-scheduler", "run-1")
        assert result is True
        img = scenario["scenario"][0]["images"]["inferenceScheduler"]
        assert img == {
            "repository": "ghcr.io/me/llm-d-inference-scheduler",
            "tag": "run-1",
            "pullPolicy": "Always",
        }

    def test_treatment_multiple_entries(self):
        """BC-1: All scenario entries get injected, not just the first."""
        scenario = {
            "scenario": [
                {"name": "entry-1"},
                {"name": "entry-2"},
            ]
        }
        result = inject_epp_image(scenario, "reg.io", "epp", "v1")
        assert result is True
        for entry in scenario["scenario"]:
            assert entry["images"]["inferenceScheduler"]["repository"] == "reg.io/epp"
            assert entry["images"]["inferenceScheduler"]["tag"] == "v1"

    def test_overwrites_existing_inference_scheduler(self):
        """BC-1: Pre-existing inferenceScheduler image is replaced, not preserved.

        Guards against regression to setdefault("inferenceScheduler", epp_img)
        which would silently produce an A/A experiment.
        """
        scenario = {
            "scenario": [
                {"name": "s", "images": {"inferenceScheduler": {"repository": "old/repo", "tag": "old-tag", "pullPolicy": "IfNotPresent"}}},
            ]
        }
        inject_epp_image(scenario, "ghcr.io/new", "epp", "run-99")
        img = scenario["scenario"][0]["images"]["inferenceScheduler"]
        assert img["repository"] == "ghcr.io/new/epp"
        assert img["tag"] == "run-99"
        assert img["pullPolicy"] == "Always"

    def test_empty_registry_skips(self):
        """BC-3: Empty registry string skips injection entirely."""
        scenario = {"scenario": [{"name": "test"}]}
        result = inject_epp_image(scenario, "", "repo", "tag")
        assert result is False
        assert "images" not in scenario["scenario"][0]

    def test_no_scenario_entries_skips(self):
        """BC-3 variant: No scenario entries means no injection."""
        scenario = {"scenario": []}
        result = inject_epp_image(scenario, "reg.io", "repo", "tag")
        assert result is False

    def test_missing_scenario_key_skips(self):
        """BC-3 variant: Missing 'scenario' key means no injection."""
        scenario = {"other_key": "value"}
        result = inject_epp_image(scenario, "reg.io", "repo", "tag")
        assert result is False

    def test_existing_images_preserved(self):
        """BC-1: Other image entries (e.g. vllm) are not clobbered."""
        scenario = {
            "scenario": [
                {"name": "s", "images": {"vllm": {"repository": "vllm-r", "tag": "vllm-t"}}},
            ]
        }
        inject_epp_image(scenario, "reg.io", "epp", "v2")
        assert scenario["scenario"][0]["images"]["vllm"] == {"repository": "vllm-r", "tag": "vllm-t"}
        assert scenario["scenario"][0]["images"]["inferenceScheduler"]["tag"] == "v2"


class TestInjectImageRef:
    def test_injects_complete_ref(self):
        scenario = {"scenario": [{"name": "s1"}]}
        result = inject_image_ref(scenario, "ghcr.io/org/scheduler", "abc12345")
        assert result is True
        img = scenario["scenario"][0]["images"]["inferenceScheduler"]
        assert img["repository"] == "ghcr.io/org/scheduler"
        assert img["tag"] == "abc12345"
        assert img["pullPolicy"] == "Always"

    def test_returns_false_for_empty_scenario(self):
        scenario = {"scenario": []}
        assert inject_image_ref(scenario, "r", "t") is False

    def test_returns_false_for_missing_scenario_key(self):
        scenario = {}
        assert inject_image_ref(scenario, "r", "t") is False

    def test_preserves_existing_images(self):
        scenario = {"scenario": [{"name": "s1", "images": {"vllm": {"repository": "vllm-r", "tag": "vllm-t"}}}]}
        inject_image_ref(scenario, "ghcr.io/org/scheduler", "tag1")
        assert scenario["scenario"][0]["images"]["vllm"] == {"repository": "vllm-r", "tag": "vllm-t"}
        assert scenario["scenario"][0]["images"]["inferenceScheduler"]["tag"] == "tag1"


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False))


_WORKSPACE_BINDINGS = {
    "data-storage": {"persistentVolumeClaim": {"claimName": "data-pvc"}},
    "source": {"persistentVolumeClaim": {"claimName": "source-pvc"}},
}


class TestEppIntegration:
    """Integration: assemble → inject → serialize → make_pipelinerun_scenario."""

    def test_epp_image_in_pipelinerun_scenario_content(self, tmp_path):
        """BC-4: EPP image string appears in the final PipelineRun scenarioContent param."""
        baseline_data = {
            "scenario": [{
                "name": "test-scenario",
                "model": {"name": "TestModel"},
                "images": {"inferenceScheduler": {"repository": "ghcr.io/orig", "tag": "v1"}},
            }]
        }
        treatment_overlay = {
            "scenario": [{
                "name": "test-scenario",
                "inferenceExtension": {"pluginsConfigFile": "plugins.yaml"},
            }]
        }
        _write(tmp_path / "baseline.yaml", baseline_data)
        _write(tmp_path / "generated" / "baseline_config.yaml", {})
        _write(tmp_path / "generated" / "treatment_config.yaml", treatment_overlay)

        _, treatment_resolved = assemble_scenarios(
            baseline_path=tmp_path / "baseline.yaml",
            treatment_path=None,
            baseline_overlay_path=tmp_path / "generated" / "baseline_config.yaml",
            treatment_overlay_path=tmp_path / "generated" / "treatment_config.yaml",
        )

        # Inject EPP image (simulating what prepare.py does)
        inject_epp_image(treatment_resolved, "ghcr.io/test", "my-epp", "run-42")

        # Serialize (same as prepare.py line 452)
        scenario_content = yaml.dump(treatment_resolved, default_flow_style=False, allow_unicode=True)

        # Generate PipelineRun (same as prepare.py line 457)
        pr = make_pipelinerun_scenario(
            phase="treatment",
            workload={"name": "integration-wl"},
            run_name="run-42",
            namespace="test-ns",
            pipeline_name="sim2real",
            scenario_content=scenario_content,
            workspace_bindings=_WORKSPACE_BINDINGS,
        )

        params = {p["name"]: p["value"] for p in pr["spec"]["params"]}
        content = params["scenarioContent"]

        # The EPP image MUST appear in the serialized scenario content
        assert "ghcr.io/test/my-epp" in content
        assert "run-42" in content

        # Verify it's valid YAML and the structure is correct
        parsed = yaml.safe_load(content)
        img = parsed["scenario"][0]["images"]["inferenceScheduler"]
        assert img["repository"] == "ghcr.io/test/my-epp"
        assert img["tag"] == "run-42"
        assert img["pullPolicy"] == "Always"

    def test_baseline_pipelinerun_has_no_epp_injection(self, tmp_path):
        """BC-2: Baseline PipelineRun does NOT contain injected EPP image."""
        baseline_data = {
            "scenario": [{
                "name": "test-scenario",
                "images": {"inferenceScheduler": {"repository": "ghcr.io/orig", "tag": "v1"}},
            }]
        }
        _write(tmp_path / "baseline.yaml", baseline_data)
        _write(tmp_path / "generated" / "baseline_config.yaml", {})
        _write(tmp_path / "generated" / "treatment_config.yaml", {})

        baseline_resolved, treatment_resolved = assemble_scenarios(
            baseline_path=tmp_path / "baseline.yaml",
            treatment_path=None,
            baseline_overlay_path=tmp_path / "generated" / "baseline_config.yaml",
            treatment_overlay_path=tmp_path / "generated" / "treatment_config.yaml",
        )

        # Only inject into treatment (as prepare.py does)
        inject_epp_image(treatment_resolved, "ghcr.io/test", "my-epp", "run-42")

        # Serialize baseline (NOT treatment)
        baseline_content = yaml.dump(baseline_resolved, default_flow_style=False, allow_unicode=True)

        pr = make_pipelinerun_scenario(
            phase="baseline",
            workload={"name": "integration-wl"},
            run_name="run-42",
            namespace="test-ns",
            pipeline_name="sim2real",
            scenario_content=baseline_content,
            workspace_bindings=_WORKSPACE_BINDINGS,
        )

        params = {p["name"]: p["value"] for p in pr["spec"]["params"]}
        content = params["scenarioContent"]

        # Baseline must NOT have the injected EPP image
        assert "ghcr.io/test/my-epp" not in content
        # It should still have the original image
        assert "ghcr.io/orig" in content
