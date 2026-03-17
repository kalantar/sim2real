# PR5 Appendix: File-Level Implementation Details

Companion to `docs/plans/2026-03-16-pr5-validation-pipeline.md`.

---

## K.1: `tools/schemas/validation_results.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Validation Results",
  "description": "Output of Stage 5 (Validate). Written by operator following validate.md prompt. Consumed by Stage 6 (PR creation).",
  "type": "object",
  "required": ["suite_a", "suite_b", "suite_c", "benchmark", "overall_verdict", "noise_cv"],
  "additionalProperties": false,
  "properties": {
    "suite_a": {
      "type": "object",
      "required": ["passed", "kendall_tau", "max_abs_error", "tuple_count"],
      "additionalProperties": false,
      "description": "Numeric fidelity and rank correlation results. passed=true when mean Kendall-tau > 0.8.",
      "properties": {
        "passed":        {"type": "boolean"},
        "kendall_tau":   {"type": "number", "minimum": -1, "maximum": 1},
        "max_abs_error": {"type": "number", "minimum": 0, "description": "Informational: max |sim_score - prod_score| across all tuples and endpoints"},
        "tuple_count":   {"type": "integer", "minimum": 0}
      }
    },
    "suite_b": {
      "type": "object",
      "required": ["passed", "rank_stability_tau", "threshold_crossing_pct", "informational_only"],
      "additionalProperties": false,
      "description": "Staleness rank stability. informational_only=true for all v1 transfers (staleness_window_ms=0).",
      "properties": {
        "passed":                  {"type": "boolean"},
        "rank_stability_tau":      {"type": "number", "minimum": -1, "maximum": 1},
        "threshold_crossing_pct":  {"type": "number", "minimum": 0, "maximum": 100},
        "informational_only":      {"type": "boolean", "description": "true for v1 (all signals have staleness_window_ms=0)"}
      }
    },
    "suite_c": {
      "type": "object",
      "required": ["passed", "deterministic", "max_pile_on_ratio"],
      "additionalProperties": false,
      "description": "Concurrent safety and pile-on check.",
      "properties": {
        "passed":            {"type": "boolean"},
        "deterministic":     {"type": "boolean", "description": "true if all 20 concurrent calls produced identical results"},
        "max_pile_on_ratio": {"type": "number", "minimum": 0, "description": "max selections / fair_share across 100 routing decisions"}
      }
    },
    "benchmark": {
      "type": "object",
      "required": ["passed", "mechanism_check_verdict", "t_eff"],
      "additionalProperties": false,
      "description": "Cluster benchmark mechanism check results.",
      "properties": {
        "passed":                   {"type": "boolean"},
        "mechanism_check_verdict":  {"type": "string", "enum": ["PASS", "FAIL", "INCONCLUSIVE"]},
        "t_eff":                    {"type": "number", "minimum": 0, "description": "Effective threshold from noise characterization"},
        "workload_classification":  {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["workload", "classification"],
            "properties": {
              "workload":       {"type": "string"},
              "classification": {"type": "string", "enum": ["matched", "unmatched"]},
              "improvement":    {"type": "number"},
              "matched_signals": {"type": "array", "items": {"type": "string"}}
            }
          }
        },
        "specificity_notes": {
          "type": "array",
          "items": {"type": "string"},
          "description": "Informational: unmatched workloads where |change|/baseline >= T_eff (from CLI specificity_failures). Empty array if none."
        }
      }
    },
    "overall_verdict": {
      "type": "string",
      "enum": ["PASS", "FAIL", "INCONCLUSIVE"],
      "description": "PASS iff Suite A passed AND Suite C passed AND mechanism_check_verdict==PASS. Suite B excluded from v1 verdict (informational_only)."
    },
    "noise_cv": {
      "type": "number",
      "minimum": 0,
      "description": "Maximum CV across all metrics from noise characterization run"
    },
    "operator_notes": {
      "type": "string",
      "description": "Optional. Required when overall_verdict is INCONCLUSIVE (operator sign-off via Step 5 Option 4). Documents the rationale for proceeding despite INCONCLUSIVE benchmark verdict."
    }
  }
}

> **Enforcement note:** The `operator_notes` field is not marked as top-level `required` because it is only mandatory when `overall_verdict` is `"INCONCLUSIVE"`. The lightweight custom validator (`transfer_cli.py validate-schema`) cannot express conditional requirements (`if/then` is in `_UNSUPPORTED_KEYWORDS`). **Mitigation:** K.10 Step 6 includes a manual verification step (lines 1495–1496) that explicitly checks for `operator_notes` presence when `overall_verdict` is `"INCONCLUSIVE"`. This manual check is the enforcement mechanism for this conditional requirement.
```

---

## K.1b: ~~`tools/schemas/stage4_output.schema.json`~~ — REMOVED

> **REMOVED:** This schema is no longer needed. Stage 4 writes no workspace artifact on success (`prompts/test.md` line 289: "No workspace artifacts written (success is implicit)"). The Stage 5 prerequisite now verifies Stage 4 completion via direct `go build`/`go vet` in the target repo (see K.10 Step 0). The schema definition below is retained for reference only — do NOT create this file.

This schema was originally planned for `validate.md` prerequisites but was removed because Stage 4 never creates `workspace/stage4_output.json`.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Stage 4 Output",
  "description": "Output of Stage 4 (Test). Records build/test status of the generated scorer plugin. Consumed by Stage 5 prerequisites.",
  "type": "object",
  "required": ["status", "build_passed", "tests_passed", "generated_file"],
  "additionalProperties": false,
  "properties": {
    "status": {
      "type": "string",
      "enum": ["ok", "error"],
      "description": "Overall stage 4 status"
    },
    "build_passed": {
      "type": "boolean",
      "description": "true if go build succeeded for the generated scorer"
    },
    "tests_passed": {
      "type": "boolean",
      "description": "true if go test passed for the generated scorer"
    },
    "generated_file": {
      "type": "string",
      "description": "Path to the generated scorer plugin file relative to repo root"
    },
    "retry_count": {
      "type": "integer",
      "minimum": 0,
      "description": "Number of test-status retries before success (0 = first attempt passed)"
    },
    "error_classes": {
      "type": "array",
      "items": {"type": "string"},
      "description": "Error classes encountered during build/test (from test-status classifier)"
    }
  }
}
```

---

## K.2: `tools/harness/evolved_algorithm.go`

```go
package harness

import (
	sim "github.com/inference-sim/inference-sim/sim"
)

// evolvedAlgorithm implements Algorithm using the EVOLVE-BLOCK penalty logic from
// routing/best_program.py:171-241.
//
// It wraps inference-sim's WeightedScoring (DefaultScorerConfigs: prefix-affinity:3,
// queue-depth:2, kv-utilization:2) for base scores, then applies the evolved penalty
// and bonus terms directly.
//
// Note on prefix-affinity: the production scorer (BLISWeightedScorer) cannot access
// prefix cache state via endpoint.GetMetrics(). It sets the prefix-affinity contribution
// to 0.0. In canonical Suite A tuples, prefix-affinity scores are equal across all sim
// endpoints because generateCanonicalTuples sets sim.Request.InputTokens to nil (zero
// value), causing ComputeBlockHashes(nil) → empty hashes → totalBlocks==0 → all
// instances score 0.0 from the prefix-affinity scorer (routing_prefix_scorer.go:27-48).
// Note: CacheHitRate=0 separately neutralizes the EVOLVE-BLOCK cache-affinity bonus
// term, but does NOT affect prefix-affinity (which reads InputTokens, not CacheHitRate).
// Suite A tests under this condition to achieve Kendall-tau > 0.8.
type evolvedAlgorithm struct {
	base sim.RoutingPolicy
}

// newEvolvedAlgorithm creates an evolvedAlgorithm with inference-sim's default scorer
// configuration (prefix-affinity:3, queue-depth:2, kv-utilization:2, blockSize=64).
// blockSize=64 matches the default used in inference-sim cluster simulations.
func newEvolvedAlgorithm() *evolvedAlgorithm {
	return &evolvedAlgorithm{
		base: sim.NewRoutingPolicy("weighted", sim.DefaultScorerConfigs(), 64),
	}
}

// Route implements Algorithm. It runs the EVOLVE-BLOCK logic:
// 1. Compute base weighted composite scores via WeightedScoring.
// 2. Apply evolved penalty/bonus terms (load cubic penalty, cache affinity,
//    KV pressure, hard load penalties).
// 3. Select instance with highest composite score (ties: first occurrence wins).
//
// The penalty coefficients match routing/best_program.py:171-241 exactly.
// DO NOT modify without re-running evolutionary optimization.
func (a *evolvedAlgorithm) Route(req *sim.Request, state *sim.RouterState) sim.RoutingDecision {
	snapshots := state.Snapshots
	if len(snapshots) == 0 {
		// Handle gracefully like trivialAlgorithm — callers should not pass empty
		// snapshots, but defense-in-depth prevents a panic propagating to production
		// goroutines if a future caller skips the EvolvedScorer.Score() guard.
		return sim.RoutingDecision{Reason: "no-endpoints"}
	}

	// Step 1: Get base weighted scores from the configured WeightedScoring.
	// WeightedScoring.Route() returns RoutingDecision.Scores with per-instance
	// composite scores (weighted sum of all configured scorers, clamped to [0,1]).
	baseDecision := a.base.Route(req, state)

	// Copy to mutable map so we can apply penalties.
	scores := make(map[string]float64, len(snapshots))
	for id, s := range baseDecision.Scores {
		scores[id] = s
	}

	// Step 2: Find minLoad for penalty threshold computations.
	// Mirrors routing/best_program.py:190-196.
	minLoad := float64(snapshots[0].EffectiveLoad())
	for _, snap := range snapshots[1:] {
		if l := float64(snap.EffectiveLoad()); l < minLoad {
			minLoad = l
		}
	}

	hasSession := req.SessionID != ""

	// Step 3: Apply EVOLVE-BLOCK penalty/bonus terms.
	// Mirrors routing/best_program.py:200-230.
	for _, snap := range snapshots {
		load := float64(snap.EffectiveLoad())

		// Strong load penalty: cubic scaling strongly prefers least loaded.
		// Fires when loadDelta > 0.2; penalty = 1 / (1 + delta^3 * 5).
		loadDelta := load - minLoad
		if loadDelta > 0.2 {
			loadPenalty := 1.0 / (1.0 + loadDelta*loadDelta*loadDelta*5.0)
			scores[snap.ID] *= loadPenalty
		}

		// Cache affinity for multi-turn sessions.
		// Fires when SessionID present AND CacheHitRate > 0.35.
		// In production this bonus is always skipped (CacheHitRate=0 fallback).
		if hasSession && snap.CacheHitRate > 0.35 {
			scores[snap.ID] *= (1.0 + snap.CacheHitRate*0.3)
		}

		// Memory pressure penalty: fires when KVUtilization > 0.82.
		// penalty = max(0.3, 1 - (kv - 0.82) * 2)
		if snap.KVUtilization > 0.82 {
			kvPenalty := 1.0 - (snap.KVUtilization-0.82)*2.0
			if kvPenalty < 0.3 {
				kvPenalty = 0.3
			}
			scores[snap.ID] *= kvPenalty
		}

		// Hard penalties for overloaded instances.
		if load > 7.0 {
			scores[snap.ID] *= 0.4
		} else if load > 4.5 {
			scores[snap.ID] *= (1.0 - (load-4.5)*0.12)
		}
	}

	// Step 4: Argmax — select instance with highest composite score.
	// Ties broken by first occurrence in snapshot order (strict >).
	// Mirrors routing/best_program.py:234-241.
	bestScore := -1.0
	bestIdx := 0
	for i, snap := range snapshots {
		if scores[snap.ID] > bestScore {
			bestScore = scores[snap.ID]
			bestIdx = i
		}
	}

	return sim.NewRoutingDecisionWithScores(snapshots[bestIdx].ID, "evolved", scores)
}
```

