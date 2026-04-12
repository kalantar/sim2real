# BLIS: Simulating llm-d-inference-scheduler and GAIE

## Overview

BLIS (Blackbox Inference Simulator) is a discrete-event simulator for LLM inference serving systems. It models the core control plane logic of **llm-d-inference-scheduler** (the production request routing and scheduling subsystem) and **GAIE** (Gateway API Inference Extension), a Kubernetes standard for flow control at the ingress layer.

This document describes how BLIS simulates these systems and the key abstractions that bridge evolved simulation policies to production implementations.

---

## Part 1: Architecture and Core Concepts

### Discrete-Event Simulation (DES)

BLIS uses a shared-clock event loop that orchestrates multiple replica instances:

```
┌─────────────────────────────────────────────────────────┐
│  ClusterSimulator (shared-clock event queue)            │
│                                                         │
│  Event Loop:                                            │
│  while (clusterEvents or instanceEvents):               │
│    • Pop earliest event (by timestamp, priority, seqID) │
│    • Advance global clock                               │
│    • Execute event handlers                             │
└─────────────────────────────────────────────────────────┘
        ↓                                    ↓
    ┌───────────┐  ┌───────────┐  ┌───────────┐
    │ Instance0 │  │ Instance1 │  │ Instance2 │  ...
    │ Simulator │  │ Simulator │  │ Simulator │
    └───────────┘  └───────────┘  └───────────┘
```

**Determinism:** All random decisions use a partitioned RNG seeded at simulation start. The same seed produces byte-identical outputs across runs.

**Timestamps:** Events have three-level ordering: (Timestamp, Priority, SequenceID) to ensure deterministic processing:
- Same-timestamp events at different pipeline stages process in priority order
- Ties broken by sequence ID for deterministic FIFO ordering within a stage

---

## Part 2: Request Pipeline (Simulating llm-d Routing)

### Stage 1: Cluster Arrival

**Event:** `ClusterArrivalEvent` (Priority 0)

When a request enters the cluster control plane, a `ClusterArrivalEvent` is scheduled at the request's arrival time. This event models a request hitting the **GAIE gateway or load balancer entry point**.

```go
// From inference-sim/sim/cluster/cluster_event.go
type ClusterArrivalEvent struct {
    time    int64        // arrival time
    request *sim.Request // request metadata
}

// Execute schedules admission decision
func (e *ClusterArrivalEvent) Execute(cs *ClusterSimulator) {
    heap.Push(&cs.clusterEvents, &AdmissionDecisionEvent{
        time:    e.time + cs.admissionLatency,
        request: e.request,
    })
}
```

**What it simulates:** Network/processing latency between the external client and the cluster control plane. The `--admission-latency` flag models this delay.

---

### Stage 2: Admission Decision

**Event:** `AdmissionDecisionEvent` (Priority 1)

The admission policy decides whether to accept or reject the request. This stage models **llm-d's admission control subsystem**.

```go
type AdmissionDecisionEvent struct {
    time    int64
    request *sim.Request
}

func (e *AdmissionDecisionEvent) Execute(cs *ClusterSimulator) {
    // Build router state (snapshots of all instances)
    state := buildRouterState(cs, e.request)
    
    // Query admission policy
    admitted, reason := cs.admissionPolicy.Admit(e.request, state)
    
    if admitted {
        // → Schedule RoutingDecisionEvent (or gateway queue for flow control)
    } else {
        // Request rejected; metrics tracked
        cs.rejectedRequests++
    }
}
```

**Admission Policies (matching llm-d patterns):**

| Policy | Behavior | Simulates |
|--------|----------|-----------|
| `always-admit` | All requests accepted | No rate limiting |
| `token-bucket` | Rate-limited by token budget | Token-bucket rate limiting (e.g., 5000 tokens/sec) |
| `tier-shed` | Sheds lower-priority requests under overload | SLO-aware admission shedding (e.g., shed Sheddable/Batch when Critical/Standard at capacity) |

