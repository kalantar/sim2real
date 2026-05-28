# pipeline/

Four scripts that drive the sim2real transfer pipeline. Run from the repo root.

```
setup.py → prepare.py → [/sim2real-translate] → deploy.py
```

`run.py` manages runs independently of the main flow.

---

## Running with an Experiment Repo

When algorithm content lives in its own repo (peer directory), pass `--experiment-root`:

```bash
# From the sim2real/ directory:
python pipeline/setup.py   --experiment-root ../admission-control
python pipeline/prepare.py --experiment-root ../admission-control
python pipeline/deploy.py  --experiment-root ../admission-control
```

The experiment repo must contain:
- `transfer.yaml` (or `config/transfer.yaml` for backward compat) — v3 schema with `target`, `config`, `build`, `epp_image` fields
- `baseline.yaml` — llmdbenchmark-style scenario file (required for Phase 4 assembly)
- `treatment.yaml` (optional) — merged into `baseline.yaml` for the treatment scenario
- `algorithm/` and `workloads/` directories as referenced in `transfer.yaml`
- `workspace/` in `.gitignore`

`pipeline/pipeline.yaml` is the static Tekton Pipeline definition (applied by `setup.py`; Phase 4 generates PipelineRuns that reference it).

---

## setup.py

One-time, idempotent cluster bootstrap. Safe to re-run.

```bash
python pipeline/setup.py [flags]
```

| Flag | Env var | Default |
|------|---------|---------|
| `--namespace NS` | `NAMESPACE` | interactive |
| `--namespaces NS1,NS2,...` | — | — |
| `--hf-token TOKEN` | `HF_TOKEN` | interactive |
| `--github-token TOKEN` | `GITHUB_TOKEN` | — |
| `--registry REG` | — | interactive |
| `--registry-user USER` | `REGISTRY_USER` | interactive |
| `--registry-token TOKEN` | `REGISTRY_TOKEN` | interactive |
| `--run NAME` | — | `sim2real-YYYY-MM-DD` |
| `--no-cluster` | — | false |
| `--pipeline-yaml PATH` | — | `pipeline/pipeline.yaml` |
| `--redeploy-tasks` | — | false |

**`--namespaces NS1,NS2,...`** — provision multiple namespace slots for parallel pool execution. Each slot is bootstrapped identically to a single `--namespace`.

**`--no-cluster`** — generates `setup_config.json` without touching the cluster; useful when cluster access comes later.

**`--pipeline-yaml PATH`** — override the default Pipeline YAML definition (`pipeline/pipeline.yaml`). Stored in `setup_config.json`.

**`--redeploy-tasks`** — fast path to re-apply Tekton step/task YAMLs and Pipeline definition (requires `--namespace`).

Writes `workspace/setup_config.json` and `workspace/runs/<run>/run_metadata.json`. Subsequent scripts read namespace, registry, and run name from `setup_config.json`.

`setup_config.json` includes workspace bindings for two PVCs: `data-storage` (`data-pvc`) and `source` (`source-pvc`). `source-pvc` holds the BLIS binary built at pipeline runtime by the `install-blis` task. Both must be bound before `deploy.py run` accepts a namespace slot.

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
| 3 | **Translate checkpoint** — write `skill_input.json`, wait (skipped if no algorithm) | resumes on re-run |
| 4 | Assembly — resolved scenarios, cluster YAMLs, PipelineRuns | on re-run |
| 5 | Summary — write `run_summary.md` | on re-run |
| 6 | **Gate** — `[d]eploy / [e]dit / [q]uit` | on re-run |

**Phase 1 ref validation**: If `component.ref` is set in the manifest, Phase 1 resolves it via `git rev-parse` in the component submodule and compares against HEAD. A mismatch produces a warning (not a hard error) with the expected/actual SHA and a checkout command. Missing submodule or non-git directory with `component.ref` set is a hard error with an init command.

**Phase 3 checkpoint**: writes `skill_input.json` and exits cleanly (exit 0). Run `/sim2real-translate` in Claude Code, then re-run `prepare.py` to continue from Phase 4. When no `algorithm` is present in the manifest, Phase 3 is skipped entirely (baseline-only mode).

**Phase 4 assembly** reads `baseline.yaml` and `treatment.yaml` from the experiment root, merges them with skill-generated overlay files (`generated/baseline_config.yaml`, `generated/treatment_config.yaml`), and writes resolved scenario files to `cluster/`. PipelineRuns are generated with a `scenarioContent` param containing the fully resolved scenario YAML. Workloads are loaded from the manifest.

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

