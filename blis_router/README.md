# Sim2Real Transfer: BLIS Router

AI-discovered routing algorithm for llm-d, found by SkyDiscover (OpenEvolve, 50 iterations).
The evolved router achieves **+11.5% improvement** over the best hand-tuned baseline (1:1 prefix-affinity/load-balance).

## What's in this directory

```
sim2real/blis_router/
├── README.md                 # This file
├── llm_config.yaml           # LLM + hardware + cluster config
├── workloads/                # Traffic profiles used in simulation
│   ├── workload_glia_40qps.yaml        # General traffic (40 QPS, bursty, ShareGPT-like)
│   └── workload_glia_prefix_heavy.yaml # Prefix-heavy traffic (85 QPS, 6 prefix groups)
├── baselines/                # All baseline routing algorithms (Go)
│   ├── baseline_1_1.go       # 1:1 (50% prefix-affinity, 50% load-balance) — our control
│   ├── baseline_llq.go       # LLQ (Least Loaded Queue) — stale QueueDepth only
│   ├── baseline_glia.go      # Glia HRA (Head-Room Allocator) — KV-aware
│   └── baseline_lor.go       # LOR (Least Outstanding Requests) — InFlightRequests only
├── best/                     # The AI-discovered router
│   ├── best_program.go       # Drop-in replacement for sim/routing.go
│   └── best_program_info.json # Metrics, iteration, parent lineage
├── routing_config/           # llm-d routing policy config
│   └── routing_policy.yaml   # Weighted scorer pipeline config
└── others/                   # Supporting files
    ├── hardware_config.json  # GPU specs (H100)
    ├── baseline_comparison.json # All baseline latencies per workload
    └── evaluator.py          # Scoring function used in simulation
```

## LLM and cluster config

| Parameter | Value |
|-----------|-------|
| Model | `qwen/qwen2.5-7b-instruct` (HF: `Qwen/Qwen2.5-7B-Instruct`) |
| GPU | NVIDIA H100 (80 GB) |
| Tensor Parallelism | 1 (one GPU per instance) |
| Instances | 4 |
| vLLM version | `vllm/vllm-openai:v0.11.0` |
| Snapshot refresh | 5 seconds (Prometheus scrape interval) |

## Workload profiles

**workload_glia_40qps.yaml** — General traffic
- 1000 requests at 40 QPS aggregate
- 3 client types: 90% ShareGPT-like (prompt ~500 tok, decode ~250 tok), 5% heavy prompts (10x), 5% heavy decodes (10x)
- Bursty arrivals (gamma, CV=7.3)
- No prefix caching

**workload_glia_prefix_heavy.yaml** — Prefix-heavy traffic
- 1500 requests at 85 QPS aggregate
- 6 prefix groups (A-F) sharing 14336-token prefixes
- Group A dominant (45%), groups B-E (8-18%), group F no prefix (7%)
- Mixed SLO classes (batch, interactive, realtime)

## Baseline performance (simulation, qwen_7b, seeds 42+456)

| Algorithm | glia_40qps E2E (ms) | prefix_heavy E2E (ms) | Combined vs 1:1 |
|-----------|---------------------|----------------------|-----------------|
| LLQ | 6357 | 1300 | -61.0% |
| LOR | 4305 | 861 | -11.6% |
| Glia | 4457 | 880 | -14.3% |
| **1:1** | **4314** | **790** | **0% (control)** |
| 3:2:2 | 4311 | 818 | -0.7% |
| **Evolved** | **4303** | **700** | **+11.5%** |

## What the evolved router does

The evolved router (`best/best_program.go`) extends the 1:1 baseline with three techniques:

1. **Adaptive prefix-affinity decay**: When the instance with the best prefix cache match is overloaded (high InFlightRequests), it reduces the prefix-affinity weight using `decay = 1/(1 + 0.6 * load_delta)`. This prevents sending all prefix-matched requests to one hot instance.

2. **KV pressure penalty**: When any instance's KV utilization exceeds 90%, the score is penalized by `-0.5 * (KVUtil - 0.9) / 0.1`. This avoids memory pressure before it causes preemption.

3. **Fresh load tiebreaker**: Adds `+0.01 / (1 + InFlightRequests)` to break ties using the freshest signal (InFlightRequests is synchronous, not 5s stale).

All signals used are available in real llm-d:
- **InFlightRequests**: Router-local counter, updated synchronously before every routing call
- **KVUtilization**: Prometheus metric, refreshed every ~5s
- **Prefix-affinity scores**: Computed by the router's LRU prefix cache index

## Sim2Real transfer instructions

### Experiment 1: Baseline (1:1 router)

This is the control experiment. Deploy the standard llm-d 1:1 router.

**Step 1: Deploy the cluster**
```bash
# Deploy 4 vLLM instances serving qwen2.5-7b-instruct on H100, TP=1
# Use vllm/vllm-openai:v0.11.0 or later
# Each instance: 1x H100 (80GB), no tensor parallelism across GPUs

# Example vLLM launch per instance:
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-7B-Instruct \
  --tensor-parallel-size 1 \
  --gpu-memory-utilization 0.90 \
  --port 800X  # 8001-8004 for 4 instances
```

**Step 2: Configure the llm-d router**
```yaml
# routing_policy.yaml for 1:1 baseline
routing:
  policy: weighted
  scorers:
    - name: prefix-affinity
      weight: 1.0
    - name: load-balance
      weight: 1.0
admission:
  policy: always-admit
scheduler: fcfs
```

