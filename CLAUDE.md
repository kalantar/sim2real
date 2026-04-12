# CLAUDE.md — sim2real

## Project Overview

sim2real is a pipeline for transferring simulation-discovered routing algorithms from
inference-sim to production llm-d-inference-scheduler scorer plugins.

## Repository Structure

- `config/` — Version-controlled environment defaults (`env_defaults.yaml`, `transfer.yaml`) — infrastructure choices and algorithm manifest
- `docs/transfer/` — Mapping artifacts, scorer template, calibration log
- `docs/plans/` — Design docs and implementation plans
- `pipeline/` — Pipeline entry points and shared library (see [`pipeline/README.md`](pipeline/README.md))
- `workspace/` — Inter-stage artifacts (gitignored, not committed)

## Submodules

- `inference-sim/` — Discrete-event LLM inference simulator (source of evolved algorithms)
- `llm-d-inference-scheduler/` — Production scheduler with scorer plugin system (target)
- `tektonc-data-collection/` — Tekton-based cluster data collection pipeline

## Transfer Pipeline

The pipeline runs in four scripts with a skill invoked between prepare and deploy:

```
setup.py → prepare.py → [/sim2real-translate] → deploy.py
```

**`pipeline/setup.py`** — One-time cluster bootstrap (namespace, RBAC, secrets, PVCs, Tekton tasks). Idempotent — safe to re-run.

**`pipeline/prepare.py`** — 6-phase state machine. Re-running skips completed phases (tracked in `.state.json`):

| Phase | Name | Description |
|-------|------|-------------|
| 1 | Init | Load `config/transfer.yaml`, validate file prerequisites |
| 2 | Context | Assemble context document, cache by SHA-256 hash |
| 3 | Translate checkpoint | Write `skill_input.json`; exit and wait for `/sim2real-translate` skill |
| 4 | Assembly | Generate `algorithm_values.yaml`, merge values, compile cluster YAMLs |
| 5 | Summary | Write `run_summary.md` |
| 6 | Gate | Human review: `[d]eploy / [e]dit / [q]uit` |

**`/sim2real-translate`** — AI skill that reads `skill_input.json` and writes `translation_output.json`. Run this after prepare exits at Phase 3, then re-run prepare to continue.

**`pipeline/deploy.py`** — Builds EPP image, applies Tekton Pipeline resources, submits PipelineRuns. Use `deploy.py collect` to pull results from the cluster PVC after runs complete.

**`pipeline/run.py`** — Lists, inspects, and switches between runs. `switch` syncs generated files into the `llm-d-inference-scheduler` submodule.

## Pipeline Library (`pipeline/lib/`)

| Module | Purpose |
|--------|---------|
| `manifest.py` | Loads and validates `config/transfer.yaml` (v2/v3 schema) |
| `state_machine.py` | Phase tracking with atomic JSON persistence (`.state.json`) |
| `context_builder.py` | Assembles context document, caches by SHA-256 hash |
| `values.py` | Deep-merges env defaults + algorithm values into `values.yaml` |
| `tekton.py` | Compiles Tekton Pipeline/PipelineRun YAMLs via `tektonc.py` |
| `run_manager.py` | `list_runs`, `inspect_run`, `switch_run` logic |

## Workspace Artifacts

All artifacts live under `workspace/` (gitignored). Key files:

| File | Written by | Read by |
|------|-----------|---------|
| `setup_config.json` | `setup.py` | `prepare.py`, `deploy.py` |
| `runs/<run>/.state.json` | `prepare.py` | `prepare.py`, `deploy.py` |
| `runs/<run>/run_metadata.json` | `setup.py`, `deploy.py` | `deploy.py`, `run.py` |
| `runs/<run>/skill_input.json` | `prepare.py` Phase 3 | `/sim2real-translate` skill |
| `runs/<run>/translation_output.json` | `/sim2real-translate` skill | `prepare.py` Phase 4 |
| `runs/<run>/algorithm_values.yaml` | `prepare.py` Phase 4 | `prepare.py` Phase 4 (merge) |
| `runs/<run>/values.yaml` | `prepare.py` Phase 4 | `deploy.py` |
| `runs/<run>/cluster/{package}/*.yaml` | `prepare.py` Phase 4 | `deploy.py` |
| `runs/<run>/run_summary.md` | `prepare.py` Phase 5 | human review |
| `runs/<run>/deploy_{phase}_log/` | `deploy.py collect` | `/sim2real-analyze` skill |
| `context/{scenario}/{hash}.md` | `prepare.py` Phase 2 | `prepare.py` Phase 2 (cache) |

