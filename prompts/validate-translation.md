---
stage: 3.5
version: "1.0"
pipeline_commit: "set-at-runtime"
description: "Stage 3.5 — Verify generated scorer faithfully implements the EVOLVE-BLOCK algorithm"
---

# Stage 3.5: Validate Translation

Verify that the generated scorer plugin is a faithful implementation of the evolved
EVOLVE-BLOCK algorithm. Stage 3 (Generate) produces Go code from `algorithm_summary.json`
and the EVOLVE-BLOCK pseudocode; this stage verifies the translation is correct before
Stage 4 runs the build/test loop.

**Run this stage in a fresh LLM session** — not the session that generated the code.
Self-review bias reduces the chance of catching errors the generator introduced.

Three layers of checking:
1. **Mechanical pre-checks** (CLI): signal presence, constant audit, normalization
2. **LLM logic trace**: step-by-step EVOLVE-BLOCK → scorer comparison
3. **Fix-and-retry loop**: fix critical/important deviations, re-validate, halt after N retries

## Prerequisites

Verify the following artifacts exist and are schema-valid. **HALT if any check fails.**

```bash
# Stage 3 output artifact: exists + schema valid
test -f workspace/stage3_output.json || { echo "HALT: missing stage3_output.json"; exit 1; }
.venv/bin/python tools/transfer_cli.py validate-schema workspace/stage3_output.json || { echo "HALT: stage3_output.json schema validation failed"; exit 1; }

# Read scorer file path from stage3_output.json
SCORER_FILE=$(.venv/bin/python -c "import json; print(json.load(open('workspace/stage3_output.json'))['scorer_file'])")

# Verify scorer exists on disk
test -f "$SCORER_FILE" || { echo "HALT: scorer file missing: $SCORER_FILE"; exit 1; }

# algorithm_summary.json: exists + schema valid + scope passed
test -f workspace/algorithm_summary.json || { echo "HALT: missing algorithm_summary.json"; exit 1; }
.venv/bin/python tools/transfer_cli.py validate-schema workspace/algorithm_summary.json || { echo "HALT: algorithm_summary.json schema validation failed"; exit 1; }
SCOPE_PASSED=$(.venv/bin/python -c "import json; print(json.load(open('workspace/algorithm_summary.json')).get('scope_validation_passed', False))")
[ "$SCOPE_PASSED" = "True" ] || { echo "HALT: scope_validation_passed is false in algorithm_summary.json"; exit 1; }

# signal_coverage.json: exists + schema valid + coverage complete
test -f workspace/signal_coverage.json || { echo "HALT: missing signal_coverage.json"; exit 1; }
.venv/bin/python tools/transfer_cli.py validate-schema workspace/signal_coverage.json || { echo "HALT: signal_coverage.json schema validation failed"; exit 1; }
COVERAGE_COMPLETE=$(.venv/bin/python -c "import json; print(json.load(open('workspace/signal_coverage.json')).get('coverage_complete', False))")
[ "$COVERAGE_COMPLETE" = "True" ] || { echo "HALT: coverage_complete is false in signal_coverage.json"; exit 1; }

# Read EVOLVE-BLOCK source path from algorithm_summary.json and verify it exists
EVOLVE_SOURCE=$(.venv/bin/python -c "import json; print(json.load(open('workspace/algorithm_summary.json'))['evolve_block_source'].rsplit(':',1)[0])")
test -f "$EVOLVE_SOURCE" || { echo "HALT: EVOLVE-BLOCK source file missing: $EVOLVE_SOURCE"; exit 1; }
```

On HALT, write `workspace/escalation.json` with `"stage": 3` (Stage 3.5 uses stage=3 with
`_stage3_5` suffix on halt_reason to preserve the integer schema):
- Missing stage3_output.json → `"missing_stage3_output_stage3_5"`
- Schema validation failed → `"stage3_schema_validation_failed_stage3_5"`
- Scorer file missing → `"scorer_file_missing_stage3_5"`
- Missing algorithm_summary.json → `"missing_algorithm_summary_stage3_5"`
- Missing signal_coverage.json → `"missing_signal_coverage_stage3_5"`

## Stale Artifact Guard

Remove any prior Stage 3.5 artifacts before starting.

