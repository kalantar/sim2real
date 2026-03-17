# BLIS-to-llm-d Signal Mapping Artifact

**Version:** 1.0
**Target submodule:** llm-d-inference-scheduler (PARTIALLY VERIFIED — Scorer Interface Reference section verified against submodule at commit 091312c, PR2 Task 1. Signal Mapping Table field names and other sections remain UNVERIFIED pending PR3 submodule initialization.)
**Pinned commit hash:** 091312c333a50e94f5e60a2ca2926e8442eeffa9 (PR3 MUST initialize the submodule at this commit and verify all claims)

## Signal Mapping Table

> **Field name note:** The `Production Equivalent` and `Prod Access Path` columns contain field names derived from design knowledge and verified against the actual `llm-d-inference-scheduler` source at the pinned commit. Previously documented as containing `RunningQueueSize` and `RunningRequestCount` — CORRECTED in PR5 to `RunningRequestsSize` (the actual field in `fwkdl.Metrics`). See also the Scorer Interface Reference section.

| Sim Signal | Go Type | Sim Access Path | Production Equivalent | Prod Access Path | Fidelity | Staleness Window (ms) | Rationale |
|------------|---------|-----------------|----------------------|------------------|----------|-----------------------|-----------|
| QueueDepth | int | `snap.EffectiveLoad() -> QueueDepth` | `endpoint.GetMetrics().WaitingQueueSize` | LoadAware scorer | high | 0 | Same computation: count of waiting requests. Direct endpoint query in both systems. **Access:** Not accessed directly; accessed via `snap.EffectiveLoad()` composite (see Composite Signals below). |
| BatchSize | int | `snap.EffectiveLoad() -> BatchSize` | `endpoint.GetMetrics().RunningRequestsSize` (approximate) [CORRECTED in PR5: previously documented as RunningQueueSize, which does not exist; RunningRequestsSize is the actual field] | ActiveRequest scorer | medium | 0 | Sim tracks exact batch size; production uses running request count as proxy. **Access:** Not accessed directly; accessed via `snap.EffectiveLoad()` composite (see Composite Signals below). **Structural semantic gap:** batch size (number of items in a batch) differs from queue size (number of items waiting). Correlation expected to be strong but not exact. **PR5 MUST measure:** Compare sim BatchSize distribution against prod RunningRequestsSize to quantify the gap. If R² < 0.80, downgrade to low. |
| InFlightRequests | int | `snap.EffectiveLoad() -> InFlightRequests` | `endpoint.GetMetrics().RunningRequestsSize` (approximate) [CORRECTED in PR5: previously documented as RunningRequestCount, which does not exist; RunningRequestsSize is the actual field] | ActiveRequest scorer | medium | 0 | Sim tracks in-flight requests at router level; production tracks running requests at endpoint. **Access:** Not accessed directly; accessed via `snap.EffectiveLoad()` composite (see Composite Signals below). **Structural semantic gap:** Router-level counting includes requests in transit (not yet received by endpoint); endpoint-level counting only includes received requests. Same unit (request count) but different counting points. **PR5 MUST measure:** Compare sim InFlightRequests against prod RunningRequestsSize under load to quantify the router-vs-endpoint gap. If R² < 0.80, downgrade to low. **Note:** Earlier drafts mapped to `ActiveModels` which is incorrect — ActiveModels counts model instances, not requests. `RunningRequestsSize` is the correct production equivalent. **F-10 NOTE:** Since both BatchSize and InFlightRequests now map to `RunningRequestsSize`, the EffectiveLoad composite `WaitingQueueSize + RunningRequestsSize + RunningRequestsSize` double-counts that metric. PR3/PR5 scorers MUST adjust the composite computation to avoid double-counting (e.g., use `WaitingQueueSize + 2*RunningRequestsSize` intentionally documented, or find a distinct proxy). |
| KVUtilization | float64 | `snap.KVUtilization` | `endpoint.GetMetrics().KVCacheUsagePercent` | Custom scorer needed | high | 0 | Same computation: ratio of used KV cache to total. Both query endpoint metrics directly. **Units:** Sim = 0.0–1.0 ratio; Prod = 0–100 percentage. **Normalization (PR3):** Divide production value by 100 to match sim's 0.0–1.0 range (i.e., `prod_kv / 100.0`). The evolved algorithm expects the sim-scale range. **REQUIRED PR3 TEST:** PR3 MUST include a unit test verifying that production KVCacheUsagePercent values (0-100) are divided by 100 before being passed to the scorer. Without this normalization, the evolved algorithm receives values 100x larger than trained on. |
| CacheHitRate | float64 | `snap.CacheHitRate` | Prefix cache hit ratio from engine metrics | PrecisePrefixCache scorer (UNVERIFIED — no concrete `endpoint.GetMetrics()` field identified; PR3 MUST derive the access path from the PrecisePrefixCache scorer implementation in `llm-d-inference-scheduler`) | medium *(provisional)* | 0 | Sim uses router-side approximate cache index; production uses engine-reported precise cache metrics. Different data sources for same concept. **Provisional rating:** No empirical data supports the medium threshold (R² ≥ 0.80); the different data sources (approximate vs precise) could produce a larger gap than assumed. PR5 must validate empirically; if R² < 0.80, downgrade to low. |

## Composite Signals

| Composite | Expansion | Production Equivalent | Fidelity | Notes |
|-----------|-----------|----------------------|----------|-------|
| EffectiveLoad() | QueueDepth + BatchSize + InFlightRequests | WaitingQueueSize + RunningRequestsSize + RunningRequestsSize [CORRECTED in PR5: previously documented as WaitingQueueSize + RunningQueueSize + RunningRequestCount; RunningQueueSize and RunningRequestCount do not exist; RunningRequestsSize is the actual field for both] | medium (composite) | No single production metric equivalent. PR3 scorer must compute inline. Semantic gaps in constituent signals (see individual rows above) propagate to composite. **Composite fidelity computation (F-10):** The composite rating is the minimum of constituent ratings: min(high, medium, medium) = medium. **Note:** Both BatchSize and InFlightRequests map to RunningRequestsSize; scorer must handle this intentional overlap. |