**Diff to `harness.go`** — replace the final line in `LoadAlgorithm`:

```go
// BEFORE (line 130):
return &trivialAlgorithm{}, nil

// AFTER:
return newEvolvedAlgorithm(), nil
```

---

## K.3: `tools/harness/evolved_scorer.go` (wired Score method)

Replace the entire file:

```go
package harness

import (
	"context"

	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/plugin"
	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/scheduling"

	sim "github.com/inference-sim/inference-sim/sim"
)

const (
	EvolvedScorerType = "evolved-scorer"
	// sessionTokenHeader is the request header key for session affinity.
	// Matches session_affinity.go in llm-d-inference-scheduler.
	sessionTokenHeader = "x-session-token"
)

// compile-time interface assertion
var _ scheduling.Scorer = &EvolvedScorer{}

// EvolvedScorer adapts the harness Algorithm to the production scheduling.Scorer interface.
// It translates production endpoint metrics to sim types, invokes Algorithm.Route(),
// and maps the resulting per-instance scores back to the production endpoint map.
//
// Signal translation (from workspace/signal_coverage.json and mapping artifact):
//   - endpoint.GetMetrics().WaitingQueueSize    → sim.RoutingSnapshot.QueueDepth
//   - endpoint.GetMetrics().RunningRequestsSize → sim.RoutingSnapshot.InFlightRequests
//     (F-10 single-count: BatchSize=0 in production; both sim fields combined here)
//   - NormalizeKVUtilization(KVCacheUsagePercent) → sim.RoutingSnapshot.KVUtilization
//   - CacheHitRate: 0.0 (zero fallback — no production field available)
//   - request.Headers["x-session-token"] → sim.Request.SessionID
type EvolvedScorer struct {
	typedName plugin.TypedName
	alg       Algorithm
}

// NewEvolvedScorer creates an EvolvedScorer wrapping the given Algorithm.
// Panics if alg is nil.
func NewEvolvedScorer(alg Algorithm) *EvolvedScorer {
	if alg == nil {
		panic("NewEvolvedScorer: alg must not be nil")
	}
	return &EvolvedScorer{
		typedName: plugin.TypedName{Type: EvolvedScorerType},
		alg:       alg,
	}
}

// WithName sets the scorer's instance name.
func (s *EvolvedScorer) WithName(name string) *EvolvedScorer {
	s.typedName.Name = name
	return s
}

// TypedName returns the typed name of the plugin.
func (s *EvolvedScorer) TypedName() plugin.TypedName {
	return s.typedName
}

// Category returns Distribution.
func (s *EvolvedScorer) Category() scheduling.ScorerCategory {
	return scheduling.Distribution
}

// Score translates production endpoint metrics to sim types, runs the evolved algorithm,
// and returns per-endpoint scores (higher is better; not normalized to [0,1] — the
// cache affinity bonus can produce values > 1.0, though it never fires in production
// where CacheHitRate=0 for all endpoints).
//
// Endpoints with nil metrics receive score 0.0 (defensive nil guard, matches BLISWeightedScorer).
// Empty endpoint list returns an empty (non-nil) map.
func (s *EvolvedScorer) Score(_ context.Context, _ *scheduling.CycleState, req *scheduling.LLMRequest, endpoints []scheduling.Endpoint) map[scheduling.Endpoint]float64 {
	result := make(map[scheduling.Endpoint]float64, len(endpoints))
	if len(endpoints) == 0 {
		return result
	}

	// Build RouterState from production endpoint metrics.
	snapshots := make([]sim.RoutingSnapshot, 0, len(endpoints))

	for _, ep := range endpoints {
		m := ep.GetMetrics()
		if m == nil {
			result[ep] = 0.0
			continue
		}
		id := ep.String()
		snap := sim.RoutingSnapshot{
			ID:               id,
			QueueDepth:       m.WaitingQueueSize,
			InFlightRequests: m.RunningRequestsSize, // F-10: single-count, BatchSize=0
			KVUtilization:    NormalizeKVUtilization(m.KVCacheUsagePercent),
			// CacheHitRate: implicitly 0.0 (zero value) — no production field available.
			// With CacheHitRate=0, the cache affinity bonus never fires.
		}
		snapshots = append(snapshots, snap)
	}

	if len(snapshots) == 0 {
		return result
	}

	// Build sim.Request from LLMRequest.
	simReq := sim.Request{ID: "prod-request"}
	if req != nil {
		if req.RequestId != "" {
			simReq.ID = req.RequestId
		}
		if req.Headers != nil {
			simReq.SessionID = req.Headers[sessionTokenHeader]
		}
	}

	// Run evolved algorithm.
	state := sim.RouterState{Snapshots: snapshots}
	decision := s.alg.Route(&simReq, &state)

	// Map scores back to scheduling.Endpoint keys.
	for _, ep := range endpoints {
		if ep.GetMetrics() == nil {
			continue // already scored 0.0 above
		}
		id := ep.String()
		if score, ok := decision.Scores[id]; ok {
			result[ep] = score
		}
	}
	return result
}

```

**NOTE:** The `testEndpointForScorer` type below MUST be placed in a `_test.go` file (e.g., `evolved_scorer_test.go`), not in the production `evolved_scorer.go`. Test fixtures must not live in production code. In Go, types in `_test.go` files are visible to all test files within the same package.

Create `tools/harness/evolved_scorer_test.go`:

```go
package harness

import (
	fwkdl "sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/datalayer"
)

// testEndpointForScorer is a test helper used in TestEvolvedScorerScoresCorrectly.
type testEndpointForScorer struct {
	id      string
	metrics *fwkdl.Metrics
}

func (e *testEndpointForScorer) GetMetadata() *fwkdl.EndpointMetadata {
	return &fwkdl.EndpointMetadata{}
}
func (e *testEndpointForScorer) GetMetrics() *fwkdl.Metrics { return e.metrics }
func (e *testEndpointForScorer) String() string             { return e.id }
func (e *testEndpointForScorer) Get(string) (fwkdl.Cloneable, bool) { return nil, false }
func (e *testEndpointForScorer) Put(string, fwkdl.Cloneable)        {}
func (e *testEndpointForScorer) Keys() []string                     { return nil }
```

---

## K.4: `tools/harness/stats.go`

```go
package harness

import (
	"fmt"
	"sort"
)

// KendallTau computes Kendall's tau-a rank correlation between two score maps.
// Both maps must have identical key sets; returns an error if they differ.
//
// Returns a value in [-1, 1]:
//   1.0  = perfectly concordant (same ranking order)
//   0.0  = no correlation
//  -1.0  = perfectly discordant (reversed ranking order)
//
// Ties in either map contribute 0 to both concordant and discordant counts.
// The denominator is the total number of pairs (n*(n-1)/2) without tie correction,
// making this tau-a (not tau-b). Tau-a ≤ tau-b when ties exist, so this is
// conservative — if threshold 0.8 is met with tau-a, tau-b would also pass.
// With 5-endpoint tuples (10 pairs), ~10% ties yields tau-a ≈ 0.9*tau-b;
// the 0.8 threshold accommodates this margin.
func KendallTau(simScores, prodScores map[string]float64) (float64, error) {
	if err := checkKeyAlignment(simScores, prodScores); err != nil {
		return 0, fmt.Errorf("KendallTau: %w", err)
	}
	if len(simScores) <= 1 {
		return 1.0, nil // single or empty: trivially correlated
	}

	ids := make([]string, 0, len(simScores))
	for id := range simScores {
		ids = append(ids, id)
	}
	sort.Strings(ids) // deterministic pair ordering

	concordant := 0
	discordant := 0
	for i := 0; i < len(ids); i++ {
		for j := i + 1; j < len(ids); j++ {
			ai := simScores[ids[i]]
			aj := simScores[ids[j]]
			bi := prodScores[ids[i]]
			bj := prodScores[ids[j]]

			sigA := floatSign(ai - aj)
			sigB := floatSign(bi - bj)
			switch {
			case sigA*sigB > 0:
				concordant++
			case sigA*sigB < 0:
				discordant++
			// tie in either: contributes 0 (tau-a: no denominator adjustment)
			}
		}
	}

	total := len(ids) * (len(ids) - 1) / 2
	if total == 0 {
		return 1.0, nil
	}
	return float64(concordant-discordant) / float64(total), nil
}

// floatSign returns 1, -1, or 0 for the sign of x.
func floatSign(x float64) int {
	if x > 0 {
		return 1
	} else if x < 0 {
		return -1
	}
	return 0
}

// MaxAbsDiff returns the maximum absolute difference between corresponding values
// in two score maps. Returns an error if the key sets differ.
func MaxAbsDiff(a, b map[string]float64) (float64, error) {
	if err := checkKeyAlignment(a, b); err != nil {
		return 0, fmt.Errorf("MaxAbsDiff: %w", err)
	}
	max := 0.0
	for id, av := range a {
		diff := av - b[id]
		if diff < 0 {
			diff = -diff
		}
		if diff > max {
			max = diff
		}
	}
	return max, nil
}

// checkKeyAlignment returns an error if a and b have different key sets.
func checkKeyAlignment(a, b map[string]float64) error {
	if len(a) != len(b) {
		return fmt.Errorf("key set size mismatch: %d vs %d", len(a), len(b))
	}
	for k := range a {
		if _, ok := b[k]; !ok {
			return fmt.Errorf("key %q present in first map but missing from second", k)
		}
	}
	return nil
}
```

