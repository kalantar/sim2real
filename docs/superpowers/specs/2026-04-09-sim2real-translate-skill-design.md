# sim2real-translate Skill — Design Spec

**Date:** 2026-04-09
**Status:** Draft — awaiting user approval
**Parent spec:** `docs/superpowers/specs/2026-04-08-pipeline-redesign.md`

---

## Problem Statement

The current `sim2real-translate` skill is a monolithic SKILL.md that manually parses `skill_input.json` via bash, tracks state through ad-hoc file existence checks, and rebuilds context every invocation. It does not align with the pipeline redesign spec's architecture: subagent-driven context assembly, rich `translation_output.json` schema, cached context keyed by content + submodule SHAs, or the treatment config template system.

---

## Design Goals

1. **Context-efficient** — context assembly delegated to a subagent; main session stays lean for the interactive translate conversation
2. **Resumable** — state tracked in `.state.json` via `pipeline/lib/state_machine.py`; re-invoking skips completed steps
3. **Fast when nothing changed** — context cache hit means zero assembly work; translation brief reused across re-invocations
4. **Faithful to the pipeline redesign spec** — `translation_output.json` schema, artifact layout, state schema, review mechanics all match Section 4 of the parent spec
5. **Low friction** — progress shown via Claude Code task indicators; no separate dashboard script

---

## Scope

This skill implements **Steps 1–2** of the `sim2real-prepare` skill from the parent spec:

| Parent spec step | Owner | Notes |
|------------------|-------|-------|
| Step 0 — Load setup config | `pipeline/prepare.py` Phase 1 | Reads manifest, resolves scenario, validates prerequisites |
| **Step 1 — Context check** | **sim2real-translate** | Subagent on cache miss; instant on hit |
| **Step 2 — Translate + Review loop** | **sim2real-translate** | Interactive writer + build gate + parallel review |
| Steps 3–4 — Values + Cluster YAML | `pipeline/prepare.py` Phase 4 | Reads `translation_output.json` |
| Step 5 — AI config checker | `pipeline/prepare.py` (subagent) | Post-assembly check |
| Steps 6–7 — Summary + Gate | `pipeline/prepare.py` Phases 5–6 | Human review before deploy |

**What this skill does NOT do:**
- Values assembly, cluster YAML compilation, AI config check, run summary, human gate — all `pipeline/prepare.py`
- Setup or manifest loading — `pipeline/prepare.py` Phase 1
- EPP build, cluster deployment — `pipeline/deploy.py`

---

## Input Contract

The skill reads `workspace/runs/<name>/skill_input.json`, written by `pipeline/prepare.py` Phase 3 (Translation Checkpoint):

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

`skill_input.json` is the **single source of truth** for the skill. The skill does not parse `env_defaults.yaml` or resolve scenarios itself. All paths are repo-root-relative.

`context_path` may point to an already-cached context file (if prepare.py built it) or may need assembly (cache miss). The skill handles both cases.

---

## Output Contract

### `translation_output.json`

Written to `$RUN_DIR/translation_output.json`. Schema matches parent spec Section 4, Step 2:

```json
{
  "plugin_type": "adaptive-v2-scorer",
  "files_created": ["pkg/plugins/scorer/adaptive_v2_scorer.go"],
  "files_modified": ["pkg/plugins/register.go"],
  "package": "scorer",
  "test_commands": [
    ["go", "build", "./..."],
    ["go", "vet", "./..."],
    ["go", "test", "-timeout", "10m", "./pkg/plugins/scorer/...", "-v"]
  ],
  "config_kind": "EndpointPickerConfig",
  "helm_path": "gaie.treatment.helmValues.inferenceExtension.pluginsCustomConfig.custom-plugins.yaml",
  "needs_custom_config": false,
  "suggested_config": null
}
```

