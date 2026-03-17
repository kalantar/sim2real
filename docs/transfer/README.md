# Sim-to-Production Transfer Pipeline

Pipeline for transferring simulation-discovered routing algorithms to production
llm-d-inference-scheduler scorer plugins.

## Status

Under construction. See `docs/plans/2026-03-06-sim2real-transfer-macro-plan-v3.md`.

### PR3 Deliverables

- **Go test harness:** `tools/harness/` — imports inference-sim, validates algorithm loading and EVOLVE-BLOCK content hash. Types: `Algorithm`, `TestTuple`, `Result`. Functions: `LoadAlgorithm`, `RunTuples`, `NormalizeKVUtilization`, `ValidateSignalTypes`.
- **Evolved scorer shim:** `tools/harness/evolved_scorer.go` — implements `scheduling.Scorer` interface (R6). PR3 returns placeholder scores; PR5 wires actual logic.
- **Prompt templates:** `prompts/transfer.md` (orchestrator), `prompts/extract.md` (Stage 1), `prompts/translate.md` (Stage 2), `prompts/generate.md` (Stage 3).
- **Signal coverage schema:** `tools/schemas/signal_coverage.schema.json` — validates Stage 2 output.
- **CI script:** `tools/check_scorer_template.sh` — compiles scorer template Go blocks against submodule HEAD.

## Directory Layout

- `docs/transfer/` — Mapping artifacts, scorer template, calibration log
- `tools/` — Python CLI + Go test harness (PR3)
- `tools/schemas/` — JSON Schema files for workspace artifact validation
- `prompts/` — Pipeline stage prompt templates (PR3)
- `workspace/` — Inter-stage JSON artifacts (gitignored)

## R6 Resolution: LoadEvolvedBlock API

**Status:** UNVERIFIED — `LoadEvolvedBlock` does not exist in inference-sim.

**Decision:** Use shim approach (option a). The Go test harness (`tools/harness/`, PR3)
will directly instantiate `WeightedScoring` from parsed algorithm parameters rather than
relying on a dedicated `LoadEvolvedBlock` API. This keeps all sim2real PRs independent
of inference-sim changes.

**Rationale:** The evolved algorithm's EVOLVE-BLOCK modifies the `Route()` method body
of `WeightedScoring`. A shim can reconstruct this by:
1. Parsing the evolved scoring/penalty logic from the EVOLVE-BLOCK (**Note:** The source file `routing/best_program.py` is a Python file containing Go code embedded in a triple-quoted string literal `GO_ROUTING_CODE = """..."""`. PR3 must parse the Python file to extract the embedded Go, not treat the `.py` file as standalone Go.)
2. Creating a `WeightedScoring` instance with standard scorers
3. Wrapping it with the evolved penalty logic as a post-scoring step

This avoids requiring an inference-sim API PR (option b) which would block PR3.

**Shim acceptance criteria (PR3):** The shim in `tools/harness/evolved_scorer.go` MUST:
1. Implement `scheduling.Scorer` interface (Score method with correct signature) ✅ PR3
2. Accept all signals from `algorithm_summary.json` `signals[]` as input — *deferred to PR5*
3. Reproduce the scoring/penalty logic from the EVOLVE-BLOCK — *deferred to PR5*
4. Verify `evolve_block_content_hash` before parsing (BC-11) ✅ PR3 (in `LoadAlgorithm`)
5. Pass unit tests comparing shim output against simulation output for reference inputs — *deferred to PR5*

**PR3 scope note:** PR3's `evolved_scorer.go` returns uniform 0.5 placeholder scores. Criteria #2, #3, and #5 are deferred to PR5, which wires `Algorithm.Route()` into the production scorer path.

## Cross-PR Contracts (PR3 Obligations)

R5-F-6 fix: Machine-readable list of PR3 gates with severity levels for cross-PR discoverability.

PR3 MUST implement and pass these gates before merging:

1. **`test_stale_hash_aborts_parsing`** — CRITICAL. PR3 must include a test that runs extract, modifies the EVOLVE-BLOCK source, attempts to parse using the stale summary, and asserts parsing aborts with a drift detection error. (See BC-11.)
2. **KVUtilization normalization test** — CRITICAL. PR3 must include a unit test verifying that production `KVCacheUsagePercent` values (0-100) are divided by 100 before being passed to the scorer.
3. **Unknown-type signal rejection** — IMPORTANT. PR3 must verify that signals with type `"unknown"` are rejected or handled explicitly, not silently passed through to the scorer.
4. **F-10 InFlightRequests double-counting guard** — ~~IMPORTANT~~ RESOLVED in PR5. `RunningQueueSize` and `RunningRequestCount` do not exist in `fwkdl.Metrics`; the correct field is `RunningRequestsSize`. PR5 resolved F-10 by zeroing BatchSize in the production scorer, so EffectiveLoad = `WaitingQueueSize + RunningRequestsSize` (single-count). PR3 need only use `RunningRequestsSize` for InFlightRequests. (See `blis_to_llmd_mapping.md` InFlightRequests row.)
5. **CacheHitRate production access path verification** — ~~IMPORTANT~~ RESOLVED in PR5. No production `GetMetrics()` field was identified for CacheHitRate. PR5 zeroed the signal (`CacheHitRate = 0.0`) in the production scorer and downgraded fidelity to low. PR3 need not derive an access path. (See `blis_to_llmd_mapping.md` CacheHitRate row.)

## Scorer Template (PR2)

**Location:** `docs/transfer/scorer_template.go.md`
**Version:** 1.0
**Pinned commit:** `b9a4a82e0d9b83ad362e37aa3682672f8c45f331`