---

## K.5: `tools/harness/suite_a_test.go`

```go
package harness

import (
	"context"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"testing"

	sim "github.com/inference-sim/inference-sim/sim"
	blis "github.com/llm-d/llm-d-inference-scheduler/pkg/plugins/scorer"
	fwkdl "sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/datalayer"
	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/scheduling"
	"k8s.io/apimachinery/pkg/types"
)

// suiteAEndpoint implements scheduling.Endpoint for Suite A test tuples.
// It populates Metrics from a sim.RoutingSnapshot, applying the production metric
// translation: QueueDepth→WaitingQueueSize, InFlightRequests→RunningRequestsSize,
// KVUtilization*100→KVCacheUsagePercent.
//
// Canonical Suite A tuples set BatchSize=0 so EffectiveLoad maps unambiguously:
//   sim: QueueDepth + 0 + InFlightRequests == prod: WaitingQueueSize + RunningRequestsSize
type suiteAEndpoint struct {
	snap sim.RoutingSnapshot
}

func endpointFromSnap(snap sim.RoutingSnapshot) *suiteAEndpoint {
	return &suiteAEndpoint{snap: snap}
}

func (e *suiteAEndpoint) GetMetadata() *fwkdl.EndpointMetadata {
	return &fwkdl.EndpointMetadata{
		NamespacedName: types.NamespacedName{Name: e.snap.ID},
	}
}

func (e *suiteAEndpoint) GetMetrics() *fwkdl.Metrics {
	return &fwkdl.Metrics{
		WaitingQueueSize:    e.snap.QueueDepth,
		RunningRequestsSize: e.snap.InFlightRequests, // BatchSize=0 invariant
		KVCacheUsagePercent: e.snap.KVUtilization * 100.0,
	}
}

func (e *suiteAEndpoint) String() string                          { return e.snap.ID }
func (e *suiteAEndpoint) Get(string) (fwkdl.Cloneable, bool)    { return nil, false }
func (e *suiteAEndpoint) Put(string, fwkdl.Cloneable)            {}
func (e *suiteAEndpoint) Keys() []string                          { return nil }

// generateCanonicalTuples creates N test tuples for Suite A.
//
// Canonical invariants (to eliminate known semantic gaps):
//   - BatchSize=0 for all snapshots: EffectiveLoad = QueueDepth + InFlightRequests,
//     which maps exactly to WaitingQueueSize + RunningRequestsSize in production.
//   - CacheHitRate=0 for all snapshots: cache affinity bonus never fires (same in sim
//     and prod), eliminating that gap from the comparison.
//
// Varied dimensions:
//   - QueueDepth: 0–8 (exercises load penalty thresholds at 4.5 and 7.0)
//   - InFlightRequests: 0–3
//   - KVUtilization: 0.0–0.95 (exercises KV penalty at 0.82)
//   - Number of endpoints: 2–5 (per-tuple random 2–5)
func generateCanonicalTuples(n int) []TestTuple {
	rng := rand.New(rand.NewSource(42)) // deterministic seed for reproducibility
	tuples := make([]TestTuple, n)
	for i := range tuples {
		numEPs := 2 + rng.Intn(4) // 2–5 endpoints
		snaps := make([]sim.RoutingSnapshot, numEPs)
		for j := range snaps {
			snaps[j] = sim.RoutingSnapshot{
				ID:               fmt.Sprintf("ep-%d-%d", i, j),
				QueueDepth:       rng.Intn(9),                      // 0–8
				InFlightRequests: rng.Intn(4),                      // 0–3; BatchSize=0
				KVUtilization:    rng.Float64() * 0.95,              // 0.0–0.95
				CacheHitRate:     0.0,                               // canonical invariant
			}
		}
		tuples[i] = TestTuple{
			Request: sim.Request{ID: fmt.Sprintf("req-%d", i)},
			State:   sim.RouterState{Snapshots: snaps},
		}
	}
	return tuples
}

// TestSuiteA_KendallTau verifies BC-6 and BC-7.
//
// BC-6: Mean Kendall-tau rank correlation between sim algorithm and BLISWeightedScorer
//       must be > 0.8 across 200 canonical tuples (BatchSize=0, CacheHitRate=0).
// BC-7: max_abs_error is computed and logged (informational, not pass/fail).
func TestSuiteA_KendallTau(t *testing.T) {
	repoRoot := findRepoRoot(t)
	summaryPath := filepath.Join(repoRoot, "workspace", "algorithm_summary.json")
	if _, err := os.Stat(summaryPath); err != nil {
		t.Skip("requires workspace/algorithm_summary.json (run extract first)")
	}

	// Sim-side: evolved algorithm
	alg, err := LoadAlgorithm(summaryPath, repoRoot)
	if err != nil {
		t.Fatalf("LoadAlgorithm: %v", err)
	}

	// Prod-side: BLISWeightedScorer
	params := blis.BLISWeightedScorerParameters{Enabled: true}
	prodScorer := blis.NewBLISWeightedScorer(context.Background(), params).WithName("suite-a")

	tuples := generateCanonicalTuples(200)
	tauSum := 0.0
	maxAbsErr := 0.0
	nonSkipped := 0

	for i, tuple := range tuples {
		// Sim scores
		simDecision := alg.Route(&tuple.Request, &tuple.State)
		if len(simDecision.Scores) == 0 {
			t.Logf("tuple %d: sim algorithm returned no scores (no-endpoints path), skipping", i)
			continue
		}
		nonSkipped++

		// Prod scores via BLISWeightedScorer
		endpoints := make([]scheduling.Endpoint, len(tuple.State.Snapshots))
		for j, snap := range tuple.State.Snapshots {
			endpoints[j] = endpointFromSnap(snap)
		}
		// Use ScoreEndpoints helper (keys by NamespacedName.String() = "/ep-i-j")
		rawProdScores := blis.ScoreEndpoints(context.Background(), prodScorer, nil, endpoints)

		// Align: ScoreEndpoints keys by NamespacedName.String() = "/ep-i-j"
		// We need to map back to snap.ID keys.
		prodScores := make(map[string]float64, len(rawProdScores))
		missingKeys := 0
		for _, snap := range tuple.State.Snapshots {
			namespacedKey := "/" + snap.ID // NamespacedName{Name: snap.ID}.String() = "/snap.ID"
			if _, ok := rawProdScores[namespacedKey]; !ok {
				missingKeys++
				if missingKeys == 1 {
					t.Logf("WARNING tuple %d: key %q not found in rawProdScores (available keys: %v); using 0.0 fallback — check NamespacedName alignment",
						i, namespacedKey, mapKeys(rawProdScores))
				}
			}
			prodScores[snap.ID] = rawProdScores[namespacedKey]
		}
		if missingKeys == len(tuple.State.Snapshots) {
			t.Fatalf("tuple %d: ALL %d prod score keys missing — NamespacedName format mismatch (expected '/<id>', got keys: %v)",
				i, len(tuple.State.Snapshots), mapKeys(rawProdScores))
		} else if missingKeys > 0 {
			t.Fatalf("tuple %d: %d of %d prod score keys missing — partial NamespacedName mismatch (expected '/<id>', got keys: %v)",
				i, missingKeys, len(tuple.State.Snapshots), mapKeys(rawProdScores))
		}

		tau, err := KendallTau(simDecision.Scores, prodScores)
		if err != nil {
			t.Fatalf("tuple %d: KendallTau: %v", i, err)
		}
		tauSum += tau

		absErr, err := MaxAbsDiff(simDecision.Scores, prodScores)
		if err != nil {
			t.Fatalf("tuple %d: MaxAbsDiff: %v", i, err)
		}
		if absErr > maxAbsErr {
			maxAbsErr = absErr
		}
	}

	if nonSkipped == 0 {
		t.Fatal("Suite A: all tuples skipped — no equivalence data collected")
	}
	meanTau := tauSum / float64(nonSkipped)
	t.Logf("Suite A: mean_kendall_tau=%.4f, max_abs_error=%.6f, tuple_count=%d",
		meanTau, maxAbsErr, nonSkipped)

	// BC-6: rank correlation threshold
	if meanTau <= 0.8 {
		t.Errorf("Suite A FAIL: mean Kendall-tau = %.4f, want > 0.8", meanTau)
	}
	// BC-7: max_abs_error logged (not a pass/fail criterion)
}

// mapKeys returns the keys of a map for diagnostic logging.
func mapKeys(m map[string]float64) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	return keys
}
```

---

## K.6: `tools/harness/suite_b_test.go`

