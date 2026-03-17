//go:build suitea

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
		RunningRequestsSize: e.snap.InFlightRequests,
		KVCacheUsagePercent: e.snap.KVUtilization * 100.0,
	}
}

func (e *suiteAEndpoint) String() string                          { return e.snap.ID }
func (e *suiteAEndpoint) Get(string) (fwkdl.Cloneable, bool)    { return nil, false }
func (e *suiteAEndpoint) Put(string, fwkdl.Cloneable)            {}
func (e *suiteAEndpoint) Keys() []string                          { return nil }

// generateCanonicalTuples creates N test tuples for Suite A.
// Canonical: BatchSize=0, CacheHitRate=0 (eliminates known semantic gaps).
func generateCanonicalTuples(n int) []TestTuple {
	rng := rand.New(rand.NewSource(42))
	tuples := make([]TestTuple, n)
	for i := range tuples {
		numEPs := 2 + rng.Intn(4) // 2–5 endpoints
		snaps := make([]sim.RoutingSnapshot, numEPs)
		for j := range snaps {
			snaps[j] = sim.RoutingSnapshot{
				ID:               fmt.Sprintf("ep-%d-%d", i, j),
				QueueDepth:       rng.Intn(9),
				InFlightRequests: rng.Intn(4),
				KVUtilization:    rng.Float64() * 0.95,
				CacheHitRate:     0.0,
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
func TestSuiteA_KendallTau(t *testing.T) {
	repoRoot := findRepoRoot(t)
	summaryPath := filepath.Join(repoRoot, "workspace", "algorithm_summary.json")
	if _, err := os.Stat(summaryPath); err != nil {
		t.Skip("requires workspace/algorithm_summary.json (run extract first)")
	}

	alg, err := LoadAlgorithm(summaryPath, repoRoot)
	if err != nil {
		t.Fatalf("LoadAlgorithm: %v", err)
	}

	params := blis.BLISWeightedScorerParameters{Enabled: true}
	prodScorer := blis.NewBLISWeightedScorer(context.Background(), params).WithName("suite-a")

	tuples := generateCanonicalTuples(200)
	tauSum := 0.0
	maxAbsErr := 0.0
	nonSkipped := 0

	for i, tuple := range tuples {
		simDecision := alg.Route(&tuple.Request, &tuple.State)
		if len(simDecision.Scores) == 0 {
			t.Logf("tuple %d: sim algorithm returned no scores, skipping", i)
			continue
		}
		nonSkipped++

		endpoints := make([]scheduling.Endpoint, len(tuple.State.Snapshots))
		for j, snap := range tuple.State.Snapshots {
			endpoints[j] = endpointFromSnap(snap)
		}
		rawProdScores := blis.ScoreEndpoints(context.Background(), prodScorer, nil, endpoints)

		prodScores := make(map[string]float64, len(rawProdScores))
		missingKeys := 0
		for _, snap := range tuple.State.Snapshots {
			namespacedKey := "/" + snap.ID
			if _, ok := rawProdScores[namespacedKey]; !ok {
				missingKeys++
			}
			prodScores[snap.ID] = rawProdScores[namespacedKey]
		}
		if missingKeys == len(tuple.State.Snapshots) {
			t.Fatalf("tuple %d: ALL %d prod score keys missing — NamespacedName format mismatch (expected '/<id>', got keys: %v)",
				i, len(tuple.State.Snapshots), mapKeys(rawProdScores))
		} else if missingKeys > 0 {
			t.Fatalf("tuple %d: %d of %d prod score keys missing",
				i, missingKeys, len(tuple.State.Snapshots))
		}

		tau := KendallTau(simDecision.Scores, prodScores)
		tauSum += tau

		absErr := MaxAbsDiff(simDecision.Scores, prodScores)
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

	if meanTau <= 0.8 {
		t.Errorf("Suite A FAIL: mean Kendall-tau = %.4f, want > 0.8", meanTau)
	}
}

// mapKeys returns the keys of a map for diagnostic logging.
func mapKeys(m map[string]float64) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	return keys
}