**Request lifecycle constraints enforced (INV-9):**
- Admission decisions use only arrival-time metadata (SLOClass, InputTokens, TenantID)
- **Never** reads `OutputTokens` — that's an oracle field only available at completion time
- This prevents decisions based on information not available in production

---

### Stage 3: Routing Decision

**Event:** `RoutingDecisionEvent` (Priority 2)

The routing policy selects which instance handles the request. This stage models **llm-d's request routing subsystem**.

```go
type RoutingDecisionEvent struct {
    time    int64
    request *sim.Request
}

func (e *RoutingDecisionEvent) Execute(cs *ClusterSimulator) {
    // Build router state with current snapshots
    state := buildRouterState(cs, e.request)
    
    // Get routing decision (policy selects one of the snapshots)
    decision := cs.routingPolicy.Route(e.request, state)
    
    // Inject into target instance
    for _, inst := range cs.instances {
        if inst.ID() == decision.TargetInstance {
            cs.inFlightRequests[decision.TargetInstance]++
            inst.InjectRequestOnline(e.request, e.time)
            break
        }
    }
}
```

**Routing Policies (all implemented in sim/routing.go):**

| Policy | Basis | Simulates |
|--------|-------|-----------|
| `round-robin` | Sequential index | Simple load balancing |
| `least-loaded` | Queue depth + batch size | Greedy queue depth minimization |
| `weighted` | Composable scorers | llm-d's production scoring system |

**Weighted Routing Scorers:**

The production system (llm-d) uses a composable scoring framework. BLIS implements the same scorers with the same semantics:

```bash
--routing-scorers "precise-prefix-cache:2,queue-depth:1,kv-utilization:1"
```

Each scorer produces per-instance scores in [0, 1]. Scores are combined as a weighted sum, and the instance with the highest score wins.

**Implemented Scorers:**

| Scorer | Range | Semantics | Simulates |
|--------|-------|-----------|-----------|
| `precise-prefix-cache` | [0,1] | Bonus for KV cache hits (prefix affinity). Queries actual instance KV cache state. Min-max normalized. | llm-d's KV-aware routing |
| `queue-depth` | [0,1] | Penalty for high effective load (queue + batch + in-flight). Min-max normalized. | Load balancing |
| `kv-utilization` | [0,1] | Penalty for high KV cache utilization. Min-max normalized. | Preventing KV thrashing |
| `load-balance` | [0,1] | Simple load balancing (all instances equal unless load differs). | Fallback balancing |
| `prefix-affinity` | [0,1] | Legacy prefix affinity (stateful, block hash based). | Historical comparison |
| `no-hit-lru` | [0,1] | Distributes cold requests to least-recently-used endpoints. | Cold-start optimization |

**Signal Freshness (Routing Snapshot tier system, INV-7):**

Routing snapshots read instance state at different freshness tiers:

```
Tier 0 (Synchronous):   InFlightRequests (updated in real-time during routing)
Tier 1 (Immediate):     QueueDepth, BatchSize, KVUtilization (read fresh each decision)
Tier 2 (Periodic):      KV cache hash map for precise-prefix-cache (refreshed at --cache-signal-delay interval)
Tier 3 (Stale):         KV cache hash map (optional stale snapshot via --cache-signal-delay, modeling production delay)
```

Default `--cache-signal-delay 2000000` (2s) models llm-d's speculative TTL — the blind spot between routing decision and KV event propagation via ZMQ.

---

## Part 3: Flow Control (Simulating GAIE)

### Gateway Queue and Saturation-Gated Dispatch

When `--flow-control` is enabled, BLIS implements GAIE-style flow control with a **gateway queue** and **saturation detection**.

```go
// From inference-sim/sim/cluster/gateway_queue.go
type GatewayQueue struct {
    heap     gatewayQueueHeap     // priority-ordered queue
    maxDepth int                  // 0 = unlimited
    shedCount int
}

// Dequeue removes highest-priority (or earliest FIFO) request
func (q *GatewayQueue) Dequeue() *sim.Request
```

