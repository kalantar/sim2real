# Stream EPP Logs Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a parallel Tekton task that streams full EPP pod logs to the data-pvc in real time, splitting output into 5-minute time-bucketed NDJSON files inside each workload's results directory.

**Architecture:** A new `stream-epp-logs` task starts after `deploy-gaie` in parallel with the workload. It runs `kubectl logs --follow --timestamps=true` piped through a BusyBox awk splitter that writes time-bucketed files to `${resultsDir}/epp_logs/`. It polls for a per-workload sentinel file written by the modified `collect-results` task. `collect-results` loses its redundant EPP log-collection block and gains a `touch epp_stream_done` at the end. The pipeline template gains one new task entry.

**Tech Stack:** Tekton v1 YAML, POSIX sh, BusyBox awk (alpine/kubectl:1.34.1 image)

**Spec:** `docs/superpowers/specs/2026-04-12-stream-epp-logs-design.md`

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `tektonc-data-collection/tekton/tasks/stream-epp-logs.yaml` | New Tekton task: wait for EPP pod, stream logs, split by time bucket, exit on sentinel |
| Modify | `tektonc-data-collection/tekton/tasks/collect-results.yaml` | Remove EPP log-collection block; write per-workload sentinel at end |
| Modify | `tektonc-data-collection/tektoncsample/sim2real/pipeline.yaml.j2` | Add `stream-epp-logs` task with `runAfter: [deploy-gaie]` |
| Create | `tektonc-data-collection/tests/test_stream_epp_logs_awk.sh` | Shell test for the awk splitter logic (runs locally, no cluster needed) |

---

## Chunk 1: awk splitter test

### Task 1: Write and verify the awk splitter test

The awk script is the trickiest part of this feature. Write and validate it as a standalone shell test before embedding it in the task YAML.

**Files:**
- Create: `tektonc-data-collection/tests/test_stream_epp_logs_awk.sh`

- [ ] **Step 1: Create the test directory and test script**

```bash
mkdir -p tektonc-data-collection/tests
```

Create `tektonc-data-collection/tests/test_stream_epp_logs_awk.sh`:

```sh
#!/bin/sh
# Tests the awk log-splitting logic used in stream-epp-logs task.
# Run with: sh tektonc-data-collection/tests/test_stream_epp_logs_awk.sh

TMPDIR_TEST=$(mktemp -d)
trap "rm -rf ${TMPDIR_TEST}" EXIT

PASS=true

assert_file_exists() {
  if [ ! -f "$1" ]; then
    echo "FAIL: $1 not found"
    PASS=false
  else
    echo "PASS: $1 exists"
  fi
}

assert_line_count() {
  COUNT=$(wc -l < "$1" | tr -d ' ')
  if [ "${COUNT}" -eq "$2" ]; then
    echo "PASS: $(basename $1) has $2 lines"
  else
    echo "FAIL: $(basename $1) has ${COUNT} lines, expected $2"
    PASS=false
  fi
}

# --- Input: kubectl logs --timestamps=true format ---
# Format: <RFC3339-timestamp> <original-json-log-line>
# lines 1+2 → bucket 2325 (floor(28/5)*5=25, floor(29/5)*5=25)
# lines 3+4 → bucket 2330 (floor(30/5)*5=30, floor(34/5)*5=30)
# line 5    → bucket 2335 (floor(35/5)*5=35)
# line 6    → bucket 2325 (contains spaces in JSON value — must be preserved)
cat > "${TMPDIR_TEST}/input.txt" << 'EOF'
2026-04-11T23:28:39.123456789Z {"level":"Level(-5)","ts":"2026-04-11T23:28:39Z","msg":"line 1"}
2026-04-11T23:29:59.999999999Z {"level":"Level(-5)","ts":"2026-04-11T23:29:59Z","msg":"line 2"}
2026-04-11T23:30:00.000000000Z {"level":"Level(-5)","ts":"2026-04-11T23:30:00Z","msg":"line 3"}
2026-04-11T23:34:59.000000000Z {"level":"Level(-5)","ts":"2026-04-11T23:34:59Z","msg":"line 4"}
2026-04-11T23:35:00.000000000Z {"level":"Level(-5)","ts":"2026-04-11T23:35:00Z","msg":"line 5"}
2026-04-11T23:28:50.000000000Z {"level":"info","msg":"a message with spaces","key":"val"}
EOF

POD="test-pod-abc123"
WIN=5
OUTDIR="${TMPDIR_TEST}/epp_logs"
mkdir -p "${OUTDIR}"

# --- The awk script (copied verbatim from stream-epp-logs.yaml) ---
awk -v outdir="${OUTDIR}" -v pod="${POD}" -v win="${WIN}" '
  {
    split($1, dt, "T")
    split(dt[2], tm, ":")
    hour = tm[1]
    min  = int(tm[2])
    bucket = int(min / win) * win
    fname = sprintf("%s/%s_%s%02d.log", outdir, pod, hour, bucket)
    out = ""
    for (i = 2; i <= NF; i++) out = out (i > 2 ? " " : "") $i
    print out >> fname
    fflush(fname)
  }
' < "${TMPDIR_TEST}/input.txt"

# --- Assertions ---

FILE_2325="${OUTDIR}/${POD}_2325.log"
FILE_2330="${OUTDIR}/${POD}_2330.log"
FILE_2335="${OUTDIR}/${POD}_2335.log"

assert_file_exists "${FILE_2325}"
assert_file_exists "${FILE_2330}"
assert_file_exists "${FILE_2335}"

assert_line_count "${FILE_2325}" 3   # lines 1, 2, 6
assert_line_count "${FILE_2330}" 2   # lines 3, 4
assert_line_count "${FILE_2335}" 1   # line 5

# Output must be pure JSON — first char must be '{'
FIRST_CHAR=$(head -c1 "${FILE_2325}")
if [ "${FIRST_CHAR}" = "{" ]; then
  echo "PASS: output is pure JSON (no kubectl timestamp prefix)"
else
  echo "FAIL: unexpected first char '${FIRST_CHAR}', expected '{'"
  PASS=false
fi

# Spaces within JSON values must be preserved
if grep -q '"a message with spaces"' "${FILE_2325}"; then
  echo "PASS: spaces in JSON values preserved"
else
  echo "FAIL: spaces in JSON values not preserved"
  PASS=false
fi

# Files must sort lexicographically in chronological order
SORTED=$(ls "${OUTDIR}" | sort)
EXPECTED="${POD}_2325.log
${POD}_2330.log
${POD}_2335.log"
if [ "${SORTED}" = "${EXPECTED}" ]; then
  echo "PASS: files sort lexicographically = chronologically"
else
  echo "FAIL: sort order unexpected: ${SORTED}"
  PASS=false
fi

echo ""
if ${PASS}; then
  echo "ALL TESTS PASSED"
  exit 0
else
  echo "SOME TESTS FAILED"
  exit 1
fi
```

- [ ] **Step 2: Run the test — expect PASS**

```bash
sh tektonc-data-collection/tests/test_stream_epp_logs_awk.sh
```

Expected output:
```
PASS: test-pod-abc123_2325.log exists
PASS: test-pod-abc123_2330.log exists
PASS: test-pod-abc123_2335.log exists
PASS: test-pod-abc123_2325.log has 3 lines
PASS: test-pod-abc123_2330.log has 2 lines
PASS: test-pod-abc123_2335.log has 1 line
PASS: output is pure JSON (no kubectl timestamp prefix)
PASS: spaces in JSON values preserved
PASS: files sort lexicographically = chronologically

ALL TESTS PASSED
```

If any test fails, fix the awk script in the test file before continuing. **The awk in the test and in the task YAML must be byte-for-byte identical** — the test is the source of truth.

- [ ] **Step 3: Commit**

```bash
git add tektonc-data-collection/tests/test_stream_epp_logs_awk.sh
git commit -m "test: awk log-splitting logic for stream-epp-logs task"
```

---

## Chunk 2: New stream-epp-logs task

### Task 2: Create stream-epp-logs.yaml

**Files:**
- Create: `tektonc-data-collection/tekton/tasks/stream-epp-logs.yaml`

- [ ] **Step 1: Create the task file**

