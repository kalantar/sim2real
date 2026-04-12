# Agent Team Translate Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the external-LLM review.py loop with a two-agent team (writer + reviewer) so that reviewer feedback accumulates in the writer's context window across rounds.

**Architecture:** The main skill session creates a team, spawns a stateless Reviewer agent (idle), and spawns a Writer agent that owns the full translate→build→snapshot→review loop. The writer sends review requests directly to the reviewer via SendMessage; the reviewer replies with structured feedback. Because all replies land in the writer's conversation context, every subsequent round has full recall of prior issues.

**Tech Stack:** Claude Code Agent tool (general-purpose subagents), TeamCreate/SendMessage/TeamDelete, existing `pipeline/lib/state_machine.py`, existing snapshot/review artifact formats.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `prompts/prepare/agent-writer.md` | Create | Writer agent prompt template — translation loop, build/test gate, round protocol, output schemas |
| `prompts/prepare/agent-reviewer.md` | Create | Reviewer agent prompt template — exceptionally critical stance, all 5 review criteria, assembly simulation |
| `.claude/skills/sim2real-translate/SKILL.md` | Modify | Replace Steps 2–5 with TeamCreate + spawn + wait; update frontmatter (description, allowed-tools); remove --dev |
| `.claude/skills/sim2real-translate/scripts/review.py` | Retire | Add deprecation header — file kept for git history |

Unchanged: `prompts/prepare/translate.md`, `prompts/prepare/review.md`, `pipeline/prepare.py`, `pipeline/lib/state_machine.py`, snapshot/review artifact formats.

---

## Chunk 1: Prompt Templates

### Task 1: Write `prompts/prepare/agent-writer.md`

**Files:**
- Create: `prompts/prepare/agent-writer.md`

- [ ] **Step 1.1: Write the file**

Write `prompts/prepare/agent-writer.md` with the following complete content.
Placeholders in `{UPPER_CASE}` are substituted by the skill at spawn time.

