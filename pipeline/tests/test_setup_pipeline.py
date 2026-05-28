"""Tests for setup.py Pipeline YAML application in step_tekton."""
import json
from unittest.mock import patch


def _make_config(**overrides):
    """Build a minimal SetupConfig with defaults for testing."""
    from pipeline.setup import SetupConfig
    defaults = dict(
        namespace="test-ns",
        namespaces=["test-ns"],
        registry="quay.io/test",
        repo_name="llm-d-inference-scheduler",
        run_name="test-run",
        hf_token="hf_xxx",
        github_token="gh_xxx",
        registry_user="user",
        registry_token="token",
        storage_class="standard",
        is_openshift=False,
        no_cluster=False,
        pipeline_yaml=None,
    )
    defaults.update(overrides)
    return SetupConfig(**defaults)


class TestStepTektonPipelineApply:
    """step_tekton applies Pipeline YAML to namespace."""

    @patch("pipeline.setup.run")
    def test_applies_default_pipeline_yaml(self, mock_run, tmp_path):
        """step_tekton applies pipeline/pipeline.yaml (default) after steps/tasks."""
        from pipeline.setup import step_tekton, REPO_ROOT

        cfg = _make_config()
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "pod Running"
        mock_run.return_value.stderr = ""

        step_tekton(cfg)

        # Find the call that applies the pipeline YAML (with check=False, capture=True)
        default_pipeline_path = REPO_ROOT / "pipeline" / "pipeline.yaml"
        pipeline_calls = [c for c in mock_run.call_args_list
                          if str(default_pipeline_path) in str(c) and "apply" in str(c)]
        assert len(pipeline_calls) == 1

    @patch("pipeline.setup.run")
    def test_applies_custom_pipeline_yaml(self, mock_run, tmp_path):
        """step_tekton uses custom pipeline_yaml path when set on config."""
        from pipeline.setup import step_tekton

        custom_path = str(tmp_path / "custom-pipeline.yaml")
        (tmp_path / "custom-pipeline.yaml").write_text("apiVersion: tekton.dev/v1\n")
        cfg = _make_config(pipeline_yaml=custom_path)

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "pod Running"
        mock_run.return_value.stderr = ""

        step_tekton(cfg)

        pipeline_calls = [c for c in mock_run.call_args_list
                          if custom_path in str(c) and "apply" in str(c)]
        assert len(pipeline_calls) == 1

    @patch("pipeline.setup.run")
    def test_skipped_when_no_cluster(self, mock_run):
        """step_tekton is skipped entirely when no_cluster=True."""
        from pipeline.setup import step_tekton

        cfg = _make_config(no_cluster=True)
        step_tekton(cfg)

        # run() should not be called at all
        mock_run.assert_not_called()

    @patch("pipeline.setup.run")
    def test_warns_if_custom_pipeline_yaml_missing(self, mock_run, tmp_path, capsys):
        """step_tekton warns (not errors) if custom pipeline YAML doesn't exist."""
        from pipeline.setup import step_tekton

        nonexistent = str(tmp_path / "does-not-exist.yaml")
        cfg = _make_config(pipeline_yaml=nonexistent)

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "pod Running"

        # Should not raise — custom path warns
        step_tekton(cfg)

        captured = capsys.readouterr()
        assert "not found" in captured.out.lower() or "WARN" in captured.out

    @patch("pipeline.setup.run")
    @patch("pipeline.setup.REPO_ROOT")
    def test_fatal_if_default_pipeline_yaml_missing(self, mock_root, mock_run, tmp_path):
        """step_tekton exits fatally if default pipeline/pipeline.yaml is missing."""
        from pipeline.setup import step_tekton

        # Point REPO_ROOT to a dir without pipeline/pipeline.yaml
        mock_root.__truediv__ = lambda self, other: tmp_path / other
        cfg = _make_config(pipeline_yaml=None)

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "pod Running"

        import pytest
        with patch("pipeline.setup.REPO_ROOT", tmp_path):
            with pytest.raises(SystemExit):
                step_tekton(cfg)

    @patch("pipeline.setup.run")
    def test_kubectl_apply_failure_warns_and_continues(self, mock_run, tmp_path, capsys):
        """step_tekton warns (not exits) if kubectl apply for Pipeline fails."""
        from pipeline.setup import step_tekton, REPO_ROOT

        cfg = _make_config()

        def side_effect(cmd, *, check=True, capture=False, input=None):
            class R:
                returncode = 0
                stdout = "pod Running"
                stderr = ""
            r = R()
            if "pipeline.yaml" in str(cmd):
                r.returncode = 1
                r.stderr = "error: RBAC denied"
            return r

        mock_run.side_effect = side_effect

        pipeline_path = REPO_ROOT / "pipeline" / "pipeline.yaml"
        if pipeline_path.exists():
            step_tekton(cfg)
            captured = capsys.readouterr()
            assert "Failed to apply Pipeline" in captured.out


