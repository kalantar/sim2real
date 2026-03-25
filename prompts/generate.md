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
test -f workspace/algorithm_summary.json || { echo "HALT: missing algorithm_summary.json"; exit 1; }
.venv/bin/python tools/transfer_cli.py validate-schema workspace/algorithm_summary.json || { echo "HALT: algorithm_summary.json schema validation failed"; exit 1; }
.venv/bin/python -c "import json,sys; d=json.load(open('workspace/algorithm_summary.json')); sys.exit(0 if d.get('scope_validation_passed') is True else 1)" || { echo "HALT: algorithm_summary.json scope_validation_passed is not true"; exit 1; }

# signal_coverage.json: exists + schema valid + coverage complete
test -f workspace/signal_coverage.json || { echo "HALT: missing signal_coverage.json"; exit 1; }
.venv/bin/python tools/transfer_cli.py validate-schema workspace/signal_coverage.json || { echo "HALT: signal_coverage.json schema validation failed"; exit 1; }
.venv/bin/python -c "import json,sys; d=json.load(open('workspace/signal_coverage.json')); sys.exit(0 if d.get('coverage_complete') is True and len(d.get('unmapped_signals',[])) == 0 else 1)" || { echo "HALT: signal_coverage.json coverage_complete is not true or has unmapped signals"; exit 1; }

# Submodule staleness check: signal_coverage.json commit_hash must match HEAD
.venv/bin/python -c "
import json, subprocess, sys
d = json.load(open('workspace/signal_coverage.json'))
head = subprocess.check_output(['git', '-C', 'llm-d-inference-scheduler', 'rev-parse', 'HEAD']).decode().strip()
match = head.startswith(d['commit_hash']) or d['commit_hash'].startswith(head)
sys.exit(0 if match else 1)
" || { echo "HALT: signal_coverage.json commit_hash does not match llm-d-inference-scheduler HEAD"; exit 1; }

# Scorer template and mapping artifact
test -f docs/transfer/scorer_template.go.md || { echo "HALT: missing scorer template"; exit 1; }
test -f docs/transfer/blis_to_llmd_mapping.md || { echo "HALT: missing mapping artifact"; exit 1; }
```

On HALT, write `workspace/escalation.json` with the appropriate `halt_reason`:
- Missing signal_coverage.json → `"missing_signal_coverage"`
- Commit hash mismatch → `"stale_signal_coverage"`

## Stale Artifact Guard

Delete any existing output artifacts to prevent stale files from a prior run.

```bash
rm -f workspace/stage3_output.json
rm -f workspace/tekton/algorithm_values.yaml
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

**Skip this step entirely if `CacheHitRate` is not present in `workspace/signal_coverage.json` `signals[]` at all.** The current blis_router algorithm does not use CacheHitRate; generating dead `cacheHitRate` code when the signal is absent would add unreachable code and could mask missing-signal detection bugs. Always read the artifact to determine this — do not assume based on prior runs.

Check `workspace/signal_coverage.json` for the CacheHitRate signal:

- **If absent (not in `signals[]` at all):** skip this step entirely — do not emit any CacheHitRate code.
- **If mapped** (has a `prod_access_path`): use that path in generated code.
- **If unmapped or `prod_access_path: "UNVERIFIED"`**: emit `// CacheHitRate: production access path unavailable — using zero fallback` and assign `cacheHitRate = 0.0`.
- **HALT if CacheHitRate is used as a multiplier** in the EVOLVE-BLOCK scoring logic (a zero fallback would zero out the entire score). Use `halt_reason: "cache_hit_rate_unavailable_stage3"`.

## Step 5: F-10 Double-Counting Guard

**Skip this step entirely if `composite_signals` in `workspace/algorithm_summary.json` is empty.** An empty `composite_signals: []` means the EVOLVE-BLOCK contains no composite method calls (e.g., no `EffectiveLoad()`), so there is no composite to check for double-counting. Always read the artifact to determine this — do not assume based on prior runs.

Check if any two signals in the EffectiveLoad composite share the same `prod_access_path`:

- **If double-counting detected**: use `RunningRequestsSize` once with an adjusted coefficient (both composite inputs map to this single production field — keep the combined weight but emit the field access only once).
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

## Step 8: Generate tekton artifacts

After writing `workspace/stage3_output.json`, generate the Tekton benchmarking artifacts for Stage 5.

### Part A: Prerequisites and inference-sim tag

**Prerequisites check:**
```bash
.venv/bin/python tools/transfer_cli.py validate-schema workspace/algorithm_summary.json
.venv/bin/python tools/transfer_cli.py validate-schema workspace/signal_coverage.json
```
**HALT if either exits non-zero.**

