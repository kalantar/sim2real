# sim2real-translate: YAML Generation + Expert Agent Redesign

**Date:** 2026-04-10
**Status:** Draft

## Problem

The current `sim2real-translate` skill has three gaps:

1. **YAML generation is unguided.** The writer produces `treatment_config.yaml` from scratch with no structured path from the simulation config to the real EPP config. There is no baseline derivation step, so the treatment config is disconnected from what the baseline actually looks like in the real world.

2. **Context assembly is static.** `context.files` are appended verbatim. The writer and reviewer have no way to ask questions about the live repos mid-task. If they need to know the exact signature of a GAIE interface, they either guess or get it wrong.

3. **No user feedback loop.** The skill runs to completion and presents results at the end. Users cannot course-correct on the baseline config, the treatment config, or the generated code before the next stage begins.

## Design

### 1. `transfer.yaml` v3 Structure

```yaml
kind: sim2real-transfer
version: 3

scenario: adaptive-routing

algorithm:
  source: sim2real_golden_correct/routers/router_adaptive.go
  config: sim2real_golden_correct/routers/policy_adaptive.yaml

baseline:
  sim:
    config: sim2real_golden_correct/routers/policy_baseline_322.yaml
  real:
    config: sim2real_golden_correct/routers/baseline_epp_template.yaml
    notes: |
      Free-text hints about how to translate sim scorer fields to real EPP YAML.
      E.g. "use EndpointPickerConfig.Scorers[], each entry has Name + Weight."

workloads:
  - sim2real_golden_correct/workloads/workload_fm8_short_output_highrate.yaml

llm_config: admission_by60/llm_config.yaml

hints:
  files:
    - path: sim2real_golden_correct/README.md
    - path: sim2real_golden_correct/transfer_hint.md
  notes: "..."

context:
  files:
    - blis-context.md
    - gaie_context.md
    - llm-d-inference-scheduler-context.md
```

**Key changes from v2:**
- `baseline.config` → `baseline.sim.config` (rename, same meaning)
- New `baseline.real.config` — user-provided YAML template showing the real EPP structure with placeholder values; skill fills in actual values from `baseline.sim.config`
- New `baseline.real.notes` — free-text translation hints alongside the template

`prepare.py` reads the new fields and includes them in `skill_input.json`.

**v2 → v3 migration:** If `manifest.version == 2`, `prepare.py` reads `baseline.config` as `baseline.sim.config` and treats `baseline.real.config` and `baseline.real.notes` as absent. Absent `baseline.real.*` fields are valid — the skill skips Phase 2's template-filling step and goes straight to user presentation with a warning. In-progress v2 runs are not affected; their state files have no `baseline_derivation` phase and the skill enters Phase 4 directly.

### 2. Three-Agent Team

The team now has three members: **Writer**, **Reviewer**, and **Expert**.

#### Expert Agent

The Expert is spawned first and stays alive for the entire skill run (no re-initialization, no token waste on re-spawning).

**Initialization sequence:**
1. Read `transfer.yaml` in full — baseline.sim.config, baseline.real.config template, baseline.real.notes, hints, algorithm paths
2. Read all `context.files` as foundation
3. Targeted exploration of all three repos using Glob/Grep/Read — guided by what `context.files` and the algorithm/baseline configs reference, not exhaustive file-by-file reads:
   - **inference-sim:** scorer types, signal names, and metric definitions referenced in `algorithm.config` and `baseline.sim.config`; follow symbol names to find their definitions
   - **llm-d-inference-scheduler:** `pkg/plugins/` directory structure (Glob first), then read interfaces and any existing scorers/filters/pickers/profiles that are relevant to the scenario; plugin registration pattern
   - **GAIE upstream:** full architecture breadth — request lifecycle (filter → scorer → picker → profile), admission control, runner/config loading system (`WithSchedulerConfig` vs YAML loader), all framework interfaces under `pkg/epp/framework/interface/`, `EndpointPickerConfig` and related config structs. GAIE architecture is read broadly because its design constraints affect what the writer can and cannot do.

Expert does not read every file in every repo. It reads what the context references, follows those symbols to their definitions, and fills gaps on demand when queried.

**Query protocol:** Writer and Reviewer send freeform natural-language questions to Expert via `SendMessage`. Expert searches the live repos and replies with grounded answers + file references (file path + line number). If multiple questions arrive before Expert has replied to the first, Expert answers them in order — no parallelism required. Expert maintains a single shared context (not per-requester threads); both Writer and Reviewer see the same accumulated knowledge.

**Tools:** Glob, Grep, Read only.

#### Writer Agent

Owns the build/test gate and all file generation. Consults Expert on demand. Reports to main session at user pause points.

#### Reviewer Agent

Spawned idle at skill start. Receives review requests from Writer. Can query Expert before issuing verdict. Returns APPROVE or NEEDS_CHANGES.

### 3. Skill Phase Flow

Four phases tracked in `.state.json`. Each phase is independently resumable.

