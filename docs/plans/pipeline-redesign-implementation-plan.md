# Pipeline Redesign — Implementation Plan

**Spec:** `docs/superpowers/specs/2026-04-08-pipeline-redesign.md`  
**Written:** 2026-04-09  
**Status:** Ready

---

## What Changes, What Stays

**Changes:**
- `config/transfer.yaml` — complete schema redesign (v1 → v2)
- `config/env_defaults.yaml` — restructured to `common` + `scenarios.*`
- `scripts/lib/manifest.py` — update validation for v2 schema
- `tools/transfer_cli.py` — update `merge-values` (new `--scenario` arg); add `compile-epp-configs` subcommand
- `.claude/skills/sim2real-prepare/SKILL.md` — full rewrite (7-step orchestrator)
- `scripts/deploy.py` — add `READY TO DEPLOY` precondition; read pre-built cluster YAMLs
- `prompts/` — reorganize into `prepare/` and `deploy/`; write new `translate.md` and `review.md`

**New files:**
- `scripts/lib/context_cache.py` — hash computation + cache lookup/write
- `scripts/lib/run_summary.py` — mechanical `run_summary.md` assembly
- `tools/schemas/translation_output.json` — JSON schema for the writer's output artifact
- `prompts/prepare/translate.md` — generic translate prompt
- `prompts/prepare/review.md` — reviewer prompt (multi-model loop)

**Deleted:**
- `prompts/extract.md`, `prompts/extract-full.md`, `prompts/test.md`, `prompts/transfer.md`
- `prompts/translate.md` (old signal-only version), `prompts/validate-translation.md`
- `prompts/generate.md` (replaced by `prompts/prepare/translate.md`)
- `scripts/prepare.py` (archived as `scripts/prepare.py.bak` after skill is verified)

**Unchanged:**
- `scripts/setup.py` — one-time bootstrap, untouched
- `scripts/deploy.py` cluster logic (Tekton, EPP build, results collection)
- `tools/transfer_cli.py` other subcommands (`benchmark`, `compare`, `validate-translation`, etc.)
- `scripts/lib/llm.py`, `scripts/lib/consensus.py`, `scripts/lib/gates.py`
- `prompts/equivalence-gate.md`, `prompts/build-push.md`, `prompts/pr.md`

---

## Architecture Note

The `sim2real-prepare` **skill IS the LLM session** — it runs as interactive Claude Code, not as a Python script calling Claude. This means:
- The skill file (`SKILL.md`) contains instructions to Claude, not shell commands  
- The interactive translate step (Step 2) runs in the main session because it requires real-time operator conversation; subagents can't be interrupted mid-execution
- Context assembly (Step 1) and AI config check (Step 5) are spawned as **Agent subagents** — isolated context windows, write results to disk, return
- Reviewer calls in Step 2 are **HTTP API calls via `lib/llm.py`** — they never touch the main session's context window

---

## Tasks

Tasks are ordered by dependency. Independent tasks can be done in parallel.

---

### Task 1 — Redesign `config/transfer.yaml` to v2 schema

**Depends on:** nothing

Replace the current v1 manifest (which mixed algorithm inputs with production target config) with a clean v2 manifest that contains only algorithm-specific inputs. The LLM discovers production target details from codebase context.

**New schema:**
```yaml
kind: sim2real-transfer
version: 2

scenario: routing   # routing | admission_control

algorithm:
  source: sim2real_golden/routers/router_adaptive_v2.go
  config: sim2real_golden/routers/policy_adaptive_v2.yaml

baseline:
  config: sim2real_golden/routers/policy_baseline_211.yaml

workloads:
  - sim2real_golden/workloads/workload_fm2a_groups_gt_instances.yaml
  - sim2real_golden/workloads/workload_fm3_burst.yaml

llm_config: sim2real_golden/llm_config.yaml

context:                    # optional
  files:
    - docs/transfer/blis_to_llmd_mapping.md
  notes: |
    Optional inline context added to the LLM during translation.
```

