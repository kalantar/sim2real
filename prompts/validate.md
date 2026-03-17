---
stage: 5
name: validate
description: Validate the generated scorer plugin against the simulation algorithm using 3-suite equivalence testing, noise characterization, and cluster benchmarks.
inputs:
  - workspace/algorithm_summary.json
  - workspace/signal_coverage.json
  - workspace/stage3_output.json
  - (Stage 4 success verified via go build/go vet — no workspace artifact)
outputs:
  - workspace/validation_results.json
---

# Stage 5: Validate

You are running Stage 5 of the sim-to-production transfer pipeline. This stage validates
equivalence between the generated scorer plugin and the original simulation algorithm.

## Prerequisites

Before proceeding, verify all predecessor artifacts exist and are valid:

```bash
python tools/transfer_cli.py validate-schema workspace/signal_coverage.json
python tools/transfer_cli.py validate-schema workspace/stage3_output.json
```

**HALT if either command exits non-zero.** Message: "HALT: Stage [2|3] prerequisite missing or invalid: workspace/<file>"

Verify Stage 4 completed successfully (scorer builds and tests pass):

```bash
(cd llm-d-inference-scheduler && go build ./pkg/plugins/scorer/... && go vet ./pkg/plugins/scorer/...)
```

**HALT if either command exits non-zero.** Message: "HALT: Stage 4 prerequisite failed — generated scorer does not build cleanly. Run Stage 4 first."

> **Note:** Stage 4 does not write a workspace artifact on success (success is implicit in the build/test passing). This prerequisite verifies the scorer builds by re-running `go build` and `go vet` scoped to the scorer package only — using `./...` would build all packages in the module and may fail for unrelated reasons (see Section H cross-system invariants).

Verify Stage 1 extract artifact exists (required by Suite A `t.Skip` guard):

```bash
test -f workspace/algorithm_summary.json || echo "HALT: workspace/algorithm_summary.json missing (run extract first)"
python tools/transfer_cli.py validate-schema workspace/algorithm_summary.json
```

**HALT if `workspace/algorithm_summary.json` is absent or invalid.** Without it, Suite A silently skips (exits 0/PASS) without running any equivalence checks — this would bypass the go/no-go gate.

## Step 1: Noise Characterization

**[OPERATOR ACTION REQUIRED]** This step requires live cluster access that Claude Code cannot perform.
Use the `llm-d-benchmark` harness (submodule at `llm-d-benchmark/`) to run 5 baseline requests against the production cluster (without the evolved scorer enabled).
Record P50/P95/P99 latency **per request** (not per workload). Run exactly 5 total requests using a single representative workload configuration. Save to `workspace/baseline_runs.json` in format:

```json
{"runs": [{"p50": 0.12, "p95": 0.25, "p99": 0.45}, ...]}
```

The `runs` array must contain exactly 5 entries (one per baseline request). The `noise-characterize` command computes CV and T_eff from these entries; an incorrect count will skew thresholds.

**Format note:** The `baseline_runs.json` format stores one entry per benchmark run (not per workload). Collect 5 runs total across all workloads in a single benchmark pass — e.g., one run covers all workload metrics. An operator with 3 workload files still produces 5 `runs[]` entries, not 15.

Then compute CV and T_eff:

```bash
python tools/transfer_cli.py noise-characterize --runs workspace/baseline_runs.json
```

**HALT if exit code 1 (CV > 15%).** Message: "HALT: Noise too high — re-run during lower-variance window."
Maximum 3 noise-characterization attempts (per R4 in macro plan). After 3 consecutive CV > 15% failures, halt the transfer entirely — do not proceed to Suite A or subsequent steps.
**HALT if exit code 2 (infrastructure error).** Message: "HALT: noise-characterize infrastructure error — check that workspace/baseline_runs.json exists and contains valid JSON."
Record `t_eff` from the `t_eff` field in the JSON output for use in Step 5 (the `--t-eff` argument to the `benchmark` command). Also record `noise_cv = max(per_metric_cv.values())` from the JSON output for use in Step 6 (`validation_results.json`).

## Step 2: Suite A — Rank Correlation Equivalence

Run Suite A once with `-json -v` to get both pass/fail verdict (from exit code) and structured numerical output (from JSON-wrapped `t.Logf` lines):

```bash
set -o pipefail
go test ./tools/harness/... -run TestSuiteA_KendallTau -v -timeout 60s -json 2>&1 | tee /tmp/suite_a_output.json
SUITE_A_EXIT=${PIPESTATUS[0]}
```

