# Stream EPP Logs — Design Spec

**Date:** 2026-04-12  
**Status:** Draft

## Problem

EPP (Endpoint Picker Proxy) logs are extremely verbose — hundreds of lines per second of NDJSON. CRI-O's per-container log rotation limit (~10–50 MB) is exhausted in roughly one minute of EPP output. `kubectl logs` on a running pod only surfaces the current (unrotated) log file, so the `collect-results` task — which runs at end-of-workload — captures only the final ~1 minute of EPP activity. All earlier logs are permanently lost.

## Goal

Capture the full EPP log for every benchmark phase and store it on `data-pvc` in time-bucketed files for post-run analysis. The EPP pod must continue writing to stdout (live visibility in the OpenShift UI is a hard requirement — no in-pod file redirection).

## Non-Goals

- Real-time log search or querying during the run.
- Log collection for vLLM decode pods (already captured fully; they are less verbose).
- Changes to the EPP deployment or container configuration.

## Design

### Approach: Parallel streaming Tekton task

A new task `stream-epp-logs` starts immediately after `deploy-gaie` and runs in parallel with the workload tasks. It follows EPP stdout via `kubectl logs --follow --timestamps=true` and splits the stream into 5-minute time-bucketed files on `data-pvc`. The task exits when `collect-results` writes a sentinel file signalling that all workloads are complete.

This approach requires no cluster-level changes, no modifications to the EPP container, and integrates cleanly with Tekton's DAG execution model.

### New task: `stream-epp-logs`

**File:** `tektonc-data-collection/tekton/tasks/stream-epp-logs.yaml`

**Parameters:**

| Name | Type | Description |
|---|---|---|
| `namespace` | string | Kubernetes namespace |
| `modelLabel` | string | e.g. `sim2real-<experimentId>` |
| `phaseDir` | string | Phase-level results path on data-pvc, e.g. `<runName>/baseline` |
| `windowMinutes` | string | Log rotation window in minutes (default: `"5"`) |

**Workspace:** `data` (shared data-pvc)

**Steps:**

1. **wait-and-stream**

   - Poll (every 3 s) until at least one pod with the prefix `${MODEL_LABEL}-gaie...-epp` appears in `${NAMESPACE}`.
   - `mkdir -p "${phaseDir}/epp_logs"` on the PVC.
   - For each EPP pod found, launch a background pipeline:
     ```
     kubectl logs --follow --timestamps=true <pod> -n <ns> | awk-splitter
     ```
     The awk splitter (inline in the task script) reads each line, extracts the RFC3339 timestamp prepended by `--timestamps=true` (first whitespace-delimited field), parses hour (`HH`) and minute (`MM`), computes `bucket = floor(MM / windowMinutes) * windowMinutes`, and appends the line to:
     ```
     ${phaseDir}/epp_logs/${pod}_${HH}${bucket:02d}.log
     ```
     `fflush()` is called after each write so lines land on disk promptly rather than buffering.
   - Enter a sentinel-poll loop (every 5 s):
     - If `${phaseDir}/epp_stream_done` exists → break.
     - If all tracked pods have disappeared (crash / OOM) → break.
   - Kill all background stream processes (SIGTERM to the process group), `wait` for them to flush and exit.
   - Exit 0 (non-fatal; missing pods or partial streams must not fail the pipeline).

**Image:** `alpine/kubectl:1.34.1` (already used in the pipeline; BusyBox awk supports `fflush`).

### Log file layout

All EPP log files for a phase land in a single flat directory:

```
<runName>/<phase>/epp_logs/
  <pod-name>_HH{bucket:02d}.log
```

Examples (5-minute windows, UTC):
```
sim2real-adaptive6-gaie-epp-997dbb5f7-hpp7x_2325.log   # 23:25–23:29
sim2real-adaptive6-gaie-epp-997dbb5f7-hpp7x_2330.log   # 23:30–23:34
sim2real-adaptive6-gaie-epp-997dbb5f7-hpp7x_2335.log   # 23:35–23:39
```

If multiple EPP replicas are running, each pod produces its own set of files in the same directory.

### Modified task: `collect-results`

**Changes:**

1. Add a `phaseDir` parameter (type: string). The pipeline passes `$(params.runName)/{{ phase }}`.
2. Remove the EPP pod log-collection block (lines 45–71 in the current script). The streaming task owns EPP logs.
3. At the end of the `collect-logs` step, write the sentinel:
   ```sh
   touch "${phaseDir}/epp_stream_done"
   ```
   This signals `stream-epp-logs` to exit cleanly.
4. Keep the vLLM decode pod log-collection block unchanged.

### Modified pipeline template: `pipeline.yaml.j2`

1. Add pipeline-level param `phaseDir` with default value `$(params.runName)/{{ phase }}`.
2. Add task `stream-epp-logs`:
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
       - name: phaseDir
         value: "$(params.runName)/{{ phase }}"
   ```
3. Pass `phaseDir` to `collect-results`:
   ```yaml
   - name: phaseDir
     value: "$(params.runName)/{{ phase }}"
   ```

### Execution flow and Tekton DAG safety

```
deploy-gaie
  ├── stream-epp-logs  ─────────────────────────────────────────┐
  └── (other setup tasks)                                        │
        └── run-workload                                         │
              └── collect-results  →  writes epp_stream_done ───┘
                                                                 ↓
                                              stream-epp-logs exits
                                                                 ↓
                                    ALL main tasks complete → finally block runs
                                                                 ↓
                                              EPP deployment deleted
```

Tekton's `finally` block only runs after all main tasks complete. Because `stream-epp-logs` is a main task and exits only after reading the sentinel (written by `collect-results`), the EPP pod is guaranteed to still be running until after streaming finishes. No deadlock: `collect-results` does not depend on `stream-epp-logs`.

### Edge cases

| Scenario | Behaviour |
|---|---|
| EPP pod not found after 60 s | Task logs a warning and exits 0 (non-fatal) |
| EPP pod crashes mid-run | `kubectl logs --follow` exits; that pod's stream ends; task continues polling sentinel for remaining pods |
| Sentinel never written (pipeline failure) | Streaming task runs until its Tekton task timeout (pipeline-level `timeout`) |
| Multiple EPP replicas | All pods are streamed; each produces its own files in the shared `epp_logs/` directory |

### Impact on `deploy.py` / results collection

`scripts/deploy.py` collects results from `data-pvc` after the pipeline completes. The new `epp_logs/` directory sits at the phase level alongside workload result directories:

```
<runName>/
  baseline/
    epp_logs/
      <pod>_2325.log
      <pod>_2330.log
    workload_fm3_burst/
      traces.jsonl
    workload_overload/
      traces.jsonl
  treatment/
    epp_logs/
      ...
```

`deploy.py` can enumerate `epp_logs/` and present files sorted by bucket name (lexicographic order = time order).

## Files Changed

| File | Change |
|---|---|
| `tekton/tasks/stream-epp-logs.yaml` | New task |
| `tekton/tasks/collect-results.yaml` | Add `phaseDir` param, remove EPP collection, add sentinel write |
| `tektoncsample/sim2real/pipeline.yaml.j2` | Add `stream-epp-logs` task, pass `phaseDir` to `collect-results` |
