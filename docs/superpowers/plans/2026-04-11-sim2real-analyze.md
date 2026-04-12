# sim2real-analyze Skill Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Note:** Per user instruction, skip all `git commit` steps in this plan.

**Goal:** Implement the `/sim2real-analyze` Claude Code skill — a deterministic comparison table script plus an interactive data analysis loop for sim2real pipeline run results.

**Architecture:** Three deliverables: `compute_table.py` (stdlib-only script that reads raw trace CSVs and prints a per-workload TTFT/TPOT/E2E comparison table), `SKILL.md` (Claude skill instructions for the interactive analysis loop), and a one-line change to `pipeline/deploy.py` to update the "Next:" hint after collect completes.

**Tech Stack:** Python 3.10+ stdlib (`csv`, `statistics`, `pathlib`, `argparse`, `json`), pytest for tests, pandas/matplotlib/seaborn available at skill runtime for user-requested charts.

---

## Chunk 1: `compute_table.py` + tests

### Task 1: `compute_table.py` with full test coverage

**Files:**
- Create: `.claude/skills/sim2real-analyze/scripts/compute_table.py`
- Create: `.claude/skills/sim2real-analyze/tests/test_compute_table.py`

**Context:**
- Spec: `docs/superpowers/specs/2026-04-11-sim2real-analyze-design.md`
- Trace CSVs live at `workspace/runs/<name>/deploy_{baseline,treatment}_log/workload_<name>/trace_data.csv`
- Required CSV columns: `send_time_us`, `first_chunk_time_us`, `last_chunk_time_us`, `output_tokens`, `status`
- Only `status == "ok"` rows used for metric computation
- Workload dirs have `workload_` prefix and underscores; display name strips prefix and converts `_` → `-`
- Row format string: `f"  {metric:<14}{baseline:>9.1f}{treatment:>9.1f}{delta:>+9.1f}      {change}"`
- Separator: 2-space indent + `─` × 64 (Unicode U+2500)

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p .claude/skills/sim2real-analyze/scripts
mkdir -p .claude/skills/sim2real-analyze/tests
```

- [ ] **Step 2: Write the test file**

Create `.claude/skills/sim2real-analyze/tests/test_compute_table.py`:

```python
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
    # delta should be negative
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


def monkeypatch_workspace(module, ws_path: Path):
    module.WORKSPACE_DIR = ws_path


# ── Integration: load_csv ────────────────────────────────────────────────────