**Resolve inference-sim image tag:**
```bash
BLIS_IMAGE_TAG=$(cd inference-sim && git describe --tags 2>/dev/null)
if [ -z "$BLIS_IMAGE_TAG" ]; then
  echo "HALT: git describe --tags returned empty — no tags on inference-sim submodule"; exit 1
fi
if echo "$BLIS_IMAGE_TAG" | grep -qE -- '-g[0-9a-f]+$'; then
  echo "HALT: '$BLIS_IMAGE_TAG' is an un-tagged commit (contains -g suffix). Bump the submodule to a release tag first."; exit 1
fi
if ! echo "$BLIS_IMAGE_TAG" | grep -qE '^v?[0-9]+\.[0-9]+'; then
  echo "HALT: '$BLIS_IMAGE_TAG' does not match expected release-tag pattern v?N.N"; exit 1
fi
echo "Resolved tag: $BLIS_IMAGE_TAG"
```
Record `$BLIS_IMAGE_TAG` for use in `observe.image` below.

### Part B: Read env_defaults

**Read `config/env_defaults.yaml` to understand the current infrastructure configuration.**

The file contains the stable infrastructure defaults that don't change between algorithm runs: gateway type, connection pool settings, baseline scorer config, model deployment constants. Read it before generating `algorithm_values.yaml` so you understand:
- Which gateway provider is configured (`stack.gateway.helmValues.gateway.provider`)
- The treatment scorer type naming convention to use in `stack.gaie.treatment`

If the user specified an override at prompt time (e.g., "use kgateway instead of istio"), apply the override by editing `config/env_defaults.yaml` before proceeding. This persists the override for future runs.

### Part C: Generate algorithm_values.yaml

**Generate `workspace/tekton/algorithm_values.yaml`** containing only the BLIS-derived values.

Using `blis_router/llm_config.yaml`, `blis_router/CLUSTER.md`, and `workspace/stage3_output.json` (for `scorer_type`), generate ONLY the BLIS-derived values. The output must match the `algorithm_values.schema.json` schema.

Key translation rules from `blis_router/llm_config.yaml`:
- `stack.model.modelName`: model HF repo ID (e.g. `Qwen/Qwen2.5-7B-Instruct`)
- `stack.model.helmValues.modelArtifacts.name`: same model ID
- `stack.model.helmValues.modelArtifacts.uri`: `pvc://model-pvc/models/<sanitized-model-name>`
  (sanitize: lowercase, replace `/` with `-`, keep alphanumeric/hyphens/dots, strip everything else;
  e.g. `Qwen/Qwen2.5-7B-Instruct` → `qwen-qwen2.5-7b-instruct`)
- `stack.model.helmValues.decode.replicas`: `cluster.num_instances`
- `stack.model.helmValues.decode.parallelism.tensor`: `serving.tensor_parallel_size`
- `stack.model.helmValues.decode.containers[0].modelCommand`: `vllmServe`
  *(Required — specifying the `containers` array in values replaces the chart default entirely, dropping
  `modelCommand`. Without it the Helm chart requires an explicit `command`, causing a render error:
  "When .container.modelCommand not set or `custom`, a `command` is required.")*
- `stack.model.helmValues.decode.containers[0].mountModelVolume`: `true`
  *(Required — same reason: specifying the `containers` array replaces the chart default entirely.
  Omitting this field causes the model PVC to not be mounted in the vllm pod,
  resulting in a startup crash when vllm cannot find the model path.)*
- `stack.model.helmValues.decode.containers[0].image`: `vllm/vllm-openai:<serving.vllm_version>`
  *(Note: `merge-values` will replace this with `stack.model.vllm_image` from `config/env_defaults.yaml`
  if that field is set — e.g. to substitute a llm-d custom vLLM build. Record the original sim image
  here regardless; the override applies at merge time.)*
