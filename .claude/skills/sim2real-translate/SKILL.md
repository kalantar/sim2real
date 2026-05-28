---
name: sim2real-translate
description: |
  Translates a simulation-discovered algorithm into a production plugin for
  llm-d-inference-scheduler. Reads skill_input.json (written by prepare.py),
  spawns a three-agent team (expert + writer + reviewer) where the expert does
  deep repo exploration in parallel with writer/reviewer initialization, then
  the writer owns the translate loop, build/test gate, and review rounds.
  Use after prepare.py reaches the translation checkpoint.
argument-hint: "[--experiment-root PATH] [--rounds N] [--rebuild-context]"
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

Run from the **experiment repo root** (where `transfer.yaml`, `algorithm/`, and
`llm-d-inference-scheduler/` live) OR from any directory with `--experiment-root PATH`.
The `sim2real/` pipeline root is discovered automatically from `setup_config.json`.

Target repo commands run via subshell: `(cd $TARGET_REPO && ...)`

Before each major step, verify the experiment root has the expected layout:
```bash
test -f "$EXPERIMENT_ROOT/workspace/setup_config.json" || { echo "ERROR: no setup_config.json in $EXPERIMENT_ROOT/workspace/"; exit 1; }
```

## Arguments

- `--experiment-root PATH` — Path to experiment repo (default: current working directory)
- `--rounds N` — Max review rounds (default: 3)
- `--rebuild-context` — Force context reassembly even if cache exists

Initialize shell variables from skill invocation arguments:

```bash
EXPERIMENT_ROOT=$(pwd)  # overridden by --experiment-root PATH
REVIEW_ROUNDS=3         # override with --rounds N
REBUILD_CONTEXT=false   # set true with --rebuild-context
```

If `--experiment-root PATH` was passed, set `EXPERIMENT_ROOT=$(realpath PATH)`.
If `--rounds N` was passed, set `REVIEW_ROUNDS=N`. If `--rebuild-context` was
passed, set `REBUILD_CONTEXT=true`.

## Prerequisites

Use TaskCreate to create an overall progress task:
```
TaskCreate subject="sim2real-translate: $RUN_NAME" description="Running translation skill"
```

Load `setup_config.json` and derive `REPO_ROOT` and `RUN_DIR`:

```bash
python3 -c "
import json, sys, os
from pathlib import Path

exp = os.environ.get('EXPERIMENT_ROOT', os.getcwd())
ws = Path(exp) / 'workspace'

# --- current_run: setup_config.json or latest run with skill_input.json ---
config_path = ws / 'setup_config.json'
run_name = ''
repo_root = ''
if config_path.exists():
    cfg = json.loads(config_path.read_text())
    run_name = cfg.get('current_run', '')
    repo_root = cfg.get('sim2real_root', '')

if not run_name:
    runs_dir = ws / 'runs'
    if runs_dir.exists():
        candidates = sorted(
            [d for d in runs_dir.iterdir()
             if d.is_dir() and (d / 'skill_input.json').exists()],
            key=lambda d: d.stat().st_mtime, reverse=True
        )
        if candidates:
            run_name = candidates[0].name

if not run_name:
    print('HALT: No run with skill_input.json found in workspace/runs/. Run pipeline/prepare.py first.', file=sys.stderr)
    sys.exit(1)

run_dir = ws / 'runs' / run_name
if not (run_dir / 'skill_input.json').exists():
    print(f'HALT: {run_dir}/skill_input.json not found. Run pipeline/prepare.py first.', file=sys.stderr)
    sys.exit(1)

# --- sim2real root: setup_config.json > SIM2REAL_ROOT env > error ---
if not repo_root:
    repo_root = os.environ.get('SIM2REAL_ROOT', '')
if not repo_root or not Path(repo_root).exists():
    print('HALT: Cannot determine sim2real repo root.', file=sys.stderr)
    print('  Fix A: Run pipeline/setup.py first (records sim2real_root in workspace/setup_config.json)', file=sys.stderr)
    print('  Fix B: Set SIM2REAL_ROOT env var: export SIM2REAL_ROOT=/path/to/sim2real', file=sys.stderr)
    sys.exit(1)

print(f'RUN_DIR={run_dir}')
print(f'REPO_ROOT={repo_root}')
" | tee /tmp/translate_prereq.txt
test $? -eq 0 || exit 1

RUN_DIR=$(grep '^RUN_DIR=' /tmp/translate_prereq.txt | cut -d= -f2-)
REPO_ROOT=$(grep '^REPO_ROOT=' /tmp/translate_prereq.txt | cut -d= -f2-)
test -n "$RUN_DIR" || exit 1
test -n "$REPO_ROOT" || exit 1
```

Validate and load `skill_input.json`:

