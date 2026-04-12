# BLIS-to-llm-d Signal Mapping Artifact

**Version:** 1.0
**Target submodule:** llm-d-inference-scheduler (VERIFIED — Scorer Interface Reference section verified against submodule at commit b9a4a82, PR2 Task 1. Signal Mapping Table field names VERIFIED in PR3/PR5 against `fwkdl.Metrics` at commit `b9a4a82`. Submodule bumped to 4cd7046 in PR5; logging import path updated; Scorer interface and fwkdl.Metrics fields unchanged.)
**Pinned commit hash:** 6f5cb93

## Signal Mapping Table

> **Field name note:** The `Production Equivalent` and `Prod Access Path` columns contain field names derived from design knowledge and verified against the actual `llm-d-inference-scheduler` source at the pinned commit. Previously documented as containing `RunningQueueSize` and `RunningRequestCount` — CORRECTED in PR5 to `RunningRequestsSize` (the actual field in `fwkdl.Metrics`). See also the Scorer Interface Reference section.

| Sim Signal | Go Type | Sim Access Path | Production Equivalent | Prod Access Path | Fidelity | Staleness Window (ms) | Rationale |
|------------|---------|-----------------|----------------------|------------------|----------|-----------------------|-----------|
| InFlightRequests | int | `snap.InFlightRequests` | `endpoint.GetMetrics().RunningRequestsSize` (approximate) [CORRECTED in PR5: previously documented as RunningRequestCount, which does not exist; RunningRequestsSize is the actual field] | BLISWeightedScorer (`blis_weighted_scoring.go:135`; **not** ActiveRequest scorer — ActiveRequest uses a router-side TTL cache `endpointCounts`, not `GetMetrics().RunningRequestsSize`) | medium | 0 | Sim tracks in-flight requests at router level; production tracks running requests at endpoint. **Structural semantic gap:** Router-level counting includes requests in transit (not yet received by endpoint); endpoint-level counting only includes received requests. Same unit (request count) but different counting points. **PR5 resolution (R² measurement deferred):** Empirical R² measurement of sim InFlightRequests vs prod RunningRequestsSize was not performed in PR5 — it requires live cluster instrumentation under load, which is out of scope for the validation pipeline's offline test suites. Medium fidelity is accepted provisionally for v1 based on design analysis (same unit: request count; known gap: router-level vs endpoint-level counting). **PR6 or post-v1 SHOULD measure** R² under load; if R² < 0.80, downgrade to low. **Note:** Earlier drafts mapped to `ActiveModels` which is incorrect — ActiveModels counts model instances, not requests. `RunningRequestsSize` is the correct production equivalent. |
| KVUtilization | float64 | `snap.KVUtilization` | `endpoint.GetMetrics().KVCacheUsagePercent` | Custom scorer needed | high | 0 | Same computation: ratio of used KV cache to total. Both query endpoint metrics directly. **Units:** Sim = 0.0–1.0 ratio; Prod = 0–100 percentage. **Normalization (PR3):** Divide production value by 100 to match sim's 0.0–1.0 range (i.e., `prod_kv / 100.0`). The evolved algorithm expects the sim-scale range. **REQUIRED PR3 TEST:** PR3 MUST include a unit test verifying that production KVCacheUsagePercent values (0-100) are divided by 100 before being passed to the scorer. Without this normalization, the evolved algorithm receives values 100x larger than trained on. |
| QueueDepth | int | `snap.QueueDepth` | `endpoint.GetMetrics().WaitingQueueSize` | Custom scorer needed | high | 5000 | Direct queue depth measurement. Both count requests waiting to be scheduled. Staleness window matches production snapshot refresh interval (5 s). Used in `EffectiveLoad()` composite and by the load-aware scorer. |
| BatchSize | int | `snap.BatchSize` | `endpoint.GetMetrics().RunningRequestsSize` | Custom scorer needed | medium | 0 | Sim BatchSize counts requests actively executing in the current batch. Production RunningRequestsSize is the closest equivalent (requests running at the endpoint). Same semantic as InFlightRequests mapping — both use RunningRequestsSize. **Structural note:** BatchSize and InFlightRequests both map to RunningRequestsSize; production EffectiveLoad equivalent is `WaitingQueueSize + 2*RunningRequestsSize`. |

## Fidelity Rating Scale

- **high**: Same computation, same data source, negligible staleness. Quantitative: R² ≥ 0.99 or max |sim − prod| ≤ 1% of range.
- **medium**: Equivalent computation but different data source or non-trivial staleness. Quantitative: R² ≥ 0.80 or rank-order correlation ≥ 0.90.
- **low**: Approximate or proxy signal with known semantic gap. Pipeline halts on low-fidelity signals. Quantitative: R² < 0.80 or qualitative gap documented.

