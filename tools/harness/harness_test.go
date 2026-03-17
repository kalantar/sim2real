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
	sourceDir := filepath.Join(repoRoot, "blis_router", "best")
	if err := os.MkdirAll(sourceDir, 0o755); err != nil {
		t.Fatal(err)
	}
	originalSource := "line1\n// EVOLVE-BLOCK-START\noriginal logic\n// EVOLVE-BLOCK-END\nline5"
	sourcePath := filepath.Join(sourceDir, "best_program.go")
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
		"evolve_block_source":        "blis_router/best/best_program.go:2-4",
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
		"evolve_block_source":        "blis_router/best/best_program.go:1-1",
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

	// Use the actual blis_router/best/best_program.go and run extract
	routingDir := filepath.Join(repoRoot, "blis_router", "best")
	if _, err := os.Stat(filepath.Join(routingDir, "best_program.go")); err != nil {
		t.Skip("requires blis_router/best/best_program.go")
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

	// BC-3: KV pressure penalty fires when KVUtilization > 0.9 (new threshold)
	kvState := sim.RouterState{
		Snapshots: []sim.RoutingSnapshot{
			{ID: "high-kv", QueueDepth: 0, KVUtilization: 0.95},
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
// a score and that the KV penalty fires correctly when KVUtilization > 0.9.
// BC-I11: single-endpoint edge case.
func TestEvolvedAlgorithmSingleEndpoint(t *testing.T) {
	alg := newEvolvedAlgorithm()

	state := sim.RouterState{
		Snapshots: []sim.RoutingSnapshot{
			{ID: "solo", QueueDepth: 0, InFlightRequests: 0, KVUtilization: 0.95},
		},
	}
	decision := alg.Route(&sim.Request{ID: "r-single"}, &state)

	score, ok := decision.Scores["solo"]
	if !ok {
		t.Fatal("expected score for 'solo' endpoint, got none")
	}
	// KV penalty fires: scores[id] -= 0.5*(0.95-0.9)/0.1 = 0.25 (subtractive).
	// Base WeightedScoring score at KVUtil=0.95 ≈ 0.324; tiebreaker adds 0.01/(1+0)=0.01.
	// Unpenalized total ≈ 0.334; penalized ≈ 0.334 - 0.25 = 0.084.
	// Assert score < 0.3 (strictly below the unpenalized base ~0.334) to verify the penalty
	// actually fired — score < 1.0 alone is vacuously true with or without the penalty.
	// NOTE: do NOT assert score > 0.0; subtractive penalty can produce negative scores.
	if score >= 0.3 {
		t.Errorf("expected score < 0.3 (KV penalty fired: 0.5*(0.95-0.9)/0.1=0.25 subtracted from base ~0.334), got %f", score)
	}
	if decision.TargetInstance != "solo" {
		t.Errorf("expected TargetInstance='solo' (only endpoint), got %q", decision.TargetInstance)
	}
}

// TestEvolvedAlgorithmKVPenaltyBoundary verifies that the KV penalty does NOT fire
// at exactly KVUtilization=0.9 (condition is strictly > 0.9).
// BC-I12: penalty boundary condition.
func TestEvolvedAlgorithmKVPenaltyBoundary(t *testing.T) {
	alg := newEvolvedAlgorithm()

	state := sim.RouterState{
		Snapshots: []sim.RoutingSnapshot{
			{ID: "ep-0", QueueDepth: 0, InFlightRequests: 0, KVUtilization: 0.9},
			{ID: "ep-1", QueueDepth: 0, InFlightRequests: 0, KVUtilization: 0.9},
		},
	}
	decision := alg.Route(&sim.Request{ID: "r-boundary"}, &state)

	score0, ok0 := decision.Scores["ep-0"]
	score1, ok1 := decision.Scores["ep-1"]
	if !ok0 || !ok1 {
		t.Fatalf("expected scores for both endpoints; ep-0 ok=%v ep-1 ok=%v", ok0, ok1)
	}
	if score0 != score1 {
		t.Errorf("expected equal scores at KV=0.9 (penalty does not fire); got ep-0=%f ep-1=%f", score0, score1)
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
	// Verifies metric translation: WaitingQueueSize/RunningRequestsSize/KVCacheUsagePercent → sim fields.
	// Also verifies Score() handles a request with session headers without error.
	alg := newEvolvedAlgorithm()
	scorer := NewEvolvedScorer(alg).WithName("test")

	heavy := &testEndpointForScorer{
		id: "heavy",
		metrics: &fwkdl.Metrics{
			WaitingQueueSize:    5,
			RunningRequestsSize: 3, // higher load → lower base score from WeightedScoring
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

	// Verify Score() handles a request with session headers (no error expected).
	// Note: evolved_scorer.go no longer extracts SessionID from headers (removed in Task 5).
	// This sub-block tests that the scorer is robust to irrelevant headers.
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
	sourceDir := filepath.Join(repoRoot, "blis_router", "best")
	if err := os.MkdirAll(sourceDir, 0o755); err != nil {
		t.Fatal(err)
	}
	sourceContent := "line1\n// EVOLVE-BLOCK-START\nlogic\n// EVOLVE-BLOCK-END\nline5"
	if err := os.WriteFile(filepath.Join(sourceDir, "best_program.go"), []byte(sourceContent), 0o644); err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name      string
		summary   map[string]interface{}
		wantErr   string
	}{
		{
			name:    "missing content hash",
			summary: map[string]interface{}{"evolve_block_source": "blis_router/best/best_program.go:2-4", "evolve_block_content_hash": "", "signals": []interface{}{}},
			wantErr: "missing required field 'evolve_block_content_hash'",
		},
		{
			name:    "missing source",
			summary: map[string]interface{}{"evolve_block_source": "", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
			wantErr: "missing required field 'evolve_block_source'",
		},
		{
			name:    "invalid source format (no colon)",
			summary: map[string]interface{}{"evolve_block_source": "blis_router/best/best_program.go", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
			wantErr: "invalid evolve_block_source format",
		},
		{
			name:    "path traversal",
			summary: map[string]interface{}{"evolve_block_source": "../../../etc/passwd:1-1", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
			wantErr: "escapes repo root",
		},
		{
			name:    "invalid line range format",
			summary: map[string]interface{}{"evolve_block_source": "blis_router/best/best_program.go:2", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
			wantErr: "invalid line range format",
		},
		{
			name:    "invalid start line",
			summary: map[string]interface{}{"evolve_block_source": "blis_router/best/best_program.go:abc-4", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
			wantErr: "invalid start line",
		},
		{
			name:    "invalid end line",
			summary: map[string]interface{}{"evolve_block_source": "blis_router/best/best_program.go:2-xyz", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
			wantErr: "invalid end line",
		},
		{
			name:    "line range out of bounds",
			summary: map[string]interface{}{"evolve_block_source": "blis_router/best/best_program.go:1-999", "evolve_block_content_hash": "abc123", "signals": []interface{}{}},
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

// TestEvolvedAlgorithmInflightTiebreaker verifies BC-5: the 0.01/(1+InFlightRequests)
// tiebreaker favors the endpoint with fewer in-flight requests when all other scoring
// factors are equal.
//
// ISOLATION DESIGN: InFlightRequests feeds into EffectiveLoad (QueueDepth + BatchSize +
// InFlightRequests), which the base WeightedScoring load-balance scorer uses. Setting
// InFlightRequests=0 vs 5 with QueueDepth=0 for both would make EffectiveLoad 0 vs 5,
// causing large base score differences that mask the tiebreaker. To isolate the
// tiebreaker, we compensate with QueueDepth so EffectiveLoad is equal:
//   idle:  QueueDepth=5, InFlightRequests=0 → EffectiveLoad=5
//   busy:  QueueDepth=0, InFlightRequests=5 → EffectiveLoad=5
// Equal EffectiveLoad → equal base scores from load-balance scorer.
// Equal KV=0.0 → equal base scores from kv-utilization scorer.
// InputTokens=nil → prefix-affinity scores 0.0 for both.
// Only the tiebreaker term (0.01/(1+InFlightRequests)) then differentiates the scores.
func TestEvolvedAlgorithmInflightTiebreaker(t *testing.T) {
	alg := newEvolvedAlgorithm()
	state := sim.RouterState{
		Snapshots: []sim.RoutingSnapshot{
			// idle: QueueDepth=5, InFlightRequests=0 → EffectiveLoad=5 (equal to busy)
			{ID: "idle", QueueDepth: 5, InFlightRequests: 0, KVUtilization: 0.0},
			// busy: QueueDepth=0, InFlightRequests=5 → EffectiveLoad=5 (equal to idle)
			{ID: "busy", QueueDepth: 0, InFlightRequests: 5, KVUtilization: 0.0},
		},
	}
	decision := alg.Route(&sim.Request{}, &state)
	scores := decision.Scores
	if scores["idle"] <= scores["busy"] {
		t.Errorf("expected idle (InFlight=0) score > busy (InFlight=5) score; got idle=%f busy=%f",
			scores["idle"], scores["busy"])
	}
	// Verify tiebreaker magnitudes: 0.01/(1+0)=0.01 vs 0.01/(1+5)≈0.00167.
	// With equal base scores, the diff should be close to the tiebreaker delta alone.
	expectedIdle := 0.01 / (1.0 + 0)
	expectedBusy := 0.01 / (1.0 + 5)
	if diff := scores["idle"] - scores["busy"]; diff < (expectedIdle-expectedBusy)*0.99 {
		t.Errorf("tiebreaker delta too small: got %f, expected ~%f", diff, expectedIdle-expectedBusy)
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