```bash
python3 -c "
import json, sys
si = json.load(open('$RUN_DIR/skill_input.json'))
required = ['run_name', 'run_dir', 'scenario', 'context_path', 'manifest_path',
            'algorithm_source', 'baseline_sim_config',
            'target', 'build_commands', 'config_kind', 'context']
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

Load into shell variables. All paths in `skill_input.json` are relative to
`$EXPERIMENT_ROOT`; prefix each with `$EXPERIMENT_ROOT/` to get absolute paths:

```bash
RUN_NAME=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['run_name'])")
SCENARIO=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['scenario'])")
CONTEXT_PATH=$EXPERIMENT_ROOT/$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['context_path'])")
MANIFEST_PATH=$EXPERIMENT_ROOT/$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['manifest_path'])")
ALGO_SOURCE=$EXPERIMENT_ROOT/$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['algorithm_source'])")
ALGO_CONFIG=$(python3 -c "import json; v=json.load(open('$RUN_DIR/skill_input.json')).get('algorithm_config'); print('$EXPERIMENT_ROOT/' + v if v else '')")
TARGET_REPO=$EXPERIMENT_ROOT/$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['target']['repo'])")
CONFIG_KIND=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['config_kind'])")
CONTEXT_TEXT=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json')).get('context', {}).get('text', ''))")
BASELINE_SIM_CONFIG=$(python3 -c "import json; v=json.load(open('$RUN_DIR/skill_input.json'))['baseline_sim_config']; print('$EXPERIMENT_ROOT/' + v if v else '')")
BASELINE_REAL_CONFIG=$(python3 -c "import json; v=json.load(open('$RUN_DIR/skill_input.json')).get('baseline_real_config'); print('$EXPERIMENT_ROOT/' + v if v else 'null')")
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
python3 $REPO_ROOT/pipeline/prepare.py context
echo "Context file prepared."
```

**If `context_file_populated=false` (always true after a fresh prepare.py run):**
enrich the existing context document with production interfaces from the target repo.

The context file at `$CONTEXT_PATH` already contains content assembled by `prepare.py`
from the manifest's `context.files` (e.g., README.md, config.md). This content includes
deployment guidance, signal mapping, InferenceObjectives, and other scenario-specific
details that the writer depends on. **Do not overwrite it.**

Read the existing file, then **append** the following sections to it using the Edit tool
(append at end of file). Explore `$TARGET_REPO` to gather the content for each section:

1. Production interfaces for `$SCENARIO` — explore the target repo to find the relevant
   interface definitions:
   - routing → `Scorer` interface, `EndpointPickerConfig`, `SchedulingContext`
   - admission_control → `Admission` interface, `AdmissionPolicyConfig`
   - flowcontrol → `UsageLimitPolicy` interface, `SaturationDetector`
   Read from the actual `.go` source files.
2. One or two representative example plugins from the same directory — read in full
3. Plugin registration pattern: explore `$TARGET_REPO` to discover the registration file
   (typically `runner.go` or `register.go`) and read the relevant section

Append these sections to `$CONTEXT_PATH`:

```
## Production Interfaces
[Interface definitions extracted verbatim from source, not paraphrased]

## Example Plugin: <filename>
[full contents of a representative plugin in the same category]

## Plugin Registration
[registration pattern showing how plugins are registered]
```

Do NOT include context.text in the context file — it is held in session memory via $CONTEXT_TEXT.
Do NOT rewrite the file header or existing content — only append new sections.

Verify the file was written:

```bash
test -f "$CONTEXT_PATH" || { echo "HALT: Failed to write context document to $CONTEXT_PATH"; exit 1; }
```

Mark context as populated in state:

```bash
python3 -c "
import json, tempfile
from pathlib import Path
from datetime import datetime, timezone

state_path = Path('$RUN_DIR') / '.state.json'
state = json.loads(state_path.read_text())
state.setdefault('phases', {}).setdefault('context', {})['context_file_populated'] = True
fd, tmp = tempfile.mkstemp(dir='$RUN_DIR', suffix='.tmp')
import os; os.close(fd)
Path(tmp).write_text(json.dumps(state, indent=2))
Path(tmp).replace(state_path)
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

Read the three prompt templates from the skill's prompts/ directory:
- `$REPO_ROOT/.claude/skills/sim2real-translate/prompts/agent-expert.md`
- `$REPO_ROOT/.claude/skills/sim2real-translate/prompts/agent-writer.md`
- `$REPO_ROOT/.claude/skills/sim2real-translate/prompts/agent-reviewer.md`