**Remove from the v1 manifest:**
- `algorithm.experiment_dir` (replaced by direct file paths)
- `target.*` (LLM discovers production target from codebase)
- `config.*` (moved to `env_defaults.yaml` per-scenario sections)
- `validation.*` (removed; deploy.py handles this)
- `artifacts.*` (internal details, not user-facing)

**Populate with current run's values** (keep as a working manifest, don't blank it out):
- `scenario: routing`
- `algorithm.source: sim2real_golden/routers/router_adaptive_v2.go`
- `algorithm.config: sim2real_golden/routers/policy_adaptive_v2.yaml`
- `baseline.config: sim2real_golden/routers/policy_baseline_211.yaml`
- workloads from current `algorithm.workloads`
- `llm_config: sim2real_golden/llm_config.yaml`
- `context.files: [docs/transfer/blis_to_llmd_mapping.md]`
- `context.notes`: carry over `context.mapping_notes` content

**Note — `run_metadata.json` is NOT touched by this task.** That file is created by `setup.py` with the schema `{namespace, registry, pipeline_commit, stages: {setup, prepare, deploy}}` and is independent of the transfer manifest format. The prepare skill updates only `stages.prepare.status` in `run_metadata.json` (see Task 11, Step 7). No changes to `setup.py` or `run_metadata.json` format are required.

---

### Task 2 — Restructure `config/env_defaults.yaml` to `common` + `scenarios`

**Depends on:** nothing

The current flat structure mixes routing-specific infra (gateway, connection pool, baseline EPP config) with scenario-agnostic infra (model config, EPP image, fast_iteration). The new structure separates these.

**New structure:**
```yaml
common:
  observe:
    request_multiplier: 10
    noise_runs: 5
  stack:
    model:
      vllm_image: ghcr.io/llm-d/llm-d-cuda:v0.5.1
      helmValues:
        # ... model helmValues (auth, servicePort, prefill, decode, etc.) — unchanged
    gaie:
      epp_image:
        upstream:          # unchanged from current stack.gaie.epp_image.upstream
          hub: ghcr.io/llm-d
          name: llm-d-inference-scheduler
          tag: latest
          pullPolicy: IfNotPresent
        build:             # written by setup.py; current values preserved
          hub: ghcr.io/kalantar
          name: llm-d-inference-scheduler
          tag: admin6
          platform: linux/amd64
          pullPolicy: Always
  pipeline:
    fast_iteration: true
    sleepDuration: 30s

scenarios:
  routing:
    gaie:
      shared:
        helmValues:
          inferenceExtension:
            pluginsConfigFile: custom-plugins.yaml
            flags:
              v: 5
            monitoring:
              interval: "5s"
          provider:
            name: istio
          istio:
            destinationRule:
              trafficPolicy:
                connectionPool:
                  # ... connection pool settings from current stack.gaie.shared
      baseline:
        helmValues:
          inferenceExtension:
            pluginsCustomConfig:
              # ... baseline EPP config from current stack.gaie.baseline
    gateway:                         # from current stack.gateway
      helmValues:
        gateway:
          provider: istio
          # ...
    treatment_config_template: |
      apiVersion: inference.networking.x-k8s.io/v1alpha1
      kind: EndpointPickerConfig
      plugins:
      - type: {plugin_type}
      - type: decode-filter
      - type: max-score-picker
      - type: single-profile-handler
      schedulingProfiles:
      - name: default
        plugins:
        - pluginRef: decode-filter
        - pluginRef: max-score-picker
        - pluginRef: {plugin_type}
          weight: 1
    baseline_config_template: |
      apiVersion: inference.networking.x-k8s.io/v1alpha1
      kind: EndpointPickerConfig
      plugins:
      - type: load-aware-scorer
      # ... (full baseline config)

  admission_control:
    gaie:
      inferenceObjectives:   # from current stack.gaie.inferenceObjectives
        - name: critical
          priority: 100
        - name: standard
          priority: 0
        - name: sheddable
          priority: -10
        - name: batch
          priority: -50
    treatment_config_template: |
      apiVersion: inference.networking.x-k8s.io/v1alpha1
      kind: AdmissionPolicyConfig
      policies:
      - type: {plugin_type}
    baseline_config_template: |
      apiVersion: inference.networking.x-k8s.io/v1alpha1
      kind: AdmissionPolicyConfig
      policies:
      - type: always-admit
```

