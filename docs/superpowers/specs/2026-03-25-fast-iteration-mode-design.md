# Fast-Iteration Mode + Benchmark Comparison Table

**Issues:** #26 (fast-iteration mode), #27 (benchmark comparison table)
**Date:** 2026-03-25
**Status:** Approved

## Problem

The sim2real transfer pipeline is slow to iterate during active algorithm development. Two stages consume significant time without value during development cycles:

- **Stage 5 cluster benchmarks** — noise characterization requires multiple K8s pipeline runs deploying fresh infra. Suites A/B/C (local Go tests) are fast; the cluster work is not.
- **Stage 6 PR creation** — creates unwanted repository activity before an algorithm is ready.

Additionally, after cluster benchmarks complete in full-pipeline mode, there is no automated performance summary (issue #27).

## Goals

1. Skip cluster benchmarks and PR creation by default during active development.
2. Provide a single, persistent config toggle to switch between fast and full pipeline.
3. Add an automated benchmark comparison table to full-pipeline Stage 5 (always, not gated by fast mode).

## Non-Goals

- Per-run CLI flags.
- Separate prompt file variants.
- Changing any behavior of Suites A, B, or C.

## Design

### Config (`config/env_defaults.yaml`)

Add a `pipeline` block:

```yaml
pipeline:
  fast_iteration: true   # Skip cluster benchmarks (Stage 5) and PR creation (Stage 6).
                         # Set to false when algorithm is ready for full validation.
```

Default is `true`. To run the full pipeline, change to `false` and re-run the affected stages.

`merge-values` passes through only keys it recognizes; the `pipeline` key is not merged into `values.yaml`. An explicit ignore guard will be added if needed after verification.

### Stage 5 (`prompts/validate.md`) — Fast-Iteration Branch

A new **Fast-Iteration Check** section is inserted immediately after the prerequisites block, before Step 1 (Noise Characterization Gate):

```bash
FAST_ITER=$(python -c "import yaml; \
  print(yaml.safe_load(open('config/env_defaults.yaml'))['pipeline']['fast_iteration'])")
```

**If `FAST_ITER` is `True`:**

1. Print: `"FAST MODE: Skipping cluster benchmarks (pipeline.fast_iteration=true)"`
2. Run Suites A, B, and C (existing Steps 2–4) unchanged.
3. Write `workspace/validation_results.json` with `suite_a`, `suite_b`, `suite_c` results and an `overall_verdict` derived from suite results only (`PASS` if all three pass, `FAIL` otherwise). The `benchmark` and `noise_cv` fields are intentionally omitted.
4. Print: `"FAST MODE: Cluster benchmarks skipped. Set pipeline.fast_iteration=false to run full validation."`
5. Exit 0.

**If `FAST_ITER` is `False`:** proceed with Step 1 (Noise Characterization Gate) and the full pipeline as today.

Since Stage 6 is also skipped in fast mode, the partial `validation_results.json` (missing `benchmark`/`noise_cv`) is never consumed by a downstream stage.

### Stage 6 (`prompts/pr.md`) — Fast-Iteration Branch

A new **Fast-Iteration Check** section is inserted at the very top, before the prerequisites block:

```bash
FAST_ITER=$(python -c "import yaml; \
  print(yaml.safe_load(open('config/env_defaults.yaml'))['pipeline']['fast_iteration'])")
```

**If `FAST_ITER` is `True`:**

1. Print: `"FAST MODE: PR creation skipped (pipeline.fast_iteration=true)."`
2. Print: `"Set pipeline.fast_iteration=false and re-run Stage 6 when ready to create PRs."`
3. Exit 0. No artifacts written.

**If `FAST_ITER` is `False`:** proceed with prerequisites and PR creation as today.

### Issue #27 — Benchmark Comparison Table (full-mode only)

A new **Step 5d: Benchmark Comparison Table** is appended to Stage 5 after Step 5c-merge, in the full-mode path only (naturally unreachable in fast mode since that path exits early):

```bash
python tools/transfer_cli.py compare \
  --baseline workspace/baseline_results.json \
  --treatment workspace/treatment_results.json \
  --out workspace/comparison_table.txt
```

HALT if `compare` exits non-zero.

The table is printed to stdout and written to `workspace/comparison_table.txt` (gitignored via `workspace/`).

#### `compare` subcommand (`tools/transfer_cli.py`)

New subcommand. Reads `baseline_results.json` and `treatment_results.json`. Produces a nine-row ASCII table (TTFT/TPOT/E2E × mean/p50/p99) with columns: metric, baseline (ms), treatment (ms), delta (ms), change (% + `better`/`worse` label). Lower latency = better.

Example:

```
Metric         Baseline   Treatment  Delta(ms)  Change
─────────────────────────────────────────────────────
TTFT mean       142.3      128.7      -13.6      -9.6% (better)
TTFT p50        138.1      124.2      -13.9     -10.1% (better)
TTFT p99        201.4      195.8       -5.6      -2.8% (better)
TPOT mean        31.2       32.8       +1.6      +5.1% (worse)
TPOT p50         29.8       31.1       +1.3      +4.4% (worse)
TPOT p99         45.2       47.6       +2.4      +5.3% (worse)
E2E mean        173.5      161.5      -12.0      -6.9% (better)
E2E p50         168.0      155.3      -12.7      -7.6% (better)
E2E p99         246.6      243.4       -3.2      -1.3% (better)
```

Exit 0 on success. Exit 1 if input files are missing or malformed.

## Artifacts

| Artifact | Fast mode | Full mode |
|---|---|---|
| `workspace/validation_results.json` | Written (suites only, no benchmark/noise_cv) | Written (complete, schema-valid) |
| `workspace/comparison_table.txt` | Not written | Written |
| PRs in llm-d repos | Not created | Created |

## Exit Codes

Stage 5 and Stage 6 exit codes are unchanged. Fast mode exits 0 on success, 1 on suite failure or infrastructure error — same as today.

## Implementation Scope

| File | Change |
|---|---|
| `config/env_defaults.yaml` | Add `pipeline.fast_iteration: true` |
| `prompts/validate.md` | Add fast-iteration check after prerequisites; add Step 5d (compare) in full-mode path |
| `prompts/pr.md` | Add fast-iteration check at top |
| `tools/transfer_cli.py` | Add `compare` subcommand |

No changes to Stages 1–4, `transfer_cli.py` subcommands other than `compare`, or any workspace schemas (partial `validation_results.json` in fast mode is intentionally not schema-validated).
