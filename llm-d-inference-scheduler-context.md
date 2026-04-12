# llm-d-inference-scheduler Context

## Overview

The **llm-d-inference-scheduler** is an extensible inference gateway scheduler built on top of the Kubernetes-native **Gateway API Inference Extension (GIE)**. It makes optimized routing decisions for LLM inference requests across model-serving pods in a cluster.

### Core Architecture

- **Envoy Gateway**: Programmable data plane for request routing
- **Endpoint Picker (EPP)**: Control plane component that makes scheduling decisions
- **Plugin System**: Extensible framework for filters, scorers, and profile handlers

### Routing Flow

1. **Filtering**: Pods pass through sequential filter chains to exclude candidates based on criteria
2. **Scoring**: Filtered pods are scored using weighted scorers
3. **Selection**: Highest-scored pod is selected; ties are broken randomly

---

## Plugin System Overview

The scheduler uses a plugin-based architecture with the following plugin categories:

| Category | Purpose | Examples |
|----------|---------|----------|
| **Filters** | Exclude pods based on criteria | Label matching, role-based filtering |
| **Scorers** | Assign scores to candidate pods | Load-aware, session affinity, KV cache locality |
| **Profile Handlers** | Determine which scheduling profiles apply to requests | Data-parallel, disaggregated prefill/decode |
| **Prepare Data** | Pre-process request data for scoring | Tokenization |
| **Pre-Request** | Handle pre-routing logic | Header injection |
| **Data Layer** | Provide metadata and state | Model information extraction |

---

## Scorers

Scorers are the core decision-making plugins that assign quality scores to candidate endpoints. They run sequentially on filtered pods and can be combined with configurable weights.

### Available Scorers

#### 1. **Precise Prefix Cache Scorer** (`precise-prefix-cache-scorer`)

**Purpose**: Routes requests to endpoints with matching KV cache prefixes for cache reuse.

**Key Features**:
- Matches request prefixes against KV block indexes in candidate endpoints
- Supports speculative indexing to close the gap between routing decision and KV event arrival
- Handles tokenization and block-based KV cache indexing
- Highly configurable with token processor and indexer configs

**Parameters**:
- `tokenProcessorConfig`: Block size and token processing settings
- `indexerConfig`: KV block matching and score computation
- `kvEventsConfig`: KV cache event subscription settings
- `speculativeIndexing`: Enable proactive cache entry prediction
- `speculativeTTL`: Lifetime of speculative entries (default: 2s)

**Use Case**: Maximizes KV cache hits by routing to pods with pre-cached prefixes.

---

#### 2. **Session Affinity Scorer** (`session-affinity-scorer`)

**Purpose**: Routes subsequent requests in a session to the same pod as the initial request.

**Key Features**:
- Tracks session tokens via HTTP headers (`x-session-token`)
- Gives high score to the original destination pod
- Enables stateful request handling within sessions

**Parameters**: None (stateless configuration)

**Use Case**: Maintains session state and request continuity for multi-turn conversations.

---

#### 3. **Load Aware Scorer** (`load-aware-scorer`)

**Purpose**: Routes requests away from overloaded pods by considering queue depth.

**Key Features**:
- Scores based on request queue depth relative to a threshold
- Distributes load more evenly across the cluster
- Configurable threshold for queue size

**Parameters**:
- `threshold`: Queue depth threshold (default: 128)

**Scoring**: Pods below threshold score higher; scores degrade as queue depth increases above threshold.

**Use Case**: Prevents hotspots and improves overall cluster throughput.

---

#### 4. **Active Request Scorer** (`active-request-scorer`)

**Purpose**: Balances load based on actively processing requests per pod.

**Key Features**:
- Tracks in-flight requests with timeout handling
- Configurable idle threshold (pods with ≤ threshold requests are "idle")
- Optional scoring gap between idle and busy pods via `maxBusyScore`
- TTL-based eviction of stale request entries

**Parameters**:
- `requestTimeout`: How long before in-flight requests are considered stale (default: 2 minutes)
- `idleThreshold`: Max requests to be considered idle (default: 0)
- `maxBusyScore`: Max score for busy pods (0.0-1.0, default: 1.0)

**Scoring**: Idle pods score 1.0; busy pods score between `maxBusyScore` and lower.

**Use Case**: Fine-grained load distribution with special handling for idle pods.

---

#### 5. **Running Requests Scorer** (`running-requests-scorer`)

**Purpose**: Scores pods based on current running request count.

**Key Features**:
- Configurable threshold for max running requests
- Linear scoring based on request count

**Parameters**:
- `maxRunningRequests`: Target max for scoring (default: 32)

**Scoring**: Linear inverse: score = 1 - (running_requests / maxRunningRequests)

**Use Case**: Simple load balancing based on request count.

---

#### 6. **No-Hit LRU Scorer** (`no-hit-lru-scorer`)

**Purpose**: Penalizes endpoints that recently missed KV cache lookups.