**Key moves:**
- `stack.gateway` → `scenarios.routing.gateway`
- `stack.model` → `common.stack.model`  
- `stack.gaie.epp_image` → `common.stack.gaie.epp_image`
- `stack.gaie.inferenceObjectives` → `scenarios.admission_control.gaie.inferenceObjectives`
- `stack.gaie.shared` → `scenarios.routing.gaie.shared`
- `stack.gaie.baseline` → `scenarios.routing.gaie.baseline`
- `observe` → `common.observe`
- `pipeline` → `common.pipeline`
- `config.treatment_config_template` in v1 `transfer.yaml` → `scenarios.routing.treatment_config_template`
- `config.baseline_config_template` in v1 `transfer.yaml` → `scenarios.routing.baseline_config_template`

---

### Task 3 — Update `scripts/lib/manifest.py` for v2 schema

**Depends on:** Task 1 (defines new schema)

Replace the v1 `load_manifest()` validation with v2 validation. The utility functions (`set_nested_path`, `_has_nested_path`, `_get_nested_path`) are reusable; keep them.

**New required fields:**
```python
required_fields = [
    'scenario',
    'algorithm.source',
    'algorithm.config',
    'baseline.config',
    'workloads',
    'llm_config',
]
```

**New validation:**
- `scenario` must be a non-empty string
- `workloads` must be a non-empty list
- `version` must be `2` (error with clear message if v1 format detected)

**New defaults:**
```python
defaults = {
    'context.files': [],
    'context.notes': '',
}
```

**Remove:** all v1 field validation (`target.*`, `config.*`, `validation.*`, `artifacts.*`)

---

### Task 4 — Update `tools/transfer_cli.py` `merge-values` for scenario-aware merge

**Depends on:** Task 2 (new env_defaults structure)

The `merge-values` subcommand currently takes `--env config/env_defaults.yaml` and merges it wholesale. With the new structure, it needs to:

1. Load `env_defaults.yaml`
2. If the file has a `common` key (new format), merge `common` + `scenarios.<scenario>` as the effective env base
3. If the file has a `stack` key at root (old format), use as-is (backward compat during transition)
4. Continue with existing deep-merge logic

**New CLI signature:**
```
transfer_cli.py merge-values \
  --env config/env_defaults.yaml \
  --scenario routing \            ← new required arg
  --algorithm workspace/tekton/algorithm_values.yaml \
  --out workspace/runs/<name>/values.yaml
```

**Implementation:** Before the existing merge, prepend a merge step:
```python
if 'common' in env_data:
    scenario_overrides = env_data.get('scenarios', {}).get(scenario, {})
    effective_env = deep_merge(env_data['common'], scenario_overrides)
else:
    effective_env = env_data   # legacy flat format
```

Also: strip `scenarios` key from the effective env before merging with algorithm values.

---

### Task 5 — Add `compile-epp-configs` subcommand to `tools/transfer_cli.py`

**Depends on:** nothing (new functionality)

New subcommand that extracts EPP configs from `values.yaml` and writes them as standalone files. This is the mechanial part of Step 4 in the skill.

```
transfer_cli.py compile-epp-configs \
  --values workspace/runs/<name>/values.yaml \
  --out-dir workspace/runs/<name>/cluster/
```

**What it produces:**
- `cluster/epp-baseline.yaml` — extracted from `gaie.baseline.helmValues.inferenceExtension.pluginsCustomConfig.custom-plugins.yaml` (routing) or equivalent for other scenarios
- `cluster/epp-treatment.yaml` — extracted from `gaie.treatment.helmValues...`

The exact helm path to extract from is determined by `config_kind` field in `translation_output.json`. Read that file if present in the run directory, or accept `--helm-path` as an override.

**Exit codes:** 0 = success, 1 = missing/malformed inputs, 2 = output write error

