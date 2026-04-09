# setup.py Redesign Spec

**Date:** 2026-04-09
**Parent design:** `docs/superpowers/brainstorms/2026-04-09-pipeline-redesign.md`
**Scope:** Rewrite `pipeline/setup.py` to match the pipeline redesign spec

---

## Problem

The current `setup.py` deviates from the pipeline redesign spec in three ways:

1. **Double prompting for registry info** — EPP/registry credentials collected early (step 1 interactive mode) then again at step 10 (`create_registry_secret`). Interactive users are asked twice.
2. **Test push failure is a warning** — `test_registry_push()` prints `warn()` on failure instead of treating it as an actionable error. Users don't realize their credentials are broken until the in-cluster EPP build fails hours later.
3. **Step structure doesn't match spec** — Current code has ~12 steps (prerequisites, submodules, venv, RBAC, Tekton verify, etc.) while the design spec defines a clean 7-step flow. Some steps (submodules, venv) are out of scope for a cluster bootstrap tool.

## Approach

Rewrite `setup.py` with an 8-step structure. Reuse existing helper functions (`run()`, `which()`, `prompt()`, `prompt_secret()`, `secret_exists()`, `check_cluster_reachable()`, `detect_openshift()`). New orchestration and data flow.

---

## Data Model

All config collected upfront in step 1, stored in a dataclass:

```python
@dataclass
class SetupConfig:
    namespace: str
    registry: str
    repo_name: str
    run_name: str
    hf_token: str
    registry_user: str
    registry_token: str
    storage_class: str
    is_openshift: bool
    no_cluster: bool
```

Container runtime (`podman` or `docker`) is auto-detected — not prompted. Only needed for test push.

After step 1, every subsequent step receives the populated `SetupConfig`. No more prompting after step 1.

---

## Step Flow

```
━━━ sim2real setup ━━━

━━━ [1/8] Configuration ━━━
  Collect all values (interactive or from flags)

━━━ [2/8] Namespace ━━━
  Check cluster reachable, create/verify namespace, set kubectl context

━━━ [3/8] RBAC ━━━
  Apply roles.yaml, OpenShift SCC policies if applicable

━━━ [4/8] Secrets ━━━
  Create/update hf-secret and registry-secret

━━━ [5/8] Registry Credential Test ━━━
  Optional test push with retry on failure

━━━ [6/8] PVCs ━━━
  Create model-pvc (300Gi), data-pvc (20Gi), source-pvc (20Gi)

━━━ [7/8] Tekton ━━━
  Verify Tekton operator, deploy steps and tasks

━━━ [8/8] Config ━━━
  Write setup_config.json, run_metadata.json, update env_defaults.yaml
```

### Step 1: Configuration

Collects all values in one pass. Three modes:

**Fresh install (no `setup_config.json`):** Prompt for each value. CLI flags skip the corresponding prompt.

**Reuse (existing `setup_config.json`):** Show current values as defaults. Enter reuses, typing overrides. Secrets are masked (`[hf_***]`).

**Fully scripted (all flags provided):** No prompts at all.

Values collected:
- Namespace (flag: `--namespace`, env: `NAMESPACE`)
- Registry (flag: `--registry`)
- Repo name (flag: `--repo-name`, default: `llm-d-inference-scheduler`)
- Run name (flag: `--run`, default: `sim2real-YYYY-MM-DD`)
- HuggingFace token (flag: `--hf-token`, env: `HF_TOKEN`)
- Registry username (flag: `--registry-user`, env: `QUAY_ROBOT_USERNAME`)
- Registry token (flag: `--registry-token`, env: `QUAY_ROBOT_TOKEN`)
- Storage class (flag: `--storage-class`, env: none). Precedence: explicit `--storage-class` flag > OpenShift auto-detection (`ibm-spectrum-scale-fileset`) > interactive prompt (shows available classes from cluster). If `--no-cluster`, prompt with no cluster list.

Also detected (not prompted, not in `SetupConfig`):
- OpenShift (`oc whoami` check) — stored as `is_openshift` in SetupConfig
- Container runtime (podman > docker) — auto-detected, stored separately for `setup_config.json` output. Only used by step 5 (test push). If neither found, test push is skipped with a note.

### Step 2: Namespace

- Run `check_cluster_reachable()` (classified errors: DNS, timeout, auth, no config)
- Check if namespace exists; create if not (OpenShift: `oc new-project`, otherwise: `kubectl create ns`)
- Set kubectl context to namespace

Skipped with `--no-cluster`.

### Step 3: RBAC

- Apply `tektonc-data-collection/tekton/roles.yaml` with `$NAMESPACE` substitution
- OpenShift: apply SCC policies (anyuid for default + helm-installer, privileged for helm-installer, cluster-admin for helm-installer)

