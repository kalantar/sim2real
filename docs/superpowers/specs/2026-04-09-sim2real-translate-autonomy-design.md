# sim2real-translate Autonomy Design

**Date:** 2026-04-09
**Status:** Approved

## Problem

The current `sim2real-translate` skill is constrained by static target config in
`env_defaults.yaml`: `plugin_dir`, `register_file`, `rewrite_file`, and `package` must be
pre-specified before the skill runs. This breaks down when an algorithm requires creating
multiple files, modifying files outside the expected plugin directory, or when the right
approach (create vs. rewrite) can only be determined after reading the codebase and user hints.

## Approach: Hint-driven Autonomous Translation

The skill becomes fully autonomous about what files to touch. `env_defaults.yaml` target
config shrinks to just `repo`. User-provided transfer hints (inline text + files) are
embedded in `skill_input.json` by `prepare.py` and injected dynamically in-session during
translation — not baked into any cached file.

The cached context document remains a pure snapshot of codebase state (interfaces, examples,
registration patterns). Hints are ephemeral, per-transfer, and read directly in the skill's
main session (not via subagent injection or disk write).

---

## Changes

### 1. `transfer.yaml` — new `hints` section

```yaml
hints:
  text: |
    Modify the existing precise_prefix_cache.go scorer to implement the new
    adaptive routing algorithm. Do not create a new file.
  files:
    - sim2real_golden_correct/transfer_hint.md

context:
  files:
    - sim2real_golden_correct/README.md
```

- `hints.text` — inline free-form transfer guidance; replaces `context.notes` (deprecated)
- `hints.files` — paths to hint files (e.g. `transfer_hint.md`); contents are read by
  `prepare.py` and embedded in `skill_input.json`
- `context.files` — codebase-understanding material that feeds the cached context doc
- `hints` section is **optional**: when absent or both `hints.text` and `hints.files` are
  empty/absent, `prepare.py` sets `hints: {text: "", files: []}` in `skill_input.json` and
  the skill proceeds with purely autonomous exploration (no error)
- `context.notes` is deprecated. If present, `load_manifest` emits a warning:
  `"context.notes is deprecated; use hints.text instead"`. It does NOT error. The value is
  ignored (not migrated automatically). Users should manually move it to `hints.text`.

### 2. `env_defaults.yaml` — `target` shrinks to `repo` only

Before:
```yaml
target:
  repo: llm-d-inference-scheduler
  plugin_dir: pkg/plugins/scorer/
  register_file: pkg/plugins/register.go
  package: scorer
  rewrite_file: pkg/plugins/scorer/precise_prefix_cache.go  # optional
build:
  commands: [...]
  test_scope: ./pkg/plugins/scorer/...
```

After:
```yaml
target:
  repo: llm-d-inference-scheduler
build:
  commands: [...]
```

Removed from `target`: `plugin_dir`, `register_file`, `rewrite_file`, `package`. The skill
discovers and decides all of these based on hints + codebase exploration.

`build.test_scope` is removed. `prepare.py` Phase 3 no longer appends a scoped test command
to `build_commands` (current code lines 216–218 are removed). `build.commands` (go build,
go vet) is kept. The skill determines the test scope from whatever package it modified and
writes the full `test_commands` list to `translation_output.json`.

`config.kind`, `config.helm_path`, `gaie`, `stack`, `observe`: all unchanged.

### 3. `prepare.py` and `pipeline/lib/manifest.py` — manifest loading changes

**`manifest.py` (`load_manifest`):**
- Validates `hints.files` entries exist on disk (same error behavior as `context.files`)
- Emits warning if `context.notes` is present (deprecated)
- `hints` section is optional: missing `hints` key → treat as `{text: "", files: []}`
- Reads hint file contents and embeds them in the parsed manifest

**`prepare.py` Phase 3 (`_phase_translate`):**
- Writes `hints` into `skill_input.json`:

```json
{
  "target": {
    "repo": "llm-d-inference-scheduler"
  },
  "hints": {
    "text": "Modify the existing precise_prefix_cache.go ...",
    "files": [
      {
        "path": "sim2real_golden_correct/transfer_hint.md",
        "content": "<full file content>"
      }
    ]
  }
}
```

