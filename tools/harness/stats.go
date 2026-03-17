package harness

import "sort"

// KendallTau computes Kendall's tau-a rank correlation between two score maps.
// Both maps must have identical key sets.
//
// Returns a value in [-1, 1]:
//   1.0  = perfectly concordant (same ranking order)
//   0.0  = no correlation
//  -1.0  = perfectly discordant (reversed ranking order)
//
// Ties contribute 0 (tau-a, not tau-b).
func KendallTau(simScores, prodScores map[string]float64) float64 {
	if len(simScores) <= 1 {
		return 1.0
	}

	ids := make([]string, 0, len(simScores))
	for id := range simScores {
		ids = append(ids, id)
	}
	sort.Strings(ids)

	concordant := 0
	discordant := 0
	for i := 0; i < len(ids); i++ {
		for j := i + 1; j < len(ids); j++ {
			ai := simScores[ids[i]]
			aj := simScores[ids[j]]
			bi := prodScores[ids[i]]
			bj := prodScores[ids[j]]

			sigA := floatSign(ai - aj)
			sigB := floatSign(bi - bj)
			switch {
			case sigA*sigB > 0:
				concordant++
			case sigA*sigB < 0:
				discordant++
			}
		}
	}

	total := len(ids) * (len(ids) - 1) / 2
	if total == 0 {
		return 1.0
	}
	return float64(concordant-discordant) / float64(total)
}

// floatSign returns 1, -1, or 0 for the sign of x.
func floatSign(x float64) int {
	if x > 0 {
		return 1
	} else if x < 0 {
		return -1
	}
	return 0
}

// MaxAbsDiff returns the maximum absolute difference between corresponding values
// in two score maps. Keys must be identical.
func MaxAbsDiff(a, b map[string]float64) float64 {
	max := 0.0
	for id, av := range a {
		diff := av - b[id]
		if diff < 0 {
			diff = -diff
		}
		if diff > max {
			max = diff
		}
	}
	return max
}
