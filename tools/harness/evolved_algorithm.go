package harness

import (
	sim "github.com/inference-sim/inference-sim/sim"
)

// evolvedAlgorithm implements Algorithm using the EVOLVE-BLOCK logic from
// blis_router/best/best_program.go (WeightedScoring.Route EVOLVE-BLOCK-START to
// EVOLVE-BLOCK-END).
//
// The new EVOLVE-BLOCK adds three techniques on top of the base WeightedScoring:
//  1. Adaptive prefix-affinity decay: when the best prefix-cached instance is
//     overloaded, decay its weight by 1/(1 + 0.6*load_delta).
//  2. KV pressure penalty (subtractive): scores[id] -= 0.5*(KVUtil-0.9)/0.1
//     when KVUtilization > 0.9. Fires at >0.9, NOT at exactly 0.9.
//  3. Fresh load tiebreaker: scores[id] += 0.01/(1+InFlightRequests).
//
// HARNESS SIMPLIFICATION: The adaptive prefix-affinity decay (technique 1) is
// omitted from this implementation. In Suite A canonical tuples, sim.Request.InputTokens
// is nil, causing all prefix-affinity scores to be 0.0 (totalBlocks==0 → no match).
// Since bestPrefixScore is always 0.0 ≤ 0.1, the decay branch never fires.
//
// IMPORTANT: sim.NewRoutingPolicy("weighted", ...) returns a WeightedScoring
// whose Route() method IS the full EVOLVE-BLOCK, including techniques 2
// (KV pressure penalty) and 3 (inflight tiebreaker). Delegating to a.base.Route()
// therefore runs all three techniques in one call. The scores returned in
// baseDecision.Scores already have the KV penalty subtracted and the tiebreaker
// added — do NOT re-apply them after calling a.base.Route().
//
// NOTE: This implementation does NOT use CacheHitRate, SessionID, or
// EffectiveLoad() directly (none are accessed in the new EVOLVE-BLOCK).
// DO NOT modify without re-running evolutionary optimization against
// blis_router/best/best_program.go.
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

// Route implements Algorithm. It runs the EVOLVE-BLOCK logic by delegating to
// a.base (sim.NewRoutingPolicy("weighted", ...)), whose Route() method IS the
// full EVOLVE-BLOCK including:
//  1. Adaptive prefix-affinity decay (never fires in Suite A; InputTokens=nil).
//  2. KV pressure penalty: subtract 0.5*(KVUtil-0.9)/0.1 when KVUtil > 0.9.
//  3. Fresh load tiebreaker: add 0.01/(1+InFlightRequests).
// The returned baseDecision.Scores already have all three techniques applied.
// The argmax here selects the winner and relabels the decision as "evolved".
//
// WARNING — observer-callback / prefix-affinity history poisoning:
// The call to a.base.Route() below fires WeightedScoring's internal observer
// callbacks, which record the final post-EVOLVE-BLOCK routing decision (the
// argmax after KV penalty and tiebreaker are applied) in the prefix-affinity
// scorer's session history — this is the actual evolved target, not a stale
// base-only argmax. In Suite A canonical tuples sim.Request.InputTokens is nil,
// so all prefix-affinity scores are 0.0 (totalBlocks==0 → no match) and the
// observer records no preference; the callback is harmless in that case.
// However, future test authors who construct requests with non-nil InputTokens
// must be aware: if the KV pressure penalty changes the argmax relative to a
// purely prefix-affinity-driven choice, the observer records the KV-adjusted
// target, which then influences prefix-affinity scores for subsequent requests
// in the same session. This is correct behavior (the observer records the actual
// routing target), but it can cause surprising prefix-affinity score drift in
// multi-request session tests.
func (a *evolvedAlgorithm) Route(req *sim.Request, state *sim.RouterState) sim.RoutingDecision {
	snapshots := state.Snapshots
	if len(snapshots) == 0 {
		panic("evolvedAlgorithm.Route: empty snapshots")
	}

	// Step 1: Delegate to base WeightedScoring, which IS the full EVOLVE-BLOCK.
	// baseDecision.Scores already contains scores with KV penalty (technique 2)
	// and inflight tiebreaker (technique 3) applied — do NOT re-apply them.
	baseDecision := a.base.Route(req, state)

	// Step 2: Argmax over the already-final scores — select instance with highest
	// score (first wins on tie). Relabel the decision as "evolved".
	scores := baseDecision.Scores
	bestScore := scores[snapshots[0].ID]
	bestIdx := 0
	for i, snap := range snapshots[1:] {
		if scores[snap.ID] > bestScore {
			bestScore = scores[snap.ID]
			bestIdx = i + 1
		}
	}

	return sim.NewRoutingDecisionWithScores(snapshots[bestIdx].ID, "evolved", scores)
}