def test_load_csv_returns_rows(tmp_path):
    csv_path = tmp_path / "trace_data.csv"
    make_csv(csv_path, [make_row()])
    rows = compute_table._load_csv(csv_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"


def test_load_csv_missing_column_exits(tmp_path, capsys):
    csv_path = tmp_path / "trace_data.csv"
    # Write CSV missing output_tokens
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
    # Remove both log dirs
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
    # Only in baseline
    make_csv(baseline_log / "workload_a" / "trace_data.csv", [make_row()])
    # Only in treatment
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
    # Common workload
    make_csv(baseline_log / "workload_common" / "trace_data.csv", rows)
    make_csv(treatment_log / "workload_common" / "trace_data.csv", rows)
    # Baseline-only workload
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
    # Need at least one good workload so we don't hit "no workloads found"
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
    # Mix: 2 rows with tokens=1 (excluded from TPOT), 5 with tokens=10
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
    # TPOT should still appear (5 valid rows)
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
    # TTFT and E2E still appear
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
    # Also create setup_config pointing to a different (nonexistent) run
    (ws).mkdir(parents=True, exist_ok=True)
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
    # CSV missing output_tokens column
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
```

- [ ] **Step 3: Run tests to verify they all fail**

```bash
cd /Users/jchen/go/src/inference-sim/sim2real
python -m pytest .claude/skills/sim2real-analyze/tests/test_compute_table.py -v 2>&1 | head -40
```

Expected: All fail with `ModuleNotFoundError: No module named 'compute_table'` or similar import errors.

- [ ] **Step 4: Write `compute_table.py`**

Create `.claude/skills/sim2real-analyze/scripts/compute_table.py`:

```python
#!/usr/bin/env python3
"""Compute per-workload comparison table from sim2real trace CSVs.

Invocation:
    python .claude/skills/sim2real-analyze/scripts/compute_table.py --run <name>
    # --run defaults to current_run from workspace/setup_config.json if omitted
"""
import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

# Repo root: script is at {repo}/.claude/skills/sim2real-analyze/scripts/compute_table.py
REPO_ROOT = Path(__file__).resolve().parents[4]
WORKSPACE_DIR = REPO_ROOT / "workspace"

REQUIRED_COLS = {"send_time_us", "first_chunk_time_us", "last_chunk_time_us", "output_tokens", "status"}
SEP = "  " + "\u2500" * 64  # ────────────────────────────────────────────────────────────────


def err(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"Warning: {msg}", file=sys.stderr)


def _resolve_run(run_arg: str | None) -> str:
    """Return run name from argument or setup_config.json."""
    if run_arg:
        return run_arg
    cfg_path = WORKSPACE_DIR / "setup_config.json"
    try:
        cfg = json.loads(cfg_path.read_text())
        run = cfg.get("current_run", "")
        if run:
            return run
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    err("no run specified — use --run NAME or set current_run in workspace/setup_config.json")
    sys.exit(1)


def _display_name(dir_name: str) -> str:
    """workload_fm8_short_output_highrate → fm8-short-output-highrate"""
    name = dir_name
    if name.startswith("workload_"):
        name = name[len("workload_"):]
    return name.replace("_", "-")


def _load_csv(csv_path: Path) -> list[dict]:
    """Load CSV, validate required columns. Returns list of dicts. Exits 1 on error."""
    try:
        text = csv_path.read_text()
    except OSError:
        err(f"{csv_path}: failed to parse CSV")
        sys.exit(1)

    if not text.strip():
        err(f"{csv_path}: empty or invalid CSV file")
        sys.exit(1)

    try:
        reader = csv.DictReader(text.splitlines())
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    except Exception:
        err(f"{csv_path}: failed to parse CSV")
        sys.exit(1)

    if not fieldnames:
        err(f"{csv_path}: empty or invalid CSV file")
        sys.exit(1)

    missing = sorted(REQUIRED_COLS - set(fieldnames))
    if missing:
        err(f"{csv_path}: missing required columns: {', '.join(missing)}")
        sys.exit(1)

    return rows


def _compute_metric(values: list[float]) -> tuple[float, float, float]:
    """Return (mean, p50, p99). Uses single value directly if len==1."""
    mean = statistics.mean(values)
    if len(values) == 1:
        return mean, values[0], values[0]
    q = statistics.quantiles(values, n=100, method="exclusive")
    return mean, q[49], q[98]


def _format_change(baseline: float, treatment: float) -> str:
    """Format the Change column: pct with verdict, or N/A if baseline==0."""
    if baseline == 0.0:
        return "N/A"
    pct = (treatment - baseline) / baseline * 100
    rounded = round(pct, 1)
    if rounded == 0.0:
        verdict = "(no change)"
    elif pct < 0:
        verdict = "(better)"
    else:
        verdict = "(worse)"
    return f"{pct:+.1f}% {verdict}"


def _format_row(metric: str, baseline: float, treatment: float) -> str:
    delta = treatment - baseline
    change = _format_change(baseline, treatment)
    return f"  {metric:<14}{baseline:>9.1f}{treatment:>9.1f}{delta:>+9.1f}      {change}"


def _process_workload(
    wl_dir_name: str,
    baseline_log: Path,
    treatment_log: Path,
) -> list[str]:
    """Build output lines for one workload. Returns [] if workload should be skipped."""
    display = _display_name(wl_dir_name)

    baseline_rows = _load_csv(baseline_log / wl_dir_name / "trace_data.csv")
    treatment_rows = _load_csv(treatment_log / wl_dir_name / "trace_data.csv")

    b_ok = [r for r in baseline_rows if r["status"] == "ok"]
    t_ok = [r for r in treatment_rows if r["status"] == "ok"]

    if not b_ok or not t_ok:
        warn(f"skipping workload '{wl_dir_name}' — no rows with status == \"ok\"")
        return []

    def _ttft(rows):
        return [(int(r["first_chunk_time_us"]) - int(r["send_time_us"])) / 1000 for r in rows]

    def _e2e(rows):
        return [(int(r["last_chunk_time_us"]) - int(r["send_time_us"])) / 1000 for r in rows]

    def _tpot(rows):
        valid = [r for r in rows if int(r["output_tokens"]) > 1]
        return [
            (int(r["last_chunk_time_us"]) - int(r["first_chunk_time_us"]))
            / (int(r["output_tokens"]) - 1)
            / 1000
            for r in valid
        ]

    b_ttft = _ttft(b_ok)
    t_ttft = _ttft(t_ok)
    b_e2e = _e2e(b_ok)
    t_e2e = _e2e(t_ok)
    b_tpot = _tpot(b_ok)
    t_tpot = _tpot(t_ok)

    no_tpot = not b_tpot or not t_tpot
    if no_tpot:
        warn(f"skipping TPOT for workload '{wl_dir_name}' — no rows with output_tokens > 1")

    lines: list[str] = [
        f"=== Workload: {display} ===",
        "  Metric        Baseline  Treatment  Delta(ms)              Change",
        SEP,
    ]

    for label, bvals, tvals in [
        ("TTFT mean", [statistics.mean(b_ttft)], [statistics.mean(t_ttft)]),
        ("TTFT p50", [_compute_metric(b_ttft)[1]], [_compute_metric(t_ttft)[1]]),
        ("TTFT p99", [_compute_metric(b_ttft)[2]], [_compute_metric(t_ttft)[2]]),
    ]:
        lines.append(_format_row(label, bvals[0], tvals[0]))

    if not no_tpot:
        b_tpot_mean, b_tpot_p50, b_tpot_p99 = _compute_metric(b_tpot)
        t_tpot_mean, t_tpot_p50, t_tpot_p99 = _compute_metric(t_tpot)
        lines.append(_format_row("TPOT mean", b_tpot_mean, t_tpot_mean))
        lines.append(_format_row("TPOT p50", b_tpot_p50, t_tpot_p50))
        lines.append(_format_row("TPOT p99", b_tpot_p99, t_tpot_p99))

    b_e2e_mean, b_e2e_p50, b_e2e_p99 = _compute_metric(b_e2e)
    t_e2e_mean, t_e2e_p50, t_e2e_p99 = _compute_metric(t_e2e)
    lines.append(_format_row("E2E mean", b_e2e_mean, t_e2e_mean))
    lines.append(_format_row("E2E p50", b_e2e_p50, t_e2e_p50))
    lines.append(_format_row("E2E p99", b_e2e_p99, t_e2e_p99))

    return lines


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute per-workload comparison table from sim2real trace CSVs"
    )
    parser.add_argument("--run", metavar="NAME",
                        help="Run name (default: current_run from setup_config.json)")
    args = parser.parse_args()

    run_name = _resolve_run(args.run)
    run_dir = WORKSPACE_DIR / "runs" / run_name
    baseline_log = run_dir / "deploy_baseline_log"
    treatment_log = run_dir / "deploy_treatment_log"

    if not baseline_log.exists() or not treatment_log.exists():
        err("need both deploy_baseline_log/ and deploy_treatment_log/ — run 'pipeline/deploy.py collect' first")
        sys.exit(1)

    baseline_wl = {
        d.name for d in baseline_log.iterdir()
        if d.is_dir() and d.name.startswith("workload_")
    }
    treatment_wl = {
        d.name for d in treatment_log.iterdir()
        if d.is_dir() and d.name.startswith("workload_")
    }

    for wl in sorted(baseline_wl - treatment_wl):
        warn(f"skipping workload '{wl}' — not present in both phases")
    for wl in sorted(treatment_wl - baseline_wl):
        warn(f"skipping workload '{wl}' — not present in both phases")

    common = sorted(baseline_wl & treatment_wl)
    if not common:
        err("no workloads found in both baseline and treatment logs")
        sys.exit(1)

    sections: list[list[str]] = []
    for wl_dir_name in common:
        lines = _process_workload(wl_dir_name, baseline_log, treatment_log)
        if lines:
            sections.append(lines)

    if not sections:
        err("no workloads found in both baseline and treatment logs")
        sys.exit(1)

    output = "\n".join("\n".join(section) for section in sections)
    print(output)

    table_file = run_dir / "deploy_comparison_table.txt"
    table_file.write_text(output + "\n")


