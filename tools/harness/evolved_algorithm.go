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
		base: sim.NewRoutingPolicy("weighted", sim.DefaultScorerConfigs(), 64, nil),
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
		panic("evolvedAlgorithm.Route: empty snapshots")
	}

	// Step 1: Get base weighted scores from the configured WeightedScoring.
	baseDecision := a.base.Route(req, state)

	// Copy to mutable map so we can apply penalties.
	scores := make(map[string]float64, len(snapshots))
	for id, s := range baseDecision.Scores {
		scores[id] = s
	}

	// Step 2: Find minLoad for penalty threshold computations.
	minLoad := float64(snapshots[0].EffectiveLoad())
	for _, snap := range snapshots[1:] {
		if l := float64(snap.EffectiveLoad()); l < minLoad {
			minLoad = l
		}
	}

	hasSession := req.SessionID != ""

	// Step 3: Apply EVOLVE-BLOCK penalty/bonus terms.
	for _, snap := range snapshots {
		load := float64(snap.EffectiveLoad())

		// Strong load penalty: cubic scaling strongly prefers least loaded.
		loadDelta := load - minLoad
		if loadDelta > 0.2 {
			loadPenalty := 1.0 / (1.0 + loadDelta*loadDelta*loadDelta*5.0)
			scores[snap.ID] *= loadPenalty
		}

		// Cache affinity for multi-turn sessions.
		if hasSession && snap.CacheHitRate > 0.35 {
			scores[snap.ID] *= (1.0 + snap.CacheHitRate*0.3)
		}

		// Memory pressure penalty: fires when KVUtilization > 0.82.
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
