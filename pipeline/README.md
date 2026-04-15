# pipeline/

Four scripts that drive the sim2real transfer pipeline. Run from the repo root.

```
setup.py → prepare.py → [/sim2real-translate] → deploy.py
```

`run.py` manages runs independently of the main flow.

---

## setup.py

One-time, idempotent cluster bootstrap. Safe to re-run.

```bash
python pipeline/setup.py [flags]
```

| Flag | Env var | Default |
|------|---------|---------|
| `--namespace NS` | `NAMESPACE` | interactive |
| `--hf-token TOKEN` | `HF_TOKEN` | interactive |
| `--registry REG` | — | interactive |
| `--registry-user USER` | `QUAY_ROBOT_USERNAME` | interactive |
| `--registry-token TOKEN` | `QUAY_ROBOT_TOKEN` | interactive |
| `--run NAME` | — | `sim2real-YYYY-MM-DD` |
| `--no-cluster` | — | false |
| `--redeploy-tasks` | — | false |

**`--no-cluster`** — generates `setup_config.json` without touching the cluster; useful when cluster access comes later.

**`--redeploy-tasks`** — fast path to re-apply Tekton step/task YAMLs only (requires `--namespace`).

Writes `workspace/setup_config.json` and `workspace/runs/<run>/run_metadata.json`. Subsequent scripts read namespace, registry, and run name from `setup_config.json`.

---

## prepare.py

6-phase state machine. Re-running skips completed phases.

```bash
python pipeline/prepare.py [--force] [--rebuild-context] [--manifest PATH] [--run NAME]
```

| Phase | Name | Skippable |
|-------|------|-----------|
| 1 | Init — validate manifest + prerequisites | on re-run |
| 2 | Context — assemble + cache context doc (SHA-256) | on cache hit |
| 3 | **Translate checkpoint** — write `skill_input.json`, wait | resumes on re-run |
| 4 | Assembly — `algorithm_values.yaml`, `values.yaml`, cluster YAMLs | on re-run |
| 5 | Summary — write `run_summary.md` | on re-run |
| 6 | **Gate** — `[d]eploy / [e]dit / [q]uit` | on re-run |

**Phase 3 checkpoint**: writes `skill_input.json` and exits cleanly (exit 0). Run `/sim2real-translate` in Claude Code, then re-run `prepare.py` to continue from Phase 4.

**Phase 6 gate**: `d` marks the run `READY TO DEPLOY` (required by `deploy.py`). `e` drops you back to edit files, then re-displays the summary. `q` marks `abandoned` and exits.

**Subcommands:**

```bash
python pipeline/prepare.py status              # show phase state for current run
python pipeline/prepare.py context             # rebuild context cache only
python pipeline/prepare.py assemble            # re-run phases 4–6 (translation must exist)
python pipeline/prepare.py validate-assembly   # run assembly checks standalone
```

**`--force`** — ignores `.state.json` and regenerates all phases.

**`--rebuild-context`** — ignores SHA cache and re-assembles context.

Phase state is tracked per-run in `workspace/runs/<run>/.state.json`. Delete it (or use `--force`) to reset.

---

## deploy.py

Builds the EPP image, applies Tekton resources, and submits PipelineRuns. Requires gate verdict `READY TO DEPLOY`.

```bash
python pipeline/deploy.py [flags]
```

| Flag | Default | Notes |
|------|---------|-------|
| `--run NAME` | from `setup_config.json` | override active run |
| `--package NAME…` | `experiment` | `baseline`, `treatment`, or `experiment` |
| `--skip-build-epp` | false | reuse `epp_image` from `run_metadata.json` |
| `--dry-run` | false | print kubectl commands without applying |

**Default package is `experiment`** — a sequential baseline-then-treatment pipeline. Pass `--package baseline treatment` to submit them independently.

**`--skip-build-epp`** — skips the image build; use when resubmitting after a failed PipelineRun without changing the scorer.

**Collect results:**

```bash
python pipeline/deploy.py collect [--package NAME…]
```

Polls PipelineRun status, extracts results from the cluster PVC, and writes to `workspace/runs/<run>/results/{baseline,treatment}/<workload>/`.

---

## run.py

Manage and switch between runs.

```bash
python pipeline/run.py list                     # all runs: name, scenario, phase, verdict, active
python pipeline/run.py inspect <name>           # full detail: phases, generated files, deploy stages
python pipeline/run.py switch <name>            # set active run + sync generated/ into submodule
```

**`switch`** copies files listed in `translation_output.json` (`files_created` + `files_modified`) into the `llm-d-inference-scheduler` submodule and updates `setup_config.json`. Prompts before overwriting uncommitted submodule changes.

---

## config/transfer.yaml

Manifest consumed by `prepare.py`. Version 3 required.

```yaml
kind: sim2real-transfer
version: 3
scenario: <name>            # must match a scenario in config/env_defaults.yaml

algorithm:
  source: <path>            # sim algorithm implementation
  config: <path>            # sim algorithm config

baseline:
  sim:
    config: <path>          # baseline policy for sim
  real:
    config: <path>          # optional: baseline EPP config template
    notes: |                # optional: notes embedded in skill_input.json

workloads:
  - <path>                  # one or more workload YAMLs

hints:
  files: [<path>, ...]      # hint files passed to translation skill
  text: |                   # freeform hint text

context:
  files: [<path>, ...]      # files assembled into context document (Phase 2)
```

All paths are relative to the repo root and validated at Phase 1.

---

## Common patterns

```bash
# Resume after translation
python pipeline/prepare.py

# Force full regeneration
python pipeline/prepare.py --force

# Resubmit without rebuilding EPP
python pipeline/deploy.py --skip-build-epp

# Dry-run deploy
python pipeline/deploy.py --dry-run

# Deploy individual packages
python pipeline/deploy.py --package baseline treatment

# Collect results for a specific package
python pipeline/deploy.py collect --package treatment
```