Create `tektonc-data-collection/tekton/tasks/stream-epp-logs.yaml`:

```yaml
# tektonc-data-collection/tekton/tasks/stream-epp-logs.yaml
apiVersion: tekton.dev/v1
kind: Task
metadata:
  name: stream-epp-logs
spec:
  description: >-
    Streams EPP pod logs to data-pvc throughout a workload run, splitting
    output into 5-minute time-bucketed NDJSON files under resultsDir/epp_logs/.
    Runs in parallel with run-workload; exits when collect-results writes
    resultsDir/epp_stream_done. Non-fatal: failures do not fail the pipeline.

  workspaces:
    - name: data
      description: "Shared PVC (data-pvc)"

  params:
    - { name: namespace,     type: string }
    - { name: modelLabel,    type: string }
    - { name: resultsDir,    type: string }
    - { name: windowMinutes, type: string, default: "5" }

  steps:
    - name: wait-and-stream
      image: alpine/kubectl:1.34.1
      securityContext:
        runAsUser: 0
      script: |
        #!/bin/sh
        set +e

        NAMESPACE="$(params.namespace)"
        MODEL_LABEL="$(params.modelLabel)"
        RESULTS_DIR="/workspace/data/$(params.resultsDir)"
        LOG_DIR="${RESULTS_DIR}/epp_logs"
        SENTINEL="${RESULTS_DIR}/epp_stream_done"
        WINDOW="$(params.windowMinutes)"

        # Phase 0 — Validation
        if ! echo "${WINDOW}" | grep -qE '^[1-9][0-9]?$' || [ "${WINDOW}" -gt 60 ]; then
          echo "ERROR: windowMinutes must be integer 1-60, got: ${WINDOW}"
          exit 0
        fi

        # Phase 1 — Pod discovery
        # Uses same truncation logic as deploy-gaie and collect-results.
        RELEASE="${MODEL_LABEL}-gaie"
        EPP_DEPLOY="$(echo -n "${RELEASE}" | cut -c1-40)-epp"

        echo "Waiting for EPP pod with prefix: ${EPP_DEPLOY}"
        EPP_PODS=""
        i=0
        while [ $i -lt 20 ]; do
          EPP_PODS=$(kubectl get pods -n "${NAMESPACE}" \
            --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null \
            | grep "^${EPP_DEPLOY}" || true)
          [ -n "${EPP_PODS}" ] && break
          i=$((i+1))
          sleep 3
        done

        if [ -z "${EPP_PODS}" ]; then
          echo "WARNING: No EPP pods found with prefix ${EPP_DEPLOY} after 60s, skipping."
          exit 0
        fi

        # Phase 2 — Streaming
        mkdir -p "${LOG_DIR}"
        PIDS=""
        for pod in ${EPP_PODS}; do
          echo "Streaming EPP logs: ${pod} → ${LOG_DIR}/"
          kubectl logs --follow --timestamps=true "${pod}" -n "${NAMESPACE}" \
            | awk -v outdir="${LOG_DIR}" -v pod="${pod}" -v win="${WINDOW}" '
              {
                split($1, dt, "T")
                split(dt[2], tm, ":")
                hour = tm[1]
                min  = int(tm[2])
                bucket = int(min / win) * win
                fname = sprintf("%s/%s_%s%02d.log", outdir, pod, hour, bucket)
                out = ""
                for (i = 2; i <= NF; i++) out = out (i > 2 ? " " : "") $i
                print out >> fname
                fflush(fname)
              }
            ' &
          # $! is the awk PID (rightmost in pipe). Killing awk breaks the pipe;
          # kubectl receives SIGPIPE and exits cleanly.
          PIDS="${PIDS} $!"
        done

        # Phase 3 — Sentinel poll
        echo "Streaming started. Polling for sentinel: ${SENTINEL}"
        while true; do
          if [ -f "${SENTINEL}" ]; then
            echo "Sentinel found, stopping streams."
            break
          fi
          ALL_DEAD=true
          for pid in ${PIDS}; do
            if kill -0 "${pid}" 2>/dev/null; then
              ALL_DEAD=false
              break
            fi
          done
          if ${ALL_DEAD}; then
            echo "All EPP stream processes exited."
            break
          fi
          sleep 5
        done

        kill ${PIDS} 2>/dev/null || true
        wait ${PIDS} 2>/dev/null || true
        echo "EPP log streaming complete for $(params.resultsDir)"
```

