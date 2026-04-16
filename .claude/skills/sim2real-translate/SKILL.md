---
name: sim2real-translate
description: |
  Translates a simulation-discovered algorithm into a production plugin for
  llm-d-inference-scheduler. Reads skill_input.json (written by prepare.py),
  spawns a three-agent team (expert + writer + reviewer) where the expert does
  deep repo exploration in parallel with writer/reviewer initialization, then
  the writer owns the translate loop, build/test gate, and review rounds.
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

# sim2real-translate

Translate a simulation algorithm into a production plugin using a two-agent
team: a writer that owns the build/test gate and round loop, and a reviewer
that is exceptionally critical and includes assembly simulation checks.

## CRITICAL: Working Directory

**All commands in this skill must run from the `sim2real/` root directory.**
The target production repo (e.g., `llm-d-inference-scheduler/`) is a submodule.
Run commands there via subshell: `(cd llm-d-inference-scheduler && ...)`

Before each major step, verify:
```bash
test -f config/env_defaults.yaml || { echo "ERROR: not in sim2real root"; exit 1; }
```

## Arguments

- `--rounds N` — Max review rounds (default: 3)
- `--rebuild-context` — Force context reassembly even if cache exists

Initialize shell variables from skill invocation arguments:

```bash
REPO_ROOT=$(pwd)      # sim2real repo root
REVIEW_ROUNDS=3       # override with --rounds N
REBUILD_CONTEXT=false # set true with --rebuild-context
```

If `--rounds N` was passed, set `REVIEW_ROUNDS=N`. If `--rebuild-context` was
passed, set `REBUILD_CONTEXT=true`.

## Prerequisites

Use TaskCreate to create an overall progress task:
```
TaskCreate subject="sim2real-translate: $RUN_NAME" description="Running translation skill"
```

Read current run directory:

```bash
RUN_DIR=$(python3 -c "
import json, sys, os
config_path = 'workspace/setup_config.json'
if not os.path.exists(config_path):
    print('HALT: workspace/setup_config.json not found. Run /sim2real-setup first.', file=sys.stderr)
    sys.exit(1)
config = json.load(open(config_path))
run_name = config.get('current_run', '')
if not run_name:
    print('HALT: No current_run in setup_config.json.', file=sys.stderr)
    sys.exit(1)
run_dir = f'workspace/runs/{run_name}'
if not os.path.exists(f'{run_dir}/skill_input.json'):
    print(f'HALT: {run_dir}/skill_input.json not found. Run prepare.py first.', file=sys.stderr)
    sys.exit(1)
print(run_dir)
")
test -n "$RUN_DIR" || exit 1
```

Validate and load `skill_input.json`:

```bash
python3 -c "
import json, sys
si = json.load(open('$RUN_DIR/skill_input.json'))
required = ['run_name', 'run_dir', 'scenario', 'context_path', 'manifest_path',
            'algorithm_source', 'baseline_sim_config',
            'target', 'build_commands', 'config_kind', 'hints']
missing = [f for f in required if f not in si]
if missing:
    print(f'HALT: skill_input.json missing fields: {missing}')
    sys.exit(1)
if 'repo' not in si['target']:
    print('HALT: skill_input.json target.repo missing')
    sys.exit(1)
print(f'Loaded: run={si[\"run_name\"]} scenario={si[\"scenario\"]} config_kind={si[\"config_kind\"]}')
" || exit 1
```

Load into shell variables:

```bash
RUN_NAME=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['run_name'])")
SCENARIO=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['scenario'])")
CONTEXT_PATH=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['context_path'])")
MANIFEST_PATH=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['manifest_path'])")
ALGO_SOURCE=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['algorithm_source'])")
ALGO_CONFIG=$(python3 -c "import json; v=json.load(open('$RUN_DIR/skill_input.json')).get('algorithm_config'); print(v or '')")
TARGET_REPO=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['target']['repo'])")
CONFIG_KIND=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['config_kind'])")
HINTS_TEXT=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json')).get('hints', {}).get('text', ''))")
HINTS_FILES_CONTENT=$(python3 -c "
import json
hints = json.load(open('$RUN_DIR/skill_input.json')).get('hints', {}).get('files', [])
for f in hints:
    print(f'### {f[\"path\"]}')
    print(f['content'])
    print()
")
BASELINE_SIM_CONFIG=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['baseline_sim_config'])")
BASELINE_REAL_CONFIG=$(python3 -c "import json; v=json.load(open('$RUN_DIR/skill_input.json')).get('baseline_real_config'); print(v if v else 'null')")
BASELINE_REAL_NOTES=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json')).get('baseline_real_notes', ''))")
```

