# Noise Characterization Procedure

**Purpose:** Establish baseline measurement variance before transfer benchmarks.
Determines T_eff (effective improvement threshold) that accounts for cluster noise.

**When to run:** Before Stage 5 cluster benchmarks, in the same cluster environment
that will be used for the transfer benchmark.

## Procedure

1. Ensure the cluster is in steady state (no unusual traffic, stable resource usage).

2. Run exactly 5 baseline requests using a single representative workload configuration
   with the default scheduler (without evolved scorer). Record P50, P95, P99 latency per
   request (one entry per request, not per workload).

3. Save results to `workspace/baseline_runs.json`:
   ```json
   {"runs": [
       {"p50": 0.12, "p95": 0.25, "p99": 0.45},
       {"p50": 0.11, "p95": 0.24, "p99": 0.44},
       {"p50": 0.13, "p95": 0.26, "p99": 0.46},
       {"p50": 0.12, "p95": 0.25, "p99": 0.45},
       {"p50": 0.11, "p95": 0.23, "p99": 0.43}
   ]}
   ```

4. Run noise characterization:
   ```bash
   python tools/transfer_cli.py noise-characterize --runs workspace/baseline_runs.json
   ```

5. If `halt: true` (CV > 15%): investigate noise source and re-run during lower-variance window.
   Maximum 3 attempts (per R4 in macro plan). After 3 failures, halt the transfer.

## T_eff Formula

```
T_eff = max(5%, 2 × CV_max)
```

Where `CV_max` is the maximum coefficient of variation across all latency metrics.

**Rationale:** CV_max = 15% is the halt threshold for noise characterization.
At the halt boundary (CV_max = 0.15), T_eff = max(5%, 2 × 0.15) = 30%.
Above 15% CV, the noise floor exceeds plausible algorithm improvement for v1
transfers, and the resulting T_eff (≥ 30%) makes single-run benchmarks too
imprecise to detect meaningful improvement.

## Recording

Record T_eff from the noise characterization command's JSON output (the `t_eff` field).
Do NOT modify `workspace/baseline_runs.json` — it is the CLI input file.
T_eff and `noise_cv` (`max(per_metric_cv.values())`) will be recorded in `workspace/validation_results.json` (Step 6 of validate.md).
Pass T_eff to the `benchmark` command: `--t-eff <value>`.
