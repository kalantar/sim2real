package harness

import "testing"

func TestKendallTau(t *testing.T) {
	tests := []struct {
		name      string
		sim       map[string]float64
		prod      map[string]float64
		wantTau   float64
	}{
		{
			name:    "perfectly concordant",
			sim:     map[string]float64{"a": 1.0, "b": 2.0},
			prod:    map[string]float64{"a": 1.0, "b": 2.0},
			wantTau: 1.0,
		},
		{
			name:    "perfectly discordant",
			sim:     map[string]float64{"a": 2.0, "b": 1.0},
			prod:    map[string]float64{"a": 1.0, "b": 2.0},
			wantTau: -1.0,
		},
		{
			name:    "single key (len <= 1 early-return)",
			sim:     map[string]float64{"a": 1.0},
			prod:    map[string]float64{"a": 1.0},
			wantTau: 1.0,
		},
		{
			name:    "all-tied in sim (tau-a convention)",
			sim:     map[string]float64{"a": 1.0, "b": 1.0},
			prod:    map[string]float64{"a": 1.0, "b": 2.0},
			wantTau: 0.0,
		},
		{
			name:    "empty maps",
			sim:     map[string]float64{},
			prod:    map[string]float64{},
			wantTau: 1.0,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := KendallTau(tc.sim, tc.prod)
			if got != tc.wantTau {
				t.Errorf("KendallTau(%v, %v) = %f, want %f", tc.sim, tc.prod, got, tc.wantTau)
			}
		})
	}
}

func TestMaxAbsDiff(t *testing.T) {
	tests := []struct {
		name     string
		a        map[string]float64
		b        map[string]float64
		wantDiff float64
	}{
		{
			name:     "same values",
			a:        map[string]float64{"a": 1.0, "b": 2.0},
			b:        map[string]float64{"a": 1.0, "b": 2.0},
			wantDiff: 0.0,
		},
		{
			name:     "one large diff",
			a:        map[string]float64{"a": 1.0, "b": 2.0},
			b:        map[string]float64{"a": 1.0, "b": 4.0},
			wantDiff: 2.0,
		},
		{
			name:     "negative diff (absolute value)",
			a:        map[string]float64{"a": 3.0},
			b:        map[string]float64{"a": 1.0},
			wantDiff: 2.0,
		},
		{
			name:     "empty maps",
			a:        map[string]float64{},
			b:        map[string]float64{},
			wantDiff: 0.0,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := MaxAbsDiff(tc.a, tc.b)
			if got != tc.wantDiff {
				t.Errorf("MaxAbsDiff(%v, %v) = %f, want %f", tc.a, tc.b, got, tc.wantDiff)
			}
		})
	}
}
