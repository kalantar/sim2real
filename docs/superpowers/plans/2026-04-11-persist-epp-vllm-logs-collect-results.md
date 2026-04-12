# Design: Persist EPP and vLLM Logs to data-pvc (collect-results)

**Date:** 2026-04-11
**Status:** Draft

## Problem

`collect-results` is currently a no-op synchronization barrier. When the pipeline's
`finally` block runs, it deletes the EPP and vLLM deployments — discarding their logs.
The only recovery path is Tekton pod log retention (short-lived) or re-deploying the stack.

EPP logs (at `v=5` verbosity) and vLLM decode logs contain per-request scheduling decisions
and inference state that are essential for diagnosing unexpected benchmark results.

## Goals

1. Persist EPP container logs to data-pvc before `finally` tears down the deployment.
2. Persist vLLM decode pod logs to data-pvc before `finally` tears down the model.
3. Non-fatal: log collection failures must not fail the pipeline.
4. Minimal blast radius: only `collect-results.yaml` and `pipeline.yaml.j2` change.

## Non-Goals

- Real-time log streaming during workload execution.
- Collecting prefill pod logs (`prefill.create: false` in all current sim2real configs).
- Changing the extraction path in `scripts/deploy.py` (follow-on; see Relationship section).

## Storage Layout

Logs are written **inside each workload's results directory**, alongside the existing
`trace_header.yaml` and `trace_data.csv` files. Log filenames match the pod name.

```
/workspace/data/
└── <runName>/
    └── <phase>/
        └── <workloadName>/                     # existing workload results dir
            ├── trace_header.yaml               # existing
            ├── trace_data.csv                  # existing
            ├── <epp-pod-name>.log              # NEW — EPP pod log
            ├── <vllm-decode-pod-0>.log         # NEW — decode pod logs
            ├── <vllm-decode-pod-1>.log
            ├── <vllm-decode-pod-2>.log
            └── <vllm-decode-pod-3>.log
```

After `pipeline/deploy.py collect` extracts results from the PVC, the same files land at:
```
workspace/runs/<runName>/deploy_<phase>_log/<workloadName>/
    <epp-pod-name>.log
    <vllm-decode-pod-N>.log    (× 4 for a 4-replica decode setup)
```

**Write semantics:** EPP and vLLM serve all workloads in a phase. Each workload's
`collect-results` run writes the same pods' logs into that workload's directory, capturing
the cumulative log at that point in time. With sequential workload execution, later
workloads receive more complete logs. All workload directories get their own copy.

## New Parameters on collect-results Task

| Parameter    | Type   | Description |
|--------------|--------|-------------|
| `namespace`  | string | Kubernetes namespace where EPP/vLLM pods are running |
| `modelLabel` | string | Pod selector label value (e.g. `sim2real-<experimentId>`) |
| `resultsDir` | string | Relative path on data-pvc to the workload results directory |

`resultsDir` mirrors the `run-workload` task's same-named param:
`$(params.runName)/{{ phase }}/$(params.workloadName)`. No new path concept is needed —
logs go directly into the same directory as the trace files.

## EPP Log Collection

EPP pods are found by listing namespace pods whose name starts with the EPP deployment
name prefix. The deployment name follows the same derivation as `deploy-gaie.yaml`:

```sh
RELEASE="${MODEL_LABEL}-gaie"
EPP_DEPLOY="$(echo -n "${RELEASE}" | cut -c1-40)-epp"
```

Each matching pod's log is written as `<pod-name>.log`:

```sh
EPP_PODS=$(kubectl get pods -n "${NAMESPACE}" \
  --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null \
  | grep "^${EPP_DEPLOY}" || true)

for pod in ${EPP_PODS}; do
  kubectl logs "${pod}" -n "${NAMESPACE}" --all-containers=false \
    > "${RESULTS_DIR}/${pod}.log" 2>&1 || true
done
```

`--all-containers=false` targets the main EPP container only (excludes Envoy sidecar).
Full logs, no `--tail` truncation.

## vLLM Decode Log Collection

Decode pods are labeled `llm-d.ai/model=<modelLabel>`. Each pod's log is written as
`<pod-name>.log`:

```sh
DECODE_PODS=$(kubectl get pods -n "${NAMESPACE}" \
  -l "llm-d.ai/model=${MODEL_LABEL}" \
  --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null || true)

for pod in ${DECODE_PODS}; do
  kubectl logs "${pod}" -n "${NAMESPACE}" \
    > "${RESULTS_DIR}/${pod}.log" 2>&1 || true
done
```