```bash
rm -f workspace/translation_validation.json

# Only remove escalation.json if it was written by Stage 3.5
.venv/bin/python -c "
import json, os
esc = 'workspace/escalation.json'
if os.path.isfile(esc):
    try:
        d = json.load(open(esc))
        if d.get('stage') == 3 and '_stage3_5' in d.get('halt_reason', ''):
            os.remove(esc)
            print('Removed stale Stage 3.5 escalation artifact')
    except (json.JSONDecodeError, KeyError):
        pass
"
```

## Retry State

Initialize these counters at the start of Stage 3.5. Track them across all fix attempts.

| Counter | Initial | Halt threshold | Action on limit |
|---------|---------|----------------|-----------------|
| `retries_translation` | 0 | >= 3 (2 retries done) | HALT: `translation_fix_limit_exceeded_stage3_5` |
| `retries_total` | 0 | >= 4 (3 retries done) | HALT: `translation_total_retry_limit_exceeded_stage3_5` |
| `last_deviation_signature` | null | same as current → immediate halt | HALT: `identical_consecutive_deviations_stage3_5` |

**Deviation signature** = `(check_id, severity, first_8_chars_of_sha256(description))`.
If two consecutive fix attempts produce the same deviation signature, halt immediately.

## Step 1: EVOLVE-BLOCK Content Hash Verification

Re-verify the EVOLVE-BLOCK content hash independently of Stage 3 to guard against source
drift between stages.

```bash
EVOLVE_BLOCK_SOURCE_FIELD=$(.venv/bin/python -c "import json; print(json.load(open('workspace/algorithm_summary.json'))['evolve_block_source'])")
EXPECTED_HASH=$(.venv/bin/python -c "import json; print(json.load(open('workspace/algorithm_summary.json'))['evolve_block_content_hash'])")

# Extract file path and line range
EVOLVE_FILE=$(echo "$EVOLVE_BLOCK_SOURCE_FIELD" | cut -d: -f1)
LINE_RANGE=$(echo "$EVOLVE_BLOCK_SOURCE_FIELD" | cut -d: -f2)
START_LINE=$(echo "$LINE_RANGE" | cut -d- -f1)
END_LINE=$(echo "$LINE_RANGE" | cut -d- -f2)

# Compute SHA-256 of the EVOLVE-BLOCK region (inclusive of marker lines)
ACTUAL_HASH=$(.venv/bin/python -c "
import hashlib, sys
with open('$EVOLVE_FILE') as f:
    lines = f.readlines()
block = lines[$((START_LINE - 1)):$END_LINE]
content = ''.join(block)
print(hashlib.sha256(content.encode()).hexdigest())
")

[ "$ACTUAL_HASH" = "$EXPECTED_HASH" ] || {
  echo "HALT: EVOLVE-BLOCK hash mismatch. Source changed since Stage 1 extraction."
  echo "Expected: $EXPECTED_HASH"
  echo "Actual:   $ACTUAL_HASH"
  exit 1
}
echo "EVOLVE-BLOCK hash verified: $ACTUAL_HASH"
```

On hash mismatch, **HALT** with `halt_reason: "evolve_block_hash_mismatch_stage3_5"`.

## Step 2: Mechanical Pre-Checks

Run the `validate-translation` CLI subcommand. This performs three mechanical checks:
- **Signal presence**: every sim signal's production metric name appears in the scorer source
- **Constant audit**: evolved numeric constants from the EVOLVE-BLOCK appear in the scorer
- **Normalization check**: signals with `divide_prod_by_100` have a `/100` in the scorer

```bash
.venv/bin/python tools/transfer_cli.py validate-translation \
  --algorithm workspace/algorithm_summary.json \
  --signal-coverage workspace/signal_coverage.json \
  --scorer-file "$SCORER_FILE" \
  | tee /tmp/stage3_5_mechanical_checks.json
MECHANICAL_EXIT=${PIPESTATUS[0]:-${pipestatus[1]}}
```

- If `MECHANICAL_EXIT == 2`: **HALT immediately** with `halt_reason: "infrastructure_error_stage3_5"`.
- If `MECHANICAL_EXIT == 1`: mechanical failures found — record them as deviations and proceed to
  Step 5 (Fix Attempts) after Step 3.
