"""Tests for pipeline.lib.tekton module."""
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── Fixture: minimal compiled Pipeline dict (mirrors sim2real/pipeline.yaml.j2) ──

_COMPILED_PIPELINE = {
    "apiVersion": "tekton.dev/v1",
    "kind": "Pipeline",
    "metadata": {"name": "sim2real-baseline"},
    "spec": {
        "params": [
            {"name": "experimentId", "type": "string"},
            {"name": "namespace", "type": "string"},
            {"name": "sleepDuration", "type": "string", "default": "30s"},
            {"name": "runName", "type": "string"},
            {"name": "workloadName", "type": "string"},
            {"name": "workloadSpec", "type": "string"},
        ],
        "workspaces": [
            {"name": "model-cache"},
            {"name": "hf-credentials"},
            {"name": "data-storage"},
        ],
        "tasks": [
            {"name": "download-model", "taskRef": {"name": "download-model"}, "params": []},
            {"name": "deploy-gateway", "taskRef": {"name": "deploy-gateway"}, "params": []},
            {"name": "deploy-gaie", "taskRef": {"name": "deploy-gaie"}, "params": []},
            {
                "name": "deploy-model",
                "taskRef": {"name": "deploy-model"},
                "runAfter": ["download-model", "deploy-gaie"],
                "params": [],
            },
            {
                "name": "pause-after-model-deploy",
                "taskRef": {"name": "sleep"},
                "runAfter": ["deploy-model"],
                "params": [{"name": "duration", "value": "$(params.sleepDuration)"}],
            },
            {
                "name": "deploy-httproute",
                "taskRef": {"name": "deploy-httproute"},
                "runAfter": ["deploy-gateway", "deploy-gaie"],
                "params": [],
            },
            {
                "name": "deploy-inference-objectives",
                "taskRef": {"name": "deploy-inference-objectives"},
                "runAfter": ["deploy-gaie"],
                "params": [],
            },
            {
                "name": "stream-epp-logs",
                "taskRef": {"name": "stream-epp-logs"},
                "runAfter": ["deploy-inference-objectives"],
                "params": [{"name": "workloadName", "value": "$(params.workloadName)"}],
            },
            {
                "name": "run-workload",
                "taskRef": {"name": "run-workload-blis-observe"},
                "runAfter": [
                    "pause-after-model-deploy",
                    "deploy-httproute",
                    "deploy-inference-objectives",
                ],
                "params": [
                    {"name": "workloadSpec", "value": "$(params.workloadSpec)"},
                    {"name": "workloadName", "value": "$(params.workloadName)"},
                ],
            },
            {
                "name": "collect-results",
                "taskRef": {"name": "collect-results"},
                "runAfter": ["run-workload"],
                "params": [],
            },
        ],
        "finally": [
            {"name": "delete-inference-objectives", "taskRef": {"name": "delete-inference-objectives"}, "params": []},
            {"name": "delete-httproute", "taskRef": {"name": "delete-httproute"}, "params": []},
            {"name": "delete-model", "taskRef": {"name": "delete-model"}, "params": []},
            {"name": "delete-gaie", "taskRef": {"name": "delete-gaie"}, "params": []},
            {"name": "delete-gateway", "taskRef": {"name": "delete-gateway"}, "params": []},
        ],
    },
}



def test_returns_false_when_tektonc_absent(tmp_path):
    """When tektonc binary is missing, compile_pipeline returns False."""
    from pipeline.lib import tekton

    # Patch REPO_ROOT so tektonc path points to nonexistent location
    with patch.object(tekton, "REPO_ROOT", tmp_path):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "pipeline.yaml.j2").write_text("dummy template")

        values_file = tmp_path / "values.yaml"
        values_file.write_text("phase: treatment\n")

        out_dir = tmp_path / "out"

        result = tekton.compile_pipeline(template_dir, values_file, "treatment", out_dir)
        assert result is False


