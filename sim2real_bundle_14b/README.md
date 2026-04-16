# Sim2Real Bundle: Preemptive Shed Admitter (Qwen3-14B)

Simulation-evolved admission control that improves SLO attainment for protected requests by **+47-49pp** under overload. Transferable to llm-d/GAIE as an `AdmissionPlugin`.

## Algorithm: Adaptive Admission (Treatment)

**Source**: [`algorithm/admission.go`](algorithm/admission.go)

1. Compute **cluster saturation**: `avg(max(QD/5.0, KV/0.8))` across all instances
2. **Critical/standard** (priority >= 0): always admit
3. **Sheddable** (priority -50): most aggressive — ramp [0.005, 0.05]
4. **Batch** (priority -10): less aggressive — ramp [0.01, 0.10]

**Key insight**: GAIE legacy waits until saturation=1.0 to shed — by then queues are deep and latency is ruined. This algorithm starts at near-zero saturation. The *timing* of shedding matters more than the total amount shed.

**Nil metrics handling (startup edge case)**: When a pod first starts and hasn't reported metrics yet (~200-500ms), treat it as fully loaded — matching GAIE's conservative default. In `computeSaturation`, if metrics are nil, add `1.0` to the total instead of skipping the pod. Without this, nil-metric pods pull the saturation average down, causing under-shedding during startup.

## Algorithm: GAIE Control

**Source**: [`algorithm/admission_control.go`](algorithm/admission_control.go)

Same saturation formula, same plugin interface, but uses the standard GAIE binary shedding rule:

1. Compute **cluster saturation**: `avg(max(QD/5.0, KV/0.8))` across all instances
2. **Critical/standard** (priority >= 0): always admit
3. **All negative priority** (sheddable, batch, background): reject when saturation >= 1.0

No probabilistic ramp — hard cutoff at saturation=1.0. Empty pods → saturation=1.0 (conservative).

Purpose: deploy alongside the adaptive algorithm to isolate the effect of preemptive shedding vs the plugin framework itself.

## Config

BLIS simulation calibrated to real vLLM on 4x H100-SXM-80GB (~73 req/s). See [`config.md`](config.md) for details.

## How to Transfer to llm-d/GAIE

Use this section to create Go plugins and YAML configs for llm-d. Both algorithms implement the same `AdmissionPlugin` interface and read the same signals.

### GAIE Interface

Implement `requestcontrol.AdmissionPlugin` (`pkg/epp/framework/interface/requestcontrol/plugins.go:78`). Return `nil` to admit, `error` to reject.

### Signal Mapping

| Signal | GAIE accessor |
|--------|--------------|
| Queue depth | `pod.GetMetrics().WaitingQueueSize` |
| KV utilization (0-1) | `pod.GetMetrics().KVCacheUsagePercent` (already 0-1 despite the field name) |
| Priority | `request.Objectives.Priority` |

### Priority Tiers (InferenceModel CRs)

| Tier | Priority | Adaptive shedding | GAIE control shedding |
|------|----------|-------------------|----------------------|
| critical | 100 | Never | Never |
| standard | 0 | Never | Never |
| batch | -10 | Less aggressive — ramp [0.01, 0.10] | Reject at saturation >= 1.0 |
| sheddable | -50 | Most aggressive — ramp [0.005, 0.05] | Reject at saturation >= 1.0 |

### Transfer: Adaptive Admission (Treatment)

**Pseudocode:**

```
function AdmitRequest(request, pods):
    priority = request.Objectives.Priority

    if priority >= 0:       return ADMIT   // critical, standard
    if len(pods) == 0:      return ADMIT   // safe default

    // Cluster saturation (GAIE formula)
    saturation = 0
    for each pod in pods:
        m = pod.GetMetrics()
        if m == nil:
            saturation += 1.0          // no metrics yet → treat as fully loaded
            continue
        qRatio  = m.WaitingQueueSize / 5.0
        kvRatio = m.KVCacheUsagePercent / 0.8
        saturation += max(qRatio, kvRatio)
    saturation /= len(pods)

    // Shedding ramp by priority
    if priority == -50:        // sheddable: MOST aggressive
        shedStart, shedFull = 0.005, 0.05
    else if priority == -10:   // batch: LESS aggressive
        shedStart, shedFull = 0.01, 0.10

    if saturation < shedStart: return ADMIT

    prob = min((saturation - shedStart) / (shedFull - shedStart), 1.0)
    if rand() < prob:          return REJECT
    return ADMIT
```