Field guidelines:
- `plugin_type` — kebab-case name matching Go registration (e.g., `adaptive-v2-scorer`)
- `files_created` / `files_modified` — paths relative to target repo root
- `package` — Go package name (e.g., `scorer`, `admission`)
- `test_commands` — the writer determines the right commands for what it generated. Initially seeded from `skill_input.json`'s `build_commands`, but the writer may customize them (e.g., if it created files in a different package or needs a narrower test scope). Once `translation_output.json` exists, the build/test gate always reads `test_commands` from there, ignoring `build_commands`
- `config_kind` — `EndpointPickerConfig`, `AdmissionPolicyConfig`, etc.
- `helm_path` — dot-separated path into `values.yaml` where the treatment config belongs
- `needs_custom_config` — `true` if the treatment config deviates from the scenario's standard template
- `suggested_config` — if `needs_custom_config` is true, the writer's suggested config structure; `null` otherwise

### `treatment_config.yaml`

Written to `$RUN_DIR/treatment_config.yaml`. The writer produces this alongside the Go code because the config and code are coupled — the config references the plugin types the writer registered.

For simple cases, the writer fills the scenario's `treatment_config_template` from `env_defaults.yaml` (substituting `{plugin_type}`) and sets `needs_custom_config: false`. For custom cases (multi-profile, non-standard wiring), the writer produces a bespoke config and sets `needs_custom_config: true` in `translation_output.json`. In both cases, the translate skill writes the file — `pipeline/prepare.py` Phase 4 validates and consumes it but does not regenerate it.

### Other artifacts

| Artifact | Location | Description |
|----------|----------|-------------|
| `snapshots/v1/`, `v2/`, ... | `$RUN_DIR/snapshots/` | File copies after each build-passing revision |
| `review/round_1.json`, ... | `$RUN_DIR/review/` | Per-round review results |
| `generated/` | `$RUN_DIR/generated/` | Final copies of all created/modified files + treatment config |

---

## State Tracking

