# Simplified Transfer Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 9-stage prompt-driven transfer pipeline with 3 Claude skills (`/sim2real-setup`, `/sim2real-prepare`, `/sim2real-deploy`) plus a multi-model AI review tool, all living under `.claude/`.

**Architecture:** Four skills under `.claude/skills/`, each with a `SKILL.md` and supporting scripts. The setup skill calls an idempotent `setup.sh`. The prepare skill consolidates Extract + Translate + Generate + AI Review. The deploy skill consolidates Test + Equivalence Gate + Build (in-cluster via Kaniko) + Benchmarks + PR. A `review_translation.py` script handles multi-model LiteLLM calls. Main repo is read-only.

**Tech Stack:** Bash (setup script, skill orchestration), Python (LiteLLM review tool), Jinja2/YAML (existing tektonc tooling), Kaniko (in-cluster image builds)

---

## File Structure

```
.claude/skills/
├── sim2real-setup/
│   ├── SKILL.md              # Setup skill definition
│   └── scripts/
│       └── setup.sh          # Idempotent cluster setup
├── sim2real-prepare/
│   ├── SKILL.md              # Phase 1 skill definition
│   └── scripts/
│       ├── review_translation.py   # Multi-model review orchestrator
│       └── build_review_request.py # JSON request builder for reviews
└── sim2real-deploy/
    └── SKILL.md              # Phase 2 skill definition
```

---

### Task 1: `/sim2real-setup` Skill — SKILL.md

**Files:**
- Create: `.claude/skills/sim2real-setup/SKILL.md`

- [ ] **Step 1: Create the SKILL.md with frontmatter**

```markdown
---
name: sim2real-setup
description: |
  One-time cluster and environment setup for the sim2real transfer pipeline.
  Creates namespace, secrets, RBAC roles, PVCs, and deploys Tekton tasks.
  Idempotent — safe to re-run.
argument-hint: "[--namespace NAME] [--hf-token TOKEN]"
user-invocable: true
allowed-tools:
  - Bash(**/setup.sh *)
  - Bash(kubectl *)
  - Bash(oc *)
  - Bash(tkn *)
  - Bash(python3 *)
  - Bash(pip *)
  - Glob
  - Read
---

# sim2real-setup

One-time setup for the sim2real transfer pipeline. This skill configures
your cluster environment and deploys all required Tekton resources.

## Finding the Script

**IMPORTANT:** Before running any commands, locate the setup script:

```
Glob: **/skills/sim2real-setup/scripts/setup.sh
```

Store the result as `[SETUP_SCRIPT]`.

## Usage

```bash
[SETUP_SCRIPT]
[SETUP_SCRIPT] --namespace my-ns --hf-token hf_abc123
```

The script prompts interactively for any values not provided via flags or
environment variables (`$NAMESPACE`, `$HF_TOKEN`).

## What It Does

1. Verifies prerequisites: `kubectl`, `tkn`, `python3`, `gh`, `podman`/`docker`
2. Initializes git submodules: `inference-sim`, `llm-d-inference-scheduler`, `tektonc-data-collection`
3. Creates Python venv and installs dependencies
4. Prompts for `NAMESPACE` (if not set via `$NAMESPACE` or `--namespace`)
5. Prompts for HuggingFace token (if not set via `$HF_TOKEN` or `--hf-token`)
6. Creates namespace
7. Creates `hf-secret` in namespace
8. Applies RBAC roles from `tekton/roles.yaml`
9. Creates PVCs: `model-pvc` (300Gi), `data-pvc` (20Gi), `source-pvc` (20Gi)
10. Verifies Tekton operator is running
11. Deploys Tekton step and task definitions
12. Prompts for container registry and creates `registry-secret`

> **OpenShift users:** The script detects OpenShift and runs additional
> commands: `oc adm policy add-scc-to-user anyuid -z default -n $NAMESPACE`.
> PVCs use `ocs-storagecluster-cephfs` storageClassName on OpenShift.

## After Setup

Edit `config/env_defaults.yaml` for infrastructure choices:
- `stack.gateway.helmValues.gateway.provider` — `istio` or `kgateway`
- `stack.model.vllm_image` — optional vLLM image override
- `stack.gaie.epp_image.build.hub` — your container registry
- `pipeline.fast_iteration` — `true` to skip noise gate + mechanism check
- `observe.request_multiplier` — workload scaling factor

Then run `/sim2real-prepare` to start the pipeline.
```

- [ ] **Step 2: Commit**

```bash
git add .claude/skills/sim2real-setup/SKILL.md
git commit -m "feat: add /sim2real-setup skill definition"
```

---

### Task 2: `/sim2real-setup` — setup.sh Script

**Files:**
- Create: `.claude/skills/sim2real-setup/scripts/setup.sh`

- [ ] **Step 1: Create the setup script**