- If `MECHANICAL_EXIT == 0`: no mechanical failures — proceed to Step 3.

## Step 3: LLM Logic Trace

Read both source files in full:

```bash
cat "$SCORER_FILE"                    # Generated production scorer
sed -n '177,262p' "$EVOLVE_SOURCE"    # EVOLVE-BLOCK lines (START through END inclusive)
```

Systematically trace each EVOLVE-BLOCK logic step through the generated scorer. For each
check, record the result in the `logic_trace_checks` array of the output artifact.

### Check 3a — Multi-dimensional scorer evaluation
**EVOLVE-BLOCK lines 178-181**

```go
allDimScores := make([]map[string]float64, len(ws.scorers))
for i, scorer := range ws.scorers {
    allDimScores[i] = scorer(req, snapshots)
}
```

In the production scorer, the simulation's `ws.scorers` loop is replaced by inline computation
of each dimension within `Score()`. The scorer receives a slice of endpoints and must compute:
- dim[0]: prefix-affinity proxy (based on KV utilization availability)
- dim[1]: load-balance (min-max normalization of effective load)

**Verify**: both dimensions are computed per-endpoint in `Score()`.
Failure is **important** if a dimension is missing entirely, **minor** if it is an equivalent
reformulation.

### Check 3b — Fresh load signal (minInflight)
**EVOLVE-BLOCK lines 183-189**

```go
minInflight := snapshots[0].InFlightRequests
for _, snap := range snapshots {
    if snap.InFlightRequests < minInflight {
        minInflight = snap.InFlightRequests
    }
}
```

Production mapping: `InFlightRequests` → `endpoint.GetMetrics().RunningRequestsSize` (int).

**Verify**: the scorer iterates over all endpoints to find the minimum `RunningRequestsSize`.
Failure (missing or wrong) is **critical** because minInflight is used in the adaptive decay.

### Check 3c — Best prefix-affinity instance detection
**EVOLVE-BLOCK lines 191-201**

```go
bestPrefixID := ""
bestPrefixScore := -1.0
if len(allDimScores) > 0 {
    for _, snap := range snapshots {
        if ps := allDimScores[0][snap.ID]; ps > bestPrefixScore {
            bestPrefixScore = ps
            bestPrefixID = snap.ID
        }
    }
}
```

**Verify**: the scorer finds the endpoint with the highest dim[0] (prefix-affinity) score and
tracks its index or reference for use in adaptive weighting.
Failure is **critical** because bestPrefixIdx is used in the adaptive decay calculation.

### Check 3d — Adaptive weight decay
**EVOLVE-BLOCK lines 203-223**

```go
aw := make([]float64, len(ws.weights))
copy(aw, ws.weights)
if bestPrefixID != "" && bestPrefixScore > 0.1 && len(ws.weights) >= 2 {
    cachedLoad := 0
    for _, snap := range snapshots {
        if snap.ID == bestPrefixID { cachedLoad = snap.InFlightRequests; break }
    }
    if delta := cachedLoad - minInflight; delta > 0 {
        decay := 1.0 / (1.0 + 0.6*float64(delta))
        aw[0] = ws.weights[0] * decay
        aw[1] = 1.0 - aw[0]
    }
}
```

**Verify** each element:
- Decay formula: `decay = 1.0 / (1.0 + 0.6*float64(delta))` — constant `0.6` required
- `aw[0] = initial_prefix_weight * decay`
- `aw[1] = 1.0 - aw[0]` (valid for exactly 2 dimensions)
- Conditions before decay: `bestPrefixScore > 0.1` AND `delta > 0`
- `delta = cachedLoad - minInflight` where `cachedLoad` is the best-prefix endpoint's inFlight

Missing decay → **critical** (ranking changes). Wrong constant → **important** (magnitude changes).
Wrong condition → **important** (decay applied in wrong cases).

### Check 3e — Composite score with clamping
**EVOLVE-BLOCK lines 225-234**

```go
for i := range ws.scorers {
    for _, snap := range snapshots {
        s := allDimScores[i][snap.ID]
        if s < 0 { s = 0 }
        if s > 1 { s = 1 }
        scores[snap.ID] += s * aw[i]
    }
}
```

