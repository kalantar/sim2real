---
stage: 6
name: pr
description: "Stage 6 — Create PRs in llm-d repos, append calibration log entry"
inputs:
  - workspace/validation_results.json
  - workspace/transfer_evidence.md
  - workspace/algorithm_summary.json
outputs:
  - llm-d-inference-scheduler PR URL
  - docs/transfer/calibration_log.md entry
---

# Stage 6: PR Creation

You are running Stage 6 of the sim-to-production transfer pipeline. This stage
creates PRs in the llm-d target repositories and records a calibration log entry.

## Fast-Iteration Check

> **Ordering invariant:** This check MUST run before any prerequisite validation. In fast mode, `validation_results.json` is a partial artifact that will fail schema validation.

```bash
FAST_ITER=$(.venv/bin/python -c "
import sys, yaml
try:
    d = yaml.safe_load(open('config/env_defaults.yaml'))
except Exception as e:
    print(f'ERROR: cannot read config/env_defaults.yaml: {e}', file=sys.stderr)
    sys.exit(2)
val = d.get('pipeline', {}).get('fast_iteration', True)
if not isinstance(val, bool):
    print(f'ERROR: pipeline.fast_iteration must be a boolean, got {type(val).__name__}: {val!r}', file=sys.stderr)
    sys.exit(2)
print('true' if val else 'false')
") || { echo "HALT: failed to read pipeline.fast_iteration from config/env_defaults.yaml"; exit 2; }

if [ "$FAST_ITER" = "true" ]; then
  echo "FAST MODE: PR creation skipped (pipeline.fast_iteration=true)."
  echo "Set pipeline.fast_iteration=false and re-run Stage 6 when ready to create PRs."
  exit 0
fi
```

**If `FAST_ITER` is `"true"`:** exit 0 immediately. Do not run prerequisites or any subsequent steps.

**If `FAST_ITER` is `"false"`:** proceed to Prerequisites below.

## Prerequisites

Verify all predecessor artifacts exist and are valid:

```bash
.venv/bin/python tools/transfer_cli.py validate-schema workspace/validation_results.json \
  || { echo "HALT: validation_results.json missing or invalid"; exit 1; }
.venv/bin/python tools/transfer_cli.py validate-schema workspace/algorithm_summary.json \
  || { echo "HALT: algorithm_summary.json missing or invalid"; exit 1; }
test -s workspace/transfer_evidence.md \
  || { echo "HALT: workspace/transfer_evidence.md missing or empty — run generate-evidence first"; exit 1; }
```

**HALT if any command exits non-zero.**

## Step 1: Check Overall Verdict

```bash
VERDICT=$(.venv/bin/python -c "import json; print(json.load(open('workspace/validation_results.json'))['overall_verdict'])")
echo "overall_verdict: $VERDICT"

if [ "$VERDICT" = "FAIL" ]; then
  echo "HALT: overall_verdict is FAIL — do not create PRs. Document failure and stop."
  exit 1
fi

if [ "$VERDICT" = "INCONCLUSIVE" ]; then
  OPERATOR_NOTES=$(.venv/bin/python -c "
import json
v = json.load(open('workspace/validation_results.json'))
print(v.get('operator_notes', '').strip())
" 2>/dev/null)
  if [ -z "$OPERATOR_NOTES" ]; then
    echo "HALT: overall_verdict is INCONCLUSIVE but operator_notes is absent or empty."
    echo "Set operator_notes in workspace/validation_results.json with rationale before proceeding."
    exit 1
  fi
  echo "WARN: Proceeding with INCONCLUSIVE verdict under operator sign-off: $OPERATOR_NOTES"
elif [ "$VERDICT" != "PASS" ]; then
  echo "HALT: unexpected overall_verdict '$VERDICT' — expected PASS, FAIL, or INCONCLUSIVE."
  exit 1
fi
```

**HALT if FAIL. HALT if INCONCLUSIVE without operator_notes. HALT if verdict is not a known value.**

## Step 2: Check gh Auth

```bash
gh auth status \
  || { echo "HALT: gh auth check failed — run 'gh auth login' and retry Stage 6."; exit 1; }
```

**HALT if gh auth status exits non-zero.**

