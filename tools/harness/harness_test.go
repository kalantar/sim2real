package harness

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	sim "github.com/inference-sim/inference-sim/sim"
	fwkdl "sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/datalayer"
	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/scheduling"
)

func TestEquivalenceTrivial(t *testing.T) {
	// BC-2: 2 endpoints with different load, non-zero scores.
	// pod-a has InFlightRequests:3, ensuring EffectiveLoad() > 0 regardless of the exact formula.
	alg := &trivialAlgorithm{}
	tuples := []TestTuple{
		{
			Request: sim.Request{ID: "req-1"},
			State: sim.RouterState{
				Snapshots: []sim.RoutingSnapshot{
					{ID: "pod-a", QueueDepth: 2, BatchSize: 1, InFlightRequests: 3},
					{ID: "pod-b", QueueDepth: 0, BatchSize: 0, InFlightRequests: 0},
				},
			},
		},
	}

	results := RunTuples(alg, tuples)
	if len(results) != 1 {
		t.Fatalf("expected 1 result, got %d", len(results))
	}
	r := results[0]
	if r.Error != nil {
		t.Fatalf("unexpected error: %v", r.Error)
	}
	if len(r.SimScores) == 0 {
		t.Fatal("expected non-empty scores")
	}
	// pod-b (load 0) should score higher than pod-a (load >= 3)
	if r.SimScores["pod-a"] >= r.SimScores["pod-b"] {
		t.Errorf("expected pod-b > pod-a, got pod-a=%f pod-b=%f",
			r.SimScores["pod-a"], r.SimScores["pod-b"])
	}
	// All scores must be > 0
	for id, score := range r.SimScores {
		if score <= 0 {
			t.Errorf("expected positive score for %s, got %f", id, score)
		}
	}
}

func TestStaleHashAbortsParsing(t *testing.T) {
	// BC-11 + Cross-PR contract #1: content hash mismatch detected
	repoRoot := t.TempDir()
	workspaceDir := t.TempDir()

	// Create source file with EVOLVE-BLOCK
	sourceDir := filepath.Join(repoRoot, "routing")
	if err := os.MkdirAll(sourceDir, 0o755); err != nil {
		t.Fatal(err)
	}
	originalSource := "line1\n// EVOLVE-BLOCK-START\noriginal logic\n// EVOLVE-BLOCK-END\nline5"
	sourcePath := filepath.Join(sourceDir, "best_program.py")
	if err := os.WriteFile(sourcePath, []byte(originalSource), 0o644); err != nil {
		t.Fatal(err)
	}

	// Compute hash of original EVOLVE-BLOCK (lines 2-4, 1-based)
	originalBlock := "// EVOLVE-BLOCK-START\noriginal logic\n// EVOLVE-BLOCK-END"
	hash := sha256.Sum256([]byte(originalBlock))
	originalHash := hex.EncodeToString(hash[:])

	// Write algorithm_summary.json with the original hash
	summary := map[string]interface{}{
		"algorithm_name":             "test",
		"evolve_block_source":        "routing/best_program.py:2-4",
		"evolve_block_content_hash":  originalHash,
		"signals":                    []interface{}{},
		"composite_signals":          []interface{}{},
		"metrics":                    map[string]interface{}{"combined_score": 0},
		"scope_validation_passed":    true,
		"mapping_artifact_version":   "1.0",
		"fidelity_checked":           true,
	}
	summaryBytes, err := json.Marshal(summary)
	if err != nil {
		t.Fatal(err)
	}
	summaryPath := filepath.Join(workspaceDir, "algorithm_summary.json")
	if err := os.WriteFile(summaryPath, summaryBytes, 0o644); err != nil {
		t.Fatal(err)
	}

	// Verify loading works with matching hash
	_, err = LoadAlgorithm(summaryPath, repoRoot)
	if err != nil {
		t.Fatalf("expected successful load with matching hash, got: %v", err)
	}

	// Modify the source file (simulate drift)
	modifiedSource := "line1\n// EVOLVE-BLOCK-START\nMODIFIED logic\n// EVOLVE-BLOCK-END\nline5"
	if err := os.WriteFile(sourcePath, []byte(modifiedSource), 0o644); err != nil {
		t.Fatal(err)
	}

	// LoadAlgorithm should fail with hash mismatch
	_, err = LoadAlgorithm(summaryPath, repoRoot)
	if err == nil {
		t.Fatal("expected error for stale hash, got nil")
	}
	if !strings.Contains(err.Error(), "hash mismatch") {
		t.Errorf("expected 'hash mismatch' in error, got: %v", err)
	}
}