- Removes from `skill_input.json`: `target.plugin_dir`, `target.register_file`,
  `target.package`, `target.rewrite_file`, `context_notes`
- Removes test command appending (current lines 216–218: `test_scope` + append)

### 4. Context document (cached file) — no hints section

The cached context doc contains only stable codebase state. Hints are never written to it.

```
# Translation Context
Scenario: $SCENARIO | inference-sim@<sha> | llm-d@<sha>

## Production Interfaces
[Scorer/Admission interface, EndpointPickerConfig — from source]

## Example Plugin: <filename>
[full contents]

## Plugin Registration
[full contents of register.go]

## <additional context files>
[full contents]
```

The context hash covers only this content. Hint changes never invalidate the cache.

### 5. SKILL.md changes

**Validation block (current lines 103–108): remove `plugin_dir`, `register_file`,
`package` from required target fields.** Only `repo` is required:

```python
required_target = ['repo']
missing = [f for f in required_target if f not in si['target']]
```

**Shell variable loading: remove** `PLUGIN_DIR`, `REGISTER_FILE`, `PACKAGE`,
`REWRITE_FILE`, `CONTEXT_NOTES`.

**Add hints loading:**
```bash
HINTS_TEXT=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json')).get('hints', {}).get('text', ''))")
HINTS_FILES=$(python3 -c "
import json
hints = json.load(open('$RUN_DIR/skill_input.json')).get('hints', {}).get('files', [])
for f in hints:
    print(f'### {f[\"path\"]}')
    print(f['content'])
    print()
")
```

**Step 2 (Translate): replace branching on `REWRITE_FILE` with autonomous flow:**
1. Read `skill_input.json` → extract `HINTS_TEXT` and `HINTS_FILES` content directly in
   the main session (not via subagent; not written to disk)
2. Read the cached context doc (`$CONTEXT_PATH`)
3. Proceed with hints as the mandate and context as the codebase reference
4. Explore `$TARGET_REPO` freely — no directories restricted
5. Decide create/modify/both based on hints + exploration
6. Write all changes to `$TARGET_REPO`
7. Write `translation_output.json`

**Step 6 (Output): `generated/` directory is a flat snapshot for operator reference:**

Files are copied by basename (`Path(f).name`) as today — `generated/adaptive.go`,
`generated/adaptive_test.go`, `generated/treatment_config.yaml`, etc. The full path of
each file is already captured in `translation_output.json` (`files_created`,
`files_modified`), which is the authoritative record of where each file lives in the
target repo. `generated/` is a convenience snapshot; `translation_output.json` is the
provenance record.

This behavior is unchanged from today.

**Step 5 (Review): add `--hints-json` to `review.py` invocation:**
```bash
HINTS_JSON=$(python3 -c "
import json
si = json.load(open('$RUN_DIR/skill_input.json'))
print(json.dumps(si.get('hints', {'text': '', 'files': []})))
")

python3 .claude/skills/sim2real-translate/scripts/review.py \
    --plugin-files $PLUGIN_FILES \
    --algorithm "$ALGO_SOURCE" \
    --algorithm-config "$ALGO_CONFIG" \
    --context "$CONTEXT_PATH" \
    --treatment-config "$RUN_DIR/treatment_config.yaml" \
    --hints-json "$HINTS_JSON" \
    --round $ROUND_NUM \
    --out "$RUN_DIR/review/round_${ROUND_NUM}.json" \
    $([ "$DEV_MODE" = "true" ] && echo "--dev")
```

`review.py` uses hints when evaluating whether the translation honored the user's mandate.
`--hints-json` is optional (empty hints → review evaluates correctness only).

### 6. `translation_output.json` — schema changes

Replace `needs_custom_config` (bool) + `suggested_config` (null or object) with
`treatment_config_generated` (bool). Add `register_file`. Total: 10 fields.

**`treatment_config_generated` contract:**
- `true` means the skill **wrote `treatment_config.yaml` during this run**. Assembly
  reads that file. Assembly MUST verify the file exists; if missing → error.
