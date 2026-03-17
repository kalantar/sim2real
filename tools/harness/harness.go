package harness

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime/debug"
	"strconv"
	"strings"

	sim "github.com/inference-sim/inference-sim/sim"
)

// Algorithm is an opaque handle to a loaded evolved algorithm.
// Mirrors the Route(Request, RouterState) → RoutingDecision signature from inference-sim.
type Algorithm interface {
	Route(req *sim.Request, state *sim.RouterState) sim.RoutingDecision
}

// TestTuple is the input format for equivalence tests.
type TestTuple struct {
	Request sim.Request
	State   sim.RouterState
}

// Result captures per-tuple test output.
type Result struct {
	Tuple       TestTuple
	SimScores   map[string]float64 // scores from sim algorithm
	ProdScores  map[string]float64 // scores from production scorer (populated by PR5)
	Passed      bool
	NoEndpoints bool // true when algorithm returned empty Scores due to no available endpoints
	Error       error
	ScoreDiffs  map[string]float64
}

// algorithmSummary is a subset of algorithm_summary.json fields needed by the harness.
type algorithmSummary struct {
	EvolveBlockSource      string           `json:"evolve_block_source"`
	EvolveBlockContentHash string           `json:"evolve_block_content_hash"`
	Signals                []summarySignal  `json:"signals"`
}

// summarySignal is a subset of algorithm_summary.json signals[] items.
type summarySignal struct {
	Name string `json:"name"`
	Type string `json:"type"`
}

// LoadAlgorithm loads an evolved algorithm by verifying the EVOLVE-BLOCK content hash.
// summaryPath: path to workspace/algorithm_summary.json
// repoRoot: repository root for resolving relative source paths
// Returns: Algorithm interface wrapping evolvedAlgorithm, which implements the full EVOLVE-BLOCK logic (KV penalty + inflight tiebreaker) from blis_router/best/best_program.go.
func LoadAlgorithm(summaryPath, repoRoot string) (Algorithm, error) {
	data, err := os.ReadFile(summaryPath)
	if err != nil {
		return nil, fmt.Errorf("read algorithm summary: %w", err)
	}
	var summary algorithmSummary
	if err := json.Unmarshal(data, &summary); err != nil {
		return nil, fmt.Errorf("parse algorithm summary: %w", err)
	}
	if summary.EvolveBlockContentHash == "" {
		return nil, fmt.Errorf("algorithm_summary.json missing required field 'evolve_block_content_hash'")
	}
	if summary.EvolveBlockSource == "" {
		return nil, fmt.Errorf("algorithm_summary.json missing required field 'evolve_block_source'")
	}

	// Parse source path and line range (format: "path/to/file.go:START-END")
	parts := strings.SplitN(summary.EvolveBlockSource, ":", 2)
	if len(parts) != 2 {
		return nil, fmt.Errorf("invalid evolve_block_source format: %q", summary.EvolveBlockSource)
	}
	var sourcePath string
	if filepath.IsAbs(parts[0]) {
		// Defensive: schema pattern rejects absolute paths, so schema-validated artifacts
		// never reach this branch. Retained for callers that bypass schema validation.
		sourcePath = parts[0]
	} else {
		sourcePath = filepath.Join(repoRoot, parts[0])
	}
	// Guard against path traversal
	absSource, err := filepath.Abs(sourcePath)
	if err != nil {
		return nil, fmt.Errorf("resolve absolute path for source %q: %w", sourcePath, err)
	}
	absRoot, err := filepath.Abs(repoRoot)
	if err != nil {
		return nil, fmt.Errorf("resolve absolute path for repo root %q: %w", repoRoot, err)
	}
	rel, err := filepath.Rel(absRoot, absSource)
	if err != nil || strings.HasPrefix(rel, "..") {
		return nil, fmt.Errorf("evolve_block_source path %q escapes repo root", parts[0])
	}
	rangeParts := strings.SplitN(parts[1], "-", 2)
	if len(rangeParts) != 2 {
		return nil, fmt.Errorf("invalid line range format: %q", parts[1])
	}
	startLine, err := strconv.Atoi(rangeParts[0])
	if err != nil {
		return nil, fmt.Errorf("invalid start line: %w", err)
	}
	endLine, err := strconv.Atoi(rangeParts[1])
	if err != nil {
		return nil, fmt.Errorf("invalid end line: %w", err)
	}

	// Read source file and extract EVOLVE-BLOCK lines
	sourceData, err := os.ReadFile(sourcePath)
	if err != nil {
		return nil, fmt.Errorf("read source file %s: %w", sourcePath, err)
	}
	// Normalize CRLF to LF before splitting — ensures hash matches transfer_cli.py
	normalized := strings.ReplaceAll(string(sourceData), "\r\n", "\n")
	lines := strings.Split(normalized, "\n")
	if startLine < 1 || endLine > len(lines) || startLine > endLine {
		return nil, fmt.Errorf("line range %d-%d out of bounds (file has %d lines)",
			startLine, endLine, len(lines))
	}
	blockLines := lines[startLine-1 : endLine]
	block := strings.Join(blockLines, "\n")

	// Verify content hash (must match transfer_cli.py extract algorithm exactly)
	hash := sha256.Sum256([]byte(block))
	computedHash := hex.EncodeToString(hash[:])
	if computedHash != summary.EvolveBlockContentHash {
		return nil, fmt.Errorf(
			"EVOLVE-BLOCK content hash mismatch: expected %s, computed %s — "+
				"source has changed since extraction, re-run extract stage",
			summary.EvolveBlockContentHash, computedHash)
	}

	return newEvolvedAlgorithm(), nil
}

