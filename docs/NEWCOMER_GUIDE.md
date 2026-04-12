# sim2real — Newcomer's Guide

> **What is this repo?**
> `sim2real` transfers AI-discovered LLM routing algorithms from a discrete-event
> simulator (`inference-sim`) into production code (`llm-d-inference-scheduler`).
> An evolutionary optimizer found a routing algorithm that beats the best hand-tuned
> baseline by **+11.5%** on end-to-end latency. This pipeline automates the translation,
> testing, and deployment of that algorithm as a production Go scorer plugin.

---

## Table of Contents

1. [Mental Model: What's Actually Happening](#1-mental-model)
2. [Repository Layout](#2-repository-layout)
3. [Submodules](#3-submodules)
4. [The Input: blis_router/](#4-the-input-blis_router)
5. [The Pipeline: Six Stages](#5-the-pipeline-six-stages)
6. [How to Run the Pipeline](#6-how-to-run-the-pipeline)
7. [Key Config: env_defaults.yaml](#7-key-config-env_defaultsyaml)
8. [Workspace Artifacts](#8-workspace-artifacts)
9. [CLI Reference](#9-cli-reference)
10. [Testing](#10-testing)
11. [Common Failure Modes](#11-common-failure-modes)
12. [Glossary](#12-glossary)

---

## 1. Mental Model

```
┌─────────────────────────────────────────────────────────────────────┐
│  inference-sim (submodule)                                          │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  Discrete-event LLM simulator runs 50 iterations of           │  │
│  │  evolutionary optimization → finds best routing algorithm     │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                            │                                         │
│                            ▼                                         │
│             blis_router/best/best_program.go                        │
│             (evolved algorithm — Go code for the simulator)         │
└────────────────────────────────────────────────────────────────────-┘
                              │
                              │  sim2real pipeline (this repo)
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Stage 1: Extract  →  Stage 2: Translate  →  Stage 3: Generate     │
│  Stage 4: Test     →  Stage 4.5: Build/Push  →  Stage 5: Validate  │
│  Stage 6: PR                                                         │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  llm-d-inference-scheduler (submodule)                              │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  Production Go scorer plugin — same logic, real signals        │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

The **sim** uses synthetic signals (`InFlightRequests_sim`, `KVUtilization_sim`).
**Production** has equivalent signals but accessed via different Go struct fields
with different scales (e.g., `KVUtilization` is 0–100 in prod vs 0–1 in sim).
The pipeline handles this signal mapping and normalization automatically.

Each pipeline stage is driven by a **prompt template** (`prompts/<stage>.md`)
that an LLM agent (Claude) reads and executes step-by-step. The pipeline is
not a traditional script — it is a structured set of instructions for an AI agent.

---

## 2. Repository Layout

```
sim2real/
├── blis_router/              ← Input: evolved algorithm + workloads
│   ├── best/
│   │   ├── best_program.go          # The evolved routing algorithm (EVOLVE-BLOCK inside)
│   │   └── best_program_info.json   # Metrics, iteration number, parent lineage
│   ├── workloads/                   # Traffic profiles used in simulation
│   ├── baselines/                   # Comparison baseline algorithms
│   ├── llm_config.yaml             # LLM + hardware config from simulation
│   └── CLUSTER.md                  # Hardware spec to reproduce on a real cluster
│
├── config/
│   └── env_defaults.yaml           # Infrastructure defaults (edit per environment)
│
├── docs/
│   ├── transfer/
│   │   ├── blis_to_llmd_mapping.md  # Signal name mapping: sim → production
│   │   ├── scorer_template.go.md   # Go scorer template used in Stage 3
│   │   └── calibration_log.md      # Post-transfer calibration notes
│   └── plans/                      # Design docs and implementation plans
│
├── prompts/                        # ← Pipeline stage prompt templates (run these)
│   ├── transfer.md                 # Orchestrator: runs all stages 1–6
│   ├── extract.md                  # Stage 1
│   ├── translate.md                # Stage 2
│   ├── generate.md                 # Stage 3
│   ├── test.md                     # Stage 4
│   ├── build-push.md               # Stage 4.5
│   ├── validate.md                 # Stage 5
│   └── pr.md                       # Stage 6
│
├── tools/
│   ├── transfer_cli.py             # Python CLI: extract, validate-schema, test-status, etc.
│   ├── schemas/                    # JSON Schema files for artifact validation
│   └── harness/                    # Go test harness (Stage 3/4 equivalence tests)
│
├── workspace/                      # ← Generated artifacts (gitignored, do not commit)
│   ├── algorithm_summary.json      # Stage 1 output
│   ├── signal_coverage.json        # Stage 2 output
│   ├── stage3_output.json          # Stage 3 output (paths to generated files)
│   ├── validation_results.json     # Stage 5 output
│   └── tekton/                     # Tekton pipeline artifacts (Stage 3 generates, Stage 5 uses)
│
├── inference-sim/                  # Submodule: discrete-event simulator
├── llm-d-inference-scheduler/      # Submodule: production scheduler (target)
├── llm-d-benchmark/                # Submodule: benchmark harness
└── tektonc-data-collection/        # Submodule: cluster data collection pipeline
```

> **Rule:** Never edit files under `workspace/` directly. They are generated by
> pipeline stages. Fix bugs in the stage prompt or CLI tool that generates them.

---

## 3. Submodules

The repo has four submodules. Initialize them before running the pipeline:

```bash
# Initialize all submodules at once
git submodule update --init

# Or initialize specific ones
git submodule update --init inference-sim
git submodule update --init llm-d-inference-scheduler
```

| Submodule | Role |
|-----------|------|
| `inference-sim/` | Source of the evolved algorithm; also provides the `blis` CLI used in Stage 5 noise runs |
| `llm-d-inference-scheduler/` | Production target — the pipeline writes Go scorer files here |
| `llm-d-benchmark/` | Benchmark tooling for cluster-level validation |
| `tektonc-data-collection/` | Tekton-based cluster data collection pipeline |

---

## 4. The Input: blis_router/

Before the pipeline runs, the input artifacts must already exist in `blis_router/best/`.
These are the outputs of the evolutionary optimization run in `inference-sim`.

### best_program.go — The Evolved Algorithm

This file contains the routing logic discovered by the optimizer. The key part is
the **EVOLVE-BLOCK** — a delimited section of Go code that implements the scoring logic:

```
// EVOLVE-BLOCK-START
...scoring logic using InFlightRequests, KVUtilization, etc...
// EVOLVE-BLOCK-END
```

The pipeline extracts exactly this block, maps its signals to production equivalents,
and generates a new Go file that implements the same logic using production APIs.

### What the evolved algorithm does

The router extends the standard 1:1 (prefix-affinity + load-balance) baseline with:

1. **Adaptive prefix-affinity decay** — reduces prefix-affinity weight when the
   best-matched instance is overloaded. Prevents hotspotting.
2. **KV pressure penalty** — penalizes instances with >90% KV utilization.
   Avoids memory preemption before it happens.
3. **Fresh load tiebreaker** — adds a small bonus using the synchronous
   `InFlightRequests` signal to break ties between otherwise equal instances.

**Performance**: +11.5% over the 1:1 baseline on a prefix-heavy workload.

### llm_config.yaml

Describes the simulation environment: model name, GPU type, tensor parallelism,
number of instances, vLLM version. Stage 3 reads this to generate matching
Tekton deployment configs.

---

## 5. The Pipeline: Six Stages

Each stage is a markdown file in `prompts/` that an AI agent (Claude Code) reads
and executes. Between stages, artifacts are validated by schema and semantic checks.
**Any stage that fails halts the pipeline** — there is no silent skipping.

### Stage 1: Extract (`prompts/extract.md`)

**What it does:** Parses `best_program.go` to extract the EVOLVE-BLOCK and identify
the signals it uses.

**Input:** `blis_router/best/best_program.go`, `blis_router/best/best_program_info.json`

**Output:** `workspace/algorithm_summary.json`

```json
{
  "algorithm_name": "blis-weighted-scoring",
  "signals": [
    {"name": "InFlightRequests", "type": "gauge", ...},
    {"name": "KVUtilization", "type": "gauge", "normalization_note": "divide_prod_by_100", ...}
  ],
  "scope_validation_passed": true,
  "evolve_block_content_hash": "<sha256>"
}
```

**Key check:** `scope_validation_passed` must be `true`. If the algorithm uses
simulator-only constructs that can't translate to production, the pipeline halts here.

---

### Stage 2: Translate (`prompts/translate.md`)

**What it does:** Maps each simulation signal to its production equivalent using
`docs/transfer/blis_to_llmd_mapping.md`. Produces a coverage report.

**Input:** `workspace/algorithm_summary.json`, `docs/transfer/blis_to_llmd_mapping.md`

**Output:** `workspace/signal_coverage.json`

```json
{
  "signals": [
    {
      "sim_name": "KVUtilization",
      "prod_name": "KVCacheUsagePercent",
      "prod_access_path": "endpoint.Metrics.KVCacheUsagePercent",
      "normalization": "divide_prod_by_100",
      "fidelity_rating": "high"
    }
  ],
  "coverage_complete": true,
  "commit_hash": "<llm-d-inference-scheduler HEAD>"
}
```

**Key check:** `coverage_complete` must be `true`. If any signal can't be mapped,
Stage 3 cannot generate correct code.

**Submodule staleness check:** The `llm-d-inference-scheduler` submodule commit
must match the commit pinned in `docs/transfer/blis_to_llmd_mapping.md`. This
ensures the signal field names haven't changed.

---

### Stage 3: Generate (`prompts/generate.md`)

**What it does:** The most complex stage. Reads the EVOLVE-BLOCK and generates
production Go code:
- `llm-d-inference-scheduler/pkg/plugins/scorer/<name>.go` — the scorer plugin
- `llm-d-inference-scheduler/pkg/plugins/scorer/<name>_test.go` — unit tests
- Modifies `llm-d-inference-scheduler/pkg/plugins/register.go` to register the new scorer

Also generates Tekton benchmarking artifacts for Stage 5:
- `workspace/tekton/algorithm_values.yaml` — BLIS-derived values (model, GPU, scorer config)
- `workspace/tekton/values.yaml` — merged config (algorithm values + env_defaults)
- `workspace/tekton/pipelinerun-{noise,baseline,treatment}.yaml` — Tekton PipelineRun stubs

**Output:** `workspace/stage3_output.json` (paths to all generated files)

**Two-layer config merge (Step 8):**

```
config/env_defaults.yaml          ← infrastructure (gateway, connection pool, etc.)
         +
workspace/tekton/algorithm_values.yaml  ← BLIS-derived (model, GPU, scorer)
         │
         ▼  merge-values CLI
workspace/tekton/values.yaml      ← consumed by Stage 5 compile-pipeline
```

---

### Stage 4: Test (`prompts/test.md`)

**What it does:** Builds and tests the generated scorer plugin.

```bash
cd llm-d-inference-scheduler
go build ./...          # must pass
go vet ./...            # must pass
go test -timeout 10m ./pkg/plugins/scorer/... -v   # must pass
```

**Retry logic:** Stage 4 has structured retry logic. Compilation and test failures
are classified by `tools/transfer_cli.py test-status`. The LLM agent reads the
error, fixes the generated code, and retries. Limits:
- Max 3 retries per error class (compilation, test failure)
- Max 5 total retries
- Halts immediately on identical consecutive errors (loop detection)

**No output artifact on success** — pass is implicit (the builds pass).

---

### Stage 4.5: Build & Push (`prompts/build-push.md`)

**What it does:** Builds a Docker/Podman image from the modified
`llm-d-inference-scheduler` submodule (which now contains the new scorer plugin)
and pushes it to a container registry. This image is used by the treatment phase
in Stage 5 cluster benchmarks.

**Prerequisites:** Configure your registry in `config/env_defaults.yaml`:
```yaml
stack:
  gaie:
    epp_image:
      build:
        hub: ghcr.io/<your-org>
```
Also log in: `echo $GITHUB_PAT | podman login ghcr.io -u <username> --password-stdin`

---

### Stage 5: Validate (`prompts/validate.md`)

**What it does:** Multi-suite validation of the transferred algorithm.

#### Fast mode vs Full mode

Controlled by `pipeline.fast_iteration` in `config/env_defaults.yaml`:

| | Fast mode (`true`) | Full mode (`false`) |
|--|--|--|
| Suite A (unit equivalence) | ✅ | ✅ |
| Suite B (informational) | ✅ | ✅ |
| Suite C (cluster equivalence) | ✅ | ✅ |
| Noise characterization | ❌ skipped | ✅ |
| Mechanism check | ❌ skipped | ✅ |
| Stage 6 PR creation | ❌ skipped | ✅ |

Use **fast mode** while iterating; switch to **full mode** when the algorithm is
ready for production submission.

#### The three suites

**Suite A — Simulation equivalence:**
Runs the evolved algorithm (sim) against the generated scorer (production) on
the same inputs. Checks that outputs match within tolerance.

**Suite B — Signal fidelity (informational):**
Compares signal ranges between sim and production. Flags mismatches but does
not gate the pipeline (v1 behavior).

**Suite C — Cluster benchmark equivalence:**
Submits Tekton pipelines to a real Kubernetes cluster via `tkn`:
- Noise run (full mode only): characterizes cluster noise floor
- Baseline run: deploys standard load-aware scorer, measures latency
- Treatment run: deploys generated scorer plugin, measures latency

**Output:** `workspace/validation_results.json` with `overall_verdict: PASS | INCONCLUSIVE | FAIL`

#### Cluster prerequisites (Stage 5)

```bash
export NAMESPACE=<your-namespace>
kubectl config current-context   # verify correct cluster
tkn version                      # Tekton CLI installed
kubectl get pods -n tekton-pipelines   # Tekton operator running
kubectl get pvc data-pvc -n $NAMESPACE # PVC exists
```

---

### Stage 6: PR (`prompts/pr.md`)

**What it does:** Creates pull requests in the target repositories with the
generated scorer plugin. Updates the calibration log.

**Skipped in fast mode** (`pipeline.fast_iteration: true`).

---

## 6. How to Run the Pipeline

### First-time setup

```bash
# 1. Clone the repo with submodules
git clone --recurse-submodules <repo-url>
cd sim2real

# 2. Set up Python environment
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Verify submodules
git submodule status
# Lines should start with a commit hash, NOT '-' (which means uninitialized)
```

### Running the full pipeline (orchestrated)

Open `prompts/transfer.md` in Claude Code and run it. The orchestrator:
1. Verifies all prerequisites
2. Records the current repo commit (`workspace/pipeline_commit.txt`)
3. Executes Stages 1–6 in sequence
4. Validates artifacts between each stage

### Running individual stages

If you need to re-run a specific stage (e.g., Stage 3 after updating the scorer template):

```
# Open the stage prompt in Claude Code
prompts/extract.md      ← Stage 1
prompts/translate.md    ← Stage 2
prompts/generate.md     ← Stage 3
prompts/test.md         ← Stage 4
prompts/build-push.md   ← Stage 4.5
prompts/validate.md     ← Stage 5
```

Each stage validates its input artifacts first. You cannot skip stages — the
artifacts form a dependency chain.

### Fast iteration vs full validation

Edit `config/env_defaults.yaml`:
```yaml
pipeline:
  fast_iteration: true   # quick iteration (no noise/mechanism check/PR)
  # fast_iteration: false  # full validation + PR creation
```

---

## 7. Key Config: env_defaults.yaml

`config/env_defaults.yaml` is **version-controlled** and contains infrastructure
choices that persist across algorithm runs. Edit this file (not workspace files)
when changing environment settings.

| Key | What it controls |
|-----|-----------------|
| `stack.gateway.helmValues.gateway.provider` | Gateway provider: `istio` or `kgateway` |
| `stack.model.vllm_image` | Override vLLM image (e.g., use llm-d custom build); comment out to use sim image |
| `stack.gaie.epp_image.build.hub` | Your container registry for the treatment EPP image |
| `stack.gaie.baseline.helmValues` | Baseline scorer config (load-aware scorer) |
| `observe.noise_runs` | Number of noise characterization runs (full mode) |
| `observe.request_multiplier` | Scale workload `num_requests` for real-cluster (default: 10x) |
| `pipeline.fast_iteration` | `true` = skip noise/mechanism/PR; `false` = full validation |

---

## 8. Workspace Artifacts

All files under `workspace/` are generated. The pipeline stages produce and consume them:

```
Stage 1  →  workspace/algorithm_summary.json
Stage 2  →  workspace/signal_coverage.json
Stage 3  →  workspace/stage3_output.json
             workspace/tekton/algorithm_values.yaml
             workspace/tekton/values.yaml
             workspace/tekton/pipelinerun-{noise,baseline,treatment}.yaml
Stage 4  →  (no artifact; success is implicit)
Stage 4.5 → (updates workspace/tekton/algorithm_values.yaml with EPP image tag)
Stage 5  →  workspace/validation_results.json
             workspace/comparison_table.txt (full mode only)
```

Each artifact is validated against a JSON Schema in `tools/schemas/`. If a stage
halts with an error, it writes `workspace/escalation.json` describing the failure.

**Do not fix workspace files directly.** Find the source that generates them
(the stage prompt or CLI tool) and fix there.

---

## 9. CLI Reference

All CLI commands use `tools/transfer_cli.py`. Run from the repo root with the
venv activated.

```bash
# Stage 1: Extract algorithm metadata
python tools/transfer_cli.py extract blis_router/best/

# Stage 1 with CI strict mode (enforces minimum signal count)
python tools/transfer_cli.py extract --strict blis_router/best/

# Validate a workspace artifact against its JSON Schema
python tools/transfer_cli.py validate-schema workspace/algorithm_summary.json
python tools/transfer_cli.py validate-schema workspace/signal_coverage.json
python tools/transfer_cli.py validate-schema workspace/stage3_output.json

# Classify go build/test output (used in Stage 4 retry logic)
# Exit 0 = clean, 1 = error found, 2 = infrastructure error
cat /tmp/build_output.txt | python tools/transfer_cli.py test-status

# Merge env_defaults + algorithm_values → values.yaml (Stage 3 Step 8)
python tools/transfer_cli.py merge-values \
  --env config/env_defaults.yaml \
  --algorithm workspace/tekton/algorithm_values.yaml \
  --out workspace/tekton/values.yaml

# Compute mechanism check (full mode Stage 5)
python tools/transfer_cli.py benchmark \
  --noise workspace/noise_results.json \
  --baseline workspace/baseline_results.json \
  --treatment workspace/treatment_results.json \
  --signal-coverage workspace/signal_coverage.json \
  --workloads-dir blis_router/workloads/ \
  --out workspace/benchmark_output.json

# Print latency comparison table (Stage 5)
python tools/transfer_cli.py compare \
  --baseline workspace/baseline_results.json \
  --treatment workspace/treatment_results.json \
  --out workspace/comparison_table.txt
```

---

## 10. Testing

```bash
# Run all Python unit tests
python -m pytest tools/ -v

# Lint (if ruff is installed)
ruff check tools/

# Verify the Go submodule builds cleanly before running Stage 4
cd llm-d-inference-scheduler
go build ./...
go vet ./...
cd ..
```

---

## 11. Common Failure Modes

### "HALT: stale submodule commit"
The `llm-d-inference-scheduler` submodule has been updated since the
`blis_to_llmd_mapping.md` was written. The signal field names may have changed.
Update `docs/transfer/blis_to_llmd_mapping.md` to reflect the new commit,
then re-run Stage 2.

### "HALT: scope_validation_passed is false"
The evolved algorithm uses patterns that don't map to production (e.g.,
simulator-only global state, non-standard data structures). Review the
EVOLVE-BLOCK in `blis_router/best/best_program.go`.

### Stage 4 halts with `identical_consecutive_errors`
The LLM is making the same mistake twice. The retry is not addressing the root
cause. Manually inspect the error in `workspace/escalation.json` and fix the
generated scorer file directly, then re-run Stage 4.

### Stage 4 halts with `build_compilation_failure` referencing non-generated files
The `llm-d-inference-scheduler` submodule has a pre-existing build failure
unrelated to the generated code. Run `cd llm-d-inference-scheduler && go build ./...`
to confirm, then fix the submodule before running Stage 4.

### Stage 5 cluster pipeline fails with `NAMESPACE not set`
```bash
export NAMESPACE=<your-kubernetes-namespace>
```

### `workspace/algorithm_summary.json` is stale after a failed Stage 1
Stage 1 deletes the output artifact before running. If it fails mid-run, the
artifact may not exist or may be from a prior successful run. Always use the
exit code (not file presence) to determine success.

### `merge-values` fails with PyYAML not found
```bash
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 12. Glossary

| Term | Meaning |
|------|---------|
| **EVOLVE-BLOCK** | The delimited section of `best_program.go` containing the evolved scoring logic. Bounded by `// EVOLVE-BLOCK-START` and `// EVOLVE-BLOCK-END`. |
| **BLIS** | The name of the evolved router (BLIS-weighted-scoring). Named after the evolutionary optimization experiment. |
| **Scorer plugin** | A Go struct implementing `scheduling.Scorer` in `llm-d-inference-scheduler`. Scores endpoint candidates for each incoming request. |
| **Signal** | A runtime metric used by the routing algorithm (e.g., `InFlightRequests`, `KVUtilization`). Has different names and scales in sim vs production. |
| **signal_coverage.json** | The Stage 2 artifact mapping sim signal names to production field names + normalization rules. |
| **EPP image** | The Endpoint Picker Process container image. Contains the compiled scheduler binary with scorer plugins statically linked in. |
| **Tekton pipeline** | Kubernetes-native CI/CD pipelines used to run cluster benchmarks in Stage 5. |
| **fast_iteration** | A config flag that skips noise characterization, mechanism check, and PR creation. Use `true` while iterating, `false` for production submission. |
| **escalation.json** | Written to `workspace/` when a stage halts. Contains `stage`, `halt_reason`, and `details` describing the failure. |
| **Algorithm values** | BLIS-derived Tekton config: model name, GPU count, scorer config. Written to `workspace/tekton/algorithm_values.yaml` by Stage 3. |
| **env_defaults** | Infrastructure config committed to `config/env_defaults.yaml`. Gateway provider, connection pool settings, registry. |
| **workspace/** | All generated inter-stage artifacts. Gitignored. Regenerated by running the pipeline. Never edit directly. |