```bash
#!/usr/bin/env bash
set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step()  { echo -e "\n${BLUE}━━━ Step $1: $2 ━━━${NC}"; }

# ── Parse args ──────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --hf-token)  HF_TOKEN="$2"; shift 2 ;;
    --registry)  REGISTRY="$2"; shift 2 ;;
    *) err "Unknown arg: $1"; exit 1 ;;
  esac
done

# ── Find repo root ─────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TEKTONC_DIR="${REPO_ROOT}"
# Walk up to find sim2real root (parent of tektonc-data-collection)
SIM2REAL_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"

# ── Step 1: Prerequisites ──────────────────────────────────────────
step 1 "Checking prerequisites"
MISSING=()
for cmd in kubectl tkn python3 gh; do
  command -v "$cmd" &>/dev/null || MISSING+=("$cmd")
done
# Check for container runtime (podman or docker)
CONTAINER_RT=""
if command -v podman &>/dev/null; then
  CONTAINER_RT="podman"
elif command -v docker &>/dev/null; then
  CONTAINER_RT="docker"
else
  MISSING+=("podman or docker")
fi
if [ ${#MISSING[@]} -gt 0 ]; then
  err "Missing prerequisites: ${MISSING[*]}"
  exit 1
fi
ok "All prerequisites found (container runtime: ${CONTAINER_RT})"

# ── Step 2: Submodules ─────────────────────────────────────────────
step 2 "Initializing git submodules"
cd "${SIM2REAL_ROOT}"
git submodule update --init inference-sim llm-d-inference-scheduler tektonc-data-collection 2>/dev/null || true
ok "Submodules initialized"

# ── Step 3: Python venv ────────────────────────────────────────────
step 3 "Setting up Python environment"
cd "${SIM2REAL_ROOT}"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  info "Created .venv"
fi
source .venv/bin/activate
pip install -q -r requirements.txt 2>/dev/null || pip install -q PyYAML jinja2
ok "Python venv ready"

# ── Step 4: Namespace ──────────────────────────────────────────────
step 4 "Configuring namespace"
if [ -z "${NAMESPACE:-}" ]; then
  read -rp "Enter Kubernetes namespace: " NAMESPACE
fi
if [ -z "${NAMESPACE}" ]; then
  err "NAMESPACE is required"; exit 1
fi
export NAMESPACE

# Detect OpenShift
IS_OPENSHIFT=false
if command -v oc &>/dev/null && oc whoami &>/dev/null 2>&1; then
  IS_OPENSHIFT=true
  info "OpenShift detected"
fi

# Create namespace (idempotent)
if kubectl get ns "${NAMESPACE}" &>/dev/null; then
  ok "Namespace ${NAMESPACE} already exists"
else
  if [ "$IS_OPENSHIFT" = true ]; then
    oc new-project "${NAMESPACE}" 2>/dev/null || kubectl create ns "${NAMESPACE}"
  else
    kubectl create ns "${NAMESPACE}"
  fi
  ok "Created namespace ${NAMESPACE}"
fi

# ── Step 5: HuggingFace secret ─────────────────────────────────────
step 5 "Creating HuggingFace secret"
if [ -z "${HF_TOKEN:-}" ]; then
  read -rsp "Enter HuggingFace token (HF_TOKEN): " HF_TOKEN
  echo
fi
if [ -z "${HF_TOKEN}" ]; then
  err "HF_TOKEN is required"; exit 1
fi
kubectl create secret generic hf-secret \
  --namespace "${NAMESPACE}" \
  --from-literal="HF_TOKEN=${HF_TOKEN}" \
  --dry-run=client -o yaml | kubectl apply -f -
ok "hf-secret created/updated"

# ── Step 6: RBAC roles ─────────────────────────────────────────────
step 6 "Applying RBAC roles"
cd "${TEKTONC_DIR}"
envsubst '$NAMESPACE' < tekton/roles.yaml | kubectl apply -f -
if [ "$IS_OPENSHIFT" = true ]; then
  warn "OpenShift: adding SCC policies"
  oc adm policy add-scc-to-user anyuid -z default -n "${NAMESPACE}" 2>/dev/null || true
  oc adm policy add-scc-to-user anyuid -z helm-installer -n "${NAMESPACE}" 2>/dev/null || true
fi
ok "RBAC roles applied"

# ── Step 7: PVCs ───────────────────────────────────────────────────
step 7 "Creating PVCs"

# Determine storageClassName
STORAGE_CLASS=""
if [ "$IS_OPENSHIFT" = true ]; then
  STORAGE_CLASS="ocs-storagecluster-cephfs"
  info "OpenShift: using storageClassName=${STORAGE_CLASS}"
fi
SC_SNIPPET=""
if [ -n "${STORAGE_CLASS}" ]; then
  SC_SNIPPET="  storageClassName: ${STORAGE_CLASS}"
fi

create_pvc() {
  local name=$1 size=$2
  if kubectl get pvc "${name}" -n "${NAMESPACE}" &>/dev/null; then
    ok "PVC ${name} already exists"
    return
  fi
  cat <<YAML | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${name}
  namespace: ${NAMESPACE}
spec:
${SC_SNIPPET}
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: ${size}
YAML
  ok "Created PVC ${name} (${size})"
}

create_pvc model-pvc 300Gi
create_pvc data-pvc 20Gi
create_pvc source-pvc 20Gi

# ── Step 8: Tekton operator ────────────────────────────────────────
step 8 "Verifying Tekton operator"
if kubectl get pods -n tekton-pipelines 2>/dev/null | grep -q Running; then
  ok "Tekton operator is running"
else
  warn "Tekton operator not detected in tekton-pipelines namespace"
  warn "Install Tekton Pipelines: https://tekton.dev/docs/installation/pipelines/"
fi

# ── Step 9: Deploy step/task definitions ───────────────────────────
step 9 "Deploying Tekton steps and tasks"
cd "${TEKTONC_DIR}"
for f in tekton/steps/*.yaml; do
  [ -f "$f" ] && kubectl apply -f "$f" -n "${NAMESPACE}"
done
for f in tekton/tasks/*.yaml; do
  [ -f "$f" ] && kubectl apply -f "$f" -n "${NAMESPACE}"
done
ok "Tekton steps and tasks deployed"

# ── Step 10: Registry secret ───────────────────────────────────────
step 10 "Configuring container registry"
if [ -z "${REGISTRY:-}" ]; then
  read -rp "Enter container registry (e.g., ghcr.io/username): " REGISTRY
fi
if [ -n "${REGISTRY}" ]; then
  info "Logging in to ${REGISTRY}..."
  ${CONTAINER_RT} login "${REGISTRY%%/*}" || warn "Registry login failed — you can retry later"

  # Create registry-secret for in-cluster Kaniko builds
  CONFIG_PATH="${HOME}/.docker/config.json"
  [ -f "${HOME}/.config/containers/auth.json" ] && CONFIG_PATH="${HOME}/.config/containers/auth.json"
  if [ -f "${CONFIG_PATH}" ]; then
    kubectl create secret docker-registry registry-secret \
      --namespace "${NAMESPACE}" \
      --from-file=.dockerconfigjson="${CONFIG_PATH}" \
      --dry-run=client -o yaml | kubectl apply -f -
    ok "registry-secret created/updated for Kaniko builds"
  else
    warn "No container auth config found at ${CONFIG_PATH}"
    warn "Create registry-secret manually for in-cluster builds"
  fi
else
  warn "No registry specified — skipping. Set later in config/env_defaults.yaml"
fi

# ── Done ───────────────────────────────────────────────────────────
echo
echo -e "${GREEN}━━━ Setup complete ━━━${NC}"
echo
echo "Next steps:"
echo "  1. Review config/env_defaults.yaml (gateway, vLLM image, registry, fast_iteration)"
echo "  2. Run /sim2real-prepare to start the transfer pipeline"
echo
```