**Note on `compile-pipeline`:** Per `CLAUDE.md`, this subcommand already exists in `tools/transfer_cli.py` and invokes `tektonc-data-collection/tektonc/tektonc.py` via subprocess. Before implementing Task 11 Step 4, verify the existing signature (what `--values` / `--out-dir` args it takes) and whether it already writes to a run-specific output directory. If the current `compile-pipeline` writes to a fixed location (e.g., `workspace/tekton/`), it will need to accept an `--out-dir` arg so the skill can direct output to `workspace/runs/<name>/cluster/`. Add that arg if missing — no other changes to `compile-pipeline` are required.

---

### Task 6 — Create `scripts/lib/context_cache.py`

**Depends on:** nothing

Small module (~100 lines) for context hash computation and cache management. Used by the prepare skill via Bash subprocess.

```python
# context_cache.py — two entry points for the skill to call via bash

def compute_hash(context_files: list[str], inference_sim_path: str, llmd_path: str) -> str:
    """SHA256 of: sorted file contents of context_files + inference-sim HEAD SHA + llmd HEAD SHA"""

def check_cache(workspace_dir: str, scenario: str, hash_val: str) -> str | None:
    """Return path to cached context.md or None"""

def write_cache(workspace_dir: str, scenario: str, hash_val: str, content: str) -> str:
    """Write context.md to workspace/context/<scenario>/<hash>.md; return path"""
```

**Hash inputs (all content-based):**
- Content of each file in `context.files` (caller passes these paths, read from disk; error if missing)
- Output of `git -C inference-sim rev-parse HEAD`
- Output of `git -C llm-d-inference-scheduler rev-parse HEAD`

**Cache path:** `workspace/context/<scenario>/<hash>.md`

**Important — `context.notes` are NOT cached.** The file written to disk (`<hash>.md`) is the pure cacheable context: mapping doc, production interfaces, example files, registration pattern. `context.notes` are run-specific and appended in-memory by the main skill session after reading the cached file, immediately before the translate step. The subagent never receives `context.notes`.

**CLI wrapper** (so the skill can call it from Bash):
```bash
python tools/transfer_cli.py context-hash \
  --files docs/transfer/blis_to_llmd_mapping.md \
  --inference-sim inference-sim \
  --llmd llm-d-inference-scheduler \
  --scenario routing
# stdout: hash value
# exit 0: success; exit 1: file not found
```

Add `context-hash` as a new subcommand in `transfer_cli.py` backed by `context_cache.py`.

---

### Task 7 — Define `translation_output.json` schema

**Depends on:** nothing

The writer LLM (Step 2 of the skill) must produce `workspace/runs/<name>/translation_output.json`. Define the schema so it can be validated downstream.

Create `tools/schemas/translation_output.json`:
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["plugin_type", "files_created", "files_modified", "package",
               "test_commands", "config_kind", "helm_path", "needs_custom_config"],
  "properties": {
    "plugin_type": { "type": "string" },
    "files_created": { "type": "array", "items": { "type": "string" } },
    "files_modified": { "type": "array", "items": { "type": "string" } },
    "package": { "type": "string" },
    "test_commands": {
      "type": "array",
      "items": { "type": "array", "items": { "type": "string" } }
    },
    "config_kind": {
      "type": "string",
      "enum": ["EndpointPickerConfig", "AdmissionPolicyConfig"]
    },
    "helm_path": { "type": "string" },
    "needs_custom_config": { "type": "boolean" },
    "suggested_config": { "type": ["string", "null"] }
  }
}
```

Add a `validate-schema` call for this artifact in the skill (Step 2 post-write check).

---

### Task 8 — Create `scripts/lib/run_summary.py`

**Depends on:** Task 7 (reads translation_output.json)

Mechanical assembly of `run_summary.md` from run artifacts. No LLM involved. Called from the skill via Bash.

**Entry point:**
```bash
python scripts/lib/run_summary.py \
  --run-dir workspace/runs/<name>/ \
  --transfer-yaml config/transfer.yaml \
  --out workspace/runs/<name>/run_summary.md