Ensures all scenario images exist and orchestrates PipelineRun execution across namespace slots. Operates independently of `transfer.yaml` — driven by workspace files and `setup_config.json`.

```bash
python pipeline/deploy.py {build|run|status|collect|stop|reset|wipe|pairs} [flags]
```

Common flags (all subcommands):

| Flag | Default | Notes |
|------|---------|-------|
| `--run NAME` | from `setup_config.json` | override active run |
| `--experiment-root PATH` | cwd | path to experiment repo |
| `--skip-build` | false | skip image build pre-flight |

**Image build** — `deploy.py build` (called implicitly as pre-flight by `deploy.py run`) iterates over all resolved scenarios in `cluster/`, collects unique `images.inferenceScheduler` refs, and builds any that are stale. Baseline images are tagged by the component directory's HEAD SHA (8 chars); treatment images by the run name. Source hash comparison skips builds when the image is already current.

**Pair discovery** — `deploy.py run` discovers `pipelinerun-*.yaml` files at the `cluster/` root. Each file's pair key is derived as `wl-` + filename stem minus the `pipelinerun-` prefix.

**Collection phases** — `deploy.py collect` derives valid phases dynamically from progress data (packages with status `done`). Falls back to `[baseline, treatment]` when no progress exists. Use `--package` to filter, or `--package experiment` to collect all known phases.

**`--skip-build`** — skips the image build; use when resubmitting after a failed PipelineRun without changing the scorer.

**Subcommands:**

```bash
python pipeline/deploy.py build   [flags]   # ensure all scenario images exist (pre-flight for run)
python pipeline/deploy.py run     [flags]   # ensure images + orchestrate parallel pool execution
python pipeline/deploy.py status            # show progress snapshot of all (workload, package) pairs
python pipeline/deploy.py collect [flags]     # pull results from the cluster PVC
python pipeline/deploy.py stop               # stop the remote orchestrator Job
python pipeline/deploy.py reset [flags]     # reset all non-pending pairs to pending (with cluster cleanup)
python pipeline/deploy.py wipe  [flags]     # delete local result files for pairs in scope
python pipeline/deploy.py pairs   [flags]   # list available pair keys, workloads, and packages
```

**`deploy.py run`** — assigns `(workload, package)` pairs to free namespace slots, polls for completion, and retries pairs that time out. Reads progress from the run-scoped `sim2real-progress-{run}` ConfigMap to resume interrupted runs. Requires a configured namespace. Use `deploy.py collect` to pull results off-cluster after runs complete.