Verify source files exist:

```bash
test -f "$ALGO_SOURCE" || { echo "HALT: algorithm source not found: $ALGO_SOURCE"; exit 1; }
[ -z "$ALGO_CONFIG" ] || test -f "$ALGO_CONFIG" || { echo "HALT: algorithm config not found: $ALGO_CONFIG"; exit 1; }
test -d "$TARGET_REPO" || { echo "HALT: target repo not found: $TARGET_REPO"; exit 1; }
```

## Resumability Check

Load `.state.json` to detect completed phases:

```bash
python3 -c "
import json, os, sys
from pathlib import Path
state_path = Path('$RUN_DIR') / '.state.json'
if not state_path.exists():
    print('State: fresh run')
    print('BASELINE_DONE=false')
    print('TREATMENT_DONE=false')
    sys.exit(0)
state = json.loads(state_path.read_text())
phases = state.get('phases', {})
ctx = phases.get('context', {})
bd = phases.get('baseline_derivation', {})
td = phases.get('treatment_derivation', {})
tr = phases.get('translate', {})
print(f'context: {ctx.get(\"status\", \"pending\")}' + (f' (hash={ctx[\"hash\"]})' if ctx.get('hash') else ''))
print(f'baseline_derivation: {bd.get(\"status\", \"pending\")} user_approved={bd.get(\"user_approved\", False)}')
print(f'treatment_derivation: {td.get(\"status\", \"pending\")} user_approved={td.get(\"user_approved\", False)}')
print(f'translate: {tr.get(\"status\", \"pending\")}')
if tr.get('status') == 'done':
    print(f'  files={tr.get(\"files\", [])}')
    print(f'  review_rounds={tr.get(\"review_rounds\", 0)} consensus={tr.get(\"consensus\", \"?\")}')
bd_done = bd.get('status') == 'done' and bd.get('user_approved', False)
td_done = td.get('status') == 'done' and td.get('user_approved', False)
print(f'BASELINE_DONE={\"true\" if bd_done else \"false\"}')
print(f'TREATMENT_DONE={\"true\" if td_done else \"false\"}')
" | tee /tmp/resume_state.txt

# Parse skip flags from python output
BASELINE_DONE=$(grep '^BASELINE_DONE=' /tmp/resume_state.txt | cut -d= -f2)
TREATMENT_DONE=$(grep '^TREATMENT_DONE=' /tmp/resume_state.txt | cut -d= -f2)
```

**If `translate` phase is `done` and `translation_output.json` exists:**
- Read `translation_output.json` and verify all `files_created` + `files_modified` exist
  in `$RUN_DIR/generated/`
- If complete: print the summary (see Step 6) and HALT with:
  `Translation already complete. Re-run prepare.py to continue.`
- If `generated/` is missing files: jump directly to Step 6 to re-copy

**If `context_file_populated=true` in state AND `$CONTEXT_PATH` exists AND `REBUILD_CONTEXT=false`:**
- Skip Step 1 entirely

Note: `prepare.py` marks `context_file_prepared=true, context_file_populated=false` when
it assembles the raw context file from `context.files`. The skill's subagent enriches that
file with production interfaces, example plugins, and the registration pattern from the
target repo, then marks `context_file_populated=true`. Checking `context_file_populated`
(not just `context_file_prepared`) ensures the skill always runs Step 1 on a fresh
prepare.py run.

**If snapshots or review rounds exist but `translate` is not `done`:**
- Resume the loop: check which snapshot versions exist and which review rounds exist,
  then start from the appropriate point

## Step 1: Context Check

Use TaskCreate: `"Step 1: Context Check"` → TaskUpdate in_progress

Check the two context fields in state:

```bash
python3 -c "
import json, sys
from pathlib import Path
state_path = Path('$RUN_DIR') / '.state.json'
if not state_path.exists():
    print('context_file_prepared=false context_file_populated=false')
    sys.exit(0)
ctx = json.loads(state_path.read_text()).get('phases', {}).get('context', {})
print(f'context_file_prepared={str(ctx.get(\"context_file_prepared\", False)).lower()}')
print(f'context_file_populated={str(ctx.get(\"context_file_populated\", False)).lower()}')
"
```

