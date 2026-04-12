# Pipeline Redesign — Brainstorm (Revised)

**Date:** 2026-04-09
**Constraints from user:**
- Audience: small team (2-3 engineers)
- Generality: generic framework for arbitrary algorithm types
- LLM role: current scope is right (generation, review, config check) — the problem is execution quality
- Execution: hybrid — scripts for mechanical parts, interactive skill for LLM-heavy translation

**Feedback incorporated (round 2):**
- Setup: optional credential verification (test image push)
- Context: natural language hints in transfer.yaml, not structured file lists in env_defaults
- Equivalence gate: removed — too high a bar for users right now
- Treatment config: LLM-generated alongside Go code during translation, not from templates
- Baseline config: user-provided or scenario defaults
- Reviewer: sees context.md + all artifacts (code + config)
- Deploy: fire-and-forget, apply PipelineRuns then exit. No waiting, no PR, no parallel options.
- Add `prepare.py assemble` subcommand to reproduce YAMLs
- Add `prepare.py validate-assembly` subcommand
- Clean up unused prompts and scripts

---

## Core Insight

The current pipeline conflates two fundamentally different kinds of work:

1. **Mechanical** — reading configs, hashing files, merging YAML, compiling Tekton resources, running `go build`
2. **Creative** — translating a simulated algorithm into production code AND its config, reviewing fidelity

These should have different execution models. Mechanical work belongs in deterministic Python scripts. Creative work belongs in an interactive session where the operator can converse, redirect, and make judgment calls.

The current `prepare.py` tries to do both by shelling out to `claude -p` in subprocess loops. This is the root cause of most instability.

---

## Proposed Pipeline

```
scripts/setup.py          one-time cluster bootstrap
         │                  - optional: --test-push to verify registry credentials
         ▼
scripts/prepare.py        mechanical: context cache, post-translation assembly
         │
         ├── phases 1-2: load manifest, build context cache
         │
         ├── phase 3: CHECKPOINT — "run /sim2real-prepare now"
         │                          ↕
         │              /sim2real-prepare skill (interactive translation + review)
         │              - writes Go code + treatment config + translation_output.json
         │              - multi-model review of ALL artifacts
         │                          ↕
         ├── phases 4-5: assembly, summary, human review
         │
         ▼
scripts/deploy.py         fire-and-forget: EPP build, apply PipelineRuns, exit
         │
         ▼
scripts/analyze.py        pull results when ready, comparison tables, charts
```

### The user experience

```bash
# One-time setup (with optional credential test)
$ python scripts/setup.py --namespace sim2real-jchen --registry quay.io/jchen --test-push

# Edit manifest — just the algorithm-specific parts
$ vim config/transfer.yaml

# First run of prepare — builds context, hits translation checkpoint
$ python scripts/prepare.py

━━━ [1/6] Init ━━━
  Manifest: config/transfer.yaml (v2, routing, 2 workloads)
  Run: sim2real-2026-04-09

━━━ [2/6] Context ━━━
  Hash: a1b2c3 (inference-sim@d4f1a2c, llm-d@e9b3f1a)
  Cache: MISS → assembling workspace/context/routing/a1b2c3.md

━━━ [3/6] Translation ━━━
  No translation_output.json found.

  Run the interactive translation:
    /sim2real-prepare

  Then re-run: python scripts/prepare.py

# Run the interactive skill
$ claude
> /sim2real-prepare
[... interactive: writer produces Go code + treatment config ...]
[... build/test gate after each revision ...]
[... multi-model reviewers check code + config + fidelity ...]
[Translation complete: adaptive-v2-scorer, 3 rounds, 2/3 consensus]

# Resume prepare
$ python scripts/prepare.py

━━━ [1/6] Init ━━━ ✓
━━━ [2/6] Context ━━━ ✓ (cached)
━━━ [3/6] Translation ━━━ ✓ (adaptive-v2-scorer, 3 rounds, 2/3 consensus)

━━━ [4/6] Assembly ━━━
  Treatment config: from translation (adaptive-v2-scorer)
  Baseline config: from scenario defaults
  Values merge: OK → workspace/runs/.../values.yaml
  Cluster YAMLs: compiled (2 files)
  Config check: PASS (plugin type consistent across code + EPP config)

━━━ [5/6] Summary ━━━
  Written: workspace/runs/.../run_summary.md

━━━ [6/6] Review Gate ━━━
  [... displays run_summary.md ...]
  [d]eploy / [e]dit / [q]uit: d
  ✓ READY TO DEPLOY

# Deploy — fire and forget
$ python scripts/deploy.py

━━━ [1/3] Pre-flight ━━━
  Run: sim2real-2026-04-09 (READY TO DEPLOY ✓)

━━━ [2/3] Build EPP ━━━
  Building treatment EPP image...
  Pushed: quay.io/jchen/llm-d-inference-scheduler:sim2real-2026-04-09

━━━ [3/3] Submit Experiments ━━━
  Applied: pipelinerun/sim2real-baseline-1744200000
  Applied: pipelinerun/sim2real-treatment-1744200001

  Experiments submitted to cluster. Check status:
    tkn pipelinerun list -n sim2real-jchen
    tkn pipelinerun logs sim2real-baseline-1744200000 -n sim2real-jchen -f

# Later, when experiments finish
$ python scripts/analyze.py

━━━ Pulling results ━━━
  Baseline: 2 workloads collected
  Treatment: 2 workloads collected

━━━ Comparison ━━━
  [... comparison table, charts, verdict ...]
```

---

## 1. `scripts/setup.py` — Cluster Bootstrap

One-time, idempotent. **Interactive by default** — if arguments are omitted, the CLI prompts for each value. If `setup_config.json` already exists, prompts show the current value and let the user accept (Enter) or override.

