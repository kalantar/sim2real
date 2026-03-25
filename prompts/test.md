---
stage: 4
version: "1.0"
pipeline_commit: "set-at-runtime"
description: "Stage 4 — Build and test generated scorer plugin with retry logic"
---

# Stage 4: Test

Build and test the generated scorer plugin in the llm-d-inference-scheduler
submodule. This stage runs `go build`, `go vet`, and `go test` with structured
retry logic: errors are classified, retries are tracked per class, and the stage
halts when retry limits are exceeded or loop detection triggers.

## Prerequisites

Verify Stage 3 output exists and is valid. **HALT if any check fails.**

```bash
# Stage 3 output artifact: exists + schema valid
test -f workspace/stage3_output.json || { echo "HALT: missing stage3_output.json"; exit 1; }
.venv/bin/python tools/transfer_cli.py validate-schema workspace/stage3_output.json || { echo "HALT: stage3_output.json schema validation failed"; exit 1; }

# Read file paths from stage3_output.json
SCORER_FILE=$(.venv/bin/python -c "import json; print(json.load(open('workspace/stage3_output.json'))['scorer_file'])")
TEST_FILE=$(.venv/bin/python -c "import json; print(json.load(open('workspace/stage3_output.json'))['test_file'])")
REGISTER_FILE=$(.venv/bin/python -c "import json; print(json.load(open('workspace/stage3_output.json'))['register_file'])")

# Verify generated files exist on disk
test -f "$SCORER_FILE" || { echo "HALT: scorer file missing: $SCORER_FILE"; exit 1; }
test -f "$TEST_FILE" || { echo "HALT: test file missing: $TEST_FILE"; exit 1; }
test -f "$REGISTER_FILE" || { echo "HALT: register file missing: $REGISTER_FILE"; exit 1; }
```

On HALT, write `workspace/escalation.json`:
- Missing stage3_output.json → `"missing_stage3_output"`
- Schema validation failed → `"stage3_schema_validation_failed"`
- Scorer file missing → `"scorer_file_missing"`
- Test file missing → `"test_file_missing"`
- Register file missing → `"register_file_missing"`

**Stage 3.5 gate (translation validation):** If `workspace/translation_validation.json` exists,
check its `verdict` field before proceeding. If `verdict == "fail"`, **HALT immediately** with
`halt_reason: "critical_translation_deviation_stage3_5"` and include in details that Stage 3.5
found unfixed translation deviations — re-run Stage 3.5 or Stage 3 before retrying Stage 4.
If `translation_validation.json` does not exist, proceed normally (Stage 3.5 is recommended
but not blocking when absent).

```bash
if [ -f workspace/translation_validation.json ]; then
    .venv/bin/python tools/transfer_cli.py validate-schema workspace/translation_validation.json \
      || echo "WARNING: translation_validation.json failed schema check"
    VERDICT=$(.venv/bin/python -c "import json; print(json.load(open('workspace/translation_validation.json')).get('verdict',''))")
    [ "$VERDICT" != "fail" ] || {
      echo "HALT: Stage 3.5 translation validation failed (verdict=fail). Re-run Stage 3.5 before Stage 4."
      exit 1
    }
fi
```

**Important:** Steps 1 and 2 run `go build ./...` and `go vet ./...` on the entire
submodule. If a pre-existing build or vet failure exists in an unrelated package,
Stage 4 will enter the retry loop even though the generated code is correct. Before
running Stage 4, verify the submodule builds cleanly by running
`cd llm-d-inference-scheduler && go build ./... && go vet ./... && cd ..` and confirming
exit code 0. If it fails, resolve pre-existing issues first. If Stage 4 halts with
`build_compilation_failure` and the errors reference files NOT listed in
`stage3_output.json`, this indicates a pre-existing submodule issue, not a generated
code problem.

## Stale Artifact Guard