if __name__ == "__main__":
    main()
```

Note: The `_process_workload` function above calls `_compute_metric` three times for TTFT (once each for mean, p50, p99) — that's redundant. Refactor to call it once per phase:

```python
    b_ttft_mean, b_ttft_p50, b_ttft_p99 = _compute_metric(b_ttft)
    t_ttft_mean, t_ttft_p50, t_ttft_p99 = _compute_metric(t_ttft)
    lines.append(_format_row("TTFT mean", b_ttft_mean, t_ttft_mean))
    lines.append(_format_row("TTFT p50",  b_ttft_p50,  t_ttft_p50))
    lines.append(_format_row("TTFT p99",  b_ttft_p99,  t_ttft_p99))
```

Use this refactored version instead of the loop/list form above.

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /Users/jchen/go/src/inference-sim/sim2real
python -m pytest .claude/skills/sim2real-analyze/tests/test_compute_table.py -v
```

Expected: All tests PASS.

- [ ] **Step 6: Smoke-test against real data**

```bash
cd /Users/jchen/go/src/inference-sim/sim2real
python .claude/skills/sim2real-analyze/scripts/compute_table.py --run adaptive6
```

Expected: Comparison table printed to stdout with at least one workload section.

---

## Chunk 2: SKILL.md + deploy.py update

