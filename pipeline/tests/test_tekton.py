"""Tests for Tekton PipelineRun generation."""

from pipeline.lib.tekton import make_pipelinerun_scenario


# ── Tests for make_pipelinerun_scenario ──────────────────────────────────────

_WORKSPACE_BINDINGS = {
    "data-storage":   {"persistentVolumeClaim": {"claimName": "data-pvc"}},
    "source":         {"persistentVolumeClaim": {"claimName": "source-pvc"}},
}

def test_make_pipelinerun_scenario_name():
    pr = make_pipelinerun_scenario(
        phase="baseline", workload={"name": "wl-smoke"}, run_name="ac",
        namespace="kalantar-0", pipeline_name="sim2real-ac",
        scenario_content="scenario: []",
        workspace_bindings=_WORKSPACE_BINDINGS,
    )
    assert pr["metadata"]["name"] == "baseline-wl-smoke-ac"
    assert pr["metadata"]["namespace"] == "kalantar-0"


def test_make_pipelinerun_scenario_params():
    pr = make_pipelinerun_scenario(
        phase="treatment", workload={"name": "chatbot-mid"}, run_name="ac",
        namespace="ns", pipeline_name="sim2real-ac",
        scenario_content="scenario:\n- name: test\n",
        workspace_bindings=_WORKSPACE_BINDINGS,
    )
    params = {p["name"]: p["value"] for p in pr["spec"]["params"]}
    assert params["phase"] == "treatment"
    assert params["scenarioContent"] == "scenario:\n- name: test\n"
    assert params["workloadName"] == "chatbot-mid"
    assert "gaieConfig" not in params
    assert "inferenceObjectives" not in params


def test_make_pipelinerun_scenario_spec_content_default():
    pr = make_pipelinerun_scenario(
        phase="baseline", workload={"name": "wl"}, run_name="r",
        namespace="ns", pipeline_name="sim2real-r",
        scenario_content="{}",
        workspace_bindings=_WORKSPACE_BINDINGS,
    )
    params = {p["name"]: p["value"] for p in pr["spec"]["params"]}
    assert "specContent" in params
    spec = params["specContent"]
    assert "/workspace/source/llm-d-benchmark" in spec
    assert "/tmp/llmdbench-config/scenario.yaml" in spec
    assert "values_file:" in spec
    assert "template_dir:" in spec


def test_make_pipelinerun_scenario_spec_content_custom():
    from pipeline.lib.tekton import make_pipelinerun_scenario
    custom_spec = "base_dir: /custom\nscenario_file:\n  path: /custom/scenario.yaml\n"
    pr = make_pipelinerun_scenario(
        phase="baseline", workload={"name": "wl"}, run_name="r",
        namespace="ns", pipeline_name="sim2real-r",
        scenario_content="{}",
        workspace_bindings=_WORKSPACE_BINDINGS,
        spec_content=custom_spec,
    )
    params = {p["name"]: p["value"] for p in pr["spec"]["params"]}
    assert params["specContent"] == custom_spec


def test_make_pipelinerun_scenario_workspace_bindings():
    pr = make_pipelinerun_scenario(
        phase="baseline", workload={"name": "wl"}, run_name="r",
        namespace="ns", pipeline_name="sim2real-r",
        scenario_content="{}",
        workspace_bindings=_WORKSPACE_BINDINGS,
    )
    ws_names = {ws["name"] for ws in pr["spec"]["workspaces"]}
    assert "source" in ws_names
    assert "data-storage" in ws_names
    assert "model-cache" not in ws_names
    assert "hf-credentials" not in ws_names


# ── Tests for phase name sanitization ─────────────────────────────────────────


def test_phase_name_in_pipelinerun():
    """Custom phase names appear in PipelineRun metadata and params."""
    pr = make_pipelinerun_scenario(
        phase="b1",
        workload={"name": "wl-smoke"},
        run_name="test-run",
        namespace="ns-0",
        pipeline_name="sim2real",
        scenario_content="scenario: []",
    )
    assert pr["metadata"]["name"] == "b1-wl-smoke-test-run"
    params = {p["name"]: p["value"] for p in pr["spec"]["params"]}
    assert params["phase"] == "b1"


def test_phase_underscore_sanitized_in_name():
    """Underscores in phase names are converted to hyphens in PipelineRun name."""
    pr = make_pipelinerun_scenario(
        phase="my_phase",
        workload={"name": "wl-smoke"},
        run_name="test-run",
        namespace="ns-0",
        pipeline_name="sim2real",
        scenario_content="scenario: []",
    )
    assert pr["metadata"]["name"] == "my-phase-wl-smoke-test-run"
    params = {p["name"]: p["value"] for p in pr["spec"]["params"]}
    assert params["phase"] == "my_phase"


def test_default_spec_content():
    """PipelineRun includes default spec content when none provided."""
    pr = make_pipelinerun_scenario(
        phase="baseline",
        workload={"name": "wl-smoke"},
        run_name="run-1",
        namespace="ns-0",
        pipeline_name="sim2real",
        scenario_content="scenario: []",
    )
    params = {p["name"]: p["value"] for p in pr["spec"]["params"]}
    assert "defaults.yaml" in params["specContent"]


def test_workspace_bindings():
    """Workspace bindings are applied when provided."""
    pr = make_pipelinerun_scenario(
        phase="baseline",
        workload={"name": "wl-smoke"},
        run_name="run-1",
        namespace="ns-0",
        pipeline_name="sim2real",
        scenario_content="scenario: []",
        workspace_bindings={"data-storage": {"persistentVolumeClaim": {"claimName": "my-pvc"}}},
    )
    assert "workspaces" in pr["spec"]
    ws = pr["spec"]["workspaces"]
    assert ws[0]["name"] == "data-storage"
    assert ws[0]["persistentVolumeClaim"]["claimName"] == "my-pvc"
