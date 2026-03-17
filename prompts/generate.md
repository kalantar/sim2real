---
stage: 3
version: "1.0"
pipeline_commit: "set-at-runtime"
description: "Stage 3 — Generate production scorer plugin from evolved algorithm"
---

# Stage 3: Generate

Generate a production `scheduling.Scorer` plugin for llm-d-inference-scheduler
from the evolved algorithm. This is the most complex stage — it reads the scorer
template, algorithm summary, signal coverage, and mapping artifact to produce
Go source files implementing the evolved scoring logic.

## Prerequisites

Verify all required input artifacts exist and are valid. **HALT if any check fails.**

```bash
# algorithm_summary.json: exists + schema valid + scope passed
test -f workspace/algorithm_summary.json || echo "HALT: missing algorithm_summary.json"
.venv/bin/python tools/transfer_cli.py validate-schema workspace/algorithm_summary.json
.venv/bin/python -c "import json,sys; d=json.load(open('workspace/algorithm_summary.json')); sys.exit(0 if d.get('scope_validation_passed') is True else 1)"

# signal_coverage.json: exists + schema valid + coverage complete
test -f workspace/signal_coverage.json || echo "HALT: missing signal_coverage.json"
.venv/bin/python tools/transfer_cli.py validate-schema workspace/signal_coverage.json
.venv/bin/python -c "import json,sys; d=json.load(open('workspace/signal_coverage.json')); sys.exit(0 if d.get('coverage_complete') is True and len(d.get('unmapped_signals',[])) == 0 else 1)"

# Submodule staleness check: signal_coverage.json commit_hash must match HEAD
.venv/bin/python -c "
import json, subprocess, sys
d = json.load(open('workspace/signal_coverage.json'))
head = subprocess.check_output(['git', '-C', 'llm-d-inference-scheduler', 'rev-parse', 'HEAD']).decode().strip()
match = head.startswith(d['commit_hash']) or d['commit_hash'].startswith(head)
sys.exit(0 if match else 1)
"

# Scorer template and mapping artifact
test -f docs/transfer/scorer_template.go.md || echo "HALT: missing scorer template"
test -f docs/transfer/blis_to_llmd_mapping.md || echo "HALT: missing mapping artifact"
```

On HALT, write `workspace/escalation.json` with the appropriate `halt_reason`:
- Missing signal_coverage.json → `"missing_signal_coverage"`
- Commit hash mismatch → `"stale_signal_coverage"`

## Stale Artifact Guard

Delete any existing output artifacts to prevent stale files from a prior run.

```bash
rm -f workspace/stage3_output.json
# Also remove any previously generated scorer files (read algorithm_name from summary)
ALGO_NAME=$(.venv/bin/python -c "import json; print(json.load(open('workspace/algorithm_summary.json'))['algorithm_name'])" 2>/dev/null || true)
if [ -n "$ALGO_NAME" ]; then
    SANITIZED=$(echo "$ALGO_NAME" | tr ' .-' '_' | tr -s '_' | tr '[:upper:]' '[:lower:]' | sed 's/^_//;s/_$//')
    rm -f "llm-d-inference-scheduler/pkg/plugins/scorer/${SANITIZED}.go"
    rm -f "llm-d-inference-scheduler/pkg/plugins/scorer/${SANITIZED}_test.go"
fi
```

## Step 1: EVOLVE-BLOCK Content Hash Verification

Re-verify the EVOLVE-BLOCK content hash independently (do not rely on Stage 1 verification — source may have changed between stages).

1. Read `evolve_block_content_hash` and `evolve_block_source` from `workspace/algorithm_summary.json`.
2. Read the source file at the specified path and line range.
3. Extract the EVOLVE-BLOCK lines, join with `\n` (no trailing newline).
4. Compute SHA-256 and compare to the stored hash.

**HALT on mismatch** with `halt_reason: "evolve_block_hash_mismatch_stage3"`.

## Step 2: UNVERIFIED Field Resolution

Read `docs/transfer/scorer_template.go.md` and identify fields marked `UNVERIFIED`.
Check the scorer template header for a HALT CONDITION specifying the threshold.

- If fewer than the threshold number of UNVERIFIED fields can be resolved, **HALT** with `halt_reason: "unverified_field_threshold_not_met"`.
- If no explicit threshold is specified, treat ANY unresolved UNVERIFIED field as a halt condition.

For each UNVERIFIED field:
1. Check if the field exists in the `sigs.k8s.io/gateway-api-inference-extension` Metrics type.
2. If found, mark as resolved. If not found, check for alternative field names.
3. Document resolution in the generated code comments.

## Step 3: Code Generation

Parse the EVOLVE-BLOCK and generate production scorer code:

### 3a: Parse Scoring Logic

Read the EVOLVE-BLOCK from `blis_router/best/best_program.go` and identify:
- Scoring weights applied to each signal
- Penalty functions (e.g., cubic load penalty, KV pressure penalty)
- Decision thresholds (e.g., score cutoffs, load thresholds)
- Composite signal computations (e.g., EffectiveLoad formula)
- Conditional branching logic (e.g., SessionID affinity check)

### 3b: Map Signals to Production Fields