**HALT if `SUITE_A_EXIT` is non-zero** (or if `set -o pipefail` is unavailable, check for `"Action":"fail"` in `/tmp/suite_a_output.json`). Message: "HALT: Suite A FAIL — check test output for root cause (rank divergence if mean tau ≤ 0.8, or key-format mismatch if t.Fatalf reports missing keys)."

Extract numerical results from the same output:

```bash
grep -oE 'mean_kendall_tau=[0-9]+\.[0-9]+|max_abs_error=[0-9]+\.[0-9]+|tuple_count=[0-9]+' /tmp/suite_a_output.json
```

Record: mean_kendall_tau, max_abs_error, tuple_count from test output. Parse the `t.Logf` line: `Suite A: mean_kendall_tau=X.XXXX, max_abs_error=X.XXXXXX, tuple_count=NNN`.

## Step 3: Suite B — Staleness Rank Stability (Informational)

Run Suite B with `-json -v` and tee to capture structured output (same pattern as Steps 2 and 4):

```bash
go test ./tools/harness/... -run TestSuiteB_StalenessStability -v -timeout 30s -json 2>&1 | tee /tmp/suite_b_output.json
```

Suite B results are informational_only=true for v1 (all signals have staleness_window_ms=0).
Do NOT halt on Suite B results.

Extract numerical results from the captured output:

```bash
grep -oE 'rank_stability_tau=[0-9]+\.[0-9]+|threshold_crossing_pct=[0-9]+\.[0-9]+%' /tmp/suite_b_output.json
```

> **Note:** The `%` is part of the logged format (`"%.1f%%"` in the Go test), so `threshold_crossing_pct` appears as e.g. `threshold_crossing_pct=0.0%` in the output. The grep pattern above includes the trailing `%`.

Record: rank_stability_tau, threshold_crossing_pct from test output. Parse the `t.Logf` line: `Suite B: rank_stability_tau=X.XXXX, threshold_crossing_pct=X.XX%`.

## Step 4: Suite C — Concurrent Safety and Pile-On

Run Suite C once with `-json -v` and tee to capture both pass/fail verdict and structured output (same pattern as Step 2):

```bash
set -o pipefail
go test ./tools/harness/... -run TestSuiteC -v -race -timeout 60s -json 2>&1 | tee /tmp/suite_c_output.json
SUITE_C_EXIT=${PIPESTATUS[0]}
```

**HALT if `SUITE_C_EXIT` is non-zero** (or if `set -o pipefail` is unavailable, check for `"Action":"fail"` in `/tmp/suite_c_output.json`). Message: "HALT: Suite C FAIL."

Extract structured results from the captured output:

```bash
grep -oE 'max_pile_on_ratio=[0-9]+\.[0-9]+' /tmp/suite_c_output.json
```

Record: deterministic (true if TestSuiteC_ConcurrentDeterminism passes), max_pile_on_ratio from `t.Logf` line: `Suite C pile-on: max_pile_on_ratio=X.XX`.

## Step 5: Cluster Benchmarks

**[OPERATOR ACTION REQUIRED]** This step requires live cluster access that Claude Code cannot perform.

**Prerequisite check:** Verify workload YAML files exist before classification:

```bash
ls routing/workload_v2_*.yaml
```

**HALT if no files match the glob.** Message: "HALT: No routing/workload_v2_*.yaml files found — cannot classify workloads as matched/unmatched. Ensure routing artifacts are present." Without these files, the LLM has no data to perform workload classification and would fabricate assignments.

For each benchmark workload, classify as **matched** or **unmatched** using this rule:

> A workload is **matched** if the signals exercised by the workload (per `routing/workload_v2_*.yaml` parameter ranges) overlap with at least one signal listed in `workspace/signal_coverage.json` `signals[]` that has `mapped == true` (equivalently, `prod_name` is non-null). A workload is **unmatched** if none of its exercised signals are mapped.
>
> **Concrete check:** For each workload YAML, identify which sim parameters vary using the YAML-field-to-signal mapping below. If any of those signals appear in `signal_coverage.json` `signals[]` with `mapped: true` (i.e., `prod_name` is non-null), the workload is matched. Otherwise unmatched.
>
> **YAML field → signal_coverage.json `sim_name` mapping:**
>
> | Workload YAML field pattern | signal_coverage `sim_name` |
> |----------------------------|---------------------------|
> | `queue_depth_range`, `queue_depth_min/max` | `QueueDepth` |
> | `kv_util_range`, `kv_util_min/max`, `kv_utilization` | `KVUtilization` |
> | `in_flight_range`, `in_flight_requests` | `InFlightRequests` |
> | `cache_hit_rate`, `cache_hit_range` | `CacheHitRate` |
>
> If a workload YAML contains parameter ranges for fields not in this table, check `workspace/signal_coverage.json` `signals[].sim_name` for exact matches. The table above covers v1 EVOLVE-BLOCK signals; future algorithms may introduce additional signal names.

