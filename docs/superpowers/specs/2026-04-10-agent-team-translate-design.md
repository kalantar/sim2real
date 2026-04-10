# Agent Team Design: sim2real-translate Writer + Reviewer

**Date:** 2026-04-10
**Status:** Approved
**Replaces:** `scripts/review.py` multi-model external LLM loop

---

## Problem

The current translate skill runs the writer (main Claude session) and reviewer
(review.py calling external LLMs) in a loop, but the writer never receives
reviewer feedback in its context window. Each round re-reads the same original
inputs. This causes repeated mistakes across rounds and was the root cause of
the `adaptive4` run completing with unresolved CRITICAL issues.

---

## Design: Writer-led loop, Reviewer as oracle

The main session stays responsible for context building (Phase 2) and output
artifact collection (Phase 6). Steps 2–5 of the skill (translate, build/test,
snapshot, review loop) are replaced by a two-agent team.

### Architecture

```
Main Session
├── Phase 1: Context building — unchanged (reads llm-d + inference-sim repos)
├── Phase 2: TeamCreate + spawn Reviewer (idle) + spawn Writer (active)
│   └── waits for Writer to signal "done" or "escalate"
└── Phase 3: copy to generated/, update state.json, summary — unchanged

Writer Agent  (general-purpose)
├── Owns: Go code authoring, build/test gate, snapshots, round loop
├── Sends: review request to Reviewer after each green build
├── Receives: structured feedback in its own conversation context (accumulated)
└── Signals: Main when approved or rounds exhausted

Reviewer Agent  (general-purpose)
├── Stays idle until Writer sends a review request
├── On each request: reads current Go files + treatment_config.yaml fresh
├── Checks: fidelity, code quality, registration, config correctness,
│          AND assembly simulation (see Config Check section)
└── Replies: APPROVE or NEEDS_CHANGES with specific issues directly to Writer
```

The key fix: reviewer feedback arrives as a `SendMessage` reply inside the
writer's own conversation, so every subsequent iteration has full recall of
every prior issue and what was done about it.

---

## Agent Initialization

Both agents are spawned by the main session with context injected at spawn
time. They share the same base context but play opposite roles.

### Writer agent — `prompts/prepare/agent-writer.md`

Injected at spawn:
- `skill_input.json` fields: run_dir, context_path, algorithm_source,
  algorithm_config, hints (text + file contents)
- Translation guidance from `translate.md` (incorporated)
- Loop protocol: after each green build → SendMessage(Reviewer) → wait →
  fix if NEEDS_CHANGES → next round
- Round budget (default 3), escalation instruction when exhausted
- Snapshot instructions (vN after each green build)

### Reviewer agent — `prompts/prepare/agent-reviewer.md`

Injected at spawn:
- Same base: context_path, algorithm_source, algorithm_config, hints
- Review criteria from `review.md` (incorporated, hardened — see below)
- Run artifacts for config validation (read fresh on each review):
  - `config/env_defaults.yaml` — contains the baseline EPP config under
    `stack.<scenario>.gaie.baseline.helmValues.inferenceExtension.pluginsCustomConfig["custom-plugins.yaml"]`;
    this is the canonical shape reference (note: `values.yaml` does not exist
    yet during Phase 3 translation — it is generated in Phase 4 Assembly)
  - `translation_output.json` — for cross-referencing `plugin_type`,
    `config_kind`, `treatment_config_generated`, `register_file`
  - `llm-d-inference-scheduler/pkg/plugins/register.go`

---

## Communication Protocol

### Writer → Reviewer

```
REVIEW REQUEST — Round N
Plugin files: [list of absolute paths]
Treatment config: {run_dir}/treatment_config.yaml
Build: PASSED
Changed since last round: <summary or "initial">
```

### Reviewer → Writer

```
VERDICT: APPROVE | NEEDS_CHANGES

[if NEEDS_CHANGES]
Issues:
1. [category] Description
   File: path/to/file.go, Line: ~N
   Fix: specific correction
...
```

Categories: `fidelity`, `config`, `registration`, `code-quality`, `assembly`

The writer reads feedback as natural text accumulated in its conversation
context — no JSON parsing required.

---

## Reviewer: Exceptionally Critical Stance

The reviewer is biased toward NEEDS_CHANGES. To issue APPROVE it must
explicitly verify every criterion below and find no violations. When in
doubt, it raises an issue.

### Fidelity
- Every signal from the mapping document is used
- All thresholds and weights are preserved from the algorithm config
- Regime detection or scoring logic exactly matches the source algorithm

### Code Quality
- Interfaces correctly implemented (Scorer, ProfileHandler, etc.)
- No implicit assumptions about scorer ordering unless explicitly documented
- Production patterns from context document are followed

### Registration
- Plugin has a `Type` constant matching `plugin_type` in `translation_output.json`
- Plugin has a `Factory` function
- `register.go` contains `plugin.Register(pkg.TypeConst, pkg.FactoryFunc)`
- Type string is consistent across: Go constant, register.go call,
  treatment_config.yaml, and translation_output.json

### Config Correctness + Assembly Simulation

This is the most critical check. The reviewer must:

1. **Read `config/env_defaults.yaml`** → locate the baseline EPP config
   embedded as a string at:
   `stack.<scenario>.gaie.baseline.helmValues.inferenceExtension.pluginsCustomConfig["custom-plugins.yaml"]`
   → parse that string as YAML → this is the canonical shape the treatment
   config must follow. (`values.yaml` is generated in Phase 4 Assembly and
   does not exist at review time.)

2. **Simulate the embed**: `prepare.py` embeds `treatment_config.yaml` as raw
   text at the hardcoded path:
   `stack.gaie.treatment.helmValues.inferenceExtension.pluginsCustomConfig["custom-plugins.yaml"]`
   The reviewer must verify:
   - The treatment config is **valid YAML** (no syntax errors, no characters
     that break PyYAML serialization)
   - When deserialized, it produces a structurally valid `EndpointPickerConfig`
   - `kind` matches `config_kind` in `translation_output.json`
   - `treatment_config_generated: true` is set in `translation_output.json`
     when a custom config was written

3. **Key/value adherence**: every key in the treatment config must either
   appear in the baseline config or be explicitly justified. No invented keys
   that would be silently ignored by the EPP.

4. **Cross-reference `translation_output.json`**: every `plugin_type`
   referenced in the config is registered in `register.go`.

Any config issue is raised as `[assembly]` category NEEDS_CHANGES — not a
warning.

---

## Loop Flow and Stopping Conditions

```
Main: TeamCreate("translate-{run_name}")
Main: Spawn Reviewer with agent-reviewer.md + injected artifacts
Main: Spawn Writer with agent-writer.md + injected args

Writer loop (rounds 1..REVIEW_ROUNDS):
  Write/fix Go files in target repo
  Run build/test (up to 6 retries)
    → if all retries fail: SendMessage(Main, "build-failed: <error>") → exit
  Take snapshot vN
  SendMessage(Reviewer, review request round N)
  Wait for reply

  APPROVE  → write translation_output.json (10 required fields — see schema below)
             write review/round_N.json with reviewer verdict
             update .state.json (review_rounds=N, consensus="approved")
             SendMessage(Main, "done") → exit

  NEEDS_CHANGES + round < REVIEW_ROUNDS
           → fix code using full accumulated review history → next round

  NEEDS_CHANGES + round == REVIEW_ROUNDS
           → SendMessage(Main, "escalate: N rounds exhausted, issues: ...") → exit

Main on "done":
  Copy files to generated/, update state.json, print summary
  Shutdown Writer + Reviewer, TeamDelete

Main on "escalate":
  Prompt operator: [c]ontinue N more / [a]ccept / [q]uit

Main on "build-failed":
  Surface build error to operator
```

---

## Artifact Schemas

### `translation_output.json` (10 required fields)

```json
{
  "plugin_type": "<kebab-case type name>",
  "files_created": ["pkg/plugins/profile/foo.go"],
  "files_modified": ["pkg/plugins/register.go"],
  "package": "<Go package name>",
  "register_file": "<path relative to target repo, or null>",
  "test_commands": [["go", "build", "./pkg/plugins/<pkg>/..."]],
  "config_kind": "EndpointPickerConfig",
  "helm_path": "gaie.treatment.helmValues.inferenceExtension.pluginsCustomConfig.custom-plugins.yaml",
  "treatment_config_generated": true,
  "description": "<one-line summary>"
}
```

`review_rounds` and `consensus` are **not** fields in this file — they are
stored as metadata in `.state.json` by the state machine.

### `review/round_N.json`

The writer persists each reviewer reply in the existing format that prepare.py
Phase 6 summary consumes:

```json
{
  "round": N,
  "consensus": true | false,
  "approve_count": 0 | 1,
  "total_successful": 1,
  "reviews": [
    {
      "model": "agent-reviewer",
      "verdict": "APPROVE | NEEDS_CHANGES",
      "issues": [],
      "summary": "..."
    }
  ]
}
```

### Skill allowed-tools

The updated SKILL.md must declare `SendMessage`, `TeamCreate`, and `TeamDelete`
in its `allowed-tools` list (in addition to the existing tool set) to enable
the agent team pattern.

---

## Files Created / Changed

| File | Action |
|------|--------|
| `prompts/prepare/agent-writer.md` | New — writer agent prompt |
| `prompts/prepare/agent-reviewer.md` | New — reviewer agent prompt |
| `.claude/skills/sim2real-translate/SKILL.md` | Modified — Steps 2–5 replaced |
| `.claude/skills/sim2real-translate/scripts/review.py` | Retired |

`prompts/prepare/translate.md` and `prompts/prepare/review.md` are retained
as reference material but superseded by the agent prompts.

---

## What Does Not Change

- `prepare.py` — no changes; it writes `skill_input.json` and reads
  `translation_output.json` exactly as before
- Context building (Phase 2 of skill) — main session assembles context.md
  from llm-d + inference-sim repos, unchanged
- Output artifact collection (Phase 6) — main session copies to `generated/`,
  updates `state.json`, prints summary, unchanged
- Snapshot format — `workspace/runs/{run}/snapshots/vN/` unchanged
- Round review JSON — `workspace/runs/{run}/review/round_N.json` format
  unchanged (writer persists reviewer replies in this format)
