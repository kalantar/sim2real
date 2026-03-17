package harness

import (
	"context"
	"testing"

	fwkdl "sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/datalayer"
	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/scheduling"
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

// TestEvolvedScorerNilMetrics verifies that endpoints with nil metrics score 0.0
// and that a normal (non-nil metrics) endpoint alongside still receives a score.
func TestEvolvedScorerNilMetrics(t *testing.T) {
	alg := newEvolvedAlgorithm()
	scorer := NewEvolvedScorer(alg).WithName("nil-metrics-test")

	nilMetricsEp := &testEndpointForScorer{
		id:      "ep-nil",
		metrics: nil,
	}
	normalEp := &testEndpointForScorer{
		id: "ep-normal",
		metrics: &fwkdl.Metrics{
			WaitingQueueSize:    0,
			RunningRequestsSize: 1,
			KVCacheUsagePercent: 30.0,
		},
	}

	endpoints := []scheduling.Endpoint{nilMetricsEp, normalEp}
	scores := scorer.Score(context.Background(), nil, nil, endpoints)

	if len(scores) != 2 {
		t.Fatalf("expected 2 scores, got %d", len(scores))
	}

	if scores[nilMetricsEp] != 0.0 {
		t.Errorf("nil-metrics endpoint: expected score 0.0, got %f", scores[nilMetricsEp])
	}

	if _, ok := scores[normalEp]; !ok {
		t.Error("normal endpoint: missing score in result map")
	}
}