def test_returns_true_on_subprocess_success(tmp_path):
    """When subprocess succeeds, compile_pipeline returns True."""
    from pipeline.lib import tekton

    # Patch REPO_ROOT and create tektonc binary
    with patch.object(tekton, "REPO_ROOT", tmp_path):
        tektonc_path = tmp_path / "tektonc-data-collection" / "tektonc"
        tektonc_path.mkdir(parents=True)
        (tektonc_path / "tektonc.py").write_text("#!/usr/bin/env python3\nprint('dummy')")

        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "pipeline.yaml.j2").write_text("dummy template")

        values_file = tmp_path / "values.yaml"
        values_file.write_text("phase: treatment\n")

        out_dir = tmp_path / "out"

        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            result = tekton.compile_pipeline(template_dir, values_file, "treatment", out_dir)
            assert result is True


def test_returns_false_on_subprocess_failure(tmp_path):
    """When subprocess fails, compile_pipeline returns False."""
    from pipeline.lib import tekton

    # Patch REPO_ROOT and create tektonc binary
    with patch.object(tekton, "REPO_ROOT", tmp_path):
        tektonc_path = tmp_path / "tektonc-data-collection" / "tektonc"
        tektonc_path.mkdir(parents=True)
        (tektonc_path / "tektonc.py").write_text("#!/usr/bin/env python3\nprint('dummy')")

        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "pipeline.yaml.j2").write_text("dummy template")

        values_file = tmp_path / "values.yaml"
        values_file.write_text("phase: treatment\n")

        out_dir = tmp_path / "out"

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "compilation error"
        with patch("subprocess.run", return_value=mock_result):
            result = tekton.compile_pipeline(template_dir, values_file, "treatment", out_dir)
            assert result is False


def test_uses_unified_template_when_present(tmp_path):
    """When both unified and phase-specific templates exist, unified takes precedence."""
    from pipeline.lib import tekton

    # Patch REPO_ROOT and create tektonc binary
    with patch.object(tekton, "REPO_ROOT", tmp_path):
        tektonc_path = tmp_path / "tektonc-data-collection" / "tektonc"
        tektonc_path.mkdir(parents=True)
        (tektonc_path / "tektonc.py").write_text("#!/usr/bin/env python3\nprint('dummy')")

        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        # Create both templates
        (template_dir / "pipeline.yaml.j2").write_text("unified template")
        (template_dir / "treatment-pipeline.yaml.j2").write_text("phase-specific template")

        values_file = tmp_path / "values.yaml"
        values_file.write_text("phase: treatment\n")

        out_dir = tmp_path / "out"

        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = tekton.compile_pipeline(template_dir, values_file, "treatment", out_dir)
            assert result is True

            # Verify subprocess.run was called with the unified template
            call_args = mock_run.call_args[0][0]
            # call_args is the list passed to subprocess.run
            # Format: [sys.executable, str(tektonc), "-t", str(template_file), "-f", tmp_file, "-o", str(out_file)]
            template_arg_index = call_args.index("-t") + 1
            template_used = Path(call_args[template_arg_index])
            assert template_used.name == "pipeline.yaml.j2"


# ── make_standby_pipeline tests ───────────────────────────────────────────────


def test_standby_pipeline_removes_workload_tasks():
    """make_standby_pipeline strips run-workload, collect-results, stream-epp-logs."""
    from pipeline.lib.tekton import make_standby_pipeline, _WORKLOAD_TASK_NAMES

    pipeline, _ = make_standby_pipeline("baseline", _COMPILED_PIPELINE, "run-1", "test-ns")
    task_names = {t["name"] for t in pipeline["spec"]["tasks"]}
    assert task_names.isdisjoint(_WORKLOAD_TASK_NAMES), (
        f"Workload tasks still present: {task_names & _WORKLOAD_TASK_NAMES}"
    )


def test_standby_pipeline_adds_standby_task():
    """make_standby_pipeline adds a 'standby' sleep task."""
    from pipeline.lib.tekton import make_standby_pipeline, _STANDBY_SLEEP_DURATION

    pipeline, _ = make_standby_pipeline("baseline", _COMPILED_PIPELINE, "run-1", "test-ns")
    task_names = {t["name"] for t in pipeline["spec"]["tasks"]}
    assert "standby" in task_names

    standby = next(t for t in pipeline["spec"]["tasks"] if t["name"] == "standby")
    assert standby["taskRef"]["name"] == "sleep"
    assert any(
        p["name"] == "duration" and p["value"] == _STANDBY_SLEEP_DURATION
        for p in standby.get("params", [])
    )