```go
package harness

import (
	"fmt"
	"testing"

	sim "github.com/inference-sim/inference-sim/sim"
)

// SuiteBResult captures Suite B output for validation_results.json.
type SuiteBResult struct {
	Passed               bool
	RankStabilityTau     float64
	ThresholdCrossingPct float64
	InformationalOnly    bool
}

// runSuiteB executes Suite B: staleness rank stability.
//
// All v1 signals have staleness_window_ms=0 (approximate-scorer signals).
// Zero staleness means zero perturbation → identical rankings → tau=1.0.
// Suite B passes trivially in v1; results are informational_only=true.
//
// Future versions: when precise-scorer signals (staleness_window_ms > 0) are
// transferred, Suite B should introduce correlated perturbations and test
// rank_stability_tau > 0.7.
func runSuiteB(t *testing.T) SuiteBResult {
	t.Helper()

	alg := newEvolvedAlgorithm()

	// Base tuples (no perturbation)
	base := []TestTuple{
		{
			Request: sim.Request{ID: "b-req-1"},
			State: sim.RouterState{
				Snapshots: []sim.RoutingSnapshot{
					{ID: "ep-0", QueueDepth: 2, InFlightRequests: 1, KVUtilization: 0.5},
					{ID: "ep-1", QueueDepth: 4, InFlightRequests: 0, KVUtilization: 0.3},
					{ID: "ep-2", QueueDepth: 0, InFlightRequests: 2, KVUtilization: 0.7},
				},
			},
		},
		{
			Request: sim.Request{ID: "b-req-2"},
			State: sim.RouterState{
				Snapshots: []sim.RoutingSnapshot{
					{ID: "ep-0", QueueDepth: 6, InFlightRequests: 1, KVUtilization: 0.9},
					{ID: "ep-1", QueueDepth: 1, InFlightRequests: 0, KVUtilization: 0.2},
				},
			},
		},
	}

	// With staleness_window_ms=0: perturbation = 0. Perturbed tuples = base tuples.
	// Repetitions: 3 (per macro plan).
	const repetitions = 3

	tauSum := 0.0
	count := 0

	for rep := 0; rep < repetitions; rep++ {
		for _, tuple := range base {
			baseDecision := alg.Route(&tuple.Request, &tuple.State)
			// Perturbed state: zero perturbation (staleness=0), identical to base.
			perturbedDecision := alg.Route(&tuple.Request, &tuple.State)

			tau, err := KendallTau(baseDecision.Scores, perturbedDecision.Scores)
			if err != nil {
				t.Fatalf("Suite B rep %d req %s: KendallTau: %v", rep, tuple.Request.ID, err)
			}
			tauSum += tau
			count++
		}
	}

	var meanTau float64
	if count > 0 {
		meanTau = tauSum / float64(count)
	}

	// v1: all signals have staleness_window_ms=0 → informational_only=true
	return SuiteBResult{
		Passed:               true, // trivially passes in v1
		RankStabilityTau:     meanTau,
		ThresholdCrossingPct: 0.0, // zero perturbation → no threshold crossings
		InformationalOnly:    true,
	}
}

// TestSuiteB_StatenessStability verifies BC-8.
func TestSuiteB_StatenessStability(t *testing.T) {
	result := runSuiteB(t)

	if !result.InformationalOnly {
		t.Error("Suite B: expected informational_only=true for v1 approximate-scorer signals")
	}
	if !result.Passed {
		t.Errorf("Suite B: expected passed=true (trivially), got tau=%.3f", result.RankStabilityTau)
	}
	if result.RankStabilityTau < 0.99 {
		t.Errorf("Suite B: rank_stability_tau = %.3f, want ~1.0 (zero staleness)", result.RankStabilityTau)
	}
	t.Logf("Suite B: rank_stability_tau=%.4f, threshold_crossing_pct=%.1f%%, informational_only=%v",
		result.RankStabilityTau, result.ThresholdCrossingPct, result.InformationalOnly)
}

// Ensure SuiteBResult.RankStabilityTau is not unused in non-test code.
var _ = fmt.Sprintf
```

---

## K.7: `tools/harness/suite_c_test.go`

```go
package harness

import (
	"fmt"
	"sync"
	"testing"

	sim "github.com/inference-sim/inference-sim/sim"
)

// TestSuiteC_ConcurrentDeterminism verifies BC-9.
// 20 goroutines each create their own evolvedAlgorithm and call Route() with identical
// inputs; all results must match.
//
// Per-goroutine instances are required because WeightedScoring includes the prefix-affinity
// scorer, which captures mutable closure variables (cachedHashes, cachedReqID in
// routing_prefix_scorer.go:27-28) with no synchronization. Sharing a single instance
// would cause data races detectable by -race. This mirrors production behavior where
// schedulers create per-request scorer instances.
func TestSuiteC_ConcurrentDeterminism(t *testing.T) {
	tuple := TestTuple{
		Request: sim.Request{ID: "concurrent-req"},
		State: sim.RouterState{
			Snapshots: []sim.RoutingSnapshot{
				{ID: "ep-0", QueueDepth: 2, InFlightRequests: 1, KVUtilization: 0.3},
				{ID: "ep-1", QueueDepth: 5, InFlightRequests: 0, KVUtilization: 0.85}, // KV penalty
				{ID: "ep-2", QueueDepth: 1, InFlightRequests: 2, KVUtilization: 0.5},
				{ID: "ep-3", QueueDepth: 8, InFlightRequests: 0, KVUtilization: 0.1}, // hard load penalty
			},
		},
	}

	const goroutines = 20
	results := make([]sim.RoutingDecision, goroutines)
	var wg sync.WaitGroup
	for i := range results {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			alg := newEvolvedAlgorithm() // per-goroutine instance (BC-9 mechanism)
			results[idx] = alg.Route(&tuple.Request, &tuple.State)
		}(i)
	}
	wg.Wait()

	ref := results[0]
	if ref.TargetInstance == "" {
		t.Fatal("reference result has empty TargetInstance")
	}
	for i := 1; i < goroutines; i++ {
		if results[i].TargetInstance != ref.TargetInstance {
			t.Errorf("goroutine %d: target %q != ref %q", i, results[i].TargetInstance, ref.TargetInstance)
		}
		for id, refScore := range ref.Scores {
			if results[i].Scores[id] != refScore {
				t.Errorf("goroutine %d: Scores[%s] = %f, ref = %f", i, id, results[i].Scores[id], refScore)
			}
		}
	}
	t.Logf("Suite C concurrent: all %d goroutines selected %q with identical scores", goroutines, ref.TargetInstance)
}

// TestSuiteC_PileOn verifies BC-10.
// 100 routing decisions with varied load assignments; no endpoint selected > 2x fair share.
func TestSuiteC_PileOn(t *testing.T) {
	alg := newEvolvedAlgorithm()
	const (
		numEndpoints = 5
		numDecisions = 100
		fairShare    = float64(numDecisions) / numEndpoints // 20
		maxAllowed   = 2 * fairShare                        // 40
	)

	counts := make(map[string]int, numEndpoints)

	// Generate tuples where exactly one endpoint has lowest load (rotating pattern).
	// Each endpoint gets lowest load numDecisions/numEndpoints = 20 times.
	for i := 0; i < numDecisions; i++ {
		snaps := make([]sim.RoutingSnapshot, numEndpoints)
		for j := 0; j < numEndpoints; j++ {
			snaps[j] = sim.RoutingSnapshot{
				ID:         fmt.Sprintf("ep-%d", j),
				QueueDepth: 3, // base load
			}
		}
		// Rotate: endpoint (i % numEndpoints) gets lowest load
		snaps[i%numEndpoints].QueueDepth = 0

		state := sim.RouterState{Snapshots: snaps}
		decision := alg.Route(&sim.Request{ID: fmt.Sprintf("req-%d", i)}, &state)
		counts[decision.TargetInstance]++
	}

	maxPileOn := 0.0
	for id, cnt := range counts {
		ratio := float64(cnt) / fairShare
		if ratio > maxPileOn {
			maxPileOn = ratio
		}
		if float64(cnt) > maxAllowed {
			t.Errorf("pile-on: endpoint %s selected %d times (> 2x fair share %.0f)", id, cnt, maxAllowed)
		}
	}
	t.Logf("Suite C pile-on: max_pile_on_ratio=%.2f (threshold: 2.0), counts=%v", maxPileOn, counts)
}
```

---

## K.8: `transfer_cli.py` — `noise-characterize` command

**PREREQUISITE:** Before adding K.8 or K.9, add `import os` to the module-level imports in `tools/transfer_cli.py` (alongside `import argparse`, `import json`, etc.). Both `_cmd_noise_characterize` and `_cmd_benchmark` reference `os.environ` for the `_SIM2REAL_ALLOWED_ROOT` env var. Without this import, both commands raise `NameError: name 'os' is not defined` at runtime.

Add this function and subcommand to `tools/transfer_cli.py`. Insert after the existing `test-status` command implementation.

```python
# ─── noise-characterize ───────────────────────────────────────────────────────

def _cmd_noise_characterize(args: argparse.Namespace) -> int:
    """Compute per-metric CV and T_eff from baseline latency runs.

    Input JSON format:
        {"runs": [{"p50": float, "p95": float, "p99": float}, ...]}

    At least one latency field (p50, p95, p99) must be present per run.
    Runs with 0 values for a metric are excluded from that metric's CV.

    Output JSON:
        {"status": "ok"|"error", "per_metric_cv": {"p50": float, ...},
         "t_eff": float, "halt": bool, "errors": []}

    Exit codes:
        0 = success (halt=false)
        1 = validation failure (halt=true, CV > 15%)
        2 = infrastructure error (file missing or invalid JSON)
    """
    import math

    runs_path = Path(args.runs).resolve()
    allowed_root = Path(os.environ["_SIM2REAL_ALLOWED_ROOT"]).resolve() if "_SIM2REAL_ALLOWED_ROOT" in os.environ else REPO_ROOT
    if not runs_path.is_relative_to(allowed_root):
        return _output("error", 2,
                       errors=[f"Runs path '{runs_path}' is outside allowed root '{allowed_root}'."],
                       per_metric_cv={}, t_eff=0.0, halt=False)
    if not runs_path.exists():
        return _output("error", 2, errors=[f"runs file not found: {args.runs}"],
                       per_metric_cv={}, t_eff=0.0, halt=False)

    if runs_path.stat().st_size > MAX_FILE_SIZE:
        return _output("error", 2,
                       errors=[f"runs file exceeds {MAX_FILE_SIZE} bytes: {args.runs}"],
                       per_metric_cv={}, t_eff=0.0, halt=False)

    try:
        data = json.loads(runs_path.read_text())
    except json.JSONDecodeError as e:
        return _output("error", 2, errors=[f"invalid JSON in {args.runs}: {e}"],
                       per_metric_cv={}, t_eff=0.0, halt=False)

    if not isinstance(data, dict) or "runs" not in data:
        return _output("error", 2,
                       errors=["missing 'runs' key in input JSON"],
                       per_metric_cv={}, t_eff=0.0, halt=False)

    runs = data["runs"]
    if not isinstance(runs, list) or len(runs) == 0:
        return _output("error", 2,
                       errors=["'runs' must be a non-empty list (BC-16: malformed input → exit 2)"],
                       per_metric_cv={}, t_eff=0.0, halt=False)

    metrics = ["p50", "p95", "p99"]
    per_metric_cv: dict[str, float] = {}
    skipped_metrics: list[str] = []

    for metric in metrics:
        values = [r[metric] for r in runs if isinstance(r, dict) and metric in r
                  and isinstance(r[metric], (int, float)) and not math.isnan(r[metric])
                  and not math.isinf(r[metric]) and r[metric] > 0]
        filtered = len([r for r in runs if isinstance(r, dict) and metric in r]) - len(values)
        if len(values) < 2:
            skipped_metrics.append(
                f"{metric}: insufficient valid data ({len(values)} point(s) after filtering {filtered} invalid/non-positive value(s))"
            )
            continue  # insufficient data for this metric; skip

        mean = sum(values) / len(values)
        if mean == 0:
            skipped_metrics.append(f"{metric}: mean=0 after filtering")
            continue
        # Sample variance (Bessel's correction: n-1) — population variance underestimates
        # std dev for small n (e.g. 5 baseline runs), which could mask borderline-noisy benchmarks.
        variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        std = math.sqrt(variance)
        per_metric_cv[metric] = std / mean

    if not per_metric_cv:
        n_runs = len([r for r in runs if isinstance(r, dict)])
        return _output("error", 2,
                       errors=[f"insufficient runs for CV computation: need ≥2 data points per metric, got {n_runs} run(s) with valid latency values",
                               *skipped_metrics],
                       per_metric_cv={}, t_eff=0.0, halt=False, skipped_metrics=skipped_metrics)

    max_cv = max(per_metric_cv.values())
    t_eff = max(0.05, 2.0 * max_cv)
    halt = max_cv > 0.15

    if halt:
        return _output("error", 1, per_metric_cv=per_metric_cv, t_eff=t_eff, halt=True,
                       skipped_metrics=skipped_metrics,
                       errors=[f"noise too high: max CV={max_cv:.4f} > 0.15 threshold"])

    return _output("ok", 0, per_metric_cv=per_metric_cv, t_eff=t_eff, halt=False,
                   skipped_metrics=skipped_metrics)
```