**YAML Config:**

```yaml
apiVersion: inference.networking.x-k8s.io/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: preemptive-shed-admitter
  name: preemptive-shed
  parameters:
    queueDepthThreshold: 5
    kvCacheUtilThreshold: 0.8
    sheddableShedStart: 0.005    # sheddable (priority=-50): most aggressive
    sheddableShedFull: 0.05
    batchShedStart: 0.01         # batch (priority=-10): less aggressive
    batchShedFull: 0.10
- type: random-picker
saturationDetector:
  queueDepthThreshold: 999999999
  kvCacheUtilThreshold: 0.999
schedulingProfiles:
- name: default
  plugins:
  - pluginRef: random-picker
```

Ensure the Go plugin applies `sheddableShed*` params to sheddable requests (priority=-50) and `batchShed*` params to batch requests (priority=-10).

### Transfer: GAIE Control

**Pseudocode:**

```
function AdmitRequest(request, pods):
    priority = request.Objectives.Priority

    if priority >= 0:       return ADMIT   // critical, standard
    if len(pods) == 0:      return ADMIT   // safe default

    // Cluster saturation (same GAIE formula)
    saturation = 0
    for each pod in pods:
        m = pod.GetMetrics()
        if m == nil:
            saturation += 1.0          // no metrics yet → treat as fully loaded
            continue
        qRatio  = m.WaitingQueueSize / 5.0
        kvRatio = m.KVCacheUsagePercent / 0.8
        saturation += max(qRatio, kvRatio)
    saturation /= len(pods)

    if saturation >= 1.0:      return REJECT   // binary cutoff
    return ADMIT
```

**YAML Config:**

```yaml
apiVersion: inference.networking.x-k8s.io/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: gaie-control-admitter
  name: gaie-control
  parameters:
    queueDepthThreshold: 5
    kvCacheUtilThreshold: 0.8
- type: random-picker
saturationDetector:
  queueDepthThreshold: 999999999
  kvCacheUtilThreshold: 0.999
schedulingProfiles:
- name: default
  plugins:
  - pluginRef: random-picker
```

### Disabling GAIE Legacy Admission (Required for Treatment and Control)

GAIE has a **two-layer admission pipeline**. Both layers run on every request:

1. **Layer 1 (built-in, always active)**: `LegacyAdmissionController` — rejects sheddable requests (priority < 0) when saturation >= 1.0. Runs at `director.go:168` *before* any custom plugins. Cannot be disabled via config.
2. **Layer 2 (plugin-based)**: Custom `AdmissionPlugin` instances registered in YAML — run at `director.go:189` *after* Layer 1 passes.

**Problem**: For variants 2 (GAIE Control) and 3 (Adaptive), we want our custom plugin to be the **sole** admission decision-maker. If Layer 1 is also active, both run — our plugin's decisions are masked by the legacy controller at saturation >= 1.0.

**Solution**: Set the legacy saturation detector thresholds astronomically high via inline `saturationDetector` config so it never triggers. Add this to the YAML config for both treatment and control variants:

```yaml
saturationDetector:
  queueDepthThreshold: 999999999   # effectively infinite — legacy never triggers on QD
  kvCacheUtilThreshold: 0.999      # near-max (1.0 is boundary of GAIE validation)
```

This makes `Saturation() ≈ 0.0` always, so `LegacyAdmissionController` at `admission.go:64-84` never enters the rejection path:

```go
// admission.go:64-84 — with saturation ≈ 0.0, this never rejects:
func rejectIfSheddableAndSaturated(...) error {
    if requtil.IsSheddable(priority) {
        if sd.Saturation(ctx, ...) >= 1.0 {   // 0.0 < 1.0 → skip
            return errcommon.Error{...}
        }
    }
    return nil  // always reaches here
}
```

**Variant 1 (Default llm-d)** does NOT need this — it uses the built-in legacy admission as-is, which is exactly what we're comparing against.

### Load Generator Concurrency (Important)

BLIS simulation is true open-loop — all requests arrive at their scheduled times with no concurrency limit. To match this in real deployment, `blis observe` must use a high `--max-concurrency` so the client-side semaphore never blocks:

```bash
blis observe --max-concurrency 3000 ...
```

The default (`--max-concurrency 256`) is too low for overload experiments. At 110 QPS with ~7s average E2E, Little's law requires ~770 concurrent slots. With 256, requests queue client-side, the server only sees ~34 QPS (well within capacity), and the admission algorithm has nothing to shed. This makes baseline look artificially good and treatment look worse — the opposite of the simulation prediction.

Sizing: worst-case concurrent = QPS × p99 E2E. At 140 QPS × 20s = 2,800. 3000 covers all workloads with headroom. Resource cost is negligible (goroutines + TCP sockets; pod ulimit is ~1M).

### Deployment Plan

Three variants to deploy and compare:

1. **Default llm-d** — built-in GAIE legacy admission (no custom plugin, no saturation override)
2. **GAIE Control** (`admission_control.go` → `gaie-control-admitter` plugin) — same GAIE logic via custom plugin, with legacy admission disabled via noop saturation detector
3. **Adaptive** (`admission.go` → `preemptive-shed-admitter` plugin) — preemptive probabilistic shedding, with legacy admission disabled via noop saturation detector

Comparing (1) vs (2) isolates plugin framework overhead. Comparing (2) vs (3) isolates the algorithm improvement.

All three variants use `random-picker` with no scorers — purely random endpoint selection. The admission algorithm is the only variable.

### Key GAIE Source Files

- `AdmissionPlugin` interface: `pkg/epp/framework/interface/requestcontrol/plugins.go:78`
- Saturation formula: `pkg/epp/framework/plugins/flowcontrol/saturationdetector/utilization/detector.go`
- Endpoint metrics: `pkg/epp/framework/interface/datalayer/metrics.go`
- Request priority: `pkg/epp/framework/interface/scheduling/types.go:40`
- Plugin registry: `llm-d-inference-scheduler/pkg/plugins/register.go`

## Simulation Results

### W1: Sustained Overload (rate=110, ~1.5x capacity)

| Tier | GAIE SLO<10s | Adaptive SLO<10s | **Gain** | GAIE Mean E2E | Adaptive Mean E2E |
|------|-------------|-----------------|---------|--------------|------------------|
| critical | 52.0% | 99.8% | **+47.8pp** | 9,897ms | 5,814ms |
| standard | 51.2% | 99.8% | **+48.5pp** | 9,899ms | 5,815ms |
| sheddable | 53.2% | 100% | +46.8pp | 9,747ms | 3,450ms |
| batch | 53.3% | 100% | +46.7pp | 9,768ms | 4,113ms |

### W3: High Sheddable Fraction (rate=210, ~2.9x capacity)

| Tier | GAIE SLO<10s | Adaptive SLO<10s | **Gain** | GAIE Mean E2E | Adaptive Mean E2E |
|------|-------------|-----------------|---------|--------------|------------------|
| critical | 50.9% | 97.6% | **+46.6pp** | 9,924ms | 6,574ms |
| standard | 51.0% | 97.8% | **+46.8pp** | 9,926ms | 6,568ms |
| sheddable | 52.5% | 100% | +47.5pp | 9,854ms | 4,031ms |
| batch | 56.1% | 100% | +43.9pp | 9,618ms | 4,244ms |