func TestRunTuplesPanicRecovery(t *testing.T) {
	// BC-12: panic in Algorithm.Route is captured, not propagated
	panickingAlg := &panicAlgorithm{}
	tuples := []TestTuple{
		{
			Request: sim.Request{ID: "req-panic"},
			State: sim.RouterState{
				Snapshots: []sim.RoutingSnapshot{{ID: "pod-a"}},
			},
		},
		{
			Request: sim.Request{ID: "req-ok"},
			State: sim.RouterState{
				Snapshots: []sim.RoutingSnapshot{{ID: "pod-b", QueueDepth: 1}},
			},
		},
	}

	results := RunTuples(panickingAlg, tuples)
	if len(results) != 2 {
		t.Fatalf("expected 2 results, got %d", len(results))
	}
	if results[0].Error == nil {
		t.Error("expected error for panicking tuple")
	}
	if !strings.Contains(results[0].Error.Error(), "panic") {
		t.Errorf("expected 'panic' in error message, got: %v", results[0].Error)
	}
	if results[1].Error != nil {
		t.Errorf("expected no error for second tuple, got: %v", results[1].Error)
	}
}

func TestKVUtilizationNormalization(t *testing.T) {
	// Cross-PR contract #2: KVCacheUsagePercent (0-100) must be divided by 100
	prodValue := 75.0
	normalized := NormalizeKVUtilization(prodValue)
	if normalized < 0.0 || normalized > 1.0 {
		t.Errorf("normalized KVUtilization out of [0,1] range: %f", normalized)
	}
	if normalized != 0.75 {
		t.Errorf("expected 0.75, got %f", normalized)
	}

	// Boundary cases
	for _, tc := range []struct{ prod, expected float64 }{
		{0.0, 0.0},
		{100.0, 1.0},
		{50.0, 0.5},
		{-5.0, 0.0},
		{100.5, 1.0},
		{200.0, 1.0},
	} {
		got := NormalizeKVUtilization(tc.prod)
		if got != tc.expected {
			t.Errorf("NormalizeKVUtilization(%f): expected %f, got %f",
				tc.prod, tc.expected, got)
		}
	}
}

func TestUnknownSignalTypeRejection(t *testing.T) {
	// Cross-PR contract #3: signals with type "unknown" must be rejected
	summaryJSON := map[string]interface{}{
		"algorithm_name":             "test",
		"evolve_block_source":        "routing/best_program.py:1-1",
		"evolve_block_content_hash":  "deadbeef",
		"signals": []interface{}{
			map[string]interface{}{
				"name": "UnknownSignal",
				"type": "unknown",
			},
		},
		"composite_signals":        []interface{}{},
		"metrics":                  map[string]interface{}{"combined_score": 0},
		"scope_validation_passed":  true,
		"mapping_artifact_version": "1.0",
		"fidelity_checked":         true,
	}
	data, err := json.Marshal(summaryJSON)
	if err != nil {
		t.Fatal(err)
	}
	err = ValidateSignalTypes(data)
	if err == nil {
		t.Fatal("expected error for signal with type 'unknown', got nil")
	}
	if !strings.Contains(err.Error(), "unknown") {
		t.Errorf("expected 'unknown' in error message, got: %v", err)
	}
}