```

**Reads:**
- `config/transfer.yaml` — algorithm name, source, workloads
- `workspace/runs/<name>/translation_output.json` — plugin_type, files_created/modified
- `workspace/runs/<name>/.state.json` — build/test pass, review rounds + consensus
- `workspace/runs/<name>/ai_check_result.json` — PASSED / SKIPPED + details
- `workspace/runs/<name>/cluster/epp-baseline.yaml` — EPP config to embed
- `workspace/runs/<name>/cluster/epp-treatment.yaml` — EPP config to embed
- `workspace/runs/<name>/values.yaml` — vLLM image, replicas, GPU, workload request counts
- `workspace/setup_config.json` — namespace

**Produces:** `run_summary.md` in the format shown in spec §5. Does **not** write `READY TO DEPLOY` — that's written by the human review gate (Step 7 of the skill).

**Exit codes:** 0 = success, 1 = missing required artifact (report which one), 2 = write error

---

### Task 9 — Write `prompts/prepare/translate.md`

**Depends on:** nothing

New generic translate prompt. Replaces `prompts/generate.md` (which was scorer-specific and called `claude -p`).

The prompt is used by the writer LLM in the main session — the skill loads this file and follows its instructions. It is not a template that gets `printf`'d into a subprocess call; it's a skill sub-document.

**Contents structure:**
- Role: you are translating a simulated algorithm into production code
- Inputs to read: algorithm source (`algorithm.source`), algorithm config (`algorithm.config`), context.md (the cached context document with `context.notes` appended)
- Also read for translation context only: `baseline.config` — helps the writer understand what the baseline algorithm does, so the production translation covers the same behavioral space. This is NOT the production baseline EPP config; it is the simulated baseline policy YAML.
- What to produce: whatever changes are needed across the entire `llm-d-inference-scheduler` submodule to faithfully implement the algorithm. This typically means a new plugin file and a `register.go` edit, but may include revisions to existing files anywhere in the submodule — shared interfaces, scheduler logic, existing plugin files — if the algorithm requires it. The writer has full read and write access and should use it.
- Record all changes in `translation_output.json`: every file created goes in `files_created`; every file modified (including `register.go` and any other touched file) goes in `files_modified`. The build/test gate reads `test_commands` from this artifact, so `test_commands` must cover all affected packages, not just the plugin directory.
- `translation_output.json` format: copy the schema from Task 7
- Translation fidelity checklist (from current `generate.md` and `validate-translation.md`)
- Instruction: explain your translation choices; stay in conversation until operator approves
- Instruction: after writing files, run the build/test gate (via Bash) before calling translation done

**Scenario-agnostic:** The prompt does not mention `EndpointPickerConfig` specifically. The writer determines the right plugin interface from context.md and the codebase.

---

### Task 10 — Write `prompts/prepare/review.md`

**Depends on:** nothing

Reviewer prompt for one round of the multi-model review loop. Used as input to `lib/llm.py` API calls (not a skill document).

**Input variables** (substituted before sending to API):
- `{ALGORITHM_SOURCE}` — full content of algorithm source file
- `{CONTEXT_SUMMARY}` — first N lines of context.md (full is too large for inline API call)
- `{GENERATED_FILES}` — content of all `files_created` + `files_modified`

**Required output format** (the reviewer must respond in this structure):
```json
{
  "decision": "APPROVE" | "NEEDS_CHANGES",
  "issues": [
    {
      "severity": "CRITICAL" | "IMPORTANT" | "MINOR",
      "description": "...",
      "location": "filename:line_or_function (optional)"
    }
  ],
  "summary": "One-sentence summary of this review"
}
```

**Evaluation criteria:** translation fidelity (does the production code faithfully implement the algorithm?), code correctness (nil checks, edge cases), registration completeness, config consistency.

---

### Task 11 — Rewrite `.claude/skills/sim2real-prepare/SKILL.md`

**Depends on:** Tasks 1–10 (all upstream pieces must exist)

This is the largest change. The skill becomes the 7-step interactive orchestrator described in the spec. The current skill (`SKILL.md`) runs sequential bash stages; the new skill orchestrates via Agent tool spawns, interactive translation, and API calls.

**Skill structure:**

```
## Prerequisites
- workspace/setup_config.json exists (run /sim2real-setup first)
- config/transfer.yaml exists with required fields

