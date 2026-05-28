"""Tests for shell completion scripts."""

import shutil
import subprocess
from pathlib import Path

import pytest

PIPELINE_DIR = Path(__file__).resolve().parent.parent
COMPLETIONS_ZSH = PIPELINE_DIR / "completions.zsh"
COMPLETIONS_BASH = PIPELINE_DIR / "completions.bash"


def test_zsh_script_exists():
    assert COMPLETIONS_ZSH.exists()


def test_bash_script_exists():
    assert COMPLETIONS_BASH.exists()


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not available")
def test_zsh_syntax_valid():
    result = subprocess.run(
        ["zsh", "-n", str(COMPLETIONS_ZSH)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"zsh syntax error: {result.stderr}"


def test_bash_syntax_valid():
    result = subprocess.run(
        ["bash", "-n", str(COMPLETIONS_BASH)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"bash syntax error: {result.stderr}"


def test_zsh_declares_subcommands():
    content = COMPLETIONS_ZSH.read_text()
    for subcmd in ("run", "status", "collect", "reset", "pairs"):
        assert subcmd in content


def test_bash_declares_subcommands():
    content = COMPLETIONS_BASH.read_text()
    for subcmd in ("run", "status", "collect", "reset", "pairs"):
        assert subcmd in content


def test_zsh_handles_status_values():
    content = COMPLETIONS_ZSH.read_text()
    for val in ("pending", "running", "done", "failed", "timed-out",
                "stalled"):
        assert val in content
    for removed in ("collecting", "collect-failed"):
        assert removed not in content


def test_bash_handles_status_values():
    content = COMPLETIONS_BASH.read_text()
    for val in ("pending", "running", "done", "failed", "timed-out",
                "stalled"):
        assert val in content
    for removed in ("collecting", "collect-failed"):
        assert removed not in content


def test_zsh_calls_pairs_subcommand():
    content = COMPLETIONS_ZSH.read_text()
    assert "pairs --keys-only" in content
    assert "pairs --workloads-only" in content
    assert "pairs --packages-only" in content


def test_bash_calls_pairs_subcommand():
    content = COMPLETIONS_BASH.read_text()
    assert "pairs --keys-only" in content
    assert "pairs --workloads-only" in content
    assert "pairs --packages-only" in content


def test_zsh_graceful_on_failure():
    content = COMPLETIONS_ZSH.read_text()
    assert "2>/dev/null" in content


def test_bash_graceful_on_failure():
    content = COMPLETIONS_BASH.read_text()
    assert "2>/dev/null" in content


def test_bash_skips_flag_values_in_subcommand_detection():
    content = COMPLETIONS_BASH.read_text()
    assert "--experiment-root)" in content
    assert "--run)" in content
    assert "_exroot" in content
    assert "_run_name" in content


def test_zsh_has_python_wrapper():
    content = COMPLETIONS_ZSH.read_text()
    assert "_python_deploy_py" in content
    assert "compdef _python_deploy_py python" in content


def test_bash_has_python_wrapper():
    content = COMPLETIONS_BASH.read_text()
    assert "_python_deploy_py" in content
    assert "complete -F _python_deploy_py python" in content


def test_banner_suppressed_for_machine_readable_pairs(tmp_path):
    """deploy.py pairs --keys-only must not print the banner to stdout."""
    import yaml as _yaml
    from pipeline.deploy import _cmd_pairs

    pr = {
        "metadata": {"name": "run1", "namespace": "ns"},
        "spec": {"params": [
            {"name": "workloadName", "value": "wl-smoke"},
            {"name": "phase", "value": "baseline"},
        ]},
    }
    cluster_dir = tmp_path / "cluster"
    cluster_dir.mkdir()
    (cluster_dir / "pipelinerun-smoke-baseline.yaml").write_text(_yaml.dump(pr))

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _cmd_pairs(cluster_dir, keys_only=True)
    output = buf.getvalue()
    assert "sim2real-deploy" not in output
    assert "wl-smoke-baseline" in output


def test_cmd_pairs_silent_when_empty_and_machine_readable(tmp_path):
    """deploy.py pairs --keys-only with no pairs produces no stdout."""
    from pipeline.deploy import _cmd_pairs

    cluster_dir = tmp_path / "cluster"
    cluster_dir.mkdir()

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _cmd_pairs(cluster_dir, keys_only=True)
    assert buf.getvalue() == ""


def test_zsh_forwards_experiment_root():
    content = COMPLETIONS_ZSH.read_text()
    assert '_saved_exroot' in content
    assert content.count('_saved_exroot') >= 3, \
        "all three helpers must reference _saved_exroot"


def test_zsh_forwards_run():
    content = COMPLETIONS_ZSH.read_text()
    assert '_saved_run' in content
    assert content.count('_saved_run') >= 3, \
        "all three helpers must reference _saved_run"


def test_zsh_uses_python_variable():
    content = COMPLETIONS_ZSH.read_text()
    assert '${PYTHON:-python}' in content
    assert content.count('"$(python pipeline/') == 0, \
        "hardcoded 'python' should be replaced with ${PYTHON:-python}"


def test_bash_forwards_experiment_root():
    content = COMPLETIONS_BASH.read_text()
    assert '_exroot' in content
    assert any('_deploy_cmd' in line and 'experiment-root' in line
               for line in content.splitlines()), \
        "_exroot must be forwarded via _deploy_cmd"


def test_bash_forwards_run():
    content = COMPLETIONS_BASH.read_text()
    assert '_run_name' in content
    assert any('_deploy_cmd' in line and '--run' in line
               for line in content.splitlines()), \
        "_run_name must be forwarded via _deploy_cmd"


def test_bash_uses_python_variable():
    content = COMPLETIONS_BASH.read_text()
    assert '${PYTHON:-python}' in content
    lines_with_pairs = [l for l in content.splitlines()
                        if 'pairs --keys-only' in l
                        or 'pairs --workloads-only' in l
                        or 'pairs --packages-only' in l]
    for line in lines_with_pairs:
        assert '_deploy_cmd' in line, \
            f"dynamic query should use _deploy_cmd: {line}"