Add to the argument parser setup at the bottom of the file:

```python
# noise-characterize subcommand
p_noise = subparsers.add_parser("noise-characterize",
    help="Compute per-metric CV and T_eff from baseline latency runs")
p_noise.add_argument("--runs", required=True,
    help="Path to JSON file with baseline latency runs: {runs: [{p50, p95, p99}]}")
p_noise.set_defaults(func=_cmd_noise_characterize)
```

---

## K.9: `transfer_cli.py` — `benchmark` command

**PREREQUISITE:** Requires module-level `import os` and `import math` (see K.8 prerequisite). Both K.8 and K.9 use `os.environ`; K.9 uses `math.isnan`/`math.isinf` for NaN/Inf guards. Confirm both are in the module-level imports before adding these commands.

Add this function and subcommand to `tools/transfer_cli.py`.

```python
# ─── benchmark ────────────────────────────────────────────────────────────────

def _cmd_benchmark(args: argparse.Namespace) -> int:
    """Compute mechanism check from benchmark results.

    Input JSON format:
        {"workloads": [
            {"name": str, "classification": "matched"|"unmatched",
             "baseline_p99": float, "transfer_p99": float},
            ...
        ]}

    Mechanism check logic:
        improvement_i = (baseline_p99_i - transfer_p99_i) / baseline_p99_i
        verdict = PASS  if any(improvement_i >= t_eff for matched workloads)
        verdict = INCONCLUSIVE if any(0 < improvement < t_eff) but none reach threshold
        verdict = FAIL  if all improvements <= 0 for matched workloads

    Specificity check:
        For each unmatched workload, |change|/baseline must be < t_eff.
        Fails recorded in output but do NOT change mechanism_check_verdict (informational).

    Exit codes:
        0 = success (PASS or INCONCLUSIVE; operator checks JSON verdict for HALT)
            ⚠ IMPORTANT: exit 0 does NOT guarantee a passing result. The operator/LLM
            MUST parse JSON output and check mechanism_check_verdict — INCONCLUSIVE
            requires a HALT even though exit code is 0 (command ran successfully but
            evidence is insufficient to proceed).
            Status field: "ok" for PASS, "inconclusive" for INCONCLUSIVE (not "ok").
        1 = validation failure (FAIL verdict, t_eff not provided, or no matched workloads)
        2 = infrastructure error (file missing or invalid JSON)
    """
    import math

    if args.t_eff is None:
        return _output("error", 1,
                       errors=["--t-eff required: run noise-characterize first"],
                       mechanism_check_verdict="FAIL", results=[])

    t_eff = args.t_eff
    if t_eff <= 0:
        return _output("error", 1,
                       errors=[f"--t-eff must be > 0, got {t_eff}. "
                               "noise-characterize guarantees T_eff >= 0.05; "
                               "a non-positive value indicates manual override error."],
                       mechanism_check_verdict="FAIL", results=[])
    results_path = Path(args.results).resolve()
    allowed_root = Path(os.environ["_SIM2REAL_ALLOWED_ROOT"]).resolve() if "_SIM2REAL_ALLOWED_ROOT" in os.environ else REPO_ROOT
    if not results_path.is_relative_to(allowed_root):
        return _output("error", 2,
                       errors=[f"Results path '{results_path}' is outside allowed root '{allowed_root}'."],
                       mechanism_check_verdict="FAIL", results=[])
    if not results_path.exists():
        return _output("error", 2,
                       errors=[f"results file not found: {args.results}"],
                       mechanism_check_verdict="FAIL", results=[])

    if results_path.stat().st_size > MAX_FILE_SIZE:
        return _output("error", 2,
                       errors=[f"results file exceeds {MAX_FILE_SIZE} bytes: {args.results}"],
                       mechanism_check_verdict="FAIL", results=[])

    try:
        data = json.loads(results_path.read_text())
    except json.JSONDecodeError as e:
        return _output("error", 2,
                       errors=[f"invalid JSON in {args.results}: {e}"],
                       mechanism_check_verdict="FAIL", results=[])

    if not isinstance(data, dict) or "workloads" not in data:
        return _output("error", 2,
                       errors=["missing 'workloads' key in input JSON"],
                       mechanism_check_verdict="FAIL", results=[])

    workloads = data["workloads"]
    if not isinstance(workloads, list):
        return _output("error", 2,
                       errors=["'workloads' must be a list"],
                       mechanism_check_verdict="FAIL", results=[])

    results = []
    matched_improvements = []
    specificity_failures = []
    errors = []

    for w in workloads:
        if not isinstance(w, dict):
            continue
        name = w.get("name", "unknown")
        raw_classification = w.get("classification")
        if raw_classification is None:
            results.append({"workload": name, "classification": "unknown",
                             "improvement": 0.0, "error": "missing required field: classification"})
            errors.append(f"workload {name!r}: missing 'classification' field (expected 'matched' or 'unmatched')")
            continue
        classification = raw_classification
        baseline_p99 = w.get("baseline_p99", None)
        transfer_p99 = w.get("transfer_p99", None)

        if baseline_p99 is None or transfer_p99 is None:
            missing = [k for k in ["baseline_p99", "transfer_p99"]
                       if k not in w or w[k] is None]
            results.append({"workload": name, "classification": classification,
                             "improvement": 0.0, "error": f"missing required field(s): {', '.join(missing)}"})
            continue

        if not isinstance(baseline_p99, (int, float)) or math.isnan(baseline_p99) or math.isinf(baseline_p99):
            results.append({"workload": name, "classification": classification,
                             "improvement": 0.0, "error": f"baseline_p99 is not a finite number: {baseline_p99!r}"})
            continue

        if not isinstance(transfer_p99, (int, float)) or math.isnan(transfer_p99) or math.isinf(transfer_p99):
            results.append({"workload": name, "classification": classification,
                             "improvement": 0.0, "error": f"transfer_p99 is not a finite number: {transfer_p99!r}"})
            continue

        if baseline_p99 <= 0:
            results.append({"workload": name, "classification": classification,
                             "improvement": 0.0, "error": "baseline_p99 must be > 0"})
            continue

        if transfer_p99 < 0:
            results.append({"workload": name, "classification": classification,
                             "improvement": 0.0, "error": "transfer_p99 must be >= 0"})
            continue

        improvement = (baseline_p99 - transfer_p99) / baseline_p99
        results.append({"workload": name, "classification": classification,
                         "improvement": round(improvement, 6)})

        if classification == "matched":
            matched_improvements.append(improvement)
        elif classification == "unmatched":
            if abs(baseline_p99 - transfer_p99) / baseline_p99 >= t_eff:
                specificity_failures.append({
                    "workload": name,
                    "change_ratio": round(abs(baseline_p99 - transfer_p99) / baseline_p99, 6)
                })
        else:
            errors.append(f"unrecognized classification value: {classification!r} for workload {name!r} (expected 'matched' or 'unmatched')")

    if not matched_improvements:
        return _output("error", 1,
                       errors=["no matched workloads found — cannot compute mechanism check"],
                       mechanism_check_verdict="FAIL", results=results,
                       t_eff=t_eff, specificity_failures=specificity_failures)

    # Mechanism check
    if any(imp >= t_eff for imp in matched_improvements):
        verdict = "PASS"
    elif any(imp > 0 for imp in matched_improvements):
        verdict = "INCONCLUSIVE"
    else:
        verdict = "FAIL"

    # Exit code: 0 for PASS/INCONCLUSIVE, 1 for FAIL (consistent with noise-characterize)
    # Status: "ok" for PASS, "inconclusive" for INCONCLUSIVE, "error" for FAIL.
    # Using "inconclusive" (not "ok") for INCONCLUSIVE prevents downstream tools/CI
    # from treating INCONCLUSIVE as a passing result — callers must still check
    # mechanism_check_verdict, but status != "ok" provides an additional safety net.
    exit_code = 1 if verdict == "FAIL" else 0
    status = "error" if verdict == "FAIL" else ("inconclusive" if verdict == "INCONCLUSIVE" else "ok")
    if verdict == "FAIL":
        errors.append("mechanism check FAIL: no matched workload improvement >= T_eff")
    if specificity_failures:
        errors.append(f"specificity check failed for {len(specificity_failures)} unmatched workload(s)")

    passed = verdict == "PASS"
    return _output(status, exit_code, mechanism_check_verdict=verdict, passed=passed,
                   results=results, t_eff=t_eff, specificity_failures=specificity_failures,
                   errors=errors)
```