## Step 0 — Load config
Read workspace/setup_config.json → current_run, namespace, registry, repo_name
Read config/transfer.yaml (validate via manifest.py)
Resolve run_dir = workspace/runs/<current_run>/
Create run_dir if new run; load .state.json if resuming

## Step 1 — Context check
Compute hash via: python tools/transfer_cli.py context-hash ...
If workspace/context/<scenario>/<hash>.md exists → cache hit
  Report: "Using cached context (routing, hash: abc123, inference-sim@d4f1a2c, llm-d@e9b3f1a)"
  Skip subagent.
If cache miss → spawn context-assembly subagent:
  - Reads: context.files (from manifest), production interfaces, mapping doc, registration pattern
  - Does NOT receive context.notes — those are run-specific, not cached
  - Writes: workspace/context/<scenario>/<hash>.md (pure cached artifact)
  - Returns path

## Step 2 — Translate + Review loop
[Stays in main session — interactive]
Load context.md from cache; append context.notes from manifest in-memory (not written to disk)
The combined document (cached context + appended notes) is what the writer uses.
Follow prompts/prepare/translate.md as writer instructions
After initial code write:
  - Run build/test gate: read test_commands from translation_output.json, run sequentially
  - If fail → hand error to writer, loop back
  - If pass → validate translation_output.json against schema:
      python tools/transfer_cli.py validate-schema \
        workspace/runs/<name>/translation_output.json \
        --schema tools/schemas/translation_output.json
    Fail here = writer must fix the artifact, not proceed to review
  - Start reviewer loop:
    For each round (default 3):
      Call lib/llm.py with review.md prompt in parallel: gpt-4o, gemini-2.5-flash, claude-opus-4-6
      Save results to workspace/runs/<name>/review/round_N.json
      Report consensus (e.g., "Round 1: 2/3 approved")
      Writer refines based on feedback → build/test gate → next round
      (Reviews in round N+1 see the refined code; same models re-review each round)
    If consensus reached → proceed
    If rounds exhausted → show summary, prompt: [c]ontinue / [a]ccept / [q]uit

## Step 3 — Values assembly
3a: Generate treatment config
  - Read needs_custom_config from translation_output.json
  - If false: fill {plugin_type} into scenarios.<scenario>.treatment_config_template → write
    workspace/runs/<name>/treatment_config.yaml
  - If true: LLM API call via lib/llm.py with plugin source code + template + suggested_config
    (use suggested_config from translation_output.json as the starting point the LLM refines)
    → write workspace/runs/<name>/treatment_config.yaml

  For baseline EPP config: use scenarios.<scenario>.baseline_config_template from env_defaults.yaml
  (fill any {plugin_type} placeholder if present) → write workspace/runs/<name>/baseline_config.yaml

  NOTE: baseline.config from transfer.yaml is a simulated policy YAML used only as translation
  context in Step 2. It is NOT the source for the production baseline EPP config. The production
  baseline EPP config always comes from baseline_config_template in env_defaults.yaml.
3b: Run merge-values
  - python tools/transfer_cli.py merge-values \
      --env config/env_defaults.yaml \
      --scenario <scenario> \
      --algorithm workspace/tekton/algorithm_values.yaml \
      --out workspace/runs/<name>/values.yaml
  - Validate output against schema

## Step 4 — Cluster YAML assembly
- Extract EPP configs: python tools/transfer_cli.py compile-epp-configs \
    --values workspace/runs/<name>/values.yaml \
    --out-dir workspace/runs/<name>/cluster/
- Compile pipeline runs: python tools/transfer_cli.py compile-pipeline \
    [existing args — see Task 4 note below]
    → writes cluster/pipelinerun-baseline.yaml and cluster/pipelinerun-treatment.yaml

