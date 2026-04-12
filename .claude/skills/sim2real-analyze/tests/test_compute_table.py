"""Tests for compute_table.py — sim2real-analyze comparison table script."""
import csv
import json
import sys
from pathlib import Path

import pytest

# Add scripts dir to path so we can import compute_table
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import compute_table


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_csv(path: Path, rows: list[dict]) -> None:
    """Write a trace CSV with required columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def make_row(
    send=1000, first=2000, last=5000, tokens=10, status="ok"
) -> dict:
    return {
        "send_time_us": str(send),
        "first_chunk_time_us": str(first),
        "last_chunk_time_us": str(last),
        "output_tokens": str(tokens),
        "status": status,
    }


def setup_run(tmp_path: Path, run_name: str = "testrun") -> tuple[Path, Path, Path]:
    """Create workspace structure, return (workspace_dir, baseline_log, treatment_log)."""
    run_dir = tmp_path / "workspace" / "runs" / run_name
    baseline_log = run_dir / "deploy_baseline_log"
    treatment_log = run_dir / "deploy_treatment_log"
    baseline_log.mkdir(parents=True)
    treatment_log.mkdir(parents=True)
    return tmp_path / "workspace", baseline_log, treatment_log


def monkeypatch_workspace(module, ws_path: Path):
    module.WORKSPACE_DIR = ws_path


# ── Unit: _display_name ───────────────────────────────────────────────────────

def test_display_name_strips_prefix_converts_underscores():
    assert compute_table._display_name("workload_fm8_short_output_highrate") == "fm8-short-output-highrate"


def test_display_name_no_prefix_passthrough():
    assert compute_table._display_name("somedir") == "somedir"


def test_display_name_workload_only():
    assert compute_table._display_name("workload_simple") == "simple"


# ── Unit: _compute_metric ─────────────────────────────────────────────────────

def test_compute_metric_single_value():
    mean, p50, p99 = compute_table._compute_metric([42.0])
    assert mean == 42.0
    assert p50 == 42.0
    assert p99 == 42.0


def test_compute_metric_two_values():
    mean, p50, p99 = compute_table._compute_metric([10.0, 20.0])
    assert mean == 15.0


def test_compute_metric_many_values():
    values = [float(i) for i in range(1, 101)]  # 1..100
    mean, p50, p99 = compute_table._compute_metric(values)
    assert mean == 50.5
    assert p50 == pytest.approx(50.0, abs=1.0)
    assert p99 == pytest.approx(99.0, abs=1.0)


# ── Unit: _format_change ─────────────────────────────────────────────────────

def test_format_change_better():
    result = compute_table._format_change(100.0, 90.0)
    assert "(better)" in result
    assert "-10.0%" in result


def test_format_change_worse():
    result = compute_table._format_change(100.0, 110.0)
    assert "(worse)" in result
    assert "+10.0%" in result


def test_format_change_no_change():
    result = compute_table._format_change(100.0, 100.0)
    assert "(no change)" in result


def test_format_change_baseline_zero():
    result = compute_table._format_change(0.0, 50.0)
    assert result == "N/A"


def test_format_change_rounds_to_zero():
    # 0.04% rounds to 0.0%
    result = compute_table._format_change(1000.0, 1000.4)
    assert "(no change)" in result


# ── Unit: _format_row ────────────────────────────────────────────────────────

def test_format_row_structure():
    row = compute_table._format_row("TTFT mean", 5929.5, 5879.6)
    assert row.startswith("  ")
    assert "5929.5" in row
    assert "5879.6" in row
    assert "-49.9" in row or "-50" in row


# ── Integration: resolve_run ─────────────────────────────────────────────────

def test_resolve_run_uses_arg(monkeypatch):
    monkeypatch.setattr(compute_table, "WORKSPACE_DIR", Path("/nonexistent"))
    assert compute_table._resolve_run("myrun") == "myrun"


def test_resolve_run_reads_setup_config(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    cfg = ws / "setup_config.json"
    cfg.write_text(json.dumps({"current_run": "adaptive6"}))
    monkeypatch_workspace(compute_table, ws)
    assert compute_table._resolve_run(None) == "adaptive6"


def test_resolve_run_missing_config_exits(tmp_path, capsys):
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch_workspace(compute_table, ws)
    with pytest.raises(SystemExit) as exc:
        compute_table._resolve_run(None)
    assert exc.value.code == 1
    assert "no run specified" in capsys.readouterr().err


# ── Integration: load_csv ────────────────────────────────────────────────────

def test_load_csv_returns_rows(tmp_path):
    csv_path = tmp_path / "trace_data.csv"
    make_csv(csv_path, [make_row()])
    rows = compute_table._load_csv(csv_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"


def test_load_csv_missing_column_exits(tmp_path, capsys):
    csv_path = tmp_path / "trace_data.csv"
    with csv_path.open("w") as f:
        f.write("send_time_us,first_chunk_time_us,last_chunk_time_us,status\n")
        f.write("1000,2000,5000,ok\n")
    with pytest.raises(SystemExit) as exc:
        compute_table._load_csv(csv_path)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "missing required columns" in err
    assert "output_tokens" in err


def test_load_csv_empty_exits(tmp_path, capsys):
    csv_path = tmp_path / "trace_data.csv"
    csv_path.write_text("")
    with pytest.raises(SystemExit) as exc:
        compute_table._load_csv(csv_path)
    assert exc.value.code == 1
    assert "empty or invalid" in capsys.readouterr().err


# ── Integration: main — error paths ─────────────────────────────────────────

def test_main_missing_both_log_dirs(tmp_path, capsys, monkeypatch):
    ws, _, _ = setup_run(tmp_path)
    import shutil
    shutil.rmtree(tmp_path / "workspace" / "runs" / "testrun" / "deploy_baseline_log")
    shutil.rmtree(tmp_path / "workspace" / "runs" / "testrun" / "deploy_treatment_log")
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    with pytest.raises(SystemExit) as exc:
        compute_table.main()
    assert exc.value.code == 1
    assert "deploy_baseline_log" in capsys.readouterr().err


def test_main_missing_one_log_dir(tmp_path, capsys, monkeypatch):
    ws, baseline_log, treatment_log = setup_run(tmp_path)
    import shutil
    shutil.rmtree(treatment_log)
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    with pytest.raises(SystemExit) as exc:
        compute_table.main()
    assert exc.value.code == 1
    assert "deploy_baseline_log" in capsys.readouterr().err


def test_main_no_common_workloads(tmp_path, capsys, monkeypatch):
    ws, baseline_log, treatment_log = setup_run(tmp_path)
    make_csv(baseline_log / "workload_a" / "trace_data.csv", [make_row()])
    make_csv(treatment_log / "workload_b" / "trace_data.csv", [make_row()])
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    with pytest.raises(SystemExit) as exc:
        compute_table.main()
    assert exc.value.code == 1
    assert "no workloads found" in capsys.readouterr().err


# ── Integration: main — happy paths ──────────────────────────────────────────

def test_main_happy_path_single_workload(tmp_path, capsys, monkeypatch):
    ws, baseline_log, treatment_log = setup_run(tmp_path)
    rows = [make_row(send=0, first=1000000, last=6000000, tokens=10) for _ in range(10)]
    make_csv(baseline_log / "workload_fm8_short_output_highrate" / "trace_data.csv", rows)
    make_csv(treatment_log / "workload_fm8_short_output_highrate" / "trace_data.csv", rows)
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    compute_table.main()
    out = capsys.readouterr().out
    assert "fm8-short-output-highrate" in out
    assert "TTFT mean" in out
    assert "TPOT mean" in out
    assert "E2E mean" in out


def test_main_happy_path_two_workloads(tmp_path, capsys, monkeypatch):
    ws, baseline_log, treatment_log = setup_run(tmp_path)
    rows = [make_row(send=0, first=1000000, last=6000000, tokens=10) for _ in range(5)]
    for wl in ["workload_short", "workload_long"]:
        make_csv(baseline_log / wl / "trace_data.csv", rows)
        make_csv(treatment_log / wl / "trace_data.csv", rows)
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    compute_table.main()
    out = capsys.readouterr().out
    assert "short" in out
    assert "long" in out


def test_main_workload_only_in_baseline_skipped(tmp_path, capsys, monkeypatch):
    ws, baseline_log, treatment_log = setup_run(tmp_path)
    rows = [make_row(send=0, first=1000000, last=6000000, tokens=10) for _ in range(5)]
    make_csv(baseline_log / "workload_common" / "trace_data.csv", rows)
    make_csv(treatment_log / "workload_common" / "trace_data.csv", rows)
    make_csv(baseline_log / "workload_solo" / "trace_data.csv", rows)
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    compute_table.main()
    captured = capsys.readouterr()
    assert "skipping workload 'workload_solo'" in captured.err
    assert "solo" not in captured.out


def test_main_no_ok_rows_skipped(tmp_path, capsys, monkeypatch):
    ws, baseline_log, treatment_log = setup_run(tmp_path)
    bad_rows = [make_row(status="error") for _ in range(5)]
    good_rows = [make_row(send=0, first=1000000, last=6000000, tokens=10) for _ in range(5)]
    make_csv(baseline_log / "workload_bad" / "trace_data.csv", bad_rows)
    make_csv(treatment_log / "workload_bad" / "trace_data.csv", bad_rows)
    make_csv(baseline_log / "workload_good" / "trace_data.csv", good_rows)
    make_csv(treatment_log / "workload_good" / "trace_data.csv", good_rows)
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    compute_table.main()
    captured = capsys.readouterr()
    assert 'no rows with status == "ok"' in captured.err
    assert "bad" not in captured.out


def test_main_tpot_partial_exclusion(tmp_path, capsys, monkeypatch):
    """Rows with output_tokens <= 1 excluded from TPOT; valid rows still aggregated."""
    ws, baseline_log, treatment_log = setup_run(tmp_path)
    rows = (
        [make_row(send=0, first=1000000, last=6000000, tokens=1) for _ in range(2)] +
        [make_row(send=0, first=1000000, last=6000000, tokens=10) for _ in range(5)]
    )
    make_csv(baseline_log / "workload_mixed" / "trace_data.csv", rows)
    make_csv(treatment_log / "workload_mixed" / "trace_data.csv", rows)
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    compute_table.main()
    out = capsys.readouterr().out
    assert "TPOT mean" in out
    assert "TPOT p50" in out
    assert "TPOT p99" in out


def test_main_tpot_all_excluded_skipped(tmp_path, capsys, monkeypatch):
    """When all rows have output_tokens <= 1, TPOT rows are omitted from output."""
    ws, baseline_log, treatment_log = setup_run(tmp_path)
    rows = [make_row(send=0, first=1000000, last=6000000, tokens=1) for _ in range(5)]
    make_csv(baseline_log / "workload_notpot" / "trace_data.csv", rows)
    make_csv(treatment_log / "workload_notpot" / "trace_data.csv", rows)
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    compute_table.main()
    captured = capsys.readouterr()
    assert "skipping TPOT" in captured.err
    assert "TPOT" not in captured.out
    assert "TTFT mean" in captured.out
    assert "E2E mean" in captured.out


def test_main_single_row_workload(tmp_path, capsys, monkeypatch):
    """Single ok row — percentile falls back to the single value."""
    ws, baseline_log, treatment_log = setup_run(tmp_path)
    rows = [make_row(send=0, first=1000000, last=6000000, tokens=10)]
    make_csv(baseline_log / "workload_single" / "trace_data.csv", rows)
    make_csv(treatment_log / "workload_single" / "trace_data.csv", rows)
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    compute_table.main()  # must not raise
    out = capsys.readouterr().out
    assert "TTFT mean" in out


def test_main_baseline_zero_shows_na(tmp_path, capsys, monkeypatch):
    """If baseline metric is 0.0, Change column shows N/A."""
    ws, baseline_log, treatment_log = setup_run(tmp_path)
    # send == first_chunk → TTFT = 0.0
    baseline_rows = [make_row(send=1000000, first=1000000, last=6000000, tokens=10) for _ in range(5)]
    treatment_rows = [make_row(send=1000000, first=2000000, last=6000000, tokens=10) for _ in range(5)]
    make_csv(baseline_log / "workload_zero" / "trace_data.csv", baseline_rows)
    make_csv(treatment_log / "workload_zero" / "trace_data.csv", treatment_rows)
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    compute_table.main()
    out = capsys.readouterr().out
    assert "N/A" in out


def test_main_overwrites_existing_table(tmp_path, capsys, monkeypatch):
    """deploy_comparison_table.txt is overwritten, not appended."""
    ws, baseline_log, treatment_log = setup_run(tmp_path)
    rows = [make_row(send=0, first=1000000, last=6000000, tokens=10) for _ in range(5)]
    make_csv(baseline_log / "workload_w" / "trace_data.csv", rows)
    make_csv(treatment_log / "workload_w" / "trace_data.csv", rows)
    table_file = tmp_path / "workspace" / "runs" / "testrun" / "deploy_comparison_table.txt"
    table_file.write_text("OLD CONTENT\n")
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    compute_table.main()
    content = table_file.read_text()
    assert "OLD CONTENT" not in content
    assert "TTFT mean" in content


def test_main_run_arg_overrides_setup_config(tmp_path, capsys, monkeypatch):
    """--run argument takes precedence over current_run in setup_config.json."""
    ws, baseline_log, treatment_log = setup_run(tmp_path, run_name="myrun")
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "setup_config.json").write_text(json.dumps({"current_run": "otherrun"}))
    rows = [make_row(send=0, first=1000000, last=6000000, tokens=10) for _ in range(5)]
    make_csv(baseline_log / "workload_w" / "trace_data.csv", rows)
    make_csv(treatment_log / "workload_w" / "trace_data.csv", rows)
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "myrun"])
    compute_table.main()
    out = capsys.readouterr().out
    assert "TTFT mean" in out


def test_main_csv_missing_column_exits(tmp_path, capsys, monkeypatch):
    ws, baseline_log, treatment_log = setup_run(tmp_path)
    bad_csv = baseline_log / "workload_w" / "trace_data.csv"
    bad_csv.parent.mkdir(parents=True, exist_ok=True)
    with bad_csv.open("w") as f:
        f.write("send_time_us,first_chunk_time_us,last_chunk_time_us,status\n")
        f.write("0,1000000,6000000,ok\n")
    make_csv(treatment_log / "workload_w" / "trace_data.csv",
             [make_row(send=0, first=1000000, last=6000000, tokens=10)])
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    with pytest.raises(SystemExit) as exc:
        compute_table.main()
    assert exc.value.code == 1
    assert "missing required columns" in capsys.readouterr().err


def test_main_two_workloads_blank_line_separator(tmp_path, capsys, monkeypatch):
    """Blank line between workload sections, no blank line before first."""
    ws, baseline_log, treatment_log = setup_run(tmp_path)
    rows = [make_row(send=0, first=1000000, last=6000000, tokens=10) for _ in range(3)]
    for wl in ["workload_alpha", "workload_beta"]:
        make_csv(baseline_log / wl / "trace_data.csv", rows)
        make_csv(treatment_log / wl / "trace_data.csv", rows)
    monkeypatch_workspace(compute_table, ws)
    monkeypatch.setattr(sys, "argv", ["compute_table.py", "--run", "testrun"])
    compute_table.main()
    out = capsys.readouterr().out
    # No blank line before first section
    assert not out.startswith("\n")
    # Blank line (double newline) between sections
    assert "\n\n" in out
    # Both sections present
    assert "alpha" in out
    assert "beta" in out