- [ ] **Step 2: Make executable**

```bash
chmod +x .claude/skills/sim2real-setup/scripts/setup.sh
```

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/sim2real-setup/scripts/setup.sh
git commit -m "feat: add setup.sh for /sim2real-setup skill"
```

---

### Task 3: Multi-Model Review Scripts

**Files:**
- Create: `.claude/skills/sim2real-prepare/scripts/review_translation.py`
- Create: `.claude/skills/sim2real-prepare/scripts/build_review_request.py`

- [ ] **Step 1: Create build_review_request.py**

This builds the JSON payload for each reviewer model.

```python
#!/usr/bin/env python3
"""Build a structured review request for translation consistency checking."""

import json
import sys


def build_request(model: str, scorer_code: str, algorithm_summary: str,
                  signal_coverage: str, evolve_block: str, round_num: int) -> str:
    """Return JSON string for a /v1/chat/completions request."""
    system_prompt = (
        "You are a technical reviewer verifying that a generated Go scorer plugin "
        "faithfully implements an evolved routing algorithm. You will receive:\n"
        "1. The generated scorer Go code\n"
        "2. The algorithm summary (extracted metadata)\n"
        "3. The signal coverage mapping (sim signals → production equivalents)\n"
        "4. The original EVOLVE-BLOCK source (the ground truth)\n\n"
        "For EACH signal in the signal coverage, verify:\n"
        "- The scorer reads the correct production field\n"
        "- Normalization matches the algorithm summary\n"
        "- The weight/coefficient matches the EVOLVE-BLOCK\n"
        "- The scoring logic (comparison, threshold, combination) is faithful\n\n"
        "Respond with ONLY valid JSON matching this schema:\n"
        '{"verdict": "consistent"|"inconsistent", '
        '"per_signal": [{"signal": "name", "consistent": true|false, '
        '"rationale": "..."}], '
        '"issues": ["..."], "suggestions": ["..."]}'
    )

    user_content = (
        f"## Round {round_num}\n\n"
        f"## Generated Scorer Code\n```go\n{scorer_code}\n```\n\n"
        f"## Algorithm Summary\n```json\n{algorithm_summary}\n```\n\n"
        f"## Signal Coverage\n```json\n{signal_coverage}\n```\n\n"
        f"## EVOLVE-BLOCK Source\n```go\n{evolve_block}\n```\n"
    )

    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": 4000,
    }
    return json.dumps(request)


def main():
    if len(sys.argv) < 6:
        print("Usage: build_review_request.py MODEL SCORER_FILE ALGO_FILE SIGNAL_FILE EVOLVE_FILE [ROUND]",
              file=sys.stderr)
        sys.exit(1)

    model = sys.argv[1]
    with open(sys.argv[2]) as f:
        scorer_code = f.read()
    with open(sys.argv[3]) as f:
        algorithm_summary = f.read()
    with open(sys.argv[4]) as f:
        signal_coverage = f.read()
    with open(sys.argv[5]) as f:
        evolve_block = f.read()
    round_num = int(sys.argv[6]) if len(sys.argv) > 6 else 1

    print(build_request(model, scorer_code, algorithm_summary,
                        signal_coverage, evolve_block, round_num))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Create review_translation.py**

This orchestrates N rounds of multi-model review.

```python
#!/usr/bin/env python3
"""Multi-model translation review orchestrator.

Sends generated scorer code to multiple LLM models for independent
consistency review against the original EVOLVE-BLOCK algorithm.

Usage:
    review_translation.py --scorer FILE --algorithm FILE --signals FILE \
        --evolve-block FILE --rounds N --out FILE

Environment:
    OPENAI_API_KEY / OPENAI_BASE_URL  (primary)
    ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL (fallback)
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

MODELS = [
    "Azure/gpt-4o",
    "GCP/gemini-2.5-flash",
    "aws/claude-opus-4-6",
]

SCRIPT_DIR = Path(__file__).parent
BUILD_REQUEST = SCRIPT_DIR / "build_review_request.py"

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
NC = "\033[0m"


def resolve_api_credentials():
    """Resolve API key and base URL from environment."""
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL", os.environ.get("OPENAI_URL", "https://api.openai.com"))

    if not api_key:
        api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

    if not api_key:
        print(f"{RED}[ERROR]{NC} No API key found. Set OPENAI_API_KEY or ANTHROPIC_AUTH_TOKEN.",
              file=sys.stderr)
        sys.exit(1)

    return api_key, base_url


def call_model(model, scorer_file, algo_file, signal_file, evolve_file,
               round_num, api_key, base_url):
    """Call a single model and return parsed review JSON."""
    # Build request
    result = subprocess.run(
        ["python3", str(BUILD_REQUEST), model, scorer_file, algo_file,
         signal_file, evolve_file, str(round_num)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"{RED}[ERROR]{NC} Failed to build request for {model}: {result.stderr}",
              file=sys.stderr)
        return None

    request_json = result.stdout.strip()

    # Call API
    endpoint = f"{base_url}/v1/chat/completions"
    try:
        api_result = subprocess.run(
            ["curl", "-s", "-w", "\n%{http_code}", "-X", "POST", endpoint,
             "--connect-timeout", "30", "--max-time", "300",
             "-H", "Content-Type: application/json",
             "-H", f"Authorization: Bearer {api_key}",
             "-d", request_json],
            capture_output=True, text=True, timeout=320
        )
    except subprocess.TimeoutExpired:
        print(f"{RED}[ERROR]{NC} Timeout calling {model}", file=sys.stderr)
        return None

    output = api_result.stdout.strip()
    lines = output.split("\n")
    http_code = lines[-1] if lines else "0"
    response_body = "\n".join(lines[:-1])

    if http_code != "200":
        print(f"{RED}[ERROR]{NC} {model} returned HTTP {http_code}: {response_body[:200]}",
              file=sys.stderr)
        return None

    # Parse response
    try:
        response = json.loads(response_body)
        content = response["choices"][0]["message"]["content"]
        # Strip markdown code fences if present
        if content.startswith("```"):
            content = "\n".join(content.split("\n")[1:])
        if content.endswith("```"):
            content = "\n".join(content.split("\n")[:-1])
        review = json.loads(content)
        review["model"] = model
        review["round"] = round_num
        return review
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"{RED}[ERROR]{NC} Failed to parse {model} response: {e}",
              file=sys.stderr)
        return None


