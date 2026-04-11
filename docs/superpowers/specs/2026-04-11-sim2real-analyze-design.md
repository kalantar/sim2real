# sim2real-analyze Skill Design

**Date:** 2026-04-11
**Status:** Approved

## Problem

After `pipeline/deploy.py collect` completes, the user has raw request-level trace CSVs in `workspace/runs/<name>/deploy_{baseline,treatment}_log/{workload}/trace_data.csv`. There is no first-class way to:

- See a summary comparison table (baseline vs treatment, per workload) without running a deprecated CLI tool
- Ask open-ended analysis questions about the data (latency distributions, throughput over time, tail latency comparisons, cross-run diffs, etc.)

## Solution

A `/sim2real-analyze` Claude Code skill that:

1. Runs a deterministic helper script to compute and print a per-workload comparison table from the raw CSVs
2. Enters an interactive loop where the user can ask any data analysis question and the skill — acting as a data visualization expert — writes and executes Python code to satisfy the request

No new files go in `pipeline/`. Everything lives in `.claude/skills/sim2real-analyze/`.

## File Structure

```
.claude/skills/sim2real-analyze/
  SKILL.md                    ← skill instructions + interactive analysis loop
  scripts/
    compute_table.py          ← stdlib-only: CSV → per-workload comparison table
```

## `compute_table.py`

**Invocation:**
```bash
python .claude/skills/sim2real-analyze/scripts/compute_table.py --run <name>
# --run defaults to current_run from workspace/setup_config.json
```

**Inputs:**
- `workspace/runs/<name>/deploy_baseline_log/{workload}/trace_data.csv`
- `workspace/runs/<name>/deploy_treatment_log/{workload}/trace_data.csv`
- Workloads: all subdirectory names present in both log directories

**Metric computation** (all timestamps in microseconds, output in milliseconds):
- **TTFT** = `(first_chunk_time_us - send_time_us) / 1000`
- **TPOT** = `(last_chunk_time_us - first_chunk_time_us) / max(output_tokens - 1, 1) / 1000`
- **E2E** = `(last_chunk_time_us - send_time_us) / 1000`

Aggregates: mean, p50 (median), p99 per workload, per phase (baseline and treatment).

**Output format** — printed to stdout and saved to `workspace/runs/<name>/deploy_comparison_table.txt`:

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

One section per workload. Blank line between workloads. "better" = delta negative for latency metrics (lower is better); "worse" = delta positive.

**Error handling:**
- Missing `deploy_baseline_log/` or `deploy_treatment_log/`: exit 1 with message `Error: missing log directory — run 'pipeline/deploy.py collect' first`
- Workload present in baseline but not treatment (or vice versa): skip that workload, print warning
- CSV missing required columns: exit 1 naming the file and missing columns

**Dependencies:** Python 3.10+ stdlib only (`csv`, `statistics`, `pathlib`, `argparse`).

## `SKILL.md` — Interactive Analysis Loop

### Skill metadata

```yaml
name: sim2real-analyze
description: |
  Analyze sim2real pipeline run results. Shows per-workload latency comparison
  tables (TTFT/TPOT/E2E baseline vs treatment) and handles any user analysis
  request: charts, distributions, HTML reports, cross-run comparisons.
argument-hint: "[--run NAME]"
user-invocable: true
```

### Skill flow

**Step 1 — Resolve run**
Read `current_run` from `workspace/setup_config.json`. If absent or empty, list available runs (from `workspace/runs/*/`) and ask the user to pick one. Accept `--run <name>` argument to override.

**Step 2 — Ask**
Prompt: `"Found run '<name>'. Show the comparison table? (or describe what you'd like to analyze)"`

The user can say yes/proceed to see the table, or skip directly to a specific request.

**Step 3 — Compute and print table**
```bash
python .claude/skills/sim2real-analyze/scripts/compute_table.py --run <name>
```
Print the output. If the script exits 1, surface the error and stop.

**Step 4 — Interactive loop**
After the table (or if the user skips to a direct request), ask:
`"What would you like to analyze next? (or 'done' to exit)"`

Handle any request by writing a Python script and executing it via Bash. Examples of requests the skill must handle:

| User request | Skill action |
|---|---|
| TTFT distribution plot | Write matplotlib script, save PNG to `results_charts/`, report path |
| Throughput over time | Compute request rate from `arrival_time_us`, save PNG |
| Tail latency heatmap (workloads × metrics) | seaborn heatmap PNG |
| Compare with another run | Load CSVs from both runs, overlay chart |
| HTML summary report | Write self-contained HTML with embedded charts |
| Custom metric (e.g. prefill vs decode breakdown) | Derive from CSV columns, print or chart |

The skill proactively suggests follow-up analyses when it notices interesting patterns (e.g., if p99 is worse while mean is better, offer to show the distribution).

**Step 5 — Output**
- PNGs: `workspace/runs/<name>/results_charts/<descriptive-name>.png`
- HTML: `workspace/runs/<name>/results_charts/<descriptive-name>.html`, then `open` it
- Inline tables: printed directly to terminal

Loop continues until the user says "done" or dismisses.

### Data context the skill always has

```
workspace/runs/<name>/
  deploy_baseline_log/
    {workload}/
      trace_data.csv        # columns: request_id, arrival_time_us, send_time_us,
                            #   first_chunk_time_us, last_chunk_time_us,
                            #   input_tokens, output_tokens, status, ...
      trace_header.yaml     # model, time_unit, workload_spec, server config
  deploy_treatment_log/
    {workload}/
      trace_data.csv
      trace_header.yaml
  deploy_comparison_table.txt   # written by compute_table.py
```

Time unit: microseconds (divide by 1000 for milliseconds). Filter to `status == "ok"` rows only for metric computation.

### Libraries available in generated scripts

The skill may use any Python library available in the project virtualenv: `pandas`, `matplotlib`, `seaborn`, `numpy`. Fall back to stdlib if a library is unavailable and note the limitation.

## Changes to `pipeline/deploy.py`

The "Next:" hint printed after a successful collect is updated from:
```
Next:      python pipeline/analyze.py --run <name>
```
to:
```
Next:      /sim2real-analyze
```

## Non-Goals

- Replacing `transfer_cli.py` subcommands other than `compare` (out of scope)
- Automated report generation without user interaction (can be added later)
- Statistical significance testing (can be added as a user-requested analysis)
- Saving analysis session state across invocations