> **Field name translation (CLI output → validation_results.json schema):**
> The CLI outputs `results` (a list of per-workload improvement records), `specificity_failures`, and `errors`.
> These are **operational output fields** not present in the `validation_results.schema.json` benchmark object.
> When writing `validation_results.json` (Step 6 of validate.md), the operator/LLM must translate:
> - CLI `passed` → schema `passed` (same key, no translation — computed as `mechanism_check_verdict == "PASS"`)
> - CLI `results` → schema `workload_classification` (copy the list)
> - CLI `mechanism_check_verdict` → schema `mechanism_check_verdict` (same key, no translation)
> - CLI `t_eff` → schema `t_eff` (same key, no translation)
> - CLI `specificity_failures` → schema `specificity_notes` (convert each failure dict to a human-readable string, e.g., `"workload-X: |change|/baseline=0.35 >= T_eff=0.30"`; empty array if none)
> - CLI `errors`, `status` → **omit** (not in schema; `additionalProperties: false`)

Add to the argument parser:

```python
# benchmark subcommand
p_bench = subparsers.add_parser("benchmark",
    help="Compute mechanism check from benchmark results")
p_bench.add_argument("--results", required=True,
    help="Path to JSON file with workload results")
p_bench.add_argument("--t-eff", type=float, default=None,
    help="Effective threshold from noise-characterize")
p_bench.set_defaults(func=_cmd_benchmark)
```

---

## K.10: `prompts/validate.md`

```markdown
---
stage: 5
name: validate
description: Validate the generated scorer plugin against the simulation algorithm using 3-suite equivalence testing, noise characterization, and cluster benchmarks.
inputs:
  - workspace/algorithm_summary.json
  - workspace/signal_coverage.json
  - workspace/stage3_output.json
  - (Stage 4 success verified via go build/go vet — no workspace artifact)
outputs:
  - workspace/validation_results.json
---

# Stage 5: Validate

You are running Stage 5 of the sim-to-production transfer pipeline. This stage validates
equivalence between the generated scorer plugin and the original simulation algorithm.

## Prerequisites

Before proceeding, verify all predecessor artifacts exist and are valid:

```bash
python tools/transfer_cli.py validate-schema workspace/signal_coverage.json
python tools/transfer_cli.py validate-schema workspace/stage3_output.json
```

**HALT if either command exits non-zero.** Message: "HALT: Stage [2|3] prerequisite missing or invalid: workspace/<file>"

Verify Stage 4 completed successfully (scorer builds and tests pass):

```bash
(cd llm-d-inference-scheduler && go build ./pkg/plugins/scorer/... && go vet ./pkg/plugins/scorer/...)
```

**HALT if either command exits non-zero.** Message: "HALT: Stage 4 prerequisite failed — generated scorer does not build cleanly. Run Stage 4 first."

> **Note:** Stage 4 does not write a workspace artifact on success (success is implicit in the build/test passing). This prerequisite verifies the scorer builds by re-running `go build` and `go vet` scoped to the scorer package only — using `./...` would build all packages in the module and may fail for unrelated reasons (see Section H cross-system invariants).

Verify Stage 1 extract artifact exists (required by Suite A `t.Skip` guard):

```bash
test -f workspace/algorithm_summary.json || echo "HALT: workspace/algorithm_summary.json missing (run extract first)"
python tools/transfer_cli.py validate-schema workspace/algorithm_summary.json
```

**HALT if `workspace/algorithm_summary.json` is absent or invalid.** Without it, Suite A silently skips (exits 0/PASS) without running any equivalence checks — this would bypass the go/no-go gate.

## Step 1: Noise Characterization

**[OPERATOR ACTION REQUIRED]** This step requires live cluster access that Claude Code cannot perform.
Use the `llm-d-benchmark` harness (submodule at `llm-d-benchmark/`) to run 5 baseline benchmark runs against the production cluster (without the evolved scorer enabled).
Each run produces one P50/P95/P99 latency measurement. Save all runs to `workspace/baseline_runs.json` in format:

```json
{"runs": [{"p50": 0.12, "p95": 0.25, "p99": 0.45}, ...]}
```

Then compute CV and T_eff:

```bash
python tools/transfer_cli.py noise-characterize --runs workspace/baseline_runs.json
```

**HALT if exit code 1 (CV > 15%).** Message: "HALT: Noise too high — re-run during lower-variance window."
Maximum 3 noise-characterization attempts (per R4 in macro plan). After 3 consecutive CV > 15% failures, halt the transfer entirely — do not proceed to Suite A or subsequent steps.
**HALT if exit code 2 (infrastructure error).** Message: "HALT: noise-characterize infrastructure error — check that workspace/baseline_runs.json exists and contains valid JSON."
Record T_eff value for use in Step 5. Also record `noise_cv = max(per_metric_cv.values())` from the JSON output for use in Step 6 (`validation_results.json`).

## Step 2: Suite A — Rank Correlation Equivalence

Run Suite A once with `-json -v` to get both pass/fail verdict (from exit code) and structured numerical output (from JSON-wrapped `t.Logf` lines):

```bash
set -o pipefail
go test ./tools/harness/... -run TestSuiteA_KendallTau -v -timeout 60s -json 2>&1 | tee /tmp/suite_a_output.json
SUITE_A_EXIT=${PIPESTATUS[0]}
```

**HALT if `SUITE_A_EXIT` is non-zero** (or if `set -o pipefail` is unavailable, check for `"Action":"fail"` in `/tmp/suite_a_output.json`). Message: "HALT: Suite A FAIL — check test output for root cause (rank divergence if mean tau ≤ 0.8, or key-format mismatch if t.Fatalf reports missing keys)."

Extract numerical results from the same output:

```bash
grep -oE 'mean_kendall_tau=[0-9.]*|max_abs_error=[0-9.]*|tuple_count=[0-9]*' /tmp/suite_a_output.json
```

Record: mean_kendall_tau, max_abs_error, tuple_count from test output. Parse the `t.Logf` line: `Suite A: mean_kendall_tau=X.XXXX, max_abs_error=X.XXXXXX, tuple_count=NNN`.

## Step 3: Suite B — Staleness Rank Stability (Informational)

Run Suite B with `-json -v` and tee to capture structured output (same pattern as Steps 2 and 4):

```bash
go test ./tools/harness/... -run TestSuiteB_StatenessStability -v -timeout 30s -json 2>&1 | tee /tmp/suite_b_output.json
```

Suite B results are informational_only=true for v1 (all signals have staleness_window_ms=0).
Do NOT halt on Suite B results.

Extract numerical results from the captured output:

```bash
grep -oE 'rank_stability_tau=[0-9.]*|threshold_crossing_pct=[0-9.]*' /tmp/suite_b_output.json
```

Record: rank_stability_tau, threshold_crossing_pct from test output. Parse the `t.Logf` line: `Suite B: rank_stability_tau=X.XXXX, threshold_crossing_pct=X.XX`.

## Step 4: Suite C — Concurrent Safety and Pile-On

Run Suite C once with `-json -v` and tee to capture both pass/fail verdict and structured output (same pattern as Step 2):

```bash
set -o pipefail
go test ./tools/harness/... -run TestSuiteC -v -race -timeout 60s -json 2>&1 | tee /tmp/suite_c_output.json
SUITE_C_EXIT=${PIPESTATUS[0]}
```

**HALT if `SUITE_C_EXIT` is non-zero** (or if `set -o pipefail` is unavailable, check for `"Action":"fail"` in `/tmp/suite_c_output.json`). Message: "HALT: Suite C FAIL."

Extract structured results from the captured output:

```bash
grep -oE 'max_pile_on_ratio=[0-9.]*' /tmp/suite_c_output.json
```

Record: deterministic (true if TestSuiteC_ConcurrentDeterminism passes), max_pile_on_ratio from `t.Logf` line: `Suite C pile-on: max_pile_on_ratio=X.XX`.

## Step 5: Cluster Benchmarks

**[OPERATOR ACTION REQUIRED]** This step requires live cluster access that Claude Code cannot perform.

**Prerequisite check:** Verify workload YAML files exist before classification:

```bash
ls routing/workload_v2_*.yaml
```

**HALT if no files match the glob.** Message: "HALT: No routing/workload_v2_*.yaml files found — cannot classify workloads as matched/unmatched. Ensure routing artifacts are present." Without these files, the LLM has no data to perform workload classification and would fabricate assignments.

For each benchmark workload, classify as **matched** or **unmatched** using this rule:

> A workload is **matched** if the signals exercised by the workload (per `routing/workload_v2_*.yaml` parameter ranges) overlap with at least one signal listed in `workspace/signal_coverage.json` `signals[]` that has `mapped == true` (equivalently, `prod_name` is non-null). A workload is **unmatched** if none of its exercised signals are mapped.
>
> **Concrete check:** For each workload YAML, identify which sim parameters vary using the YAML-field-to-signal mapping below. If any of those signals appear in `signal_coverage.json` `signals[]` with `mapped: true` (i.e., `prod_name` is non-null), the workload is matched. Otherwise unmatched.
>
> **YAML field → signal_coverage.json `sim_name` mapping:**
>
> | Workload YAML field pattern | signal_coverage `sim_name` |
> |----------------------------|---------------------------|
> | `queue_depth_range`, `queue_depth_min/max` | `QueueDepth` |
> | `kv_util_range`, `kv_util_min/max`, `kv_utilization` | `KVUtilization` |
> | `in_flight_range`, `in_flight_requests` | `InFlightRequests` |
> | `cache_hit_rate`, `cache_hit_range` | `CacheHitRate` |
>
> If a workload YAML contains parameter ranges for fields not in this table, check `workspace/signal_coverage.json` `signals[].sim_name` for exact matches. The table above covers v1 EVOLVE-BLOCK signals; future algorithms may introduce additional signal names.

Use the `llm-d-benchmark` harness (submodule at `llm-d-benchmark/`) to run baseline and transfer benchmark configurations. Save results to `workspace/benchmark_results.json`:

```json
{"workloads": [
    {"name": "workload-name", "classification": "matched|unmatched",
     "baseline_p99": 0.45, "transfer_p99": 0.40}
]}
```

Then compute mechanism check:

```bash
python tools/transfer_cli.py benchmark --results workspace/benchmark_results.json --t-eff <T_EFF_FROM_STEP_1>
```

**HALT if exit code 2 (infrastructure error — file missing or invalid JSON).** Message: "HALT: benchmark infrastructure error." Do NOT attempt to parse JSON output on exit code 2; the output may be absent or malformed.

⚠ **Unlike other pipeline commands, `benchmark` exits 0 for both PASS and INCONCLUSIVE.** You MUST parse the JSON output to check `mechanism_check_verdict` — do NOT rely on exit code alone.

**HALT if mechanism_check_verdict == "FAIL".** Message: "HALT: Mechanism check FAIL — generated scorer shows no improvement."
**HALT if mechanism_check_verdict == "INCONCLUSIVE".** Message: "HALT: Mechanism check INCONCLUSIVE — improvement detected but below T_eff threshold."
  Remediation options for INCONCLUSIVE:
  1. **Re-run with more baseline samples** — increase from 5 to 10+ runs in Step 1 to reduce T_eff (lower noise → lower threshold → INCONCLUSIVE may become PASS).
  2. **Re-run during lower-variance window** — cluster noise may be temporarily elevated; retry during off-peak hours.
  3. **Inspect per-workload improvements** — check the `results` array in the benchmark JSON output. If one matched workload is close to T_eff, a targeted re-run of that workload with more samples may resolve the ambiguity.
  4. **Accept as soft-pass with operator sign-off** — if improvement is consistent across matched workloads but marginally below T_eff, the operator may document the rationale in the `operator_notes` field of `validation_results.json` and proceed to Stage 6 with an `overall_verdict: "INCONCLUSIVE"`. This is the **only** path that produces an INCONCLUSIVE overall_verdict — it requires explicit operator sign-off.
Record: mechanism_check_verdict, workload classification results, specificity_notes.

## Step 6: Write validation_results.json

Compile results from Steps 1–5 into `workspace/validation_results.json`:

```json
{
  "suite_a": {
    "passed": <true|false>,
    "kendall_tau": <mean_tau>,
    "max_abs_error": <max_abs_err>,
    "tuple_count": <tuple_count from Step 2 test output (may be < 200 if tuples were skipped)>
  },
  "suite_b": {
    "passed": true,
    "rank_stability_tau": <tau>,
    "threshold_crossing_pct": 0.0,
    "informational_only": true
  },
  "suite_c": {
    "passed": <true|false>,
    "deterministic": true,
    "max_pile_on_ratio": <ratio>
  },
  "benchmark": {
    "passed": <mechanism_check_verdict == "PASS">,
    "mechanism_check_verdict": "<PASS|FAIL|INCONCLUSIVE>",
    "t_eff": <t_eff>,
    "workload_classification": <copy from benchmark CLI output's "results" field — NOTE: the CLI outputs this as "results", rename to "workload_classification" for the schema>,
    "specificity_notes": <convert each CLI "specificity_failures" entry to a string, e.g., "workload-X: |change|/baseline=0.35 >= T_eff=0.30"; empty array [] if none>
  },
  "overall_verdict": "<PASS|FAIL|INCONCLUSIVE>",
  "noise_cv": <max_cv_from_step_1>,
  "operator_notes": "<required if overall_verdict is INCONCLUSIVE; omit otherwise>"
}
```

**Computing `noise_cv`:** The `noise-characterize` command outputs `per_metric_cv` (a dict keyed by metric name, e.g. `{"p50": 0.03, "p95": 0.04, "p99": 0.05}`). Compute `noise_cv = max(per_metric_cv.values())`. For example, if per_metric_cv is `{"p50": 0.03, "p95": 0.04, "p99": 0.05}`, then `noise_cv = 0.05`. Do NOT use `t_eff` as a substitute (t_eff = max(0.05, 2*noise_cv), which is a derived value).

**overall_verdict computation:**
- PASS iff suite_a.passed AND suite_c.passed AND benchmark.mechanism_check_verdict == "PASS"
- INCONCLUSIVE iff suite_a.passed AND suite_c.passed AND benchmark.mechanism_check_verdict == "INCONCLUSIVE" AND operator sign-off was given (Step 5 Option 4). The `operator_notes` field MUST be populated with the sign-off rationale.
- FAIL otherwise
- Suite B excluded from v1 verdict (informational_only=true)
- Note: INCONCLUSIVE is only reachable via the Step 5 Option 4 operator sign-off path. Without sign-off, Step 5 HALTs on INCONCLUSIVE benchmark verdict before reaching Step 6.
<!-- TODO(v2-precise-scorer): Include Suite B in verdict when staleness_window_ms > 0 -->

Validate the written artifact:

```bash
python tools/transfer_cli.py validate-schema workspace/validation_results.json
```

**HALT if validate-schema exits non-zero.**

**Manual verification (required — the lightweight validator cannot enforce `if/then` conditionals):**
If `overall_verdict` is `"INCONCLUSIVE"`, verify that `operator_notes` is present and non-empty in `workspace/validation_results.json`. This is the audit trail for the Option 4 soft-pass path. **HALT if `overall_verdict` is `"INCONCLUSIVE"` and `operator_notes` is absent or empty.** Message: "HALT: operator_notes required for INCONCLUSIVE verdict (Option 4 soft-pass audit trail)."

## Step 7: Proceed to Stage 6

If overall_verdict == "PASS", proceed to `prompts/pr.md` (Stage 6). **Note:** `prompts/pr.md` is a PR6 deliverable and will not exist until PR6 is merged. If PR6 has not yet landed, stop here — Stage 5 is complete and Stage 6 will be available after PR6.
If overall_verdict == "INCONCLUSIVE" (only reachable via Step 5 Option 4 operator sign-off), proceed to Stage 6 with the documented rationale in `operator_notes`. The same PR6 note above applies.
If overall_verdict == "FAIL", do NOT proceed — stop and document the failure.

## Halt Conditions Summary

| Condition | Trigger | Action |
|-----------|---------|--------|
| Missing prerequisite artifact | algorithm_summary.json, signal_coverage.json, or stage3_output.json absent/invalid; or Stage 4 `go build`/`go vet` fails | HALT: "Stage [N] prerequisite missing" |
| Suite A SKIP (false pass) | algorithm_summary.json absent → `t.Skip` → exit 0 without running equivalence | Caught by prerequisite check above; algorithm_summary.json must exist before Suite A runs |
| Noise CV > 15% | noise-characterize exit 1 | HALT: "Noise too high" |
| Noise infrastructure error | noise-characterize exit 2 | HALT: "noise-characterize infrastructure error" |
| Benchmark infrastructure error | benchmark exit 2 (file missing or invalid JSON) | HALT: "benchmark infrastructure error" |
| Suite A FAIL | PIPESTATUS[0] non-zero or `"Action":"fail"` in JSON output (rank divergence or key-format mismatch) | HALT: "Suite A FAIL — check test output for root cause" |
| Suite C FAIL | PIPESTATUS[0] non-zero or `"Action":"fail"` in JSON output (determinism violated or pile-on > 2.0) | HALT: "Suite C FAIL" |
| Mechanism FAIL | no matched workload improvement ≥ T_eff | HALT: "Mechanism check FAIL" |
| Mechanism INCONCLUSIVE | improvement > 0 but < T_eff | HALT: "Mechanism check INCONCLUSIVE" — **unless** operator invokes Option 4 (soft-pass with sign-off). If Option 4 is chosen, proceed to Step 6 with `overall_verdict: "INCONCLUSIVE"` and populate `operator_notes` with rationale. See Step 5 Option 4 and Step 7 for details. |
| Specificity check failures | Unmatched workload(s) with |change|/baseline ≥ T_eff | No halt — informational only. Record in `specificity_notes` array of benchmark object for audit trail. Does not affect mechanism_check_verdict. |
| Schema validation failure | validate-schema exits non-zero | HALT: "Schema validation failed" |
```

