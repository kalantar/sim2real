---
name: sim2real-bootstrap
description: |
  Bootstrap a BLIS-generated experiment folder into a sim2real transfer bundle.
  Use when an experiment folder has algorithms/, workloads/, and config but lacks
  transfer.yaml, baselines/, or a component submodule. Derives and assembles all
  artifacts needed for the sim2real pipeline (transfer.yaml, baseline scenarios,
  component submodule, workload selection) with user approval at each gate.
argument-hint: "[experiment-folder-path]"
user-invocable: true
---

# sim2real-bootstrap

Turn a BLIS-generated experiment folder into a pipeline-ready sim2real bundle.

## Arguments

`$ARGUMENTS` is the path to the experiment folder. Defaults to the current directory if omitted.

```bash
EXPERIMENT_ROOT=$(realpath "${ARGUMENTS:-.}")
SIM2REAL=$(realpath "$(dirname "$0")/../..")  # or detect from context
```

If no argument given, use the current working directory as the experiment folder.

## Prerequisites

Verify before starting:
```bash
test -d "$EXPERIMENT_ROOT/algorithms" || { echo "ERROR: no algorithms/ dir"; exit 1; }
test -d "$EXPERIMENT_ROOT/workloads"  || { echo "ERROR: no workloads/ dir"; exit 1; }
```

The experiment folder MUST contain:
- `algorithms/*.go` — simulation-evolved algorithm source
- `workloads/*.yaml` — benchmark workload definitions
- Configuration source (one of: `config.md`, `config.yaml`, structured JSON)

## Task Execution

Use TaskCreate to track progress. Execute tasks sequentially; each
derive-and-approve task MUST pause for user confirmation via AskUserQuestion.

---

### Task 0: Ensure .gitignore

**action:** create-if-missing

Check if `.gitignore` exists in experiment root. If missing, create:

```
# Temporary
tmp/

# Python
.venv/
__pycache__/
*.pyc
*.pyo
*.egg-info/
dist/
build/

# macOS
.DS_Store
._*

# temporary folder if one
tmp/
```

---

### Task 1: Derive target component repository

**action:** derive-and-approve

Scan the experiment folder for references to the target component repo.

**Scan sources:**
- Go import paths in `algorithms/*.go` (pattern: `github.com/<org>/<repo>/...`)
- Code blocks in `README.md` with import paths
- Commit references in `config.md` tables (column: "Component" or "llm-d")
- Any existing `.gitmodules`

**Extract per candidate:**
- `repo_url`: full GitHub URL
- `repo_name`: last path segment (e.g., `llm-d-router`)
- `ref_from_folder`: pinned commit if found in docs
- `evidence`: list of file:line references

**Determine ref to pin:**

Prefer latest stable release tag. Fall back to main/commit if required interfaces
don't exist in any release:

```bash
cd /tmp && git clone --bare "$COMPONENT_URL" component-check.git && cd component-check.git
LATEST_RELEASE=$(git tag --sort=-version:refname | grep -v rc | head -1)
# Check if interfaces used by the algorithm exist at that tag
# If yes: COMPONENT_REF=$LATEST_RELEASE
# If no: use commit from folder docs or latest main
```

**Present to user with AskUserQuestion:**
```
Derived target component:
  repo: <url>
  evidence: <file:line list>
  ref selection:
    - latest release: <tag> (interfaces present? yes/no)
    - folder-pinned commit: <ref> (interfaces present? yes/no)
    - recommended: <chosen ref> (<reason>)
```

If >1 candidate found, halt and report — bootstrap supports only one target.

**Output variables for subsequent tasks:**
```bash
COMPONENT_URL="https://github.com/<org>/<repo>"
COMPONENT_NAME="<repo>"
COMPONENT_REF="<chosen ref>"
```

---

### Task 2: Add component submodule

**action:** shell  
**depends:** task-1

```bash
cd "$EXPERIMENT_ROOT"
git submodule add "$COMPONENT_URL" "$COMPONENT_NAME"
cd "$COMPONENT_NAME"
git fetch --tags
git checkout "$COMPONENT_REF"
cd ..
git add .gitmodules "$COMPONENT_NAME"
```