def test_standby_pipeline_keeps_finally_block():
    """make_standby_pipeline preserves spec.finally unchanged."""
    from pipeline.lib.tekton import make_standby_pipeline

    pipeline, _ = make_standby_pipeline("baseline", _COMPILED_PIPELINE, "run-1", "test-ns")
    finally_names = {t["name"] for t in pipeline["spec"]["finally"]}
    expected = {"delete-inference-objectives", "delete-httproute", "delete-model",
                "delete-gaie", "delete-gateway"}
    assert finally_names == expected


def test_standby_pipeline_strips_workload_params():
    """make_standby_pipeline removes workloadName and workloadSpec from pipeline params."""
    from pipeline.lib.tekton import make_standby_pipeline, _WORKLOAD_PARAM_NAMES

    pipeline, _ = make_standby_pipeline("baseline", _COMPILED_PIPELINE, "run-1", "test-ns")
    param_names = {p["name"] for p in pipeline["spec"]["params"]}
    assert param_names.isdisjoint(_WORKLOAD_PARAM_NAMES), (
        f"Workload params still present: {param_names & _WORKLOAD_PARAM_NAMES}"
    )


def test_standby_pipeline_naming():
    """make_standby_pipeline uses standby-scoped names for Pipeline and PipelineRun."""
    from pipeline.lib.tekton import make_standby_pipeline

    pipeline, pr = make_standby_pipeline("treatment", _COMPILED_PIPELINE, "my-run", "ns")
    assert pipeline["metadata"]["name"] == "sim2real-treatment-standby-my-run"
    assert pr["metadata"]["name"] == "treatment-standby-my-run"
    assert pr["spec"]["pipelineRef"]["name"] == "sim2real-treatment-standby-my-run"


def test_standby_pipeline_standby_runs_after_leaves():
    """The standby task waits on all leaf deploy tasks."""
    from pipeline.lib.tekton import make_standby_pipeline

    pipeline, _ = make_standby_pipeline("baseline", _COMPILED_PIPELINE, "run-1", "test-ns")
    standby = next(t for t in pipeline["spec"]["tasks"] if t["name"] == "standby")

    # After removing workload tasks, leaves are: pause-after-model-deploy,
    # deploy-httproute, deploy-inference-objectives
    run_after = set(standby.get("runAfter", []))
    assert "pause-after-model-deploy" in run_after
    assert "deploy-httproute" in run_after
    assert "deploy-inference-objectives" in run_after


def test_standby_pipeline_no_dangling_runafter():
    """No deploy task has a dangling runAfter ref to a removed workload task."""
    from pipeline.lib.tekton import make_standby_pipeline, _WORKLOAD_TASK_NAMES

    pipeline, _ = make_standby_pipeline("baseline", _COMPILED_PIPELINE, "run-1", "test-ns")
    for task in pipeline["spec"]["tasks"]:
        for ref in task.get("runAfter", []):
            assert ref not in _WORKLOAD_TASK_NAMES, (
                f"Task '{task['name']}' still has dangling runAfter ref '{ref}'"
            )


def test_standby_pipeline_pipelinerun_has_no_workload_params():
    """PipelineRun emitted by make_standby_pipeline carries no workload params."""
    from pipeline.lib.tekton import make_standby_pipeline

    _, pr = make_standby_pipeline("baseline", _COMPILED_PIPELINE, "run-1", "test-ns")
    pr_param_names = {p["name"] for p in pr["spec"]["params"]}
    assert "workloadName" not in pr_param_names
    assert "workloadSpec" not in pr_param_names


def test_standby_pipeline_compiled_pipeline_unchanged():
    """make_standby_pipeline does not mutate the input compiled_pipeline dict."""
    import copy
    from pipeline.lib.tekton import make_standby_pipeline

    original = copy.deepcopy(_COMPILED_PIPELINE)
    make_standby_pipeline("baseline", _COMPILED_PIPELINE, "run-1", "test-ns")
    assert _COMPILED_PIPELINE == original