---

## K.11: `docs/transfer/noise_characterization.md`

```markdown
# Noise Characterization Procedure

**Purpose:** Establish baseline measurement variance before transfer benchmarks.
Determines T_eff (effective improvement threshold) that accounts for cluster noise.

**When to run:** Before Stage 5 cluster benchmarks, in the same cluster environment
that will be used for the transfer benchmark.

## Procedure

1. Ensure the cluster is in steady state (no unusual traffic, stable resource usage).

2. Run 5 baseline benchmark runs using the default scheduler (without evolved scorer):
   Each run produces one P50/P95/P99 latency measurement per benchmark workload.

3. Save results to `workspace/baseline_runs.json`:
   ```json
   {"runs": [
       {"p50": 0.12, "p95": 0.25, "p99": 0.45},
       {"p50": 0.11, "p95": 0.24, "p99": 0.44},
       {"p50": 0.13, "p95": 0.26, "p99": 0.46},
       {"p50": 0.12, "p95": 0.25, "p99": 0.45},
       {"p50": 0.11, "p95": 0.23, "p99": 0.43}
   ]}
   ```

4. Run noise characterization:
   ```bash
   python tools/transfer_cli.py noise-characterize --runs workspace/baseline_runs.json
   ```

5. If `halt: true` (CV > 15%): investigate noise source and re-run during lower-variance window.
   Maximum 3 attempts (per R4 in macro plan). After 3 failures, halt the transfer.

## T_eff Formula

```
T_eff = max(5%, 2 × CV_max)
```

Where `CV_max` is the maximum coefficient of variation across all latency metrics.

**Rationale:** CV_max = 15% is the halt threshold for noise characterization.
At the halt boundary (CV_max = 0.15), T_eff = max(5%, 2 × 0.15) = 30%.
Above 15% CV, the noise floor exceeds plausible algorithm improvement for v1
transfers, and the resulting T_eff (≥ 30%) makes single-run benchmarks too
imprecise to detect meaningful improvement.

## Recording

Record T_eff from the noise characterization command's JSON output (the `t_eff` field).
Do NOT modify `workspace/baseline_runs.json` — it is the CLI input file.
T_eff and `noise_cv` (`max(per_metric_cv.values())`) will be recorded in `workspace/validation_results.json` (Step 6 of validate.md).
Pass T_eff to the `benchmark` command: `--t-eff <value>`.
```

---

