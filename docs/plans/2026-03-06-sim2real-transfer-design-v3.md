# Sim-to-Production Algorithm Transfer Pipeline Design (v3)

**Date:** 2026-03-06
**Status:** Draft
**Author:** Claude Code + kalantar
**Supersedes:** `docs/plans/2026-02-26-blis-to-llmd-transfer-design-v2.md`

### Revision History

| Date | Change | Reason |
|------|--------|--------|
| 2026-03-06 | v3 rewrite | Re-home pipeline from OpenEvolve to sim2real. Remove OpenEvolve dependency. Prompt-driven architecture with thin CLI tools. Work locally in submodules, PR as final step. Drop operational scaffolding, keep validation rigor. Design for generality (routing first). 8 stages → 6. |

---

## Analysis Questions

This pipeline exists to answer:

1. **Does the sim-discovered algorithm's benefit survive the abstraction gap?** — Do simulation-predicted improvements appear in production benchmarks?
2. **Which simulation signals have production equivalents that preserve the algorithm's mechanism?** — Can we faithfully map the signals the algorithm depends on?
3. **What is the minimum fidelity needed for the benefit to transfer?** — When a signal maps imperfectly, does the algorithm degrade gracefully or break?

Every validation step (Translate, Equivalence Testing, Cluster Benchmark) traces back to at least one of these questions.

## Problem Statement

We have a simulation environment (inference-sim) that discovers adaptive algorithms — routing policies, admission policies, priority policies — validated in simulation but existing only as Go code within the simulator's abstractions.

We need a **transfer pipeline** that takes a discovered algorithm and its evaluation artifacts, translates it into production-quality code for a target system (e.g., `llm-d-inference-scheduler`), validates the translation preserves the algorithm's behavior and benefit, and produces tested PRs against the target repos.

### Scope (v1 — routing transfer)

**In scope:**
- Single algorithm transfer (one evolved code block at a time)
- Algorithms expressible as conditional linear combinations of endpoint signals (e.g., "if input is long, weight prefix higher")
- Algorithms that use only High or Medium fidelity signals (see Signal Mapping)
- Primarily non-disaggregated clusters
- Manual trigger, interactive Claude Code session

**Out of scope:**
- P/D disaggregation-aware transfer (requires simulation extension)
- Algorithms requiring non-linear transformations beyond what the target system provides
- Algorithms that depend on Low-fidelity signals (pipeline halts for human review)
- Bidirectional transfer (production insights back to simulation)
- Multi-algorithm transfer or algorithm composition
- Continuous integration trigger

### Input Artifacts

The pipeline consumes opaque artifacts from a completed experiment. It does not depend on how these artifacts were produced.

- **`best_program.py`** — Contains the evolved algorithm as embedded source code with `EVOLVE-BLOCK-START`/`EVOLVE-BLOCK-END` markers
- **`best_program_info.json`** — Experiment metadata: metrics, per-workload results (under `metrics.artifacts.workload_results`), baseline comparisons
- **`<workload_name>.yaml` files** — Workload configurations sufficient to reproduce the experiment's traffic patterns

Input artifacts live in per-type directories (e.g., `routing/` for the first transfer type).

### Constraints

**Operational:**
- **Trigger:** Manual, post-experiment (user decides which algorithm to transfer)
- **Automation:** LLM-driven with supporting CLI tools — Claude Code reads prompt templates and executes each stage interactively
- **Human involvement:** Interactive Claude Code session — user sees each step and can intervene
- **Translation engine:** LLM-powered (Claude reads both codebases and bridges the abstraction gap)

**Quality:**
- Generated code must pass existing CI of target repos
- Generated code must follow target system plugin conventions (as documented in the scorer template artifact)

**Dependencies:**
- Pipeline depends on `llm-d-inference-scheduler` and `llm-d-benchmark` submodules
- The mapping artifact pins a "last verified against" commit hash for each

---

## Architecture Overview

### Pipeline Stages

```
[1. Extract] → [2. Translate] → [3. Generate] → [4. Test] → [5. Validate] → [6. PR]
```