> **Note:** Quantitative thresholds are provisional targets for PR5 (validation pipeline). PR1 ratings are based on design analysis; empirical validation deferred to Stage 5.
>
> **Rollback procedure if PR5 downgrades a rating to low:** If empirical validation in PR5 reveals that a signal rated medium (e.g., CacheHitRate) actually has R² < 0.80, the rating must be downgraded to low in this mapping artifact. This will cause BC-6 to halt future extract runs.

## Scorer Interface Reference

> **Verified against** llm-d-inference-scheduler at commit `b9a4a82` (2026-03-17) — signatures confirmed indirectly via LoadAware's `var _ scheduling.Scorer = &LoadAware{}` type assertion and matching method signatures. Bumped to `4cd7046` (2026-03-20): interface unchanged, logging package path changed (see import note below).
> **Interface source:** The `scheduling.Scorer` interface is defined in the external dependency `sigs.k8s.io/gateway-api-inference-extension v0.0.0-20260316135939-f0ca6aef5114` (was `v0.0.0-20260128235548-fd30cb97714a`), not in the llm-d-inference-scheduler repository itself. Verification reads LoadAware's implementation of the interface as the ground truth.

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

**Metric access (VERIFIED):**
- `endpoint.GetMetrics().WaitingQueueSize` — **VERIFIED** (load_aware.go:87)
- `endpoint.GetMetrics().RunningRequestsSize` — **VERIFIED** (CORRECTED in PR5: previously documented as RunningQueueSize, which does not exist; RunningRequestsSize is the actual field in fwkdl.Metrics)
- ~~`endpoint.GetMetrics().RunningRequestCount`~~ — **DOES NOT EXIST** (CORRECTED in PR5: previously documented as RunningRequestCount; RunningRequestsSize is the actual field; both BatchSize and InFlightRequests map to RunningRequestsSize)
- `endpoint.GetMetrics().KVCacheUsagePercent` — **VERIFIED** (fwkdl.Metrics struct field at `sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/datalayer/metrics.go:33`, confirmed present at submodule pin b9a4a82; re-confirmed at 4cd7046)

**Config:** YAML-based with scorer name, type, weight, and optional parameters (JSON blob parsed by factory).

> **Note:** Submodule initialization was completed in PR3. All field names were verified in PR5.

## Implicit Signal Dependencies (Required by Base Scoring, Not in EVOLVE-BLOCK)

The following signals are NOT part of the EVOLVE-BLOCK signal set and therefore do not appear in the Signal Mapping Table above. However, they are **required by the base WeightedScoring infrastructure** (e.g., `EffectiveLoad` composite) and **MUST be populated by Stage 3 scorer implementations**.

- **QueueDepth** → `endpoint.GetMetrics().WaitingQueueSize` — Required by base WeightedScoring `EffectiveLoad` composite. Not in the EVOLVE-BLOCK directly. Populated at `evolved_scorer.go:91` as `QueueDepth: m.WaitingQueueSize`. Stage 3 MUST include this population to keep EffectiveLoad computable.

**EffectiveLoad production expansion:** The sim composite `EffectiveLoad() = QueueDepth + BatchSize + InFlightRequests` maps to the production expression `WaitingQueueSize + RunningRequestsSize + RunningRequestsSize`. Because `BatchSize` and `InFlightRequests` both map to `RunningRequestsSize` (see Signal Mapping Table), the production equivalent is `WaitingQueueSize + 2*RunningRequestsSize`. Stage 3 generator MUST use this expansion when implementing the EffectiveLoad composite — the full formula is not derivable from the Signal Mapping Table alone without this note.

**Stage 3 contract:** Any generated scorer MUST populate `QueueDepth` from `m.WaitingQueueSize`, even though `QueueDepth` is absent from the EVOLVE-BLOCK signal list. Omitting this will cause `EffectiveLoad` to compute incorrectly.

## Notes

- All v1 signals have `staleness_window_ms = 0` (approximate-scorer class).
- **Temporal semantics assumption:** Temporal semantics verified in PR5: production `endpoint.GetMetrics()` provides point-in-time snapshot metrics, consistent with sim RoutingSnapshot assumptions.
- **Missing/default value assumption:** All production metrics are assumed to be always available from `endpoint.GetMetrics()`. If a metric field is missing, the PR3 scorer should return score 0.0 for that endpoint.

## Submodule Prerequisites

### inference-sim: minimum commit for `blis observe` (PR #704)

Stage 5 cluster benchmarking requires `blis observe` CLI to be present in the
inference-sim submodule. This command is added by PR #704
(`feat(cmd): add blis observe command for real-server latency collection`).

**Minimum commit hash:** `<fill in after PR #704 merges>`

**How to verify:** `grep -q "AddCommand(observeCmd)" inference-sim/cmd/root.go`

**How to bump the submodule:**

~~~bash
cd inference-sim
git fetch origin
git checkout <minimum_commit_hash>
cd ..
git add inference-sim
git commit -m "chore: bump inference-sim to include blis observe (#704)"
~~~

After bumping, rebuild the blis container image and update `observe.image` in `values.yaml`.
