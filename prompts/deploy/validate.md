---
stage: 5
name: validate
description: Validate the generated scorer plugin against the simulation algorithm. Suite A/B/C results come from Stage 4.5 (equivalence gate); this stage adds noise characterization and cluster benchmarks.
inputs:
  - workspace/algorithm_summary.json
  - workspace/signal_coverage.json
  - workspace/stage3_output.json
  - workspace/equivalence_results.json
  - (Stage 4 success verified via go build/go vet — no workspace artifact)
outputs:
  - workspace/validation_results.json
  - workspace/comparison_table.txt (full mode only)
---

# Stage 5: Validate

You are running Stage 5 of the sim-to-production transfer pipeline. This stage validates
equivalence between the generated scorer plugin and the original simulation algorithm.

## Prerequisites

Before proceeding, verify all predecessor artifacts exist and are valid:

```bash
.venv/bin/python tools/transfer_cli.py validate-schema workspace/signal_coverage.json
.venv/bin/python tools/transfer_cli.py validate-schema workspace/stage3_output.json
```

**HALT if either command exits non-zero.** Message: "HALT: Stage [2|3] prerequisite missing or invalid: workspace/<file>"

Verify Stage 4 completed successfully (scorer builds and tests pass):

```bash
(cd llm-d-inference-scheduler && GOWORK=off go build ./pkg/plugins/scorer/... && GOWORK=off go vet ./pkg/plugins/scorer/...)
```

**HALT if either command exits non-zero.** Message: "HALT: Stage 4 prerequisite failed — generated scorer does not build cleanly. Run Stage 4 first."

> **Note:** Stage 4 does not write a workspace artifact on success (success is implicit in the build/test passing). This prerequisite verifies the scorer builds by re-running `go build` and `go vet` scoped to the scorer package only — using `./...` would build all packages in the module and may fail for unrelated reasons (see Section H cross-system invariants).

Verify Stage 1 extract artifact exists (referenced by cluster benchmark steps):

```bash
test -f workspace/algorithm_summary.json || { echo "HALT: workspace/algorithm_summary.json missing (run extract first)"; exit 1; }
.venv/bin/python tools/transfer_cli.py validate-schema workspace/algorithm_summary.json || { echo "HALT: workspace/algorithm_summary.json schema validation failed"; exit 1; }
```

**HALT if `workspace/algorithm_summary.json` is absent or invalid.** This artifact is required by the cluster benchmark steps and the mechanism check in this stage.

Verify Stage 4.5 equivalence gate passed:

```bash
test -f workspace/equivalence_results.json || { echo "HALT: workspace/equivalence_results.json missing (run Stage 4.5 equivalence gate first)"; exit 1; }
.venv/bin/python tools/transfer_cli.py validate-schema workspace/equivalence_results.json || { echo "HALT: workspace/equivalence_results.json schema validation failed"; exit 1; }
.venv/bin/python -c "
import json, sys
d = json.load(open('workspace/equivalence_results.json'))
if not d.get('suite_a', {}).get('passed'):
    print('HALT: Suite A did not pass in Stage 4.5'); sys.exit(1)
if not d.get('suite_c', {}).get('passed'):
    print('HALT: Suite C did not pass in Stage 4.5'); sys.exit(1)
print('Stage 4.5 equivalence gate verified')
" || exit 1
```

**HALT if equivalence_results.json is absent, invalid, or shows suite failures.**

## Fast-Iteration Check