def run_review_round(round_num, args, api_key, base_url):
    """Run one round of reviews across all models. Return list of reviews."""
    print(f"\n{BLUE}━━━ Review Round {round_num}/{args.rounds} ━━━{NC}")
    reviews = []
    for model in MODELS:
        print(f"  Reviewing with {model}...", end=" ", flush=True)
        review = call_model(model, args.scorer, args.algorithm, args.signals,
                            args.evolve_block, round_num, api_key, base_url)
        if review is None:
            print(f"{RED}FAILED{NC}")
            reviews.append({
                "model": model, "round": round_num,
                "verdict": "inconsistent",
                "per_signal": [],
                "issues": ["API call failed — treating as inconsistent"],
                "suggestions": [],
            })
        elif review.get("verdict") == "consistent":
            print(f"{GREEN}CONSISTENT{NC}")
            reviews.append(review)
        else:
            print(f"{YELLOW}INCONSISTENT{NC}")
            for issue in review.get("issues", []):
                print(f"    - {issue}")
            reviews.append(review)
    return reviews


def check_consensus(reviews):
    """Return True if all reviews are consistent."""
    return all(r.get("verdict") == "consistent" for r in reviews)


def main():
    parser = argparse.ArgumentParser(description="Multi-model translation review")
    parser.add_argument("--scorer", required=True, help="Path to generated scorer .go file")
    parser.add_argument("--algorithm", required=True, help="Path to algorithm_summary.json")
    parser.add_argument("--signals", required=True, help="Path to signal_coverage.json")
    parser.add_argument("--evolve-block", required=True, help="Path to EVOLVE-BLOCK source")
    parser.add_argument("--rounds", type=int, default=2, help="Number of review rounds (default: 2)")
    parser.add_argument("--out", required=True, help="Output path for translation_reviews.json")
    args = parser.parse_args()

    # Validate inputs exist
    for path_attr in ("scorer", "algorithm", "signals", "evolve_block"):
        path = getattr(args, path_attr)
        if not os.path.isfile(path):
            print(f"{RED}[ERROR]{NC} File not found: {path}", file=sys.stderr)
            sys.exit(1)

    api_key, base_url = resolve_api_credentials()

    all_rounds = []
    final_verdict = "inconsistent"

    for round_num in range(1, args.rounds + 1):
        reviews = run_review_round(round_num, args, api_key, base_url)
        consensus = check_consensus(reviews)

        round_result = {
            "round": round_num,
            "reviews": reviews,
            "consensus": consensus,
            "fixes_applied": [],
        }
        all_rounds.append(round_result)

        if consensus:
            print(f"\n{GREEN}━━━ Consensus reached in round {round_num} ━━━{NC}")
            final_verdict = "consistent"
            break

        if round_num < args.rounds:
            # Collect issues for the Claude session to fix
            all_issues = []
            for r in reviews:
                if r.get("verdict") == "inconsistent":
                    all_issues.extend(r.get("issues", []))
            print(f"\n{YELLOW}Issues to fix before round {round_num + 1}:{NC}")
            for issue in all_issues:
                print(f"  - {issue}")
            print(f"\n{YELLOW}FIX_NEEDED: Apply fixes to the scorer, then re-run this script.{NC}")
            # Write partial results so the skill can read issues
            partial = {
                "rounds": all_rounds,
                "final_verdict": "pending",
                "total_rounds": round_num,
                "models_used": MODELS,
                "issues_to_fix": all_issues,
            }
            with open(args.out, "w") as f:
                json.dump(partial, f, indent=2)
            sys.exit(2)  # Exit 2 = fixes needed
        else:
            print(f"\n{RED}━━━ Final round {round_num}: no consensus — HALT ━━━{NC}")
            final_verdict = "inconsistent"

    # Write final results
    output = {
        "rounds": all_rounds,
        "final_verdict": final_verdict,
        "total_rounds": len(all_rounds),
        "models_used": MODELS,
    }
    with open(args.out, "w") as f:
        json.dump(output, f, indent=2)

    if final_verdict == "consistent":
        print(f"\n{GREEN}Review passed. Results written to {args.out}{NC}")
        sys.exit(0)
    else:
        print(f"\n{RED}Review FAILED. Results written to {args.out}{NC}")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/sim2real-prepare/scripts/build_review_request.py \
       .claude/skills/sim2real-prepare/scripts/review_translation.py
git commit -m "feat: add multi-model translation review scripts"
```

---

### Task 4: `/sim2real-prepare` Skill — SKILL.md

**Files:**
- Create: `.claude/skills/sim2real-prepare/SKILL.md`

- [ ] **Step 1: Create the SKILL.md**

```markdown
---
name: sim2real-prepare
description: |
  Phase 1 of the sim2real transfer pipeline. Extracts algorithm metadata,
  translates signals, generates scorer plugin, and runs multi-model AI review.
  Use after /sim2real-setup. Produces all artifacts needed for /sim2real-deploy.