**How it works:**

1. **Admission → Gateway Queue:** Admitted requests enter the gateway queue instead of immediately routing
2. **Saturation Detection:** A `SaturationDetector` checks if the cluster is saturated:
   - `utilization`: UtilizationDetector — checks max KV cache utilization across instances
   - `concurrency`: ConcurrencyDetector — checks max in-flight request count
3. **Completion-Triggered Dispatch:** Each time an instance completes a request, the gateway queue tries to dequeue and route one queued request
4. **Queue Shedding:** When the queue is at `maxDepth` capacity, lower-priority requests are shed (higher priority keeps its spot)

**Dispatch Ordering:**

- `fifo`: First-in-first-out (FIFO)
- `priority`: Priority-ordered (higher SLO tier first), then FIFO

**Metrics tracked:**

```
gateway_queue_depth   — current queue size (snapshot at completion time)
gateway_queue_shed    — cumulative requests shed (added to INV-1 conservation equation)
```

**What it simulates:** GAIE's saturation-gated dispatch mechanism — holding back requests when the cluster cannot serve them immediately, freeing CPU resources for in-flight work, and automatically dispatching when capacity opens up.

---

## Part 4: Per-Instance Simulation (Discrete-Event Execution)

Once a request is routed to an instance, it enters the **per-instance discrete-event simulator** (sim/simulator.go). Each instance has its own event queue.

### Request Lifecycle Within Instance

```
Routed Request
    ↓
Wait Queue (FIFO or Priority ordered)
    ↓
Batch Formation (accumulate tokens up to max batch size or token limit)
    ↓
Prefill Phase (all requests in batch process input tokens in parallel)
    ↓
Decode Phase (generate output tokens one by one; requests exit on completion)
    ↓
Completion (request done, latency recorded)
```

### KV Cache Management

**Single-Tier (GPU only):**
- Fixed number of KV blocks (calculated from model size, GPU memory, TP degree)
- Prefix caching: consecutive request prefixes reuse KV blocks
- Block hash computed via `sim/internal/hash/hash.go` (deterministic, seed-based)

**Tiered (GPU + CPU offload):**
- KV blocks spill from GPU to CPU when GPU is full
- Higher latency for CPU blocks but more capacity
- Simulates production vLLM tiered caching

### Latency Estimation

Five latency model backends available via `--latency-model`:

| Mode | Basis | Use Case |
|------|-------|----------|
| `roofline` | Analytical FLOPs/bandwidth | Fast, architecture-aware (default) |
| `trained-physics` | Roofline basis + learned corrections | Generalizes across models and TP (recommended) |
| `blackbox` | Per-model trained coefficients (α/β) | Requires calibration per model |
| `cross-model` | Physics-informed architecture features | MoE-aware, generalizes |
| `trained-roofline` | Roofline × learned multiplier | Legacy, for comparison |

**Roofline formula (from sim/latency/roofline.go):**

```
latency_ms = (compute_flops / tflops_peak) + (data_bytes / bw_peak_tbps)
```

where:
- `compute_flops` = model FLOPs (depends on sequence length and TP)
- `data_bytes` = weight + KV cache bytes transferred
- `tflops_peak`, `bw_peak_tbps` = GPU capabilities (H100: 989 TFLOPs, 3.35 TB/s)

---

## Part 5: Multi-Model Routing and Disaggregation

### Per-Model Request Filtering (T044, T048)

When requests specify a model (`req.Model != ""`), routing filters to instances serving that model:

```go
func buildRouterState(cs *ClusterSimulator, req *sim.Request) *RouterState {
    snapshots := make([]RoutingSnapshot, 0)
    for _, inst := range cs.instances {
        if req != nil && req.Model != "" && inst.Model != req.Model {
            continue  // Skip instances serving different models
        }
        snapshots = append(snapshots, inst.Snapshot(...))
    }
    return &RouterState{Snapshots: snapshots}
}
```