| Stage | What | Input | Output |
|---|---|---|---|
| **Extract** | Parse input artifacts, extract evolved code block, validate scope | `best_program.py`, `best_program_info.json`, workload YAMLs | Algorithm summary (code, signals used, scope verdict, per-workload results) |
| **Translate** | Map simulation signals to target system equivalents, classify branches, produce coverage report | Algorithm summary + mapping artifact | Signal coverage report + mapped algorithm spec |
| **Generate** | LLM generates target-system code (plugin, tests, configs) in the target submodule | Mapped spec + scorer template + target submodule | Files on a local branch in the target submodule |
| **Test** | Build and test generated code locally in the target submodule | Generated files | Pass/fail + error context (LLM fixes and retries interactively) |
| **Validate** | Equivalence testing (Suites A/B/C via Go harness using inference-sim) + cluster benchmarks with mechanism check | Passing code + inference-sim + cluster access | Per-suite results + benchmark verdict |
| **PR** | Push branches, create PRs against upstream repos | Validated code + validation results | PR URLs |

**Stage 1 scope validation:** Before proceeding to Stage 2, Stage 1 checks whether the discovered algorithm falls within the "conditional linear combinations" scope:
- **In scope (pass):** `+`, `-`, `*`, `/` (scalar arithmetic), comparisons, `if`/`else`, ternary operators
- **Marginal:** `min`, `max`, `abs`, `clamp` — piecewise-linear, often translatable but require review
- **Reject:** `exp`, `log`, `pow`, `sqrt`, trigonometric functions, lookup tables, neural network layers

### What Drives Each Stage

- **Prompt templates** (`prompts/`) — Markdown files containing LLM instructions for each stage. These are the primary pipeline artifact. Claude Code reads a prompt template and executes the stage interactively.
- **CLI tools** (`tools/`) — A thin Python CLI that the LLM invokes for mechanical tasks: parsing `best_program_info.json`, running the equivalence harness, collecting benchmark metrics, etc.
- **Go test harness** (`tools/harness/`) — Uses inference-sim (submodule) to compile and run the original algorithm against test tuples for equivalence testing.

### Invocation

Interactive Claude Code session. The user points at an input directory:

```
> Transfer the algorithm from routing/ to llm-d-inference-scheduler
```

A top-level prompt template (`prompts/transfer.md`) guides Claude Code through all six stages, calling stage-specific prompts and CLI tools as needed.

### Generality

The architecture generalizes beyond routing:
- Each **transfer type** (routing, admission, priority) has its own mapping artifact, prompt templates, and plugin template
- The CLI tools, equivalence harness pattern, and pipeline stages are shared
- `routing/` is the first instance; future types would add e.g. `admission/` with their own input artifacts and mapping artifacts

---

## The Abstraction Gap

### Why Direct Copy Doesn't Work

The simulation and production systems share the same conceptual architecture (weighted scoring across instances) but differ in:

| Gap | Description | How Bridged | Validated By |
|---|---|---|---|
| **Data model** | Sim uses simple structs; production uses endpoints with async metrics | Scorer template type conversions | Test (compilation) |
| **Request representation** | Sim has typed fields; production carries serialized payloads requiring parsing | Generated scorer includes parsing logic | Test (request parsing unit test) |
| **Signal richness** | Some production signals are richer (e.g., per-request prefix match vs. aggregate) | Per-signal decision: upgrade or degrade to match | Validate Suite A (upgrade signal tests) |
| **Metric staleness** | Sim uses synchronous snapshots; production metrics are async with variable staleness | Synthetic staleness injection in tests | Validate Suite B |
| **Concurrency** | Sim evaluates sequentially; production processes concurrently | Sequential-but-rapid test pattern | Validate Suite C |
| **Weight mechanism** | Sim uses normalized weights summing to 1.0; production may use different convention | Documented in mapping artifact | Test (unit tests) |

### Translation Pattern: Composite Plugin

The discovered adaptive logic maps to a **new plugin** that reads endpoint metrics directly and applies the sim-discovered conditional weighting logic. The plugin:

- Reads per-endpoint signals through the target system's standard metric access interface — it does not wrap or delegate to other plugin instances
- Applies conditional weight adjustments based on request characteristics — these conditions and weights are the core translation target from the evolved code block
- Registers as the sole scorer (or replaces the scorers whose logic it internalizes) to avoid double-counting signals
- Implements the full plugin interface following the target system's conventions (as documented in the scorer template artifact)
- Is disableable via config toggle without code removal

**Double-counting prevention:** The signal coverage report's `scorer_overlap` field lists which existing scorers share signals with the new plugin and recommends `disable` or `keep` for each. Unit tests must verify overlapping scorers are disabled in the generated config.

**No-op default:** When the new plugin is disabled, the target system must behave identically to its pre-transfer state. Unit tests must verify this.

This pattern is:
- **Additive at the code level** — introduces a new plugin file without modifying existing source
- **Requires configuration changes** — scoring config updated to register the new plugin and disable overlapping ones
- **Reviewable** — the adaptive logic is isolated and clearly attributable to simulation discovery
- **Testable** — can unit test independently and verify semantic equivalence against simulation