**Verify**:
- Each dimension score is clamped to [0, 1] before weighting
- Weighted sum: `score += clamp(dim_score) * adapted_weight`

Missing clamp → **minor** (edge case only). Missing weighted sum → **critical**.

### Check 3f — KV pressure penalty
**EVOLVE-BLOCK lines 237-240**

```go
if snap.KVUtilization > 0.9 {
    scores[snap.ID] -= 0.5 * (snap.KVUtilization - 0.9) / 0.1
}
```

In production, `KVUtilization` is `KVCacheUsagePercent / 100.0` (0.0–1.0 normalized).
`signal_coverage.json` normalization `divide_prod_by_100` must be applied before the threshold.

**Verify**:
- Threshold comparison is on the normalized value (0.0–1.0): `kvUtil > 0.9` where `kvUtil = KVCacheUsagePercent / 100.0`
- Penalty formula: `score -= 0.5 * (kvUtil - 0.9) / 0.1`
- Constants: `0.5`, `0.9`, `0.1`

If threshold is applied to raw `KVCacheUsagePercent` (0–100) without normalization → **critical** (penalty triggers at wrong point, i.e., > 90 instead of > 90%). Wrong constant → **important**.

### Check 3g — InFlightRequests tiebreaker
**EVOLVE-BLOCK lines 241-243**

```go
scores[snap.ID] += 0.01 / (1.0 + float64(snap.InFlightRequests))
```

Production mapping: `InFlightRequests` → `RunningRequestsSize`.

**Verify**:
- Tiebreaker term: `score += 0.01 / (1.0 + float64(RunningRequestsSize))`
- Constant: `0.01`

Missing tiebreaker → **minor** (only affects tie-breaking behavior). Wrong constant → **minor**.

### Check 3h — Argmax with random tie-breaking
**EVOLVE-BLOCK lines 245-262**

```go
// argmax over scores[snap.ID] with random tie-breaking
```

**INTENTIONAL GAP**: The production scorer returns a `map[Endpoint]float64` of per-endpoint
scores. The framework performs argmax and endpoint selection. The `Score()` method must NOT
implement argmax itself — doing so would violate the `scheduling.Scorer` contract.
Random tie-breaking is not translated.

Record as `"status": "intentional_gap"`, `"severity": "intentional"`.

## Step 4: Known-Gap Documentation

Collect all checks with `status: "intentional_gap"` into the `known_gaps` array of the output
artifact. The current expected intentional gaps for this algorithm are:

1. **argmax_delegation**: Argmax with random tie-breaking (EVOLVE-BLOCK lines 245-262) is not
   translated. The framework calls `Score()` and performs argmax. This is required by the
   `scheduling.Scorer` contract.

2. **multi_scorer_loop**: The simulation's `for i, scorer := range ws.scorers` loop is not a
   separate object iteration in production — all dimension scoring is inlined within `Score()`.

Any additional intentional gaps discovered during Step 3 should be documented here.

## Step 5: Fix Attempts

If any deviations with severity `critical` or `important` were found in Steps 2 or 3:

1. **Choose one deviation to fix** — prefer the highest-severity deviation.
2. **Identify the specific lines** in `$SCORER_FILE` that need correction.
3. **Describe expected vs actual** — quote the EVOLVE-BLOCK reference and the current
   implementation.
4. **Apply the fix** to `$SCORER_FILE` **only**. Do not modify test files or register.go.
5. **Compute the deviation signature** for the fix attempt. If it matches
   `last_deviation_signature`, **HALT** with `halt_reason: "identical_consecutive_deviations_stage3_5"`.
6. **Increment** `retries_translation` and `retries_total`. Check halt limits:
   - `retries_translation >= 3` → HALT: `translation_fix_limit_exceeded_stage3_5`
   - `retries_total >= 4` → HALT: `translation_total_retry_limit_exceeded_stage3_5`
7. **Re-run Step 2** (mechanical pre-checks) and **Step 3** (logic trace) with the updated file.
8. If all remaining deviations are `minor` or `intentional` → proceed to Step 6.
9. If `critical` or `important` deviations remain → return to Step 5 (fix the next deviation).