Verify:
```bash
test -d "$COMPONENT_NAME/.git" && (cd "$COMPONENT_NAME" && git log --oneline -1)
```

---

### Task 3: Generate baseline scenario(s)

**action:** derive-and-approve

Generate llm-d-benchmark scenario YAML(s) for baseline arm(s).

**Bundled scripts** (in this skill directory — invoke in place, do NOT copy):
- `generate_from_config.py` — parses `config.md` markdown tables into scenario
  YAMLs with provenance comments. Preferred path for most experiments.
- `generate_scenarios.py` — converts JSON config (`top3_selection.json`) into
  scenario YAMLs. Use only when a JSON input file exists.
- `generate_scenarios.README.md` — documents field mappings, lookup tables,
  omission rules, and coverage gaps for the JSON-input path.

**Process:**

1. Determine input format:
   - If `config.md` exists (most experiments): use `generate_from_config.py`
   - If `scenario_input.json` or `top3_selection.json` exists: use `generate_scenarios.py`

2. If using `generate_from_config.py` (preferred):
   ```bash
   SKILL_DIR="<this skill's directory>"
   python3 "$SKILL_DIR/generate_from_config.py" "$EXPERIMENT_ROOT/config.md" \
       -o "$EXPERIMENT_ROOT/baselines/"
   ```
   The script will:
   - Parse the vLLM configuration table from config.md
   - Apply MODEL_METADATA and HARDWARE_LABELS lookups
   - Emit `--no-enable-prefix-caching` unless explicitly enabled
   - Write YAML with inline provenance comments showing each value's source
   - Error on unknown models/hardware (update lookup tables in the script if needed)

3. If using `generate_scenarios.py` (JSON input):
   ```bash
   python3 "$SKILL_DIR/generate_scenarios.py" "$INPUT_FILE" "$EXPERIMENT_ROOT/baselines/"
   ```
   Review any "unknown fields" warnings from the script's output.

4. If neither input exists, flag to user — cannot generate baseline without config.

**Output schema per baseline:**
```yaml
scenario:
- name: <lowercase-alphanumeric, no hyphens, 1-20 chars>

  model:
    name: <model HuggingFace ID>
    shortName: <derived>
    path: <derived>
    huggingfaceId: <same as name>
    size: <storage estimate>
    maxModelLen: <from lookup or config>
    blockSize: <from vllm_args>
    gpuMemoryUtilization: <from vllm_args>

  decode:
    replicas: <from config>
    acceleratorType:
      labelKey: nvidia.com/gpu.product
      labelValue: <from hardware lookup>
    vllm:
      additionalFlags:
      - "--flag=value"
```

**Constraints:**
- Scenario names MUST be lowercase alphanumeric only (1-20 chars, no hyphens).
  The manifest validator enforces this. Sanitize if generate script produces hyphens.

**Present to user:** Show generated scenario(s) with key fields (replicas, TP,
max-num-seqs, hardware) and ask for approval.

**Verify:**
```bash
for f in baselines/*.yaml; do
  python3 -c "import yaml; yaml.safe_load(open('$f'))" && echo "OK: $f"
done
```

---

### Task 4: Select workloads

**action:** shell

Include all YAML files in `workloads/`. No filtering — the benchmark harness
handles dormant workloads gracefully, and including them confirms the algorithm
is inert under low load.

```bash
ls "$EXPERIMENT_ROOT"/workloads/*.yaml
```

**Output:** ordered list of all workload paths (alphabetical) for transfer.yaml.

---

### Task 5: Create transfer.yaml

**action:** derive-and-approve  
**depends:** task-2, task-3, task-4

Assemble transfer manifest from all prior task outputs.

**Derivation:**

1. `scenario`: from experiment folder name or README title
2. `component`: from task-1 ($COMPONENT_NAME, $COMPONENT_REF)
3. `component.kind`: scan algorithm Go source for interface references:
   - `UsageLimitPolicy` / flowcontrol -> `EndpointPickerConfig`
   - `Scorer` / scheduling -> `EndpointPickerConfig`
   - `Admission` -> `AdmissionPolicyConfig`
