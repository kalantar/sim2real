# sim2real-analyze Skill Design

**Date:** 2026-04-11
**Status:** Approved

## Problem

After `pipeline/deploy.py collect` completes, the user has raw request-level trace CSVs in
`workspace/runs/<name>/deploy_{baseline,treatment}_log/{workload}/trace_data.csv`. There is no
first-class way to:

- See a summary comparison table (baseline vs treatment, per workload) without deprecated tooling
- Ask open-ended analysis questions about the data (latency distributions, throughput over time,
  tail latency comparisons, cross-run diffs, etc.)

## Solution

A `/sim2real-analyze` Claude Code skill that:

1. Runs a deterministic helper script to compute and print a per-workload comparison table from
   the raw CSVs
2. Enters an interactive loop where the user can ask any data analysis question and the skill —
   acting as a data visualization expert — writes and executes Python code to satisfy the request

No new files go in `pipeline/`. Everything lives in `.claude/skills/sim2real-analyze/`.

## File Structure

```
.claude/skills/sim2real-analyze/
  SKILL.md                    ← skill instructions + interactive analysis loop
  scripts/
    compute_table.py          ← stdlib-only: CSV → per-workload comparison table
```

## `compute_table.py`

### Invocation

```bash
python .claude/skills/sim2real-analyze/scripts/compute_table.py --run <name>
# --run defaults to current_run from workspace/setup_config.json if omitted
```

### Inputs

- `workspace/runs/<name>/deploy_baseline_log/{workload}/trace_data.csv`
- `workspace/runs/<name>/deploy_treatment_log/{workload}/trace_data.csv`
- Workloads compared: subdirectory names present in **both** log directories

**Workload directory naming:** On-disk directories are named with a `workload_` prefix and underscores
(e.g. `workload_fm8_short_output_highrate`). The display name shown in the table strips the
`workload_` prefix and converts underscores to hyphens (e.g. `fm8-short-output-highrate`).
The `--run` argument, warning messages, and error messages use the on-disk directory name as-is.
Subdirectories that do not start with `workload_` are silently ignored (no warning).

### Required CSV columns

`send_time_us`, `first_chunk_time_us`, `last_chunk_time_us`, `output_tokens`, `status`

Exit 1 if any required column is missing, naming the file and the missing columns.

Only rows where `status == "ok"` are included in metric computation.

### Metric computation (all timestamps in µs, output in ms)

- **TTFT** = `(first_chunk_time_us - send_time_us) / 1000`
- **TPOT** = `(last_chunk_time_us - first_chunk_time_us) / (output_tokens - 1) / 1000`
  — computed only for rows where `output_tokens > 1`; rows with ≤1 output token are excluded
  from TPOT aggregation. If **no** rows remain after filtering (all rows have `output_tokens ≤ 1`),
  skip all TPOT rows for that workload and print a warning to stderr:
  `Warning: skipping TPOT for workload '<name>' — no rows with output_tokens > 1`
  (where `<name>` is the on-disk directory name, e.g. `workload_fm8_short_output_highrate`).
  The workload table section is still printed, with only the 6 TTFT and E2E rows (no TPOT rows).
- **E2E** = `(last_chunk_time_us - send_time_us) / 1000`

Aggregates: mean, p50 (median), p99 per workload per phase.

**Aggregation implementation:**
- Mean: `statistics.mean(values)`
- p50/p99: `statistics.quantiles(values, n=100, method='exclusive')` →
  `result[49]` for p50, `result[98]` for p99.
  Requires at least 2 values; if only 1 value is present use it directly for all percentiles.

### Output format

Printed to stdout and **always overwritten** to
`workspace/runs/<name>/deploy_comparison_table.txt`.

```
=== Workload: fm8-short-output-highrate ===
  Metric        Baseline  Treatment  Delta(ms)              Change
  ────────────────────────────────────────────────────────────────
  TTFT mean       5929.5     5879.6      -49.9      -0.8% (better)
  TTFT p50        6057.9     5933.9     -124.0      -2.0% (better)
  TTFT p99        6583.4     6776.1     +192.7      +2.9% (worse)
  TPOT mean         24.2       22.8       -1.4      -5.6% (better)
  TPOT p50          25.9       24.1       -1.8      -7.1% (better)
  TPOT p99          33.7       33.2       -0.5      -1.6% (better)
  E2E mean        6141.4     6079.0      -62.4      -1.0% (better)
  E2E p50         6226.9     6122.9     -104.0      -1.7% (better)
  E2E p99         6823.0     6995.6     +172.6      +2.5% (worse)
```