## Step 3: Push Branch to llm-d-inference-scheduler

```bash
ALG_NAME=$(.venv/bin/python -c "import json; print(json.load(open('workspace/algorithm_summary.json'))['algorithm_name'])")
BRANCH="transfer/${ALG_NAME}"

# Check if branch already exists on remote
cd llm-d-inference-scheduler
REMOTE_URL=$(git remote get-url origin 2>/dev/null || git remote get-url upstream 2>/dev/null || echo "")
if git ls-remote --exit-code --heads origin "$BRANCH" 2>/dev/null; then
  TIMESTAMP=$(date +%Y%m%d-%H%M%S)
  BRANCH="${BRANCH}-${TIMESTAMP}"
  echo "WARN: branch already exists — using timestamped branch: $BRANCH"
fi

git checkout -b "$BRANCH" 2>/dev/null || git checkout "$BRANCH"
git push origin "$BRANCH" \
  || { echo "HALT: git push failed for branch $BRANCH in llm-d-inference-scheduler"; cd ..; exit 1; }
echo "Pushed branch: $BRANCH to llm-d-inference-scheduler"
cd ..
```

**HALT if git push fails. Record $BRANCH for PR creation.**

## Step 4: Append Calibration Log Entry

Append the calibration entry **before** creating PRs. If the append fails, no PRs have been created yet — safe to halt and investigate.

```bash
.venv/bin/python tools/transfer_cli.py append-calibration-log \
  --workspace workspace/ \
  --calibration-log docs/transfer/calibration_log.md \
  || { echo "HALT: append-calibration-log failed — inspect docs/transfer/calibration_log.md before proceeding"; exit 1; }
```

**HALT if exit non-zero. Fix calibration log before continuing to PR creation.**

## Step 5: Create PR in llm-d-inference-scheduler

```bash
SUITE_A_TAU=$(.venv/bin/python -c "import json; print(json.load(open('workspace/validation_results.json'))['suite_a']['kendall_tau'])")
SUITE_C_PASS=$(.venv/bin/python -c "import json; print(str(json.load(open('workspace/validation_results.json'))['suite_c']['passed']).lower())")
MECH=$(.venv/bin/python -c "import json; print(json.load(open('workspace/validation_results.json'))['benchmark']['mechanism_check_verdict'])")

# Write body to a temp file to avoid shell quoting issues with Markdown content
# (transfer_evidence.md may contain double quotes, backslashes, or $ signs).
PR_BODY_FILE=$(mktemp)
cat << 'PRBODYEOF' > "$PR_BODY_FILE"
## Summary

Sim-to-production transfer: `ALG_NAME_PLACEHOLDER`

**Validation:**
- Suite A Kendall-tau: `SUITE_A_TAU_PLACEHOLDER` (threshold: 0.8)
- Suite C concurrent safety: `SUITE_C_PASS_PLACEHOLDER`
- Mechanism check: `MECH_PLACEHOLDER`
- Overall verdict: `VERDICT_PLACEHOLDER`

## Evidence

EVIDENCE_PLACEHOLDER

## Rollback

To disable: in EndpointPickerConfig, find the `plugins` entry with `type: blis-weighted-scorer` (or by the explicit `name:` you used when adding it in Stage 4) and set `parameters.enabled: false`.
PRBODYEOF
# Substitute placeholders (safe: sed operates on file content, not shell)
sed -i "s|ALG_NAME_PLACEHOLDER|${ALG_NAME}|g" "$PR_BODY_FILE"
sed -i "s|SUITE_A_TAU_PLACEHOLDER|${SUITE_A_TAU}|g" "$PR_BODY_FILE"
sed -i "s|SUITE_C_PASS_PLACEHOLDER|${SUITE_C_PASS}|g" "$PR_BODY_FILE"
sed -i "s|MECH_PLACEHOLDER|${MECH}|g" "$PR_BODY_FILE"
sed -i "s|VERDICT_PLACEHOLDER|${VERDICT}|g" "$PR_BODY_FILE"
# Replace evidence placeholder with actual content (evidence may contain sed special chars,
# so use python for the substitution)
.venv/bin/python - "$PR_BODY_FILE" <<'PYEOF'
import sys, pathlib
body_file = pathlib.Path(sys.argv[1])
evidence = pathlib.Path("workspace/transfer_evidence.md").read_text()
body_file.write_text(body_file.read_text().replace("EVIDENCE_PLACEHOLDER", evidence))
PYEOF

cd llm-d-inference-scheduler
gh pr create \
  --title "feat(scorer): add ${ALG_NAME} sim-to-production scorer plugin" \
  --base main \
  --head "$BRANCH" \
  --body-file "$PR_BODY_FILE" \
  || { PUSH_BRANCH="$BRANCH"; rm -f "$PR_BODY_FILE"; echo "HALT: gh pr create failed for llm-d-inference-scheduler. Branch '$PUSH_BRANCH' is already pushed — create PR manually or retry 'gh pr create'."; cd ..; exit 1; }
rm -f "$PR_BODY_FILE"

SCHEDULER_PR_URL=$(gh pr view --json url -q .url)
echo "Created PR: $SCHEDULER_PR_URL"
cd ..
```

