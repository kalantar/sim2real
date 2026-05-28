"""Tests for deploy.py pairs subcommand."""
import yaml as _yaml


def _make_cluster(tmp_path, pairs):
    """Write pipelinerun-*.yaml files into tmp_path for each (workload, package) tuple."""
    for wl, pkg in pairs:
        pr = {
            "apiVersion": "tekton.dev/v1",
            "kind": "PipelineRun",
            "metadata": {"name": f"{pkg}-{wl}-run1", "namespace": "sim2real-0"},
            "spec": {"params": [
                {"name": "workloadName", "value": f"wl-{wl}"},
                {"name": "phase", "value": pkg},
            ]},
        }
        (tmp_path / f"pipelinerun-{wl}-{pkg}.yaml").write_text(_yaml.dump(pr))


def test_pairs_table_lists_all(tmp_path, capsys):
    """Default mode prints a table with all pair keys, workloads, and packages."""
    from pipeline.deploy import _cmd_pairs
    _make_cluster(tmp_path, [("smoke", "baseline"), ("smoke", "treatment"), ("load", "baseline")])

    _cmd_pairs(tmp_path)
    out = capsys.readouterr().out

    assert "wl-smoke-baseline" in out
    assert "wl-smoke-treatment" in out
    assert "wl-load-baseline" in out
    assert "PAIR" in out
    assert "WORKLOAD" in out
    assert "PACKAGE" in out
    assert "3 pairs" in out


def test_pairs_keys_only(tmp_path, capsys):
    """--keys-only prints one pair key per line, no header."""
    from pipeline.deploy import _cmd_pairs
    _make_cluster(tmp_path, [("smoke", "baseline"), ("load", "treatment")])

    _cmd_pairs(tmp_path, keys_only=True)
    out = capsys.readouterr().out

    lines = out.strip().splitlines()
    assert lines == ["wl-load-treatment", "wl-smoke-baseline"]
    assert "PAIR" not in out


def test_pairs_workloads_only(tmp_path, capsys):
    """--workloads-only prints distinct workload names, one per line."""
    from pipeline.deploy import _cmd_pairs
    _make_cluster(tmp_path, [("smoke", "baseline"), ("smoke", "treatment"), ("load", "baseline")])

    _cmd_pairs(tmp_path, workloads_only=True)
    out = capsys.readouterr().out

    lines = out.strip().splitlines()
    assert set(lines) == {"wl-smoke", "wl-load"}


def test_pairs_packages_only(tmp_path, capsys):
    """--packages-only prints distinct package names, one per line."""
    from pipeline.deploy import _cmd_pairs
    _make_cluster(tmp_path, [("smoke", "baseline"), ("smoke", "treatment"), ("load", "baseline")])

    _cmd_pairs(tmp_path, packages_only=True)
    out = capsys.readouterr().out

    lines = out.strip().splitlines()
    assert set(lines) == {"baseline", "treatment"}


def test_pairs_empty_cluster(tmp_path, capsys):
    """Empty cluster directory prints a zero-pairs message."""
    from pipeline.deploy import _cmd_pairs

    _cmd_pairs(tmp_path)
    out = capsys.readouterr().out

    assert "0 pairs" in out


def test_pairs_missing_cluster(tmp_path, capsys):
    """Non-existent cluster directory prints a zero-pairs message."""
    from pipeline.deploy import _cmd_pairs

    _cmd_pairs(tmp_path / "nonexistent")
    out = capsys.readouterr().out

    assert "0 pairs" in out


def test_pairs_single_pair(tmp_path, capsys):
    """Single pair works (boundary for column-width calculation)."""
    from pipeline.deploy import _cmd_pairs
    _make_cluster(tmp_path, [("a", "b")])

    _cmd_pairs(tmp_path)
    out = capsys.readouterr().out

    assert "wl-a-b" in out
    assert "1 pairs" in out


def test_pairs_all_files_corrupt(tmp_path, capsys):
    """When files exist but all fail to parse, message distinguishes from empty dir."""
    from pipeline.deploy import _cmd_pairs
    (tmp_path / "pipelinerun-bad1.yaml").write_text("{{invalid")
    (tmp_path / "pipelinerun-bad2.yaml").write_text("[[broken")

    _cmd_pairs(tmp_path)
    out = capsys.readouterr().out

    assert "0 pairs" in out
    assert "failed to parse" in out


def test_pairs_cli_mutually_exclusive_flags():
    """--keys-only, --workloads-only, --packages-only are mutually exclusive."""
    import pytest
    from pipeline.deploy import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["pairs", "--keys-only", "--workloads-only"])
    assert exc_info.value.code != 0