argument-hint: "[--reviews N]"
user-invocable: true
allowed-tools:
  - Bash(**/review_translation.py *)
  - Bash(**/build_review_request.py *)
  - Bash(python3 *)
  - Bash(cd * && *)
  - Bash(cat *)
  - Bash(test *)
  - Bash(git *)
  - Glob
  - Read
  - Edit
  - Write
  - Grep
---

# sim2real-prepare

Phase 1 of the sim2real transfer pipeline: Extract, Translate, Generate, Review.

## Arguments

- `--reviews N` — Number of multi-model review rounds (default: 2)

Parse the argument: if the user invokes `/sim2real-prepare --reviews 3`, set
`REVIEW_ROUNDS=3`. Otherwise `REVIEW_ROUNDS=2`.

## Prerequisites

Before starting, verify all required artifacts and submodules. **HALT if any
check fails.**

```bash
# Required input artifacts
test -f blis_router/best/best_program.go || { echo "HALT: missing blis_router/best/best_program.go"; exit 1; }
test -f blis_router/best/best_program_info.json || { echo "HALT: missing best_program_info.json"; exit 1; }
test -f docs/transfer/blis_to_llmd_mapping.md || { echo "HALT: missing mapping artifact"; exit 1; }
test -f docs/transfer/scorer_template.go.md || { echo "HALT: missing scorer template"; exit 1; }

# Submodules initialized
test -d inference-sim/sim || { echo "HALT: inference-sim submodule not initialized"; exit 1; }
test -d llm-d-inference-scheduler/pkg || { echo "HALT: llm-d-inference-scheduler not initialized"; exit 1; }
```

Record the pipeline commit:
```bash
mkdir -p workspace
git rev-parse HEAD > workspace/pipeline_commit.txt
```

## Stage 1: Extract

Follow the logic from `prompts/extract.md`. Key steps:

1. Delete stale artifact: `rm -f workspace/algorithm_summary.json`
2. Run extraction:
   ```bash
   .venv/bin/python tools/transfer_cli.py extract --strict blis_router/best/
   ```
3. Validate output:
   ```bash
   test -f workspace/algorithm_summary.json || { echo "HALT: extraction failed"; exit 1; }
   .venv/bin/python tools/transfer_cli.py validate-schema workspace/algorithm_summary.json
   .venv/bin/python -c "import json,sys; d=json.load(open('workspace/algorithm_summary.json')); sys.exit(0 if d.get('scope_validation_passed') is True else 1)"
   ```

**HALT on any failure.** Do not proceed to Translate.

### Artifacts produced
- `workspace/algorithm_summary.json`

## Stage 2: Translate

Follow the logic from `prompts/translate.md`. Key steps:

1. Delete stale artifact: `rm -f workspace/signal_coverage.json`
2. Check submodule staleness — compare mapping artifact commit hash with
   `llm-d-inference-scheduler` submodule HEAD (7-char prefix match).
3. For each signal in `algorithm_summary.json`:
   - Look up in `docs/transfer/blis_to_llmd_mapping.md`
   - Record: prod_name, prod_access_path, fidelity, staleness_window_ms
   - Propagate normalization and fidelity_provisional flags
4. Handle unknown-type signals — move to `unmapped_signals[]` and HALT if any.
5. F-10 double-counting detection (skip if `composite_signals` is empty).
6. Write `signal_coverage.json` and validate:
   ```bash
   .venv/bin/python tools/transfer_cli.py validate-schema workspace/signal_coverage.json
   .venv/bin/python -c "import json,sys; d=json.load(open('workspace/signal_coverage.json')); sys.exit(0 if d.get('coverage_complete') is True and len(d.get('unmapped_signals',[])) == 0 else 1)"
   ```

**HALT on any failure.** Do not proceed to Generate.

### Artifacts produced
- `workspace/signal_coverage.json`

## Stage 3: Generate

Follow the logic from `prompts/generate.md`. Key steps:

1. Delete stale outputs: `rm -f workspace/stage3_output.json workspace/tekton/algorithm_values.yaml workspace/tekton/values.yaml`
2. Re-verify EVOLVE-BLOCK content hash against `algorithm_summary.json`.
3. Generate scorer code:
   - Parse EVOLVE-BLOCK source
   - Map signals using `signal_coverage.json`
   - Apply normalizations from `algorithm_summary.json`
   - Generate scorer `.go`, test `.go`, and registration in `llm-d-inference-scheduler/pkg/plugins/scorer/`
   - Use template from `docs/transfer/scorer_template.go.md`
4. Validate generated code — no PLACEHOLDER markers, correct imports, type assertions.
5. Write `workspace/stage3_output.json`.
6. Generate Tekton artifacts:
   - Resolve inference-sim image tag
   - Read `config/env_defaults.yaml`
   - Generate `workspace/tekton/algorithm_values.yaml`
   - Merge: `.venv/bin/python tools/transfer_cli.py merge-values --env config/env_defaults.yaml --algorithm workspace/tekton/algorithm_values.yaml --out workspace/tekton/values.yaml`
7. Validate all outputs:
   ```bash
   .venv/bin/python tools/transfer_cli.py validate-schema workspace/stage3_output.json
   test -f workspace/tekton/values.yaml || { echo "HALT: merge-values failed"; exit 1; }
   ```

**HALT on any failure.** Do not proceed to Review.

### Artifacts produced
- `workspace/stage3_output.json`
- `llm-d-inference-scheduler/pkg/plugins/scorer/<name>.go`
- `llm-d-inference-scheduler/pkg/plugins/scorer/<name>_test.go`
- `workspace/tekton/algorithm_values.yaml`
- `workspace/tekton/values.yaml`

## Stage 4: Multi-Model AI Review

Locate the review script:
```
Glob: **/skills/sim2real-prepare/scripts/review_translation.py
```
Store the result as `[REVIEW_SCRIPT]`.

Extract the scorer file path and EVOLVE-BLOCK path:
```bash
SCORER_FILE=$(python3 -c "import json; print(json.load(open('workspace/stage3_output.json'))['scorer_file'])")
EVOLVE_BLOCK=$(python3 -c "import json; print(json.load(open('workspace/algorithm_summary.json'))['evolve_block_file'])")
```