class TestSetupConfigJson:
    """setup_config.json includes pipeline_yaml key."""

    def test_config_output_includes_pipeline_yaml(self, tmp_path):
        """step_config_output writes pipeline_yaml to setup_config.json."""
        from pipeline.setup import step_config_output
        import pipeline.setup as setup_module

        # Point EXPERIMENT_ROOT to tmp_path
        original_experiment_root = setup_module.EXPERIMENT_ROOT
        setup_module.EXPERIMENT_ROOT = tmp_path

        try:
            run_dir = tmp_path / "workspace" / "runs" / "test-run"
            run_dir.mkdir(parents=True)

            cfg = _make_config(pipeline_yaml="/path/to/pipeline.yaml")
            step_config_output(cfg, run_dir, "podman")

            config_path = tmp_path / "workspace" / "setup_config.json"
            assert config_path.exists()
            data = json.loads(config_path.read_text())
            assert "pipeline_yaml" in data
            assert data["pipeline_yaml"] == "/path/to/pipeline.yaml"
        finally:
            setup_module.EXPERIMENT_ROOT = original_experiment_root

    def test_config_output_pipeline_yaml_none(self, tmp_path):
        """When pipeline_yaml is None, key still present with null value."""
        from pipeline.setup import step_config_output
        import pipeline.setup as setup_module

        original_experiment_root = setup_module.EXPERIMENT_ROOT
        setup_module.EXPERIMENT_ROOT = tmp_path

        try:
            run_dir = tmp_path / "workspace" / "runs" / "test-run"
            run_dir.mkdir(parents=True)

            cfg = _make_config(pipeline_yaml=None)
            step_config_output(cfg, run_dir, "podman")

            config_path = tmp_path / "workspace" / "setup_config.json"
            data = json.loads(config_path.read_text())
            assert "pipeline_yaml" in data
            assert data["pipeline_yaml"] is None
        finally:
            setup_module.EXPERIMENT_ROOT = original_experiment_root


    def test_config_output_includes_hf_secret_name(self, tmp_path):
        """setup_config.json includes hf_secret_name field."""
        from pipeline.setup import step_config_output
        import pipeline.setup as setup_module

        original_experiment_root = setup_module.EXPERIMENT_ROOT
        setup_module.EXPERIMENT_ROOT = tmp_path

        try:
            run_dir = tmp_path / "workspace" / "runs" / "test-run"
            run_dir.mkdir(parents=True)

            cfg = _make_config()
            step_config_output(cfg, run_dir, "podman")

            config_path = tmp_path / "workspace" / "setup_config.json"
            data = json.loads(config_path.read_text())
            assert data["hf_secret_name"] == "hf-secret"
        finally:
            setup_module.EXPERIMENT_ROOT = original_experiment_root


class TestBuildParser:
    """build_parser includes --pipeline-yaml flag."""

    def test_pipeline_yaml_flag_exists(self):
        from pipeline.setup import build_parser
        parser = build_parser()
        args = parser.parse_args(["--pipeline-yaml", "/path/to/pipeline.yaml"])
        assert args.pipeline_yaml == "/path/to/pipeline.yaml"

    def test_pipeline_yaml_defaults_to_none(self):
        from pipeline.setup import build_parser
        parser = build_parser()
        args = parser.parse_args([])
        assert args.pipeline_yaml is None

    def test_orchestrator_image_flag_exists(self):
        from pipeline.setup import build_parser
        parser = build_parser()
        args = parser.parse_args(["--orchestrator-image", "ghcr.io/inference-sim/sim2real/orchestrator:latest"])
        assert args.orchestrator_image == "ghcr.io/inference-sim/sim2real/orchestrator:latest"

    def test_orchestrator_image_defaults_to_none(self):
        from pipeline.setup import build_parser
        parser = build_parser()
        args = parser.parse_args([])
        assert args.orchestrator_image is None


