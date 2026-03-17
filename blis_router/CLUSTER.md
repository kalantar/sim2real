# BLIS Cluster Configuration

## Overview

This document captures the exact hardware and workload parameters used during BLIS algorithm
evaluation in the sim2real pipeline. Its purpose is to allow real-cluster validation tests
(Stage 5) to reproduce the same conditions on a live llm-d cluster that were present during
simulation-based evolutionary optimization.

Source files:
- Hardware: `blis_router/llm_config.yaml`, `blis_router/others/hardware_config.json`
- Workloads: `blis_router/workloads/`
- Routing: `blis_router/routing_config/routing_policy.yaml`

---

## Hardware Configuration

The evaluation used 4× NVIDIA H100 80 GiB instances running vLLM, each pod serving one GPU
(tensor-parallel-size=1).

Deploy with the following `llm-d-modelservice` Helm values:

```yaml
modelArtifacts:
  name: Qwen/Qwen2.5-7B-Instruct
  uri: hf://Qwen/Qwen2.5-7B-Instruct
  size: 20Gi
  authSecretName: <hf-token-secret>  # fill in before deploying
  mountPath: /model-cache

accelerator:
  type: nvidia
  dra: false

decode:
  create: true
  replicas: 4
  parallelism:
    tensor: 1
    data: 1
  containers:
    - name: vllm
      image: vllm/vllm-openai:v0.11.0
      modelCommand: vllmServe
      args:
        - "--tensor-parallel-size=1"
        - "--gpu-memory-utilization=0.90"
        - "--block-size=16"
        - "--max-num-seqs=256"
        - "--max-num-batched-tokens=2048"
      resources:
        limits:
          nvidia.com/gpu: "1"
          memory: "80Gi"
        requests:
          nvidia.com/gpu: "1"
          memory: "80Gi"
```

**Node selector:** Pods must be scheduled on H100 nodes. Add a node selector or node affinity
matching your cluster's H100 label (e.g., `nvidia.com/gpu.product: H100-SXM5-80GB`).

### Gaps — fields not derivable from `llm_config.yaml`

The values block above was constructed by translating `llm_config.yaml`, but several fields
had no source there and were either hardcoded or left as placeholders. Any automated
translation pipeline must resolve these separately:

| Field | Value used | Gap / source |
|-------|-----------|--------------|
| `modelArtifacts.size` | `20Gi` | PVC storage size for model weights. Not in `llm_config.yaml`. Must be determined from model file size (~15 GiB for Qwen2.5-7B) plus headroom. |
| `modelArtifacts.authSecretName` | `<hf-token-secret>` | Kubernetes secret name. Operator-supplied; no analog in `llm_config.yaml`. |
| `modelArtifacts.mountPath` | `/model-cache` | Container path where model weights are mounted. Convention only; not in `llm_config.yaml`. |
| `accelerator.dra` | `false` | DRA (Dynamic Resource Allocation) mode. Not captured in `llm_config.yaml`; depends on cluster capabilities. |
| `parallelism.data` | `1` | `llm_config.yaml` has `cluster.num_instances: 4` and `serving.tensor_parallelism: 1`. The mapping to `replicas` vs `parallelism.data` is ambiguous — with TP=1, all 4 instances are independent replicas (`replicas: 4`, `data: 1`), but the translation rule is not explicit. |
| `containers[*].name` | `vllm` | Container name is an llm-d convention; not in `llm_config.yaml`. |
| `containers[*].modelCommand` | `vllmServe` | llm-d-specific command type; no equivalent in `llm_config.yaml`. |
| `resources.memory` (pod limit) | `80Gi` | `hardware.memory_gib: 80.0` in `llm_config.yaml` is GPU VRAM, not container memory. Pod memory limit is a separate Kubernetes concern and will typically be larger than GPU VRAM. The `80Gi` here is a placeholder. |
| `--max-model-len` arg | omitted | `vllm_config.max_model_len: 0` means unlimited (model native); the convention of omitting the flag rather than passing `--max-model-len=0` is implicit and not documented in `llm_config.yaml`. |
| Node selector label | cluster-specific | `hardware.gpu: H100` names the GPU type but does not encode the Kubernetes node label used in the target cluster. |

---

## Workload Characteristics

### Workload 1 — `workload_glia_40qps.yaml` (ShareGPT / Glia baseline)

ShareGPT-like general traffic reproducing the Glia paper workload
(Hamadanian et al., arXiv:2510.27176, Section 5). No prefix caching; all requests are
interactive SLO. Arrivals are highly bursty across all client groups.

| Parameter | Value |
|-----------|-------|
| Aggregate rate | 40 QPS |
| Total requests | 1,000 |
| Simulated duration | ~133 s |
| Seed | 42 |

**Client groups:**