### Review loop

For each round from 1 to `REVIEW_ROUNDS`:

1. Run the review script:
   ```bash
   python3 [REVIEW_SCRIPT] \
     --scorer "$SCORER_FILE" \
     --algorithm workspace/algorithm_summary.json \
     --signals workspace/signal_coverage.json \
     --evolve-block "$EVOLVE_BLOCK" \
     --rounds 1 \
     --out workspace/translation_reviews.json
   ```

2. Check exit code:
   - **Exit 0** — consensus reached. Proceed.
   - **Exit 2** — fixes needed. Read `workspace/translation_reviews.json`,
     extract `issues_to_fix`, apply fixes to the scorer file, then re-run
     the review for the next round.
   - **Exit 1** — final round, no consensus. **HALT.**

3. After applying fixes, re-validate the scorer builds:
   ```bash
   cd llm-d-inference-scheduler && GOWORK=off go build ./... && cd ..
   ```
   If build fails after fix, **HALT**.

**IMPORTANT:** After all review rounds, if `final_verdict` is `inconsistent`,
**HALT the pipeline.** Do not continue to `/sim2real-deploy`.

### Artifacts produced
- `workspace/translation_reviews.json`

## Completion

Print artifact summary:

```
━━━ /sim2real-prepare complete ━━━

Artifacts produced:
  workspace/algorithm_summary.json    ✓ Extract
  workspace/signal_coverage.json      ✓ Translate
  workspace/stage3_output.json        ✓ Generate
  workspace/tekton/values.yaml        ✓ Generate (merged)
  workspace/translation_reviews.json  ✓ AI Review (N rounds, consensus)
  <scorer_file>                       ✓ Generate

Next: run /sim2real-deploy
```
```

- [ ] **Step 2: Commit**

```bash
git add .claude/skills/sim2real-prepare/SKILL.md
git commit -m "feat: add /sim2real-prepare skill definition"
```

---

### Task 5: `/sim2real-deploy` Skill — SKILL.md

**Files:**
- Create: `.claude/skills/sim2real-deploy/SKILL.md`

- [ ] **Step 1: Create the SKILL.md**

```markdown
---
name: sim2real-deploy
description: |
  Phase 2 of the sim2real transfer pipeline. Builds, tests, runs equivalence
  gate, builds EPP image (in-cluster via Kaniko), runs cluster benchmarks,
  and creates PRs. Use after /sim2real-prepare completes successfully.
user-invocable: true
allowed-tools:
  - Bash(kubectl *)
  - Bash(oc *)
  - Bash(tkn *)
  - Bash(python3 *)
  - Bash(cd * && *)
  - Bash(cat *)
  - Bash(test *)
  - Bash(git *)
  - Bash(gh *)
  - Glob
  - Read
  - Edit
  - Write
  - Grep
---

# sim2real-deploy

Phase 2 of the sim2real transfer pipeline: Test, Equivalence Gate, Build EPP,
Cluster Benchmarks, PR.

## Prerequisites

Before starting, verify all Phase 1 artifacts. **HALT if any check fails.**

```bash
# Phase 1 artifacts
for f in workspace/algorithm_summary.json workspace/signal_coverage.json \
         workspace/stage3_output.json workspace/tekton/values.yaml \
         workspace/translation_reviews.json; do
  test -f "$f" || { echo "HALT: missing $f — run /sim2real-prepare first"; exit 1; }
done

# Schema validation
for f in workspace/algorithm_summary.json workspace/signal_coverage.json \
         workspace/stage3_output.json; do
  .venv/bin/python tools/transfer_cli.py validate-schema "$f" || { echo "HALT: $f schema invalid"; exit 1; }
done

# AI review must have passed
python3 -c "
import json, sys
d = json.load(open('workspace/translation_reviews.json'))
if d.get('final_verdict') != 'consistent':
    print('HALT: AI review verdict is not consistent'); sys.exit(1)
" || exit 1

# Scorer file exists and builds
SCORER_FILE=$(python3 -c "import json; print(json.load(open('workspace/stage3_output.json'))['scorer_file'])")
test -f "$SCORER_FILE" || { echo "HALT: scorer file missing"; exit 1; }
(cd llm-d-inference-scheduler && GOWORK=off go build ./...) || { echo "HALT: scorer build failed"; exit 1; }

# Registry configuration
python3 -c "
import yaml, sys
d = yaml.safe_load(open('config/env_defaults.yaml'))
hub = d.get('stack',{}).get('gaie',{}).get('epp_image',{}).get('build',{}).get('hub','')
if not hub or 'REPLACE_ME' in hub:
    print('HALT: set epp_image.build.hub in config/env_defaults.yaml'); sys.exit(1)
" || exit 1

# Pipeline commit drift check
if [ -f workspace/pipeline_commit.txt ]; then
  EXPECTED=$(cat workspace/pipeline_commit.txt)
  ACTUAL=$(git rev-parse HEAD)
  if [ "$EXPECTED" != "$ACTUAL" ]; then
    echo "WARNING: repo HEAD has drifted since /sim2real-prepare"
  fi
fi
```

## Stage 1: Build & Test

Follow the logic from `prompts/test.md`. This stage has a retry loop.

**Retry state:**
- `retries_compilation=0` (limit: 4)
- `retries_test_failure=0` (limit: 4)
- `retries_total=0` (limit: 6)
- `error_signatures=[]` (oscillation limit: 3)

**Loop:**

1. Build:
   ```bash
   cd llm-d-inference-scheduler && GOWORK=off go build ./...
   ```
2. Vet:
   ```bash
   cd llm-d-inference-scheduler && GOWORK=off go vet ./...
   ```
3. Test:
   ```bash
   cd llm-d-inference-scheduler && GOWORK=off go test -timeout 10m ./pkg/plugins/scorer/... -v
   ```
4. On failure, classify error via `test-status`, apply fix, increment counters.
   **HALT** on: infrastructure errors, identical consecutive errors, oscillation
   (3 occurrences of same signature), per-class limit (4), or total limit (6).