class TestOrchestratorImageConfig:
    """setup_config.json includes orchestrator_image key."""

    def test_config_output_includes_orchestrator_image(self, tmp_path):
        """step_config_output writes orchestrator_image to setup_config.json."""
        from pipeline.setup import step_config_output
        import pipeline.setup as setup_module

        original = setup_module.EXPERIMENT_ROOT
        setup_module.EXPERIMENT_ROOT = tmp_path
        try:
            run_dir = tmp_path / "workspace" / "runs" / "test-run"
            run_dir.mkdir(parents=True)
            cfg = _make_config(orchestrator_image="ghcr.io/inference-sim/sim2real/orchestrator:abc123")
            step_config_output(cfg, run_dir, "podman")
            data = json.loads((tmp_path / "workspace" / "setup_config.json").read_text())
            assert data["orchestrator_image"] == "ghcr.io/inference-sim/sim2real/orchestrator:abc123"
        finally:
            setup_module.EXPERIMENT_ROOT = original

    def test_config_output_orchestrator_image_empty(self, tmp_path):
        """orchestrator_image written as empty string when not set."""
        from pipeline.setup import step_config_output
        import pipeline.setup as setup_module

        original = setup_module.EXPERIMENT_ROOT
        setup_module.EXPERIMENT_ROOT = tmp_path
        try:
            run_dir = tmp_path / "workspace" / "runs" / "test-run"
            run_dir.mkdir(parents=True)
            cfg = _make_config()
            step_config_output(cfg, run_dir, "podman")
            data = json.loads((tmp_path / "workspace" / "setup_config.json").read_text())
            assert "orchestrator_image" in data
            assert data["orchestrator_image"] == ""
        finally:
            setup_module.EXPERIMENT_ROOT = original


class TestRedeployTasksPipeline:
    """--redeploy-tasks also applies Pipeline YAML."""

    @patch("pipeline.setup.run")
    def test_redeploy_applies_pipeline(self, mock_run, tmp_path):
        """--redeploy-tasks applies Pipeline alongside steps/tasks."""
        from pipeline.setup import main, REPO_ROOT

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        pipeline_path = REPO_ROOT / "pipeline" / "pipeline.yaml"
        if not pipeline_path.exists():
            return  # skip if repo structure doesn't have the file

        with patch("sys.argv", ["setup.py", "--redeploy-tasks", "--namespace", "test-ns"]):
            result = main()

        assert result == 0
        pipeline_calls = [c for c in mock_run.call_args_list
                          if "pipeline.yaml" in str(c)]
        assert len(pipeline_calls) >= 1

    @patch("pipeline.setup.run")
    def test_redeploy_custom_pipeline_missing_warns(self, mock_run, tmp_path, capsys):
        """--redeploy-tasks with missing custom --pipeline-yaml warns."""
        from pipeline.setup import main

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        nonexistent = str(tmp_path / "nope.yaml")
        with patch("sys.argv", ["setup.py", "--redeploy-tasks", "--namespace", "ns",
                                "--pipeline-yaml", nonexistent]):
            result = main()

        assert result == 0
        captured = capsys.readouterr()
        assert "not found" in captured.out.lower() or "WARN" in captured.out

    @patch("pipeline.setup.run")
    def test_redeploy_default_pipeline_missing_errors(self, mock_run, tmp_path):
        """--redeploy-tasks fails if default pipeline.yaml is missing."""
        from pipeline.setup import main

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = ""

        with patch("pipeline.setup.REPO_ROOT", tmp_path), \
             patch("pipeline.setup.TEKTONC_DIR", tmp_path), \
             patch("sys.argv", ["setup.py", "--redeploy-tasks", "--namespace", "ns"]):
            result = main()

        assert result == 1