**Column format rules:**
- `Metric`: left-aligned, 14 chars
- `Baseline`, `Treatment`: right-aligned, 9 chars, 1 decimal place
- `Delta(ms)`: right-aligned, 9 chars, 1 decimal place, `+` prefix for positive values
- `Change`: free-width, 6 spaces of padding after `Delta(ms)`, then `{pct:+.1f}% ({verdict})`
  — percentage = `(treatment - baseline) / baseline * 100`, rounded to 1 decimal place
  — for latency metrics, negative pct = better (lower latency); positive pct = worse
  — display `(no change)` when rounded pct is exactly `0.0`
  — if `baseline == 0.0`, display `N/A` for the percentage column

Separator line uses Unicode U+2500 (`─`) repeated 64 times with 2-space indent to match header width.
Row format string: `f"  {metric:<14}{baseline:>9.1f}{treatment:>9.1f}{delta:>+9.1f}      {change}"`

Blank line between workload sections. No blank line before the first section.

### Error handling

| Condition | Behavior |
|---|---|
| `deploy_baseline_log/` or `deploy_treatment_log/` missing entirely | Exit 1: `Error: need both deploy_baseline_log/ and deploy_treatment_log/ — run 'pipeline/deploy.py collect' first` |
| Workload dir present in baseline but not treatment (or vice versa) | Skip that workload, print to stderr: `Warning: skipping workload '<name>' — not present in both phases` |
| No workloads found in common between both phases | Exit 1: `Error: no workloads found in both baseline and treatment logs` |
| CSV missing required columns | Exit 1: `Error: <path>: missing required columns: <col1>, <col2>` (comma-space separated list) |
| CSV is malformed or unparseable | Exit 1: `Error: <path>: failed to parse CSV` |
| CSV is empty (no header) | Exit 1: `Error: <path>: empty or invalid CSV file` |
| CSV has no rows with `status == "ok"` | Skip that workload, print to stderr: `Warning: skipping workload '<name>' — no rows with status == "ok"` |
| `workspace/setup_config.json` missing and `--run` not provided | Exit 1: `Error: no run specified — use --run NAME or set current_run in workspace/setup_config.json` |

Errors go to stderr. Format: `Error: <message>` (no ANSI color codes — script is invoked by the
skill which handles its own terminal formatting).

### Dependencies

Python 3.10+ stdlib only: `csv`, `statistics`, `pathlib`, `argparse`, `json`

## `SKILL.md` — Interactive Analysis Loop

### Skill metadata

```yaml
name: sim2real-analyze
description: |
  Analyze sim2real pipeline run results. Shows per-workload latency comparison tables
  (TTFT/TPOT/E2E baseline vs treatment) and handles any user analysis request: charts,
  distributions, HTML reports, cross-run comparisons.
argument-hint: "[--run NAME]"
user-invocable: true
```

### Skill flow

**Step 1 — Resolve run**

Read `current_run` from `workspace/setup_config.json`. If absent or empty, list available
run directories under `workspace/runs/` and ask the user to pick one. Accept `--run <name>`
argument to override.

If `current_run` names a run whose directory does not exist under `workspace/runs/`, warn the
user (`Warning: run '<name>' not found`) and fall back to the directory listing prompt.

Fallback behavior when listing runs:
- If `workspace/runs/` does not exist: stop with `Error: workspace/runs/ not found — no runs available`
- If `workspace/runs/` is empty: stop with `Error: no runs found in workspace/runs/`
- Otherwise: display a numbered list of available run names and prompt `Enter run name:`

**Step 2 — Ask**

Prompt the user:
```
Found run '<name>'. Show the comparison table? (or describe what you'd like to analyze)
```

The user can say yes/proceed to see the table, or describe a specific analysis to jump straight
to Step 4.

**Step 3 — Compute and print table**

```bash
python .claude/skills/sim2real-analyze/scripts/compute_table.py --run <name>
```

Print the output. If the script exits 1, surface the error message to the user and stop.

**Step 4 — Interactive analysis loop**

After showing the table (or if the user jumped directly to a request), ask:
```
What would you like to analyze next? (or 'done' to exit)
```