**On success:** All build/vet/test pass. Proceed.

## Stage 2: Equivalence Gate

Follow the logic from `prompts/equivalence-gate.md`.

Delete stale artifacts: `rm -f workspace/equivalence_results.json`

**Retry state:** Same structure as Build & Test (per-suite and total limits).

Run suites:

1. **Suite A** — Rank correlation (Kendall-tau ≥ 0.8):
   ```bash
   cd llm-d-inference-scheduler && GOWORK=off go test -tags suitea -json -v -timeout 10m ./pkg/plugins/scorer/...
   ```
2. **Suite B** — Staleness stability (informational, no halt on failure):
   ```bash
   cd llm-d-inference-scheduler && GOWORK=off go test -tags suiteb -json -v -timeout 10m ./pkg/plugins/scorer/...
   ```
3. **Suite C** — Concurrent safety:
   ```bash
   cd llm-d-inference-scheduler && GOWORK=off go test -tags suitec -race -json -v -timeout 10m ./pkg/plugins/scorer/...
   ```

**HALT** if Suite A or Suite C fails after retries.

Write `workspace/equivalence_results.json` with suite metrics.

Validate:
```bash
.venv/bin/python tools/transfer_cli.py validate-schema workspace/equivalence_results.json
python3 -c "
import json, sys
d = json.load(open('workspace/equivalence_results.json'))
if not d.get('suite_a', {}).get('passed'):
    print('HALT: Suite A did not pass'); sys.exit(1)
if not d.get('suite_c', {}).get('passed'):
    print('HALT: Suite C did not pass'); sys.exit(1)
print('Equivalence gate: PASS (tau=' + str(d['suite_a']['kendall_tau']) + ')')
"
```

### Artifacts produced
- `workspace/equivalence_results.json`

## Stage 3: Build EPP Image (In-Cluster via Kaniko)

Follow the logic from `prompts/build-push.md`, but use Kaniko instead of local build.

1. Copy source to cluster:
   ```bash
   # Create a temp pod to populate source-pvc
   kubectl run source-copy --rm -i --restart=Never \
     --namespace ${NAMESPACE} \
     --image=busybox \
     --overrides='{"spec":{"volumes":[{"name":"src","persistentVolumeClaim":{"claimName":"source-pvc"}}],"containers":[{"name":"source-copy","image":"busybox","command":["sh","-c","sleep 300"],"volumeMounts":[{"name":"src","mountPath":"/workspace/source"}]}]}}' &
   sleep 5
   kubectl cp llm-d-inference-scheduler/ ${NAMESPACE}/source-copy:/workspace/source/
   kubectl delete pod source-copy --namespace ${NAMESPACE} --force
   ```

2. Resolve image tag:
   ```bash
   GIT_SHA=$(cd llm-d-inference-scheduler && git rev-parse --short HEAD)
   TAG="sim2real-${GIT_SHA}"
   REGISTRY_HUB=$(python3 -c "import yaml; d=yaml.safe_load(open('config/env_defaults.yaml')); print(d['stack']['gaie']['epp_image']['build']['hub'])")
   IMAGE_NAME=$(python3 -c "import yaml; d=yaml.safe_load(open('config/env_defaults.yaml')); print(d['stack']['gaie']['epp_image']['build']['name'])")
   FULL_IMAGE="${REGISTRY_HUB}/${IMAGE_NAME}:${TAG}"
   ```

3. Submit Kaniko build pod:
   ```yaml
   apiVersion: v1
   kind: Pod
   metadata:
     name: epp-build-${GIT_SHA}
     namespace: ${NAMESPACE}
   spec:
     restartPolicy: Never
     containers:
       - name: kaniko
         image: gcr.io/kaniko-project/executor:latest
         args:
           - --dockerfile=Dockerfile
           - --context=dir:///workspace/source
           - --destination=${FULL_IMAGE}
           - --custom-platform=linux/amd64
         volumeMounts:
           - name: source
             mountPath: /workspace/source
           - name: registry-creds
             mountPath: /kaniko/.docker
     volumes:
       - name: source
         persistentVolumeClaim:
           claimName: source-pvc
       - name: registry-creds
         secret:
           secretName: registry-secret
           items:
             - key: .dockerconfigjson
               path: config.json
   ```

4. Wait for completion:
   ```bash
   kubectl wait --for=condition=Succeeded pod/epp-build-${GIT_SHA} \
     --namespace ${NAMESPACE} --timeout=1800s
   ```
   On failure: `kubectl logs epp-build-${GIT_SHA} -n ${NAMESPACE}` and **HALT**.

5. Inject image reference into `workspace/tekton/algorithm_values.yaml`:
   Update `stack.gaie.treatment.helmValues.inferenceExtension.image.hub` and `.tag`.

6. Re-merge values:
   ```bash
   .venv/bin/python tools/transfer_cli.py merge-values \
     --env config/env_defaults.yaml \
     --algorithm workspace/tekton/algorithm_values.yaml \
     --out workspace/tekton/values.yaml
   ```

7. Recompile pipeline YAMLs using tektonc.

> **OpenShift:** Can use `oc new-build --binary --strategy=docker` instead
> of Kaniko. Create a BuildConfig and start with `oc start-build --from-dir`.

### Artifacts produced
- EPP container image in registry
- Updated `workspace/tekton/algorithm_values.yaml`
- Updated `workspace/tekton/values.yaml`

## Stage 4: Cluster Benchmarks

Follow the logic from `prompts/validate.md`.

### Fast-iteration mode

Check `config/env_defaults.yaml` for `pipeline.fast_iteration: true`. If set:
- Skip noise characterization and mechanism check
- Write partial `validation_results.json` from equivalence results
- Run baseline and treatment pipelines only
- Generate comparison table
- Skip to Completion (no PR creation)

### Full validation flow

1. **Noise characterization** — run `observe.noise_runs` sequential pipeline
   submissions with fresh infrastructure per run. Extract results.
