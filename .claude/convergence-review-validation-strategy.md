# Validation Strategy Review: BLIS-to-llm-d Transfer Design

**Focus:** Verification & Validation (V&V) coverage, fidelity validation, invariants, and hypothesis testing.

## CRITICAL FINDINGS (9)

1. **Stage 6 Suite A baseline: Kendall-tau threshold lacks justification** [CRITICAL]
   - Claim: Kendall-tau > 0.8 catches "logical inversions and threshold translation errors"
   - Problem: (a) No literature citation or empirical justification for 0.8 boundary; (b) Borderline range [0.75, 0.85] contradicts the 0.8 hard threshold — suggests uncertainty; (c) No power analysis or false-positive rate documented
   - Missing: Simulation validation that tau=0.8 actually detects the claimed error classes (inversions, off-by-one threshold errors) with high sensitivity
   - Falsification test: Create synthetic algorithms with known translation errors (inverted weights, ±1 threshold shifts) and verify detection rate at tau=0.8
   - Impact: A threshold chosen without empirical grounding may accept broken translations or reject correct ones

2. **Stage 6 Suite B staleness sensitivity: No per-signal threshold degradation model** [CRITICAL]
   - Claim: "If not [Kendall-tau > 0.7], the algorithm's thresholds are staleness-sensitive — flag for human review with a per-signal degradation report"
   - Problem: (a) Design specifies 3 repetitions with different seeds but doesn't define how to aggregate across seeds (mean? worst-case? per-seed pass/fail?); (b) No model for *which* staleness windows cause rank inversion — post-hoc reporting doesn't answer whether the algorithm is fixable vs. fundamentally incompatible
   - Missing: Decision tree for human reviewers: if signal X's staleness causes failures, is there a fallback (e.g., substitute a faster-updating proxy signal, adjust thresholds)?
   - Falsification test: Create an algorithm known to be staleness-robust (e.g., conditions only on very stable metrics) and one known to be fragile (tight thresholds on volatile metrics), verify Suite B correctly stratifies them
   - Impact: Human review comment will likely be "looks concerning" without actionable next steps

3. **Stage 7 go/no-go: "Noise characterization" threshold lacks statistical foundation** [CRITICAL]
   - Claim: "The improvement threshold must exceed 2× the observed CV to distinguish signal from noise"
   - Problem: (a) 2× is stated as rule-of-thumb but no citation; (b) This sets improvement threshold = 2×CV, but what if CV = 3% → threshold = 6%? Design says "If CV > 3%, raise threshold or use multi-run testing" but doesn't specify *how much* to raise; (c) No power calculation or Type I/II error budgets
   - Missing: Sampling distribution analysis — under what conditions does a 5% measured improvement with noise CV=2.5% reject the null (no improvement) at 95% confidence? Requires effect size, sample size, degrees of freedom
   - Falsification test: Run 5 baseline benchmarks, compute CV, set threshold = 2×CV, then intentionally degrade the algorithm 2.5× the threshold and verify detection rate
   - Impact: Go/no-go decision rests on an unjustified multiplier; risk of Type II error (missing real improvements) or Type I error (false positives)

4. **Stage 7 mechanism check: "First or tied-first" is a proxy for equivalence, not a proof** [CRITICAL]
   - Claim: Matched workload "must rank first or tied-first in improvement across all workloads. (Rationale: if an unrelated workload shows the largest improvement, the benefit may come from a confounding factor)"
   - Problem: (a) Ranking first ≠ mechanism preserved. A high-variance workload might show spurious first-place improvement by chance; (b) Rationale is backwards: if matched workload ranks *second*, that doesn't prove the mechanism failed — it could mean the algorithm generalizes better than expected; (c) Mechanism validation requires ablation (remove the conditional logic, verify improvement vanishes), not ranking
   - Missing: Specification of what "confounding factor" means and how ranking addresses it. Design should define mechanism-specific ablation tests (e.g., if algorithm uses prefix-match weighting, ablate that weighting and verify degradation)
   - Falsification test: Create synthetic scenario where matched workload ranks 2nd by 0.5% but is statistically dominant when confidence intervals are computed; verify design would reject this
   - Impact: A first-place ranking is necessary but not sufficient for mechanism validation

5. **Stage 4 retry loop: No diagnostic information on why retries occur or when to stop** [CRITICAL]
   - Claim: "Up to 3 retries. On 3x failure: stop pipeline, report errors — no PR created"
   - Problem: (a) Design says "error context appended to LLM prompt" but doesn't specify what information is included (compiler errors, test names, stack traces?); (b) No guidance on failure mode taxonomy (compilation vs. logic vs. test flake); (c) No criterion for stopping early if a failure is "known unfixable" vs. retrying all 3 times
   - Missing: Error classification scheme and early-exit rules (e.g., if "undefined reference to FooSignal" appears 3 times, stop immediately, as mapping artifact is stale)
   - Falsification test: Seed a code generation with a systematic error (e.g., wrong type for a signal accessor); verify that retries actually fix it vs. regenerating the same error
   - Impact: Retries may waste compute regenerating the same failure, or worse, fail after 3 attempts when an earlier exit would have been faster