// trivialAlgorithm is a retained reference implementation. It is no longer used by LoadAlgorithm (replaced by evolvedAlgorithm in this PR).
type trivialAlgorithm struct{}

func (a *trivialAlgorithm) Route(req *sim.Request, state *sim.RouterState) sim.RoutingDecision {
	if len(state.Snapshots) == 0 {
		return sim.RoutingDecision{Reason: "no-endpoints"}
	}
	scores := make(map[string]float64, len(state.Snapshots))
	for _, snap := range state.Snapshots {
		scores[snap.ID] = 1.0 / (1.0 + float64(snap.EffectiveLoad()))
	}
	bestID := state.Snapshots[0].ID
	bestScore := scores[bestID]
	for id, score := range scores {
		if score > bestScore {
			bestScore = score
			bestID = id
		}
	}
	return sim.NewRoutingDecisionWithScores(bestID, "trivial-inverse-load", scores)
}

// RunTuples executes tuples against the algorithm and returns per-tuple results.
// Per-tuple errors (including panics) are captured in Result.Error.
func RunTuples(alg Algorithm, tuples []TestTuple) []Result {
	results := make([]Result, len(tuples))
	for i, tuple := range tuples {
		results[i] = runOneTuple(alg, tuple)
	}
	return results
}

// NormalizeKVUtilization converts production KVCacheUsagePercent (0-100 scale)
// to simulation KVUtilization (0.0-1.0 scale) by dividing by 100.
// Values outside [0, 100] are silently clamped to the boundary; this is intentional
// because upstream metrics may briefly exceed nominal bounds during bursts.
// Cross-PR contract #2: Stage 3 generated code MUST apply this normalization.
func NormalizeKVUtilization(prodPercent float64) float64 {
	if prodPercent < 0 {
		prodPercent = 0
	} else if prodPercent > 100 {
		prodPercent = 100
	}
	return prodPercent / 100.0
}

// ValidateSignalTypes checks algorithm_summary.json for signals with type "unknown".
// Cross-PR contract #3: unknown-type signals must be rejected or handled explicitly.
func ValidateSignalTypes(data []byte) error {
	var summary algorithmSummary
	if err := json.Unmarshal(data, &summary); err != nil {
		return fmt.Errorf("parse algorithm summary: %w", err)
	}
	var unknowns []string
	for _, sig := range summary.Signals {
		if sig.Type == "unknown" {
			unknowns = append(unknowns, sig.Name)
		}
	}
	if len(unknowns) > 0 {
		return fmt.Errorf("signals with unknown type must be resolved before proceeding: %s",
			strings.Join(unknowns, ", "))
	}
	return nil
}

func runOneTuple(alg Algorithm, tuple TestTuple) (result Result) {
	result.Tuple = tuple
	defer func() {
		if r := recover(); r != nil {
			result.Error = fmt.Errorf("panic during Route: %v\n%s", r, debug.Stack())
		}
	}()
	decision := alg.Route(&tuple.Request, &tuple.State)
	result.SimScores = decision.Scores
	if len(result.SimScores) == 0 && decision.Reason == "no-endpoints" {
		result.NoEndpoints = true
		result.Passed = true
	} else if len(result.SimScores) == 0 {
		result.Passed = false
		result.Error = fmt.Errorf("algorithm returned empty scores with reason %q", decision.Reason)
	} else {
		result.Passed = true
	}
	return result
}