**Use case:** Multi-model serving clusters where different instances serve different models.

### Prefill/Decode Disaggregation (PD)

When `--prefill-instances` and `--decode-instances` are set, BLIS simulates **Prefill/Decode disaggregation** — a two-pool cluster architecture:

- **Prefill Pool:** GPU-optimized for input token processing (high compute utilization)
- **Decode Pool:** Memory-optimized for output token generation (low bandwidth per token)

**Pipeline:**

```
Request → Prefill Pool (process input tokens) → KV Transfer → Decode Pool → Output
```

**Disaggregation Decision:** A `DisaggregationDecider` determines whether to disaggregate:

- `prefix-threshold`: Disaggregate if input tokens exceed a threshold (e.g., > 512)
- `direct-to-decode`: Disaggregate if KV generation would fit on decode instance directly
- `always-disaggregate`: Always split (pathological for testing)

**KV Transfer Modeling:**

```
KV bytes transferred = (num_output_tokens × batch_size × kv_bytes_per_token)
transfer_latency = kv_bytes / inter_node_bandwidth
```

Models network transfer time between prefill and decode pools (e.g., 25 Gbps NIC, 100 µs latency).

---

## Part 6: Metrics and Observability

### Per-Request Metrics

Each request captures:

```
arrival_time         — when the request arrived
enqueue_time         — when it entered the wait queue
schedule_time        — when batch formation selected it
completion_time      — when the last token was generated

computed_metrics:
  ttft_ms = schedule_time - arrival_time       (Time-to-First-Token)
  itl_ms = (completion_time - schedule_time) / num_output_tokens  (Inter-Token Latency)
  e2e_ms = completion_time - arrival_time      (End-to-End)
```

### Cluster-Level Aggregates

```
ttft_mean_ms, ttft_p99_ms              — TTFT percentiles
itl_mean_ms, itl_p99_ms                — ITL percentiles
e2e_mean_ms, e2e_p99_ms                — E2E percentiles

tokens_per_sec                         — throughput (output tokens / simulation duration)
responses_per_sec                      — request throughput
preemption_count                       — number of requests evicted from batch
completed_requests                     — requests that finished
dropped_unservable                     — requests dropped (insufficient KV or timeout)
gateway_queue_depth                    — current gateway queue size
gateway_queue_shed                     — total sheddings from gateway queue
rejected_requests                      — rejected by admission policy
routing_rejections                     — rejected at routing (no routable instances)
```

### Per-SLO-Class Fairness

When workload specifies SLO classes (critical, standard, sheddable, batch, background):

```
per_slo_metrics[class]:
  count                    — requests in this class
  ttft_mean_ms             — mean TTFT for this class
  completed_requests       — completions in this class
  jain_fairness_index      — fairness across instances (0-1, higher = fairer)
```

**Jain Fairness Index:**

```
J = (sum of throughputs)² / (N × sum of (throughput²))
```

where N = number of SLO classes. J=1 means all classes receive proportional throughput; J<1 indicates starvation.

### Request-Level Trace (Optional)

When `--trace-level decisions` is set:

```
request_id              — unique ID
admission_decision      — (admitted, reason)
routing_decision        — (target_instance, scores, reason)
regret_analysis         — (top_k alternatives, missed opportunity)
```

Used for post-hoc counterfactual analysis: "What if we had routed to instance 2 instead?"

---

## Part 7: Bridges to Production (sim2real Transfer)

### Transfer Pipeline (sim2real project)

The evolved algorithms from BLIS are transferred to production via a 9-stage pipeline:

1. **Extract:** Parse EVOLVE-BLOCK, extract algorithm summary
2. **Translate:** Map sim signals to production equivalents (sim queue depth → llm-d in_flight_requests)
3. **Generate:** LLM produces scorer plugin code
4. **Validate:** Verify generated code matches algorithm semantics
5. **Test:** Build and integration test with real llm-d
6. **Equivalence Gate:** A/B/C suite rank correlation validation
7. **Build & Push:** Docker container image
8. **Validate:** Cluster benchmarking
9. **PR:** Create production PRs

