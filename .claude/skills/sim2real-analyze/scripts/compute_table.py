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
SEP = "  " + "\u2500" * 64


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

    b_ttft_mean, b_ttft_p50, b_ttft_p99 = _compute_metric(b_ttft)
    t_ttft_mean, t_ttft_p50, t_ttft_p99 = _compute_metric(t_ttft)
    lines.append(_format_row("TTFT mean", b_ttft_mean, t_ttft_mean))
    lines.append(_format_row("TTFT p50",  b_ttft_p50,  t_ttft_p50))
    lines.append(_format_row("TTFT p99",  b_ttft_p99,  t_ttft_p99))

    if not no_tpot:
        b_tpot_mean, b_tpot_p50, b_tpot_p99 = _compute_metric(b_tpot)
        t_tpot_mean, t_tpot_p50, t_tpot_p99 = _compute_metric(t_tpot)
        lines.append(_format_row("TPOT mean", b_tpot_mean, t_tpot_mean))
        lines.append(_format_row("TPOT p50",  b_tpot_p50,  t_tpot_p50))
        lines.append(_format_row("TPOT p99",  b_tpot_p99,  t_tpot_p99))

    b_e2e_mean, b_e2e_p50, b_e2e_p99 = _compute_metric(b_e2e)
    t_e2e_mean, t_e2e_p50, t_e2e_p99 = _compute_metric(t_e2e)
    lines.append(_format_row("E2E mean", b_e2e_mean, t_e2e_mean))
    lines.append(_format_row("E2E p50",  b_e2e_p50,  t_e2e_p50))
    lines.append(_format_row("E2E p99",  b_e2e_p99,  t_e2e_p99))

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

    output = "\n\n".join("\n".join(section) for section in sections)
    print(output)

    table_file = run_dir / "deploy_comparison_table.txt"
    table_file.write_text(output + "\n")


if __name__ == "__main__":
    main()