---

## Validation Pipeline

### Stage 2→3 Translation Validation

Stage 2 (Translate) and Stage 3 (Generate) are both LLM-powered. To catch translation errors before they compound:

1. **Mapped spec review (end of Stage 2):** The LLM verifies the spec against the original code block: "Does this pseudocode preserve all conditions and weight assignments?" Self-check, not a guarantee — catches obvious omissions.
2. **Branch-count consistency check:** The mapped spec must reference the same number of conditional branches as the original code block.
3. **Signal-set consistency check (bidirectional):** Signals in the mapped spec must exactly match `signals_used` from Stage 1. Extra signals → LLM hallucinated a mapping. Missing signals → translation dropped a dependency.

### Stage 4: Test Gate

The minimum bar for proceeding to validation. Local only:

1. Run unit tests for the new plugin package only — isolate new code failures
2. Run the full repo build, test, and lint suite — catch integration breakage
3. Generated unit tests must include: (a) request parsing test, (b) disabled-scorer no-op test, (c) overlap assertion (disabled scorers have weight zero in config)
4. On failure: the LLM reads the error, fixes the code, and retries interactively

### Stage 5: Validate

#### Equivalence Testing (Suites A/B/C)

All suites run locally using a Go test harness built against inference-sim. The harness compiles the original algorithm from `best_program.py`, feeds test tuples, and captures score vectors. The translated production plugin is tested via its own unit test framework reading the same tuples.

Suites run in order: A → B → C. If a suite fails, dependent suites are skipped.

**Suite A — Baseline equivalence (controlled):**
- Generate test tuples systematically: for each input dimension the algorithm conditions on (e.g., input size, queue depth, cache state), sample at min, median, max, and each threshold value +/-1. Cartesian product across dimensions, capped at 200 tuples (threshold boundaries prioritized when cap is reached).
- Run each tuple through both the original algorithm (via Go harness + inference-sim) and the translated plugin.
- **Two pass criteria (both must pass):**
  1. **Numeric fidelity:** Per tuple, `abs(sim_score - prod_score) <= 1e-6` (absolute) OR `<= 1%` (relative)
  2. **Rank correlation:** Kendall-tau > 0.8 (configurable, provisional until calibrated)
- For signals marked as `"Upgrade"` in the coverage report, include additional tuples (up to 5 per Upgrade signal) sampling the divergence range.

**Suite B — Staleness sensitivity:**
- Re-run Suite A tuples with synthetic staleness injected into production signal reads, using per-source-group staleness windows from the mapping artifact. Signals from the same collection source receive correlated staleness.
- 3 repetitions with different staleness seeds.
- **Pass criteria (both must pass):**
  1. **Rank stability:** Kendall-tau > 0.7 across all repetitions
  2. **Threshold-crossing stability:** <20% of threshold-boundary tuples change classification under staleness

**Suite C — Concurrency stress:**
- C1 (parallel safety): 20 concurrent routing decisions against the same endpoint snapshot. Verify: no panics, no NaN, deterministic results.
- C2 (pile-on dynamics): 20 sequential-but-rapid requests with state updates between decisions. No endpoint receives >2x fair share.

#### Cluster Benchmarks

Two deployments against the same workloads (reproduced from the input workload YAMLs):

1. **Baseline run** — stock scorer config, new plugin disabled
2. **Treatment run** — new plugin enabled, overlapping scorers disabled

Metrics collected: latency (TTFT + E2E, mean + P95) and throughput.

**Go/no-go criteria (all thresholds provisional, configurable):**
- Improvement > 5% on at least one workload
- No regression > 2% on any workload
- P95 latency regression < 2% on any workload
- **Mechanism check** (evaluated in order):
  1. Matched workload (where the algorithm was discovered) must show improvement >= threshold. If not → **FAIL**.
  2. If all workloads improve by similar amounts (max - min < threshold/2), benefit is likely from overhead reduction, not mechanism → **INCONCLUSIVE**, requires human review.
  3. Matched workload must rank first or tied-first in improvement, AND exceed mean improvement by at least threshold/2. If not → **FAIL**.
  4. If all above pass → **PASS**.

**Improvement magnitude comparison (informational, not a gate):**

| Workload | Sim Predicted Improvement | Cluster Observed Improvement | Ratio (Observed/Predicted) |
|---|---|---|---|

- Ratio < 0.3 → "significant attenuation" flag
- Ratio > 2.0 → "unexpected amplification" flag
- Tracked per-workload-type in calibration log across transfers.