- `false` means the skill did not write `treatment_config.yaml`. Assembly copies baseline
  EPP config into the treatment slot. If `treatment_config.yaml` exists on disk from a prior
  run, assembly ignores it (the flag is authoritative, not the file's presence).

**Final 10-field schema:**

| Field | Type | Description |
|-------|------|-------------|
| `plugin_type` | string | Kebab-case type name matching Go registration |
| `files_created` | string[] | Paths relative to target repo |
| `files_modified` | string[] | Paths relative to target repo |
| `package` | string | Go package name (skill-determined) |
| `register_file` | string\|null | Path relative to target repo where plugin was registered; `null` if no new registration (e.g. rewrite of existing plugin) |
| `test_commands` | string[][] | Full build + vet + scoped test commands |
| `config_kind` | string | Kubernetes resource kind (from `config.kind` in env_defaults) |
| `helm_path` | string | Helm values path for treatment EPP config |
| `treatment_config_generated` | bool | `true` if skill wrote `treatment_config.yaml` this run |
| `description` | string | One-line human-readable summary of the plugin written by the skill; may be empty string if no description warranted |

### 7. Assembly — `treatment_config_generated` behavior

In `_generate_algorithm_values`, after handling vllm args:

```python
if output["treatment_config_generated"]:
    # Verify file exists
    tc_path = out_path.parent / "treatment_config.yaml"
    if not tc_path.exists():
        raise RuntimeError(
            f"treatment_config_generated=true but treatment_config.yaml not found at {tc_path}"
        )
    tc_content = tc_path.read_text()
    (alg_values["stack"]
     .setdefault("gaie", {})
     .setdefault("treatment", {})
     .setdefault("helmValues", {})
     .setdefault("inferenceExtension", {})
     ["pluginsCustomConfig"]) = {"custom-plugins.yaml": tc_content}
else:
    # No custom EPP config — copy baseline config into treatment slot
    baseline_cfg = (resolved
                    .get("gaie", {})
                    .get("baseline", {})
                    .get("helmValues", {})
                    .get("inferenceExtension", {})
                    .get("pluginsCustomConfig", {}))
    if not baseline_cfg:
        # Scenario has no baseline EPP config (e.g. admission_control with helmValues: {})
        # Emit warning; leave treatment slot empty — deploy must handle it
        import warnings
        warnings.warn(
            f"treatment_config_generated=false and baseline has no EPP config; "
            f"treatment pluginsCustomConfig will be empty"
        )
    else:
        (alg_values["stack"]
         .setdefault("gaie", {})
         .setdefault("treatment", {})
         .setdefault("helmValues", {})
         .setdefault("inferenceExtension", {})
         ["pluginsCustomConfig"]) = baseline_cfg
```

Both cases always attempt to produce a populated treatment slot. When baseline has no EPP
config and `treatment_config_generated=false`, a warning is emitted and the treatment slot
is left without `pluginsCustomConfig` — this is valid for scenarios like `admission_control`
where EPP config is not driven by a custom YAML.

### 8. `validate_assembly` — use `register_file` from output

Replace the current Check 1 (which reads `target.get("register_file", "")` from resolved
config) with:

```python
register_file = output.get("register_file")
if register_file is not None:
    register_path = REPO_ROOT / target.get("repo", "") / register_file
    if register_path.exists():
        register_content = register_path.read_text()
        if plugin_type not in register_content:
            errors.append(
                f"plugin_type '{plugin_type}' not found in {register_file}"
            )
    else:
        errors.append(f"register_file not found on disk: {register_file}")
else:
    # register_file is null — rewrite mode, no new registration.
    # plugin_type is expected in the modified files; skip this check.
    pass
```

All other checks (Check 2: plugin_type in epp.yaml; Check 3: treatment_config kind match;
Check 4: files_created exist) are unchanged.

### 9. `_compile_cluster_packages` — richer cluster artifacts

**Current output per package (`cluster/{phase}/`):**
- `epp.yaml` — raw EPP plugin config text (not a K8s resource)
- `{phase}-pipeline.yaml` — full Tekton `kind: Pipeline` resource

**Problems:**
- `epp.yaml` is redundant: the EPP config is already embedded inside `{phase}-pipeline.yaml`
  as the `gaie_config` parameter (injected by `cmd_compile_pipeline` from `values.yaml`)
- There is no PipelineRun YAML — the execution request that binds a Pipeline to specific
  runtime parameters (experiment ID, namespace, workload) is missing, making it impossible
  to inspect what would actually be submitted to the cluster

**New output per package (`cluster/{phase}/`):**
- `{phase}-pipeline.yaml` — full Tekton `kind: Pipeline` resource (unchanged)
- `pipelinerun-{workload_name}.yaml` per workload — Tekton `kind: PipelineRun` resource

**`epp.yaml` is dropped.** The EPP config is fully captured inside `{phase}-pipeline.yaml`.

**PipelineRun generation:** For each workload in `values.yaml` (`observe.workloads`), write
a `pipelinerun-{workload_name}.yaml`. If a PipelineRun stub template exists in
`tektonc-data-collection/tektoncsample/sim2real/pipelinerun.yaml` (or similar), use
`render-pipelinerun` with variable substitution. Otherwise generate the minimal PipelineRun
structure directly in Python — the Pipeline name and parameter names are known from the
template (`experimentId`, `namespace`, `runName`, `workloadName`, `workloadSpec`,
`sleepDuration`):

```python
def _make_pipelinerun(phase: str, workload: dict, run_name: str, namespace: str) -> dict:
    wl_name = workload.get("name", workload.get("workload_name", "unknown"))
    return {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": {
            "name": f"sim2real-{phase}-{wl_name[:20]}",
            "namespace": namespace,
        },
        "spec": {
            "pipelineRef": {"name": f"sim2real-{phase}"},
            "params": [
                {"name": "experimentId", "value": run_name},
                {"name": "namespace",    "value": namespace},
                {"name": "runName",      "value": run_name},
                {"name": "workloadName", "value": wl_name},
                {"name": "workloadSpec", "value": yaml.dump(workload)},
            ],
            "workspaces": [
                {"name": "model-cache",    "persistentVolumeClaim": {"claimName": "model-cache"}},
                {"name": "hf-credentials", "secret": {"secretName": "hf-secret"}},
                {"name": "data-storage",   "persistentVolumeClaim": {"claimName": "data-storage"}},
            ],
        }
    }
```

`namespace` comes from `workspace/setup_config.json` (already read by `_load_setup_config`).
If namespace is absent, fall back to `"default"` with a warning.

**`validate_assembly` Check 2** currently checks `cluster/treatment/epp.yaml` — update to
check `cluster/treatment/{treatment}-pipeline.yaml` contains `plugin_type` instead (since
EPP config now lives inside the Pipeline YAML, not in a separate file).

---

## What Does NOT Change

- Context caching logic (`build_context`, hash computation, `context_file_populated` flag)
- Skill Step 1 (context enrichment with production interfaces + examples)
- Skill Steps 3–6 (build/test gate, snapshot, review loop, output copy) — Step 6 documents that `generated/` is a flat basename snapshot and `translation_output.json` is the provenance record
- `review.py` consensus logic (only the invocation gains `--hints-json`)
- `config.kind`, `config.helm_path`, `gaie`, `stack`, `observe` in `env_defaults.yaml`
- `translation_output.json` fields: `plugin_type`, `files_created`, `files_modified`,
  `package`, `test_commands`, `config_kind`, `helm_path`, `description`

---

## Migration Notes

- Existing `transfer.yaml` files using `context.notes` should move that text to `hints.text`.
  Until migrated, `context.notes` is silently ignored with a warning.
- Existing `env_defaults.yaml` scenarios should remove `target.plugin_dir`,
  `target.register_file`, `target.rewrite_file`, `target.package`, and `build.test_scope`.
  Add a `hints` section to the corresponding `transfer.yaml` to preserve the intent as user
  guidance rather than hard config.
- `cluster/{phase}/epp.yaml` is no longer generated. Any tooling that reads that file must
  be updated to read from `{phase}-pipeline.yaml` or from `values.yaml` directly.
- `cluster/{phase}/` gains per-workload `pipelinerun-{workload}.yaml` files that did not
  exist before.
- `translation_output.json` files from previous runs are schema-incompatible:
  - Missing: `register_file`, `treatment_config_generated`
  - Present (stale): `needs_custom_config`, `suggested_config`
  Old run dirs must be re-translated. The `description` field is new; old files missing it
  must also be re-translated.