## Step 5 — AI config checker
Spawn ai-checker subagent (Agent tool):
  - Reads: all files listed in files_created AND files_modified from translation_output.json,
    translation_output.json itself, cluster/epp-treatment.yaml, values.yaml
    (files_modified must be included — the writer may have revised existing files such as
     interfaces or shared types, not just created new ones; all changed files are in scope.
     values.yaml is needed to verify that helm_path from translation_output.json
     correctly points to the config placement in the merged values)
  - Checks: plugin_type consistent in code + EPP config + register.go + helm_path
  - Writes: workspace/runs/<name>/ai_check_result.json
  - If issue found: reports to main session, operator decides: [r]etranslate or [f]ix manually

## Step 6 — Run summary
python scripts/lib/run_summary.py \
  --run-dir workspace/runs/<name>/ \
  --transfer-yaml config/transfer.yaml \
  --out workspace/runs/<name>/run_summary.md

## Step 7 — Human review gate
Display run_summary.md contents
Prompt: "Type 'deploy' to approve, 'edit' to open for edits, or 'quit' to abandon"
On 'deploy': append "READY TO DEPLOY" to run_summary.md; update .state.json gate=done
On 'edit': open file, re-display after edits, re-prompt
On 'quit': exit without marking ready
```

**State tracking:** All steps write completion markers to `.state.json`. Re-running the skill skips completed steps and reports which step it's resuming from.

**Update `run_metadata.json`:** When step 7 completes, set `stages.prepare.status = "completed"` in `workspace/runs/<name>/run_metadata.json` for deploy.py compatibility.

**Arguments:**
- `--reviews N` — reviewer rounds (default 3)
- `--rebuild-context` — force context cache miss
- `--skip-ai-check` — skip Step 5 (records as `"status": "skipped"` in state)
- `--dev` — reviewer loop uses only `aws/claude-opus-4-6`

---

### Task 12 — Update `scripts/deploy.py`

**Depends on:** Task 7 (translation_output.json schema; run_summary.md format)

The spec says cluster orchestration logic (Tekton, EPP build steps) is **out of scope**. But `deploy.py` currently reads ~8 fields from the v1 manifest that don't exist in v2. These path and source changes must be made even though the cluster operations themselves don't change.

**A. Add `READY TO DEPLOY` precondition:**
At the top of `check_prerequisites()` (called early in `main()`), replace the old artifact list check with:
```python
run_summary_path = run_dir / "run_summary.md"
if not run_summary_path.exists():
    sys.exit("ERROR: run_summary.md not found. Run /sim2real-prepare first.")
if "READY TO DEPLOY" not in run_summary_path.read_text():
    sys.exit("ERROR: run not approved. Complete /sim2real-prepare and type 'deploy'.")
# Check pre-built cluster YAMLs exist
for name in ["epp-baseline.yaml", "epp-treatment.yaml",
             "pipelinerun-baseline.yaml", "pipelinerun-treatment.yaml"]:
    p = run_dir / "cluster" / name
    if not p.exists():
        sys.exit(f"ERROR: missing cluster YAML: {p}")
```

**B. Update artifact paths — v1 → v2 locations:**

| Current (v1) | New (v2) |
|---|---|
| `run_dir / "prepare_tekton" / "values.yaml"` | `run_dir / "values.yaml"` |
| `run_dir / "prepare_stage3_output.json"` checks | `run_dir / "translation_output.json"` |
| `run_dir / "prepare_algorithm_summary.json"` checks | removed — subsumed by run_summary.md check |
| `run_dir / "prepare_signal_coverage.json"` checks | removed |
| `run_dir / "prepare_translation_reviews.json"` checks | removed |
| `run_dir / "prepare_equivalence_results.json"` checks | removed |

**C. Redirect manifest field reads to hardcoded paths or translation_output.json:**

The current `deploy.py` reads several fields from the manifest that no longer exist in v2:

- `manifest["config"]["helm_path"]` → read from `translation_output.json["helm_path"]` instead (in `_preflight_cmd` and `stage_build_epp`)
- `manifest["target"]["test_commands"][0]` → read from `translation_output.json["test_commands"][0]` (in `_preflight_cmd`)
- `manifest["target"]["repo"]` → hardcode `"llm-d-inference-scheduler"` (the repo never changes)
- `manifest["config"]["env_defaults"]` → hardcode `"config/env_defaults.yaml"`
- `manifest["algorithm"]["experiment_dir"]` + `manifest["algorithm"]["workloads"]` (used to find workloads dir for benchmarks, line ~1075) → read `workloads` list directly from v2 `transfer.yaml` via `manifest.py` loader

**D. Update `--manifest` arg:**
The `--manifest` CLI arg still points to `config/transfer.yaml`. Keep it. After v2 load, the manifest provides `workloads` list (needed for benchmarks) and `scenario` (needed for config lookup). Load with the updated `manifest.py` v2 loader from Task 3.

**Unchanged:** `stage_build_epp` cluster build logic, `_run_pipeline_phase` Tekton trigger, `stage_benchmarks` benchmark collection, `stage_pr` PR creation logic (only its manifest field reads need updating per C above).

---

### Task 13 — Reorganize `prompts/` directory

**Depends on:** Tasks 9 and 10 (new prompts must exist first)

```
prompts/prepare/
  translate.md       ← moved from Task 9
  review.md          ← moved from Task 10