If no pods match the loop is a no-op.

## collect-results.yaml — Full Task After Change

```yaml
apiVersion: tekton.dev/v1
kind: Task
metadata:
  name: collect-results
spec:
  description: >-
    Synchronization barrier. Runs after all run-workload-blis-observe tasks.
    Also collects EPP and vLLM decode pod logs into the workload results directory
    on data-pvc before the finally block deletes those deployments.

  workspaces:
    - name: data
      description: "Shared PVC (data-pvc)"

  params:
    - { name: namespace,   type: string }
    - { name: modelLabel,  type: string }
    - { name: resultsDir,  type: string }

  steps:
    - name: barrier
      image: alpine:3.19
      script: |
        #!/bin/sh
        echo "All workload tasks complete."
        echo "Results available on data-pvc."

    - name: collect-logs
      image: alpine/kubectl:1.34.1
      securityContext:
        runAsUser: 0
      script: |
        #!/bin/sh
        # Non-fatal: failures here must not fail the pipeline.
        set +e

        NAMESPACE="$(params.namespace)"
        MODEL_LABEL="$(params.modelLabel)"
        RESULTS_DIR="/workspace/data/$(params.resultsDir)"

        # Directory was created by run-workload; ensure it exists defensively.
        mkdir -p "${RESULTS_DIR}"

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
            echo "Collecting EPP log: ${pod}"
            kubectl logs "${pod}" -n "${NAMESPACE}" --all-containers=false \
              > "${RESULTS_DIR}/${pod}.log" 2>&1
          done
        fi

        # --- vLLM decode pod logs ---
        DECODE_PODS=$(kubectl get pods -n "${NAMESPACE}" \
          -l "llm-d.ai/model=${MODEL_LABEL}" \
          --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null || true)

        if [ -z "${DECODE_PODS}" ]; then
          echo "No vLLM decode pods found for llm-d.ai/model=${MODEL_LABEL}, skipping."
        else
          for pod in ${DECODE_PODS}; do
            echo "Collecting vLLM log: ${pod}"
            kubectl logs "${pod}" -n "${NAMESPACE}" \
              > "${RESULTS_DIR}/${pod}.log" 2>&1
          done
        fi

        echo "Log collection complete for $(params.resultsDir)."
```

## pipeline.yaml.j2 — Diff for collect-results Task

```yaml
    - name: collect-results
      runAfter: ["run-workload"]
      taskRef:
        name: collect-results
      workspaces:
        - name: data
          workspace: data-storage
      params:                                                                # NEW
        - name: namespace                                                    # NEW
          value: "$(params.namespace)"                                       # NEW
        - name: modelLabel                                                   # NEW
          value: "sim2real-$(params.experimentId)"                           # NEW
        - name: resultsDir                                                   # NEW
          value: "$(params.runName)/{{ phase }}/$(params.workloadName)"      # NEW
```

`{{ phase }}` is a Jinja compile-time variable rendered by tektonc, same as in `run-workload`'s `resultsDir`.

## Files Changed

| File | Change |
|------|--------|
| `tektonc-data-collection/tekton/tasks/collect-results.yaml` | Add 3 params; add `collect-logs` step |
| `tektonc-data-collection/tektoncsample/sim2real/pipeline.yaml.j2` | Wire 3 new params to `collect-results` |

No changes to `pipeline/lib/`, `config/env_defaults.yaml`, or any other task.

Note: `pipeline/deploy.py` was updated separately (collect bug fix) but those changes are
unrelated to this design — they fix how phases are discovered and how the extractor pod
works, not log collection.

## Relationship to 2026-03-30-persist-vllm-decode-logs.md

That plan collects vLLM logs in `scripts/deploy.py` via `kubectl get pods` on the
operator's machine **after** pipeline completion.

**`pipeline/deploy.py`**: No follow-on needed. `_extract_phase_from_pvc` does a flat
`kubectl cp` of the entire `{runName}/{phase}/` directory, so log files written to the
workload directories are automatically included in the extraction.

**`scripts/deploy.py`**: The follow-on still applies — `_extract_phase_results` copies from
`/data/{phase}/` and could be updated to rely on the PVC logs instead of querying kubectl
for decode pods separately.