## Development

- Python >= 3.10
- Tests: `python -m pytest pipeline/ -v`
- Lint: `ruff check pipeline/` (if installed)
- PyYAML required for `values.py` and `tekton.py` — install via `pip install -r requirements.txt`

## CI

CI runs on every push and PR to `main` (`.github/workflows/test.yml`). **It must pass before merging.**

Two checks run in order:

```bash
# 1. Lint (pyflakes errors only — F codes)
ruff check pipeline/ .claude/skills/ --select F

# 2. Tests
python -m pytest pipeline/ .claude/skills/sim2real-analyze/tests/ .claude/skills/sim2real-translate/tests/ -v
```

Run both locally before pushing. If your change adds a new module, test file location, or skill, update `.github/workflows/test.yml` to include it — CI only covers paths explicitly listed.

## Contributing: Read This First

**Before developing anything in `pipeline/`**, read [`pipeline/README.md`](pipeline/README.md). It documents all entry points, CLI flags, phase behaviors, workspace artifacts, and common patterns.

**After any change to `pipeline/`** that affects CLI flags, phase behavior, artifact schema, or subcommands, update `pipeline/README.md` to match.

## Contributing: Pipeline Stage Contracts

The `pipeline/` scripts form a linear dependency chain:

```
setup.py → prepare.py → [sim2real-translate skill] → deploy.py
```

Each stage communicates with the next through files written to `workspace/`. When modifying any stage, you **must** trace both directions:

- **Upstream** — does your change depend on output produced by a prior stage? Verify the prior stage still produces what you expect, or update it too.
- **Downstream** — does any later stage consume what your stage produces? If you change an output file's schema, keys, or format, update every consumer of that file in the same change.

The workspace artifact table above is the definitive map of what each stage reads and writes. Use it to determine the blast radius of any change.

## Fixing Pipeline Issues: Always Fix Upstream

`workspace/` is generated — any file there can be regenerated by re-running the stage that produced it. **Fixing a workspace file directly is never sufficient.** When a bug is found in a workspace artifact, the fix must go in the source that generates it:

- `algorithm_values.yaml` and `values.yaml` — generated by `prepare.py` Phase 4 (`pipeline/lib/values.py`)
- `translation_output.json` — written by the `/sim2real-translate` skill; re-run the skill to regenerate
- Cluster YAMLs under `cluster/` — generated by `prepare.py` Phase 4 (`pipeline/lib/tekton.py`)
- Any other workspace artifact — trace it to its generating phase and fix there

If a fix only exists in `workspace/`, it will be silently lost the next time that phase runs.

## Two-Layer Tekton Config Architecture

`prepare.py` Phase 4 generates Tekton benchmarking artifacts using a two-layer config model:

**Layer 1 — `config/env_defaults.yaml`** (version-controlled, edit per environment):
Infrastructure choices: gateway type and sizing, connection pool settings, baseline scorer config, model deployment constants (auth secret, service port, `prefill.create`), and `observe.noise_runs`. Also contains:
- `stack.model.vllm_image` — when set, overrides the vLLM serving image (e.g. a llm-d custom build); comment out to use the image from `llm_config` in the manifest.
- `pipeline.fast_iteration` (boolean, default `true`): when `true`, skips noise gate and mechanism check; Stage 6 skips PR creation. Set to `false` for full validation and PR submission. Stripped from `values.yaml`.
- `observe.request_multiplier` (number, default `10`): multiplies each workload's `num_requests` by this factor. Strips from `values.yaml`. Scales sim workloads up for real-cluster benchmarks.

Edit this file when switching gateway providers, tuning cluster-level parameters, overriding the vLLM image, or changing fast-iteration mode.

**Layer 2 — `workspace/runs/<run>/algorithm_values.yaml`** (generated by Phase 4, gitignored):
Values derived from the manifest: model name, vLLM config (replicas, GPU, memory), treatment scorer config, inference-sim image tag, and workload specs. Regenerated each run.

**Merge** is performed automatically by `prepare.py` Phase 4 via `pipeline/lib/values.py`:
- Deep-merges algorithm values over env defaults
- `gaie.shared.helmValues` is flattened into both `gaie.baseline` and `gaie.treatment`; `gaie.shared` is removed from output
- Lists of scalars: replaced entirely. Lists of dicts: named-key merge (by `name`, `mountPath`, or `containerPort`) when all items share a key field; positional deep-merge otherwise. Explicit `[]` clears the base list.
- Output is `workspace/runs/<run>/values.yaml`, consumed by `deploy.py`
