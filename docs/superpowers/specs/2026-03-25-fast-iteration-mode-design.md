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

`merge-values` currently passes ALL keys from `env_defaults.yaml` into `values.yaml` via `_deep_merge`. The `pipeline` block must be explicitly stripped from the env data before the merge so it does not appear in `workspace/tekton/values.yaml` (which is consumed by `compile-pipeline` / Tekton templates). Only the `pipeline` key is stripped; all other top-level keys (including `observe`, `stack`, `gaie`) continue to pass through unchanged. This is a required change to `cmd_merge_values` and is included in the implementation scope.

### Stage 5 (`prompts/validate.md`) — Fast-Iteration Branch

A new **Fast-Iteration Check** section is inserted immediately after the prerequisites block, before Step 1 (Noise Characterization Gate):

```bash
FAST_ITER=$(.venv/bin/python -c "
import yaml
d = yaml.safe_load(open('config/env_defaults.yaml'))
val = d.get('pipeline', {}).get('fast_iteration', True)
print('true' if val else 'false')
")

if [ "$FAST_ITER" = "true" ]; then
  # fast-iteration path — see below
fi
```

The Python snippet normalises the YAML value to lowercase `"true"` or `"false"` regardless of how it is written in the file (`true`, `yes`, `True`, etc.). The `.get('pipeline', {}).get('fast_iteration', True)` fallback defaults to `true` if the key is absent (safe for older checkouts).

**If `FAST_ITER` is `"true"`:**

> **All existing prerequisite checks still run in fast mode.** The fast-iteration check is placed after the prerequisites block, not before it. Prerequisites (including `validate-schema workspace/algorithm_summary.json`) are required by Suites A/B/C and must not be skipped.

1. Print: `"FAST MODE: Skipping noise gate and cluster benchmarks (pipeline.fast_iteration=true)"`
2. Skip Step 1 (Noise Characterization Gate) entirely — jump directly to Step 2.
3. Run Suites A, B, and C (existing Steps 2–4) unchanged.
4. Write `workspace/validation_results.json` with `suite_a`, `suite_b`, `suite_c` results. The `overall_verdict` is derived from suite results only (`PASS` if all three pass, `FAIL` otherwise). The `benchmark`, `noise_cv`, and schema-required `overall_verdict` for full mode are intentionally absent; the artifact is not schema-validated.
5. Print: `"FAST MODE: Cluster benchmarks skipped. Set pipeline.fast_iteration=false to run full validation."`
6. Exit 0.

**If `FAST_ITER` is `"false"`:** proceed with Step 1 (Noise Characterization Gate) and the full pipeline as today.

> **Stale artifact note:** If a schema-valid `validation_results.json` exists from a previous full-pipeline run, fast mode will overwrite it with a partial (non-schema-valid) file. If you subsequently flip back to full mode, you must re-run Stage 5 from Step 1. Flipping the flag and proceeding directly to Stage 6 will fail at Stage 6's prerequisite schema check — this is the expected enforcement gate.

Since Stage 6 is also skipped in fast mode, the partial `validation_results.json` is never consumed by a downstream stage.

### Stage 6 (`prompts/pr.md`) — Fast-Iteration Branch

A new **Fast-Iteration Check** section is inserted at the very top of the prompt, **before the prerequisites block** (including before the `validate-schema workspace/validation_results.json` prerequisite check). This ordering is critical: in fast mode the `validation_results.json` is a partial artifact that will fail schema validation, so the fast-iteration check must fire and exit before any prerequisite validation runs.

```bash
FAST_ITER=$(.venv/bin/python -c "
import yaml
d = yaml.safe_load(open('config/env_defaults.yaml'))
val = d.get('pipeline', {}).get('fast_iteration', True)
print('true' if val else 'false')
")

if [ "$FAST_ITER" = "true" ]; then
  echo "FAST MODE: PR creation skipped (pipeline.fast_iteration=true)."
  echo "Set pipeline.fast_iteration=false and re-run Stage 6 when ready to create PRs."
  exit 0
fi
```

**If `FAST_ITER` is `"true"`:** exits 0 immediately. No artifacts written. The prerequisite checks and PR creation steps are not reached.

**If `FAST_ITER` is `"false"`:** fall through to the prerequisites block and PR creation as today. If an operator flips `fast_iteration` to `false` and runs Stage 6 without first re-running full Stage 5, the `validate-schema workspace/validation_results.json` prerequisite check will fail (partial artifact missing required fields) — this is the intended enforcement gate.