**If `context_file_prepared=false`:** prepare.py hasn't assembled the raw context file
yet. Run it now:

```bash
python3 pipeline/prepare.py context
echo "Context file prepared."
```

**If `context_file_populated=false` (always true after a fresh prepare.py run):**
assemble the full structured context document directly in the current session.

Read these files using the Read tool:

1. All files listed in `manifest["context"]["files"]` — read each one in full
2. Production interfaces for `$SCENARIO` from `$TARGET_REPO` — explore `$TARGET_REPO/pkg/plugins/`
   to find the relevant interface definitions:
   - routing → `Scorer` interface, `EndpointPickerConfig`, `SchedulingContext`
   - admission_control → `Admission` interface, `AdmissionPolicyConfig`
   Read from the actual `.go` source files in that directory.
3. One or two representative example plugins from the same directory — read in full
4. Plugin registration pattern: explore `$TARGET_REPO` to discover the registration file
   (typically a `register.go` or `plugins.go`) and read it in full

Then use the Write tool to write `$CONTEXT_PATH` with this structure:

```
# Translation Context
Scenario: $SCENARIO | inference-sim@<sha> | llm-d@<sha>

## Signal Mapping
[full contents of mapping document]

## Production Interfaces
[Scorer/Admission interface, EndpointPickerConfig, SchedulingContext
 — extracted verbatim from source, not paraphrased]

## Example Plugin: <filename>
[full contents]

## Plugin Registration
[full contents of the discovered registration file]

## <any additional manifest context file>
[full contents]
```

Do NOT include hints in the context file — hints are held in session memory via $HINTS_TEXT and $HINTS_FILES_CONTENT.

Verify the file was written:

```bash
test -f "$CONTEXT_PATH" || { echo "HALT: Failed to write context document to $CONTEXT_PATH"; exit 1; }
```

Mark context as populated in state:

```bash
python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from pipeline.lib.state_machine import StateMachine
state = StateMachine.load('$RUN_DIR')
state.update('context', context_file_populated=True)
print('State updated: context_file_populated=true')
"
```

TaskUpdate Step 1 → completed

## Step 2: Team Translation

Use TaskCreate: `"Step 2: Team Translation"` → TaskUpdate in_progress

Read the BUILD_COMMANDS list from skill_input.json:

```bash
BUILD_COMMANDS=$(python3 -c "
import json
si = json.load(open('$RUN_DIR/skill_input.json'))
print(json.dumps(si.get('build_commands', [])))
")
```

Read the three prompt templates and construct agent prompts by substituting
all `{PLACEHOLDER}` values with the corresponding shell variables
(`{REPO_ROOT}` → `$REPO_ROOT`, `{RUN_DIR}` → `$RUN_DIR`, `{CONTEXT_PATH}` →
`$CONTEXT_PATH`, `{ALGO_SOURCE}` → `$ALGO_SOURCE`, `{ALGO_CONFIG}` →
`$ALGO_CONFIG`, `{BASELINE_SIM_CONFIG}` → `$BASELINE_SIM_CONFIG`,
`{BASELINE_REAL_CONFIG}` → `$BASELINE_REAL_CONFIG`,
`{BASELINE_REAL_NOTES}` → `$BASELINE_REAL_NOTES`,
`{EXPERT_AGENT_NAME}` → `"expert"`,
`{TARGET_REPO}` → `$TARGET_REPO`, `{CONFIG_KIND}` →
`$CONFIG_KIND`, `{REVIEW_ROUNDS}` → `$REVIEW_ROUNDS`, `{SCENARIO}` →
`$SCENARIO`, `{BUILD_COMMANDS}` → `$BUILD_COMMANDS`, `{HINTS_TEXT}` →
`$HINTS_TEXT`, `{HINTS_FILES_CONTENT}` → `$HINTS_FILES_CONTENT`,
`{MAIN_SESSION_NAME}` → `"main-session"`).

Use TeamCreate to create the team:
```
TeamCreate(team_name="translate-$RUN_NAME", description="sim2real expert+writer+reviewer for $RUN_NAME")
```