Remove any prior escalation artifact from Stage 4 (but preserve Stage 3's if present).

```bash
# Only remove escalation.json if it was written by Stage 4
.venv/bin/python -c "
import json, os
esc = 'workspace/escalation.json'
if os.path.isfile(esc):
    try:
        d = json.load(open(esc))
        if d.get('stage') == 4:
            os.remove(esc)
            print('Removed stale Stage 4 escalation artifact')
    except (json.JSONDecodeError, KeyError):
        pass
"
```

## Retry State

Initialize these counters at the start of Stage 4. Track them across all retry
attempts.

| Counter | Initial | Halt threshold | Action on limit |
|---------|---------|----------------|-----------------|
| `retries_compilation` | 0 | >= 4 (3 retries done) | HALT: `build_compilation_failure` |
| `retries_test_failure` | 0 | >= 4 (3 retries done) | HALT: `test_failure_limit_exceeded` |
| `retries_total` | 0 | >= 6 (5 retries done) | HALT: `total_retry_limit_exceeded` |
| `error_signatures` | [] | 3 occurrences of same signature | HALT: `oscillating_errors` |
| `last_error_signature` | null | same as current → immediate halt | HALT: `identical_consecutive_errors` |

**Error signature** = `(error_class, first_error_message)` where `first_error_message`
is the `message` field of the first entry in `test-status` output `errors[]`.
If `errors[]` is empty (unrecognized output format), use the first non-empty line of
the raw Go output as `first_error_message`. If raw output is also empty, use the
sentinel string `"<unclassified>"`.

## Step 1: Run Go Build

**CRITICAL: Use the EXACT command below. Do NOT substitute package paths.**

```bash
cd llm-d-inference-scheduler
set -o pipefail
go build ./... 2>&1 | tee /tmp/stage4_build_output.txt
# Portable: bash uses PIPESTATUS (0-indexed), zsh uses pipestatus (1-indexed)
BUILD_EXIT=${PIPESTATUS[0]:-${pipestatus[1]}}
cd ..
```

**Why `./...` and not a specific package:** This command MUST build the entire
submodule to catch registration errors in `pkg/plugins/register.go`. Building only
`pkg/plugins/scorer` will NOT detect these errors.

If `BUILD_EXIT == 0`, proceed to Step 2.

If `BUILD_EXIT != 0`, classify the error:

```bash
cat /tmp/stage4_build_output.txt | .venv/bin/python tools/transfer_cli.py test-status | tee /tmp/stage4_build_status.json
TEST_STATUS_EXIT=${PIPESTATUS[1]:-${pipestatus[2]}}
```

If `TEST_STATUS_EXIT == 2`: **HALT immediately** (CLI infrastructure error — stdin too large, invalid UTF-8, or read failure). Write escalation.json with `halt_reason: "infrastructure_error_stage4"`.

Read the JSON output from `/tmp/stage4_build_status.json`:
- If `error_class == "infrastructure"`: **HALT immediately** (do not retry). Write escalation.json with `halt_reason: "infrastructure_error_stage4"`.
- If `error_class == "compilation"`: proceed to **Step 4: Retry**.
- Otherwise (including `error_class == "none"`): classify as compilation error and proceed to **Step 4: Retry**.

## Step 2: Run Go Vet

**CRITICAL: Use the EXACT command below. Do NOT substitute package paths.**

```bash
cd llm-d-inference-scheduler
set -o pipefail
go vet ./... 2>&1 | tee /tmp/stage4_vet_output.txt
# Portable: bash uses PIPESTATUS (0-indexed), zsh uses pipestatus (1-indexed)
VET_EXIT=${PIPESTATUS[0]:-${pipestatus[1]}}
cd ..
```

**Why `./...` and not a specific package:** This command MUST vet the entire
submodule to catch issues in `pkg/plugins/register.go`. Vetting only
`pkg/plugins/scorer` will NOT detect these errors.

If `VET_EXIT == 0`, proceed to Step 3.

If `VET_EXIT != 0`, classify the error:

```bash
cat /tmp/stage4_vet_output.txt | .venv/bin/python tools/transfer_cli.py test-status | tee /tmp/stage4_vet_status.json
TEST_STATUS_EXIT=${PIPESTATUS[1]:-${pipestatus[2]}}
```

If `TEST_STATUS_EXIT == 2`: **HALT immediately** (CLI infrastructure error). Write escalation.json with `halt_reason: "infrastructure_error_stage4"`.

Read the JSON output from `/tmp/stage4_vet_status.json`:
- If `error_class == "infrastructure"`: **HALT immediately**. Write escalation.json with `halt_reason: "infrastructure_error_stage4"`.
- Otherwise: classify as compilation error and proceed to **Step 4: Retry**.

## Step 3: Run Go Test

**CRITICAL: Use the EXACT command below. Do NOT modify the package path or timeout.**

Run only the scorer package tests (not the entire repo — to avoid unrelated failures):

```bash
cd llm-d-inference-scheduler
set -o pipefail
go test -timeout 10m ./pkg/plugins/scorer/... -v 2>&1 | tee /tmp/stage4_test_output.txt
# Portable: bash uses PIPESTATUS (0-indexed), zsh uses pipestatus (1-indexed)
TEST_EXIT=${PIPESTATUS[0]:-${pipestatus[1]}}
cd ..
```

If `TEST_EXIT == 0`: **Stage 4 PASSES.** Proceed to Step 5 (Completion).

If `TEST_EXIT != 0`, classify the error:

```bash
cat /tmp/stage4_test_output.txt | .venv/bin/python tools/transfer_cli.py test-status | tee /tmp/stage4_test_status.json
TEST_STATUS_EXIT=${PIPESTATUS[1]:-${pipestatus[2]}}
```

If `TEST_STATUS_EXIT == 2`: **HALT immediately** (CLI infrastructure error). Write escalation.json with `halt_reason: "infrastructure_error_stage4"`.

Read the JSON output from `/tmp/stage4_test_status.json`:
- If `error_class == "infrastructure"`: **HALT immediately**. Write escalation.json with `halt_reason: "infrastructure_error_stage4"`.
- If `error_class == "test_failure"` or `error_class == "compilation"`: proceed to **Step 4: Retry**.
- Otherwise (including `error_class == "none"`): classify as test failure and proceed to **Step 4: Retry**.

## Step 4: Retry

Before retrying, perform these checks IN ORDER:

### 4a: Check infrastructure class

If the error class is `infrastructure`, **HALT immediately**. Infrastructure errors
are never retried — they indicate environment problems (module resolution, timeouts),
not generated code issues.

### 4b: Check identical consecutive errors

Compute the error signature: `(error_class, first_error_message)` from the saved
`/tmp/stage4_*_status.json` file. If `errors[]` is empty, use the first non-empty line
of the raw Go output file (`/tmp/stage4_*_output.txt`) as `first_error_message`, or
`"<unclassified>"` if raw output is also empty.

If this signature is identical to `last_error_signature`, **HALT immediately** with
`halt_reason: "identical_consecutive_errors"`. The LLM is making the same mistake.

### 4c: Check non-consecutive duplicate (oscillation detection)

Add the current error signature to `error_signatures[]`.

Count how many times this exact signature appears in the list. If the count reaches 3,
**HALT** with `halt_reason: "oscillating_errors"`. The LLM is oscillating between
error states.

### 4d: Check per-class retry limit

Increment the appropriate class counter based on the **effective** error class
(i.e., after applying the fallback classification from Steps 1/2/3):
- Compilation error (or `error_class == "none"` from Step 1 or Step 2): `retries_compilation += 1`
- Test failure (or `error_class == "none"` from Step 3): `retries_test_failure += 1`

If the counter is now **4 or more** (i.e., 3 retries already attempted), **HALT**:
- `retries_compilation >= 4` → `build_compilation_failure`
- `retries_test_failure >= 4` → `test_failure_limit_exceeded`

(The limit is 3 retries per class. The counter starts at 0 and is incremented before
this check, so the 4th increment means 3 retries have already been attempted.)

### 4e: Check total retry limit

Increment `retries_total += 1`.

If `retries_total` is now **6 or more** (i.e., 5 retries already attempted), **HALT**
with `halt_reason: "total_retry_limit_exceeded"`.

### 4f: Apply fix and retry

If all checks pass:

1. Update `last_error_signature` with the current signature.
2. Re-read `workspace/stage3_output.json` to get the current file paths
   (`scorer_file`, `test_file`, `register_file`). Do not rely on shell variables
   set earlier — they are not preserved across tool calls in an LLM agent session.
3. Read the full error output from `/tmp/stage4_build_output.txt`,
   `/tmp/stage4_vet_output.txt`, or `/tmp/stage4_test_output.txt`
   (whichever corresponds to the failing step).
4. Identify which file(s) need changes from the error output (file paths are
   included in compilation errors; test names map to test files).
5. Apply the fix to the generated code. **Only modify files listed in
   `workspace/stage3_output.json`** (`scorer_file`, `test_file`, `register_file`).
   Do NOT modify other submodule files.
6. Return to **Step 1** (full rebuild — do NOT skip ahead to Step 3 even if
   only test files were changed, since compilation must be re-verified).

## Step 5: Completion

Stage 4 passes when:
- `go build ./...` exits 0
- `go vet ./...` exits 0
- `go test -timeout 10m ./pkg/plugins/scorer/... -v` exits 0

No output artifact is written. Stage 4 success is verified by the orchestrator's
between-stage validation (re-runs `go build ./...`, `go vet ./...`, and
`go test ./pkg/plugins/scorer/... -v` after Stage 4).

## Halt Conditions

| Condition | halt_reason | Retryable? | Action |
|-----------|-------------|------------|--------|
| Missing stage3_output.json | `missing_stage3_output` | No | Write escalation.json, HALT |
| stage3_output.json schema validation failed | `stage3_schema_validation_failed` | No | Write escalation.json, HALT |
| Scorer file not found on disk | `scorer_file_missing` | No | Write escalation.json, HALT |
| Test file not found on disk | `test_file_missing` | No | Write escalation.json, HALT |
| Register file not found on disk | `register_file_missing` | No | Write escalation.json, HALT |
| Infrastructure error (module/timeout) | `infrastructure_error_stage4` | No | Write escalation.json, HALT |
| Compilation retries >= 4 (3 retries done) | `build_compilation_failure` | No | Write escalation.json, HALT |
| Test failure retries >= 4 (3 retries done) | `test_failure_limit_exceeded` | No | Write escalation.json, HALT |
| Total retries >= 6 (5 retries done) | `total_retry_limit_exceeded` | No | Write escalation.json, HALT |
| Identical consecutive errors | `identical_consecutive_errors` | No | Write escalation.json, HALT |
| Same error signature 3 times | `oscillating_errors` | No | Write escalation.json, HALT |

On any halt, write `workspace/escalation.json`:

```json
{
  "stage": 4,
  "halt_reason": "<halt_reason from table above>",
  "details": "<human-readable description including: last error output, retry counts, error class, and recommended next steps>"
}
```

**Recommended next steps by halt reason:**
- `build_compilation_failure`: The LLM could not fix compilation errors in 3 attempts. Manually review the generated code against the scorer template. Common causes: missing imports, incorrect type conversions, API mismatch with submodule HEAD.
- `test_failure_limit_exceeded`: Tests fail consistently. Manually inspect the test expectations against the evolved algorithm logic. Common causes: incorrect normalization, wrong scoring formula translation.
- `infrastructure_error_stage4`: The Go build environment is broken. Check: `go env`, `go mod download` in the submodule, network access to module proxy.
- `identical_consecutive_errors` / `oscillating_errors`: The LLM is stuck in a loop. The fix attempt is not addressing the root cause. Manually review the error and the attempted fix.
- `total_retry_limit_exceeded`: Too many errors of different types. The generated code likely has fundamental issues. Consider re-running Stage 3 (regenerate).

## Expected Outputs

**On success:**
- `go build`, `go vet`, `go test` all pass in llm-d-inference-scheduler
- No workspace artifacts written (success is implicit)

**On halt:**
- `workspace/escalation.json` with Stage 4 halt reason and details