2. **Baseline pipeline** — submit and wait (4h timeout). Extract results.
3. **Treatment pipeline** — submit and wait (4h timeout). Extract results.
4. **Mechanism check:**
   ```bash
   .venv/bin/python tools/transfer_cli.py benchmark \
     --noise workspace/noise_results.json \
     --baseline workspace/baseline_results.json \
     --treatment workspace/treatment_results.json \
     --signal-coverage workspace/signal_coverage.json \
     --workloads-dir blis_router/workloads/ \
     --out workspace/benchmark_output.json
   ```
   - Exit 0 = PASS or INCONCLUSIVE (check `mechanism_check_verdict`)
   - Exit 1 = FAIL → **HALT**
   - Exit 2 = ERROR → **HALT**
5. **Merge results** into `workspace/validation_results.json`.
6. **Comparison table:**
   ```bash
   .venv/bin/python tools/transfer_cli.py compare \
     --baseline workspace/baseline_results.json \
     --treatment workspace/treatment_results.json \
     --out workspace/comparison_table.txt
   ```

### Decision point: INCONCLUSIVE verdict

If `mechanism_check_verdict` is INCONCLUSIVE, **pause and ask the user:**

> Mechanism check verdict is INCONCLUSIVE. Options:
> 1. Re-run during lower-variance window
> 2. Inspect per-workload improvements and retry targeted workloads
> 3. Accept as soft-pass (requires operator_notes in validation_results.json)

If the user chooses option 3, they must provide `operator_notes` text.
Write it into `workspace/validation_results.json` and set
`overall_verdict: "INCONCLUSIVE"`.

### Artifacts produced
- `workspace/noise_results.json` (if not fast_iteration)
- `workspace/baseline_results.json`
- `workspace/treatment_results.json`
- `workspace/benchmark_output.json` (if not fast_iteration)
- `workspace/validation_results.json`
- `workspace/comparison_table.txt`
- `workspace/transfer_evidence.md`

## Stage 5: PR

Follow the logic from `prompts/pr.md`.

### Fast-iteration mode
If `pipeline.fast_iteration: true`, skip PR creation entirely. Print:
```
Fast-iteration mode: PR creation skipped.
Review comparison_table.txt and re-run with fast_iteration=false when ready.
```

### Full mode

1. Check verdict — **HALT** on FAIL. Verify `operator_notes` present if INCONCLUSIVE.
2. Verify `gh auth status`.
3. Push branch:
   ```bash
   ALG_NAME=$(python3 -c "import json; print(json.load(open('workspace/algorithm_summary.json'))['algorithm_name'])")
   cd llm-d-inference-scheduler
   git checkout -b "transfer/${ALG_NAME}"
   git add -A
   git commit -m "feat: add ${ALG_NAME} scorer plugin (sim2real transfer)"
   git push -u origin "transfer/${ALG_NAME}"
   ```
4. Append calibration log entry.
5. Create PR via `gh pr create`.

### Artifacts produced
- `docs/transfer/calibration_log.md` (appended)
- PR in `llm-d-inference-scheduler` repo

## Completion

Print artifact summary and final status.

```
━━━ /sim2real-deploy complete ━━━

Verdict: <PASS|INCONCLUSIVE>
EPP Image: <full image reference>

Artifacts:
  workspace/equivalence_results.json  ✓ Equivalence Gate
  workspace/validation_results.json   ✓ Cluster Benchmarks
  workspace/comparison_table.txt      ✓ Comparison

PR: <URL or "skipped (fast_iteration)">

Pipeline commit check: <OK or WARNING: drifted>
```
```

- [ ] **Step 2: Commit**

```bash
git add .claude/skills/sim2real-deploy/SKILL.md
git commit -m "feat: add /sim2real-deploy skill definition"
```

---

### Task 6: Final Verification

- [ ] **Step 1: Verify file structure**

```bash
find .claude/skills/sim2real-* -type f | sort
```

Expected output:
```
.claude/skills/sim2real-deploy/SKILL.md
.claude/skills/sim2real-prepare/scripts/build_review_request.py
.claude/skills/sim2real-prepare/scripts/review_translation.py
.claude/skills/sim2real-prepare/SKILL.md
.claude/skills/sim2real-setup/scripts/setup.sh
.claude/skills/sim2real-setup/SKILL.md
```

- [ ] **Step 2: Verify setup.sh is executable**

```bash
test -x .claude/skills/sim2real-setup/scripts/setup.sh && echo "OK" || echo "FAIL: not executable"
```

- [ ] **Step 3: Verify Python scripts parse**

```bash
python3 -c "import py_compile; py_compile.compile('.claude/skills/sim2real-prepare/scripts/review_translation.py', doraise=True)"
python3 -c "import py_compile; py_compile.compile('.claude/skills/sim2real-prepare/scripts/build_review_request.py', doraise=True)"
```

- [ ] **Step 4: Verify skills are discoverable**

Start a new Claude Code session and check that `/sim2real-setup`, `/sim2real-prepare`,
and `/sim2real-deploy` appear in the skill listing.

- [ ] **Step 5: Dry-run setup.sh with --help**

```bash
.claude/skills/sim2real-setup/scripts/setup.sh --help 2>&1 || true
```

Note: setup.sh doesn't have `--help` yet — this will exit with "Unknown arg" which
confirms arg parsing works.

- [ ] **Step 6: Verify no main repo files were modified**

```bash
git diff --name-only HEAD
git status --short | grep -v '^\?\? .claude/' | grep -v '^\?\? docs/superpowers/' || echo "OK: no main repo changes"
```

- [ ] **Step 7: Final commit (all tasks)**

```bash
git add .claude/skills/
git status
git commit -m "feat: simplified sim2real pipeline — 3 skills + setup

Replaces 9-stage prompt pipeline with:
- /sim2real-setup: one-time cluster setup
- /sim2real-prepare: extract + translate + generate + AI review
- /sim2real-deploy: test + equivalence + build (Kaniko) + benchmark + PR

Adds multi-model translation review via LiteLLM (GPT-4o, Gemini, Claude).
All files under .claude/ — main repo unchanged."
```