> **Ordering invariant:** The fast-iteration check in pr.md must always appear before any prerequisite validation. This ordering is a correctness requirement: the partial `validation_results.json` written by fast-mode Stage 5 will fail schema validation. Any future edit to pr.md must preserve this ordering.

> **Note:** `build-push-epp` internally calls `cmd_merge_values`; the `pipeline` key strip (which occurs before `_deep_merge` is called) applies there too, which is correct. `cmd_build_push_epp`'s own direct reads of `env_defaults.yaml` (e.g., `epp_image.build`) are unaffected — the strip is applied to the dict passed to `_deep_merge`, not to the file.

### Issue #27 — Benchmark Comparison Table (full-mode only)

A new **Step 5e: Benchmark Comparison Table** is appended to Stage 5 after the existing Step 5d (Generate evidence document), in the full-mode path only (naturally unreachable in fast mode since that path exits early at the fast-iteration check):

```bash
.venv/bin/python tools/transfer_cli.py compare \
  --baseline workspace/baseline_results.json \
  --treatment workspace/treatment_results.json \
  --out workspace/comparison_table.txt
```

HALT if `compare` exits non-zero.

The table is printed to stdout and written to `workspace/comparison_table.txt` (gitignored via `workspace/`). Stdout output is human feedback only — not pipeline-consumable. The file is the durable artifact. `workspace/comparison_table.txt` should also be added to the `outputs:` frontmatter of `validate.md`.

#### `compare` subcommand (`tools/transfer_cli.py`)

New subcommand. Reads `baseline_results.json` and `treatment_results.json`. The current schemas (`additionalProperties: false`) define exactly four metrics per workload: `ttft_p50`, `ttft_p99`, `tpot_p50`, `tpot_p99`. The table has four rows per workload. Lower latency is always better (negative delta = `better`; positive delta = `worse`).

If multiple workloads are present, one table section is printed per workload. Columns: metric, baseline (ms), treatment (ms), delta (ms), change (% + `better`/`worse` label).

**Workload mismatch policy:** If a workload name appears in one file but not the other, print a warning line (`WARN: workload <name> missing in <file> — skipped`) and continue. If at least one workload pair is found, exit 0. If no workloads can be paired at all, exit 1.

**Re-run behaviour:** If Step 5e HALTs (exit 1), `workspace/comparison_table.txt` may not exist or may be stale from a prior run. When re-running Stage 5 after fixing the issue, Step 5e will overwrite the file; no cleanup needed.

Example (single workload):

```
=== Workload: gpt-j-6b-1000rps ===
Metric      Baseline   Treatment  Delta(ms)   Change
────────────────────────────────────────────────────
TTFT p50     142.3      128.7      -13.6      -9.6% (better)
TTFT p99     201.4      195.8       -5.6      -2.8% (better)
TPOT p50      29.8       31.1       +1.3      +4.4% (worse)
TPOT p99      45.2       47.6       +2.4      +5.3% (worse)
```

Exit 0 on success. Exit 1 if input files are missing or malformed.

## Artifacts

| Artifact | Fast mode | Full mode |
|---|---|---|
| `workspace/validation_results.json` | Written (suites only; no benchmark, noise_cv, or overall_verdict in full-mode schema sense) | Written (complete, schema-valid; partial written by Step 4b, completed by Step 5c-merge) |
| `workspace/comparison_table.txt` | Not written | Written |
| PRs in llm-d repos | Not created | Created |

## Exit Codes

Stage 5 and Stage 6 exit codes are unchanged. Fast mode exits 0 on success, 1 on suite failure or infrastructure error — same as today.

## Implementation Scope

| File | Change |
|---|---|
| `config/env_defaults.yaml` | Add `pipeline.fast_iteration: true` |
| `prompts/validate.md` | Add fast-iteration check after prerequisites; if fast, run Steps 2–4, write partial artifact, exit 0; add Step 5e (compare) after existing Step 5d in full-mode path; add `workspace/comparison_table.txt` to `outputs:` frontmatter |
| `prompts/pr.md` | Add fast-iteration check at top |
| `tools/transfer_cli.py` | Add `compare` subcommand; add `pipeline` key strip in `cmd_merge_values` before `_deep_merge` |

No changes to Stages 1–4, `transfer_cli.py` subcommands other than `compare`, or any workspace schemas (partial `validation_results.json` in fast mode is intentionally not schema-validated).
