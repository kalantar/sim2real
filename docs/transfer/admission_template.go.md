# Admission Plugin Template — llm-d-inference-scheduler

**Version:** 1.0
**Pinned commit:** `4cd7046e2cf9121be6cdc2fc0815dbeeba721c9f`
**Module:** `github.com/llm-d/llm-d-inference-scheduler`
**Go version:** 1.25.7

> **For Stage 3 LLM:** Use this template as the structural reference for generating a
> production admission control plugin. The admission plugin is a **new plugin type** in
> llm-d-inference-scheduler — there are no existing admission plugins to reference.
> Follow the conventions from the scorer and filter plugins for factory pattern,
> registration, and type assertions.
>
> **Key difference from scorers:** Scorers receive per-endpoint data and return per-endpoint
> scores. The admission plugin receives the full endpoint list and the request, then makes
> a binary admit/reject decision based on **cluster-wide aggregate** metrics. The plugin
> must aggregate per-endpoint metrics into cluster-wide signals to match the simulation's
> `RouterState.Snapshots` aggregation pattern.
>
> **Signal mappings:** Use `docs/transfer/blis_to_llmd_admission_mapping.md` to map
> simulation signal names to production metric access paths.
>
> **Baseline reference:** Read the baseline file in `context.extra` to understand the
> simulation's `AdmissionPolicy` interface, available signals, and state fields.

---

## Section 1: Package Structure

The admission plugin lives in a new `admission` package under the plugins directory.

```
llm-d-inference-scheduler/
├── pkg/
│   └── plugins/
│       ├── register.go                ← Add plugin.Register() call here
│       ├── scorer/                    ← Existing scorer plugins (reference only)
│       ├── filter/                    ← Existing filter plugins (reference only)
│       └── admission/                 ← NEW — admission plugin package
│           ├── <your_plugin>.go       ← Admission plugin implementation
│           └── <your_plugin>_test.go  ← Test file (package admission_test)
└── test/
    └── utils/
        └── context.go                 ← Test context helper
```

**Convention:** One plugin per file. File name matches plugin type (snake_case).
Test file is `<name>_test.go` using the external test package (`package admission_test`).

---

## Section 2: Plugin Interface

The admission plugin implements `scheduling.Filter` — a filter that removes ALL endpoints
(rejecting the request) or keeps all endpoints (admitting the request). This reuses the
existing plugin framework without requiring new interfaces.

> **Design rationale:** The `scheduling.Filter` interface receives
> `(ctx, cycleState, request, endpoints)` which gives us access to both the request
> metadata (SLO class, tenant ID) and all endpoint metrics (for cluster-wide aggregation).
> Returning an empty endpoint list effectively rejects the request.

```go
package admission

import (
	"context"
	"encoding/json"
	"sync"
	"time"

	"sigs.k8s.io/controller-runtime/pkg/log"
	logutil "sigs.k8s.io/gateway-api-inference-extension/pkg/common/observability/logging"
	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/plugin"
	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/scheduling"
)

const (
	// PluginType is the unique type name for this admission plugin.
	// Convention: lowercase-hyphenated, suffixed with "-admission-policy".
	PluginType = "by60-admission-policy"
)

// Compile-time type assertion.
var _ scheduling.Filter = &AdmissionPlugin{}

// AdmissionPlugin implements cluster-aware admission control as a Filter.
// It aggregates per-endpoint metrics to make cluster-wide admit/reject decisions.
type AdmissionPlugin struct {
	typedName plugin.TypedName
	enabled   bool
	mu        sync.Mutex // Protects mutable admission state

	// Stateful admission tracking (mirrors simulation's AdaptiveAdmission)
	// These fields persist across calls because the plugin instance is reused.
	tenantTokens   map[string]float64
	tenantRequests map[string]int
	classCounters  map[string]int
	windowStart    int64
	windowCount    int
	totalAdmitted  int
	totalRejected  int
}
```

---

## Section 3: Factory Function

```go
// Factory creates the admission plugin from config parameters.
func Factory(name string, rawParameters json.RawMessage, _ plugin.Handle) (plugin.Plugin, error) {
	p := &AdmissionPlugin{
		typedName:      plugin.TypedName{Type: PluginType, Name: name},
		enabled:        true,
		tenantTokens:   make(map[string]float64),
		tenantRequests: make(map[string]int),
		classCounters:  make(map[string]int),
	}
	// Parse optional config parameters if needed
	if rawParameters != nil {
		// Add parameter parsing here if the plugin accepts configuration
	}
	return p, nil
}
```

---

## Section 4: TypedName

```go
// TypedName returns the plugin's typed name.
func (p *AdmissionPlugin) TypedName() plugin.TypedName {
	return p.typedName
}
```

---

## Section 5: Filter Method (Admission Logic)

The `Filter` method is where the admission decision happens. Return all endpoints
to admit, return empty slice to reject.