Use the `llm-d-benchmark` harness (submodule at `llm-d-benchmark/`) to run baseline and transfer benchmark configurations. Save results to `workspace/benchmark_results.json`:

```json
{"workloads": [
    {"name": "workload-name", "classification": "matched|unmatched",
     "baseline_p99": 0.45, "transfer_p99": 0.40}
]}
```

Then compute mechanism check:

```bash
python tools/transfer_cli.py benchmark --results workspace/benchmark_results.json --t-eff <T_EFF_FROM_STEP_1>
```

**HALT if exit code 2 (infrastructure error — file missing or invalid JSON).** Message: "HALT: benchmark infrastructure error." Do NOT attempt to parse JSON output on exit code 2; the output may be absent or malformed.

⚠ **Unlike other pipeline commands, `benchmark` exits 0 for both PASS and INCONCLUSIVE.** You MUST parse the JSON output to check `mechanism_check_verdict` — do NOT rely on exit code alone.

**HALT if mechanism_check_verdict == "FAIL".** Message: "HALT: Mechanism check FAIL — generated scorer shows no improvement."
**HALT if mechanism_check_verdict == "INCONCLUSIVE".** Message: "HALT: Mechanism check INCONCLUSIVE — improvement detected but below T_eff threshold."
  Remediation options for INCONCLUSIVE:
  1. **Re-run with more baseline samples** — increase from 5 to 10+ runs in Step 1 to reduce T_eff (lower noise → lower threshold → INCONCLUSIVE may become PASS).
  2. **Re-run during lower-variance window** — cluster noise may be temporarily elevated; retry during off-peak hours.
  3. **Inspect per-workload improvements** — check the `workload_classification` array in the benchmark JSON output. If one matched workload is close to T_eff, a targeted re-run of that workload with more samples may resolve the ambiguity.
  4. **Accept as soft-pass with operator sign-off** — if improvement is consistent across matched workloads but marginally below T_eff, the operator may document the rationale in the `operator_notes` field of `validation_results.json` and proceed to Stage 6 with an `overall_verdict: "INCONCLUSIVE"`. This is the **only** path that produces an INCONCLUSIVE overall_verdict — it requires explicit operator sign-off.
Record: mechanism_check_verdict, workload classification results, specificity_notes.

## Step 6: Write validation_results.json

Compile results from Steps 1–5 into `workspace/validation_results.json`:

```json
{
  "suite_a": {
    "passed": <true|false>,
    "kendall_tau": <mean_tau>,
    "max_abs_error": <max_abs_err>,
    "tuple_count": <tuple_count from Step 2 test output (may be < 200 if tuples were skipped)>
  },
  "suite_b": {
    "passed": true,
    "rank_stability_tau": <tau>,
    "threshold_crossing_pct": 0.0,
    "informational_only": true
  },
  "suite_c": {
    "passed": <true|false>,
    "deterministic": true,
    "max_pile_on_ratio": <ratio>
  },
  "benchmark": {
    "passed": <mechanism_check_verdict == "PASS">,
    "mechanism_check_verdict": "<PASS|FAIL|INCONCLUSIVE>",
    "t_eff": <t_eff>,
    "workload_classification": <copy from benchmark CLI output's "workload_classification" field>,
    "specificity_notes": <convert each CLI "specificity_failures" entry to a string, e.g., "workload-X: |change|/baseline=0.35 >= T_eff=0.30"; empty array [] if none>
  },
  "overall_verdict": "<PASS|FAIL|INCONCLUSIVE>",
  "noise_cv": <max_cv_from_step_1>,
  "operator_notes": "<required if overall_verdict is INCONCLUSIVE; omit otherwise>"
}
```