For each user request, the skill writes a self-contained Python script to a temp file named
`/tmp/sim2real_analyze_{8-char hex}.py` (unique per request) and executes it via Bash. The file
is deleted after execution. The skill sees the script's stdout/stderr and reports results to the
user. The skill loop retains memory of what has been generated in the session so far
(e.g., "show me that last chart again" works).

**What the script can do:**
- Load CSVs with `pandas.read_csv()`
- Compute any derived metrics from the raw trace columns
- Generate charts with `matplotlib` / `seaborn`, saving to
  `workspace/runs/<name>/results_charts/<descriptive-name>.png`
- Generate HTML reports saving to
  `workspace/runs/<name>/results_charts/<descriptive-name>.html`
- Print custom tables to stdout

The skill creates `workspace/runs/<name>/results_charts/` if it does not already exist before
writing any chart or report file. If directory creation fails, surface the OS error to the user
and continue the interactive loop (skip the current request).

**After each script runs:**
- For PNG outputs: report the path to the user (`Saved: workspace/runs/<name>/results_charts/...`)
- For HTML outputs: report the path and `open` it in the browser
- For stdout tables: print them directly in the conversation

The skill proactively suggests follow-up analyses when patterns are notable (e.g., if p99 is
worse while mean is better, offer to show the latency distribution to understand the tail).

**Library availability:**
If `pandas` or `matplotlib` are not importable, print:
```
Some analysis features require pandas and matplotlib. Install with:
  pip install pandas matplotlib seaborn
```
and fall back to stdlib-only analysis (tables and basic statistics only, no charts).

**Step 5 — Exit**

Loop continues until the user says "done", "exit", or similar dismissal.

### Data the skill always has available

```
workspace/runs/<name>/
  deploy_baseline_log/
    {workload}/
      trace_data.csv        # send_time_us, first_chunk_time_us, last_chunk_time_us,
                            # output_tokens, arrival_time_us, input_tokens, status, ...
      trace_header.yaml     # model, time_unit (microseconds), workload_spec, server config
  deploy_treatment_log/
    {workload}/
      trace_data.csv
      trace_header.yaml
  deploy_comparison_table.txt   # written by compute_table.py (overwritten on each run)
```

All timestamps are in **microseconds**. Divide by 1000 for milliseconds. Filter to
`status == "ok"` rows for metric computation.

### Example user requests and skill responses

| User request | Skill action |
|---|---|
| "TTFT distribution for each workload" | Generate overlaid histogram (baseline vs treatment) per workload, save PNG |
| "Throughput over time" | Compute request arrival rate from `arrival_time_us` in 1s buckets, line chart PNG |
| "Compare with run admin5" | Load CSVs from both runs, overlay TTFT CDF chart |
| "Tail latency breakdown" | Bar chart of p95/p99/p999 for each metric per workload |
| "HTML summary report" | Write self-contained HTML with embedded base64 charts, `open` it |
| "Input token distribution" | Histogram of `input_tokens` column for both phases |
| "Which workload regressed most?" | Compute % change in E2E p99 per workload, print ranked table |

## Changes to `pipeline/deploy.py`

In `_cmd_collect()`, update the "Next:" print statement from:
```python
print(f"\n  Next:      python pipeline/analyze.py --run {run_dir.name}")
```
to:
```python
print(f"\n  Next:      /sim2real-analyze")
```

## Testing

Tests live in `.claude/skills/sim2real-analyze/tests/test_compute_table.py`.

Key test cases for `compute_table.py`:
- Happy path: two workloads, both phases, correct table output
- Single workload present in both phases
- Workload in baseline only — skipped with warning
- Both log directories missing — exit 1
- One log directory missing — exit 1
- CSV missing required column — exit 1
- CSV with no `status == "ok"` rows — workload skipped with warning
- TPOT with some rows where `output_tokens <= 1` — those rows excluded, valid rows aggregated correctly
- TPOT where all rows have `output_tokens <= 1` — TPOT rows skipped with warning
- Single-row workload (only 1 `status == "ok"` row) — percentile falls back to the single value
- Baseline metric is 0.0 — percentage displayed as `N/A`
- `--run` argument overrides `current_run` in setup_config.json
- Existing `deploy_comparison_table.txt` is overwritten

## Non-Goals

- Automated report generation without user interaction (can be added later)
- Statistical significance testing (can be added as a user-requested analysis)
- Saving analysis session state across skill invocations