6. **Signal equivalence validation: No specification of equivalence function or oracle** [CRITICAL]
   - Claim: Stage 6 includes "Signal upgrade validation" for richer llm-d signals
   - Problem: Design specifies test *structure* (per-request variation for prefix cache affinity) but no oracle (what does it mean for prefix match per-request to be "equivalent" to BLIS's aggregate CacheHitRate?). Is equivalence statistical rank correlation? Behavioral equivalence under all scenarios?
   - Missing: Formal equivalence definition: two signals are equivalent if (a) they rank-order endpoints identically, OR (b) the algorithm's conditional logic produces identical results, OR (c) something else? Without this, "equivalence" is undefined
   - Falsification test: Create an algorithm that specifically depends on detecting when per-request prefix match varies within an endpoint; verify it fails with aggregate CacheHitRate (expected), then verify Suite A's equivalence test catches this
   - Impact: Semantic equivalence tests may pass while the translation fails in practice

7. **Artifact prerequisites are TODO but no execution plan for creating them** [CRITICAL]
   - Claim: Three artifacts are "prerequisites — the pipeline cannot execute without them. They must be created before the first transfer attempt."
   - Problem: Design says "TODO" and lists who maintains them, but no design for *what* they contain beyond one-line summaries. E.g., `scorer_template.go.md` is "Example of a well-structured llm-d scorer" — what is the acceptance criterion? Is it one example, all patterns, anti-patterns?
   - Missing: Detailed specification or acceptance criteria for each artifact, not just title + purpose
   - Falsification test: Ask three different people to create `scorer_template.go.md` independently; if they create different templates, the spec was underspecified
   - Impact: When the first transfer attempt occurs, these artifacts may be incomplete, halting the pipeline mid-execution

8. **Cluster benchmark: "Health check" abort criterion is fuzzy** [CRITICAL]
   - Claim: "If error rate exceeds 10% or any endpoint becomes unresponsive within the first 60 seconds, abort"
   - Problem: (a) "Error rate" undefined: whose errors? LLM inference failures? Timeout errors? Request drops? (b) "Unresponsive" undefined: no response for how long? One request? All requests? What's the detection latency? (c) "Within first 60 seconds" — why 60s and not 120s or 10s?
   - Missing: Precise operational definitions (e.g., "error rate = (failed requests) / (total requests attempted)"; "unresponsive = no HTTP 2xx response within 30s for 3 consecutive requests")
   - Falsification test: Run benchmark and manually introduce failures at different rates (5%, 15%, 25%); verify abort decision is consistent
   - Impact: Benchmark aborts may be inconsistent or delayed, wasting time or collecting invalid data

9. **Stage 6 Suite C pile-on validation: No definition of "fair share"** [CRITICAL]
   - Claim: "No single endpoint receives more than 2× its fair share of the 20 requests"
   - Problem: (a) "Fair share" is 1/N × requests, but when exactly is fairness computed? After all 20 requests? Per window? (b) Does 2× threshold come from design analysis or heuristic? (c) An algorithm that distributes 11/10/9 or 11/9/0 violates 2× with 3 endpoints, but which is worse?
   - Missing: Definition of fair-share fairness metric (e.g., Gini coefficient, max-min ratio) and empirical justification for 2× threshold
   - Falsification test: Create an algorithm that intentionally loads one endpoint and verify detection at exactly 2× threshold
   - Impact: Pile-on validation may pass when the algorithm concentrates load unsustainably

---

## IMPORTANT FINDINGS (13)

10. **Suite A test coverage: "Cartesian product capped at 200" may undersample** [IMPORTANT]
    - Claim: "sample at min, median, max, and each threshold value ±1. Use Cartesian product across dimensions, capped at 200 tuples"
    - Problem: If algorithm conditions on 4 dimensions with 5 thresholds each, full product = 5^4 = 625 tuples → reduced to 200. Which tuples are dropped? Random sample? Stratified? No specification.
    - Missing: Sampling strategy and justification for the 200 cap. Is 200 sufficient coverage? Power analysis of Kendall-tau test with N=200 samples
    - Impact: If sampling is random, different runs may test different regions of the parameter space; stratified sampling would be more repeatable

11. **Suite B staleness windows: "Documented in the mapping artifact per signal" — no active verification** [IMPORTANT]
    - Claim: Staleness windows are drawn from mapping artifact
    - Problem: Mapping artifact is TODO. Design doesn't specify: (a) who validates that staleness windows are accurate (should they be measured on the actual target cluster?); (b) how often are they updated if scraper intervals change?
    - Missing: Validation method for staleness windows themselves — are they correct? Should Stage 1 include a scraper-configuration audit?
    - Impact: If staleness windows are wrong, Suite B results are invalid

12. **Stage 5 draft PR creation: Conditions for creating PR branch are unspecified** [IMPORTANT]
    - Claim: "PR created at Stage 5 as draft"
    - Problem: (a) Are new branches created? Branches named how? (b) What if the branch already exists (resume case)? (c) Force-push or abort? (d) PR number assignment — who decides (automation, user)?
    - Missing: Workflow for branch/PR lifecycle (create vs. update, conflict resolution, idempotency)
    - Impact: PR infrastructure is not clearly defined; edge cases (resume, conflict, branch conflicts) may cause failures

13. **Stage 7 baseline caching: "Cache baseline results with cluster config fingerprint" — no staleness bound** [IMPORTANT]
    - Claim: "Reuse if <24h old and config unchanged"
    - Problem: (a) What's the "cluster config fingerprint"? All K8s manifests? Scheduler configuration? LLM model? (b) 24h threshold is arbitrary — cluster behavior may drift beyond 24h from config changes outside the fingerprint (e.g., underlying hardware maintenance)
    - Missing: Definition of fingerprint and sensitivity analysis on 24h threshold
    - Impact: Cached baseline may not be representative, leading to false performance claims

14. **Stage 2 staleness prevention: "Commit hash within 50 commits of HEAD"** [IMPORTANT]
    - Claim: "Stage 1 must verify that the mapping artifact's 'last verified against' commit hash is within 50 commits of the current llm-d HEAD"
    - Problem: (a) 50 commits is ~2-3 days of development; what if breaking changes landed? (b) No automation to check this; if Stage 1 is manual (it's not specified), how is this enforced? (c) If Stage 1 detects staleness, design says "halt with a message" — manual workflow to update?
    - Missing: Automated check, clear procedure for updating mapping artifact, definition of what "breaking change" means
    - Impact: Stale mapping artifact could pass the 50-commit check but still map signals incorrectly

15. **Stage 6 Suite A "borderline" flag logic: Human review instruction missing** [IMPORTANT]
    - Claim: "If Kendall-tau is in the range [0.75, 0.85], flag as borderline and require human review before proceeding"
    - Problem: Design doesn't say: (a) what should the human reviewer check? (b) are they deciding to accept the translation, or return to Stage 3? (c) what information is provided to them?
    - Missing: Human reviewer's checklist/flowchart for borderline cases
    - Impact: Human reviews may be inconsistent (some accept, some reject)

16. **Mechanism check "provisional" tag: What happens to provisionally-validated PRs?** [IMPORTANT]
    - Claim: "If only one run is available (v1 default), the mechanism check result is tagged as provisional"
    - Problem: Design says PRs are promoted to "ready-for-review" but doesn't specify: (a) if provisional passes, is it promoted? (b) is there a label "provisional" or is the distinction internal? (c) will code reviewers accept provisional results?
    - Missing: Workflow for provisional PRs (merge policy, re-validation trigger)
    - Impact: Provisional results may be merged without multi-run confirmation, reducing confidence

17. **Suite C C2 sequential arrivals: "Rapid succession" timing undefined** [IMPORTANT]
    - Claim: "Issue 20 requests in rapid succession, with each request seeing a snapshot that reflects all prior routing decisions' effects"
    - Problem: What is "rapid succession"? Sub-millisecond? Sub-second? If the real system processes at 1000 req/s but the test issues requests at 100 req/s, the concurrency dynamics differ.
    - Missing: Specification of request inter-arrival time distribution (e.g., Poisson with λ=1000/s, or fixed intervals)
    - Impact: Test may not exercise the concurrency patterns present in production

18. **Stage 4 Unit Test: "Pre-existing issue" vs. new-code failure detection** [IMPORTANT]
    - Claim: "If only the full suite fails (pre-existing issue), flag for human review instead of retrying code generation"
    - Problem: How is "pre-existing" detected? (a) by checking if the failure also occurs on the baseline branch? (Design doesn't specify this check) (b) If not checked, this rule is never invoked. (c) If checked, it adds latency (full test suite run on baseline branch)
    - Missing: Definition of detection method for pre-existing failures
    - Impact: Design intent may not be executed; new code may get retried when it shouldn't, or vice versa

19. **Equivalence test oracle for "no panics, no NaN"** [IMPORTANT]
    - Claim: Suite A/B/C include assertions "No panics, no NaN, scores in expected range"
    - Problem: (a) "Expected range" is unspecified — is it [0, 1]? [0, ∞)? Per-scorer-specific? (b) What's the test oracle for checking these post-hoc? (c) No specification of what happens if assertions fail
    - Missing: Formal specification of score bounds and failure handling
    - Impact: Vague assertions may pass without catching real bugs

20. **Stage 8: No specification of how go/no-go criteria combine** [IMPORTANT]
    - Claim: Multiple criteria: improvement ≥5%, no regression >2%, P95 <2%, mechanism check pass
    - Problem: (a) Are these AND or OR? Design says "default thresholds" suggesting AND, but not explicit. (b) If one metric fails (e.g., improvement is 5.1% but P95 regresses 2.5%), does it fail? (c) No weighting scheme if criteria trade off
    - Missing: Boolean combination logic and tie-breaking rules
    - Impact: Go/no-go decision may be ambiguous in corner cases

22. **Suite B aggregation across seeds: No specification** [IMPORTANT]
    - Claim: "Run 3 repetitions with different staleness seeds per repetition. Assert Kendall-tau remains > 0.7 across all repetitions"
    - Problem: (a) Does "across all repetitions" mean ALL three must pass (AND), or average must exceed 0.7? (b) If one seed is an outlier, is it dropped? (c) How are seeds chosen (random, fixed)?
    - Missing: Aggregation function and outlier handling
    - Impact: Test results may be inconsistent

---

## SUGGESTION FINDINGS (5)

23. **Future extension #6: "Statistical confidence" correctly identifies a major gap** [SUGGESTION]
    - Note: Design acknowledges that single-run benchmarks are not statistically rigorous, and notes that v1 defaults to provisional results
    - Recommendation: Consider requiring ≥2 runs for the v1 prototype, even if staged (baseline + treatment) rather than 3 independent runs. This would reduce the "provisional" tag and enable basic significance testing

24. **Stage 1 extract: No specification of where best program artifact comes from** [SUGGESTION]
    - Claim: "Input: Best program artifact + experiment metadata"
    - Issue: Design doesn't specify: is this an OpenEvolve checkpoint? A hand-selected artifact? What format?
    - Recommendation: Specify the artifact source and format (e.g., "best_program.json from checkpoint_N/")

25. **Integration tests: "Mocked endpoints" are not production-like** [SUGGESTION]
    - Claim: Integration tests use "mocked endpoints"
    - Issue: Mocks may not exercise realistic combinations of metrics. E.g., a mock with zero queue depth and 100% KV utilization may be unrealistic.
    - Recommendation: Generate realistic synthetic endpoint states based on the training/validation data from the BLIS experiment

26. **Stage 7 workload matching: No tie-breaking rule if multiple archetypes are similar** [SUGGESTION]
    - Claim: "Identify the matched workload: the llm-d benchmark workload whose archetype matches the BLIS workload"
    - Issue: If BLIS workload is "high-concurrency with moderate prefix reuse," both "random concurrent" and "multi-turn chat" may be reasonable matches.
    - Recommendation: Define a precedence or similarity metric for workload matching

27. **Signal mapping: No version pinning for the mapper itself** [SUGGESTION]
    - Issue: Design pins llm-d commit but doesn't pin the OpenEvolve codebase version. If BLIS signals change (e.g., new signal added), mapping artifact becomes incomplete.
    - Recommendation: Pin mapping artifact to both llm-d AND OpenEvolve commits

---

## Summary Statistics

- **Total findings:** 27
- **CRITICAL:** 9
- **IMPORTANT:** 13
- **SUGGESTION:** 5

## Key Gaps in V&V Strategy

### Verification (Does it do what's specified?)
- Missing: Unit test acceptance criteria for the generated scorer
- Missing: Integration test oracle (what makes a score "valid"?)
- Missing: Build/lint/test suite integration point (how is "full suite fails" detected?)

### Validation (Does it do what's needed?)
- Missing: Formal specification of "equivalence" (rank correlation alone is insufficient)
- Missing: Ablation tests to validate mechanism (not just ranking)
- Missing: Real-world fidelity validation (staleness windows, endpoint state distributions)
- Missing: Hypothesis experiments to validate design assumptions

### Invariants Not Specified
- No statement of what must be true after each stage
- No postcondition verification

### Falsifiability
- Design assumes certain failure modes can be detected by Kendall-tau, ranking, and heuristic checks
- No experiments designed to validate these detection capabilities
- No definition of what would prove the design wrong

---

## Recommended Next Steps

1. **Add falsification tests** to Stage 6: Create synthetic algorithms with known translation errors; verify they are detected
2. **Formalize equivalence criteria** with both rank-correlation and behavioral components
3. **Specify all thresholds with empirical grounding**: run pilot noise-characterization study on target cluster
4. **Add ablation tests** to mechanism validation (remove conditional logic, verify improvement disappears)
5. **Complete artifact specifications** with acceptance criteria before starting any transfer
6. **Automate Stage 1 staleness check** and define update procedure
7. **Define human reviewer checklists** for borderline and pre-existing-failure cases