For each signal, use `workspace/signal_coverage.json` to get:
- `prod_access_path` — the Go code to access this signal from the endpoint
- `normalization` — any required normalization (e.g., `divide_prod_by_100`)

### 3c: Apply Normalization

For each signal with a `normalization` field:
- `divide_prod_by_100`: divide production value by 100 (e.g., `KVUtilization`)
- `verify_and_normalize`: check scale and normalize if needed (e.g., `CacheHitRate`)
- `boolean_presence_check`: convert to boolean (e.g., `SessionID != ""`)

If a signal has no `normalization` field:
1. Check `algorithm_summary.json` `normalization_note` as a fallback source.
2. If neither source provides normalization, treat as identity (no scaling) and emit a `// WARNING: no normalization specified for <signal_name>` comment.

### 3d: Generate Scorer File

Use `docs/transfer/scorer_template.go.md` as the structural reference:
- Section 1: Package declaration and imports
- Section 2: Type definition and compile-time assertions
- Section 3: Factory function (follow existing scorer patterns)
- Section 4: `TypedName()` and `Category()` methods
- Section 5: `Score()` method with production signal access, normalization, and evolved scoring logic
- Section 6: Test patterns
- Section 7: Registration

Output file: `llm-d-inference-scheduler/pkg/plugins/scorer/<name>.go`
where `<name>` = `algorithm_name` from `algorithm_summary.json`, sanitized to snake_case.

**Snake_case rules:** replace spaces/hyphens/dots with underscores, collapse consecutive underscores, strip leading/trailing underscores, lowercase, prepend `scorer_` if starts with digit, strip non-ASCII.

### 3e: Generate Test File

Generate `<name>_test.go` following Section 6 patterns from the scorer template.

### 3f: Add Registration

Add the scorer factory registration to `llm-d-inference-scheduler/pkg/plugins/register.go`.

## Step 4: CacheHitRate Handling

Check `workspace/signal_coverage.json` for the CacheHitRate signal:

- **If mapped** (has a `prod_access_path`): use that path in generated code.
- **If unmapped or `prod_access_path: "UNVERIFIED"`**: emit `// CacheHitRate: production access path unavailable — using zero fallback` and assign `cacheHitRate = 0.0`.
- **HALT if CacheHitRate is used as a multiplier** in the EVOLVE-BLOCK scoring logic (a zero fallback would zero out the entire score). Use `halt_reason: "cache_hit_rate_unavailable_stage3"`.

## Step 5: F-10 Double-Counting Guard

Check if any two signals in the EffectiveLoad composite share the same `prod_access_path`:

- **If double-counting detected**: use `RunningQueueSize` once with adjusted coefficient (option a) or use `RunningRequestCount` as an alternative proxy (option b).
- **If neither alternative available**: emit a `// WARNING: F-10 double-counting risk` comment and use the single-count approach.
- **HALT only if** double-counting would affect >50% of the scoring weight.

## Step 6: Stage 3 Output Validation

Before writing `workspace/stage3_output.json`, verify:

1. **No PLACEHOLDER markers** remain in the generated scorer code.
2. **Structural invariants** from scorer_template.go.md Section 7:
   - Import paths unchanged
   - Type assertion present (`var _ scheduling.Scorer = &...{}`)
   - Factory function registered
   - UNVERIFIED fields remain commented-out if unresolved
3. **Do NOT compile** (`go build`) — compilation is deferred to Stage 4.

## Step 7: Write stage3_output.json

Construct and validate the output artifact:

```bash
.venv/bin/python tools/transfer_cli.py validate-schema workspace/stage3_output.json
```

**HALT if validation fails** with `halt_reason: "post_write_validation_failure_stage3"`.

## Halt Conditions

| Condition | halt_reason | Action |
|-----------|-------------|--------|
| Missing signal_coverage.json | `missing_signal_coverage` | Write escalation.json, HALT |
| Stale signal_coverage commit | `stale_signal_coverage` | Write escalation.json, HALT |
| EVOLVE-BLOCK hash mismatch | `evolve_block_hash_mismatch_stage3` | Write escalation.json, HALT |
| UNVERIFIED field threshold | `unverified_field_threshold_not_met` | Write escalation.json, HALT |
| CacheHitRate unavailable (multiplier) | `cache_hit_rate_unavailable_stage3` | Write escalation.json, HALT |
| Pre-write validation failure | `pre_write_validation_failure_stage3` | Write escalation.json, HALT |
| Post-write validation failure | `post_write_validation_failure_stage3` | Write escalation.json, HALT |

On any halt, write `workspace/escalation.json` per the escalation schema with `"stage": 3` and the appropriate `halt_reason`.

## Expected Outputs

- `llm-d-inference-scheduler/pkg/plugins/scorer/<name>.go` — generated scorer plugin
- `llm-d-inference-scheduler/pkg/plugins/scorer/<name>_test.go` — generated tests
- `llm-d-inference-scheduler/pkg/plugins/register.go` — modified with new registration
- `workspace/stage3_output.json` — stage output artifact with:
  - `scorer_file`: path to generated scorer
  - `test_file`: path to generated test file
  - `register_file`: path to register.go
  - `scorer_type`: the TypedName type string