**Fix scope**: only modify the scorer file at `$SCORER_FILE`. Do not introduce new logic not
present in the EVOLVE-BLOCK. Each fix must correspond directly to a specific EVOLVE-BLOCK
element identified in the logic trace check.

On HALT from fix retries, write escalation.json with `halt_reason: "translation_fix_limit_exceeded_stage3_5"` or `"translation_total_retry_limit_exceeded_stage3_5"` as appropriate.
Include in `details`: the list of remaining unfixed deviations and their check_ids.

## Step 6: Write Output Artifact

Assemble `workspace/translation_validation.json` from the results of Steps 1-5:

```json
{
  "evolve_block_hash": "<hash verified in Step 1>",
  "scorer_file": "<value from stage3_output.json scorer_file>",
  "signal_checks": [ /* from Step 2 CLI output */ ],
  "constant_checks": [ /* from Step 2 CLI output */ ],
  "logic_trace_checks": [
    {
      "check_id": "logic_3a_dim_scoring",
      "status": "pass" | "deviation" | "intentional_gap",
      "severity": "<if deviation/gap>",
      "description": "<what was found>",
      "evolve_block_lines": "178-181",
      "scorer_lines": "<line range in scorer, or null>"
    }
    /* ... one entry per check 3a-3h */
  ],
  "known_gaps": [ /* from Step 4 */ ],
  "deviations": [ /* non-intentional deviations, fixed or unfixed */ ],
  "fix_attempts": {
    "retries_translation": <counter value>,
    "retries_total": <counter value>
  },
  "verdict": "pass" | "pass_with_known_gaps" | "fail"
}
```

**Verdict rules**:
- `"pass"` — zero deviations of any severity in Steps 2 and 3.
- `"pass_with_known_gaps"` — zero critical or important deviations remaining; only minor or
  intentional (after all fixes applied).
- `"fail"` — one or more critical or important deviations remain after retries exhausted.

```bash
# Validate the artifact before declaring success
.venv/bin/python tools/transfer_cli.py validate-schema workspace/translation_validation.json \
  || { echo "HALT: translation_validation.json failed schema validation"; exit 1; }
```

If schema validation fails, fix the artifact structure and re-validate before proceeding.

## Step 7: Verdict and Handoff

| Verdict | Condition | Next action |
|---------|-----------|-------------|
| `pass` | Zero deviations | Proceed to Stage 4 |
| `pass_with_known_gaps` | Zero critical/important; only minor/intentional | Proceed to Stage 4 |
| `fail` | Critical or important deviations remain | HALT, write escalation.json |

On `fail`, write `workspace/escalation.json`:
```json
{
  "stage": 3,
  "halt_reason": "critical_translation_deviation_stage3_5",
  "details": "<describe remaining deviations, referencing check_ids and EVOLVE-BLOCK lines>"
}
```

Validate against schema:
```bash
.venv/bin/python tools/transfer_cli.py validate-schema workspace/escalation.json
```

## Halt Conditions Summary

| Condition | halt_reason | Notes |
|-----------|-------------|-------|
| Missing stage3_output.json | `missing_stage3_output_stage3_5` | Prerequisite |
| stage3_output.json schema invalid | `stage3_schema_validation_failed_stage3_5` | Prerequisite |
| Scorer file not on disk | `scorer_file_missing_stage3_5` | Prerequisite |
| Missing algorithm_summary.json | `missing_algorithm_summary_stage3_5` | Prerequisite |
| Missing signal_coverage.json | `missing_signal_coverage_stage3_5` | Prerequisite |
| EVOLVE-BLOCK hash mismatch | `evolve_block_hash_mismatch_stage3_5` | Step 1 |
| CLI infrastructure error | `infrastructure_error_stage3_5` | Step 2 |
| Fix retries exhausted (per-class) | `translation_fix_limit_exceeded_stage3_5` | Step 5 |
| Total retries exhausted | `translation_total_retry_limit_exceeded_stage3_5` | Step 5 |
| Identical consecutive deviations | `identical_consecutive_deviations_stage3_5` | Step 5 |
| Critical deviation unfixable | `critical_translation_deviation_stage3_5` | Step 7 |

All escalation artifacts use `"stage": 3` (integer schema). The `_stage3_5` suffix in
`halt_reason` disambiguates Stage 3.5 escalations from Stage 3 escalations.