The scorer template is an annotated example showing llm-d-inference-scheduler plugin conventions. Stage 3's LLM uses it as the structural reference for generating production scorer code.

**PR3 obligations for scorer template:**
1. **Compilation check:** PR3 adds `tools/check_scorer_template.sh` that extracts Go code blocks from the template, compiles them against the current submodule HEAD, and fails if compilation fails. This provides automated staleness detection.
2. **Metric field verification:** ~~PR3 MUST confirm UNVERIFIED fields (`RunningQueueSize`, `RunningRequestCount`, `KVCacheUsagePercent`)~~ RESOLVED in PR5: `RunningQueueSize` and `RunningRequestCount` do not exist — the correct field is `RunningRequestsSize`. `KVCacheUsagePercent` is verified. Both the mapping artifact and scorer template have been updated in PR5. PR3 should still initialize the submodule and confirm fields as a safety check.
3. **CacheHitRate access path:** ~~PR3 must determine the correct access path for cache hit rate information~~ RESOLVED in PR5. No production `GetMetrics()` field was identified for CacheHitRate. PR5 zeroed the signal and downgraded fidelity to low. PR3 need not derive an access path. (See gate check item 5 above and `blis_to_llmd_mapping.md` CacheHitRate row.)
4. **Placeholder replacement validation:** The template's `Score()` body contains a `PLACEHOLDER` comment marking the example scoring logic. After code generation, PR3 MUST verify that no `PLACEHOLDER` markers remain in the generated scorer code. If any remain, the generation is incomplete.
5. **Nil-score framework behavior verification (BC-4):** PR3 must verify nil-score handling in the framework's aggregation logic. The template's `Score()` returns `nil` when the scorer is disabled (feature-flag opt-out). PR3 must confirm that the scheduler skips a scorer returning nil scores and routes using remaining active scorers only. See scorer_template.go.md Section 5.

**PR4 obligations for scorer template:**
1. **Stage 3 output artifact consumption:** PR4 MUST read `workspace/stage3_output.json` (written by Stage 3) to locate the generated scorer file, test file, registration file, and scorer type for build and test. See scorer_template.go.md, Stage 3 Output Validation section (after Section 8), items 5-6.

## Prompt Template Contract (PR3)

PR3 prompt templates MUST consume these PR1 artifacts:
- **Input:** `workspace/algorithm_summary.json` (signal metadata, EVOLVE-BLOCK location, content hash)
- **Input:** `docs/transfer/blis_to_llmd_mapping.md` (signal mappings, fidelity ratings, normalization notes)
- **Output:** Generated Go source implementing `scheduling.Scorer`

**Prompt template requirements:**
1. Each prompt MUST include the signal list from `algorithm_summary.json` `signals[]`
2. Each prompt MUST include normalization notes from the mapping artifact for any signal where sim/prod units differ (e.g., KVUtilization: divide prod value by 100)
3. Each prompt MUST reference `evolve_block_source` to locate the algorithm logic
4. Generated code MUST be validated by `validate-schema` before downstream consumption

### PR4 Deliverables

- **Stage 4 prompt:** `prompts/test.md` — build + test with structured retry logic, error classification, halt conditions
- **test-status CLI command:** `tools/transfer_cli.py test-status` — classifies `go build`/`go test` output into compilation, test_failure, infrastructure error classes
- **Escalation schema update:** Stage 4 halt reasons added to `tools/schemas/escalation.schema.json`

**PR4 obligations for downstream PRs:**
1. **Stage 4 success state:** Stage 4 success means `go build ./...`, `go vet ./...`, and `go test ./pkg/plugins/scorer/... -v` all pass in the llm-d-inference-scheduler submodule. There is no `stage4_output.json` — PR5 reads generated code paths from `stage3_output.json`.
2. **Retry state not persisted:** Retry counters live only in the interactive session. If Stage 4 halts and the operator restarts, counters reset to zero.

### PR5 Deliverables

- `tools/harness/evolved_algorithm.go` — EVOLVE-BLOCK penalty logic (replaces trivialAlgorithm in LoadAlgorithm)
- `tools/harness/evolved_scorer.go` — Wired EvolvedScorer.Score() with production metric translation
- `tools/harness/stats.go` — Kendall-tau rank correlation utility
- `tools/harness/suite_a_test.go` — Suite A: 200-tuple rank correlation equivalence (BC-6, BC-7)
- `tools/harness/suite_b_test.go` — Suite B: staleness rank stability, v1 informational (BC-8)
- `tools/harness/suite_c_test.go` — Suite C: concurrent safety + pile-on check (BC-9, BC-10)
- `tools/schemas/validation_results.schema.json` — Schema for Stage 5 output artifact
- `prompts/validate.md` — Stage 5 prompt template
- `docs/transfer/noise_characterization.md` — Noise characterization procedure
- `docs/transfer/calibration_log.md` — Per-transfer calibration log template
- CLI commands: `noise-characterize`, `benchmark` in `tools/transfer_cli.py`

**Key contracts:** Suite A Kendall-tau > 0.8, Suite C determinism + pile-on ≤ 2.0, noise CV ≤ 15%

## Algorithm Logic Boundary

PR1 captures *what signals the algorithm reads* (signal metadata: names, types, access paths, normalization). PR3 captures *what the algorithm does with those signals* (behavioral logic). Concretely, "algorithm logic" means:
1. Scoring weights applied to each signal
2. Penalty functions (e.g., low-KV penalty)
3. Decision thresholds (e.g., score cutoffs)
4. Composite signal computations (e.g., EffectiveLoad formula)
5. Conditional branching logic (e.g., SessionID affinity check)
