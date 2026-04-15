package sim

import "fmt"

// AdaptiveAdmission implements preemptive probabilistic shedding discovered via BLIS simulation.
// Uses the standard GAIE saturation formula but starts shedding at near-zero thresholds.
//
// Transfer to llm-d: implement requestcontrol.AdmissionPlugin, map signals per README.md.
//   - QueueDepth → pod.GetMetrics().WaitingQueueSize
//   - KVUtilization → pod.GetMetrics().KVCacheUsagePercent (already 0-1 despite the name)
//   - sloClass → request.Objectives.Priority (>=0 protected, -1 sheddable, <=-2 batch)
type AdaptiveAdmission struct {
	totalAdmitted int
	totalRejected int
}

func NewAdaptiveAdmission() *AdaptiveAdmission {
	return &AdaptiveAdmission{}
}

// saturation computes pool-average saturation per GAIE formula:
// avg across instances of max(queueDepth/5.0, kvUtil/0.8).
// Identical to GAIE's utilization/detector.go:computeUtilization.
func saturation(snapshots []RoutingSnapshot) float64 {
	n := len(snapshots)
	if n == 0 {
		return 0.0
	}
	var total float64
	for _, snap := range snapshots {
		qRatio := float64(snap.QueueDepth) / 5.0
		kvRatio := snap.KVUtilization / 0.8
		if qRatio > kvRatio {
			total += qRatio
		} else {
			total += kvRatio
		}
	}
	return total / float64(n)
}

// Admit implements AdmissionPolicy.
//
// Decision logic (iter11, priority-corrected):
//   - critical, standard: always admit (protected tiers, GAIE priority >= 0)
//   - sheddable (GAIE priority -50): most aggressive — ramp 0.005 → 0.05
//   - batch (GAIE priority -10): less aggressive — ramp 0.01 → 0.10
//
// The key insight: GAIE legacy waits until saturation=1.0 to shed. By then queues are deep
// and latency is ruined. This algorithm starts at near-zero saturation. The TIMING of
// shedding matters more than the total amount shed.
func (a *AdaptiveAdmission) Admit(req *Request, state *RouterState) (bool, string) {
	sat := saturation(state.Snapshots)

	switch req.SLOClass {
	case "critical", "standard":
		// Protected tiers: never reject.

	case "sheddable", "background":
		// Most aggressive shedding (lowest priority). Ramp from 0.005 to 0.05.
		if sat >= 0.005 {
			p := (sat - 0.005) / 0.045 // 0→1 over [0.005, 0.05]
			if p > 1.0 {
				p = 1.0
			}
			if a.pseudoRandom() < p {
				a.totalRejected++
				return false, fmt.Sprintf("adaptive: sheddable-shed sat=%.3f p=%.2f", sat, p)
			}
		}

	case "batch":
		// Less aggressive shedding (higher priority than sheddable). Ramp from 0.01 to 0.10.
		if sat >= 0.01 {
			p := (sat - 0.01) / 0.09 // 0→1 over [0.01, 0.10]
			if p > 1.0 {
				p = 1.0
			}
			if a.pseudoRandom() < p {
				a.totalRejected++
				return false, fmt.Sprintf("adaptive: batch-shed sat=%.3f p=%.2f", sat, p)
			}
		}
	}

	a.totalAdmitted++
	return true, ""
}

// pseudoRandom returns a deterministic pseudo-random value in [0, 1) based on request count.
// In production, replace with rand.Float64() for true randomness.
func (a *AdaptiveAdmission) pseudoRandom() float64 {
	ordinal := float64(a.totalAdmitted+a.totalRejected) / 100.0
	return ordinal - float64(int(ordinal))
}