func TestCrossLanguageHashConsistency(t *testing.T) {
	// Section E(c): verify Go hash matches transfer_cli.py extract's hash
	repoRoot := findRepoRoot(t)
	venvPython := filepath.Join(repoRoot, ".venv", "bin", "python")
	if _, err := os.Stat(venvPython); err != nil {
		t.Skip("requires Python venv at .venv/bin/python")
	}

	// Use the actual routing/best_program.py and run extract
	routingDir := filepath.Join(repoRoot, "routing")
	if _, err := os.Stat(filepath.Join(routingDir, "best_program.py")); err != nil {
		t.Skip("requires routing/best_program.py")
	}

	// Run extract to get the Python-computed hash.
	// extract always writes to workspace/ relative to the repo root.
	cmd := exec.Command(venvPython, filepath.Join(repoRoot, "tools", "transfer_cli.py"), "extract", routingDir)
	cmd.Dir = repoRoot
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("extract command failed: %v\noutput: %s", err, output)
	}

	// Read the summary written by extract
	summaryPath := filepath.Join(repoRoot, "workspace", "algorithm_summary.json")
	summaryData, err := os.ReadFile(summaryPath)
	if err != nil {
		t.Fatalf("read algorithm_summary.json: %v", err)
	}

	var summary struct {
		EvolveBlockSource      string `json:"evolve_block_source"`
		EvolveBlockContentHash string `json:"evolve_block_content_hash"`
	}
	if err := json.Unmarshal(summaryData, &summary); err != nil {
		t.Fatalf("parse algorithm_summary.json: %v", err)
	}

	// Now recompute hash in Go using the same logic as LoadAlgorithm
	parts := strings.SplitN(summary.EvolveBlockSource, ":", 2)
	if len(parts) != 2 {
		t.Fatalf("invalid evolve_block_source: %q", summary.EvolveBlockSource)
	}
	sourcePath := parts[0]
	// Handle absolute paths from extract (temp dir) vs relative paths
	if !filepath.IsAbs(sourcePath) {
		sourcePath = filepath.Join(repoRoot, sourcePath)
	}
	rangeParts := strings.SplitN(parts[1], "-", 2)
	if len(rangeParts) != 2 {
		t.Fatalf("invalid line range: %q", parts[1])
	}
	startLine := mustAtoi(t, rangeParts[0])
	endLine := mustAtoi(t, rangeParts[1])

	sourceData, err := os.ReadFile(sourcePath)
	if err != nil {
		t.Fatalf("read source: %v", err)
	}
	normalized := strings.ReplaceAll(string(sourceData), "\r\n", "\n")
	lines := strings.Split(normalized, "\n")
	if startLine < 1 || endLine > len(lines) {
		t.Fatalf("line range %d-%d out of bounds (%d lines)", startLine, endLine, len(lines))
	}
	block := strings.Join(lines[startLine-1:endLine], "\n")
	goHash := sha256.Sum256([]byte(block))
	goHashStr := hex.EncodeToString(goHash[:])

	if goHashStr != summary.EvolveBlockContentHash {
		t.Errorf("cross-language hash mismatch:\n  Python: %s\n  Go:     %s",
			summary.EvolveBlockContentHash, goHashStr)
	}
}

type panicAlgorithm struct {
	callCount int
}

func (a *panicAlgorithm) Route(req *sim.Request, state *sim.RouterState) sim.RoutingDecision {
	a.callCount++
	if a.callCount == 1 {
		panic("intentional test panic")
	}
	scores := map[string]float64{}
	for _, snap := range state.Snapshots {
		scores[snap.ID] = 0.5
	}
	return sim.NewRoutingDecisionWithScores(state.Snapshots[0].ID, "ok", scores)
}

// TestEquivalence is a convenience dispatcher for local development.
// NOTE: validate.md (K.10) runs suites independently via separate go test -run commands.
// Suite A requires the suitea build tag and pipeline artifacts:
//
//	go test -tags suitea -run TestSuiteA_KendallTau ./tools/harness/...
func TestEquivalence(t *testing.T) {
	t.Run("SuiteB", TestSuiteB_StalenessStability)
	t.Run("SuiteC_Concurrent", TestSuiteC_ConcurrentDeterminism)
	t.Run("SuiteC_PileOn", TestSuiteC_PileOn)
}