Read the fast-iteration flag from `config/env_defaults.yaml`:

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
```

**If `FAST_ITER` is `"true"`:**

> All prerequisite checks above have already run and passed. Suites A/B/C and the baseline/treatment cluster pipelines run in fast mode; noise and mechanism check are skipped.

1. Print: `"FAST MODE: Skipping noise gate and mechanism check (pipeline.fast_iteration=true)"`
2. Skip Step 1 (Noise Characterization Gate) entirely.
3. Suite A/B/C results already available from Stage 4.5 (`workspace/equivalence_results.json`).
4. Write `workspace/validation_results.json` per Step 4b (construct from equivalence_results.json), adding an `overall_verdict` field:
   - `"PASS"` if `suite_a.passed` is true AND `suite_c.passed` is true
   - `"FAIL"` otherwise
   - Suite B is informational-only (v1) and does not affect `overall_verdict`.
   - Do not schema-validate this file (it intentionally omits `benchmark`, `noise_cv`).
5. Run Step 5a (initialize benchmark state), then run **baseline then treatment sequentially** using the `for phase in baseline treatment` loop from Step 5b — skip the noise loop entirely. Baseline must complete (Succeeded, extracted, converted, marked done) before treatment is submitted.
6. Run Step 5e (comparison table) once baseline and treatment results are available.
7. Print: `"FAST MODE: Noise gate and mechanism check skipped. Set pipeline.fast_iteration=false to run full validation."`
8. **Exit 0.** Do not proceed to Step 5c, 5c-merge, 5d, or Step 6.

**If `FAST_ITER` is `"false"`:** proceed with Step 1 (Noise Characterization Gate) and the full pipeline as today.

> **Stale artifact note:** Fast mode writes a partial `validation_results.json` (no `benchmark` or `noise_cv`). If you flip `fast_iteration` to `false`, you must re-run Stage 5 from Step 1 (including noise) before proceeding to Stage 6. Proceeding directly to Stage 6 will fail at its prerequisite schema check — this is the expected enforcement gate.

## Step 1: Noise Characterization Gate

Noise characterization runs as the `noise` phase of the cluster pipeline (Step 5a/5b).
This step verifies noise is done before proceeding to Suites A/B/C.

**Routing preamble — run first:**

~~~bash
# Resolve NAMESPACE (operator-set env var or prompt)
# Initialize state and check noise status
BENCH_STATE_OUTPUT=$(.venv/bin/python tools/transfer_cli.py \
  benchmark-state --workspace workspace/ --namespace ${NAMESPACE:?NAMESPACE must be set})
BENCH_STATE_EXIT=$?

if [ $BENCH_STATE_EXIT -eq 2 ]; then
  echo "HALT: benchmark-state failed — missing workspace/algorithm_summary.json. Run Stage 1 extract first."
  exit 1
elif [ $BENCH_STATE_EXIT -ne 0 ]; then
  echo "HALT: benchmark-state failed (exit $BENCH_STATE_EXIT). Check cluster context."
  echo "$BENCH_STATE_OUTPUT"
  exit 1
fi

NOISE_STATUS=$(echo "$BENCH_STATE_OUTPUT" \
  | .venv/bin/python -c "import sys,json; print(json.load(sys.stdin)['phases']['noise']['status'])")

if [ "$NOISE_STATUS" != "done" ]; then
  echo "REENTER: Noise phase is '$NOISE_STATUS' — jump to Step 5 (5a and 5b for noise phase only)."
  echo "After noise phase completes: re-run validate.md from Step 1 (do NOT fall through to Suite A/B/C now)."
  # Signal to automated harnesses: this is a planned re-entry pause, not a success completion.
  # Exit 3 = REENTER (distinct from exit 0 = complete, exit 1 = error, exit 2 = infrastructure error).
  exit 3
fi
# If noise_status == "done": fall through to Step 2 (Suite A) below.
~~~

**If noise is `done`:** proceed to Step 2 (Suite A).
**If noise is not `done`:** script exits 3 (REENTER). Jump to Step 5 now, run the noise phase pipeline,
then re-enter Stage 5 from the top for Pass 2 (Suites A/B/C + baseline/treatment).
Automated harnesses should treat exit 3 as a planned re-entry pause (not an error and not a completion).

T_eff is computed internally by `transfer_cli.py benchmark` from `workspace/noise_results.json`.
The old `baseline_runs.json` format and `noise-characterize` subcommand are superseded.

## Steps 2-4: Suite Results (from Stage 4.5)

Suite A/B/C execution has moved to Stage 4.5 (Equivalence Gate). This stage reads the
results from `workspace/equivalence_results.json` rather than running suites directly.

> **Why suites moved:** Suite A is the primary detector of scoring formula bugs. Running it
> before the image build (Stage 4.75) prevents building images with buggy scorers and enables
> re-validation loops when the scorer is fixed.

## Step 4b: Write partial validation_results.json

Construct `workspace/validation_results.json` from the Stage 4.5 equivalence results:

```bash
.venv/bin/python -c "
import json
eq = json.load(open('workspace/equivalence_results.json'))
val = {
    'suite_a': eq['suite_a'],
    'suite_b': eq['suite_b'],
    'suite_c': eq['suite_c']
}
open('workspace/validation_results.json', 'w').write(json.dumps(val, indent=2))
print('Wrote validation_results.json from equivalence_results.json')
" || { echo "HALT: failed to construct validation_results.json from equivalence_results.json"; exit 1; }
```

**Skip the following block if `FAST_ITER` is `false`** — `overall_verdict` will be set later by Step 5c-merge after the mechanism check.

In fast mode (`FAST_ITER` is `true`), add `overall_verdict`:

```bash
.venv/bin/python -c "
import json
val = json.load(open('workspace/validation_results.json'))
passed = val.get('suite_a', {}).get('passed') and val.get('suite_c', {}).get('passed')
val['overall_verdict'] = 'PASS' if passed else 'FAIL'
open('workspace/validation_results.json', 'w').write(json.dumps(val, indent=2))
print('overall_verdict:', val['overall_verdict'])
" || { echo "HALT: failed to set overall_verdict in validation_results.json"; exit 1; }
```

**Do not call validate-schema yet** — the partial file intentionally omits `benchmark` and `noise_cv`.

## Step 5: Cluster Benchmarks

This step submits Tekton pipelines against the production cluster for noise, baseline, and treatment phases.

> **Hardware reference:** Use `blis_router/CLUSTER.md` for the exact hardware and cluster configuration required to reproduce the simulation environment.

### 5a. Initialize state

~~~bash
.venv/bin/python tools/transfer_cli.py benchmark-state \
  --workspace workspace/ --namespace $NAMESPACE
~~~

**HALT if exit 1** (cluster context mismatch — operator ran this on a different cluster than previous phases). **HALT if exit 2** (missing `workspace/algorithm_summary.json`).

### 5b. Run noise phase (sequential runs with fresh infra), then baseline and treatment

#### Noise phase — sequential runs

Each of the `noise_runs` iterations deploys fresh infrastructure, runs the workload
once, and tears down before the next iteration starts. This ensures each noise sample
starts with a cold KV cache and is statistically independent.

**Prerequisite:** verify Stage 3 Step 8 was rerun after removing glia-40qps:

~~~bash
grep -q 'glia-40qps' workspace/tekton/values.yaml \
  && { echo "HALT: values.yaml still contains glia-40qps — re-run Stage 3 Step 8 first"; exit 1; }
~~~

**Check noise phase status (skip if already done):**

~~~bash
NOISE_STATUS=$(.venv/bin/python -c \
  "import json; print(json.load(open('workspace/benchmark_state.json'))['phases']['noise']['status'])" \
  2>/dev/null || echo "unknown")
if [ "$NOISE_STATUS" = "done" ]; then
  echo "Noise phase already done — skipping noise loop."
else
~~~

**Compile and apply the noise pipeline once (reused for all runs):**

~~~bash
.venv/bin/python tools/transfer_cli.py compile-pipeline \
  --template-dir tektonc-data-collection/tektoncsample/sim2real \
  --values workspace/tekton/values.yaml --phase noise \
  --out workspace/tekton/compiled/
kubectl apply -f workspace/tekton/compiled/noise-pipeline.yaml \
  || { echo "HALT: kubectl apply pipeline failed for noise"; exit 1; }
~~~

**Sequential noise run loop:**

~~~bash
NOISE_RUNS=$(.venv/bin/python -c \
  "import yaml; v=yaml.safe_load(open('workspace/tekton/values.yaml')); \
   print(v['observe']['noise_runs'])")

for i in $(seq 0 $((NOISE_RUNS - 1))); do
  PIPELINERUN_NAME=sim2real-noise-run${i}-$(date +%s)
  echo "=== Noise run $i of $((NOISE_RUNS - 1)): $PIPELINERUN_NAME ==="
~~~

**Pre-flight:**
~~~bash
  .venv/bin/python tools/transfer_cli.py preflight \
    --phase noise --values workspace/tekton/values.yaml --namespace $NAMESPACE
~~~
**HALT if exit 1.** Note: if the prior run's `finally` teardown is still completing,
preflight may transiently fail. Wait 30 seconds and retry once before halting.

**Submit:**
~~~bash
  .venv/bin/python tools/transfer_cli.py render-pipelinerun \
    --template workspace/tekton/pipelinerun-noise.yaml \
    --vars PIPELINERUN_NAME=$PIPELINERUN_NAME NAMESPACE=$NAMESPACE \
           PHASE=noise RUN_INDEX=$i \
    --out /tmp/pipelinerun-noise-run${i}.yaml \
    || { echo "HALT: render-pipelinerun failed for noise run $i"; exit 1; }
  kubectl apply -f /tmp/pipelinerun-noise-run${i}.yaml \
    || { echo "HALT: kubectl apply pipelinerun failed for noise run $i"; exit 1; }
  .venv/bin/python tools/transfer_cli.py benchmark-state --workspace workspace/ \
    --set-phase noise --status running --pipelinerun $PIPELINERUN_NAME
~~~

**Wait (4h timeout per run):**
~~~bash
  TIMEOUT_SECS=14400; ELAPSED=0
  while true; do
    REASON=$(tkn pr describe $PIPELINERUN_NAME \
      -o jsonpath='{.status.conditions[0].reason}' 2>/dev/null)
    echo "$REASON" | grep -qE 'Succeeded|Failed|PipelineRunCancelled|CouldntGetTask' && break
    sleep 30; ELAPSED=$((ELAPSED+30))
    if [ $ELAPSED -ge $TIMEOUT_SECS ]; then
      .venv/bin/python tools/transfer_cli.py benchmark-state --workspace workspace/ \
        --set-phase noise --status failed \
        --failure-reason "Polling timeout after ${TIMEOUT_SECS}s on run $i"
      echo "HALT: noise run $i timed out."; exit 1
    fi
  done
~~~

**On Failed:**
~~~bash
  REASON=$(tkn pr describe $PIPELINERUN_NAME \
    -o jsonpath='{.status.conditions[0].reason}' 2>/dev/null)
  if echo "$REASON" | grep -qE 'Failed|PipelineRunCancelled|CouldntGetTask'; then
    FAIL_REASON=$(tkn pr describe $PIPELINERUN_NAME \
      -o jsonpath='{.status.conditions[0].message}' 2>/dev/null || echo "PipelineRun failed")
    .venv/bin/python tools/transfer_cli.py benchmark-state --workspace workspace/ \
      --set-phase noise --status failed \
      --failure-reason "$FAIL_REASON"
    echo "HALT: noise run $i failed — $FAIL_REASON"; exit 1
  fi

done  # end noise run loop
~~~

**After loop — extract all noise runs via single extractor pod:**
~~~bash
trap "kubectl delete pod sim2real-extract-noise -n $NAMESPACE --ignore-not-found 2>/dev/null" EXIT ERR
kubectl delete pod sim2real-extract-noise -n $NAMESPACE --ignore-not-found 2>/dev/null || true
kubectl run sim2real-extract-noise --image=alpine:3.19 --restart=Never \
  --overrides='{"spec":{"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"data-pvc"}}],"containers":[{"name":"e","image":"alpine:3.19","command":["sleep","600"],"volumeMounts":[{"name":"data","mountPath":"/data"}]}]}}' \
  -n $NAMESPACE
kubectl wait pod/sim2real-extract-noise --for=condition=Ready --timeout=60s -n $NAMESPACE \
  || { echo "HALT: extractor pod not ready"; exit 1; }
kubectl cp $NAMESPACE/sim2real-extract-noise:/data/noise/ workspace/noise_raw/ --retries=3 \
  || { echo "HALT: kubectl cp failed"; exit 1; }
.venv/bin/python tools/transfer_cli.py convert-trace \
  --input-dir workspace/noise_raw/ --output workspace/noise_results.json \
  || { echo "HALT: convert-trace failed"; exit 1; }
.venv/bin/python tools/transfer_cli.py validate-schema workspace/noise_results.json \
  || { echo "HALT: schema validation failed for workspace/noise_results.json — results file is malformed, do not mark phase done"; exit 1; }
.venv/bin/python tools/transfer_cli.py benchmark-state --workspace workspace/ \
  --set-phase noise --status done --results workspace/noise_results.json

fi  # end if NOISE_STATUS != done
~~~

#### Baseline and treatment phases

Execute the procedure below for `baseline`, then `treatment` (phases with status
`done` are skipped automatically):

~~~bash
for phase in baseline treatment; do
  STATUS=$(.venv/bin/python -c \
    "import json; print(json.load(open('workspace/benchmark_state.json'))['phases']['$phase']['status'])" \
    2>/dev/null || echo "unknown")
  if [ "$STATUS" = "done" ]; then
    echo "Phase $phase already done — skipping."; continue
  fi
  echo "=== Processing phase: $phase ==="
~~~

**Pre-flight:**
~~~bash
.venv/bin/python tools/transfer_cli.py preflight \
  --phase $phase --values workspace/tekton/values.yaml --namespace $NAMESPACE
~~~
**HALT if exit 1.**

**Compile:**
~~~bash
.venv/bin/python tools/transfer_cli.py compile-pipeline \
  --template-dir tektonc-data-collection/tektoncsample/sim2real \
  --values workspace/tekton/values.yaml --phase $phase \
  --out workspace/tekton/compiled/
~~~

**Submit:**
~~~bash
kubectl apply -f workspace/tekton/compiled/${phase}-pipeline.yaml \
  || { echo "HALT: kubectl apply pipeline failed for $phase"; exit 1; }
PIPELINERUN_NAME=sim2real-${phase}-$(date +%s)
.venv/bin/python tools/transfer_cli.py render-pipelinerun \
  --template workspace/tekton/pipelinerun-${phase}.yaml \
  --vars PIPELINERUN_NAME=$PIPELINERUN_NAME NAMESPACE=$NAMESPACE PHASE=$phase \
  --out /tmp/pipelinerun-${phase}.yaml \
  || { echo "HALT: render-pipelinerun failed for $phase"; exit 1; }
kubectl apply -f /tmp/pipelinerun-${phase}.yaml \
  || { echo "HALT: kubectl apply pipelinerun failed for $phase"; exit 1; }
.venv/bin/python tools/transfer_cli.py benchmark-state --workspace workspace/ \
  --set-phase $phase --status running --pipelinerun $PIPELINERUN_NAME
~~~

**Wait (4h timeout):**
~~~bash
TIMEOUT_SECS=14400; ELAPSED=0
while true; do
  REASON=$(tkn pr describe $PIPELINERUN_NAME \
    -o jsonpath='{.status.conditions[0].reason}' 2>/dev/null)
  echo "$REASON" | grep -qE 'Succeeded|Failed|PipelineRunCancelled|CouldntGetTask' && break
  sleep 30; ELAPSED=$((ELAPSED+30))
  if [ $ELAPSED -ge $TIMEOUT_SECS ]; then
    .venv/bin/python tools/transfer_cli.py benchmark-state --workspace workspace/ \
      --set-phase $phase --status failed \
      --failure-reason "Polling timeout after ${TIMEOUT_SECS}s"
    echo "HALT: $phase pipeline timed out."; exit 1
  fi
done
~~~

**On Failed:**
~~~bash
FAIL_REASON=$(tkn pr describe $PIPELINERUN_NAME \
  -o jsonpath='{.status.conditions[0].message}' 2>/dev/null || echo "PipelineRun failed")
.venv/bin/python tools/transfer_cli.py benchmark-state --workspace workspace/ \
  --set-phase $phase --status failed \
  --failure-reason "$FAIL_REASON"
echo "HALT: $phase pipeline failed — $FAIL_REASON"; exit 1
~~~

**On Succeeded — extract via extractor pod:**
~~~bash
trap "kubectl delete pod sim2real-extract-${phase} -n $NAMESPACE --ignore-not-found 2>/dev/null" EXIT ERR
kubectl run sim2real-extract-${phase} --image=alpine:3.19 --restart=Never \
  --overrides='{"spec":{"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"data-pvc"}}],"containers":[{"name":"e","image":"alpine:3.19","command":["sleep","600"],"volumeMounts":[{"name":"data","mountPath":"/data"}]}]}}' \
  -n $NAMESPACE
kubectl wait pod/sim2real-extract-${phase} --for=condition=Ready --timeout=60s -n $NAMESPACE \
  || { echo "HALT: extractor pod not ready"; exit 1; }
kubectl cp $NAMESPACE/sim2real-extract-${phase}:/data/${phase}/ workspace/${phase}_raw/ --retries=3 \
  || { echo "HALT: kubectl cp failed"; exit 1; }
.venv/bin/python tools/transfer_cli.py convert-trace \
  --input-dir workspace/${phase}_raw/ --output workspace/${phase}_results.json \
  || { echo "HALT: convert-trace failed"; exit 1; }
.venv/bin/python tools/transfer_cli.py validate-schema workspace/${phase}_results.json \
  || { echo "HALT: schema validation failed for workspace/${phase}_results.json — results file is malformed, do not mark phase done"; exit 1; }
.venv/bin/python tools/transfer_cli.py benchmark-state --workspace workspace/ \
  --set-phase $phase --status done --results workspace/${phase}_results.json
done  # end for phase in baseline treatment
~~~

### 5c. Mechanism check

~~~bash
.venv/bin/python tools/transfer_cli.py benchmark \
  --noise    workspace/noise_results.json \
  --baseline workspace/baseline_results.json \
  --treatment workspace/treatment_results.json \
  --signal-coverage workspace/signal_coverage.json \
  --workloads-dir blis_router/workloads/ \
  --out workspace/benchmark_output.json
.venv/bin/python tools/transfer_cli.py validate-schema workspace/benchmark_output.json \
  || { echo "HALT: benchmark_output.json failed schema validation"; exit 1; }
~~~

Exit 0 = PASS or INCONCLUSIVE (parse `mechanism_check_verdict` from JSON).
**HALT if exit 1** (FAIL). **HALT if exit 2** (infrastructure error (file missing or invalid JSON — check stderr) OR ERROR verdict (no matched workloads — check `mechanism_check_verdict` in output JSON)).

~~~bash
MECH_VERDICT=$(python -c "import json; print(json.load(open('workspace/benchmark_output.json'))['mechanism_check_verdict'])")
if [ "$MECH_VERDICT" = "INCONCLUSIVE" ]; then
  echo "OPERATOR REVIEW REQUIRED: mechanism_check_verdict is INCONCLUSIVE."
  echo "Inspect workspace/benchmark_output.json, resolve ambiguity, then re-run or override manually."
  echo "Do NOT proceed to generate-evidence or Stage 6 without explicit operator sign-off."
  exit 1
fi
~~~

Remediation options for INCONCLUSIVE:
  1. **Re-run during lower-variance window** — cluster noise may be temporarily elevated; retry during off-peak hours.
  2. **Inspect per-workload improvements** — check the `workload_classification` array in `workspace/benchmark_output.json`. If one matched workload is close to T_eff, a targeted re-run of that workload may resolve the ambiguity.
  3. **Accept as soft-pass with operator sign-off** — if improvement is consistent across matched workloads but marginally below T_eff, the operator may manually set `overall_verdict: "INCONCLUSIVE"` and populate `operator_notes` with the rationale. This is the **only** path that produces an INCONCLUSIVE overall_verdict — it requires explicit operator sign-off and must bypass the automatic HALT above.

### 5c-merge. Merge benchmark output into validation_results.json

`generate-evidence` (Step 5d) reads `benchmark` from `workspace/validation_results.json`. This step merges `workspace/benchmark_output.json` into the partial file written in Step 4b.

Note: `noise_cv` from `benchmark_output.json` must be placed at the top level of `validation_results.json` (not inside `benchmark`), because `validation_results.schema.json` places `noise_cv` at top level and has `additionalProperties: false` on the `benchmark` sub-object.

~~~bash
test -f workspace/validation_results.json \
  || { echo "HALT: workspace/validation_results.json not found — ensure Suites A/B/C have run before Step 5c-merge"; exit 1; }
.venv/bin/python - <<'EOF'
import json, sys

bench = json.loads(open('workspace/benchmark_output.json').read())
val_path = 'workspace/validation_results.json'
val = json.loads(open(val_path).read())

# Copy all benchmark fields except noise_cv into val["benchmark"]
val['benchmark'] = {k: v for k, v in bench.items() if k != 'noise_cv'}
# noise_cv goes to top-level (per validation_results.schema.json)
val['noise_cv'] = bench['noise_cv']

mech = bench.get('mechanism_check_verdict', 'ERROR')
if mech == 'PASS' and val.get('suite_a', {}).get('passed') and val.get('suite_c', {}).get('passed'):
    val['overall_verdict'] = 'PASS'
elif mech == 'INCONCLUSIVE':
    val['overall_verdict'] = 'INCONCLUSIVE'
else:
    val['overall_verdict'] = 'FAIL'

open(val_path, 'w').write(json.dumps(val, indent=2))
print('Merged benchmark into validation_results.json — overall_verdict:', val['overall_verdict'])
EOF
~~~

**HALT if exit non-zero.**

~~~bash
.venv/bin/python tools/transfer_cli.py validate-schema workspace/validation_results.json \
  || { echo "HALT: validation_results.json failed schema validation after benchmark merge"; exit 1; }
~~~

### 5d. Generate evidence document

~~~bash
.venv/bin/python tools/transfer_cli.py generate-evidence \
  --workspace workspace/ --out workspace/transfer_evidence.md
~~~

**HALT if exit 1.**

### 5e. Benchmark comparison table

```bash
.venv/bin/python tools/transfer_cli.py compare \
  --baseline workspace/baseline_results.json \
  --treatment workspace/treatment_results.json \
  --out workspace/comparison_table.txt