**Spawn all three agents in a single tool-call message** so they start in parallel.
Issue three Agent tool calls at once — do not wait between them:

1. Agent(name="expert", team_name="translate-$RUN_NAME", run_in_background=true,
         subagent_type=general-purpose,
         prompt=<substituted agent-expert.md>)

2. Agent(name="reviewer", team_name="translate-$RUN_NAME", run_in_background=true,
         subagent_type=general-purpose,
         prompt=<substituted agent-reviewer.md>)

3. Agent(name="writer", team_name="translate-$RUN_NAME",
         subagent_type=general-purpose,
         prompt=<substituted agent-writer.md>)

All three start simultaneously. Expert and Reviewer initialize in the background while
Writer begins Phase 2. By the time Writer reaches Phase 4 (translation), Expert will
have completed its repo exploration and be ready to answer queries.

Wait for the Writer to send a message to the main session. Handle each case:

**On "baseline-ready: ...":**

Read `$RUN_DIR/generated/baseline_config.yaml` and print to user:
```
━━━ Baseline Config (derived from sim → real EPP) ━━━
<file contents>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Provide feedback to revise, or type 'done' to proceed.
```

Read user input. If feedback:
```
SendMessage("writer", "feedback: <user feedback text>")
```
If "done":
```bash
python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from pipeline.lib.state_machine import StateMachine
state = StateMachine.load('$RUN_DIR')
state.update('baseline_derivation', status='done', user_approved=True)
print('State: baseline_derivation done')
"
SendMessage("writer", "continue")
```

**On "treatment-ready: ...":**

Read `$RUN_DIR/generated/treatment_config.yaml` and print to user:
```
━━━ Treatment Config (derived from baseline + algorithm) ━━━
<file contents>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Provide feedback to revise, or type 'done' to proceed.
```

Handle feedback / continue as for `baseline-ready:`. On continue, update state:
```bash
python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from pipeline.lib.state_machine import StateMachine
state = StateMachine.load('$RUN_DIR')
state.update('treatment_derivation', status='done', user_approved=True)
print('State: treatment_derivation done')
"
SendMessage("writer", "continue")
```

**On "review-passed: round=N ...":**

```bash
python3 -c "
import json
o = json.load(open('$RUN_DIR/translation_output.json'))
print('Plugin files:', o.get('files_created', []))
"
python3 -c "print(open('$RUN_DIR/generated/treatment_config.yaml').read())"
```

Print to user:
```
━━━ Review Passed ━━━
Treatment Config: <contents above>
Plugin files: <list above>

Provide feedback for another round, or type 'done' to finish.
```

If feedback: `SendMessage("writer", "feedback: <text>")`
If "done": `SendMessage("writer", "done")`

**On "done: ...":**

The writer completed successfully. Proceed to Step 6 (which will shut down the team
after artifacts are collected).

**On "escalate: ...":**

Prompt the operator:
```
Translation exhausted $REVIEW_ROUNDS rounds without reviewer approval.
Remaining issues:
<paste issues from the escalate message>

Options:
  [c] Continue for N more rounds — reply with: continue N
  [a] Accept translation as-is — reply with: accept
  [q] Quit and investigate — reply with: quit
```

- If "continue N": update REVIEW_ROUNDS (e.g. `REVIEW_ROUNDS=$((REVIEW_ROUNDS + N))`),
  then spawn a NEW Writer agent with the updated value substituted into the prompt.
  The reviewer is still idle — it does not need to be restarted.
- If "accept": proceed to Step 6 with `consensus='accepted_without_consensus'`
  (Step 6 will shut down the team after artifacts are collected).
- If "quit":
  ```
  SendMessage(to="writer", "shutdown")
  SendMessage(to="reviewer", "shutdown")
  SendMessage(to="expert", "shutdown")
  TeamDelete()
  ```
  Exit without copying artifacts.

**On "build-failed: ...":**

Surface the build error to the operator:
```
Translation build failed after 6 retries.
Error: <paste error from message>

Fix the algorithm source or target repo state, then re-run /sim2real-translate.
```

Shut down the team and exit:
```
SendMessage(to="writer", "shutdown")
SendMessage(to="reviewer", "shutdown")
SendMessage(to="expert", "shutdown")
TeamDelete()
```

TaskUpdate Step 2 → completed

## Step 6: Output

Use TaskCreate: `"Step 6: Output Artifacts"` → TaskUpdate in_progress