## K.12: `docs/transfer/calibration_log.md`

```markdown
# Transfer Pipeline Calibration Log

This file records per-transfer validation results. Stage 6 appends one entry per transfer.
**Append-only: do not modify existing entries.**

## Schema

Each entry:
```
transfer_date: YYYY-MM-DD
algorithm_name: string
pipeline_commit: string (git sha of sim2real at Stage 1 start)
single_run_provisional: true (v1 — single-run validation, lower statistical confidence)
suite_a_results:
  kendall_tau: float
  max_abs_error: float
suite_b_results:
  rank_stability_tau: float
  threshold_crossing_pct: float
  informational_only: true
suite_c_results:
  deterministic: bool
  max_pile_on_ratio: float
benchmark_results:
  mechanism_check_verdict: PASS|FAIL|INCONCLUSIVE
  t_eff: float
  matched_improvement: float (best matched workload improvement)
noise_cv: float
overall_verdict: PASS|FAIL|INCONCLUSIVE
threshold_adjustments: []
```

## Threshold Review

After 3 transfers with overall_verdict PASS or FAIL (INCONCLUSIVE excluded):
- If >50% of transfers have Kendall-tau margin < 10% above 0.8: tighten threshold
- If >30% fail on Kendall-tau with margin < 5%: loosen threshold
Document adjustments in `threshold_adjustments[]` with rationale.

## Entries

<!-- Stage 6 appends entries below this line -->
```

---

## K.13: `tools/harness/go.mod` changes

Add to `tools/harness/go.mod` require block:

```
require (
    github.com/inference-sim/inference-sim v0.0.0
    github.com/llm-d/llm-d-inference-scheduler v0.0.0                         // add this
    sigs.k8s.io/gateway-api-inference-extension v0.0.0-20260128235548-fd30cb97714a
)

replace github.com/inference-sim/inference-sim => ../../inference-sim
replace github.com/llm-d/llm-d-inference-scheduler => ../../llm-d-inference-scheduler  // add this
```

After editing `go.mod`, run:

```bash
cd tools/harness && go mod tidy
```

This updates `go.sum` with the llm-d module's dependency hashes (resolved via workspace).

**Note:** The require block shown above is a **subset** of the final `go.mod` after `go mod tidy`. Because K.5 (`suite_a_test.go`) imports `k8s.io/apimachinery/pkg/types`, `go mod tidy` will add `k8s.io/apimachinery` as a direct dependency (it may already be an indirect dep via `gateway-api-inference-extension`). Other transitive dependencies may also be promoted from indirect to direct. The shown require block lists only the manually-added entries; run `go mod tidy` and commit the resulting `go.mod` and `go.sum` as-is.

**Note:** The `go.work` at repo root already includes both `./llm-d-inference-scheduler` and `./tools/harness`. The `replace` directive in `go.mod` ensures standalone `go build` (outside the workspace) also works correctly. With the workspace active, the replace is redundant but harmless.

---

## K.14: `tools/harness/evolved_algorithm_test.go` — isolated penalty/bonus unit tests

These tests verify the individual penalty terms in isolation (I8-I9 from code review). Add as a new file `tools/harness/evolved_algorithm_test.go`.

```go
package harness

import (
	"math"
	"testing"

	sim "github.com/inference-sim/inference-sim/sim"
)

// TestEvolvedAlgorithm_LoadCubicPenalty verifies BC-2:
// cubic load penalty fires when loadDelta > 0.2, strongly preferring least-loaded endpoint.
func TestEvolvedAlgorithm_LoadCubicPenalty(t *testing.T) {
	alg := newEvolvedAlgorithm()

	// Endpoint A: EffectiveLoad=5 (delta=4 from minLoad=1)
	// Endpoint B: EffectiveLoad=1 (minLoad — no penalty)
	state := &sim.RouterState{
		Snapshots: []sim.RoutingSnapshot{
			{ID: "ep-a", QueueDepth: 5, InFlightRequests: 0, KVUtilization: 0.0},
			{ID: "ep-b", QueueDepth: 1, InFlightRequests: 0, KVUtilization: 0.0},
		},
	}
	req := &sim.Request{ID: "test"}
	decision := alg.Route(req, state)

	if decision.TargetInstance != "ep-b" {
		t.Errorf("cubic load penalty: expected ep-b (lower load), got %q", decision.TargetInstance)
	}
	// Verify penalty was applied: ep-a's score should be < ep-b's score
	if decision.Scores["ep-a"] >= decision.Scores["ep-b"] {
		t.Errorf("cubic load penalty: scores[ep-a]=%.4f should be < scores[ep-b]=%.4f",
			decision.Scores["ep-a"], decision.Scores["ep-b"])
	}
}

// TestEvolvedAlgorithm_KVPressurePenalty verifies BC-3:
// KV pressure penalty fires when KVUtilization > 0.82.
func TestEvolvedAlgorithm_KVPressurePenalty(t *testing.T) {
	alg := newEvolvedAlgorithm()

	// Equal loads — only KV utilization differs.
	// ep-a: KV=0.90 (> 0.82, penalty fires); ep-b: KV=0.50 (no penalty)
	state := &sim.RouterState{
		Snapshots: []sim.RoutingSnapshot{
			{ID: "ep-a", QueueDepth: 2, InFlightRequests: 0, KVUtilization: 0.90},
			{ID: "ep-b", QueueDepth: 2, InFlightRequests: 0, KVUtilization: 0.50},
		},
	}
	req := &sim.Request{ID: "test"}
	decision := alg.Route(req, state)

	if decision.TargetInstance != "ep-b" {
		t.Errorf("KV pressure penalty: expected ep-b (lower KV), got %q", decision.TargetInstance)
	}
	// Penalty = max(0.3, 1 - (0.90-0.82)*2) = max(0.3, 0.84) = 0.84
	// Scores from base weighted scoring should be multiplied by ~0.84 for ep-a.
	if decision.Scores["ep-a"] >= decision.Scores["ep-b"] {
		t.Errorf("KV pressure penalty: scores[ep-a]=%.4f should be < scores[ep-b]=%.4f",
			decision.Scores["ep-a"], decision.Scores["ep-b"])
	}
}

// TestEvolvedAlgorithm_CacheAffinityBonus verifies that the cache affinity bonus
// fires when both SessionID is set AND CacheHitRate > 0.35.
func TestEvolvedAlgorithm_CacheAffinityBonus(t *testing.T) {
	alg := newEvolvedAlgorithm()

	// ep-a: high CacheHitRate (bonus fires with session); ep-b: no cache hit
	// Equal loads to isolate cache effect.
	state := &sim.RouterState{
		Snapshots: []sim.RoutingSnapshot{
			{ID: "ep-a", QueueDepth: 2, InFlightRequests: 0, KVUtilization: 0.3, CacheHitRate: 0.8},
			{ID: "ep-b", QueueDepth: 2, InFlightRequests: 0, KVUtilization: 0.3, CacheHitRate: 0.0},
		},
	}
	reqWithSession := &sim.Request{ID: "test", SessionID: "session-123"}
	decisionWithSession := alg.Route(reqWithSession, state)

	// With session + high CacheHitRate: ep-a should score higher (bonus fires)
	if decisionWithSession.Scores["ep-a"] <= decisionWithSession.Scores["ep-b"] {
		t.Errorf("cache affinity bonus (with session): scores[ep-a]=%.4f should be > scores[ep-b]=%.4f",
			decisionWithSession.Scores["ep-a"], decisionWithSession.Scores["ep-b"])
	}

	// Without session: bonus should NOT fire — ep-a and ep-b should have equal base scores
	reqNoSession := &sim.Request{ID: "test"}
	decisionNoSession := alg.Route(reqNoSession, state)
	diff := math.Abs(decisionNoSession.Scores["ep-a"] - decisionNoSession.Scores["ep-b"])
	if diff > 1e-9 {
		t.Errorf("cache affinity bonus (no session): scores should be equal, got diff=%.9f", diff)
	}
}

// TestEvolvedAlgorithm_EmptySnapshots verifies C3 fix:
// Route() returns gracefully on empty snapshot list (no panic).
func TestEvolvedAlgorithm_EmptySnapshots(t *testing.T) {
	alg := newEvolvedAlgorithm()
	state := &sim.RouterState{Snapshots: []sim.RoutingSnapshot{}}
	req := &sim.Request{ID: "test"}

	// Must not panic
	decision := alg.Route(req, state)
	if decision.Reason != "no-endpoints" {
		t.Errorf("empty snapshots: expected Reason=%q, got %q", "no-endpoints", decision.Reason)
	}
	if len(decision.Scores) != 0 {
		t.Errorf("empty snapshots: expected empty Scores, got %v", decision.Scores)
	}
}

// TestKendallTau_KeyMismatch verifies C1 fix: KendallTau returns error on key mismatch.
func TestKendallTau_KeyMismatch(t *testing.T) {
	sim := map[string]float64{"ep-a": 0.9, "ep-b": 0.5}
	prod := map[string]float64{"ep-a": 0.8, "ep-c": 0.6} // ep-c instead of ep-b

	_, err := KendallTau(sim, prod)
	if err == nil {
		t.Error("KendallTau: expected error on key mismatch, got nil")
	}

	_, err = MaxAbsDiff(sim, prod)
	if err == nil {
		t.Error("MaxAbsDiff: expected error on key mismatch, got nil")
	}
}

// TestKendallTau_Correctness verifies basic tau-a correctness.
func TestKendallTau_Correctness(t *testing.T) {
	// Perfectly concordant: both rank a > b > c
	sim := map[string]float64{"a": 0.9, "b": 0.6, "c": 0.3}
	prod := map[string]float64{"a": 0.8, "b": 0.5, "c": 0.2}
	tau, err := KendallTau(sim, prod)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if math.Abs(tau-1.0) > 1e-9 {
		t.Errorf("perfectly concordant: expected tau=1.0, got %.6f", tau)
	}

	// Perfectly discordant: sim ranks a > b > c, prod ranks c > b > a
	prod2 := map[string]float64{"a": 0.2, "b": 0.5, "c": 0.8}
	tau2, err := KendallTau(sim, prod2)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if math.Abs(tau2-(-1.0)) > 1e-9 {
		t.Errorf("perfectly discordant: expected tau=-1.0, got %.6f", tau2)
	}
}
```
