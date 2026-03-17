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
// would cause data races detectable by -race.
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

	for i := 0; i < numDecisions; i++ {
		snaps := make([]sim.RoutingSnapshot, numEndpoints)
		for j := 0; j < numEndpoints; j++ {
			snaps[j] = sim.RoutingSnapshot{
				ID:         fmt.Sprintf("ep-%d", j),
				QueueDepth: 3,
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