- `stack.model.helmValues.decode.containers[0].extraConfig.vllm.gpuMemoryUtilization`: `serving.gpu_memory_utilization`
- `stack.model.helmValues.decode.containers[0].extraConfig.vllm.maxNumSeqs`: `vllm_config.max_num_running_reqs`
- `stack.model.helmValues.decode.containers[0].extraConfig.vllm.maxNumBatchedTokens`: `vllm_config.max_num_scheduled_tokens`
- `stack.model.helmValues.decode.containers[0].extraConfig.startupProbe`,
  `stack.model.helmValues.decode.containers[0].extraConfig.livenessProbe`,
  `stack.model.helmValues.decode.containers[0].extraConfig.readinessProbe`: always include the
  following probes. The probe port depends on `routing.proxy.enabled` in the Helm chart:
  - **Port 8200** (default) — when `routing.proxy` is enabled (chart default), vLLM listens on
    `routing.proxy.targetPort` (default 8200) and the proxy fronts it on `routing.servicePort` (8000).
  - **Port 8000** — when proxy is disabled, vLLM listens on `routing.servicePort` directly.
  Since `env_defaults.yaml` does not disable the proxy, use **port 8200**:
  ```yaml
  extraConfig:
    startupProbe:
      httpGet:
        path: /health
        port: 8200
      failureThreshold: 60
      initialDelaySeconds: 30
      periodSeconds: 30
      timeoutSeconds: 5
    livenessProbe:
      tcpSocket:
        port: 8200
      failureThreshold: 3
      periodSeconds: 5
    readinessProbe:
      httpGet:
        path: /health
        port: 8200
      failureThreshold: 3
      periodSeconds: 5
  ```
  *(Required — same list-replacement issue as `modelCommand`: these must be explicit in
  `algorithm_values.yaml` or they will be absent from the merged output. Tracked in issue #18.)*
- `stack.model.helmValues.decode.acceleratorTypes.labelValues`: derive from `hardware.gpu` in
  `blis_router/llm_config.yaml` and the node selector label documented in `blis_router/CLUSTER.md`
  (e.g. `hardware.gpu: H100` + CLUSTER.md node selector → `["NVIDIA-H100-80GB-HBM3"]`)
- `stack.gaie.treatment.helmValues.inferenceExtension.pluginsCustomConfig`: use `scorer_type` from
  `workspace/stage3_output.json` in the EndpointPickerConfig:
  ```yaml
  gaie:
    treatment:
      helmValues:
        inferenceExtension:
          pluginsCustomConfig:
            custom-plugins.yaml: |
              apiVersion: inference.networking.k8s.io/v1alpha1
              kind: EndpointPickerConfig
              plugins:
              - type: <scorer_type from stage3_output.json>
              <...full treatment EndpointPickerConfig from Stage 3 scorer generation...>
  ```
  **Encoding note:** `pluginsCustomConfig` values are YAML block scalars (`|`) — no `\n`
  escaping required. The pipeline templates use `{{ stack.gaie.<phase>.helmValues | tojson }}`
  which serializes the full nested structure to JSON, correctly preserving multiline values.
- `observe.image`: `"ghcr.io/inference-sim/blis:$BLIS_IMAGE_TAG"` (tag resolved in Part A)
- `observe.workloads`: one entry per `blis_router/workloads/workload_*.yaml` file.
  Each entry **must** use the field name `spec:` (not `content:` or any other name) —
  the pipeline templates access `workload.spec` directly:
  ```yaml
  workloads:
    - name: <workload-name>
      source: blis_router/workloads/workload_<name>.yaml
      spec: |
        <full file contents verbatim>
  ```

**Note:** `num_requests` in workload specs is scaled by `observe.request_multiplier` (from `config/env_defaults.yaml`) during the merge step. Embed the original simulation values verbatim; do not pre-scale them.

Do NOT include in `algorithm_values.yaml`: gateway config, connection pool settings,
baseline scorer config, `routing.servicePort`, `prefill.create`, `modelArtifacts.authSecretName`,
`decode.acceleratorTypes.labelKey`, `observe.noise_runs`. These come from `config/env_defaults.yaml`
and are merged in Part D.

### Part D: Validate and merge

**Validate algorithm_values.yaml:**
```bash
# halt_reason: algorithm_values_validation_failure_stage3
.venv/bin/python tools/transfer_cli.py validate-schema workspace/tekton/algorithm_values.yaml \
  || { echo "HALT: algorithm_values.yaml schema validation failed"; exit 1; }
```

**Merge to produce values.yaml:**
```bash
# halt_reason: merge_values_failure_stage3
.venv/bin/python tools/transfer_cli.py merge-values \
  --env config/env_defaults.yaml \
  --algorithm workspace/tekton/algorithm_values.yaml \
  --out workspace/tekton/values.yaml \
  || { echo "HALT: merge-values failed"; exit 1; }
```

### Part E: Validate merged values.yaml

**Validate generated values.yaml required keys:**
```bash
.venv/bin/python -c "
import yaml
v = yaml.safe_load(open('workspace/tekton/values.yaml'))
required = ['stack', 'observe']
for k in required:
    assert k in v, f'missing key: {k}'
assert 'image' in v['observe'], 'missing observe.image'
assert '<TAG>' not in v['observe']['image'], 'unresolved <TAG> in observe.image'
assert v['observe'].get('noise_runs'), 'missing observe.noise_runs'
wl = v['observe'].get('workloads', [])
assert len(wl) > 0, 'observe.workloads must be non-empty (no workload files found in blis_router/workloads/)'
assert v['stack'].get('model', {}).get('modelName'), 'missing stack.model.modelName'
# gateway defaults
gw = v['stack'].get('gateway', {}).get('helmValues', {}).get('gateway', {})
assert gw.get('provider'), 'missing stack.gateway.helmValues.gateway.provider'
assert gw.get('gatewayClassName'), 'missing stack.gateway.helmValues.gateway.gatewayClassName'
# gaie helmValues with pluginsCustomConfig
for phase in ('baseline', 'treatment'):
    hv = v['stack'].get('gaie', {}).get(phase, {}).get('helmValues', {})
    assert hv, f'missing stack.gaie.{phase}.helmValues'
    pcc = hv.get('inferenceExtension', {}).get('pluginsCustomConfig', {})
    assert pcc, f'missing stack.gaie.{phase}.helmValues.inferenceExtension.pluginsCustomConfig'
print('OK')
"
```
**HALT if any assertion fails.**

**Generate PipelineRun stubs** (`workspace/tekton/pipelinerun-{noise,baseline,treatment}.yaml`):

Each stub must include:
```yaml
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  name: $PIPELINERUN_NAME
  namespace: $NAMESPACE
  labels:
    sim2real-phase: $PHASE
spec:
  pipelineRef:
    name: sim2real-<phase>
  taskRunTemplate:
    serviceAccountName: helm-installer   # required: deploy-model/delete-model tasks need Helm RBAC
  params:
    - name: experimentId
      value: $PIPELINERUN_NAME
    - name: namespace
      value: $NAMESPACE
  workspaces:
    - name: model-cache
      persistentVolumeClaim:
        claimName: model-pvc
    - name: hf-credentials
      secret:
        secretName: hf-secret
    - name: data-storage
      persistentVolumeClaim:
        claimName: data-pvc
```

**Update `workspace/stage3_output.json`** to include `tekton_artifacts` key:
```json
{
  "tekton_artifacts": {
    "values_yaml": "workspace/tekton/values.yaml",
    "pipeline_stubs": [
      "workspace/tekton/pipelinerun-noise.yaml",
      "workspace/tekton/pipelinerun-baseline.yaml",
      "workspace/tekton/pipelinerun-treatment.yaml"
    ]
  }
}
```

Note: `stage3_output.schema.json` defines `tekton_artifacts` with `additionalProperties: false` and only allows `values_yaml` (string) and `pipeline_stubs` (array). Individual keys per phase are not permitted — use `pipeline_stubs` array. Stage 5 reads the stub paths from this array in phase order (noise, baseline, treatment). Within `tekton_artifacts`, `values_yaml` is required; `pipeline_stubs` is optional per the schema, though Stage 5 expects it to be present.

**Validate stage3_output.json after updating:**
```bash
.venv/bin/python tools/transfer_cli.py validate-schema workspace/stage3_output.json \
  || { echo "HALT: stage3_output.json failed schema validation after tekton_artifacts update"; exit 1; }
```

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
| inference-sim tag unresolved | `inference_sim_tag_unresolved_stage3` | HALT (Step 8): write `workspace/escalation.json` with `"stage": 3` and this halt_reason |
| tekton_artifacts schema validation | `tekton_artifacts_validation_failure_stage3` | HALT (Step 8): write `workspace/escalation.json` with `"stage": 3` and this halt_reason |
| algorithm_values.yaml schema validation fails | `algorithm_values_validation_failure_stage3` | HALT (Step 8): write `workspace/escalation.json` with `"stage": 3` and this halt_reason |
| merge-values fails | `merge_values_failure_stage3` | HALT (Step 8): write `workspace/escalation.json` with `"stage": 3` and this halt_reason |

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
  - `tekton_artifacts`: `values_yaml` path and `pipeline_stubs` array
- `workspace/tekton/algorithm_values.yaml` — BLIS-derived Tekton values (generated by Step 8)
- `workspace/tekton/values.yaml` — merged Tekton pipeline values (env_defaults + algorithm_values)
- `workspace/tekton/pipelinerun-{noise,baseline,treatment}.yaml` — PipelineRun stubs
