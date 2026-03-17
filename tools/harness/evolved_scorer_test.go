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