**Key Features**:
- Tracks cold cache hits in an LRU cache
- Works in conjunction with prefix cache scorer
- Avoids re-routing to "cold" endpoints in succession
- Configurable LRU cache size

**Parameters**:
- `prefixPluginType`: Type of prefix cache plugin to monitor (default: `"prefix-cache-scorer"`)
- `prefixPluginName`: Name of the prefix cache plugin instance (default: `"prefix-cache-scorer"`)
- `lruSize`: Max endpoints to track (default: 1024)

**Use Case**: Reduces repeated cold cache hits by avoiding recently-failed endpoints.

---

#### 7. **KV Utilization Scorer** (`kv-utilization-scorer`)

**Purpose**: Routes to endpoints with available KV cache headroom.

**Key Features**:
- Scores inversely to KV cache utilization
- Prioritizes endpoints with lower cache pressure
- Linear scoring: score = 1 - (usage_percent / 100)

**Parameters**: None

**Scoring**:
- Empty cache: score ≈ 1.0
- Full cache: score ≈ 0.0

**Use Case**: Prevents cache eviction by prioritizing pods with available capacity.

---

## Filters

Filters exclude endpoints from consideration before scoring.

### Available Filters

#### 1. **By Label Filter** (`by-label`)
- Matches pods by label key and allowed values
- Can optionally accept pods without the label

#### 2. **By Label Selector Filter** (`by-label-selector`)
- Uses Kubernetes label selector expressions (key/value matching)
- More flexible label-based filtering

#### 3. **Role-Based Filters** (`encode-filter`, `decode-filter`, `prefill-filter`)
- Filters pods by inference role (encode, decode, prefill)
- Supports disaggregated inference serving (E/P/D modes)

---

## Profile Handlers

Profile handlers determine which scheduling profiles apply to requests and how to process their results.

### Available Profile Handlers

#### 1. **Data-Parallel Profile Handler** (`data-parallel-profile-handler`)
- For standard data-parallel serving (no disaggregation)

#### 2. **Disaggregated Profile Handler** (`disagg-profile-handler`)
- For prefill/decode disaggregation
- Legacy alias: `pd-profile-handler`

#### 3. **Always Disagg Multimodal Decider** (`always-disagg-mm-decider`)
- EP (Encode/Prefill) disaggregation for multimodal models

#### 4. **Prefix-Based PD Decider** (`prefix-based-pd-decider`)
- Routes prefill and decode phases based on prefix cache state

#### 5. **Always Disagg PD Decider** (`always-disagg-pd-decider`)
- Forces prefill/decode disaggregation for all requests

---

## Other Plugins

### Prepare Data Plugins

- **Tokenizer** (`tokenizer-plugin`): Pre-tokenizes requests for scoring by KV cache scorer

### Pre-Request Plugins

- **Disagg Headers Handler** (`disagg-headers-handler`): Injects headers for disaggregated routing
  - Legacy alias: `prefill-header-handler`

### Data Layer Plugins

- **Models Data Source** (`models-datasource`): Provides model metadata
- **Models Extractor** (`models-extractor`): Extracts server information

---

## Configuration Example

```yaml
apiVersion: inference.networking.x-k8s.io/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: precise-prefix-cache-scorer
  parameters:
    tokenProcessorConfig:
      blockSize: 5
    indexerConfig:
      maxPrefixBlocksToMatch: 256
    speculativeIndexing: true

- type: load-aware-scorer
  parameters:
    threshold: 128

- type: by-label
  name: role-filter
  parameters:
    label: inference-role
    validValues: ["decode"]

schedulingProfiles:
- name: decode
  plugins:
  - pluginRef: role-filter
  - pluginRef: precise-prefix-cache-scorer
    weight: 60
  - pluginRef: load-aware-scorer
    weight: 40
```

---

## Key Concepts

### Weights in Profiles
When multiple scorers are used in a scheduling profile, scores can be weighted:
- Scorer scores are multiplied by their weight
- Weights are relative (normalized)
- Enables prioritization (e.g., cache hits over load balancing)

### Disaggregated Inference
The scheduler supports separating prefill and decode phases:
- Prefill phase: Processes prompt context (computationally intensive)
- Decode phase: Generates tokens one at a time (memory intensive)
- Can be routed to different pod types optimized for each phase

### State Management
- Plugins can share state via `PluginState` (e.g., session tokens, KV cache hits)
- Enables stateful scoring decisions across plugin phases

---

## Integration with llm-d

The scheduler integrates with:
- **llm-d-kv-cache**: KV cache indexing and management
- **Tekton**: Pipeline orchestration for deployments
- **Gateway API Inference Extension (GIE)**: Upstream inference scheduling framework

---

## Customization

The plugin architecture allows custom implementations by:
1. Implementing the scorer/filter interface
2. Adding a factory function
3. Registering in `pkg/plugins/register.go`
4. Building as part of the EPP container

Refer to `docs/create_new_filter.md` for detailed plugin development guide.
