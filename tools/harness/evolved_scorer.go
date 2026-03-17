package harness

import (
	"context"
	"fmt"
	"os"

	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/plugin"
	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/scheduling"

	sim "github.com/inference-sim/inference-sim/sim"
)

const EvolvedScorerType = "evolved-scorer"

// compile-time interface assertion
var _ scheduling.Scorer = &EvolvedScorer{}

// EvolvedScorer adapts the harness Algorithm to the production scheduling.Scorer interface.
// It translates production endpoint metrics to sim types, invokes Algorithm.Route(),
// and maps the resulting per-instance scores back to the production endpoint map.
//
// Signal translation (from workspace/signal_coverage.json and mapping artifact):
//   - endpoint.GetMetrics().WaitingQueueSize    → sim.RoutingSnapshot.QueueDepth
//     (used by base WeightedScoring via EffectiveLoad; not in EVOLVE-BLOCK directly)
//   - endpoint.GetMetrics().RunningRequestsSize → sim.RoutingSnapshot.InFlightRequests
//     (F-10 single-count: BatchSize intentionally omitted — defaults to 0)
//   - NormalizeKVUtilization(KVCacheUsagePercent) → sim.RoutingSnapshot.KVUtilization
//   Note: CacheHitRate and SessionID are not used by the new EVOLVE-BLOCK.
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
// and returns per-endpoint scores from the evolved algorithm; values are not clamped to [0, 1] and may be negative when KV utilization is high (KV penalty is subtractive).
//
// WARNING — scheduler clamping under full-cluster KV saturation:
// The production scheduler framework (enforceScoreRange in scheduler_profile.go) clamps
// all scores to [0, 1] before accumulation. When all endpoints simultaneously exceed 0.9
// KV utilization, the subtractive penalty can drive all scores below 0.0 — after clamping,
// every endpoint receives score 0.0, losing all differentiation. Operators should monitor
// for this condition under sustained cluster-wide KV pressure.
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
	seenIDs := make(map[string]struct{}, len(endpoints))

	for _, ep := range endpoints {
		m := ep.GetMetrics()
		if m == nil {
			result[ep] = 0.0
			continue
		}
		id := ep.String()
		if _, dup := seenIDs[id]; dup {
			fmt.Fprintf(os.Stderr, "EvolvedScorer.Score: duplicate endpoint ID %q detected; scores may be overwritten\n", id)
		}
		seenIDs[id] = struct{}{}
		snap := sim.RoutingSnapshot{
			ID:               id,
			QueueDepth:       m.WaitingQueueSize,
			InFlightRequests: m.RunningRequestsSize, // F-10: single-count, BatchSize=0
			KVUtilization:    NormalizeKVUtilization(m.KVCacheUsagePercent),
			// CacheHitRate: implicitly 0.0 (zero value) — no production field available.
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
		} else {
			fmt.Fprintf(os.Stderr, "EvolvedScorer.Score: unexpected missing score for endpoint ID %q; assigning 0.0\n", id)
			result[ep] = 0.0
		}
	}
	return result
}