4. `component.build.commands`: standard Go build + test for relevant package
5. `baselines`: from task-3 output
6. `algorithms`: from `algorithms/*.go` — each file with a Factory function
   or plugin registration pattern is a candidate
7. `workloads`: from task-4 output
8. `context.files`: files in experiment root with deployment config or signal
   mapping (README.md, config.md, etc.)
9. `context.text`: summary of target interface and placement from algorithm source

**Output schema:**
```yaml
kind: sim2real-transfer
version: 3

scenario: <derived>

component:
  repo: <$COMPONENT_NAME>
  kind: <derived from algorithm interface>
  ref: <$COMPONENT_REF>
  base_image:
    hub: <derived from component repo org, e.g., ghcr.io/llm-d>
    name: <$COMPONENT_NAME>
  build:
    commands: <derived from algorithm imports>

algorithms:
  - name: <lowercase-alphanumeric>
    source: <relative path to algorithm .go file>
    defaults: <name of baseline from task-3 — must match baselines[].name>

baselines:
  - name: <from task-3>
    scenario: <path to baseline YAML>

workloads: <list from task-4>

context:
  text: <derived summary>
  files: <list of context files>
```

**Constraints:**
- `component.repo` must equal the submodule directory name
- All names (baselines, algorithms) must be lowercase alphanumeric only, 1-20 chars
- `context.files` paths resolved relative to experiment root
- `component` required when `algorithms` is non-empty

**Present to user:** Show summary (scenario, component@ref, algorithm count,
baseline count, workload count, context files) and ask for approval.

---

### Task 6: Verify pipeline can load manifest

**action:** shell  
**depends:** task-5

```bash
python3 -c "
import sys; sys.path.insert(0, '$SIM2REAL/pipeline/lib')
from manifest import load_manifest
m = load_manifest('$EXPERIMENT_ROOT/transfer.yaml')
print('Manifest valid')
print(f'  scenario: {m[\"scenario\"]}')
print(f'  component.repo: {m[\"component\"][\"repo\"]}')
print(f'  component.ref: {m[\"component\"].get(\"ref\", \"none\")}')
print(f'  baselines: {[b[\"name\"] for b in m[\"baselines\"]]}')
print(f'  algorithms: {[a[\"name\"] for a in m[\"algorithms\"]]}')
print(f'  workloads: {m[\"workloads\"]}')
print(f'  context.files: {m[\"context\"][\"files\"]}')
"
```

Exit code 0 and all fields printed = success.

---

## Final File Tree

```
<experiment-root>/
  transfer.yaml                  <- task-5
  baselines/
    <name>.yaml                  <- task-3
  <component-name>/             <- task-2 (submodule)
  algorithms/
    <algorithm>.go               <- pre-existing
  workloads/
    <workload>.yaml              <- pre-existing
  .gitignore                     <- task-0
```

## After Bootstrap

Tell the user:
```
Bootstrap complete. Next steps:
  1. Run: python pipeline/setup.py --experiment-root $EXPERIMENT_ROOT
  2. Run: python pipeline/prepare.py --experiment-root $EXPERIMENT_ROOT
  3. The translate skill will be invoked at Phase 3
```

## Reference: Bundled Files

This skill ships with supporting files in its directory. Invoke in place — do NOT copy to experiment roots.

| File | Purpose |
|------|---------|
| `generate_from_config.py` | Parses `config.md` markdown tables → scenario YAMLs with provenance comments. Preferred for most experiments. Handles hardware normalization, safe defaults (`--no-enable-prefix-caching`), and unknown model/hardware detection. |
| `generate_scenarios.py` | Converts JSON config (`top3_selection.json`) → scenario YAMLs. Use when JSON input exists. |
| `generate_scenarios.README.md` | Coverage map for the JSON-input path. Documents field mappings, omission rules, and gaps. |