```
Phase 1: Team Init
  → spawn Expert (loads context + targeted repo exploration)
  → spawn Reviewer (idle)

Phase 2: Baseline Config Derivation
  → Writer reads baseline.sim.config + baseline.real.config template + baseline.real.notes
  → Writer consults Expert on mapping questions
  → Writer derives baseline_config.yaml (real EPP format, actual parameter values)
  → Main session presents to user → user approves or provides feedback
  → Loop until user types 'done'

Phase 3: Treatment Config Derivation
  → Writer reads approved baseline_config.yaml + algorithm.config + algorithm.source
  → Writer identifies delta: which scorers change, which weights/thresholds change, new logic
  → Writer derives treatment_config.yaml — always functional YAML (code must read from it)
  → Main session presents to user → user approves or provides feedback
  → Loop until user types 'done'

Phase 4: Code + Review Loop
  → Writer generates Go plugin code that reads parameters from treatment_config.yaml
  → Build/test gate (6-retry max)
  → Reviewer reviews code + both YAMLs (can query Expert)
  → On APPROVE: main session surfaces code + baseline_config.yaml + treatment_config.yaml to user
      → "Round N passed review. Provide feedback or type 'done' to finish."
      → If feedback: new round (APPROVE is reset; must pass review again). If 'done': proceed to output.
  → Escalate / build-failed paths unchanged
```

**Resumability on skill re-entry:**
- Phase 2 or 3 in-progress: if `$RUN_DIR/baseline_config.yaml` (or `treatment_config.yaml`) already exists, re-present the existing file to the user rather than re-deriving from scratch.
- Phase 2 or 3 with no artifact: restart derivation from the beginning of that phase.
- Phase 4 in-progress: resume at the existing snapshot + review round (existing behavior unchanged).

**.state.json phases:**

```json
{
  "phases": {
    "context": { "status": "done", "context_file_populated": true },
    "baseline_derivation": { "status": "done", "user_approved": true },
    "treatment_derivation": { "status": "done", "user_approved": true },
    "translate": { "status": "done", "files": [...], "review_rounds": 2, "consensus": "approved" }
  }
}
```

### 4. User Pause Protocol

At each pause point (end of Phase 2, end of Phase 3, after each reviewer APPROVE in Phase 4), the main session prints the relevant artifacts inline and asks:

```
━━━ Baseline Config (derived from sim 3:2:2 → real EPP) ━━━
<contents of baseline_config.yaml>

Provide feedback to revise, or type 'done' to proceed.
```

If the user provides feedback, it is forwarded to the Writer as a new instruction. The Writer revises and re-presents (no reviewer round needed for Phase 2/3). Phase 4 feedback triggers a new build/test → review round.

### 5. Treatment Config Constraint

`treatment_config.yaml` must always be a **functional YAML** that the deployed Go code actually reads at runtime. It is never documentation-only.

If the algorithm uses a code-driven profile (e.g., `WithSchedulerConfig`), the Go code must be written to read its parameters (thresholds, weights, scorer selection) from the treatment YAML rather than hardcoding them. The Writer is responsible for ensuring this constraint is met. The Reviewer checks it.

This prevents a situation where the treatment YAML and the actual system behavior diverge, which would confuse operators.

**Reviewer check:** After each build pass, the Reviewer verifies the constraint mechanically:
1. For every numeric threshold and weight in `algorithm.config`, confirm a corresponding field exists in `treatment_config.yaml`.
2. Confirm the plugin Go file unmarshals or reads from that YAML field (look for a config struct with yaml tags, or a call to a config-loading function).
3. Flag as NEEDS_CHANGES if any scoring threshold or weight appears as a numeric literal in the Go code without a corresponding YAML field.
4. Compile-time constants for framework-level values (buffer sizes, timeouts unrelated to scoring logic) are allowed and do not need to appear in the treatment YAML.

### 6. Output Artifacts

New artifact added to the run directory:

| File | Phase | Description |
|------|-------|-------------|
| `baseline_config.yaml` | 2 | Real EPP YAML for baseline, derived from sim config + user template |
| `treatment_config.yaml` | 3 | Real EPP YAML for treatment, functional (code reads from it) |
| `translation_output.json` | 4 | Existing: plugin metadata, files created/modified |
| `generated/` | 4 | Existing: copies of all plugin files |

`baseline_config.yaml` is also copied into `generated/` alongside the plugin files.

## Implementation Impact

Files requiring changes:

| File | Change |
|------|--------|
| `.claude/skills/sim2real-translate/SKILL.md` | Spawn Expert first; add Phase 2/3 derivation steps and user pause points before existing review loop |
| `scripts/prepare.py` | Read `baseline.real.config` + `baseline.real.notes` from manifest; include in `skill_input.json` |
| `scripts/lib/manifest.py` | Validate `baseline.real.config` path exists if specified; no error if absent (optional field) |
| `prompts/prepare/agent-writer.md` | Add Phase 2 (baseline derivation) and Phase 3 (treatment derivation) instructions; add Expert query protocol |
| `prompts/prepare/agent-reviewer.md` | Add treatment config constraint check (Section 5); add Expert query protocol |
| New: `prompts/prepare/agent-expert.md` | New prompt for Expert agent initialization and query handling |

New artifact in `skill_input.json`:
```json
{
  "baseline_sim_config": "<path>",
  "baseline_real_config": "<path or null>",
  "baseline_real_notes": "<text or null>"
}
```

## What Does Not Change

- Build/test gate logic (6 retries, same commands)
- Snapshot mechanism (taken after every green build)
- Escalate / build-failed / accept-without-consensus paths
- `generated/` copy step
- prepare.py phases after translate (Assembly, Summary, Gate)
- `translation_output.json` schema (existing artifact, unchanged)