### Key Signal Mappings

| BLIS Signal | llm-d Equivalent | What It Represents |
|-------------|------------------|-------------------|
| `QueueDepth` | `request_queue.len()` | Requests waiting in per-instance queue |
| `BatchSize` | `running_batch.len()` | Requests currently processing |
| `InFlightRequests` | `dispatched_but_not_completed` | Requests routed but not yet completed |
| `KVUtilization` | `kv_cache_blocks_used / total` | Fraction of KV cache in use |
| `CacheHitRate` | `prefix_cache_hits / lookups` | Fraction of requests with prefix hits |
| `EffectiveLoad` | Sum of above (custom metric) | Composite load signal |

### Scoring Framework Transfer

Weighted routing with multiple scorers transfers directly:

**BLIS config:**
```
--routing-policy weighted \
--routing-scorers "precise-prefix-cache:2,queue-depth:1,kv-utilization:1"
```

**Generated llm-d scorer (Go plugin):**
```go
func ComputeScore(req *llmd.Request, instance *llmd.Instance) float64 {
    // Compute per-scorer subscores
    prefixScore := computePrefixCache(req, instance)    // 0-1
    queueScore := computeQueueDepth(instance)           // 0-1
    kvScore := computeKVUtil(instance)                  // 0-1
    
    // Weighted sum (weights normalized to 1.0 in BLIS)
    return 2.0*prefixScore + 1.0*queueScore + 1.0*kvScore
}

func Route(req *llmd.Request, instances []*llmd.Instance) *llmd.Instance {
    bestInstance := nil
    bestScore := -1.0
    for _, inst := range instances {
        if score := ComputeScore(req, inst); score > bestScore {
            bestScore = score
            bestInstance = inst
        }
    }
    return bestInstance
}
```

---

## Part 8: Key Invariants

### Conservation Laws (INV-1)

All requests must account for — none are silently lost:

```
num_requests == injected
              + completed
              + still_queued
              + still_running
              + dropped_unservable
              + timed_out
              + rejected_by_admission
              + rejected_by_routing
              + gateway_queue_depth
              + gateway_queue_shed
```

### Request Lifecycle (INV-5)

Timestamps must be causally ordered:

```
arrival_time ≤ enqueue_time ≤ schedule_time ≤ completion_time
```

### Clock Monotonicity (INV-3)

Simulation clock never decreases — prevents causality violations.

### Oracle Boundary (INV-9)

Routing/admission decisions use only arrival-time metadata:
- ✅ Can read: `SLOClass`, `InputTokens`, `TenantID`
- ❌ Cannot read: `OutputTokens`, `CompletionTime`

This ensures simulation decisions are feasible in production.

---

## Part 9: Configuration and Flags

### Core Simulation Parameters

```bash
--model HUGGINGFACE_ID               # Model to simulate (e.g., qwen/qwen3-14b)
--num-instances N                    # Number of replica instances
--rate REQUESTS_PER_SEC             # Request arrival rate
--num-requests N                    # Total requests to simulate
--horizon MICROSECONDS              # Simulation time limit
```

### Admission and Routing

```bash
--admission-policy POLICY           # always-admit, token-bucket, tier-shed
--admission-latency MICROSECONDS    # Admission decision overhead
--routing-policy POLICY             # round-robin, least-loaded, weighted
--routing-latency MICROSECONDS      # Routing decision overhead
--routing-scorers "name:weight,..." # Scorer configuration
```

### Flow Control (GAIE)

```bash
--flow-control                      # Enable gateway queue
--saturation-detector TYPE          # utilization or concurrency
--queue-depth-threshold N           # Instance load threshold
--dispatch-order ORDER              # fifo or priority
--max-gateway-queue-depth N         # Max gateway queue size
```