// findRepoRoot walks up from the working directory to find the repo root (contains CLAUDE.md).
func findRepoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "CLAUDE.md")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("could not find repo root (no CLAUDE.md found)")
		}
		dir = parent
	}
}

func TestLoadAlgorithmReturnsEvolved(t *testing.T) {
	repoRoot := findRepoRoot(t)
	summaryPath := filepath.Join(repoRoot, "workspace", "algorithm_summary.json")
	if _, err := os.Stat(summaryPath); err != nil {
		t.Skip("requires workspace/algorithm_summary.json (run extract first)")
	}

	alg, err := LoadAlgorithm(summaryPath, repoRoot)
	if err != nil {
		t.Skipf("workspace/algorithm_summary.json exists but LoadAlgorithm failed (stale artifact? re-run extract): %v", err)
	}

	// High-load vs low-load: evolved algorithm should prefer lower load
	highLoad := sim.RouterState{
		Snapshots: []sim.RoutingSnapshot{
			{ID: "heavy", QueueDepth: 6, InFlightRequests: 2}, // load=8 → hard penalty
			{ID: "light", QueueDepth: 0, InFlightRequests: 1}, // load=1
		},
	}
	decision := alg.Route(&sim.Request{ID: "r1"}, &highLoad)
	if decision.TargetInstance != "light" {
		t.Errorf("expected 'light' (lower load), got %q", decision.TargetInstance)
	}

	// BC-3: KV pressure penalty fires when KVUtilization > 0.82
	kvState := sim.RouterState{
		Snapshots: []sim.RoutingSnapshot{
			{ID: "high-kv", QueueDepth: 0, KVUtilization: 0.90},
			{ID: "low-kv",  QueueDepth: 0, KVUtilization: 0.50},
		},
	}
	kvDecision := alg.Route(&sim.Request{ID: "r2"}, &kvState)
	if kvDecision.TargetInstance != "low-kv" {
		t.Errorf("expected 'low-kv' (lower KV), got %q", kvDecision.TargetInstance)
	}
	if kvDecision.Scores["high-kv"] >= kvDecision.Scores["low-kv"] {
		t.Errorf("expected high-kv score < low-kv score; got high=%f low=%f",
			kvDecision.Scores["high-kv"], kvDecision.Scores["low-kv"])
	}
}

// TestEvolvedAlgorithmSingleEndpoint verifies that a single endpoint still receives
// a non-zero score and that the KV penalty fires correctly when KVUtilization > 0.82.
// BC-I11: single-endpoint edge case.
func TestEvolvedAlgorithmSingleEndpoint(t *testing.T) {
	alg := newEvolvedAlgorithm()

	state := sim.RouterState{
		Snapshots: []sim.RoutingSnapshot{
			{ID: "solo", QueueDepth: 0, InFlightRequests: 0, KVUtilization: 0.90},
		},
	}
	decision := alg.Route(&sim.Request{ID: "r-single"}, &state)

	score, ok := decision.Scores["solo"]
	if !ok {
		t.Fatal("expected score for 'solo' endpoint, got none")
	}
	if score <= 0.0 {
		t.Errorf("expected positive score for sole endpoint, got %f", score)
	}
	// KV penalty fires: max(0.3, 1-(0.90-0.82)*2) = max(0.3, 0.84) = 0.84
	// Base score may vary but the KV penalty multiplies it down from 1.0.
	if score >= 1.0 {
		t.Errorf("expected score < 1.0 (KV penalty applied), got %f", score)
	}
}