| Flag | Default | Description |
|------|---------|-------------|
| `--remote` | — | Submit orchestrator as in-cluster Job instead of running locally |
| `--only PAIR…` | — | Scope execution to specific pair keys (comma or space-separated, `wl-` prefix optional) |
| `--workload NAME…` | — | Scope execution to pairs matching these workloads (comma or space-separated) |
| `--package NAME…` | — | Scope execution to pairs matching these packages (comma or space-separated) |
| `--status STATE` | — | Scope execution to pairs with this status (e.g. `failed`, `timed-out`) |
| `--skip-teardown` | — | Skip the Tekton teardown task, leaving namespace resources intact for debugging |
| `--preserve-pipelineruns` | — | Do not delete PipelineRun objects after completion (keeps TaskRun logs for debugging) |
| `--force` | — | Reset non-pending pairs to `pending`, cleaning cluster resources (PipelineRuns + Helm) for pairs with assigned namespaces |
| `--max-retries N` | 2 | Max retries for timed-out pairs |
| `--poll-interval N` | 30 | Seconds between status polls |
| `--gpu-resource-type` | auto-derived | Override GPU resource name (derived from scenario's `accelerator.resource`, else `nvidia.com/gpu`) |
| `--default-gpu-cost N` | 1 | Fallback GPU cost per pair when not derivable from scenario |
| `--pending-threshold N` | 600 | Seconds a pod may remain Pending (recoverable reason) before early reclaim |
| `--max-pending-stalls N` | 10 | Max early reclaims before marking pair `stalled` |
| `--max-backoff N` | 600 | Maximum backoff interval in seconds during GPU scarcity |

**Early reclaim** — on each poll cycle, pods in `Running`/`Started` PipelineRuns are checked for scheduling failures. Recoverable reasons (e.g. `Insufficient nvidia.com/gpu`) trigger early reclaim after `--pending-threshold` seconds. Non-recoverable reasons (e.g. node affinity mismatch, PVC not found) fail the pair immediately. Each early reclaim increments `pending_stalls`; at `--max-pending-stalls` the pair transitions to `stalled` (terminal).

**Backoff controller** — when the capacity probe shows `free_gpus < min(pending workload GPU costs)`, the orchestrator enters exponential backoff: poll interval doubles each cycle (capped at `--max-backoff`), and dispatch is skipped until capacity returns. Backoff is also triggered when 3 early reclaims (recoverable only) occur within 10 minutes. The controller resets to normal when a pod successfully schedules or the probe shows sufficient capacity for the largest pending workload. Already-running slots continue to be monitored during backoff. Orchestrator state is persisted in the progress store under the `_orchestrator` metadata key.

**Pair statuses:** `pending` → `running` → `done`. Failure paths: `running` → `failed` (hard failure or non-recoverable pending), `running` → `timed-out` (4h timeout exceeded), `running` → `pending` (recoverable early reclaim, repeats up to `--max-pending-stalls` times) → `stalled`.

**Auto-cleanup** — when a PipelineRun succeeds, the orchestrator deletes the PipelineRun CR from the cluster. Failed PipelineRuns are left in place for debugging (`kubectl describe`, pod logs). Use `reset` to remove them when done. Note: `--skip-teardown` only suppresses the Tekton `llmdbenchmark-teardown` task (Helm-level resource cleanup); PipelineRun CR deletion by the orchestrator is unaffected. Use `--preserve-pipelineruns` to suppress PipelineRun CR deletion on success — useful for debugging steps that fail silently (e.g., `set +e` scripts that exit 0 despite internal errors).

**Remote mode** — `deploy.py run --remote` submits the orchestrator as a Kubernetes Job (`sim2real-orchestrator`) instead of running locally. The launcher builds the EPP image locally, packs workspace files into a ConfigMap, applies the Job, and waits for the pod to reach Running. Use `stop` to cancel, `status` to check progress, and `collect` to pull results after completion. Requires `orchestrator_image` in `setup_config.json`.

**`deploy.py status`** — prints the current state of all pairs. Reads from the run-scoped `sim2real-progress-{run}` ConfigMap. Requires a configured namespace.

| Flag | Description |
|------|-------------|
| `--only PAIR…` | Scope to specific pair keys (comma or space-separated, `wl-` prefix optional) |
| `--workload NAME…` | Filter by workload names (comma or space-separated) |
| `--package NAME…` | Filter by package names (comma or space-separated) |
| `--status STATE` | Filter by status (e.g. `running`, `done`, `failed`) |

**`deploy.py collect`** — extracts results from the cluster PVC and writes to `workspace/runs/<run>/results/{phase}/<workload>/`.

| Flag | Description |
|------|-------------|
| `--only PAIR…` | Scope to specific pair keys — narrows both workload and package (comma or space-separated, `wl-` prefix optional; takes precedence over `--workload`) |
| `--workload NAME…` | Scope to pairs matching these workloads (comma or space-separated) |
| `--package NAME…` | Collect only these packages (comma or space-separated, phase-level filter) |
| `--skip-logs` | Skip vLLM and EPP log files, collect only traces |

When `--only` or `--workload` is given, only matching workload subdirectories are pulled from the PVC (instead of entire phase directories). Multiple values within a flag use OR (union): `--workload X Y` matches pairs for workload X or Y. Different flags compose as AND: `--workload X Y --package baseline` pulls workloads X and Y from the baseline phase only. Requires progress data to resolve pairs.

**`deploy.py stop`** — deletes the `sim2real-orchestrator` Kubernetes Job (with cascading pod deletion) in the primary namespace. Only meaningful when the orchestrator runs as an in-cluster Job. Pair state is left as-is. If no remote orchestrator Job exists, prints a message and returns. Use `reset` separately to clear failed/stalled pair state.

**`deploy.py reset`** — resets all non-pending pairs to `pending` and removes their cluster resources (PipelineRuns, Helm releases). This includes `done` pairs — use `--preserve-done-status` to clean up cluster resources for done pairs without re-queuing them.

| Flag | Description |
|------|-------------|
| `--only PAIR…` | Scope reset to specific pair keys (comma or space-separated, `wl-` prefix optional) |
| `--workload NAME…` | Scope reset to pairs matching these workloads (comma or space-separated) |
| `--package NAME…` | Scope reset to pairs matching these packages (comma or space-separated) |
| `--status STATE` | Scope reset to pairs with this status |
| `--preserve-done-status` | Keep done pairs' status unchanged (cluster cleanup only) |
| `--dry-run` | Print what would be reset without acting |

**Safety:** Results in `workspace/runs/<run>/results/` are preserved — only cluster resources and ConfigMap status are affected.

**`deploy.py wipe`** — deletes local result files (`results/<package>/<workload>/`) for all pairs in scope. Does **not** modify pair status in the ConfigMap. Pairs with no results on disk are skipped. Empty package directories are cleaned up automatically.

| Flag | Description |
|------|-------------|
| `--only PAIR…` | Scope wipe to specific pair keys (comma or space-separated, `wl-` prefix optional) |
| `--workload NAME…` | Scope wipe to pairs matching these workloads (comma or space-separated) |
| `--package NAME…` | Scope wipe to pairs matching these packages (comma or space-separated) |
| `--dry-run` | Print what would be wiped without acting |
| `--yes` / `-y` | Skip confirmation prompt |

**Re-running wiped pairs:** `wipe` only removes files. To re-dispatch wiped pairs, use `reset` to move them back to `pending`.

**`deploy.py pairs`** — lists available pair keys, workloads, and packages by scanning `cluster/pipelinerun-*.yaml`.

| Flag | Description |
|------|-------------|
| `--keys-only` | Print pair keys only (one per line, for scripting) |
| `--workloads-only` | Print distinct workload names only (one per line) |
| `--packages-only` | Print distinct package names only (one per line) |

Flags are mutually exclusive. Default (no flag) prints a human-readable table with PAIR, WORKLOAD, and PACKAGE columns.

---

## monitor.py

Watches active namespace slots while `deploy.py run` is running. Detects pod failures,
auto-remediates transient issues (tier 1), emits rules-based suggestions (tier 2), and
calls the Anthropic API for novel failures (tier 3). Writes all findings to
`workspace/runs/<run>/health_report.md`.

```bash
# Start in a second terminal alongside deploy.py run
python pipeline/monitor.py --experiment-root ../admission-control

# Or background it
python pipeline/monitor.py --experiment-root ../admission-control &
```

**Requires:** `ANTHROPIC_API_KEY` in the environment for tier-3 API diagnosis.
If unset, tier-3 findings are written with a placeholder and no API call is made.

| Flag | Default | Description |
|------|---------|-------------|
| `--experiment-root PATH` | cwd | Root of the experiment repo |
| `--run NAME` | `current_run` from setup_config.json | Run name |
| `--interval SECONDS` | 30 | Poll interval |
| `--log-lines N` | 200 | Tail depth for pod logs sent to API |

---

## run.py

Manage and switch between runs.

```bash
python pipeline/run.py --experiment-root ../admission-control list
python pipeline/run.py --experiment-root ../admission-control inspect <name>
python pipeline/run.py --experiment-root ../admission-control switch <name>
```

`--experiment-root` defaults to the current working directory; omit it when running from the experiment repo root.

**`switch`** copies files listed in `translation_output.json` (`files_created` + `files_modified`) into the experiment repo's `llm-d-inference-scheduler/` directory and updates `setup_config.json`. Prompts before overwriting uncommitted changes.

---

## <experiment-repo>/transfer.yaml

Manifest consumed by `prepare.py`. Version 3 required.

```yaml
kind: sim2real-transfer
version: 3
scenario: <name>            # scenario name used in generated PipelineRun labels

baselines:                  # required — list of baseline specs
  - name: <pkg-name>       # unique package name (lowercase alphanumeric, 1-20 chars)
    scenario: <path>        # baseline scenario YAML (null if none)
    sim:
      config: <path>        # baseline policy for sim
    real:
      config: <path>        # optional: baseline EPP config template
      notes: |              # optional: notes embedded in skill_input.json

algorithms:                 # optional — omit for baseline-only benchmarks
  - name: <pkg-name>       # unique package name
    source: <path>          # sim algorithm implementation
    defaults: <baseline>    # name of baseline this algorithm inherits from

workloads:
  - <path>                  # one or more workload YAMLs

context:
  text: |                   # freeform instructions for translation skill
  files: [<path>, ...]      # files assembled into context document (Phase 2)

# v3 fields (required unless noted)
target:
  repo: <path>              # llm-d-inference-scheduler repo path
config:
  kind: <string>            # config kind (e.g. "gaie")
build:                      # optional — defaults applied if absent
  commands: []              # EPP build commands
epp_image:                  # optional
  upstream:
    hub: <registry>
    name: <repo>
    tag: <tag>
  build:                    # override for built EPP image coordinates
    hub: <registry>
    name: <repo>
    tag: <tag>
pipeline:                   # optional — defaults applied if absent
  name: sim2real            # Pipeline resource name referenced in PipelineRuns (default: "sim2real")
  yaml: pipeline/pipeline.yaml  # path relative to repo root (default: "pipeline/pipeline.yaml")
```

All paths are relative to the repo root and validated at Phase 1.

`component.ref` (optional): tag, branch, or commit SHA identifying the expected version of the component submodule. Phase 1 warns on mismatch (see above).

---

## Parallel Pool Execution

`setup.py --namespaces NS1,NS2,...` provisions N namespace slots, each bootstrapped identically. `prepare.py` generates one shared Tekton Pipeline plus one PipelineRun per `(workload, package)` pair. `deploy.py run` orchestrates execution by assigning pairs to free slots, polling for completion, and retrying on timeout. Use `deploy.py collect` to pull results off-cluster. `deploy.py status` reads progress from the ConfigMap.

| Artifact | Written by | Read by |
|----------|-----------|---------|
| ConfigMap `sim2real-progress-{run}` | `deploy.py run`, `deploy.py reset` | All subcommands |

All subcommands (`status`, `collect`, `run`, `reset`, `wipe`) use a run-scoped `sim2real-progress-{run}` ConfigMap as the sole progress store. Each run gets its own ConfigMap, avoiding cross-run conflicts. A configured namespace is required.

---

## Scenario Overlay Format

The `/sim2real-translate` skill produces two overlay files in `workspace/runs/<run>/generated/`:

- `baseline_config.yaml` — overlay merged onto `baseline.yaml`
- `treatment_config.yaml` — overlay merged onto the already-resolved baseline

### Assembly formula

```python
baseline_resolved = deep_merge(baseline_bundle, baseline_overlay)
treatment_resolved = deep_merge(deep_merge(baseline_resolved, treatment_diffs), treatment_overlay)
```

Where `baseline_bundle` is the experiment's `baseline.yaml`, `treatment_diffs` is the experiment's optional `treatment.yaml`, and the overlays are the skill outputs.

### Deep merge semantics

- Dict keys merge recursively (overlay overrides base)
- Lists of dicts with a common `name` field merge by name
- Lists of dicts without a common key merge positionally
- Lists of scalars are replaced entirely
- Treatment overlay only needs the delta from baseline_resolved (shared config propagates automatically)

### Required structure

Both overlays are llmdbenchmark scenario overlays. They must be valid YAML with a top-level `scenario:` list containing a single dict:

```yaml
scenario:
  - name: "<scenario-name>"

    # Fields to add or override (only include what you're changing)
    extraObjects: [...]
    inferenceExtension: {...}
    images: {...}
```

### InferenceObjective requirements

InferenceObjectives go in `extraObjects`. Each must include `spec.poolRef.name` referencing the InferencePool created by the gaie Helm chart:

```yaml
extraObjects:
  - apiVersion: inference.networking.x-k8s.io/v1alpha2
    kind: InferenceObjective
    metadata:
      name: critical
    spec:
      poolRef:
        name: ${model.idLabel}-gaie
      priority: 100
```

`${model.idLabel}` is resolved by llm-d-benchmark at render time (requires llm-d-benchmark >= PR #1103).

### Plugin config

The EPP plugin configuration goes inside `inferenceExtension.pluginsCustomConfig` as a YAML-in-YAML string:

```yaml
inferenceExtension:
  pluginsConfigFile: custom-plugins.yaml
  pluginsCustomConfig:
    custom-plugins.yaml: |
      apiVersion: inference.networking.x-k8s.io/v1alpha1
      kind: EndpointPickerConfig
      plugins:
      - type: my-plugin
        name: my-plugin
        parameters:
          threshold: 5
      schedulingProfiles:
      - name: default
        plugins:
        - pluginRef: my-plugin
```

### Typical overlay content

**Baseline overlay** — adds InferenceObjectives and the baseline EPP plugin config:
- `extraObjects` (InferenceObjectives with `poolRef`)
- `inferenceExtension.pluginsCustomConfig` (baseline scorer config)

**Treatment overlay** — only the delta from baseline:
- `inferenceExtension.pluginsCustomConfig` (evolved scorer config)
- `images.inferenceScheduler` (custom EPP image — injected by `prepare.py`, not the skill)

If treatment uses the same InferenceObjectives as baseline, do NOT repeat them — they propagate from `baseline_resolved`.

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