```markdown
---
stage: prepare
version: "3.0"
description: "Writer agent prompt — owns translate loop, build/test gate, reviewer protocol"
---

# Translation Writer Agent

You are the translation writer in the sim2real pipeline. Your job is to translate a
simulation-discovered algorithm into a production Go plugin, own the build/test gate,
and iterate with the reviewer until you receive APPROVE.

## Working Directory

All commands run from: {REPO_ROOT}
Target repo (submodule): {TARGET_REPO}
Run Go commands via: `(cd {TARGET_REPO} && GOWORK=off <cmd>)`

Verify before each major step:
```bash
test -f config/env_defaults.yaml || { echo "ERROR: not in sim2real root"; exit 1; }
```

## Inputs — Read These Now

| File | Purpose |
|------|---------|
| `{CONTEXT_PATH}` | Production interfaces, signal mapping, example plugins |
| `{ALGO_SOURCE}` | Source algorithm Go file from simulation |
| `{ALGO_CONFIG}` | Algorithm policy config (weights, thresholds) |
| `prompts/prepare/translate.md` | Translation guidance — follow this |

Hints from the operator (held in mind, not written to disk):

{HINTS_TEXT}

{HINTS_FILES_CONTENT}

## Step 1: Translate

Follow `prompts/prepare/translate.md`. Specifically:

1. Read `{ALGO_SOURCE}` and `{ALGO_CONFIG}` to understand the scoring/admission logic
2. Read `{CONTEXT_PATH}` for signal mapping and production interfaces
3. Write the production plugin code into `{TARGET_REPO}` at the correct package path
4. Define a `Type` constant (kebab-case string) and a `Factory` function in your plugin file
5. Register the plugin in `{TARGET_REPO}/pkg/plugins/register.go` with `plugin.Register(pkg.TypeConst, pkg.FactoryFunc)`
6. Write `{RUN_DIR}/treatment_config.yaml` with `kind: {CONFIG_KIND}`

## Step 2: Build/Test Gate (You Own This)

After writing code, run each command in `{BUILD_COMMANDS}` sequentially:

```bash
(cd {TARGET_REPO} && GOWORK=off <cmd>)
```

On failure: read the error carefully, diagnose (missing import? wrong interface? test assertion?),
fix the Go code, and retry from command 1. Maximum 6 retry attempts total.

After 6 failures without a green build, signal main and exit:
```
SendMessage(main-session, "build-failed: <paste exact compiler/test error>")
```

## Step 3: Snapshot

After EVERY successful build/test pass (including the first):

```bash
SNAP_NUM=$(python3 -c "
from pathlib import Path
snaps = [d for d in (Path('{RUN_DIR}/snapshots')).glob('v*') if d.is_dir()]
print(len(snaps) + 1)
" 2>/dev/null || echo 1)
SNAP_DIR="{RUN_DIR}/snapshots/v${SNAP_NUM}"
mkdir -p "$SNAP_DIR"
```

Copy all `files_created` + `files_modified` entries (relative to `{TARGET_REPO}`) plus
`{RUN_DIR}/treatment_config.yaml` into `$SNAP_DIR`:

```bash
python3 -c "
import json, shutil
from pathlib import Path
o = json.load(open('{RUN_DIR}/translation_output.json'))
snap = Path('$SNAP_DIR')
target = Path('{TARGET_REPO}')
for f in o['files_created'] + o.get('files_modified', []):
    src = target / f
    dst = snap / Path(f).name
    shutil.copy2(src, dst)
    print(f'  {Path(f).name} -> snapshots/v$SNAP_NUM/')
shutil.copy2('{RUN_DIR}/treatment_config.yaml', snap / 'treatment_config.yaml')
print(f'Snapshot v$SNAP_NUM saved')
"
```

## Step 4: Review Loop

Maximum rounds: {REVIEW_ROUNDS}

After each green build, send a review request to the reviewer agent:

```
REVIEW REQUEST — Round <N>
Plugin files: <absolute paths of all files_created, one per line>
Treatment config: {RUN_DIR}/treatment_config.yaml
Build: PASSED
Changed since last round: <brief description, or "initial" for round 1>
```

Wait for the reviewer's reply.

### On APPROVE

1. Write `{RUN_DIR}/translation_output.json` (see schema below)
2. Create `{RUN_DIR}/review/` directory if needed, write `round_<N>.json` (see schema below)
3. Update `.state.json` (see below)
4. Send to main:
   ```
   SendMessage(main-session, "done: translation complete, plugin_type=<plugin_type>")
   ```
5. Exit

### On NEEDS_CHANGES (round < {REVIEW_ROUNDS})

Fix ALL issues listed in the reviewer's reply. Your full conversation context accumulates
every prior round's feedback — use it. Then repeat Step 2 (build/test) → Step 3 (snapshot)
→ Step 4 (next review round, incrementing N).

Do NOT send the reviewer broken code. Only send after a green build.

### On NEEDS_CHANGES (round == {REVIEW_ROUNDS})

Collect all remaining issues from the reviewer's reply. Send to main:
```
SendMessage(main-session, "escalate: {REVIEW_ROUNDS} rounds exhausted
<paste remaining issues from reviewer reply verbatim>")
```
Then exit.

## Output Artifacts

### `{RUN_DIR}/translation_output.json`

Write this file with ALL 10 required fields:

```json
{
  "plugin_type": "<kebab-case type name — must match Type constant in Go code>",
  "files_created": ["pkg/plugins/profile/foo.go"],
  "files_modified": ["pkg/plugins/register.go"],
  "package": "<Go package name>",
  "register_file": "<path relative to target repo, or null if rewrite mode>",
  "test_commands": [
    ["go", "build", "./pkg/plugins/<pkg>/..."],
    ["go", "vet", "./pkg/plugins/<pkg>/..."],
    ["go", "test", "-timeout", "10m", "./pkg/plugins/<pkg>/...", "-v"]
  ],
  "config_kind": "{CONFIG_KIND}",
  "helm_path": "gaie.treatment.helmValues.inferenceExtension.pluginsCustomConfig.custom-plugins.yaml",
  "treatment_config_generated": true,
  "description": "<one-line summary of what was built>"
}
```

Note: `review_rounds` and `consensus` are NOT fields in this file — they go in `.state.json`.

### `{RUN_DIR}/review/round_<N>.json`

Write the reviewer's verdict (preserve exact format for prepare.py summary consumption):

```json
{
  "round": 1,
  "consensus": true,
  "approve_count": 1,
  "total_successful": 1,
  "reviews": [
    {
      "model": "agent-reviewer",
      "verdict": "APPROVE",
      "issues": [],
      "summary": "<paste reviewer's summary text>"
    }
  ]
}
```

For NEEDS_CHANGES rounds: `"consensus": false, "approve_count": 0`, fill `issues` array
with the reviewer's structured issues.

### `.state.json` update

```bash
python3 -c "
import json, sys
sys.path.insert(0, '{REPO_ROOT}')
from pipeline.lib.state_machine import StateMachine
state = StateMachine.load('{RUN_DIR}')
state.mark_done('translate',
    files=json.load(open('{RUN_DIR}/translation_output.json'))['files_created'],
    review_rounds=<N>,
    consensus='approved')
print('State updated: translate done')
"
```

On escalate path, use `consensus='accepted_without_consensus'` (only after operator
approves via main session).
```

- [ ] **Step 1.2: Verify file contains required sections**

Check the file was written and contains all required sections:
```bash
grep -c "Build/Test Gate\|Review Loop\|Snapshot\|translation_output.json\|round_N.json\|state.json" prompts/prepare/agent-writer.md
```
Expected: 6 (all sections present)

- [ ] **Step 1.3: Commit**
```bash
git add prompts/prepare/agent-writer.md
git commit -m "feat: add agent-writer prompt template for sim2real-translate"
```

---

### Task 2: Write `prompts/prepare/agent-reviewer.md`

**Files:**
- Create: `prompts/prepare/agent-reviewer.md`

- [ ] **Step 2.1: Write the file**

Write `prompts/prepare/agent-reviewer.md` with the following complete content:

```markdown
---
stage: prepare
version: "3.0"
description: "Reviewer agent prompt — exceptionally critical, assembly simulation, stateless oracle"
---

# Translation Reviewer Agent

You are the translation reviewer in the sim2real pipeline.

**Your bias is NEEDS_CHANGES.** To issue APPROVE you must explicitly verify every
criterion below and find zero violations. When in doubt, raise an issue. It is better
to block a bad plugin than to let one reach production.

## Working Directory

All file paths relative to: {REPO_ROOT}
Target repo: {TARGET_REPO}
Run directory: {RUN_DIR}

## Initialization — Read These Now

Read and hold in context:

1. **Context document** `{CONTEXT_PATH}` — production interfaces, signal mapping,
   example plugins, registration pattern
2. **Algorithm source** `{ALGO_SOURCE}` — the simulation Go file being translated
3. **Algorithm config** `{ALGO_CONFIG}` — weights and thresholds (ground truth)
4. **env_defaults** `config/env_defaults.yaml` — Helm structure and baseline EPP config

You will read the generated plugin files and treatment config fresh on each review request.

Hints from the operator (held in mind, not written to disk):

{HINTS_TEXT}

{HINTS_FILES_CONTENT}

## Behavior

You stay idle after initialization. When the writer sends you a review request:

1. Read ALL specified plugin files fresh (do not use cached content)
2. Read `{RUN_DIR}/treatment_config.yaml` fresh
3. Read `{RUN_DIR}/translation_output.json` for metadata cross-reference
4. Read `{TARGET_REPO}/pkg/plugins/register.go` fresh
5. Apply ALL five review criteria below (never skip one)
6. Reply directly to the writer with your verdict

**Never send APPROVE unless every criterion passes.** Check them in order.

## Review Criteria

### Criterion 1: Fidelity

The plugin must faithfully implement the source algorithm's logic:

- Every signal from the mapping document is present and correctly applied
- All thresholds and weights are preserved exactly from `{ALGO_CONFIG}` (not approximated)
- Regime-detection logic (conditions, branch order, fallthrough) matches `{ALGO_SOURCE}` exactly
- If the algorithm requires scorers in a specific order (e.g., `scorers[0]` = prefix-cache),
  the plugin enforces this and documents it in a comment
- No logic simplifications or "equivalent" rewrites — translate exactly

Flag any divergence from the source algorithm as `[fidelity]` NEEDS_CHANGES.

### Criterion 2: Code Quality

- Interfaces correctly implemented — verify signatures against context document
- Slice indexing guarded: if scorers are accessed by index, check bounds
- No implicit assumptions: all assumptions documented in comments
- Production patterns from the context document are followed (struct layout, logging,
  error propagation)
- No unused imports, dead code, or unexported types that should be exported
- Struct fields used in scoring/regime logic documented with their purpose

### Criterion 3: Registration (CRITICAL)

Verify the complete registration chain — ALL of these must be true:

1. Plugin Go file defines a `Type` constant: `const FooType = "foo-plugin-type"` (string must be kebab-case)
2. Plugin Go file defines a `Factory` function with the correct signature (check context doc)
3. `{TARGET_REPO}/pkg/plugins/register.go` contains:
   `plugin.Register(<pkg>.<TypeConst>, <pkg>.<FactoryFunc>)`
4. The type string in the Go constant matches `plugin_type` in `translation_output.json` exactly (character for character)
5. The type string in the Go constant matches the `type:` field for this plugin in `treatment_config.yaml`

If any of these five items is missing or mismatched, raise `[registration]` NEEDS_CHANGES.
This is the most common failure mode — check it carefully.

### Criterion 4: Config Correctness

- `treatment_config.yaml` has `kind:` matching `config_kind` in `translation_output.json`
- All `type:` values in the config reference plugins that are registered in `register.go`
- Scheduling profile structure and field names follow production patterns (see context doc)
- No fields in the treatment config that are absent from the baseline config without justification

### Criterion 5: Assembly Simulation (CRITICAL)

This verifies that `prepare.py` Phase 4 Assembly will succeed when it embeds the treatment
config as a raw string.

**Step A — Find the baseline shape:**
In `config/env_defaults.yaml`, find the key path:
`stack.{SCENARIO}.gaie.baseline.helmValues.inferenceExtension.pluginsCustomConfig["custom-plugins.yaml"]`
This contains an inline YAML string. Parse it mentally. This is the canonical structure
that the treatment config must mirror — same top-level keys, same nesting depth.

Note: `values.yaml` does not exist at translation time (it is generated in Phase 4),
so you MUST use `env_defaults.yaml` as the reference.

**Step B — Simulate the embed:**
`prepare.py` will do exactly this (Python pseudocode):
```python
tc_content = open("{RUN_DIR}/treatment_config.yaml").read()
alg_values["stack"]["gaie"]["treatment"]["helmValues"]["inferenceExtension"]["pluginsCustomConfig"] = {
    "custom-plugins.yaml": tc_content
}
yaml.dump(alg_values)  # must not raise
```

Verify:
- `treatment_config.yaml` is valid YAML (mentally parse it — check for unescaped colons,
  wrong indentation, missing quotes around values with special characters)
- `treatment_config_generated: true` is set in `translation_output.json`
- Every key in the treatment config also appears in the baseline config, or is explicitly
  justified (no invented keys that would silently be ignored by the EPP)
- `helm_path` in `translation_output.json` is exactly:
  `gaie.treatment.helmValues.inferenceExtension.pluginsCustomConfig.custom-plugins.yaml`

Raise any problem as `[assembly]` NEEDS_CHANGES. Assembly failures only manifest at
Phase 4 and are silent — catch them here.

## Response Format

Reply in this exact format:

**APPROVE** — You MUST include a complete verification summary. An APPROVE without
a per-criterion summary will be treated as incomplete:
```
VERDICT: APPROVE

Verification summary (required — one line per criterion):
- Fidelity: [confirm signals used, thresholds preserved, regime logic matches]
- Code quality: [confirm interfaces correct, no dead code, patterns followed]
- Registration: TypeConst=<exact value>, Factory=<name>, register.go line ~<N>
- Config: kind=<value>, all plugin types registered, keys match baseline
- Assembly: env_defaults baseline shape matches, YAML valid, helm_path correct,
  treatment_config_generated=true
```

**NEEDS_CHANGES:**
```
VERDICT: NEEDS_CHANGES

Issues:
1. [category] Description of the specific problem
   File: exact/path/to/file.go, Line: ~N
   Fix: Exact correction to make (code snippet if helpful)

2. [category] ...
```

Categories: `fidelity` | `code-quality` | `registration` | `config` | `assembly`

List ALL issues you find in a single reply. Do not hold back issues for a future round.
The writer will address all of them before the next review.
```

- [ ] **Step 2.2: Verify file contains required sections**

```bash
grep -c "Fidelity\|Code Quality\|Registration\|Config Correctness\|Assembly Simulation\|VERDICT\|NEEDS_CHANGES" prompts/prepare/agent-reviewer.md
```
Expected: 7 (all criteria + verdict keywords present)

- [ ] **Step 2.3: Verify assembly simulation section references env_defaults, not values.yaml**

```bash
grep "values.yaml" prompts/prepare/agent-reviewer.md | grep -v "does not exist\|env_defaults"
```
Expected: no output (values.yaml only appears in the "does not exist" note)

- [ ] **Step 2.4: Commit**
```bash
git add prompts/prepare/agent-reviewer.md
git commit -m "feat: add agent-reviewer prompt template for sim2real-translate"
```

---

## Chunk 2: Skill and Retirement

### Task 3: Update SKILL.md

**Files:**
- Modify: `.claude/skills/sim2real-translate/SKILL.md`

- [ ] **Step 3.1: Read the current SKILL.md**

Read `.claude/skills/sim2real-translate/SKILL.md` in full before editing.

- [ ] **Step 3.2: Update the frontmatter**

Replace the frontmatter block (lines 1–26) with:

```yaml
---
name: sim2real-translate
description: |
  Translates a simulation-discovered algorithm into a production plugin for
  llm-d-inference-scheduler. Reads skill_input.json (written by prepare.py),
  spawns a two-agent team (writer + reviewer) that owns the translate loop,
  build/test gate, and review rounds, then copies artifacts to generated/.
  Use after prepare.py reaches the translation checkpoint.
argument-hint: "[--rounds N] [--rebuild-context]"
user-invocable: true
allowed-tools:
  - Agent
  - TeamCreate
  - TeamDelete
  - SendMessage
  - TaskCreate
  - TaskUpdate
  - TaskList
  - Bash(python3 *)
  - Bash(cd * && *)
  - Bash(test *)
  - Bash(git *)
  - Glob
  - Read
  - Edit
  - Write
  - Grep
---
```

- [ ] **Step 3.3: Update the description heading and Arguments section**

Replace:
```
# sim2real-translate

Translate a simulation algorithm into a production plugin, with build/test
gates and multi-model AI review.
```
with:
```
# sim2real-translate

Translate a simulation algorithm into a production plugin using a two-agent
team: a writer that owns the build/test gate and round loop, and a reviewer
that is exceptionally critical and includes assembly simulation checks.
```

Replace the Arguments section:
```
## Arguments

- `--rounds N` — Max multi-model review rounds (default: 3)
- `--dev` — Dev mode: only use `aws/claude-opus-4-6` for reviews (faster)
- `--rebuild-context` — Force context reassembly even if cache exists
```
with:
```
## Arguments

- `--rounds N` — Max review rounds (default: 3)
- `--rebuild-context` — Force context reassembly even if cache exists
```

Replace the variable initialization block:
```bash
REPO_ROOT=$(pwd)      # sim2real repo root — used in sys.path for inline python3 -c calls
REVIEW_ROUNDS=3       # override with --rounds N
DEV_MODE=false        # set true with --dev
REBUILD_CONTEXT=false # set true with --rebuild-context
```
with:
```bash
REPO_ROOT=$(pwd)      # sim2real repo root
REVIEW_ROUNDS=3       # override with --rounds N
REBUILD_CONTEXT=false # set true with --rebuild-context
```

Replace the conditional:
```
If `--rounds N` was passed, set `REVIEW_ROUNDS=N`. If `--dev` was passed, set
`DEV_MODE=true`. If `--rebuild-context` was passed, set `REBUILD_CONTEXT=true`.
```
with:
```
If `--rounds N` was passed, set `REVIEW_ROUNDS=N`. If `--rebuild-context` was
passed, set `REBUILD_CONTEXT=true`.
```

- [ ] **Step 3.4: Replace Steps 2–5 with the team translation section**

Find the `## Step 2: Translate` heading and everything through the end of
`## Step 5: Review`. Replace the entire range with this new section:

```markdown
## Step 2: Team Translation

Use TaskCreate: `"Step 2: Team Translation"` → TaskUpdate in_progress

Read the BUILD_COMMANDS list from skill_input.json:

```bash
BUILD_COMMANDS=$(python3 -c "
import json
si = json.load(open('$RUN_DIR/skill_input.json'))
import json as j
print(j.dumps(si.get('build_commands', [])))
")
```

Read the two prompt templates and construct agent prompts by substituting
all `{PLACEHOLDER}` values with the corresponding shell variables
(`{REPO_ROOT}` → `$REPO_ROOT`, `{RUN_DIR}` → `$RUN_DIR`, `{CONTEXT_PATH}` →
`$CONTEXT_PATH`, `{ALGO_SOURCE}` → `$ALGO_SOURCE`, `{ALGO_CONFIG}` →
`$ALGO_CONFIG`, `{TARGET_REPO}` → `$TARGET_REPO`, `{CONFIG_KIND}` →
`$CONFIG_KIND`, `{REVIEW_ROUNDS}` → `$REVIEW_ROUNDS`, `{SCENARIO}` →
`$SCENARIO`, `{HINTS_TEXT}` → `$HINTS_TEXT`, `{HINTS_FILES_CONTENT}` →
`$HINTS_FILES_CONTENT`, `{BUILD_COMMANDS}` → `$BUILD_COMMANDS`).

Create the team and spawn the agents:

```bash
# Read and prepare prompt templates
WRITER_PROMPT=$(python3 -c "
import sys, re
tmpl = open('prompts/prepare/agent-writer.md').read()
subs = {
    'REPO_ROOT': '$REPO_ROOT',
    'RUN_DIR': '$RUN_DIR',
    'CONTEXT_PATH': '$CONTEXT_PATH',
    'ALGO_SOURCE': '$ALGO_SOURCE',
    'ALGO_CONFIG': '$ALGO_CONFIG',
    'TARGET_REPO': '$TARGET_REPO',
    'CONFIG_KIND': '$CONFIG_KIND',
    'REVIEW_ROUNDS': '$REVIEW_ROUNDS',
    'BUILD_COMMANDS': '$BUILD_COMMANDS',
    'HINTS_TEXT': '''$HINTS_TEXT''',
    'HINTS_FILES_CONTENT': '''$HINTS_FILES_CONTENT''',
}
for k, v in subs.items():
    tmpl = tmpl.replace('{' + k + '}', v)
print(tmpl)
")

REVIEWER_PROMPT=$(python3 -c "
import sys
tmpl = open('prompts/prepare/agent-reviewer.md').read()
subs = {
    'REPO_ROOT': '$REPO_ROOT',
    'RUN_DIR': '$RUN_DIR',
    'CONTEXT_PATH': '$CONTEXT_PATH',
    'ALGO_SOURCE': '$ALGO_SOURCE',
    'ALGO_CONFIG': '$ALGO_CONFIG',
    'TARGET_REPO': '$TARGET_REPO',
    'CONFIG_KIND': '$CONFIG_KIND',
    'REVIEW_ROUNDS': '$REVIEW_ROUNDS',
    'SCENARIO': '$SCENARIO',
    'HINTS_TEXT': '''$HINTS_TEXT''',
    'HINTS_FILES_CONTENT': '''$HINTS_FILES_CONTENT''',
}
for k, v in subs.items():
    tmpl = tmpl.replace('{' + k + '}', v)
print(tmpl)
")
```

Use TeamCreate to create the team:
```
TeamCreate(team_name="translate-$RUN_NAME", description="sim2real writer+reviewer for $RUN_NAME")
```

Use Agent tool to spawn the Reviewer (stays idle, name="reviewer"):
- Pass `$REVIEWER_PROMPT` as the prompt
- subagent_type: general-purpose
- name: "reviewer"

Use Agent tool to spawn the Writer (starts immediately, name="writer"):
- Pass `$WRITER_PROMPT` as the prompt
- subagent_type: general-purpose
- name: "writer"

Wait for the Writer to send a message to the main session. Handle each case:

**On "done: ...":**

The writer completed successfully. Proceed to Step 6.

**On "escalate: ...":**

Prompt the operator:
```
Translation exhausted {REVIEW_ROUNDS} rounds without reviewer approval.
Remaining issues:
<paste issues from the escalate message>

Options:
  [c] Continue for N more rounds — reply with: continue N
  [a] Accept translation as-is — reply with: accept
  [q] Quit and investigate — reply with: quit
```

- If "continue N": update REVIEW_ROUNDS, re-read current translation state,
  spawn a new Writer agent with updated REVIEW_ROUNDS (Reviewer is still idle —
  send it a brief "continuing" message via SendMessage to keep it in context)
- If "accept": proceed to Step 6 with `consensus='accepted_without_consensus'`
- If "quit": shut down team (Step 6 cleanup only, no artifact copy)

**On "build-failed: ...":**

Surface the build error to the operator:
```
Translation build failed after 6 retries.
Error: <paste error from message>

Fix the algorithm source or target repo state, then re-run /sim2real-translate.
```
Shut down the team and exit.

Shutdown sequence (after any terminal outcome):
```
SendMessage(to="writer", "shutdown")
SendMessage(to="reviewer", "shutdown")
TeamDelete()
```

TaskUpdate Step 2 → completed
```

- [ ] **Step 3.5: Update Step 6 to note that writer writes round_N.json**

In the existing Step 6 section, find the line about `round_N.json` (if present)
or the "Verify outputs" subsection, and add this note:

```
Note: In the agent-team design, the Writer writes `review/round_N.json` directly.
Step 6 does not need to create it — verify it exists before proceeding.
```

Also add this verification to the Step 6 output check:
```bash
# Verify round review files exist
ls $RUN_DIR/review/round_*.json 2>/dev/null || echo "WARNING: no round review files found"
```

- [ ] **Step 3.6: Verify allowed-tools contains new tools**

```bash
grep -E "TeamCreate|TeamDelete|SendMessage|TaskList" .claude/skills/sim2real-translate/SKILL.md
```
Expected: all four appear in the allowed-tools list

- [ ] **Step 3.7: Verify --dev is gone**

```bash
grep "DEV_MODE\|--dev" .claude/skills/sim2real-translate/SKILL.md
```
Expected: no output

- [ ] **Step 3.8: Verify Steps 2–5 content removed and new Step 2 present**

```bash
grep -c "multi-model\|review\.py\|ROUND_NUM\|ThreadPoolExecutor" .claude/skills/sim2real-translate/SKILL.md
```
Expected: 0

```bash
grep "TeamCreate\|Spawn.*Writer\|Spawn.*Reviewer" .claude/skills/sim2real-translate/SKILL.md
```
Expected: matches found

- [ ] **Step 3.9: Commit**
```bash
git add .claude/skills/sim2real-translate/SKILL.md
git commit -m "feat: replace review.py loop with agent-team writer+reviewer in SKILL.md"
```

---

### Task 4: Retire review.py

**Files:**
- Modify: `.claude/skills/sim2real-translate/scripts/review.py`

- [ ] **Step 4.1: Read review.py to find the first non-comment line**

Read `.claude/skills/sim2real-translate/scripts/review.py` lines 1–10.

- [ ] **Step 4.2: Add deprecation header**

Prepend the following comment block before the existing `"""` docstring or first line:

```python
# DEPRECATED: This script is retired as of 2026-04-10.
# The multi-model external LLM review loop has been replaced by the agent-team
# design in SKILL.md. The reviewer is now a spawned Claude agent using
# prompts/prepare/agent-reviewer.md.
# This file is kept for git history only — do not invoke it.
#
```

- [ ] **Step 4.3: Verify header was added**

```bash
head -8 .claude/skills/sim2real-translate/scripts/review.py
```
Expected: first line is `# DEPRECATED: ...`

- [ ] **Step 4.4: Commit**
```bash
git add .claude/skills/sim2real-translate/scripts/review.py
git commit -m "chore: retire review.py — superseded by agent-reviewer prompt"
```

---

## Verification Checklist

After all tasks complete, verify end-to-end consistency:

- [ ] Both prompt files exist and pass their section checks
- [ ] `agent-writer.md` instructs sending review requests to the reviewer agent (not calling review.py)
- [ ] `agent-reviewer.md` references `env_defaults.yaml` for assembly check (not `values.yaml`)
- [ ] SKILL.md `allowed-tools` includes `TeamCreate`, `TeamDelete`, `SendMessage`, `TaskList`
- [ ] SKILL.md has no references to `review.py`, `DEV_MODE`, or `--dev`
- [ ] `review.py` has deprecation header
- [ ] Protocol consistency: writer sends `REVIEW REQUEST — Round N` format; reviewer replies `VERDICT: APPROVE|NEEDS_CHANGES` format — these match between the two prompt files
- [ ] `translation_output.json` schema in `agent-writer.md` has exactly 10 fields (not 6)
- [ ] `round_N.json` schema in `agent-writer.md` uses `"model": "agent-reviewer"` (not a GPT/Gemini model name)