Note: The writer agent writes `review/round_N.json` directly. Verify it exists:

```bash
ls $RUN_DIR/review/round_*.json 2>/dev/null || echo "WARNING: no round review files found"
```

Determine final review outcome from the most recent round file:

```bash
FINAL_CONSENSUS=$(python3 -c "
import json
from pathlib import Path
rounds = sorted((Path('$RUN_DIR/review')).glob('round_*.json'))
if not rounds:
    print('none')
else:
    last = json.loads(rounds[-1].read_text())
    if last.get('consensus'):
        print(f'{last[\"approve_count\"]}/{last[\"total_successful\"]}')
    else:
        print('accepted_without_consensus')
" 2>/dev/null || echo "none")

REVIEW_ROUNDS_DONE=$(python3 -c "
from pathlib import Path
print(len(list((Path('$RUN_DIR/review')).glob('round_*.json'))))
" 2>/dev/null || echo 0)
```

Copy all created/modified files + treatment_config.yaml into `$RUN_DIR/generated/`:

```bash
python3 -c "
import json, shutil
from pathlib import Path
o = json.load(open('$RUN_DIR/translation_output.json'))
gen = Path('$RUN_DIR/generated')
gen.mkdir(parents=True, exist_ok=True)
target = Path('$TARGET_REPO')
for f in o['files_created'] + o.get('files_modified', []):
    src = target / f
    dst = gen / Path(f).name
    shutil.copy2(src, dst)
    print(f'  {Path(f).name} → generated/')
print('Generated artifacts ready.')
"
```

Update `.state.json`:

```bash
python3 -c "
import json, sys
sys.path.insert(0, '$REPO_ROOT')
from pipeline.lib.state_machine import StateMachine
o = json.load(open('$RUN_DIR/translation_output.json'))
state = StateMachine.load('$RUN_DIR')
state.mark_done('translate',
    files=o['files_created'],
    review_rounds=$REVIEW_ROUNDS_DONE,
    consensus='$FINAL_CONSENSUS')
print('State updated: translate done')
"
```

Verify outputs:

```bash
python3 -c "
import json, sys
from pathlib import Path
o = json.load(open('$RUN_DIR/translation_output.json'))
required = ['plugin_type', 'files_created', 'files_modified', 'package',
            'register_file', 'test_commands', 'config_kind', 'helm_path',
            'treatment_config_generated', 'description']
missing = [f for f in required if f not in o]
if missing:
    print(f'ERROR: translation_output.json missing: {missing}')
    sys.exit(1)
gen = Path('$RUN_DIR/generated')
for f in o['files_created'] + o.get('files_modified', []):
    dst = gen / Path(f).name
    if not dst.exists():
        print(f'ERROR: generated/ missing: {Path(f).name}')
        sys.exit(1)
print(f'Verified: plugin_type={o[\"plugin_type\"]}, '
      f'{len(o[\"files_created\"])} created, '
      f'{len(o.get(\"files_modified\", []))} modified')
" || exit 1
```

When everything is done, you MUST tell the user what to do next, which is continuing the python pipeline/prepare.py script to run the remaining phases (Assembly, Summary, Gate) and complete the run.

TaskUpdate all open tasks → completed.

Print completion summary:

```
━━━ /sim2real-translate complete ━━━

Plugin:   <plugin_type>
Package:  <package>
Files:    <files_created> (created)
          <files_modified> (modified)
Review:   <consensus> after <N> rounds
Snapshots: <N> versions in $RUN_DIR/snapshots/

Output artifacts:
  $RUN_DIR/translation_output.json
  $RUN_DIR/generated/
  $RUN_DIR/snapshots/
  $RUN_DIR/review/

Next: re-run prepare.py to continue through Assembly, Summary, and Gate.
  python pipeline/prepare.py
```

Shut down the agent team:
```
SendMessage(to="writer", "shutdown")
SendMessage(to="reviewer", "shutdown")
SendMessage(to="expert", "shutdown")
TeamDelete()
```

## What This Skill Does NOT Do

- Does not assemble values.yaml or cluster YAMLs (prepare.py Phase 4)
- Does not touch env_defaults.yaml or cluster config
- Does not generate run_summary.md or perform the human gate
- Job: produce working, reviewed Go code + treatment config + metadata in generated/
