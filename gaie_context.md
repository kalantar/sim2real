# GAIE (Gateway API Inference Extension) - Scorers Context

## Overview

The Gateway API Inference Extension (GAIE) is a Kubernetes-native system for intelligent inference request routing. Rather than providing a fixed set of scorers, GAIE implements an **extensible, pluggable architecture** where scoring decisions are customizable based on infrastructure needs.

## Scorer Architecture

GAIE's scheduling system uses a **profile-based plugin model**:
- Multiple scorers can be registered per `SchedulerProfile`
- Scorers provide normalized scores in the range `[0-1]`
- Scoring plugins are weighted and combined through the scheduler framework
- Weighting is configured at the `SchedulerProfile` configuration level

## Available Scorers and Components

### 1. **WeightedScorer**
- **Purpose**: Wraps individual scorers with associated weight values
- **Implementation**: `pkg/epp/scheduling/weighted_scorer.go`
- **Functionality**: Enables combining multiple scorers by associating a `float64` weight with each scorer
- **Configuration**: Requires a scorer implementing `fwksched.Scorer` interface and a weight value

### 2. **Latency Predictor**
- **Purpose**: Predicts request completion latency based on system state and request characteristics
- **Location**: `latencypredictor/` module
- **Factors Considered**:
  - `kv_cache_percentage` - KV cache utilization
  - `input_token_length` - Request input size
  - `num_request_waiting` - Queued requests
  - `num_request_running` - Currently executing requests
  - `num_tokens_generated` - Output tokens generated
- **Architecture**: Dual-server model (training and inference)
- **Endpoint**: POST `/predict` for latency estimation

### 3. **Body-Based Router (BBR)**
- **Purpose**: Extracts routing information from HTTP request body
- **Functionality**: Parses inference request messages (e.g., OpenAI API format) to extract model name
- **Use Case**: Route requests based on requested model specification in request payload

### 4. **KV-Cache Aware Routing**
- **Purpose**: Optimizes tail latency and throughput for LLM completion requests
- **Strategy**: Routes based on KV-cache state to minimize cache evictions and queuing
- **Related Proposal**: `0602-prefix-cache-aware-routing-proposal`
- **Benefit**: Improves request scheduling awareness of cached inference state

### 5. **Request Cost Optimization**
- **Purpose**: Routes requests based on cost metrics to avoid excessive load
- **Strategy**: Considers request expense/cost in routing decisions
- **Goal**: Prevents endpoint overload and associated evictions

## Scheduler Framework

The scheduler system operates through the `Scheduling Layer` (in `pkg/epp/scheduling/`):
- **Core Component**: `scheduler.go`, `scheduler_profile.go`
- **Profile Management**: Different scheduling profiles can be configured with different scorer combinations
- **Configuration**: Managed through `SchedulerProfile` definitions

## Extensibility Model

GAIE is designed for custom scorer implementation:
- Scorers implement the `fwksched.Scorer` interface
- Custom scorers can be registered as plugins
- Scoring logic can be customized per deployment
- Weighting and combination strategies are configurable

## Key Architectural Patterns

1. **Endpoint Picker (EPP)**: Primary routing mechanism providing "Routing, Flow, and Request Control layers"
2. **Metrics-based Decisions**: Routing uses real-time metrics and capabilities from model servers
3. **Extensible Framework**: Allows operators to customize routing logic for their infrastructure needs
4. **Multi-factor Optimization**: Combines multiple scoring perspectives (latency, cost, cache, etc.)

## Configuration

Scorer configuration follows a two-level model:
- **SchedulerProfile**: Specifies which scorers are active and their weights
- **Scoring Plugins**: Individual scorers register their configuration requirements

## Notes

- The GAIE architecture emphasizes **flexibility and customization** over prescriptive scorer implementations
- Specific scorer algorithms are primarily found in:
  - `pkg/epp/scheduling/` - scheduling-related scorers
  - `latencypredictor/` - ML-based latency prediction
  - Custom plugins registered per deployment
- Most scoring logic is designed to be pluggable and operator-customizable rather than hardcoded