State is persisted in `$RUN_DIR/.state.json` via `pipeline/lib/state_machine.py`. The actual implementation uses a flat `phases` dict (not `steps` — the parent spec's `steps` key was a draft convention; the implementation uses `phases`). The translate skill writes to the `context` and `translate` phase entries:

```json
{
  "run_name": "sim2real-2026-04-09",
  "scenario": "routing",
  "phases": {
    "context": { "status": "done", "hash": "abc123", "timestamp": "..." },
    "translate": {
      "status": "done",
      "files": ["pkg/plugins/scorer/adaptive_v2_scorer.go"],
      "review_rounds": 3,
      "consensus": "2/3",
      "timestamp": "..."
    }
  }
}
```

Build/test results are not tracked as separate phase entries. They fire multiple times within the translate loop. The build/test history is implicitly recorded through snapshots (each `snapshots/vN/` represents a successful build pass) and review round files (`review/round_N.json` is only written after a build-passing revision).

**Re-invocation behavior:**
- If `context` phase is done and the cached file at `workspace/context/<scenario>/<hash>.md` exists: skip context assembly
- If `translate` phase is done and `translation_output.json` exists: verify `generated/` directory has all expected files, print completion summary with plugin_type/files/rounds/consensus, and exit. No work to do.
- If `translate` phase is NOT done but snapshots/review rounds exist: resume the loop from the appropriate point (e.g., if `review/round_1.json` exists but no consensus, start round 2)

---

## Flow

### Step 1: Context Check

The main session computes the cache hash and checks whether assembly is needed.

**Cache key:** SHA-256 of:
- File contents of all `context.files` entries from the manifest
- Git commit SHA of `inference-sim` submodule HEAD
- Git commit SHA of `llm-d-inference-scheduler` submodule HEAD

**Cache location:** `workspace/context/<scenario>/<hash>.md`

`context.notes` from the manifest are excluded from the hash and are NOT written into the cached `context.md` file. Instead, the main session reads `context.notes` from `skill_input.json` and holds them in its own context window alongside `context.md` during the translate step. This means changing notes does not invalidate the cache — it only changes what the main session "knows" when writing code.

**On cache hit:** Report hash and submodule versions, skip to Step 2.

**On cache miss:** Spawn a **context-assembly subagent** (Claude Code Agent tool). The subagent gets a fresh context window, reads all necessary files, writes `context.md` to the cache path, and returns. The main session receives only the path — zero context cost.

**What the subagent assembles** (per parent spec Section 3):
- Full mapping document (sim signal → production equivalent)
- Key production interfaces and types from `llm-d-inference-scheduler` relevant to the scenario (e.g., `Scorer` interface, `EndpointPickerConfig` structure, `SchedulingContext`)
- Complete example production plugin files specified in the manifest
- Plugin registration pattern from `register.go`
- Any extra `context.files` entries

After the subagent writes the file, the main session updates `.state.json`:
```
phases.context = { "status": "done", "hash": "<hash>" }
```

### Step 2: Translate + Review Loop

This is the core of the skill. The main session handles it interactively — the user can steer, ask questions, and provide additional context.

```
Writer produces Go code + treatment_config.yaml
  → writes translation_output.json (with test_commands, helm_path, etc.)
        ↓
  [build/test gate] ──fail──→ Writer fixes → retry
        │ pass                      ↑
        ↓                           │
  Snapshot to snapshots/vN/         │
        ↓                           │
  Reviewer round N                  │
  (parallel external API calls)     │
        ↓                           │
  Writer refines code + config      │
        ↓                           │
  [build/test gate] ──fail──────────┘
        │ pass
        ↓
  Snapshot → next round or consensus
```

**Writer (initial translation):**
- Reads: cached `context.md` + algorithm source + algorithm config + `prompts/prepare/translate.md`. Also reads `context.notes` from `skill_input.json` (held in the session's context window, not appended to the cached file)
- Reads baseline config from `env_defaults.yaml` (`scenarios.<scenario>.gaie.baseline`) as structural reference
- Writes code directly into the target repo submodule (build/test needs files in place)
- Writes `treatment_config.yaml` to `$RUN_DIR/`
- Writes `translation_output.json` to `$RUN_DIR/` — the writer determines `test_commands`, `helm_path`, `config_kind`, `needs_custom_config`, `suggested_config`
- If the writer needs more context beyond `context.md`, it reads specific files directly (full file access in the skill session). The goal is that `context.md` covers the common case; targeted reads handle edge cases.
- The skill is a conversation: the writer explains its choices; the operator can intervene

**Build/test gate:**
- Reads `test_commands` from `translation_output.json` (the writer determines the right commands)
- Runs each command sequentially with CWD set to the target repo root
- On failure: structured error returned to the writer; writer fixes and retries
- Max 6 retries per build/test cycle
- After repeated failures: surfaces to the operator for decision
- The reviewer never sees code that does not compile and pass tests

**Snapshot:**
- Taken after every successful build/test pass — this includes the first build pass after the initial code write (before any review) AND each subsequent build pass after post-review fixes
- Contents: all files from `files_created` + `treatment_config.yaml`
- Naming: `snapshots/v1/`, `snapshots/v2/`, ... (monotonic, never reused)
- Purpose: diffing between revisions ("what changed after round 2 feedback?"). The final snapshot matches the live code in the submodule

**Reviewer loop:**
- **Models:** `Azure/gpt-4o`, `GCP/gemini-2.5-flash`, `aws/claude-opus-4-6` — called in parallel via `concurrent.futures` in `scripts/review.py`
- **Dev mode:** only `aws/claude-opus-4-6`
- **Each reviewer receives:** `prompts/prepare/review.md` prompt + algorithm source + `context.md` + generated Go code + treatment config
- **Each reviewer checks:**
  - Translation fidelity: does the production code faithfully implement the simulated algorithm?
  - Config correctness: does the treatment config match the generated plugin? Plugin types consistent? Profiles wired correctly?
  - Code quality: follows production patterns from context?
- **Each round:**
  - All models called in parallel
  - Each returns: APPROVE or NEEDS_CHANGES with specific issues
  - Results saved to `$RUN_DIR/review/round_N.json` — written once per round, never overwritten
  - Skill reports: "Round 1: 2/3 approved. Issues: [signal weight normalization]"
  - Writer refines code AND/OR config → build/test gate → next round
- **Consensus:** majority of successful (non-error) responses approve (≥2/3 or 1/1 in dev mode)
- **Error policy:** Model errors are non-votes
  - 2 APPROVE + 1 error → consensus (2/2 successful approved)
  - 1 APPROVE + 1 NEEDS_CHANGES + 1 error → no consensus (1/2)
  - 2 NEEDS_CHANGES + 1 error → no consensus (0/2)
  - 3 errors → no successful reviews; skill prompts: `[r]etry round / [a]ccept anyway / [q]uit`
- **Rounds exhausted without consensus:** `[c]ontinue / [a]ccept / [q]uit`
- **Default rounds:** 3; configurable via `--rounds N`
- The loop never exits silently; the operator always makes the final call

After consensus (or operator acceptance), update `.state.json`:
```
phases.translate = {
  "status": "done",
  "files": [...files_created...],
  "review_rounds": N,
  "consensus": "2/3"
}
```

### Step 3: Output

- Copy all created/modified files + `treatment_config.yaml` into `$RUN_DIR/generated/`
- Verify `translation_output.json` has all required fields
- Print completion summary:
  ```
  Plugin: adaptive-v2-scorer
  Files: pkg/plugins/scorer/adaptive_v2_scorer.go (created), pkg/plugins/register.go (modified)
  Review: 2/3 consensus after 3 rounds
  Snapshots: 3 versions

  Next: re-run pipeline/prepare.py to continue through Assembly, Summary, and Gate.
  ```

---

## Skill File Structure

```
.claude/skills/sim2real-translate/
├── SKILL.md                     ← orchestration instructions for Claude Code
└── scripts/
    └── review.py                ← multi-model reviewer (parallel threads, external API calls)
```

External prompt templates (shared with the pipeline, not bundled in the skill):
```
prompts/prepare/
├── translate.md                 ← writer guidance: how to read context, map signals, write code
└── review.md                    ← reviewer prompt: sent to external models each round
```

### `SKILL.md` structure

The skill markdown is organized as sequential steps with resumability checks at each boundary:

1. **Arguments** — parse `--rounds N`, `--dev`
2. **Prerequisites** — verify `skill_input.json` exists, load fields
3. **Resumability check** — read `.state.json`, report completed steps, skip to first incomplete
4. **Step 1: Context** — compute hash, check cache, spawn subagent if miss
5. **Step 2: Translate** — read context + algorithm + translate.md, write code + config + translation_output.json
6. **Step 3: Build/Test** — run test_commands, retry on failure, snapshot on pass
7. **Step 4: Review** — run review.py, interpret results
8. **Loop instruction** — "If NEEDS_CHANGES: return to Step 2 to fix issues, then Step 3, then Step 4"
9. **Step 5: Output** — copy to generated/, verify, update state, print summary

### `review.py`

Self-contained Python script bundled with the skill. Responsibilities:
- Reads `prompts/prepare/review.md` as the reviewer prompt template
- Assembles review payload: prompt + algorithm source + context.md + generated Go code + treatment config
- Calls all models in parallel via `concurrent.futures.ThreadPoolExecutor`
- Each model called via HTTP (OpenAI-compatible `/v1/chat/completions` endpoint)
- Parses structured JSON responses from each model
- Computes consensus (majority of successful responses)
- Writes `$RUN_DIR/review/round_N.json`
- Prints summary to stdout
- Exit codes: 0 = consensus, 1 = no consensus, 2 = all errors

CLI interface:
```bash
python3 .claude/skills/sim2real-translate/scripts/review.py \
    --plugin-files FILE [FILE ...] \
    --algorithm FILE \
    --algorithm-config FILE \
    --context FILE \
    --treatment-config FILE \
    --round N \
    --out FILE \
    [--dev]
```

---

## Context Assembly Subagent

Spawned via the Claude Code `Agent` tool when the context cache misses. Gets a fresh, isolated context window.

**Prompt construction:** The main session reads `skill_input.json` and the manifest to extract all paths, then constructs the subagent prompt with concrete values (no placeholders):

```
You are assembling a translation context document for the sim2real pipeline.

Read the following files and write a single context.md document to:
  <output_path>    (e.g., workspace/context/routing/a1b2c3.md)

1. Mapping document: <context.files[0]>    (e.g., docs/transfer/blis_to_llmd_mapping.md)
2. Production interfaces relevant to the "<scenario>" scenario from:
   <target.repo>/<target.plugin_dir>    (e.g., llm-d-inference-scheduler/pkg/plugins/scorer/)
   Read the Scorer interface (or Admission interface for admission_control) and
   key types like EndpointPickerConfig, SchedulingContext.
3. One or two existing plugin examples from the same directory — pick the most
   representative ones (e.g., load_aware.go for routing, always_admit.go for admission).
4. Plugin registration pattern from:
   <target.register_file>    (e.g., llm-d-inference-scheduler/pkg/plugins/register.go)
5. Any additional context.files entries: <context.files[1:]>

Format the output as:

# Translation Context
Scenario: <scenario> | inference-sim@<sha> | llm-d@<sha>

## Signal Mapping
[contents of mapping document]

## Production Interfaces
[relevant interface definitions and types]

## Example Plugin: <name>
[contents of example plugin]

## Plugin Registration
[contents of register.go]

## <additional file name>
[contents]

Do NOT include context.notes — those are held in the main session's context
window separately. Write the file and report the path.
```

**Cache invalidation:** The main session computes the hash BEFORE spawning the subagent. If the file at `workspace/context/<scenario>/<hash>.md` exists, no subagent is spawned. The hash changes when:
- Any `context.files` content changes
- `inference-sim` submodule HEAD changes
- `llm-d-inference-scheduler` submodule HEAD changes

**Explicit rebuild:** `--rebuild-context` flag on the skill invocation forces cache miss.

---

## Interaction with `pipeline/prepare.py`

The skill sits at prepare.py's Phase 3 (Translation Checkpoint). The handoff:

1. User runs `python pipeline/prepare.py`
2. prepare.py executes Phases 1–2 (Init, Context — may build/cache context.md)
3. Phase 3: writes `skill_input.json`, checks for `translation_output.json`
4. If no output: prints checkpoint message, exits 0
5. User invokes `/sim2real-translate` in Claude Code
6. Skill runs Steps 1–3, produces `translation_output.json` + `treatment_config.yaml` + artifacts
7. User re-runs `python pipeline/prepare.py`
8. Phase 3: finds `translation_output.json`, validates, advances
9. Phases 4–6: Assembly, Summary, Gate

**Context cache sharing:** Both prepare.py Phase 2 and the skill's Step 1 use the same cache location (`workspace/context/<scenario>/<hash>.md`) and the same hash algorithm (`pipeline/lib/context_builder.py:compute_context_hash`). If prepare.py already built the context, the skill gets a cache hit. If the skill builds it first (e.g., user skips prepare.py Phase 2), prepare.py gets a cache hit on next run. Neither duplicates work.

**State file sharing:** Both prepare.py and the skill write to the same `.state.json` in the run directory via `pipeline/lib/state_machine.py`. prepare.py writes phases `init`, `assembly`, `summary`, `gate`. The skill writes phases `context`, `translate`. No conflicts since they write different keys.

---

## What Changes vs. Current Skill

| Current skill | Redesigned skill |
|---------------|-----------------|
| Manual bash parsing of `skill_input.json` fields | Python or inline reads; SKILL.md references fields by name |
| Ad-hoc state checks (file existence, snapshot counts) | `pipeline/lib/state_machine.py` with `.state.json` |
| Context assembled every invocation in main session | Subagent on cache miss; cached by content hash + submodule SHAs |
| `translation_output.json` has 6 fields | 10 fields per spec (`test_commands`, `helm_path`, `needs_custom_config`, etc.) |
| Build commands from `skill_input.json` | Writer determines `test_commands` in `translation_output.json` |
| Standalone `dashboard.py` for progress | Claude Code task indicators (TaskCreate/TaskUpdate) |
| `build_review_request.py` + `review_translation.py` | Single `review.py` with parallel threads |
| Treatment config always written by writer | Writer writes config; `needs_custom_config` flag tells prepare.py if it's non-standard |

---

## Out of Scope

- `prompts/prepare/translate.md` content (writer prompt) — separate design task
- `prompts/prepare/review.md` content (reviewer prompt) — separate design task
- Changes to `pipeline/prepare.py` (already implemented per the pipeline redesign plan)
- Changes to `pipeline/lib/state_machine.py` or `pipeline/lib/context_builder.py`
- AI config checker (Step 5 of parent spec) — owned by prepare.py
- `scripts/deploy.py`, `scripts/analyze.py`