**Computing `noise_cv`:** The `noise-characterize` command outputs `per_metric_cv` (a dict keyed by metric name, e.g. `{"p50": 0.03, "p95": 0.04, "p99": 0.05}`). Compute `noise_cv = max(per_metric_cv.values())`. For example, if per_metric_cv is `{"p50": 0.03, "p95": 0.04, "p99": 0.05}`, then `noise_cv = 0.05`. Do NOT use `t_eff` as a substitute (t_eff = max(0.05, 2*noise_cv), which is a derived value).

**overall_verdict computation:**
- PASS iff suite_a.passed AND suite_c.passed AND benchmark.mechanism_check_verdict == "PASS"
- INCONCLUSIVE iff suite_a.passed AND suite_c.passed AND benchmark.mechanism_check_verdict == "INCONCLUSIVE" AND operator sign-off was given (Step 5 Option 4). The `operator_notes` field MUST be populated with the sign-off rationale.
- FAIL otherwise
- Suite B excluded from v1 verdict (informational_only=true)
- Note: INCONCLUSIVE is only reachable via the Step 5 Option 4 operator sign-off path. Without sign-off, Step 5 HALTs on INCONCLUSIVE benchmark verdict before reaching Step 6.
<!-- TODO(v2-precise-scorer): Include Suite B in verdict when staleness_window_ms > 0 -->

Validate the written artifact:

```bash
python tools/transfer_cli.py validate-schema workspace/validation_results.json
```

**HALT if validate-schema exits non-zero.**

**Manual verification (required — the lightweight validator cannot enforce `if/then` conditionals):**
If `overall_verdict` is `"INCONCLUSIVE"`, verify that `operator_notes` is present and non-empty in `workspace/validation_results.json`. This is the audit trail for the Option 4 soft-pass path. **HALT if `overall_verdict` is `"INCONCLUSIVE"` and `operator_notes` is absent or empty.** Message: "HALT: operator_notes required for INCONCLUSIVE verdict (Option 4 soft-pass audit trail)."

## Step 7: Proceed to Stage 6

If overall_verdict == "PASS", proceed to `prompts/pr.md` (Stage 6). **Note:** `prompts/pr.md` is a PR6 deliverable and will not exist until PR6 is merged. If PR6 has not yet landed, stop here — Stage 5 is complete and Stage 6 will be available after PR6.
If overall_verdict == "INCONCLUSIVE" (only reachable via Step 5 Option 4 operator sign-off), proceed to Stage 6 with the documented rationale in `operator_notes`. The same PR6 note above applies.
If overall_verdict == "FAIL", do NOT proceed — stop and document the failure.

## Halt Conditions Summary

| Condition | Trigger | Action |
|-----------|---------|--------|
| Missing prerequisite artifact | algorithm_summary.json, signal_coverage.json, or stage3_output.json absent/invalid; or Stage 4 `go build`/`go vet` fails | HALT: "Stage [N] prerequisite missing" |
| Suite A SKIP (false pass) | algorithm_summary.json absent → `t.Skip` → exit 0 without running equivalence | Caught by prerequisite check above; algorithm_summary.json must exist before Suite A runs |
| Noise CV > 15% | noise-characterize exit 1 | HALT: "Noise too high" |
| Noise infrastructure error | noise-characterize exit 2 | HALT: "noise-characterize infrastructure error" |
| Benchmark infrastructure error | benchmark exit 2 (file missing or invalid JSON) | HALT: "benchmark infrastructure error" |
| Suite A FAIL | PIPESTATUS[0] non-zero or `"Action":"fail"` in JSON output (rank divergence or key-format mismatch) | HALT: "Suite A FAIL — check test output for root cause" |
| Suite C FAIL | PIPESTATUS[0] non-zero or `"Action":"fail"` in JSON output (determinism violated or pile-on > 2.0) | HALT: "Suite C FAIL" |
| Mechanism FAIL | no matched workload improvement ≥ T_eff | HALT: "Mechanism check FAIL" |
| Mechanism INCONCLUSIVE | improvement > 0 but < T_eff | HALT: "Mechanism check INCONCLUSIVE" — **unless** operator invokes Option 4 (soft-pass with sign-off). If Option 4 is chosen, proceed to Step 6 with `overall_verdict: "INCONCLUSIVE"` and populate `operator_notes` with rationale. See Step 5 Option 4 and Step 7 for details. |
| Specificity check failures | Unmatched workload(s) with |change|/baseline ≥ T_eff | No halt — informational only. Record in `specificity_notes` array of benchmark object for audit trail. Does not affect mechanism_check_verdict. |
| Schema validation failure | validate-schema exits non-zero | HALT: "Schema validation failed" |