### Task 2: `SKILL.md`

**Files:**
- Create: `.claude/skills/sim2real-analyze/SKILL.md`

- [ ] **Step 1: Create SKILL.md**

Create `.claude/skills/sim2real-analyze/SKILL.md`:

````markdown
---
name: sim2real-analyze
description: |
  Analyze sim2real pipeline run results. Shows per-workload latency comparison tables
  (TTFT/TPOT/E2E baseline vs treatment) and handles any user analysis request: charts,
  distributions, HTML reports, cross-run comparisons.
argument-hint: "[--run NAME]"
user-invocable: true
allowed-tools:
  - Bash(python *)
  - Bash(python3 *)
  - Bash(ls *)
  - Bash(mkdir *)
  - Bash(rm *)
  - Bash(open *)
  - Bash(cat *)
  - Read
  - Write
  - Glob
---

# sim2real-analyze

Analyze sim2real pipeline run results interactively. You are a data visualization expert.

## Step 1 — Resolve run

Check the skill invocation arguments (everything after `/sim2real-analyze` in the user's invocation).
If the arguments contain `--run NAME`, extract NAME and use it as the run. Example: `/sim2real-analyze --run adaptive6` → run = `adaptive6`.

If not provided, read `workspace/setup_config.json`:
```bash
cat workspace/setup_config.json
```

Extract `current_run`. If missing or empty, check if `workspace/runs/` exists and list runs:
```bash
ls workspace/runs/
```

- If `workspace/runs/` does not exist: stop with `Error: workspace/runs/ not found — no runs available`
- If empty: stop with `Error: no runs found in workspace/runs/`
- Otherwise: show a numbered list and prompt `Enter run name:`

If `current_run` names a directory that doesn't exist under `workspace/runs/`, warn:
`Warning: run '<name>' not found` and fall back to the directory listing prompt.

## Step 2 — Ask

Once run name is resolved, prompt the user:

```
Found run '<name>'. Show the comparison table? (or describe what you'd like to analyze)
```

If the user says yes, proceeds, or says nothing meaningful → go to Step 3.
If the user describes a specific analysis → skip to Step 4 with that request.

## Step 3 — Compute and print comparison table

```bash
python .claude/skills/sim2real-analyze/scripts/compute_table.py --run <name>
```

Print the full output to the user. If the script exits with code 1, surface the error and stop.

After printing the table, proactively note any interesting patterns:
- If any p99 is worse while mean/p50 is better → suggest "Would you like to see the latency distribution to understand the tail?"
- If treatment is consistently better → note "Treatment shows consistent improvement."
- If treatment is consistently worse → note "Treatment shows consistent regression."

Then go to Step 4.

## Step 4 — Interactive analysis loop

Ask:
```
What would you like to analyze next? (or 'done' to exit)
```

For each user request:

1. Create `workspace/runs/<name>/results_charts/` if it doesn't exist:
   ```bash
   mkdir -p workspace/runs/<name>/results_charts/
   ```
   If this fails, tell the user and continue the loop.

2. Write a self-contained Python script to `/tmp/sim2real_analyze_<8-hex>.py`. Use `os.urandom(4).hex()` in the script name. The script must:
   - Import pandas, matplotlib, seaborn as needed
   - Load CSVs from `workspace/runs/<name>/deploy_baseline_log/{workload}/trace_data.csv` and `deploy_treatment_log/`
   - All timestamps are in **microseconds** — divide by 1000 for milliseconds
   - Filter to `status == "ok"` rows before computing any metrics
   - Save charts to `workspace/runs/<name>/results_charts/<descriptive-name>.png` or `.html`
   - Print any tabular results to stdout

3. Execute:
   ```bash
   python /tmp/sim2real_analyze_<hex>.py
   ```

4. Delete the temp file after execution:
   ```bash
   rm /tmp/sim2real_analyze_<hex>.py
   ```

5. Report results:
   - For PNG outputs: `Saved: workspace/runs/<name>/results_charts/<name>.png`
   - For HTML outputs: report path and open it: `open workspace/runs/<name>/results_charts/<name>.html`
   - For stdout tables: print them directly

**Library availability:** Before the first user analysis request, check if pandas and matplotlib are importable:
```bash
python -c "import pandas, matplotlib" 2>/dev/null
```
If exit code ≠ 0, print once:
```
Some analysis features require pandas and matplotlib. Install with:
  pip install pandas matplotlib seaborn
```
and fall back to stdlib-only analysis (tables and statistics, no charts). Do not attempt to write chart scripts for requests that require matplotlib.

**Session memory:** Remember what has been generated so far. "Show me that last chart again" → re-run or re-open the chart at its saved path.

**Proactive suggestions:** After each analysis, suggest a follow-up when patterns are notable:
- Distribution looks bimodal → "Want a CDF to see the full shape?"
- Tail latency is high → "Want a p95/p99/p999 breakdown?"
- One workload looks different → "Want to compare just that workload against another run?"

## Step 5 — Exit

Loop until the user says "done", "exit", "quit", "that's all", or similar.

## Data reference

```
workspace/runs/<name>/
  deploy_baseline_log/
    workload_<name>/
      trace_data.csv    # columns: send_time_us, first_chunk_time_us, last_chunk_time_us,
                        #          output_tokens, arrival_time_us, input_tokens, status, ...
      trace_header.yaml # model, time_unit (microseconds), workload_spec, server config
  deploy_treatment_log/
    workload_<name>/
      trace_data.csv
      trace_header.yaml
  deploy_comparison_table.txt  # written by compute_table.py
  results_charts/              # your analysis outputs go here
```

All timestamps are **microseconds**. Metrics:
- TTFT = (first_chunk_time_us - send_time_us) / 1000 ms
- TPOT = (last_chunk_time_us - first_chunk_time_us) / (output_tokens - 1) / 1000 ms (output_tokens > 1 only)
- E2E  = (last_chunk_time_us - send_time_us) / 1000 ms

## Example analysis scripts

### TTFT distribution histogram

```python
import os
import pandas as pd
import matplotlib.pyplot as plt

run = "<name>"
workloads = ["workload_fm8_short_output_highrate"]  # fill in actual workloads

fig, axes = plt.subplots(len(workloads), 1, figsize=(10, 4 * len(workloads)))
if len(workloads) == 1:
    axes = [axes]

for ax, wl in zip(axes, workloads):
    for phase in ["baseline", "treatment"]:
        df = pd.read_csv(f"workspace/runs/{run}/deploy_{phase}_log/{wl}/trace_data.csv")
        df = df[df["status"] == "ok"]  # only compute metrics for successful requests
        ttft = (df["first_chunk_time_us"] - df["send_time_us"]) / 1000
        ax.hist(ttft, bins=50, alpha=0.6, label=phase)
    wl_display = wl.replace("workload_", "").replace("_", "-")
    ax.set_title(f"TTFT distribution — {wl_display}")
    ax.set_xlabel("TTFT (ms)")
    ax.legend()

plt.tight_layout()
out = f"workspace/runs/{run}/results_charts/ttft_distribution.png"
plt.savefig(out)
print(f"Saved: {out}")
```

### Throughput over time

```python
import pandas as pd
import matplotlib.pyplot as plt

run = "<name>"
wl = "workload_fm8_short_output_highrate"

fig, ax = plt.subplots(figsize=(12, 4))
for phase in ["baseline", "treatment"]:
    df = pd.read_csv(f"workspace/runs/{run}/deploy_{phase}_log/{wl}/trace_data.csv")
    df = df[df["status"] == "ok"]  # only compute metrics for successful requests
    t0 = df["arrival_time_us"].min()
    df["t_sec"] = (df["arrival_time_us"] - t0) / 1e6
    counts = df.groupby(df["t_sec"].astype(int)).size()
    ax.plot(counts.index, counts.values, label=phase)

ax.set_xlabel("Time (s)")
ax.set_ylabel("Requests/s")
ax.set_title(f"Throughput over time — {wl.replace('workload_','').replace('_','-')}")
ax.legend()
plt.tight_layout()
out = f"workspace/runs/{run}/results_charts/throughput_over_time.png"
plt.savefig(out)
print(f"Saved: {out}")
```
````

- [ ] **Step 2: Verify SKILL.md frontmatter is valid YAML**

```bash
python3 -c "
import re
content = open('.claude/skills/sim2real-analyze/SKILL.md').read()
# Extract frontmatter between --- markers
m = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
print('Frontmatter found:', bool(m))
"
```

Expected: `Frontmatter found: True`

---

### Task 3: Update `pipeline/deploy.py` Next hint

**Files:**
- Modify: `pipeline/deploy.py:454`

- [ ] **Step 1: Apply the one-line change**

In `pipeline/deploy.py`, line 454, change:
```python
        print(f"\n  Next:      python pipeline/analyze.py --run {run_dir.name}")
```
to:
```python
        print(f"\n  Next:      /sim2real-analyze")
```

- [ ] **Step 2: Verify the change**

```bash
sed -n '454p' pipeline/deploy.py
```

Expected output: `        print(f"\n  Next:      /sim2real-analyze")`

---

## Final verification

- [ ] Run full test suite to confirm no regressions:

```bash
cd /Users/jchen/go/src/inference-sim/sim2real
python -m pytest .claude/skills/sim2real-analyze/tests/ -v
```

Expected: All tests PASS.

- [ ] Confirm the three deliverables exist:

```bash
ls -la .claude/skills/sim2real-analyze/SKILL.md \
       .claude/skills/sim2real-analyze/scripts/compute_table.py \
       .claude/skills/sim2real-analyze/tests/test_compute_table.py
```
