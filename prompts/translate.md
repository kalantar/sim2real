---
stage: 2
version: "1.0"
pipeline_commit: "set-at-runtime"
description: "Stage 2 — Map simulation signals to production equivalents"
---

# Stage 2: Translate

Map simulation signals from `algorithm_summary.json` to production equivalents
using `docs/transfer/blis_to_llmd_mapping.md`. Produces `workspace/signal_coverage.json`.

## Prerequisites

Verify all required input artifacts exist and are valid. **HALT if any check fails.**

```bash
# Verify algorithm_summary.json exists and is schema-valid
test -f workspace/algorithm_summary.json || { echo "HALT: missing algorithm_summary.json"; exit 1; }
.venv/bin/python tools/transfer_cli.py validate-schema workspace/algorithm_summary.json || { echo "HALT: schema validation failed"; exit 1; }

# Verify scope validation passed (schema permits false, so check explicitly)
.venv/bin/python -c "import json,sys; d=json.load(open('workspace/algorithm_summary.json')); sys.exit(0 if d.get('scope_validation_passed') is True else 1)" || { echo "HALT: scope validation not passed"; exit 1; }

# Verify mapping artifact exists
test -f docs/transfer/blis_to_llmd_mapping.md || { echo "HALT: missing mapping artifact"; exit 1; }

# Verify submodule initialized
test -d llm-d-inference-scheduler/pkg || { echo "HALT: llm-d-inference-scheduler submodule not initialized — run git submodule update --init llm-d-inference-scheduler"; exit 1; }

# Verify PrecisePrefixCache scorer exists (needed for CacheHitRate investigation)
test -f llm-d-inference-scheduler/pkg/plugins/scorer/precise_prefix_cache.go || { echo "HALT: PrecisePrefixCache scorer not found — verify submodule is initialized"; exit 1; }
```

## Stale Artifact Guard

Delete any existing output artifact to prevent Stage 3 from consuming a stale
result if this translate halts before writing.

```bash
rm -f workspace/signal_coverage.json
```

## Step 1: Submodule Staleness Check

Compare the llm-d-inference-scheduler HEAD against the mapping artifact's pinned commit.
Use prefix matching (mapping artifact stores abbreviated 7-char hash).

```bash
# Extract mapping artifact's pinned hash (do NOT hardcode — extract at runtime)
MAPPING_HASH=$(awk '/Pinned commit hash:/ { match($0, /[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]/); if (RSTART>0) print substr($0, RSTART, 7) }' docs/transfer/blis_to_llmd_mapping.md)

# Verify extraction succeeded
[ -n "${MAPPING_HASH}" ] || { echo "HALT: could not extract pinned commit hash from mapping artifact"; exit 1; }

# Get full submodule HEAD and compare prefix
SUBMODULE_HEAD=$(git -C llm-d-inference-scheduler rev-parse HEAD)
echo "${SUBMODULE_HEAD}" | grep -q "^${MAPPING_HASH}" || { echo "HALT: stale submodule commit — mapping artifact pinned at ${MAPPING_HASH}, submodule at ${SUBMODULE_HEAD}"; exit 1; }
```

**Store `SUBMODULE_HEAD`** — it becomes the `commit_hash` field in the output artifact.

**HALT if hashes differ** — signal mappings may have changed between commits.
Write `workspace/escalation.json` with `halt_reason: "stale_submodule_commit"`.

## Step 2: Signal Mapping

For each signal in `workspace/algorithm_summary.json` `signals[]`:

1. Look up the signal's `name` in `docs/transfer/blis_to_llmd_mapping.md`.
2. Record the production equivalent field name (`prod_name`).
3. Record the Go access path (`prod_access_path`).
4. Record the fidelity rating (`fidelity_rating`: high, medium, or low).
5. Set `staleness_window_ms` to 0 (v1 approximate-scorer signals).
6. Set `mapped: true`.

### Normalization Propagation

For each signal with a `normalization_note` in `algorithm_summary.json`:
- Extract the action key (e.g., `divide_prod_by_100`, `verify_and_normalize`, `boolean_presence_check`).
- Set the `normalization` field in the output to this action key.

### Fidelity Provisional Propagation

For each signal where `fidelity_provisional: true` in `algorithm_summary.json`:
- Copy `fidelity_provisional: true` to the corresponding signal in the output.

## Step 3: Unknown-Type Signal Resolution (Cross-PR Contract #3)

For each signal with `type: "unknown"`:

1. Look up the signal name in `docs/transfer/blis_to_llmd_mapping.md` — if a mapping exists, assign the documented type and proceed.
2. If not found, check `algorithm_summary.json` context (composite_signals, normalization_note) for type inference clues.
3. If type remains unresolvable, move the signal to `unmapped_signals[]` with a note, write the artifact with `coverage_complete: false`, and **HALT** with `halt_reason: "unknown_signal_unresolved"`.
4. **Do NOT silently drop signals** — every signal must be explicitly resolved or rejected.

## Step 4: F-10 Double-Counting Detection (Cross-PR Contract #4)

**Skip this step entirely if `composite_signals` in `workspace/algorithm_summary.json` is empty.** An empty `composite_signals: []` means the EVOLVE-BLOCK contains no composite method calls (e.g., no `EffectiveLoad()`), so there is no composite to check for double-counting. Always read the artifact to determine this — do not assume based on prior runs.

Check if two signals map to the same production metric:

- If `InFlightRequests` falls back to `RunningRequestsSize`, then EffectiveLoad becomes `WaitingQueueSize + 2*RunningRequestsSize` — double-counting that metric.
- **Detection:** Check if any two signals in the `EffectiveLoad` composite (`QueueDepth`, `BatchSize`, `InFlightRequests`) share the same `prod_access_path`.
- **If double-counting detected:** Add a `notes` field warning about the risk and document the mitigation (use single count or different proxy).
- **HALT** if double-counting would make the composite uncomputable. Write `workspace/escalation.json` with `halt_reason: "unmappable_signal"`.

## Step 5: CacheHitRate Investigation (Cross-PR Contract #5)

**Skip this step entirely if `CacheHitRate` is not present in `workspace/algorithm_summary.json` `signals[]`.** The current blis_router EVOLVE-BLOCK does not use CacheHitRate; executing this step when the signal is absent would inject a spurious entry into `signal_coverage.json` and corrupt the `coverage_complete` determination. Always read the artifact to determine this — do not assume based on prior runs.

The mapping artifact marks CacheHitRate's production access path as UNVERIFIED.
Derive the concrete access path from PrecisePrefixCache:

1. Read `llm-d-inference-scheduler/pkg/plugins/scorer/precise_prefix_cache.go`.
2. Identify how prefix cache hit rate information is accessed.
3. If a standard `GetMetrics()` field exists, use it as the `prod_access_path`.
4. If the scorer uses a ZMQ-based indexer or non-standard access, document the
   actual mechanism in the `notes` field and set `prod_access_path` to the
   identified code path.
5. If no access path can be determined, set `mapped: true` with `prod_access_path: "UNVERIFIED"` and add a `notes` field explaining the gap.

## Step 6: Pre-Write Validation

Before writing the final artifact, validate the JSON against the schema:

```bash
# Write directly to the final location (validate-schema auto-discovers the schema
# from the filename stem, so the file must be named signal_coverage.json)
mkdir -p workspace
cat > workspace/signal_coverage.json << 'ARTIFACT_EOF'
{JSON content here}
ARTIFACT_EOF

# Validate the written artifact
.venv/bin/python tools/transfer_cli.py validate-schema workspace/signal_coverage.json || { echo "HALT: pre-write validation failed"; rm -f workspace/signal_coverage.json; }
```

**HALT if pre-write validation fails** with `halt_reason: "pre_write_validation_failure"`.

## Step 7: Write Output

Write `workspace/signal_coverage.json` with all required fields:

- `signals`: array of mapped signal objects (sim_name, prod_name, prod_access_path, fidelity_rating, staleness_window_ms, mapped, plus optional normalization, notes, fidelity_provisional)
- `unmapped_signals`: array of unmapped signal names (empty if all mapped)
- `commit_hash`: set to `SUBMODULE_HEAD` from Step 1
- `coverage_complete`: true if and only if `unmapped_signals` is empty

## Step 8: Post-Write Validation

```bash
.venv/bin/python tools/transfer_cli.py validate-schema workspace/signal_coverage.json
```

**HALT if validation fails.**

## Halt Conditions

| Condition | halt_reason | Action |
|-----------|-------------|--------|
| Stale submodule commit | `stale_submodule_commit` | Write escalation.json, HALT |
| Unmappable signal | `unmappable_signal` | Write escalation.json, HALT |
| Coverage incomplete | `coverage_incomplete` | Write escalation.json, HALT |
| Unknown-type signal unresolved | `unknown_signal_unresolved` | Write artifact with coverage_complete:false, write escalation.json, HALT |
| Pre-write validation failure | `pre_write_validation_failure` | Write escalation.json, HALT |

On any halt, write `workspace/escalation.json` per the escalation schema (`tools/schemas/escalation.schema.json`) with `"stage": 2` and the appropriate `halt_reason`.

## Expected Outputs

- `workspace/signal_coverage.json` — signal mapping artifact consumed by Stage 3 and Stage 5