### Interactive mode (no args)

```
$ python scripts/setup.py

━━━ sim2real setup ━━━

━━━ [1/7] Configuration ━━━
  Namespace [sim2real-jchen]: ↵               # Enter = reuse existing
  Registry [quay.io/jchen]: quay.io/newuser   # type to override
  Repo name [llm-d-inference-scheduler]: ↵
  Run name [sim2real-2026-04-09]: ↵
  Container runtime (podman/docker) [podman]: ↵
  HuggingFace token [hf_***]: ↵              # masked, Enter = reuse
  Registry username [jchen+robot]: ↵
  Registry token [***]: ↵

━━━ [2/7] Namespace ━━━
  Creating namespace sim2real-jchen... ✓ (already exists)

━━━ [3/7] Secrets ━━━
  hf-secret: ✓ (already exists, updated)
  registry-secret: ✓ (created)

━━━ [4/7] Registry test push ━━━
  Push test image to quay.io/newuser/llm-d-inference-scheduler:_test-push?
  [y]es / [s]kip: y
  Pushing... ✓ Registry credentials verified (push + pull)

━━━ [5/7] PVCs ━━━ ✓
━━━ [6/7] Tekton tasks ━━━ ✓
━━━ [7/7] Config ━━━
  Written: workspace/setup_config.json
  Written: workspace/runs/sim2real-2026-04-09/run_metadata.json
```

### Scripted mode (all args provided)

All values can also be passed as flags for non-interactive use:
```bash
python scripts/setup.py \
  --namespace sim2real-jchen \
  --registry quay.io/jchen \
  --hf-token hf_xxx \
  --registry-user jchen+robot \
  --registry-token xxx \
  --test-push
```

### Reuse and override behavior

- If `workspace/setup_config.json` exists, all prompts show current values as defaults
- Pressing Enter reuses the existing value; typing a new value overrides it
- Secrets (HF token, registry credentials) are prompted but masked; existing secrets are reused unless overridden
- `--test-push` flag or interactive `[y/s]` prompt for registry credential verification using the actual EPP image name and path

**Run name generation:** setup.py generates the run name as `sim2real-YYYY-MM-DD` (or from `--run NAME` / interactive prompt). It writes this to `setup_config.json` as `current_run`. prepare.py and deploy.py read it from there; `--run` flag overrides.

**Other changes:**
- Write `epp_image.build` into `common` section of restructured env_defaults
- `run_metadata.json` schema with `version: 1` field for forward compatibility

---

## 2. `config/transfer.yaml` — User-Facing Manifest

Slim. Only things that change per algorithm transfer. Natural language context hints.

```yaml
kind: sim2real-transfer
version: 2

scenario: routing   # selects env_defaults section

algorithm:
  source: sim2real_golden/routers/router_adaptive_v2.go
  config: sim2real_golden/routers/policy_adaptive_v2.yaml

baseline:
  config: sim2real_golden/routers/policy_baseline_211.yaml

workloads:
  - sim2real_golden/workloads/workload_fm2a_groups_gt_instances.yaml
  - sim2real_golden/workloads/workload_fm3_burst.yaml

llm_config: sim2real_golden/llm_config.yaml

context:
  files:
    - docs/transfer/blis_to_llmd_mapping.md
  notes: |
    Adaptive-v2 uses per-request regime detection:
      cache-affinity (cacheSpread > 0.1): weight PPC highest
      memory-aware (avgKVUtil > 0.7): weight load-aware + kv-util
      load-balance (default): load-aware only

    The production scorer should implement the Scorer interface.
    Use load_aware.go as a reference for how scorers work.
    The plugin must be registered in register.go.

    Prefer multi-profile EndpointPickerConfig with a custom
    ProfileHandler over modifying the core scoring loop.
```

**What the user fills in:**
- `scenario` — one word, selects the right defaults
- `algorithm.*` — paths to their simulated algorithm files
- `baseline.config` — path to baseline policy
- `workloads` — paths to workload files
- `llm_config` — model/GPU config
- `context.files` — any docs to include in context (mapping doc, notes, etc.)
- `context.notes` — free-form natural language: translation hints, design preferences, signal mapping notes, architecture guidance. The LLM reads this and uses its judgment.

**What is NOT in this file:** production target repo, build commands, helm paths, config templates, cluster config. All of that lives in `env_defaults.yaml` per scenario.

### v2 Schema Definition

```yaml
# Required fields
kind: sim2real-transfer        # must be exactly "sim2real-transfer"
version: 2                     # integer; must be 2
scenario: <string>             # must match a key in env_defaults.yaml scenarios section

algorithm:
  source: <path>               # path to simulated algorithm source (relative to repo root)
  config: <path>               # path to algorithm policy/config YAML (relative to repo root)

baseline:
  config: <path>               # path to baseline policy/config YAML (relative to repo root)

workloads:                     # list of 1+ paths
  - <path>                     # workload YAML (relative to repo root)

llm_config: <path>             # model/GPU/memory config YAML (relative to repo root)

# Optional fields
context:
  files: [<path>, ...]         # additional files to include in context (default: [])
  notes: <string>              # free-form natural language hints (default: "")
```

All `<path>` fields are relative to the repository root.

**Version detection:** `prepare.py` checks the `version` field. If `version: 1` (or missing), it exits with:
```
Error: transfer.yaml is v1 format. v2 is required.
See docs/transfer/migration-v1-to-v2.md for migration instructions.
```