| Group | Share | SLO class | Arrival | Prompt (tokens) | Decode (tokens) |
|-------|-------|-----------|---------|-----------------|-----------------|
| sharegpt-main | 90% | interactive | Gamma CV=7.3 | Gaussian μ=500, σ=300 [10–2000] | Exponential μ=250 |
| heavy-prompt | 5% | interactive | Gamma CV=7.3 | Gaussian μ=5000, σ=2000 [1000–15000] | Exponential μ=250 |
| heavy-decode | 5% | interactive | Gamma CV=7.3 | Gaussian μ=500, σ=300 [10–2000] | Exponential μ=2500 |

Heavy-tail groups inflate the tail of the main distribution by ~10× (prompts and decodes
respectively), matching the Glia paper's Section 5 heavy-tail construction. No prefix groups.

### Workload 2 — `workload_glia_prefix_heavy.yaml` (Prefix-optimized traffic)

Six prefix groups designed to stress prefix-affinity routing. Groups A–E share 14,336-token
cached prefixes; Group F has no prefix. The dominant group (A) is bursty batch traffic;
Groups B–D are interactive; Groups E–F are realtime.

| Parameter | Value |
|-----------|-------|
| Aggregate rate | 85 QPS |
| Total requests | 1,500 |
| Seed | 42 |

**Client groups:**

| Group | ID | Share | SLO class | Arrival | Prefix (tokens) | Input μ±σ [min–max] | Output μ±σ [min–max] |
|-------|----|-------|-----------|---------|-----------------|---------------------|----------------------|
| A | dominant-batch | 45% | batch | Gamma CV=6.0 | 14,336 | 200±80 [25–500] | 100±45 [10–300] |
| B | secondary-interactive | 18% | interactive | Gamma CV=4.0 | 14,336 | 140±55 [15–380] | 70±30 [10–220] |
| C | group-c | 12% | interactive | Gamma CV=4.0 | 14,336 | 110±45 [10–320] | 60±28 [8–200] |
| D | group-d | 10% | interactive | Gamma CV=4.0 | 14,336 | 120±50 [10–330] | 55±25 [8–180] |
| E | realtime-e | 8% | realtime | Poisson | 14,336 | 80±30 [10–200] | 35±18 [5–110] |
| F | no-prefix-f | 7% | realtime | Gamma CV=5.0 | none | 90±35 [10–230] | 45±22 [5–140] |

All input/output distributions are Gaussian (tokens). Prefix lengths are exact token counts
shared across all requests within a group, enabling radix-cache hits when requests land on the
same pod.

---

## Routing Policy

Reference: `blis_router/routing_config/routing_policy.yaml`

```yaml
admission:
  policy: always-admit
priority:
  policy: constant
routing:
  policy: weighted
  scorers:
  - name: prefix-affinity
    weight: 1.0
  - name: load-balance
    weight: 1.0
scheduler: fcfs
```

The 1:1 scorer weight ratio (prefix-affinity : load-balance = 1.0 : 1.0) is the **baseline**
against which the BLIS-evolved router is compared. Real-cluster tests should use the same
baseline configuration as the control condition, then substitute the evolved scorer weights
for the experimental condition.

---

## KV Cache Notes

- **Total KV blocks:** 67,659
- **Block size:** 16 tokens/block
- **Effective KV capacity:** ~1,082,544 tokens per instance

These values were measured from real vLLM running on H100 TP=1 with the parameters above
(`--gpu-memory-utilization=0.90 --block-size=16`). When setting up the live cluster, verify
that vLLM reports the same block count at startup. A significant discrepancy indicates a
hardware or configuration mismatch (different GPU memory, different vLLM version, different
TP degree) that would invalidate the sim→real equivalence assumption.

Expected vLLM startup log line:
```
# of GPU blocks: 67659, # of CPU blocks: ...
```

---

## Deployment Checklist

Before running real-cluster validation tests, verify:

- [ ] **HF token secret** — Kubernetes secret named per `authSecretName` exists in the target
      namespace with a valid Hugging Face token that can pull `Qwen/Qwen2.5-7B-Instruct`
- [ ] **Node labels** — 4 nodes with H100 80 GiB GPUs are available and labeled so the node
      selector matches; confirm `kubectl get nodes -l <your-h100-label>`
- [ ] **KV block count** — vLLM startup logs confirm 67,659 GPU blocks per pod; if not, check
      GPU type, memory-utilization flag, and vLLM version
- [ ] **Prometheus scrape interval** — set to 5 s to match the simulator's
      `snapshot_refresh_interval_us: 5000000`; this is the cadence at which the scheduler
      observes load state
- [ ] **vLLM version** — image tag `vllm/vllm-openai:v0.11.0` matches the version used during
      BLIS evaluation; do not substitute a newer version without re-validating latency model fit
- [ ] **Routing scorer plugins** — `prefix-affinity` and `load-balance` scorer plugins are
      registered in the llm-d-inference-scheduler and report metrics to Prometheus