```go
// Filter implements scheduling.Filter.
// Admits or rejects the request based on cluster-wide metrics.
// Returns all endpoints to admit, empty slice to reject.
func (p *AdmissionPlugin) Filter(
	ctx context.Context,
	cycleState *scheduling.CycleState,
	request *scheduling.LLMRequest,
	endpoints []scheduling.Endpoint,
) []scheduling.Endpoint {
	if !p.enabled || len(endpoints) == 0 {
		return endpoints
	}

	p.mu.Lock()
	defer p.mu.Unlock()

	logger := log.FromContext(ctx).V(logutil.DEBUG)

	// ── Aggregate cluster-wide signals from per-endpoint metrics ──
	numInstances := len(endpoints)
	totalInFlight := 0
	totalQueueDepth := 0
	maxKVUtil := 0.0
	sumKVUtil := 0.0
	for _, e := range endpoints {
		m := e.GetMetrics()
		totalInFlight += int(m.RunningRequestsSize)
		totalQueueDepth += int(m.WaitingQueueSize)
		kvUtil := float64(m.KVCacheUsagePercent) / 100.0 // Normalize: prod 0-100 → sim 0.0-1.0
		if kvUtil > maxKVUtil {
			maxKVUtil = kvUtil
		}
		sumKVUtil += kvUtil
	}
	avgKVUtil := 0.0
	if numInstances > 0 {
		avgKVUtil = sumKVUtil / float64(numInstances)
	}

	// Request metadata
	// NOTE: Extract SLO class and tenant ID from request headers/labels.
	// The exact access path depends on how these are propagated in production.
	// Placeholder — replace with actual request metadata access:
	sloClass := ""    // request.GetSLOClass() or from headers
	tenantID := ""    // request.GetTenantID() or from headers
	clock := time.Now().UnixMicro()

	// Suppress unused variable warnings for signals the EVOLVE-BLOCK may not use
	_ = numInstances
	_ = totalInFlight
	_ = totalQueueDepth
	_ = maxKVUtil
	_ = avgKVUtil
	_ = sloClass
	_ = tenantID
	_ = clock

	// ── EVOLVE-BLOCK logic goes here ──
	// Translate the simulation's Admit() EVOLVE-BLOCK into production Go code.
	// Use the signal mappings from blis_to_llmd_admission_mapping.md.
	//
	// admitted := true
	// reason := ""
	// ... (translated EVOLVE-BLOCK logic) ...
	//
	// if !admitted {
	//     logger.Info("Request rejected", "reason", reason, "sloClass", sloClass)
	//     return []scheduling.Endpoint{} // Reject: return empty list
	// }

	logger.Info("Request admitted", "sloClass", sloClass, "tenantID", tenantID)
	return endpoints // Admit: return all endpoints
}
```

---

## Section 6: Registration

In `pkg/plugins/register.go`, add:

```go
import "github.com/llm-d/llm-d-inference-scheduler/pkg/plugins/admission"

// Inside RegisterAllPlugins():
plugin.Register(admission.PluginType, admission.Factory)
```

---

## Section 7: Test Patterns

```go
package admission_test

import (
	"context"
	"testing"

	"github.com/google/go-cmp/cmp/cmpopts"
	"github.com/llm-d/llm-d-inference-scheduler/pkg/plugins/admission"
	"github.com/llm-d/llm-d-inference-scheduler/test/utils"

	fwkdl "sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/datalayer"
	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/framework/interface/scheduling"
)

func TestAdmitUnderLowLoad(t *testing.T) {
	ctx := utils.NewTestContext(t)
	plugin, _ := admission.Factory("test", nil, nil)
	filter := plugin.(scheduling.Filter)

	endpoints := []scheduling.Endpoint{
		scheduling.NewEndpoint(
			&fwkdl.EndpointMetadata{},
			&fwkdl.Metrics{
				RunningRequestsSize: 5,
				WaitingQueueSize:    2,
				KVCacheUsagePercent: 30,
			},
			nil,
		),
	}

	result := filter.Filter(ctx, &scheduling.CycleState{}, &scheduling.LLMRequest{}, endpoints)
	if len(result) == 0 {
		t.Error("expected request to be admitted under low load")
	}
}

func TestRejectUnderHighLoad(t *testing.T) {
	// Create endpoints with high load metrics
	// Verify the filter returns empty slice (rejection)
}
```

---

## Key Differences from Scorer Template

| Aspect | Scorer | Admission |
|--------|--------|-----------|
| Interface | `scheduling.Scorer` | `scheduling.Filter` |
| Return value | `map[Endpoint]float64` (per-endpoint scores) | `[]Endpoint` (filtered list) |
| Signal scope | Per-endpoint | Cluster-wide (aggregated) |
| Statefulness | Stateless (fresh each call) | Stateful (sliding windows, counters) |
| Thread safety | Not needed | `sync.Mutex` required |
| Package | `pkg/plugins/scorer/` | `pkg/plugins/admission/` |
| Category | `Distribution` / `Affinity` | N/A (Filter has no category) |