// TestEvolvedAlgorithmKVPenaltyBoundary verifies that the KV penalty does NOT fire
// at exactly KVUtilization=0.82 (condition is strictly > 0.82).
// BC-I12: penalty boundary condition.
func TestEvolvedAlgorithmKVPenaltyBoundary(t *testing.T) {
	alg := newEvolvedAlgorithm()

	state := sim.RouterState{
		Snapshots: []sim.RoutingSnapshot{
			{ID: "ep-0", QueueDepth: 0, InFlightRequests: 0, KVUtilization: 0.82},
			{ID: "ep-1", QueueDepth: 0, InFlightRequests: 0, KVUtilization: 0.82},
		},
	}
	decision := alg.Route(&sim.Request{ID: "r-boundary"}, &state)

	score0, ok0 := decision.Scores["ep-0"]
	score1, ok1 := decision.Scores["ep-1"]
	if !ok0 || !ok1 {
		t.Fatalf("expected scores for both endpoints; ep-0 ok=%v ep-1 ok=%v", ok0, ok1)
	}
	if score0 != score1 {
		t.Errorf("expected equal scores at KV=0.82 (penalty does not fire); got ep-0=%f ep-1=%f", score0, score1)
	}
}

func mustAtoi(t *testing.T, s string) int {
	t.Helper()
	result, err := strconv.Atoi(s)
	if err != nil {
		t.Fatalf("atoi %q: %v", s, err)
	}
	return result
}

func TestEvolvedScorerContract(t *testing.T) {
	alg := &trivialAlgorithm{}
	scorer := NewEvolvedScorer(alg).WithName("test-scorer")

	// TypedName
	tn := scorer.TypedName()
	if tn.Type != EvolvedScorerType {
		t.Errorf("TypedName.Type = %q, want %q", tn.Type, EvolvedScorerType)
	}
	if tn.Name != "test-scorer" {
		t.Errorf("TypedName.Name = %q, want %q", tn.Name, "test-scorer")
	}

	// Category
	if scorer.Category() != scheduling.Distribution {
		t.Errorf("Category() = %v, want Distribution", scorer.Category())
	}

	// Score returns 1.0 for each endpoint (trivialAlgorithm with zero-load mockEndpoint)
	endpoints := []scheduling.Endpoint{
		&mockEndpoint{name: "ep-a"},
		&mockEndpoint{name: "ep-b"},
	}
	scores := scorer.Score(context.Background(), nil, nil, endpoints)
	if len(scores) != len(endpoints) {
		t.Fatalf("Score returned %d entries, want %d", len(scores), len(endpoints))
	}
	for _, ep := range endpoints {
		score, ok := scores[ep]
		if !ok {
			t.Errorf("missing score for endpoint")
			continue
		}
		if score != 1.0 {
			t.Errorf("score = %f, want 1.0", score)
		}
	}

	// Empty endpoints returns empty map (not nil)
	emptyScores := scorer.Score(context.Background(), nil, nil, nil)
	if emptyScores == nil {
		t.Error("Score(nil endpoints) returned nil, want empty map")
	}
	if len(emptyScores) != 0 {
		t.Errorf("Score(nil endpoints) returned %d entries, want 0", len(emptyScores))
	}
}

func TestEvolvedScorerScoresCorrectly(t *testing.T) {
	// BC-4: metric translation; BC-5: session header.
	alg := newEvolvedAlgorithm()
	scorer := NewEvolvedScorer(alg).WithName("test")

	heavy := &testEndpointForScorer{
		id: "heavy",
		metrics: &fwkdl.Metrics{
			WaitingQueueSize:    5,
			RunningRequestsSize: 3, // EffectiveLoad=8 → hard penalty (load>7)
			KVCacheUsagePercent: 50.0,
		},
	}
	light := &testEndpointForScorer{
		id: "light",
		metrics: &fwkdl.Metrics{
			WaitingQueueSize:    0,
			RunningRequestsSize: 1, // EffectiveLoad=1
			KVCacheUsagePercent: 30.0,
		},
	}

	scores := scorer.Score(context.Background(), nil, nil, []scheduling.Endpoint{heavy, light})
	if len(scores) != 2 {
		t.Fatalf("expected 2 scores, got %d", len(scores))
	}
	if scores[heavy] >= scores[light] {
		t.Errorf("expected light > heavy; got heavy=%f light=%f", scores[heavy], scores[light])
	}

	// BC-5: session header extraction
	req := &scheduling.LLMRequest{
		RequestId: "req-sess",
		Headers:   map[string]string{"x-session-token": "sess-abc"},
	}
	scoresWithSess := scorer.Score(context.Background(), nil, req, []scheduling.Endpoint{heavy, light})
	if len(scoresWithSess) != 2 {
		t.Fatalf("session request: expected 2 scores, got %d", len(scoresWithSess))
	}
}

