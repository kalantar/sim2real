"""Tests for pipeline.lib.tekton module."""
from pathlib import Path
from unittest.mock import MagicMock, patch



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