**Noise characterization prerequisite:**
Before the first cluster benchmark on a target cluster, run the baseline 5 times with identical config. Compute CV per metric. Improvement threshold must exceed 2x observed CV. Re-characterize after cluster config changes or after 30 days.

---

## Signal Mapping Summary

The canonical mapping lives in `docs/transfer/blis_to_llmd_mapping.md`. This table is a high-level orientation only — do not duplicate fidelity ratings here.

**Signals used by the current routing algorithm** (from `best_program.py` EVOLVE-BLOCK):

| Signal | Sim Field | How Used in Algorithm | Correspondence |
|---|---|---|---|
| **Effective load** | `QueueDepth + BatchSize + InFlightRequests` | Cubic load penalty when delta > 0.2; hard penalty at load > 4.5 and > 7.0 | Direct — production tracks similar load metrics |
| **KV utilization** | `KVUtilization` (float64, 0-1) | Memory pressure penalty when > 0.82 | Direct — both express as fraction |
| **Cache hit rate** | `CacheHitRate` (float64, 0-1) | Session affinity boost when > 0.35 and request has SessionID | Target may be richer (per-request prefix match vs. aggregate) |
| **Session ID** | `req.SessionID` (string) | Presence check gates cache affinity logic | Convention-dependent — how session identity is conveyed |
| **Scorer weights** | `ws.weights[]` (normalized) | Base scorer pipeline scores combined before penalties | Weight mechanism may differ in production |

**Fidelity Enum**

- **High** — Behavioral equivalence with <5% divergence in test comparisons
- **Medium** — Same concept but different granularity or access pattern
- **Low** — Requires approximation or proxy
- **Upgrade** — Target signal is strictly richer than simulation equivalent

Per-signal fidelity assignments are maintained exclusively in the mapping artifact.

---

## Supporting Artifacts & Repository Structure

### Repository Layout

```
sim2real/
├── inference-sim/              # submodule — simulation environment
├── llm-d-inference-scheduler/  # submodule — target: scorer plugins
├── llm-d-benchmark/            # submodule — target: benchmark configs
├── routing/                    # input artifacts (first transfer type)
│   ├── best_program.py
│   ├── best_program_info.json
│   └── workload_v2_*.yaml
├── prompts/                    # prompt templates (the primary pipeline artifact)
│   ├── transfer.md             # top-level orchestration prompt
│   ├── extract.md              # Stage 1
│   ├── translate.md            # Stage 2
│   ├── generate.md             # Stage 3
│   ├── test.md                 # Stage 4
│   ├── validate.md             # Stage 5
│   └── pr.md                   # Stage 6
├── tools/                      # CLI utilities invoked by the LLM
│   ├── harness/                # Go equivalence test harness (uses inference-sim)
│   └── transfer_cli.py         # parsing, metrics collection, workload config generation
├── docs/
│   ├── plans/                  # design docs, macro plans
│   └── transfer/               # mapping artifacts, calibration log, noise data
│       ├── blis_to_llmd_mapping.md
│       ├── scorer_template.go.md
│       ├── calibration_log.md
│       └── noise_characterization.md
└── workspace/                  # per-transfer working directory (gitignored)
```

### Supporting Artifacts (3)

| Artifact | Location | Purpose | Status |
|---|---|---|---|
| **Mapping artifact** | `docs/transfer/blis_to_llmd_mapping.md` | Signal/interface mapping with concrete types, metric paths, staleness windows, fidelity ratings. Pins target system commit. Single source of truth for implementation-level details. | TODO — create before first transfer |
| **Scorer template** | `docs/transfer/scorer_template.go.md` | Annotated example of a well-structured target-system plugin showing conventions, test structure, config registration. Must compile against target submodule HEAD. | TODO — create before first transfer |
| **Prompt templates** | `prompts/` | LLM instructions for each pipeline stage. Includes tuple generation strategy, diagnostic guidance, threshold configs. | TODO — create before first transfer |

All three are prerequisites — the pipeline cannot execute without them.

### Workspace

`workspace/` is the per-transfer working directory (gitignored). Holds intermediate artifacts: algorithm summary, signal coverage report, mapped spec, generated configs. Persists across stages for continuity within a transfer session.

### Generality Points

- Input artifacts live in per-type directories (`routing/`, future `admission/`, etc.)
- Mapping artifacts and scorer templates are per-transfer-type
- Prompt templates can be shared or specialized per type
- The Go test harness pattern generalizes (any sim policy can be compiled and scored against test tuples)

---

## Falsification Protocol