func TestNewEvolvedScorerNilPanics(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic for nil Algorithm, got none")
		}
	}()
	NewEvolvedScorer(nil)
}

func TestLoadAlgorithmErrorPaths(t *testing.T) {
	repoRoot := t.TempDir()
	workspaceDir := t.TempDir()

	// Create a valid source file for cases that need it
	sourceDir := filepath.Join(repoRoot, "routing")
	if err := os.MkdirAll(sourceDir, 0o755); err != nil {
		t.Fatal(err)
	}
	sourceContent := "line1\n// EVOLVE-BLOCK-START\nlogic\n// EVOLVE-BLOCK-END\nline5"
	if err := os.WriteFile(filepath.Join(sourceDir, "best_program.py"), []byte(sourceContent), 0o644); err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name      string
		summary   map[string]interface{}
		wantErr   string
	}{
		{
			name:    "missing content hash",
			summary: map[string]interface{}{"evolve_block_source": "routing/best_program.py:2-4", "evolve_block_content_hash": "", "signals": []interface{}{}},
			wantErr: "missing required field 'evolve_block_content_hash'",
		},
		{
			name:    "missing source",
			summary: map[string]interface{}{"evolve_block_source": "", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
			wantErr: "missing required field 'evolve_block_source'",
		},
		{
			name:    "invalid source format (no colon)",
			summary: map[string]interface{}{"evolve_block_source": "routing/best_program.py", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
			wantErr: "invalid evolve_block_source format",
		},
		{
			name:    "path traversal",
			summary: map[string]interface{}{"evolve_block_source": "../../../etc/passwd:1-1", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
			wantErr: "escapes repo root",
		},
		{
			name:    "invalid line range format",
			summary: map[string]interface{}{"evolve_block_source": "routing/best_program.py:2", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
			wantErr: "invalid line range format",
		},
		{
			name:    "invalid start line",
			summary: map[string]interface{}{"evolve_block_source": "routing/best_program.py:abc-4", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
			wantErr: "invalid start line",
		},
		{
			name:    "invalid end line",
			summary: map[string]interface{}{"evolve_block_source": "routing/best_program.py:2-xyz", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
			wantErr: "invalid end line",
		},
		{
			name:    "line range out of bounds",
			summary: map[string]interface{}{"evolve_block_source": "routing/best_program.py:1-999", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
			wantErr: "line range 1-999 out of bounds",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			data, err := json.Marshal(tc.summary)
			if err != nil {
				t.Fatal(err)
			}
			summaryPath := filepath.Join(workspaceDir, tc.name+".json")
			if err := os.WriteFile(summaryPath, data, 0o644); err != nil {
				t.Fatal(err)
			}
			_, err = LoadAlgorithm(summaryPath, repoRoot)
			if err == nil {
				t.Fatalf("expected error containing %q, got nil", tc.wantErr)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("expected error containing %q, got: %v", tc.wantErr, err)
			}
		})
	}
}

// mockEndpoint satisfies scheduling.Endpoint for testing.
type mockEndpoint struct {
	name string
}

func (m *mockEndpoint) GetMetadata() *fwkdl.EndpointMetadata { return &fwkdl.EndpointMetadata{} }
func (m *mockEndpoint) GetMetrics() *fwkdl.Metrics           { return &fwkdl.Metrics{} }
func (m *mockEndpoint) String() string                       { return m.name }
func (m *mockEndpoint) Get(string) (fwkdl.Cloneable, bool)   { return nil, false }
func (m *mockEndpoint) Put(string, fwkdl.Cloneable)          {}
func (m *mockEndpoint) Keys() []string                       { return nil }