**Breaking changes from v1:**
| v1 field | v2 equivalent |
|----------|---------------|
| `algorithm.experiment_dir` | Removed — all paths are repo-root-relative |
| `algorithm.policy` | Renamed to `algorithm.config` |
| `algorithm.baseline` | Moved to `baseline.config` |
| `target.*` | Moved to `env_defaults.yaml` scenarios section |
| `context.mapping` | Now just an entry in `context.files` |
| `context.template` | Removed — LLM reads from codebase |
| `context.examples` | Removed — user describes in `context.notes` |
| `config.*` | Moved to `env_defaults.yaml` scenarios section |
| `validation.*` | Removed |
| `artifacts.*` | Removed |

---

## 3. `config/env_defaults.yaml` — Per-Scenario Defaults

Restructured: `common` base + per-scenario overlays.

```yaml
common:
  observe:
    request_multiplier: 10
  build:
    commands:
      - ["go", "build", "./..."]
      - ["go", "vet", "./..."]
    # test_scope is appended to: go test -timeout 10m -v <test_scope>
    # Scenarios override this to narrow the test scope to the relevant package.
    # Default runs all tests in the repo.
    test_scope: "./..."
  stack:
    model:
      vllm_image: ""
    gaie:
      epp_image:
        upstream: { hub: ghcr.io/llm-d, name: llm-d-inference-scheduler, tag: latest }
        build: { hub: "", name: "", tag: "" }   # written by setup.py

scenarios:
  routing:
    target:
      repo: llm-d-inference-scheduler
      plugin_dir: pkg/plugins/scorer/
      register_file: pkg/plugins/register.go
      package: scorer
    build:
      test_scope: "./pkg/plugins/scorer/..."    # narrows test to scorer package
    config:
      kind: EndpointPickerConfig
      helm_path: gaie.treatment.helmValues.inferenceExtension.pluginsCustomConfig.custom-plugins.yaml
    gaie:
      shared:
        helmValues:
          inferenceExtension:
            gatewayType: istio
            connectionPool:
              maxRequestsPerConnection: 256000
      baseline:
        helmValues:
          inferenceExtension:
            pluginsCustomConfig:
              custom-plugins.yaml: |
                apiVersion: inference.networking.x-k8s.io/v1alpha1
                kind: EndpointPickerConfig
                plugins:
                - type: load-aware-scorer
                - type: decode-filter
                - type: max-score-picker
                - type: single-profile-handler
                schedulingProfiles:
                - name: default
                  plugins:
                  - pluginRef: decode-filter
                  - pluginRef: max-score-picker
                  - pluginRef: load-aware-scorer
                    weight: 1

  admission_control:
    target:
      repo: llm-d-inference-scheduler
      plugin_dir: pkg/plugins/admission/
      register_file: pkg/plugins/register.go
      package: admission
    build:
      test_scope: "./pkg/plugins/admission/..."  # narrows test to admission package
    config:
      kind: AdmissionPolicyConfig
      helm_path: gaie.treatment.admissionPolicy
    gaie:
      inferenceObjectives:
        - { name: critical, priority: 100 }
        - { name: standard, priority: 0 }
        - { name: sheddable, priority: -10 }
        - { name: batch, priority: -50 }
      baseline:
        helmValues: {}   # always-admit is the default, no special config
```

**Build command resolution:** After merging `common` + `scenarios.<name>`, the effective build commands are assembled as:
1. `common.build.commands` (go build, go vet) — always present
2. Plus: `["go", "test", "-timeout", "10m", "-v", <resolved test_scope>]` — scenario's `test_scope` overrides common's `./...`

This way `go build` and `go vet` are never duplicated across scenarios. Only the test scope varies.

**What scenario sections contain:**
- `target` — production repo, plugin directory, register file, package name
- `build.test_scope` — override for which packages to test (default: `./...`)
- `config.kind` — the K8s config type (EndpointPickerConfig, AdmissionPolicyConfig)
- `config.helm_path` — where treatment config goes in values.yaml
- `gaie` — cluster config: shared helm values, baseline config, inference objectives

**What is NOT here:** treatment config templates (LLM generates the treatment config), context file lists (user writes natural language in transfer.yaml), equivalence commands (removed).

**Adding a new algorithm type** = add a section under `scenarios`. Fill in target, build commands, config kind, baseline defaults. Done.

### merge-values update

`merge-values` gets a `--scenario` flag. The merge is a 3-step process:

**Step 1 — Resolve scenario config:**
```
env_defaults.yaml  →  deep-merge common + scenarios.routing  →  resolved
```

**Step 2 — Strip pipeline-only keys** (these drive prepare.py, not cluster YAML):
```yaml
# Before stripping (resolved scenario config):
target:          { repo: ..., plugin_dir: ..., ... }   # ← stripped
build:           { commands: [...], ... }                # ← stripped
config:          { kind: ..., helm_path: ... }           # ← stripped
observe:         { request_multiplier: 10 }              # ← kept (cluster config)
stack:           { model: ..., gaie: ... }               # ← kept (cluster config)

# After stripping:
observe:         { request_multiplier: 10 }
stack:           { model: ..., gaie: ... }
```

**Step 3 — Deep-merge** resolved cluster config + algorithm_values.yaml (model, workloads, treatment config) → `values.yaml`. Same merge semantics as today (lists of dicts merged by named key, scalars replaced).

**Removed:** `pipeline.fast_iteration` is removed from v2. The pipeline always runs full validation. The old fast-iteration shortcut (skip noise gate, skip PR) is no longer needed since noise and PR are already out of scope.

---

## 4. `scripts/prepare.py` — State Machine

Deterministic Python script. Advances through phases, each idempotent. Re-running skips completed phases (from `.state.json`). Translation phase is a checkpoint.

### Phase 1: Init