prompts/deploy/
  equivalence-gate.md  ← moved from prompts/
  build-push.md        ← moved from prompts/
  validate.md          ← moved from prompts/ (update: remove noise references)
  pr.md                ← moved from prompts/
```

**Delete:**
```
prompts/extract.md
prompts/extract-full.md
prompts/test.md
prompts/transfer.md
prompts/translate.md          (old signal-only version)
prompts/validate-translation.md  (replaced by prompts/prepare/review.md)
prompts/generate.md           (replaced by prompts/prepare/translate.md)
```

**Update `validate.md`:** Remove references to noise gate/noise characterization step (deferred per spec §6 "Note on noise"). The remaining validate.md covers cluster benchmarks after equivalence gate.

---

### Task 14 — Archive `scripts/prepare.py`

**Depends on:** Task 11 (skill must be working end-to-end first)

After the skill is verified working on a real run:
```bash
git mv scripts/prepare.py scripts/prepare.py.bak
```

Update `CLAUDE.md` to remove `prepare.py` CLI command references and point to `/sim2real-prepare`.

---

## Execution Order

```
Task 1  ──┐
Task 2  ──┼──→ Task 4 (merge-values update)
           └──→ Task 3 (manifest.py update)

Task 5  (independent)
Task 6  ──→ merge into Task 5 (context-hash subcommand lives in transfer_cli.py)
Task 7  ──→ Task 8 (run_summary reads translation_output schema)
Task 9  (independent)
Task 10 (independent)

Tasks 1–10 must complete before Task 11 (skill rewrite)

Task 11 ──→ Task 12 can be done independently but deploy.py should not be switched
             to pre-built-YAML mode until the skill is generating those YAMLs
Task 13 ──→ done after Tasks 9 and 10 (prompts exist before moving them)
Task 14 ──→ last, after skill verified on real run
```

**Safe to do in parallel:** Tasks 1+2, Tasks 5+6, Tasks 7+8, Tasks 9+10

---

## Definition of Done

- [ ] `config/transfer.yaml` loads without error under new `manifest.py`
- [ ] `merge-values --scenario routing` produces identical `values.yaml` shape to today (regression: diff old vs new merge output on current inputs)
- [ ] `compile-epp-configs` writes well-formed YAML that matches the EPP config currently embedded in `env_defaults.yaml`
- [ ] Context hash is stable across runs when inputs don't change; changes when any context file or submodule SHA changes
- [ ] Prepare skill completes Steps 0–7 on a routing run; `run_summary.md` contains all required sections
- [ ] `READY TO DEPLOY` check in `deploy.py` gates correctly: exits with clear error when not approved
- [ ] `deploy.py` reads `translation_output.json` for `helm_path` + `test_commands`; no manifest v1 field reads remain
- [ ] Deploy reads pre-built cluster YAMLs from `cluster/` without error; does not generate any YAML at deploy time
- [ ] Old prompts directory is clean (no unused files)