- [ ] **Step 2: Verify the awk block in this file is identical to the one in the passing test**

Extract just the awk block from both files and diff them:

```bash
# From test: lines between the awk '...' delimiters
grep -A20 "^awk -v outdir" tektonc-data-collection/tests/test_stream_epp_logs_awk.sh \
  | sed -n "/^awk/,/^'/p"

# From task: same block (visually compare)
grep -A20 "awk -v outdir" tektonc-data-collection/tekton/tasks/stream-epp-logs.yaml
```

They must be identical (modulo leading whitespace from YAML indentation).

- [ ] **Step 3: Validate YAML syntax**

```bash
python3 -c "
import yaml
doc = yaml.safe_load(open('tektonc-data-collection/tekton/tasks/stream-epp-logs.yaml'))
assert doc['kind'] == 'Task'
assert doc['metadata']['name'] == 'stream-epp-logs'
steps = doc['spec']['steps']
assert len(steps) == 1
assert steps[0]['name'] == 'wait-and-stream'
print('YAML valid, task name and step name correct')
"
```

Expected: `YAML valid, task name and step name correct`

- [ ] **Step 4: Commit**

```bash
git add tektonc-data-collection/tekton/tasks/stream-epp-logs.yaml
git commit -m "feat: add stream-epp-logs Tekton task"
```

---

## Chunk 3: Modify collect-results and pipeline template

### Task 3: Modify collect-results.yaml

**Files:**
- Modify: `tektonc-data-collection/tekton/tasks/collect-results.yaml`

Two changes: (1) remove the EPP log-collection block (lines 45–71), (2) add sentinel `touch` before the final echo.

- [ ] **Step 1: Remove the EPP block**

In `collect-results.yaml`, delete the section between the `# --- EPP pod logs ---` comment and the blank line before `# --- vLLM decode pod logs ---` (currently lines 45–71). The section to remove looks exactly like:

```sh
        # --- EPP pod logs ---
        RELEASE="${MODEL_LABEL}-gaie"
        EPP_DEPLOY="$(echo -n "${RELEASE}" | cut -c1-40)-epp"

        EPP_PODS=$(kubectl get pods -n "${NAMESPACE}" \
          --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null \
          | grep "^${EPP_DEPLOY}" || true)

        if [ -z "${EPP_PODS}" ]; then
          echo "No EPP pods found with prefix ${EPP_DEPLOY}, skipping."
        else
          for pod in ${EPP_PODS}; do
            echo "Collecting EPP logs: ${pod}"
            INIT_CTRS=$(kubectl get pod "${pod}" -n "${NAMESPACE}" \
              -o jsonpath='{.spec.initContainers[*].name}' 2>/dev/null || true)
            CTRS=$(kubectl get pod "${pod}" -n "${NAMESPACE}" \
              -o jsonpath='{.spec.containers[*].name}' 2>/dev/null || true)
            for ctr in ${INIT_CTRS}; do
              kubectl logs "${pod}" -n "${NAMESPACE}" -c "${ctr}" \
                > "${RESULTS_DIR}/${pod}_init-${ctr}.log" 2>&1
            done
            for ctr in ${CTRS}; do
              kubectl logs "${pod}" -n "${NAMESPACE}" -c "${ctr}" \
                > "${RESULTS_DIR}/${pod}_${ctr}.log" 2>&1
            done
          done
        fi
```

- [ ] **Step 2: Add sentinel touch before the final echo**

At the very end of the `collect-logs` step script, before the final `echo "Log collection complete..."` line, add:

```sh
        # Signal stream-epp-logs to stop following this workload's EPP pod.
        touch "${RESULTS_DIR}/epp_stream_done"
```

The end of the script should now read:

```sh
        # Signal stream-epp-logs to stop following this workload's EPP pod.
        touch "${RESULTS_DIR}/epp_stream_done"

        echo "Log collection complete for $(params.resultsDir)."
```