- Load and validate `config/transfer.yaml` (v2 schema)
- Load `workspace/setup_config.json` → `current_run`, `namespace`, `registry`
- Load `env_defaults.yaml` → merge `common` + `scenarios.<manifest.scenario>` → resolved scenario config
- Resolve run directory: `workspace/runs/<current_run>/`
- Validate: algorithm source files exist, submodules initialized, scenario section exists
- Write initial `.state.json`

### Phase 2: Context

Build a context document for the translation skill. Cached by content hash.

**Cache key:** SHA-256 of:
- File contents of each `manifest.context.files` entry (read from disk; error if any file doesn't exist)
- Git commit SHA of `inference-sim` submodule HEAD (via `git -C inference-sim rev-parse HEAD`)
- Git commit SHA of `llm-d-inference-scheduler` submodule HEAD (via `git -C llm-d-inference-scheduler rev-parse HEAD`)

**Not in the hash:** `context.notes`. Notes are NOT baked into the cached context.md. They are written separately into `skill_input.json` (see Phase 3) and the skill appends them at runtime. Changing notes doesn't invalidate the cache.

**Cache location:** `workspace/context/<scenario>/<hash>.md`

**Cache eviction:** None — old cache entries are never auto-deleted. The user can manually clean `workspace/context/` if disk space is a concern. In practice each entry is a single .md file of a few hundred KB.

**Assembly is mechanical** — no LLM. Concatenation of `context.files` with section headers:

```markdown
# Translation Context
Scenario: routing | inference-sim@d4f1a2c | llm-d@e9b3f1a

## docs/transfer/blis_to_llmd_mapping.md
[file contents]

## [any other context.files entries, each with its path as header]
[file contents]
```

The cached context.md contains **only the explicit files** — no notes, no auto-discovered interfaces. It's intentionally minimal. The translation skill runs in a Claude Code session with **full file access** to the production codebase. The user's natural language hints (in `context.notes`) guide the translator to read whatever additional files it needs (interfaces, examples, registration patterns). context.md provides the stable reference material; notes provide the per-run guidance.

All paths in `context.files` are relative to the repository root.

**Flags:**
- `--rebuild-context` — ignore cache, force reassembly

### Phase 3: Translation Checkpoint

Not an execution phase — a gate check with a handoff mechanism.

**Skill input handoff:** Before checking for translation output, prepare.py writes `workspace/runs/<name>/skill_input.json` with all paths and config the skill needs:

```json
{
  "run_name": "sim2real-2026-04-09",
  "run_dir": "workspace/runs/sim2real-2026-04-09",
  "scenario": "routing",
  "context_path": "workspace/context/routing/a1b2c3.md",
  "context_notes": "Adaptive-v2 uses per-request regime detection...",
  "manifest_path": "config/transfer.yaml",
  "algorithm_source": "sim2real_golden/routers/router_adaptive_v2.go",
  "algorithm_config": "sim2real_golden/routers/policy_adaptive_v2.yaml",
  "target": {
    "repo": "llm-d-inference-scheduler",
    "plugin_dir": "pkg/plugins/scorer/",
    "register_file": "pkg/plugins/register.go",
    "package": "scorer"
  },
  "build_commands": [
    ["go", "build", "./..."],
    ["go", "vet", "./..."],
    ["go", "test", "-timeout", "10m", "./pkg/plugins/scorer/...", "-v"]
  ],
  "config_kind": "EndpointPickerConfig"
}
```

This file is the **single source of truth** for the skill. The skill reads it instead of parsing env_defaults itself. All paths are repo-root-relative.

**Gate check:**
- If `translation_output.json` exists:
  - Validate schema (required fields: `plugin_type`, `files_created`, `files_modified`)
  - **Early kind validation:** if `treatment_config.yaml` exists, check its `kind` field matches `config_kind` from skill_input.json. Catches wrong config type before expensive assembly.
  - Report summary (plugin_type, files, review rounds, consensus), advance
- If not:
  - Write `skill_input.json` (so the skill can find it)
  - Track checkpoint hits in `.state.json` (`translate.checkpoint_hits` counter)
  - Print instructions:
    ```
    Run the interactive translation:
      /sim2real-prepare
    Then re-run: python scripts/prepare.py
    ```
  - If `checkpoint_hits >= 3`: append a warning:
    ```
    ⚠ This is the 3rd time prepare.py has paused here.
      Have you run /sim2real-prepare yet?
    ```
  - Exit cleanly (code 0)

### Phase 4: Assembly

All mechanical after translation. Before starting, optionally warns if the target repo has uncommitted changes (`git -C <target_repo> status --porcelain`), since assembly reads generated code from there.

**4a — Treatment config:**
- Read `treatment_config.yaml` from run directory (written by the skill during translation)
- Validate it exists and parses as valid YAML
- Validate `kind` matches `config.kind` from scenario config (redundant with Phase 3 early check, but defense-in-depth)

**4b — Baseline config:**
The baseline EPP config comes from `env_defaults.yaml`, specifically `scenarios.<scenario>.gaie.baseline.helmValues`. This is the production baseline (e.g., load-aware-scorer for routing, always-admit for admission control). It is NOT the same as `baseline.config` in the manifest — that field points to the *simulation* baseline policy file (used by the translator for context, not for cluster config).

**4c — Algorithm values:**
- Parse `llm_config` from manifest → model config (replicas, GPU, memory, image)
- Parse workload YAMLs → workload specs (with `request_multiplier` applied)
- Generate `workspace/runs/<name>/algorithm_values.yaml`

**4d — Values merge:**
- Run `merge-values --scenario <scenario>` with resolved env_defaults + algorithm values + treatment config
- Output: `workspace/runs/<name>/values.yaml`

**4d — Cluster YAML compilation (organized by package):**

Artifacts are organized into **packages** — `baseline` and `treatment` — so it's easy to see exactly what will be applied for each experiment arm and to resubmit a single package.

```
cluster/
├── baseline/
│   ├── epp.yaml              # EPP config for baseline
│   └── pipelinerun.yaml      # Tekton PipelineRun for baseline
└── treatment/
    ├── epp.yaml              # EPP config for treatment
    └── pipelinerun.yaml      # Tekton PipelineRun for treatment
```

- Extract EPP configs from merged values → write per-package `epp.yaml`
- Run `compile-pipeline` → write per-package `pipelinerun.yaml`

**4f — Config consistency check (`validate-assembly`):**

Deterministic checks, no LLM needed. Can also be run standalone: `python scripts/prepare.py validate-assembly`.

**Standalone mode pre-check:** Before running checks, validate that all required input files exist (`translation_output.json`, `treatment_config.yaml`, `cluster/treatment/epp.yaml`, `values.yaml`). If any are missing, print which files are absent and exit with a clear error — don't attempt partial validation.

**Checks:**
1. `plugin_type` from `translation_output.json` appears in:
   - `register.go` in the target repo (also verify `register.go` has been modified: `git -C <target_repo> diff --name-only` includes the register file)
   - `cluster/treatment/epp.yaml`
   - `values.yaml` at the expected `helm_path` from scenario config
2. `config.kind` from `treatment_config.yaml` matches `config.kind` from scenario config
3. Every file in `translation_output.json` `files_created` exists on disk in the target repo

On failure: halt with clear error showing what's inconsistent and which check failed.

### Phase 5: Summary

Mechanical assembly of `workspace/runs/<name>/run_summary.md`:
- Algorithm info (name, source, description from translation_output.json)
- Translation record (files created/modified, review rounds, consensus)
- Baseline vs. Treatment comparison table (EPP configs side by side)
- vLLM config, workloads table
- Config check result
- Checklist (build passed, tests passed, reviewers approved, config check passed)

### Phase 6: Human Review Gate

- Display `run_summary.md`
- Prompt: `[d]eploy` / `[e]dit` / `[q]uit`
- On deploy: write `READY TO DEPLOY` marker into `run_summary.md`, set `.state.json` gate status to `done` with `verdict: "READY TO DEPLOY"`
- On edit: pause for manual file editing, re-display
- On quit: set `.state.json` gate status to `abandoned`, exit without marking ready

Deploy.py checks `.state.json` gate status (not just the text marker in run_summary.md) to confirm prepare completed all phases through the gate.

### State tracking

Phase keys are lowercase, matching the names below. Status values: `pending`, `done`, `abandoned`.

```json
{
  "run_name": "sim2real-2026-04-09",
  "scenario": "routing",
  "phases": {
    "init":      { "status": "done" },
    "context":   { "status": "done", "hash": "a1b2c3", "cached": true },
    "translate": { "status": "done", "plugin_type": "adaptive-v2-scorer",
                   "review_rounds": 3, "consensus": "2/3",
                   "checkpoint_hits": 2 },
    "assembly":  { "status": "done" },
    "summary":   { "status": "done" },
    "gate":      { "status": "done", "verdict": "READY TO DEPLOY" }
  }
}
```

`translate.checkpoint_hits` tracks how many times prepare.py paused at the translation checkpoint. Reset to 0 when `translation_output.json` is found. Used for the "have you run the skill?" warning.
```

### Subcommands and flags

```bash
# Full pipeline (with translation checkpoint)
python scripts/prepare.py

# Rebuild context cache only
python scripts/prepare.py context

# Reproduce cluster YAMLs from existing translation (re-runs assembly + summary)
# Pre-check: translation_output.json and treatment_config.yaml must exist
python scripts/prepare.py assemble

# Validate assembly consistency (standalone check)
python scripts/prepare.py validate-assembly

# Show current run state
python scripts/prepare.py status

# Flags
--force              Regenerate all phases
--rebuild-context    Force context cache rebuild
--manifest PATH      Path to transfer.yaml (default: config/transfer.yaml)
--run NAME           Override run name
```

---

## 5. `/sim2real-prepare` Skill — Interactive Translation + Review

The only LLM-heavy component. Runs as a Claude Code skill. The operator converses with the writer, can intervene at any point, and makes the final call.

### What the skill produces

The skill writes **three things** during translation. The reviewer reviews all three.

1. **Go code** — production plugin files, written into the submodule (e.g., `llm-d-inference-scheduler/pkg/plugins/scorer/adaptive_v2.go`, modifications to `register.go`)
2. **Treatment config** — the YAML config for the generated plugin (e.g., EndpointPickerConfig), written to `workspace/runs/<name>/treatment_config.yaml`
3. **Translation metadata** — `workspace/runs/<name>/translation_output.json`:

```json
{
  "plugin_type": "adaptive-v2-scorer",
  "description": "Per-request regime detection with cache-affinity, memory-aware, load-balance profiles",
  "files_created": ["pkg/plugins/scorer/adaptive_v2_scorer.go"],
  "files_modified": ["pkg/plugins/register.go"]
}
```

4. **Generated file copies** — copies of all files from `files_created` and `files_modified` saved to `workspace/runs/<name>/generated/`. These are snapshots of the final accepted versions, taken after the review loop completes. They give operators a single place to inspect exactly what was deployed without digging into the submodule working tree.

Plus snapshots after each build-passing revision (see Snapshot Management below).

### Input

The skill reads **`workspace/runs/<name>/skill_input.json`** (written by prepare.py Phase 3) as its entry point. This file contains all explicit paths and resolved config — the skill doesn't need to parse env_defaults or resolve scenarios itself.

From `skill_input.json` the skill gets:
- `context_path` — path to cached context.md (mapping doc + explicit context files)
- `context_notes` — natural language hints (appended in-memory to context, not part of the cached file)
- `algorithm_source`, `algorithm_config` — paths to simulated algorithm files
- `target.*` — production repo, plugin directory, register file, package name
- `build_commands` — deterministic build/test commands
- `config_kind` — expected config type (EndpointPickerConfig, AdmissionPolicyConfig)

The skill also has **full file access** to the production codebase — it reads interfaces, examples, and registration patterns as needed based on the user's `context_notes` hints.

**How the LLM learns config structure:** The LLM reads the baseline config from `env_defaults.yaml` (e.g., the baseline EndpointPickerConfig for routing) and uses it as a structural reference. It also reads the production codebase to understand config schemas. No explicit template is needed — the LLM produces config that matches the `config_kind` from `skill_input.json`.

**Plugin naming:** The LLM determines the plugin type name (e.g., `adaptive-v2-scorer`) based on the algorithm name, scenario conventions, and existing plugins in the codebase. The chosen name is recorded in `translation_output.json` as `plugin_type`.

### Flow

```
1. Read context.md + algorithm source + algorithm config
2. Writer produces Go code + treatment config YAML
        ↓
   [build/test gate] ──fail──→ Writer fixes (interactive)
        │ pass                        ↑
        ↓                             │
   Reviewer round N                   │
   (reviews: code + config + fidelity │
    against context.md)               │
        ↓                             │
   Writer refines code AND/OR config  │
        ↓                             │
   [build/test gate] ──fail───────────┘
        │ pass
        ↓
   Next round or consensus
        ↓
   Write translation_output.json + treatment_config.yaml + final snapshot
        ↓
   Copy all generated files to workspace/runs/<name>/generated/
```

### Build/test gate

- Reads `build_commands` from `skill_input.json` (deterministic, not LLM-generated)
- Runs each command sequentially with CWD set to the target repo root (e.g., `llm-d-inference-scheduler/`)
- On failure: structured error returned to the writer conversation
- Writer and operator collaborate to fix

### Reviewer loop

- **Models:** `Azure/gpt-4o`, `GCP/gemini-2.5-flash`, `aws/claude-opus-4-6` — called in parallel via `lib/llm.py`
- **Dev mode:** only `aws/claude-opus-4-6` (env var or flag)
- **What reviewers receive:**
  - `prompts/prepare/review.md` prompt
  - Algorithm source code (the simulated algorithm being translated)
  - `context.md` (mapping doc + translation notes — so reviewers can check fidelity)
  - Generated Go code (all files created/modified)
  - Generated treatment config YAML
- **What reviewers check:**
  - Translation fidelity: does the production code faithfully implement the simulated algorithm?
  - Config correctness: does the treatment config match the generated plugin? Are plugin types consistent? Are profiles correctly wired?
  - Code quality: does it follow production patterns from context?
- **Each round:**
  - All models called in parallel
  - Each returns: APPROVE or NEEDS_CHANGES with specific issues
  - Results saved to `workspace/runs/<name>/review/round_N.json` — written **once per round**, after the build/test gate passes. If the writer fixes code after reviewer feedback, the next build-pass starts a new round (N+1). Round files are never overwritten.
  - Skill reports: "Round 1: 2/3 approved. Issues: [signal weight normalization]"
  - Writer refines code AND/OR config → build/test gate → next round
- **Error policy:** Model errors are non-votes.
  - 2 APPROVE + 1 error → consensus (2/2 successful approved)
  - 1 APPROVE + 1 NEEDS_CHANGES + 1 error → no consensus (1/2)
  - 3 errors → no consensus. Skill prompts operator: `[r]etry round` / `[a]ccept anyway` / `[q]uit`. Retry re-calls all models for the same code. This avoids silent failure on transient API issues.
- **Consensus:** majority of successful responses (≥2/3 or 1/1 in dev mode)
- **Default rounds:** 3. Operator can request more.
- **Stopping:** consensus → proceed. Rounds exhausted → `[c]ontinue` / `[a]ccept` / `[q]uit`

### Snapshot management

Snapshots are taken after each revision that passes the build/test gate. They preserve the full state of generated code at each checkpoint.

- **When:** after every successful build/test pass (initial write, each post-review refinement)
- **What:** full file contents of every file in `files_created` + `treatment_config.yaml`. Stored as a directory per revision.
- **Naming:** `snapshots/v1/`, `snapshots/v2/`, ... (monotonic counter, never reused). Each directory mirrors the file structure:
  ```
  snapshots/v1/
  ├── adaptive_v2_scorer.go
  └── treatment_config.yaml
  snapshots/v2/
  ├── adaptive_v2_scorer.go
  └── treatment_config.yaml
  ```
- **Purpose:** allows diffing between revisions ("what changed after round 2 feedback?"). The final snapshot matches the live code in the submodule.

### `translation_output.json` field guidelines

- `plugin_type`: kebab-case name matching Go registration (e.g., `adaptive-v2-scorer`)
- `description`: 1-2 sentence summary of algorithm behavior, used in `run_summary.md`
- `files_created`: paths relative to target repo root
- `files_modified`: paths relative to target repo root

### What the skill does NOT do

- Does not assemble values.yaml or cluster YAMLs (that's prepare.py phase 4)
- Does not touch env_defaults.yaml or cluster config
- Does not generate run_summary.md
- Its job: produce working, reviewed Go code + treatment config + metadata + copies of all generated files in `generated/`

---

## 6. `scripts/deploy.py` — Fire and Forget

Pre-condition: `run_summary.md` contains `READY TO DEPLOY`.

All YAMLs pre-built by prepare, organized by **package** (`baseline`, `treatment`). Deploy builds the EPP image, applies PipelineRuns, and exits. No polling, no waiting for completion.

### Packages

Each experiment arm is a **package**. Packages are independent — you can submit all, resubmit one, or add new ones later.

**Package detection:** A package is any subdirectory of `cluster/` that contains a `pipelinerun.yaml` file. Default packages produced by prepare: `baseline`, `treatment`.

### Steps

1. **Pre-flight:**
   - Check `.state.json` — all phases through `gate` must be `done` with `verdict: "READY TO DEPLOY"`. If any phase is incomplete or gate was abandoned, exit with a clear error showing which phase is blocking.
   - Read `run_metadata.json` for namespace, registry, run name
   - Discover packages from `cluster/` (any subdirectory containing `pipelinerun.yaml`)
   - If `--package` specified, validate those packages exist
2. **Build EPP image:** In-cluster BuildKit. Poll BuildRun status every 30s with a 30-minute timeout. Push to registry.
3. **Submit packages:** For each package (alphabetical order; baseline before treatment), apply `cluster/<package>/pipelinerun.yaml` to the cluster via `kubectl apply`. Each PipelineRun runs on the cluster independently of the terminal.
4. **Print status commands and exit:**
   ```
   Packages submitted:
     ✓ baseline  → pipelinerun/sim2real-baseline-1744200000
     ✓ treatment → pipelinerun/sim2real-treatment-1744200001

   Monitor:
     tkn pipelinerun list -n sim2real-jchen
     tkn pipelinerun logs sim2real-baseline-1744200000 -n sim2real-jchen -f

   When complete: python scripts/analyze.py
   ```

### Flags

```bash
python scripts/deploy.py                       # build EPP + submit all packages
python scripts/deploy.py --package treatment   # submit only the treatment package
python scripts/deploy.py --skip-build-epp      # EPP already built, just submit
python scripts/deploy.py --run NAME            # override run name
python scripts/deploy.py --dry-run             # print what would be applied, don't apply
                                               # (skips EPP build, only shows kubectl apply commands)

python scripts/deploy.py collect                       # pull results for all packages
python scripts/deploy.py collect --package treatment   # pull results for one package
```

`--package` accepts one or more package names. Defaults to all discovered packages (subdirectories of `cluster/` containing `pipelinerun.yaml`). Useful to resubmit a single experiment arm after a cluster issue without touching the other.

`collect` pulls results from the cluster PVC into `workspace/runs/<name>/results/<package>/results.json`. It checks PipelineRun status first and reports which packages are complete, pending, or failed. Only pulls results for completed packages. This separates cluster access from analysis — `analyze.py` works purely from local files.

---

## 7. `scripts/analyze.py` — Compare Results

Works purely from local files (results pulled by `deploy.py collect`). No cluster access needed.

### Steps

1. **Check results exist:** Discover expected packages from `cluster/` directories (same detection as deploy). Verify `results/<package>/results.json` exists for each. If any missing, report which and remind user to run `deploy.py collect`. If `--package` is specified, only check that package.
2. **Comparison table:** Run `transfer_cli.py compare` across packages → `comparison_table.txt`
4. **Charts:** Per-workload bar charts + heatmap (existing functionality, works well)
5. **Terminal output:** Summary with per-workload breakdown and % change

### Flags

```bash
python scripts/analyze.py                        # current run, all packages
python scripts/analyze.py --run NAME             # specific run
python scripts/analyze.py --package treatment    # pull/analyze only one package
python scripts/analyze.py --no-charts            # skip chart generation
```

---

## 8. `scripts/validate.py` — Simplified

Remove pre-deploy checks (replaced by prepare's validate-assembly). Keep:

- **`post-deploy` subcommand:** Cluster-side checks after PipelineRun submission — pod readiness in namespace, PVC bound status, Tekton PipelineRun phase (running/succeeded/failed)
- **`post-collection` subcommand:** Results integrity checks after `deploy.py collect` — JSON parseable, expected workloads present, metrics non-empty

These are quick diagnostic commands, not gates. They help the operator debug cluster issues.

---

## 9. Prompts — Cleanup

### Keep (reorganized)

All paths relative to repo root (`prompts/prepare/translate.md`, etc.).

```
prompts/
├── prepare/
│   ├── translate.md      Writer prompt (generic, not scorer-specific)
│   └── review.md         Reviewer prompt (checks code + config + fidelity)
└── deploy/
    ├── build-push.md     Build and push EPP image
    └── validate.md       Cluster benchmark validation (noise removed)
```

### Remove

| File | Reason |
|------|--------|
| `prompts/extract.md` | Extract folded into translate |
| `prompts/extract-full.md` | Extract folded into translate |
| `prompts/translate.md` | Replaced by `prepare/translate.md` |
| `prompts/test.md` | Build/test handled by writer conversation |
| `prompts/transfer.md` | Old orchestrator replaced by skill |
| `prompts/validate-translation.md` | Replaced by `prepare/review.md` |
| `prompts/equivalence-gate.md` | Equivalence gate removed |
| `prompts/pr.md` | PR feature removed for now |

---

## 10. `scripts/lib/` — Cleanup

| Module | Action |
|--------|--------|
| `lib/manifest.py` | Rewrite for v2 schema. Required: `scenario`, `algorithm.source`, `algorithm.config`, `baseline.config`, `workloads`, `llm_config`. Remove v1 validations. |
| `lib/llm.py` | Keep as-is. Used by the skill for reviewer API calls. |
| `lib/consensus.py` | Keep as-is. Used by the skill. |
| `lib/gates.py` | **Remove entirely.** `human_gate()` replaced by inline prompt in prepare.py. `review_artifacts()` replaced by the skill's reviewer loop. `_run_chat_loop()` (claude -p subprocess) no longer needed. |
| `lib/validate_checks.py` | Keep post-deploy/post-collection checks. Remove pre-deploy checks. |

---

## 11. Workspace Layout

```
workspace/
├── setup_config.json                          # written by setup.py
├── context/
│   └── routing/
│       └── a1b2c3.md                          # cached context document
└── runs/
    └── sim2real-2026-04-09/
        ├── .state.json                        # prepare phase tracking
        ├── run_metadata.json                  # cross-stage metadata
        │
        │  ── written by prepare.py phase 3 ──
        ├── skill_input.json                   # paths + config for translation skill
        │
        │  ── written by translation skill ──
        ├── translation_output.json            # plugin_type, files, description
        ├── treatment_config.yaml              # LLM-generated treatment config
        ├── snapshots/                         # full files per build-passing revision
        │   ├── v1/
        │   │   ├── adaptive_v2_scorer.go
        │   │   └── treatment_config.yaml
        │   └── v2/
        │       ├── adaptive_v2_scorer.go
        │       └── treatment_config.yaml
        ├── review/                            # one file per round (never overwritten)
        │   ├── round_1.json
        │   └── round_2.json
        │
        │  ── written by translation skill (post-review) ──
        ├── generated/                         # final accepted copies of all LLM-produced files
        │   ├── adaptive_v2_scorer.go          # from files_created
        │   ├── register.go                    # from files_modified
        │   └── treatment_config.yaml          # treatment config copy
        │
        │  ── written by prepare.py assembly ──
        ├── algorithm_values.yaml              # model + workload config
        ├── values.yaml                        # merged final values
        ├── cluster/                           # organized by package
        │   ├── baseline/
        │   │   ├── epp.yaml                   # baseline EPP config
        │   │   └── pipelinerun.yaml           # baseline Tekton PipelineRun
        │   └── treatment/
        │       ├── epp.yaml                   # treatment EPP config
        │       └── pipelinerun.yaml           # treatment Tekton PipelineRun
        ├── run_summary.md                     # human review document
        │
        │  ── written by deploy.py collect ──
        ├── results/                           # organized by package
        │   ├── baseline/
        │   │   └── results.json
        │   └── treatment/
        │       └── results.json
        ├── comparison_table.txt
        └── charts/
            ├── workload_fm2a.png
            └── heatmap.png
```

---

## Design Decisions Summary

| Decision | Rationale |
|----------|-----------|
| **Hybrid execution** (scripts + skill) | Mechanical work is deterministic and debuggable. Creative work needs conversation. |
| **LLM generates treatment config alongside code** | User shouldn't need to know config templates. LLM already understands the config structure from context. Reviewer checks both. |
| **Natural language context hints** | Lower bar for users. The translator has file access and reads what it needs based on hints. |
| **Build/test commands from scenario config** | Deterministic. If LLM gets test commands wrong, the whole loop breaks. |
| **Config kind + helm_path from scenario config** | These are structural facts about the scenario, not per-algorithm choices. |
| **Deploy is fire-and-forget** | Experiments take hours. No reason to keep a terminal open. Submit and exit. |
| **`generated/` copies in workspace** | All LLM-produced files (Go code, configs) copied to `workspace/runs/<name>/generated/` after review. Operators inspect what was deployed without digging into the submodule working tree. |
| **Package-organized artifacts** | `cluster/baseline/`, `cluster/treatment/`, `results/baseline/`, etc. Easy to inspect, easy to resubmit one package. |
| **`--package` flag on deploy/analyze** | Resubmit or analyze a single experiment arm without touching others. |
| **`fast_iteration` removed** | Pipeline always runs full validation. Noise and PR are already out of scope; the shortcut is no longer needed. |
| **No equivalence gate** | Too specialized. Existing Go test suites (Suite A/B/C) remain in the codebase but are not gated — users can run them manually. Can be added back as a prepare subcommand later. |
| **No PR feature** | Not needed yet. Can be added to deploy.py later. |
| **Sequential everything** | Simplicity. Parallel execution can be added later if needed. |
| **`validate-assembly` as standalone subcommand** | Useful for debugging after manual edits to configs. |
| **`assemble` subcommand** | Reproduce cluster YAMLs without re-translating. Useful after env_defaults changes. |

---

## Resolved Questions

1. **`compile-pipeline` approach:** Implementation detail — do whatever is easiest. Keep the submodule tool if it works; inline if simpler.

2. **Result collection:** `deploy.py collect` pulls from cluster PVC into `workspace/runs/<name>/results/<package>/results.json`. `analyze.py` works purely from local files.

3. **Context cache and notes:** Notes excluded from cache hash, written to `skill_input.json`, appended in-memory by the skill at runtime. Cache key = file contents + submodule SHAs only.

4. **Skill input handoff:** `skill_input.json` is the single handoff point from prepare.py to the skill. Contains all explicit paths, resolved config, and context notes. The skill never parses env_defaults directly.

5. **`fast_iteration` removed.** Pipeline always runs full validation. Noise and PR are already out of scope.

6. **Baseline config clarification:** `baseline.config` in transfer.yaml = simulation baseline policy (context for the translator). `gaie.baseline` in env_defaults = production baseline EPP config (used for cluster YAML). These are different artifacts serving different purposes.

7. **Equivalence tests:** Suite A/B/C Go tests remain in the codebase but are not gated in the pipeline. Users can run them manually. Can be re-added as a prepare subcommand later.
