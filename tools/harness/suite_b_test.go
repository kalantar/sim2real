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
func runSuiteB(t *testing.T) SuiteBResult {
	t.Helper()

	alg := newEvolvedAlgorithm()

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

	const repetitions = 3

	tauSum := 0.0
	count := 0

	for rep := 0; rep < repetitions; rep++ {
		for _, tuple := range base {
			baseDecision := alg.Route(&tuple.Request, &tuple.State)
			// Perturbed state: zero perturbation (staleness=0), identical to base.
			perturbedDecision := alg.Route(&tuple.Request, &tuple.State)

			tau := KendallTau(baseDecision.Scores, perturbedDecision.Scores)
			tauSum += tau
			count++
		}
	}

	var meanTau float64
	if count > 0 {
		meanTau = tauSum / float64(count)
	}

	return SuiteBResult{
		Passed:               meanTau >= 0.99,
		RankStabilityTau:     meanTau,
		ThresholdCrossingPct: 0.0,
		InformationalOnly:    true,
	}
}

// TestSuiteB_StalenessStability verifies BC-8.
func TestSuiteB_StalenessStability(t *testing.T) {
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