Construct agent prompts by substituting all `{PLACEHOLDER}` values with the
corresponding shell variables:
- `{REPO_ROOT}` → `$REPO_ROOT`
- `{EXPERIMENT_ROOT}` → `$EXPERIMENT_ROOT`
- `{RUN_DIR}` → `$RUN_DIR`
- `{CONTEXT_PATH}` → `$CONTEXT_PATH`
- `{ALGO_SOURCE}` → `$ALGO_SOURCE`
- `{ALGO_CONFIG}` → `$ALGO_CONFIG`
- `{BASELINE_SIM_CONFIG}` → `$BASELINE_SIM_CONFIG`
- `{BASELINE_REAL_CONFIG}` → `$BASELINE_REAL_CONFIG`
- `{BASELINE_REAL_NOTES}` → `$BASELINE_REAL_NOTES`
- `{EXPERT_AGENT_NAME}` → `"expert"`
- `{TARGET_REPO}` → `$TARGET_REPO`
- `{CONFIG_KIND}` → `$CONFIG_KIND`
- `{REVIEW_ROUNDS}` → `$REVIEW_ROUNDS`
- `{SCENARIO}` → `$SCENARIO`
- `{BUILD_COMMANDS}` → `$BUILD_COMMANDS`
- `{CONTEXT_TEXT}` → `$CONTEXT_TEXT`
- `{MAIN_SESSION_NAME}` → `"main-session"`

Use TeamCreate to create the team:
```
TeamCreate(team_name="translate-$RUN_NAME", description="sim2real expert+writer+reviewer for $RUN_NAME")
```

**Spawn all three agents in a single tool-call message** so they start in parallel.
Issue three Agent tool calls at once — do not wait between them:

1. Agent(name="expert", team_name="translate-$RUN_NAME", run_in_background=true,
         subagent_type=general-purpose, model="opus",
         prompt=<substituted agent-expert.md>)

2. Agent(name="reviewer", team_name="translate-$RUN_NAME", run_in_background=true,
         subagent_type=general-purpose, model="opus",
         prompt=<substituted agent-reviewer.md>)

3. Agent(name="writer", team_name="translate-$RUN_NAME",
         subagent_type=general-purpose, model="opus",
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
import json, tempfile
from pathlib import Path
from datetime import datetime, timezone

state_path = Path('$RUN_DIR') / '.state.json'
state = json.loads(state_path.read_text())
state.setdefault('phases', {})['baseline_derivation'] = {
    'status': 'done', 'user_approved': True,
    'timestamp': datetime.now(timezone.utc).isoformat()
}
fd, tmp = tempfile.mkstemp(dir='$RUN_DIR', suffix='.tmp')
import os; os.close(fd)
Path(tmp).write_text(json.dumps(state, indent=2))
Path(tmp).replace(state_path)
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
import json, tempfile
from pathlib import Path
from datetime import datetime, timezone

state_path = Path('$RUN_DIR') / '.state.json'
state = json.loads(state_path.read_text())
state.setdefault('phases', {})['treatment_derivation'] = {
    'status': 'done', 'user_approved': True,
    'timestamp': datetime.now(timezone.utc).isoformat()
}
fd, tmp = tempfile.mkstemp(dir='$RUN_DIR', suffix='.tmp')
import os; os.close(fd)
Path(tmp).write_text(json.dumps(state, indent=2))
Path(tmp).replace(state_path)
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

Derive file lists from git state, overwrite `translation_output.json`, and copy to `$RUN_DIR/generated/`:

```bash
python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT/.claude/skills/sim2real-translate/scripts')
from copy_generated import copy_generated
copy_generated('$TARGET_REPO', '$RUN_DIR')
" || exit 1
```

Update `.state.json`:

```bash
python3 -c "
import json, tempfile
from pathlib import Path
from datetime import datetime, timezone

state_path = Path('$RUN_DIR') / '.state.json'
state = json.loads(state_path.read_text())
o = json.load(open('$RUN_DIR/translation_output.json'))
state.setdefault('phases', {})['translate'] = {
    'status': 'done',
    'timestamp': datetime.now(timezone.utc).isoformat(),
    'files': o['files_created'],
    'review_rounds': $REVIEW_ROUNDS_DONE,
    'consensus': '$FINAL_CONSENSUS',
}
fd, tmp = tempfile.mkstemp(dir='$RUN_DIR', suffix='.tmp')
import os; os.close(fd)
Path(tmp).write_text(json.dumps(state, indent=2))
Path(tmp).replace(state_path)
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
            'register_file', 'test_commands', 'config_kind',
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
  python3 $REPO_ROOT/pipeline/prepare.py
  (or: cd $EXPERIMENT_ROOT && python3 $REPO_ROOT/pipeline/prepare.py)
```

Shut down the agent team:
```
SendMessage(to="writer", "shutdown")
SendMessage(to="reviewer", "shutdown")
SendMessage(to="expert", "shutdown")
TeamDelete()
```

## What This Skill Does NOT Do

- Does not assemble resolved scenarios or cluster YAMLs (prepare.py Phase 4)
- Does not touch cluster config or bundle inputs (baseline.yaml, treatment.yaml)
- Does not generate run_summary.md or perform the human gate
- Job: produce working, reviewed Go code + treatment config + metadata in generated/