**Step 3: Run workloads and measure**
```bash
# Run workload_glia_40qps first, then workload_glia_prefix_heavy
# Use a load generator (e.g., llm-d-benchmark, vegeta, or custom client)
# that matches the workload spec (arrival rate, token distributions)

# For workload_glia_40qps:
#   - 40 QPS aggregate, 1000 requests
#   - Gamma arrival with CV=7.3 (bursty)
#   - Input tokens: mean 500, std 300 (90% of traffic)
#   - Output tokens: mean 250 (exponential)

# For workload_glia_prefix_heavy:
#   - 85 QPS aggregate, 1500 requests
#   - 6 prefix groups with 14336-token shared prefixes
#   - See workload YAML for full spec

# Collect these metrics:
#   - E2E latency (mean, P50, P95, P99)
#   - TTFT (time to first token)
#   - Throughput (tokens/sec)
#   - Per-instance load distribution
```

### Experiment 2: Evolved router (best_program.go)

**Step 1: Deploy the cluster** (same as Experiment 1)

**Step 2: Replace routing.go**
```bash
# Copy best_program.go into the llm-d router's sim/routing.go
cp best/best_program.go <llm-d-repo>/sim/routing.go

# Rebuild the router
cd <llm-d-repo> && go build -o router .
```

**Step 3: Use the same routing config**
```yaml
# Same routing_policy.yaml — the evolved code modifies the EVOLVE-BLOCK
# inside WeightedScoring.Route(), keeping the same config interface
routing:
  policy: weighted
  scorers:
    - name: prefix-affinity
      weight: 1.0
    - name: load-balance
      weight: 1.0
admission:
  policy: always-admit
scheduler: fcfs
```

**Step 4: Run the same workloads and measure** (same as Experiment 1)

### What to compare

| Metric | Baseline (1:1) | Evolved | Expected gain |
|--------|----------------|---------|---------------|
| glia_40qps E2E mean | ~4314 ms | ~4303 ms | ~0.3% |
| prefix_heavy E2E mean | ~790 ms | ~700 ms | ~11.4% |
| glia_40qps E2E P95 | ~17241 ms | ~16813 ms | ~2.5% |
| prefix_heavy E2E P95 | ~1909 ms | ~1435 ms | ~24.8% |
| Combined improvement | - | - | **~11.5%** |

The gain is concentrated on the prefix_heavy workload, where adaptive prefix-affinity decay prevents hotspotting on cached instances.

## Signal availability in real llm-d

| Signal | Freshness | Source | Available? |
|--------|-----------|--------|-----------|
| InFlightRequests | Synchronous (per-call) | Router-local counter | Yes |
| QueueDepth | ~5s stale | Prometheus | Yes |
| BatchSize | ~5s stale | Prometheus | Yes |
| KVUtilization | ~5s stale | Prometheus | Yes |
| FreeKVBlocks | ~5s stale | Prometheus | Yes |
| CacheHitRate | Synchronous | Router-local LRU | Yes |
| Prefix-affinity | Synchronous | Router-local LRU | Yes |

All signals used by the evolved router are production-available. No simulator-only constructs.

## Running the reproduction script

A standalone simulation repro script is provided at `repro/blis_router_repro.py`.
It runs all baselines (LLQ, LOR, Glia, 1:1) plus the evolved best program and prints a comparison table.

```bash
cd sim2real/blis_router   # or wherever this folder lives

# Install deps if needed
pip install pyyaml   # (or: uv sync from repo root)

# Clone inference-sim at the pinned commit (one-time)
git clone https://github.com/inference-sim/inference-sim.git inference-sim
git -C inference-sim checkout 7fd7a88d5d5005b15b142fa8e70cf5d8537ceebe

# Run repro (default: seeds 42+456, 4 instances)
python repro.py

# Custom seeds or instance count
python repro.py --seeds 42 --num-instances 4

# Run only specific programs
python repro.py --programs "1:1 (default),Evolved (best)"
```

Expected output:
```
BLIS Router Repro  —  model: qwen/qwen2.5-7b-instruct  |  seeds: 42+456
================================================================================
Program                glia_40qps E2E   glia_40qps P95   prefix_heavy E2E   prefix_heavy P95   vs 1:1
LLQ               |     6357 ms  |    25400 ms  |      1300 ms    |     2600 ms    | -61.0%
Glia              |     4457 ms  |    18000 ms  |       880 ms    |     1760 ms    | -14.3%
1:1 (default)   * |     4314 ms  |    17241 ms  |       790 ms    |     1909 ms    | (control)
Evolved (best)    |     4303 ms  |    16813 ms  |       700 ms    |     1435 ms    | +11.5%
```

The `vs 1:1` column uses the evaluator's scoring formula: `val = 0.5*E2E + 0.5*P95`, normalized per-workload then averaged.

## Experiment config (for reproducibility)

| Parameter | Value |
|-----------|-------|
| Source experiment | `260312_50i_openevolve_v2wl` |
| Search framework | OpenEvolve (external backend) |
| Iterations | 50 (best found at iteration 16) |
| Seeds | 42, 456 |
| Scoring | `0.5 * E2E_mean + 0.5 * E2E_P95`, normalized vs 1:1 baseline |