Skipped with `--no-cluster`.

### Step 4: Secrets

Both secrets created here. No prompting — values come from step 1's `SetupConfig`.

**hf-secret:**
- If secret exists and user reused the existing token (pressed Enter on masked prompt): skip
- Otherwise: `kubectl create secret generic hf-secret --from-literal=HF_TOKEN=<token> --dry-run=client -o yaml | kubectl apply -f -`

**registry-secret:**
- If `registry_user` and `registry_token` are provided: create docker-registry secret
- If not: fall back to container runtime login (`podman login` / `docker login`), then create secret from the resulting config.json
- If secret exists and user reused existing values: skip

Skipped with `--no-cluster`.

### Step 5: Registry Credential Test

```
Push test image to quay.io/jchen/llm-d-inference-scheduler:_test-push?
[y]es / [s]kip: y
Pushing... ✓ Registry credentials verified
```

**On failure:**
```
[ERROR] Registry push failed: <stderr>

[r]etry / [s]kip / [q]uit: _
```

- **retry** — re-runs pull/tag/push (user may have fixed creds in another terminal)
- **skip** — warn that creds are unverified, continue
- **quit** — exit non-zero

**`--test-push` flag:** auto-accepts the `[y]es / [s]kip` prompt (runs the push automatically). On failure:
- Interactive (TTY): shows `[r]etry / [s]kip / [q]uit` prompt
- Non-interactive (no TTY, e.g. CI): exits non-zero immediately (no retry prompt)

**Push + pull verification:** After a successful push, attempt to pull the test image back (`podman pull <image>`) to verify the cluster can also pull from this registry. Report "Registry credentials verified (push + pull)" on success. If pull-back fails, warn but don't block — some registries have propagation delays.

**No container runtime:** Skip with note ("no podman/docker found, skipping test push — credentials will be verified during in-cluster EPP build").

Skipped with `--no-cluster`.

### Step 6: PVCs

Create three PVCs: `model-pvc` (300Gi), `data-pvc` (20Gi), `source-pvc` (20Gi). Skip existing. Wait for Bound status (120s timeout, warn if not bound).

Skipped with `--no-cluster`.

### Step 7: Tekton

- Verify Tekton operator is running in `tekton-pipelines` namespace. This is a **soft warning** — if the operator isn't found, print a warning with install link but continue. Tekton tasks can be pre-deployed before the operator is fully ready.
- Apply all YAML files from `tektonc-data-collection/tekton/steps/` and `tektonc-data-collection/tekton/tasks/` (sorted, applied sequentially via `kubectl apply`)

Skipped with `--no-cluster`.

### Step 8: Config

Write outputs and print next steps.

**Files written:**

`workspace/setup_config.json`:
```json
{
  "namespace": "sim2real-jchen",
  "registry": "quay.io/jchen",
  "repo_name": "llm-d-inference-scheduler",
  "storage_class": "ibm-spectrum-scale-fileset",
  "is_openshift": true,
  "tektonc_dir": "/path/to/tektonc-data-collection",
  "sim2real_root": "/path/to/sim2real",
  "container_runtime": "podman",
  "current_run": "sim2real-2026-04-09",
  "setup_timestamp": "2026-04-09T15:30:00Z"
}
```

`workspace/runs/<run_name>/run_metadata.json`:
```json
{
  "version": 1,
  "namespace": "sim2real-jchen",
  "registry": "quay.io/jchen",
  "repo_name": "llm-d-inference-scheduler",
  "storage_class": "ibm-spectrum-scale-fileset",
  "is_openshift": true,
  "container_runtime": "podman",
  "created_at": "2026-04-09T15:30:00Z",
  "pipeline_commit": "abc1234",
  "stages": {
    "setup":   { "status": "completed", "completed_at": "2026-04-09T15:30:00Z",
                 "summary": "Namespace sim2real-jchen configured, PVCs created, Tekton tasks deployed" },
    "prepare": { "status": "pending" },
    "deploy":  { "status": "pending" },
    "results": { "status": "pending" }
  }
}
```

Update `config/env_defaults.yaml` with `epp_image.build.{hub, name, tag}` values.

**Completion output:**
```
━━━ Setup complete ━━━

Setup config:  workspace/setup_config.json
Run name:      sim2real-2026-04-09
Run directory: workspace/runs/sim2real-2026-04-09/

Next steps:
  1. Edit config/transfer.yaml (algorithm source, workloads, context hints)
  2. Run: python scripts/prepare.py
```

---

## CLI Interface