### Disaggregation

```bash
--prefill-instances N               # Prefill pool size
--decode-instances N                # Decode pool size
--pd-decider STRATEGY              # prefix-threshold, direct-to-decode
--pd-prefix-threshold TOKENS       # Prefill disaggregation threshold
```

### Observability

```bash
--trace-level LEVEL                # none, decisions, full
--summarize-trace                  # Print trace summary
--counterfactual-k N               # Top-k regret analysis
```

---

## Part 10: Typical Workflows

### Single-Instance Baseline

```bash
./blis run --model qwen/qwen3-14b \
  --num-instances 1 --rate 100 --num-requests 1000
```

Produces baseline metrics (no admission, routing, or gateway overhead).

### Multi-Instance with Weighted Routing

```bash
./blis run --model qwen/qwen3-14b \
  --num-instances 4 \
  --routing-policy weighted \
  --routing-scorers "precise-prefix-cache:2,queue-depth:1,kv-utilization:1" \
  --rate 100 --num-requests 1000
```

Routes using the production-parity scorer configuration.

### With Flow Control (GAIE)

```bash
./blis run --model qwen/qwen3-14b \
  --num-instances 4 \
  --flow-control \
  --saturation-detector utilization \
  --queue-depth-threshold 0.8 \
  --dispatch-order priority \
  --rate 100 --num-requests 1000
```

Enables gateway queue with priority-based dispatch.

### Prefill/Decode Disaggregation

```bash
./blis run --model qwen/qwen3-14b \
  --prefill-instances 2 --decode-instances 8 \
  --pd-decider prefix-threshold \
  --pd-prefix-threshold 512 \
  --rate 100 --num-requests 1000
```

Models a two-pool architecture with automatic disaggregation based on input length.

### Decision Tracing and Analysis

```bash
./blis run --model qwen/qwen3-14b \
  --num-instances 4 \
  --trace-level decisions \
  --summarize-trace \
  --counterfactual-k 3 \
  --rate 100 --num-requests 1000
```

Produces trace summary with top-3 alternative routing decisions and regret analysis.

---

## Part 11: Extension and Customization

### Adding New Routing Policies

In `sim/routing.go`:

```go
type MyCustomPolicy struct {
    // state
}

func (p *MyCustomPolicy) Route(req *Request, state *RouterState) RoutingDecision {
    // Select instance based on custom logic
    // Return RoutingDecision{TargetInstance: "instance-id", Reason: "..."}
}
```

Register in the factory:

```go
case "my-custom-policy":
    return &MyCustomPolicy{...}
```

### Adding New Admission Policies

In `sim/admission.go`:

```go
type MyAdmissionPolicy struct {}

func (p *MyAdmissionPolicy) Admit(req *Request, state *RouterState) (bool, string) {
    // Return (admitted, reason)
}
```

Register in the factory.

### Adding New Scorers

In `sim/routing_scorers.go`:

```go
func scoreMyScorer(req *Request, snapshots []RoutingSnapshot) map[string]float64 {
    scores := make(map[string]float64, len(snapshots))
    for _, snap := range snapshots {
        scores[snap.ID] = computeScore(req, snap)  // 0-1
    }
    return scores
}
```

Register in `newScorerWithObserver` factory.

---

## Summary

BLIS is a faithful simulator of the production routing stack:

- **Admission Control:** Matches llm-d's SLO-aware admission policies
- **Routing:** Implements production scorers with real KV cache awareness
- **Flow Control:** Simulates GAIE saturation-gated dispatch
- **Disaggregation:** Models prefill/decode pool architecture
- **Latency Estimation:** Uses physics-informed and learned models
- **Observability:** Traces decisions for counterfactual analysis

The algorithms evolved in BLIS transfer directly to llm-d via a 9-stage pipeline that maps sim signals to production metrics and generates deployable code.

For production integration, see the [sim2real transfer pipeline](../docs/transfer/blis_to_llmd_mapping.md).
