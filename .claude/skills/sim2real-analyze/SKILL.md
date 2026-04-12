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

1. Before the first user analysis request, check if pandas and matplotlib are importable:
   ```bash
   python -c "import pandas, matplotlib" 2>/dev/null
   ```
   If exit code ≠ 0, print once:
   ```
   Some analysis features require pandas and matplotlib. Install with:
     pip install pandas matplotlib seaborn
   ```
   and fall back to stdlib-only analysis (tables and statistics, no charts). Do not attempt to write chart scripts for requests that require matplotlib.

2. Create `workspace/runs/<name>/results_charts/` if it doesn't exist:
   ```bash
   mkdir -p workspace/runs/<name>/results_charts/
   ```
   If this fails, tell the user and continue the loop.

3. Write a self-contained Python script to a temp file. Generate the filename with:
   ```python
   import os; name = f"/tmp/sim2real_analyze_{os.urandom(4).hex()}.py"
   ```
   The script must:
   - Import pandas, matplotlib, seaborn as needed
   - Load CSVs from `workspace/runs/<name>/deploy_baseline_log/{workload}/trace_data.csv` and `deploy_treatment_log/`
   - All timestamps are in **microseconds** — divide by 1000 for milliseconds
   - Filter to `status == "ok"` rows before computing any metrics
   - Save charts to `workspace/runs/<name>/results_charts/<descriptive-name>.png` or `.html`
   - Print any tabular results to stdout

4. Execute the script:
   ```bash
   python /tmp/sim2real_analyze_<hex>.py
   ```

5. Delete the temp file after execution:
   ```bash
   rm /tmp/sim2real_analyze_<hex>.py
   ```

6. Report results:
   - For PNG outputs: `Saved: workspace/runs/<name>/results_charts/<name>.png`
   - For HTML outputs: report path and open it: `open workspace/runs/<name>/results_charts/<name>.html`
   - For stdout tables: print them directly

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