```bash
# Fully interactive (prompts for everything)
python pipeline/setup.py

# Scripted (all flags, no prompts)
python pipeline/setup.py \
  --namespace sim2real-jchen \
  --registry quay.io/jchen \
  --hf-token hf_xxx \
  --registry-user jchen+robot \
  --registry-token xxx \
  --test-push

# Config only, no cluster operations
python pipeline/setup.py --no-cluster

# Re-apply Tekton tasks only (requires --namespace)
python pipeline/setup.py --redeploy-tasks

# Additional flags
--repo-name NAME       Registry repo name [llm-d-inference-scheduler]
--storage-class SC     PVC storageClassName (auto-detected for OpenShift)
--run NAME             Run name [sim2real-YYYY-MM-DD]
--test-push            Auto-accept test push prompt
--test-push-tag TAG    Image tag for test push [_test-image-push]
```

**`--redeploy-tasks` shortcut:** Skips all steps except step 7 (Tekton). Requires `--namespace`. Useful when Tekton task YAMLs change and need to be re-applied without re-running the full setup. Does not require `setup_config.json` to exist.

---

## Functions: Keep, Remove, Refactor

### Keep as-is
| Function | Used in step |
|---|---|
| `run()` | All steps |
| `which()` | Step 1 (detect container runtime, OpenShift) |
| `prompt()` | Step 1 |
| `prompt_secret()` | Step 1 |
| `secret_exists()` | Step 4 |
| `check_cluster_reachable()` | Step 2 |
| `detect_openshift()` | Step 1 |
| `apply_rbac()` | Step 3 |
| `create_pvcs()` | Step 6 |
| `write_outputs()` | Step 8 |
| `update_env_defaults()` | Step 8 |
| Color helpers (`_c`, `info`, `ok`, `warn`, `err`, `step`) | All |

### Remove
| Function | Reason |
|---|---|
| `check_prerequisites()` | Documented prerequisite. No longer a step. |
| `init_submodules()` | Documented prerequisite. |
| `setup_venv()` | Documented prerequisite. |
| `_interactive_setup()` | Replaced by new step-1 config collection. |
| `_has_explicit_args()` | Replaced by simpler check in new flow. |
| `verify_tekton()` | Folded into step 7 inline (2 lines). |

### Refactor
| Function | Change |
|---|---|
| `test_registry_push()` | Add retry loop. Return success/fail status instead of just warning. Accept retry/skip/quit prompt on failure. |
| `create_hf_secret()` | Simplify — receives token from `SetupConfig`, no prompting. |
| `create_registry_secret()` | Simplify — receives all creds from `SetupConfig`, no prompting. Rename to something like `apply_registry_secret()`. |
| `configure_namespace()` | Simplify — receives namespace from `SetupConfig`, no prompting. |
| `deploy_tekton_tasks()` | Keep logic, fold `verify_tekton` inline. |
| `configure_run_name()` | Move to step 1 config collection. Overwrite prompt stays (if run dir exists). |
| `detect_storage_class()` | Move cluster listing to step 1 config collection. |
| `build_parser()` | Update — remove flags for removed features, keep the rest. |

---

## Run Name Collision Handling

When the run directory already exists (step 1, during config collection):
```
⚠ Run 'sim2real-2026-04-09' already exists at workspace/runs/sim2real-2026-04-09
Overwrite, or enter a new name? [overwrite/NAME]: _
```
Same behavior as current code. This happens during config collection, not during step 8.

---

## `--no-cluster` Behavior

Steps 2-7 print `[skipped — --no-cluster]`. Steps 1 and 8 always run.

This allows generating `setup_config.json` and `run_metadata.json` without a cluster, useful for:
- Testing the script locally
- Generating config for a cluster that's not yet reachable
- CI dry-runs

---

## Prerequisites (Documented, Not Checked by setup.py)

These move to documentation (e.g., `docs/NEWCOMER_GUIDE.md` or README):

1. **Git submodules:** `git submodule update --init inference-sim llm-d-inference-scheduler tektonc-data-collection`
2. **Python venv:** `python -m venv .venv && pip install -r requirements.txt`
3. **Required tools:** `kubectl`, `tkn` (for cluster ops), `envsubst`, `git`
4. **Optional tools:** `podman` or `docker` (for test push only)

If submodules aren't initialized, `apply_rbac` (step 3) will fail with "roles.yaml not found" — a clear enough error.

---

## Exit Codes

- `0` — setup completed successfully
- `1` — error (cluster unreachable, secret creation failed, user quit)

---

## What This Does NOT Change

- `config/env_defaults.yaml` structure (no changes to the file format)
- `workspace/setup_config.json` schema (same fields, same consumers)
- `workspace/runs/<name>/run_metadata.json` schema (adds `version: 1` field)
- `scripts/deploy.py` or `scripts/prepare.py` (they read `setup_config.json`, unchanged)
- The `--redeploy-tasks` shortcut (kept as-is)
