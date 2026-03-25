# CLAUDE.md — sim2real

## Project Overview

sim2real is a pipeline for transferring simulation-discovered routing algorithms from
inference-sim to production llm-d-inference-scheduler scorer plugins.

## Repository Structure

- `blis_router/` — Input artifacts from evolutionary optimization (EVOLVE-BLOCK, metrics, workloads)
- `config/` — Version-controlled environment defaults (`env_defaults.yaml`) — infrastructure choices that BLIS doesn't model
- `docs/transfer/` — Mapping artifacts, scorer template, calibration log
- `docs/plans/` — Design docs and implementation plans
- `tools/` — Python CLI (`transfer_cli.py`) and Go test harness (PR3)
- `tools/schemas/` — JSON Schema files for workspace artifact validation
- `prompts/` — Pipeline stage prompt templates (PR3+)
- `workspace/` — Inter-stage JSON artifacts (gitignored, not committed)

## Submodules

- `inference-sim/` — Discrete-event LLM inference simulator (source of evolved algorithms)
- `llm-d-inference-scheduler/` — Production scheduler with scorer plugin system (target)
- `llm-d-benchmark/` — Benchmark harness for cluster-level validation (target)
- `tektonc-data-collection/` — Tekton-based cluster data collection pipeline (used by `compile-pipeline` subcommand)

## Transfer Pipeline

7-stage prompt-driven pipeline:
1. **Extract** — Parse EVOLVE-BLOCK, produce algorithm_summary.json
2. **Translate** — Map sim signals to production equivalents
3. **Generate** — LLM produces scorer plugin code
3.5. **Validate Translation** — Verify generated code faithfully implements EVOLVE-BLOCK logic
4. **Test** — Build + test with retry logic
5. **Validate** — 3-suite equivalence + cluster benchmarks
6. **PR** — Create PRs in target repos

## CLI Commands

```bash
# Extract algorithm metadata
python tools/transfer_cli.py extract blis_router/best/

# Extract with strict fidelity checks (recommended for CI)
python tools/transfer_cli.py extract --strict blis_router/best/

# Validate mapping artifact completeness
# NOTE: commit hash check only verifies presence, not currency vs submodule HEAD;
# CI workflow performs the real hash comparison
python tools/transfer_cli.py validate-mapping

# Validate workspace artifact against JSON Schema
python tools/transfer_cli.py validate-schema workspace/algorithm_summary.json

# Classify go build/test output into error classes (stdin)
# Exit 0 = clean, 1 = error found, 2 = CLI infrastructure error
echo "<go build/test output>" | python tools/transfer_cli.py test-status

# Mechanical pre-checks for translation fidelity (Stage 3.5)
# Exit 0 = all pass, 1 = failures found, 2 = infrastructure error
python tools/transfer_cli.py validate-translation \
  --algorithm workspace/algorithm_summary.json \
  --signal-coverage workspace/signal_coverage.json \
  --scorer-file llm-d-inference-scheduler/pkg/plugins/scorer/<scorer_name>.go

# noise-characterize removed — superseded by noise pipeline in Stage 5

# Merge env_defaults + algorithm_values → values.yaml (Stage 3 Step 8)
python tools/transfer_cli.py merge-values \
  --env config/env_defaults.yaml \
  --algorithm workspace/tekton/algorithm_values.yaml \
  --out workspace/tekton/values.yaml

# Compute mechanism check from noise/baseline/treatment results
python tools/transfer_cli.py benchmark \
  --noise workspace/noise_results.json \
  --baseline workspace/baseline_results.json \
  --treatment workspace/treatment_results.json \
  --signal-coverage workspace/signal_coverage.json \
  --workloads-dir blis_router/workloads/ \
  --out workspace/benchmark_output.json

# Print latency comparison table from baseline/treatment results (Stage 5 Step 5e)
# Exit 0 = table produced; 1 = no workloads paired or files missing/malformed
python tools/transfer_cli.py compare \
  --baseline workspace/baseline_results.json \
  --treatment workspace/treatment_results.json \
  --out workspace/comparison_table.txt
```

## Important: Artifact Consumption

The `extract` command produces **two** JSON outputs:
1. **File artifact** (`workspace/algorithm_summary.json`) — the pipeline contract, validated by schema. **All downstream stages MUST consume this file.**
2. **Stdout JSON** — operational metadata for human/CI feedback. **Do NOT consume stdout in downstream stages.**

**Artifact existence on failure:** The `extract` command has three distinct exit-code-1 failure modes with different artifact postconditions:
- **Fidelity failure** (low-fidelity signal detected): Exits **before** writing the artifact file. `workspace/algorithm_summary.json` will **not exist** on disk.
- **Strict-mode minimum-signal failure** (`--strict` with fewer than 2 signals): Exits **before** writing the artifact file. `workspace/algorithm_summary.json` will **not exist** on disk.
- **Scope failure** (out-of-scope pattern detected): Exits **after** writing the artifact file. `workspace/algorithm_summary.json` **will exist** on disk (with `scope_validation_passed: false`).

Downstream stages MUST use the exit code (not file existence) as the success signal. A stale artifact from a prior successful run may remain on disk after any pre-write failure (fidelity failure or strict-mode minimum-signal failure).

## Development

- Python >= 3.10, stdlib only (no external dependencies for most subcommands)
- Tests: `python -m pytest tools/ -v`
- Lint: `ruff check tools/` (if installed)

## Fixing Pipeline Issues: Always Fix Upstream