```

**HALT if exit non-zero.**

The table is printed to stdout and written to `workspace/comparison_table.txt`. Stdout output is human feedback only — not pipeline-consumable.

## Step 6: Final artifact validation

`workspace/validation_results.json` was completed by Step 5c-merge (which added `benchmark`, `noise_cv`, and `overall_verdict`). Run a final schema check:

```bash
.venv/bin/python tools/transfer_cli.py validate-schema workspace/validation_results.json
```

**HALT if validate-schema exits non-zero.**

**Manual verification (required — the lightweight validator cannot enforce `if/then` conditionals):**
If `overall_verdict` is `"INCONCLUSIVE"`, verify that `operator_notes` is present and non-empty in `workspace/validation_results.json`. This is the audit trail for the Step 5c option 3 soft-pass path. **HALT if `overall_verdict` is `"INCONCLUSIVE"` and `operator_notes` is absent or empty.** Message: "HALT: operator_notes required for INCONCLUSIVE verdict (Step 5c option 3 soft-pass audit trail)."

## Step 7: Proceed to Stage 6

If overall_verdict == "PASS", proceed to `prompts/pr.md` (Stage 6). **Note:** `prompts/pr.md` is a PR6 deliverable and will not exist until PR6 is merged. If PR6 has not yet landed, stop here — Stage 5 is complete and Stage 6 will be available after PR6.
If overall_verdict == "INCONCLUSIVE" (only reachable via Step 5c option 3 operator sign-off), proceed to Stage 6 with the documented rationale in `operator_notes`. The same PR6 note above applies.
If overall_verdict == "FAIL", do NOT proceed — stop and document the failure.

## Halt Conditions Summary

| Condition | Trigger | Action |
|-----------|---------|--------|
| Missing prerequisite artifact | algorithm_summary.json, signal_coverage.json, or stage3_output.json absent/invalid; or Stage 4 `go build`/`go vet` fails | HALT: "Stage [N] prerequisite missing" |
| Missing equivalence gate | equivalence_results.json absent, invalid, or suite_a/suite_c not passed | Caught by prerequisite check above; Stage 4.5 must pass before Stage 5 runs |
| Noise not done (Step 1) | benchmark-state exit 0 but noise phase != "done" | Exit 3 (REENTER): jump to Step 5, run noise phase, then re-enter from Step 1 |
| benchmark-state infrastructure error | benchmark-state exit 2 (missing algorithm_summary.json) | HALT: "benchmark-state failed — missing workspace/algorithm_summary.json" |
| Cluster context mismatch | benchmark-state exit 1 (kubectl context changed) | HALT: "benchmark-state failed — check cluster context" |
| Preflight failure | preflight exit 1 | HALT: "Preflight failed for $phase" |
| Pipeline submission failure | kubectl apply exits non-zero | HALT: "kubectl apply failed for $phase" |
| Pipeline polling timeout | 4h elapsed without terminal state | HALT: "$phase pipeline timed out" |
| Pipeline run failure | tkn pr describe shows Failed/PipelineRunCancelled | HALT: "$phase pipeline failed — $FAIL_REASON" |
| Extractor pod failure | kubectl cp or convert-trace exits non-zero | HALT: "extractor pod not ready" / "kubectl cp failed" / "convert-trace failed" |
| Results schema validation failure | validate-schema exits non-zero on phase_results.json | HALT: "schema validation failed for workspace/${phase}_results.json — do not mark phase done" |
| Benchmark input failure | benchmark exit 2 with no JSON output (file missing or invalid JSON) — check stderr only | HALT: "benchmark infrastructure error — see stderr" |
| Benchmark ERROR verdict | benchmark exit 2 with JSON output written — `mechanism_check_verdict` is ERROR (all workloads skipped due to name mismatch, or no matched signal classifications) | HALT: "benchmark ERROR — check workload names and signal_coverage.json" |
| Suite A FAIL (Stage 4.5 gate) | equivalence_results.json missing, invalid, or suite_a.passed=false | HALT at prerequisite check: "HALT: Suite A did not pass in Stage 4.5" |
| Suite C FAIL (Stage 4.5 gate) | equivalence_results.json missing, invalid, or suite_c.passed=false | HALT at prerequisite check: "HALT: Suite C did not pass in Stage 4.5" |
| Mechanism FAIL | no matched workload improvement ≥ T_eff | HALT: "Mechanism check FAIL" |
| Mechanism INCONCLUSIVE | improvement > 0 but < T_eff | HALT: "Mechanism check INCONCLUSIVE" — **unless** operator manually overrides (soft-pass with sign-off). If override chosen, set `overall_verdict: "INCONCLUSIVE"` and populate `operator_notes` with rationale. See Step 5c and Step 7 for details. |
| Specificity check failures | Unmatched workload(s) with \|change\|/baseline ≥ T_eff | No halt — informational only. Recorded in `specificity_notes` of benchmark output for audit trail. Does not affect mechanism_check_verdict. |
| Schema validation failure | validate-schema exits non-zero | HALT: "Schema validation failed" |