- [ ] **Step 3: Validate YAML and verify EPP block is gone**

```bash
python3 -c "
import yaml
doc = yaml.safe_load(open('tektonc-data-collection/tekton/tasks/collect-results.yaml'))
script = doc['spec']['steps'][1]['script']
assert 'EPP pod logs' not in script, 'EPP block still present'
assert 'epp_stream_done' in script, 'sentinel touch missing'
assert 'vLLM decode pod logs' in script, 'vLLM block unexpectedly removed'
print('YAML valid: EPP block removed, sentinel present, vLLM block intact')
"
```

Expected: `YAML valid: EPP block removed, sentinel present, vLLM block intact`

- [ ] **Step 4: Commit**

```bash
git add tektonc-data-collection/tekton/tasks/collect-results.yaml
git commit -m "feat: remove EPP log collection from collect-results; write stream sentinel"
```

---

### Task 4: Add stream-epp-logs to pipeline template

**Files:**
- Modify: `tektonc-data-collection/tektoncsample/sim2real/pipeline.yaml.j2`

- [ ] **Step 1: Insert the stream-epp-logs task**

In `pipeline.yaml.j2`, after the closing of the `deploy-gaie` task block (after line 57, before `deploy-model`), insert:

```yaml
    - name: stream-epp-logs
      runAfter: ["deploy-gaie"]
      taskRef:
        name: stream-epp-logs
      workspaces:
        - name: data
          workspace: data-storage
      params:
        - name: namespace
          value: "$(params.namespace)"
        - name: modelLabel
          value: "sim2real-$(params.experimentId)"
        - name: resultsDir
          value: "$(params.runName)/{{ phase }}/$(params.workloadName)"

```

Note: `{{ phase }}` is a Jinja2 template variable resolved by `tektonc` at compile time (becomes `baseline` or `treatment`). Do not replace it with a literal value.

- [ ] **Step 2: Validate the template compiles without errors**

```bash
cd tektonc-data-collection
python3 -c "
import jinja2
src = open('tektoncsample/sim2real/pipeline.yaml.j2').read()
env = jinja2.Environment(undefined=jinja2.Undefined)
env.parse(src)
print('Jinja2 template syntax valid')
"
cd ..
```

Expected: `Jinja2 template syntax valid`

Also verify the task appears in the template:

```bash
grep -A10 "name: stream-epp-logs" tektonc-data-collection/tektoncsample/sim2real/pipeline.yaml.j2
```

Expected: the task block with `runAfter: ["deploy-gaie"]` and `resultsDir` param.

- [ ] **Step 3: Commit**

```bash
git add tektonc-data-collection/tektoncsample/sim2real/pipeline.yaml.j2
git commit -m "feat: add stream-epp-logs task to sim2real pipeline template"
```

---

## Notes for the implementer

**Tekton task registration:** When deploying to the cluster, `stream-epp-logs.yaml` must be applied like other tasks:
```bash
kubectl apply -f tektonc-data-collection/tekton/tasks/stream-epp-logs.yaml -n <namespace>
```
The deploy scripts (`blis-data-collector` skill / `pipeline/deploy.py`) handle this by applying all files in `tekton/tasks/` — no additional registration needed.

**Pipeline recompilation:** The pipeline template change takes effect when `pipeline/prepare.py` is next run (`python pipeline/prepare.py ...`). The generated `cluster/<run>/baseline/sim2real-baseline-pipeline.yaml` and the combined `experiment-pipeline.yaml` are regenerated automatically.

**Sequential chaining in `make_experiment_pipeline`:** `stream-epp-logs` has no downstream dependents, making it a leaf task. `make_experiment_pipeline` (`pipeline/lib/tekton.py`) adds all leaf tasks of a group to the `anchor_tasks` list, so the next (phase, workload) group waits for `stream-epp-logs` to finish before starting. This is correct: the next group cannot begin until both `collect-results` (writes sentinel) and `stream-epp-logs` (reads sentinel, exits) have completed.

**Awk script identity:** The awk block in `stream-epp-logs.yaml` and the awk block in `tests/test_stream_epp_logs_awk.sh` must remain identical. The test is the executable specification for the splitter logic.
