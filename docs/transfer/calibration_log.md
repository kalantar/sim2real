# Transfer Pipeline Calibration Log

This file records per-transfer validation results. Stage 6 appends one entry per transfer.
**Append-only: do not modify existing entries.**

## Schema

Each entry:
```
transfer_date: YYYY-MM-DD
algorithm_name: string
pipeline_commit: string (git sha of sim2real at Stage 1 start)
single_run_provisional: true (v1 — single-run validation, lower statistical confidence)
suite_a_results:
  kendall_tau: float
  max_abs_error: float
suite_b_results:
  rank_stability_tau: float
  threshold_crossing_pct: float
  informational_only: true
suite_c_results:
  deterministic: bool
  max_pile_on_ratio: float
benchmark_results:
  mechanism_check_verdict: PASS|FAIL|INCONCLUSIVE
  t_eff: float
  matched_improvement: float (best matched workload improvement)
noise_cv: float
overall_verdict: PASS|FAIL|INCONCLUSIVE
threshold_adjustments: []
```

## Threshold Review

After 3 transfers with overall_verdict PASS or FAIL (INCONCLUSIVE excluded):
- If >50% of transfers have Kendall-tau margin < 10% above 0.8: tighten threshold
- If >30% fail on Kendall-tau with margin < 5%: loosen threshold
Document adjustments in `threshold_adjustments[]` with rationale.

## Entries

<!-- Stage 6 appends entries below this line -->