## Additional Signals (Non-RoutingSnapshot)

| Signal | Context | Production Mapping | Fidelity | Notes |
|--------|---------|-------------------|----------|-------|
| SessionID (boolean check) | `req.SessionID != ""` | Request header `x-session-token` | high | Boolean presence check — identical semantics. **VERIFIED** (session_affinity.go:20 defines `sessionTokenHeader = "x-session-token"`). |

## Fidelity Rating Scale

- **high**: Same computation, same data source, negligible staleness. Quantitative: R² ≥ 0.99 or max |sim − prod| ≤ 1% of range.
- **medium**: Equivalent computation but different data source or non-trivial staleness. Quantitative: R² ≥ 0.80 or rank-order correlation ≥ 0.90.
- **low**: Approximate or proxy signal with known semantic gap. Pipeline halts on low-fidelity signals. Quantitative: R² < 0.80 or qualitative gap documented.

> **Note:** Quantitative thresholds are provisional targets for PR5 (validation pipeline). PR1 ratings are based on design analysis; empirical validation deferred to Stage 5.
>
> **Rollback procedure if PR5 downgrades a rating to low:** If empirical validation in PR5 reveals that a signal rated medium (e.g., CacheHitRate) actually has R² < 0.80, the rating must be downgraded to low in this mapping artifact. This will cause BC-6 to halt future extract runs.

## Scorer Interface Reference

> **Verified against** llm-d-inference-scheduler at commit `091312c` (2026-03-09) — signatures confirmed indirectly via LoadAware's `var _ scheduling.Scorer = &LoadAware{}` type assertion and matching method signatures.
> **Interface source:** The `scheduling.Scorer` interface is defined in the external dependency `sigs.k8s.io/gateway-api-inference-extension v0.0.0-20260128235548-fd30cb97714a`, not in the llm-d-inference-scheduler repository itself. Verification reads LoadAware's implementation of the interface as the ground truth.

Target system: `llm-d-inference-scheduler` (gateway-api-inference-extension framework)

**Interface (VERIFIED):** `scheduling.Scorer` from `sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/scheduling`

```go
type Scorer interface {
    Score(ctx context.Context, cycleState *CycleState, request *LLMRequest, endpoints []Endpoint) map[Endpoint]float64
    TypedName() plugin.TypedName
    Category() ScorerCategory
}
```

**Factory pattern (VERIFIED):** `pkg/plugins/scorer/load_aware.go:30`
```go
func LoadAwareFactory(name string, rawParameters json.RawMessage, handle plugin.Handle) (plugin.Plugin, error)
```
> **Note:** Each scorer has its own factory function (e.g., `LoadAwareFactory`, `SessionAffinityFactory`). The PR3 evolved scorer should use `EvolvedScorerFactory` following this same signature pattern.

**Registration (VERIFIED):** `pkg/plugins/register.go`
```go
plugin.Register(scorer.LoadAwareType, scorer.LoadAwareFactory)
```

**Scorer categories (VERIFIED):**
- `scheduling.Distribution` — used by LoadAware, ActiveRequest, NoHitLRU
- `scheduling.Affinity` — used by SessionAffinity, PrecisePrefixCache

**Existing scorers (VERIFIED):** LoadAware, ActiveRequest, SessionAffinity, PrecisePrefixCache, NoHitLRU

**Metric access (PARTIALLY VERIFIED):**
- `endpoint.GetMetrics().WaitingQueueSize` — **VERIFIED** (load_aware.go:87)
- `endpoint.GetMetrics().RunningRequestsSize` — **VERIFIED** (CORRECTED in PR5: previously documented as RunningQueueSize, which does not exist; RunningRequestsSize is the actual field in fwkdl.Metrics)
- ~~`endpoint.GetMetrics().RunningRequestCount`~~ — **DOES NOT EXIST** (CORRECTED in PR5: previously documented as RunningRequestCount; RunningRequestsSize is the actual field; both BatchSize and InFlightRequests map to RunningRequestsSize)
- `endpoint.GetMetrics().KVCacheUsagePercent` — **UNVERIFIED** (not used in submodule; PrecisePrefixCache uses ZMQ-based indexer instead)

**Config:** YAML-based with scorer name, type, weight, and optional parameters (JSON blob parsed by factory).

> **Note for PR3:** This section is now partially verified. PR3 MUST initialize the submodule, run `go mod download`, and inspect the `fwkdl.Metrics` struct definition to confirm UNVERIFIED field names before generating scorer code.

## Notes

- All v1 signals have `staleness_window_ms = 0` (approximate-scorer class).
- **Temporal semantics assumption:** Sim RoutingSnapshot is a point-in-time snapshot. Production metrics are assumed to be point-in-time queries to endpoint `GetMetrics()`. PR5 must verify.
- **EffectiveLoad() composite signal:** `EffectiveLoad() = QueueDepth + BatchSize + InFlightRequests`. Production equivalent: sum the mapped production values. PR3 scorer must implement this composite computation inline.
- CacheHitRate mapping to PrecisePrefixCache scorer may require adaptation since production uses ZMQ-based precise metrics while sim uses approximate router-side index.
- **Missing/default value assumption:** All production metrics are assumed to be always available from `endpoint.GetMetrics()`. If a metric field is missing, the PR3 scorer should return score 0.0 for that endpoint.