**Early warning indicators (tracked across transfers):**
- Suite A Kendall-tau trending downward (even if still above threshold)
- Suite B staleness sensitivity increasing
- Stage 4 test failure rate increasing
- If any trend appears across 2 consecutive transfers, flag for human review.

**Full falsification:** If >=3 independent algorithms — each with High/Medium-fidelity signals, each passing equivalence tests — all fail cluster benchmarks, the abstraction gap is too wide for current simulation fidelity. Suspend pipeline, investigate simulation improvements.

**Partial falsification:** If failures cluster on specific workload types, restrict transfers to workload types with demonstrated success. Track per-workload-type pass/fail in the calibration log.

**Calibration Log**

`docs/transfer/calibration_log.md` — one entry per completed transfer. Fields: algorithm name, date, Suite A/B Kendall-tau values, cluster benchmark verdict, per-workload sim-predicted vs. observed improvement ratios, overrides used, notes. Used for threshold adjustment decisions after every 3 transfers.

---

## Diagnostic Guide

| Failure | Check | Likely Cause |
|---|---|---|
| Stage 4 (test) | Error references symbol not in mapping artifact | Stale mapping — update artifact |
| Stage 4 (test) | 3 retries exhausted | Algorithm too complex for LLM translation |
| Suite A (equivalence) | Failures at threshold boundaries | Off-by-one or inverted condition in translation |
| Suite A (equivalence) | Numeric fidelity fails, rank passes | Weight normalization mismatch |
| Suite B (staleness) | Single signal causes most degradation | That signal's staleness window larger than expected |
| Suite B (staleness) | All signals degrade | Algorithm fundamentally staleness-sensitive |
| Cluster benchmark | All workloads improve equally | Benefit from overhead reduction, not mechanism |
| Cluster benchmark | Matched workload doesn't improve | Workload reproduction issue or benefit doesn't survive production conditions |
| Cluster benchmark | No workload improves | Check noise characterization — threshold may be too tight |

---

## Future Extensions

Explicitly **out of scope** for v1:

1. **P/D-aware transfer** — Extend simulation to model prefill/decode disaggregation, then extend the pipeline to generate plugins for specific scheduling profiles
2. **Bidirectional transfer** — Production benchmark insights fed back to simulation to improve fidelity
3. **Claude Code skill** — Package the transfer as a `/transfer` skill with argument parsing
4. **Multi-algorithm transfer** — Batch transfer of top-N algorithms from a single experiment
5. **CI auto-trigger** — Auto-trigger on new best program discovery above a threshold
6. **Statistical confidence** — Require >=3 benchmark runs with significance testing instead of single-run go/no-go
7. **Combined staleness+concurrency test (Suite D)** — Test interaction of stale signals with concurrent requests
8. **Non-routing transfer types** — Admission policies, priority policies, etc. (the architecture supports this; implementation is per-type work)

---

## Summary

The transfer pipeline is an **interactive Claude Code session** guided by:
- **Prompt templates** (`prompts/`) — LLM instructions for each of the 6 pipeline stages
- **A mapping artifact** (`docs/transfer/blis_to_llmd_mapping.md`) — concrete signal/interface correspondences
- **A scorer template** (`docs/transfer/scorer_template.go.md`) — target system plugin conventions
- **CLI tools** (`tools/`) — mechanical support for parsing, equivalence testing, and metrics collection

The pipeline works locally in the target submodules, validates through equivalence testing (using inference-sim) and cluster benchmarks, and creates PRs as the final step. Validation rigor (3-suite equivalence, mechanism checks, noise characterization, falsification protocol) is preserved from v2. Operational scaffolding (retry state machines, crash recovery, artifact version schemes) is removed in favor of interactive session handling.

### Key Changes from v2

| Aspect | v2 | v3 |
|---|---|---|
| Host repo | OpenEvolve | sim2real |
| Simulation dependency | OpenEvolve + BLIS evaluator | inference-sim (submodule) |
| Pipeline driver | Python modules (`openevolve/transfer/`) | Prompt templates + thin CLI tools |
| Stages | 8 | 6 |
| PR creation | Mid-pipeline (draft PRs, comments as validation log) | Final step (work locally, PR when validated) |
| Operational scaffolding | Heavy (retry state machines, crash recovery, artifact version schemes, borderline timeouts) | Light (interactive session handles errors and human decisions) |
| Validation rigor | 3-suite equivalence + cluster benchmarks + mechanism check | Same |
| Generality | Routing-specific | Designed for multiple transfer types (routing first) |
| Supporting artifacts | 4 (mapping, template, prompt, workload generator) | 3 (mapping, template, prompts) |