`workspace/` is generated — any file there can be regenerated by re-running the pipeline stage that produced it. **Fixing a workspace file directly is never sufficient.** When a bug is found in a workspace artifact, the fix must go in the source that generates it:

- `workspace/tekton/algorithm_values.yaml` is generated by Stage 3 (`prompts/generate.md`) — fix the prompt.
- `workspace/tekton/values.yaml` is produced by `merge-values` from `config/env_defaults.yaml` + `algorithm_values.yaml` — fix the env defaults or the prompt.
- `workspace/translation_validation.json` is generated by Stage 3.5 (`prompts/validate-translation.md`) — re-run the stage to regenerate.
- Any other workspace artifact — trace it back to its generating stage prompt or CLI tool and fix there.

If a fix only exists in `workspace/`, it will be silently lost the next time that stage runs.

## Notes

- `merge-values` requires PyYAML. `preflight` and `benchmark` also import PyYAML directly and require it to be installed.
  `compile-pipeline` invokes `tektonc-data-collection/tektonc/tektonc.py` via subprocess; that tool requires `jinja2` and `PyYAML`.
  All of these are installed via `pip install -r requirements.txt`.
  The stdlib-only constraint does not apply to these subcommands.
- `benchmark` exit codes: 0 = PASS or INCONCLUSIVE (pipeline should proceed); 1 = FAIL (no matched improvement ≥ T_eff); 2 = ERROR (all workloads skipped due to name mismatch or no matched classifications) or infrastructure failure (missing/malformed input files). Operators must always parse `mechanism_check_verdict` from the output JSON to distinguish PASS from INCONCLUSIVE.
- Stage 5 validate.md exit codes (shell script level, not CLI):
  - `0` = complete (all phases done, all suites passed)
  - `1` = error/halt (context mismatch, ordering violation, suite failure)
  - `2` = infrastructure error (missing artifact, parse failure)
  - `3` = REENTER (noise phase not yet done; operator must jump to Step 5 and re-enter validate.md after noise completes). Automated harnesses must NOT treat exit 3 as a generic failure — it is a planned re-entry pause.

## Two-Layer Tekton Config Architecture

Stage 3 Step 8 generates Tekton benchmarking artifacts using a two-layer config model:

**Layer 1 — `config/env_defaults.yaml`** (version-controlled, edit per environment):
Infrastructure choices that BLIS doesn't model: gateway type and sizing, connection pool settings, baseline (load-aware) scorer config, model deployment constants (auth secret, service port, `prefill.create`), and `observe.noise_runs`. Also contains image overrides applied at merge time:
- `stack.model.vllm_image` — when set, replaces the vLLM serving image from `blis_router/llm_config.yaml` (e.g. substitute a llm-d custom vLLM build like `ghcr.io/llm-d/llm-d-cuda:v0.5.1`); comment out to use the original simulation image.

Also contains `pipeline.fast_iteration` (boolean, default `true`): when `true`, Stage 5 runs Suites A/B/C plus the baseline and treatment cluster pipelines and comparison table, but skips the noise gate and mechanism check. Stage 6 skips PR creation entirely. Set to `false` when the algorithm is ready for full validation (including noise characterization and mechanism check) and PR submission. This key is stripped by `merge-values` and does not appear in `workspace/tekton/values.yaml`.

Also contains `observe.request_multiplier` (number, default `10`): when present and > 1, `merge-values` parses each workload's embedded YAML spec and multiplies `num_requests` by this factor (rounded to int). The key is stripped from `workspace/tekton/values.yaml`. This scales simulation workloads (e.g. 1500 requests) up for real-cluster benchmarks without modifying the source workload files. Set to `1` or remove the key to disable scaling.

Edit this file when switching gateway providers, tuning cluster-level parameters, overriding the vLLM image, or changing the fast-iteration mode. The file is committed and shared across algorithm runs.

**Layer 2 — `workspace/tekton/algorithm_values.yaml`** (generated by Step 8, gitignored):
Values derived from the BLIS experiment: model name and vLLM config (replicas, GPU, memory), treatment scorer EndpointPickerConfig, inference-sim image tag, and workload specs. Regenerated each time a new algorithm is transferred.

**Merge step** (Step 8, Part D):
```bash
python tools/transfer_cli.py merge-values \
  --env config/env_defaults.yaml \
  --algorithm workspace/tekton/algorithm_values.yaml \
  --out workspace/tekton/values.yaml
```
Deep-merges algorithm values over env defaults. `gaie.shared.helmValues` (connection pool, provider, flags) is flattened into both `gaie.baseline` and `gaie.treatment` phases; `gaie.shared` is removed from the output. Lists are replaced (not appended). The merged `workspace/tekton/values.yaml` is what `compile-pipeline` consumes — same shape as before the split.

**Override flow:** If the user specifies a non-default infrastructure option at Stage 3 prompt time (e.g., "use kgateway"), apply the override by editing `config/env_defaults.yaml` before running merge-values. The override persists for future runs.

## Pipeline Status

| PR | Description | Status |
|----|-------------|--------|
| PR1 | Mapping artifact + scaffolding + CLI extract | Complete |
| PR2 | Scorer template artifact | Complete |
| PR3 | Prompt templates (Stages 1-3) + Go harness | Complete |
| PR4 | Stage 4 prompt + test retry logic | Complete |
| PR5 | Validation pipeline (Stage 5) | Complete |
| PR6 | Stage 6 + self-verification + calibration | Complete |
| PR7 | Stage 3.5 translation validation + CLI subcommand | In progress |