**HALT if gh pr create fails — report the pushed branch name for manual recovery.**

## Step 6: llm-d-benchmark PR (conditional)

If this transfer involved benchmark config changes in the llm-d-benchmark submodule, push a branch and create a PR there too. **If no benchmark config changes exist, skip this step.**

```bash
# Check for uncommitted changes in llm-d-benchmark
if ! git -C llm-d-benchmark diff --quiet HEAD; then
  BENCH_BRANCH="transfer/${ALG_NAME}"
  if git -C llm-d-benchmark ls-remote --exit-code --heads origin "$BENCH_BRANCH" 2>/dev/null; then
    TIMESTAMP=$(date +%Y%m%d-%H%M%S)
    BENCH_BRANCH="${BENCH_BRANCH}-${TIMESTAMP}"
  fi
  git -C llm-d-benchmark checkout -b "$BENCH_BRANCH"
  git -C llm-d-benchmark push origin "$BENCH_BRANCH" \
    || { echo "HALT: git push failed for llm-d-benchmark branch $BENCH_BRANCH"; exit 1; }
  cd llm-d-benchmark
  gh pr create \
    --title "feat(benchmark): add ${ALG_NAME} benchmark configs" \
    --base main \
    --head "$BENCH_BRANCH" \
    --body "Benchmark configs for sim-to-production transfer: \`${ALG_NAME}\`. See llm-d-inference-scheduler PR: ${SCHEDULER_PR_URL}" \
    || { echo "HALT: gh pr create failed for llm-d-benchmark. Branch '$BENCH_BRANCH' is pushed."; cd ..; exit 1; }
  BENCHMARK_PR_URL=$(gh pr view --json url -q .url)
  echo "Created benchmark PR: $BENCHMARK_PR_URL"
  cd ..
else
  echo "No benchmark config changes — skipping llm-d-benchmark PR."
  BENCHMARK_PR_URL="(none)"
fi
```

## Halt Conditions Summary

| Condition | Trigger | Action |
|-----------|---------|--------|
| Prerequisite artifact missing/invalid | validate-schema exits non-zero | HALT: "Stage N prerequisite missing" |
| overall_verdict == FAIL | Step 1 check | HALT: "Do not create PRs" |
| INCONCLUSIVE without operator_notes | Step 1 check | HALT: "Add operator_notes to validation_results.json" |
| gh auth not configured | Step 2 | HALT: "Run gh auth login" |
| git push fails | Step 3 or 6 | HALT: report branch name |
| append-calibration-log fails | Step 4 | HALT: "Inspect calibration_log.md before continuing" |
| gh pr create fails | Step 5 or 6 | HALT: report pushed branch for manual recovery |

## Expected Outputs

- `docs/transfer/calibration_log.md` entry: appended by Step 4
- `llm-d-inference-scheduler` PR URL: printed by Step 5
- `llm-d-benchmark` PR URL (or "none"): printed by Step 6

> **Note:** Stage 6 ends the interactive pipeline session. The generated scorer is now under review by llm-d maintainers. If they request changes, re-run Stages 3→4 to address feedback and push an updated branch.
