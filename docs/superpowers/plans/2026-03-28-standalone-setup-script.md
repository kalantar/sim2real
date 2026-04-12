# Standalone Setup Script Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `scripts/setup.py` — a standalone, interactive, idempotent Python setup script for the sim2real cluster environment, usable without Claude.

**Architecture:** A single self-contained Python script at `scripts/setup.py`. It uses `argparse` for flag-based non-interactive invocation, `subprocess.run()` for all `kubectl`/`tkn`/`git` calls, `getpass` for secrets, and `PyYAML` (already required by the project) for updating `config/env_defaults.yaml`. It is **not** merged into `tools/transfer_cli.py` — setup is one-time, interactive, and cluster-provisioning in character, while `transfer_cli.py` is pipeline-processing (stateless transforms). The two share no code; any duplication (e.g. a single `kubectl_current_context` call) is acceptable.

**Tech Stack:** Python 3.10+, `subprocess`, `argparse`, `getpass`, `json`, `pathlib`, `PyYAML` (already in `requirements.txt`), `kubectl`/`oc`, `tkn`, `git`, `envsubst`.

**Why not merge with `transfer_cli.py`:**
- `transfer_cli.py` is 2936 lines of stateless pipeline transforms; adding interactive cluster provisioning conflates two different concerns
- `transfer_cli.py` has a stdlib-only constraint for most subcommands (see CLAUDE.md); setup uses PyYAML interactively
- Setup runs once; `transfer_cli.py` commands run per pipeline stage

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `scripts/setup.py` | Create | Standalone interactive setup script |
| `.claude/skills/sim2real-setup/SKILL.md` | Modify | Add "standalone alternative" note |

---

## Chunk 1: Skeleton, argument parsing, and `--help`

### Task 1: Create `scripts/setup.py` with argparse, color helpers, and `--help`

**Files:**
- Create: `scripts/setup.py`

The repo root is two levels up from the script (`scripts/setup.py` → `..`):

```python
REPO_ROOT = Path(__file__).resolve().parent.parent
```

- [ ] **Step 1: Write the script skeleton**

```python
#!/usr/bin/env python3
"""sim2real cluster setup — one-time, idempotent environment bootstrap."""

import argparse
import getpass
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Repo layout ──────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
TEKTONC_DIR = REPO_ROOT / "tektonc-data-collection"

# ── Color helpers ────────────────────────────────────────────────────
_tty = sys.stdout.isatty()
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text

def info(msg: str)  -> None: print(_c("34", "[INFO]  ") + msg)
def ok(msg: str)    -> None: print(_c("32", "[OK]    ") + msg)
def warn(msg: str)  -> None: print(_c("33", "[WARN]  ") + msg)
def err(msg: str)   -> None: print(_c("31", "[ERROR] ") + msg, file=sys.stderr)
def step(n, title: str) -> None:
    print("\n" + _c("36", f"━━━ Step {n}: {title} ━━━"))
```

- [ ] **Step 2: Add `build_parser()` function with all flags**

```python
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="setup.py",
        description="One-time cluster and environment setup for the sim2real pipeline.\n"
                    "Idempotent — safe to re-run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables (alternatives to --flags):
  NAMESPACE, HF_TOKEN, QUAY_ROBOT_USERNAME, QUAY_ROBOT_TOKEN

Examples:
  python scripts/setup.py                         # fully interactive
  python scripts/setup.py --namespace my-ns \\
    --hf-token hf_xxx --registry quay.io/me       # pre-fill common values
  python scripts/setup.py --no-cluster            # local config only, no kubectl
""",
    )
    p.add_argument("--namespace",         metavar="NS",    help="Kubernetes namespace")
    p.add_argument("--hf-token",          metavar="TOKEN", help="HuggingFace API token")
    p.add_argument("--registry",          metavar="REG",   help="Container registry host (e.g. quay.io/username)")
    p.add_argument("--repo-name",         metavar="NAME",  default="llm-d-inference-scheduler",
                                                           help="Registry repository name [%(default)s]")
    p.add_argument("--registry-user",     metavar="USER",  help="Registry robot username")
    p.add_argument("--registry-token",    metavar="TOKEN", help="Registry robot token")
    p.add_argument("--storage-class",     metavar="SC",    help="PVC storageClassName (auto-detected for OpenShift)")
    p.add_argument("--run",               metavar="NAME",  help="Run name [sim2real-YYYY-MM-DD]")
    p.add_argument("--no-cluster",        action="store_true",
                                          help="Skip all kubectl/tkn steps (config collection + JSON outputs only)")
    return p
```

- [ ] **Step 3: Add `main()` entry point stub**

```python
def main() -> int:
    args = build_parser().parse_args()
    # Steps filled in by later tasks
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Make executable and verify `--help`**

```bash
chmod +x scripts/setup.py
python scripts/setup.py --help
```
Expected: help block printed, exit 0. Check `--no-cluster` flag appears.

- [ ] **Step 5: Commit skeleton**

```bash
git add scripts/setup.py
git commit -m "feat(scripts): add setup.py skeleton with argparse and --help"
```

---

## Chunk 2: Shared utilities — `run()`, `prompt()`, prerequisite check

### Task 2: Subprocess helper, interactive prompts, prerequisite check

**Files:**
- Modify: `scripts/setup.py`

- [ ] **Step 1: Add `run()` subprocess helper**

```python
def run(cmd: list[str], *, check: bool = True, capture: bool = False,
        input: str | None = None) -> subprocess.CompletedProcess:
    """Run a command, raise on non-zero unless check=False."""
    return subprocess.run(cmd, check=check, text=True, capture_output=capture, input=input)

def which(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None
```

- [ ] **Step 2: Add `prompt()` and `prompt_secret()` helpers**

```python
def prompt(var_name: str, message: str, default: str = "", env_var: str = "") -> str:
    """Return env_var value, or prompt interactively with optional default."""
    value = os.environ.get(env_var or var_name.upper(), "")
    if value:
        return value
    suffix = f" [{default}]" if default else ""
    raw = input(f"{message}{suffix}: ").strip()
    return raw or default

def prompt_secret(message: str, env_var: str = "") -> str:
    """Return env var value or prompt with hidden input."""
    value = os.environ.get(env_var, "")
    if value:
        return value
    return getpass.getpass(f"{message}: ")
```

- [ ] **Step 3: Add `check_prerequisites()` function**

```python
INSTALL_HINTS = {
    "kubectl": "https://kubernetes.io/docs/tasks/tools/",
    "tkn":     "https://tekton.dev/docs/cli/",
    "python3": "https://www.python.org/downloads/",
    "gh":      "https://cli.github.com/",
    "git":     "https://git-scm.com/downloads",
    "envsubst": "brew install gettext  OR  apt install gettext",
}

def check_prerequisites(no_cluster: bool) -> str:
    """Check required tools. Returns detected container runtime. Exits on missing tools."""
    step(1, "Checking prerequisites")
    missing = []

    for cmd in ["python3", "gh", "git", "envsubst"]:
        if not which(cmd):
            hint = INSTALL_HINTS.get(cmd, "")
            missing.append(f"{cmd}  →  {hint}" if hint else cmd)

    if not no_cluster:
        for cmd in ["kubectl", "tkn"]:
            if not which(cmd):
                hint = INSTALL_HINTS.get(cmd, "")
                missing.append(f"{cmd}  →  {hint}" if hint else cmd)

    container_rt = next((rt for rt in ["podman", "docker"] if which(rt)), "")
    if not container_rt and not no_cluster:
        missing.append("podman or docker  →  https://podman.io/getting-started/installation")

    if missing:
        err("Missing prerequisites:")
        for m in missing:
            print(f"  • {m}")
        sys.exit(1)

    suffix = f" (container runtime: {container_rt})" if container_rt else ""
    ok(f"All prerequisites found{suffix}")
    return container_rt
```

- [ ] **Step 4: Wire prerequisite check into `main()`**

```python
def main() -> int:
    args = build_parser().parse_args()
    container_rt = check_prerequisites(args.no_cluster)
    return 0
```

- [ ] **Step 5: Smoke test**

```bash
python scripts/setup.py --no-cluster --help
```
Expected: help block, exit 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/setup.py
git commit -m "feat(scripts): add subprocess helpers, prompt utilities, and prereq check"
```

---

## Chunk 3: Submodules, Python venv, namespace + cluster detection

### Task 3: Environment bootstrap and namespace collection

**Files:**
- Modify: `scripts/setup.py`

- [ ] **Step 1: Add `init_submodules()` (Step 2)**

```python
def init_submodules() -> None:
    step(2, "Initializing git submodules")
    run(["git", "-C", str(REPO_ROOT), "submodule", "update", "--init",
         "inference-sim", "llm-d-inference-scheduler", "tektonc-data-collection"],
        check=False)
    ok("Submodules initialized")
```

- [ ] **Step 2: Add `setup_venv()` (Step 3)**

```python
def setup_venv() -> None:
    step(3, "Setting up Python environment")
    venv_dir = REPO_ROOT / ".venv"
    if not venv_dir.exists():
        run([sys.executable, "-m", "venv", str(venv_dir)])
        info("Created .venv")
    pip = venv_dir / "bin" / "pip"
    req = REPO_ROOT / "requirements.txt"
    if req.exists():
        run([str(pip), "install", "-q", "-r", str(req)], check=False)
    else:
        run([str(pip), "install", "-q", "PyYAML", "jinja2"], check=False)
    ok("Python venv ready")
```

- [ ] **Step 3: Add `detect_openshift()` helper**

```python
def detect_openshift() -> bool:
    if not which("oc"):
        return False
    return run(["oc", "whoami"], check=False, capture=True).returncode == 0
```

- [ ] **Step 4: Add `configure_namespace()` (Step 4)**

```python
def configure_namespace(args: argparse.Namespace, is_openshift: bool) -> str:
    step(4, "Configuring namespace")
    namespace = args.namespace or prompt("namespace", "Enter Kubernetes namespace", env_var="NAMESPACE")
    if not namespace:
        err("NAMESPACE is required"); sys.exit(1)

    if not args.no_cluster:
        exists = run(["kubectl", "get", "ns", namespace], check=False, capture=True).returncode == 0
        if exists:
            ok(f"Namespace {namespace} already exists")
        else:
            if is_openshift:
                proc = run(["oc", "new-project", namespace], check=False, capture=True)
                if proc.returncode != 0 and "AlreadyExists" not in (proc.stderr or ""):
                    proc.check_returncode()
            else:
                run(["kubectl", "create", "ns", namespace])
            ok(f"Namespace {namespace} ready")

        result = run(["kubectl", "config", "set-context", "--current",
                      f"--namespace={namespace}"], check=False, capture=True)
        if result.returncode == 0:
            ok(f"kubectl context set to namespace {namespace}")
        else:
            warn("Could not set kubectl context namespace")
    return namespace
```

- [ ] **Step 5: Add `configure_run_name()` (Step 4b)**

```python
def configure_run_name(args: argparse.Namespace) -> tuple[str, Path]:
    step("4b", "Configuring run name")
    default = f"sim2real-{datetime.now().strftime('%Y-%m-%d')}"
    run_name = args.run or prompt("run_name", "Enter a name for this run", default=default)
    run_dir = REPO_ROOT / "workspace" / "runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    ok(f"Run directory: {run_dir}")
    return run_name, run_dir
```

- [ ] **Step 6: Add `detect_storage_class()` helper**

```python
def detect_storage_class(args: argparse.Namespace, is_openshift: bool) -> str:
    if args.storage_class:
        info(f"Using storageClassName: {args.storage_class}")
        return args.storage_class
    if is_openshift:
        sc = "ibm-spectrum-scale-fileset"
        info(f"OpenShift detected — using storageClass: {sc}")
        return sc
    if not args.no_cluster:
        result = run(["kubectl", "get", "storageclass", "--no-headers"],
                     check=False, capture=True)
        if result.returncode == 0 and result.stdout.strip():
            print()
            info("Available storage classes:")
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                marker = "  [default]" if "(default)" in line else ""
                print(f"  • {parts[0]}{marker}")
    sc = prompt("storage_class",
                "Enter PVC storageClassName (leave blank for cluster default)", default="")
    if sc:
        info(f"Using storageClassName: {sc}")
    else:
        info("Using cluster default storageClassName")
    return sc
```

- [ ] **Step 7: Wire Steps 2–4b into `main()`**

```python
def main() -> int:
    args = build_parser().parse_args()
    container_rt = check_prerequisites(args.no_cluster)
    init_submodules()
    setup_venv()
    is_openshift = detect_openshift()
    namespace = configure_namespace(args, is_openshift)
    run_name, run_dir = configure_run_name(args)
    storage_class = detect_storage_class(args, is_openshift)
    return 0
```

- [ ] **Step 8: Test with `--no-cluster`**

```bash
python scripts/setup.py --no-cluster --namespace test-ns --run smoke-001
```
Expected: Steps 1–4b print OK, `workspace/runs/smoke-001/` created, no kubectl calls.

- [ ] **Step 9: Commit**

```bash
git add scripts/setup.py
git commit -m "feat(scripts): add submodule init, venv, namespace, run name, storage class"
```

---

## Chunk 4: Secrets — HuggingFace and registry

### Task 4: Secret collection and creation

**Files:**
- Modify: `scripts/setup.py`

- [ ] **Step 1: Add `create_hf_secret()` (Step 5)**

```python
def create_hf_secret(args: argparse.Namespace, namespace: str) -> str:
    step(5, "Creating HuggingFace secret")
    hf_token = args.hf_token or prompt_secret("Enter HuggingFace token", env_var="HF_TOKEN")
    if not hf_token:
        err("HF_TOKEN is required"); sys.exit(1)
    if not args.no_cluster:
        yaml_out = run(
            ["kubectl", "create", "secret", "generic", "hf-secret",
             f"--namespace={namespace}",
             f"--from-literal=HF_TOKEN={hf_token}",
             "--dry-run=client", "-o", "yaml"],
            capture=True,
        ).stdout
        run(["kubectl", "apply", "-f", "-"], input=yaml_out)
        ok("hf-secret created/updated")
    else:
        ok("hf-secret (skipped — --no-cluster)")
    return hf_token
```

- [ ] **Step 2: Add `create_registry_secret()` (Step 10)**

```python
def create_registry_secret(args: argparse.Namespace, namespace: str,
                            container_rt: str) -> tuple[str, str]:
    step(10, "Configuring container registry for treatment EPP image")
    print()
    info("The pipeline compiles your scorer plugin into a treatment EPP image using")
    info("BuildKit running inside the cluster (faster than a local build, and avoids")
    info("cross-architecture issues on arm64 laptops targeting amd64 clusters).")
    info("The built image is pushed to your registry and pulled back by the cluster")
    info("for benchmarking. You need push access to a registry the cluster can also pull from.")
    print()
    info("If using Quay.io, you need a robot account with Write access:")
    print("  1. https://quay.io/repository/ — create a repository")
    print("  2. Account Settings → Robot Accounts → Create Robot Account")
    print("  3. Grant robot 'Write' on your repository")
    print()

    registry = args.registry or prompt(
        "registry", "Container registry host (e.g. quay.io/username, or blank to skip)", default="")
    repo_name = args.repo_name

    if not registry:
        warn("No registry — skipping. Update config/env_defaults.yaml manually.")
        return "", repo_name

    repo_name = prompt("repo_name", "Repository name", default=repo_name)
    docker_server = registry.split("/")[0]

    reg_user  = args.registry_user  or os.environ.get("QUAY_ROBOT_USERNAME", "")
    reg_token = args.registry_token or os.environ.get("QUAY_ROBOT_TOKEN", "")
    if not reg_user:
        reg_user = prompt("registry_user",
            "Registry robot username (or press Enter to use container login)", default="")
    if reg_user and not reg_token:
        reg_token = prompt_secret("Registry robot token", env_var="QUAY_ROBOT_TOKEN")

    if not args.no_cluster:
        if reg_user and reg_token:
            yaml_out = run(
                ["kubectl", "create", "secret", "docker-registry", "registry-secret",
                 f"--namespace={namespace}",
                 f"--docker-server={docker_server}",
                 f"--docker-username={reg_user}",
                 f"--docker-password={reg_token}",
                 "--dry-run=client", "-o", "yaml"],
                capture=True,
            ).stdout
            run(["kubectl", "apply", "-f", "-"], input=yaml_out)
            ok("registry-secret created/updated")
        else:
            info(f"Falling back to container runtime login ({container_rt})...")
            result = run([container_rt, "login", docker_server], check=False)
            if result.returncode != 0:
                err("Registry login failed. Provide --registry-user and --registry-token.")
                sys.exit(1)
            config_candidates = [
                Path.home() / ".docker" / "config.json",
                Path.home() / ".config" / "containers" / "auth.json",
                Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "containers" / "auth.json",
            ]
            config_path = next((p for p in config_candidates if p.exists()), None)
            if not config_path:
                err("Could not locate docker config after login. "
                    "Provide --registry-user and --registry-token.")
                sys.exit(1)
            yaml_out = run(
                ["kubectl", "create", "secret", "docker-registry", "registry-secret",
                 f"--namespace={namespace}",
                 f"--from-file=.dockerconfigjson={config_path}",
                 "--dry-run=client", "-o", "yaml"],
                capture=True,
            ).stdout
            run(["kubectl", "apply", "-f", "-"], input=yaml_out)
            ok("registry-secret created/updated from container login")
    else:
        ok("registry-secret (skipped — --no-cluster)")
    return registry, repo_name
```

- [ ] **Step 3: Commit**

```bash
git add scripts/setup.py
git commit -m "feat(scripts): add HF secret and registry secret steps"
```

---

## Chunk 5: RBAC, PVCs, Tekton, config update, JSON outputs

### Task 5: Cluster resource deployment, config/env_defaults.yaml update, and output files

**Files:**
- Modify: `scripts/setup.py`

- [ ] **Step 1: Add `apply_rbac()` (Step 6)**

Uses `envsubst` via subprocess — same approach as the skill script.

```python
def apply_rbac(namespace: str, is_openshift: bool, no_cluster: bool) -> None:
    step(6, "Applying RBAC roles")
    if no_cluster:
        ok("RBAC (skipped — --no-cluster)"); return
    roles_yaml = TEKTONC_DIR / "tekton" / "roles.yaml"
    if not roles_yaml.exists():
        err(f"roles.yaml not found at {roles_yaml} — did submodule init fail?"); sys.exit(1)
    env = {**os.environ, "NAMESPACE": namespace}
    subst = subprocess.run(
        ["envsubst", "$NAMESPACE"],
        input=roles_yaml.read_text(), capture_output=True, text=True, env=env, check=True,
    )
    run(["kubectl", "apply", "-f", "-"], input=subst.stdout)
    if is_openshift:
        warn("OpenShift: adding SCC policies")
        for policy_args in [
            ["add-scc-to-user",        "anyuid",       "-z", "default",        "-n", namespace],
            ["add-scc-to-user",        "anyuid",       "-z", "helm-installer", "-n", namespace],
            ["add-scc-to-user",        "privileged",   "-z", "helm-installer", "-n", namespace],
            ["add-cluster-role-to-user", "cluster-admin", "-z", "helm-installer"],
        ]:
            run(["oc", "adm", "policy"] + policy_args, check=False)
    ok("RBAC roles applied")
```

- [ ] **Step 2: Add `create_pvcs()` (Step 7)**

```python
def create_pvcs(namespace: str, storage_class: str, no_cluster: bool) -> None:
    step(7, "Creating PVCs")
    pvcs = [("model-pvc", "300Gi"), ("data-pvc", "20Gi"), ("source-pvc", "20Gi")]
    sc_line = f"  storageClassName: {storage_class}" if storage_class else ""

    for name, size in pvcs:
        if no_cluster:
            ok(f"PVC {name} (skipped — --no-cluster)"); continue
        if run(["kubectl", "get", "pvc", name, f"-n={namespace}"],
               check=False, capture=True).returncode == 0:
            ok(f"PVC {name} already exists"); continue
        manifest = (
            f"apiVersion: v1\nkind: PersistentVolumeClaim\n"
            f"metadata:\n  name: {name}\n  namespace: {namespace}\n"
            f"spec:\n{sc_line}\n  accessModes:\n    - ReadWriteMany\n"
            f"  resources:\n    requests:\n      storage: {size}\n"
        )
        run(["kubectl", "apply", "-f", "-"], input=manifest)
        ok(f"Created PVC {name} ({size})")

    if not no_cluster:
        info("Waiting for PVCs to bind (timeout 120s)...")
        for name, _ in pvcs:
            result = run(
                ["kubectl", "wait", "--for=jsonpath={.status.phase}=Bound",
                 f"pvc/{name}", f"-n={namespace}", "--timeout=120s"],
                check=False, capture=True,
            )
            if result.returncode == 0:
                ok(f"PVC {name} is Bound")
            else:
                warn(f"PVC {name} not yet Bound — check storageClass: {storage_class or '(default)'}")
```

- [ ] **Step 3: Add `verify_tekton()` (Step 8) and `deploy_tekton_tasks()` (Step 9)**

```python
def verify_tekton(no_cluster: bool) -> None:
    step(8, "Verifying Tekton operator")
    if no_cluster: return
    result = run(["kubectl", "get", "pods", "-n", "tekton-pipelines", "--no-headers"],
                 check=False, capture=True)
    if result.returncode == 0 and "Running" in result.stdout:
        ok("Tekton operator is running")
    else:
        warn("Tekton operator not detected — install: https://tekton.dev/docs/installation/pipelines/")

def deploy_tekton_tasks(namespace: str, no_cluster: bool) -> None:
    step(9, "Deploying Tekton steps and tasks")
    if no_cluster:
        ok("Tekton deployment (skipped — --no-cluster)"); return
    for subdir in ["steps", "tasks"]:
        for yaml_file in sorted((TEKTONC_DIR / "tekton" / subdir).glob("*.yaml")):
            run(["kubectl", "apply", "-f", str(yaml_file), f"-n={namespace}"])
    ok("Tekton steps and tasks deployed")
```

- [ ] **Step 4: Add `update_env_defaults()` (Step 12)**

Updates only `stack.gaie.epp_image.build.hub` and `build.name` — the treatment (developer-built) registry. Upstream EPP image coordinates live in `config/env_defaults.yaml` already and are not prompted for; the user edits that file directly if they want a different upstream image.

Uses PyYAML. Limitation: YAML comments are stripped. Noted in the completion warning.

```python
def update_env_defaults(registry: str, repo_name: str) -> None:
    step(12, "Updating config/env_defaults.yaml with registry values")
    cfg_path = REPO_ROOT / "config" / "env_defaults.yaml"
    if not cfg_path.exists():
        warn("config/env_defaults.yaml not found — skipping"); return
    if not registry:
        warn("No registry provided — config/env_defaults.yaml not updated. "
             "Update stack.gaie.epp_image.build.hub manually."); return

    try:
        import yaml
    except ImportError:
        warn("PyYAML not available — skipping config update. Run: pip install PyYAML"); return

    with cfg_path.open() as f:
        cfg = yaml.safe_load(f)

    build = (cfg.setdefault("stack", {})
               .setdefault("gaie", {})
               .setdefault("epp_image", {})
               .setdefault("build", {}))
    build["hub"]  = registry
    build["name"] = repo_name

    with cfg_path.open("w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    ok("config/env_defaults.yaml updated with registry info")
    warn("Note: PyYAML reformatted the file — inline comments were removed. "
         "Review the diff before committing.")
```

- [ ] **Step 5: Add `write_outputs()` — JSON files**

```python
def write_outputs(namespace: str, registry: str, repo_name: str,
                  storage_class: str, is_openshift: bool, container_rt: str,
                  run_name: str, run_dir: Path) -> None:
    workspace = REPO_ROOT / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    commit = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip() or "unknown"

    setup_config = {
        "namespace": namespace, "registry": registry, "repo_name": repo_name,
        "storage_class": storage_class,
        "is_openshift": is_openshift, "tektonc_dir": str(TEKTONC_DIR),
        "sim2real_root": str(REPO_ROOT), "container_runtime": container_rt,
        "setup_timestamp": now_iso, "run_name": run_name, "run_dir": str(run_dir),
    }
    setup_path = workspace / "setup_config.json"
    setup_path.write_text(json.dumps(setup_config, indent=2))
    ok(f"Setup config saved to {setup_path}")

    metadata = {
        **{k: v for k, v in setup_config.items()
           if k not in {"setup_timestamp", "tektonc_dir", "sim2real_root"}},
        "created_at": now_iso,
        "pipeline_commit": commit,
        "stages": {
            "setup":   {"status": "completed", "completed_at": now_iso,
                        "summary": f"Namespace {namespace} configured, PVCs created, Tekton tasks deployed"},
            "prepare": {"status": "pending"},
            "deploy":  {"status": "pending"},
            "results": {"status": "pending"},
        },
    }
    meta_path = run_dir / "run_metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2))
    ok(f"Run metadata saved to {meta_path}")
```

- [ ] **Step 6: Wire everything into `main()`**

```python
def main() -> int:
    args = build_parser().parse_args()

    container_rt  = check_prerequisites(args.no_cluster)
    init_submodules()
    setup_venv()

    is_openshift  = detect_openshift()
    namespace     = configure_namespace(args, is_openshift)
    run_name, run_dir = configure_run_name(args)
    storage_class = detect_storage_class(args, is_openshift)

    create_hf_secret(args, namespace)
    apply_rbac(namespace, is_openshift, args.no_cluster)
    create_pvcs(namespace, storage_class, args.no_cluster)
    verify_tekton(args.no_cluster)
    deploy_tekton_tasks(namespace, args.no_cluster)

    registry, repo_name = create_registry_secret(args, namespace, container_rt)

    update_env_defaults(registry, repo_name)
    write_outputs(namespace, registry, repo_name, storage_class, is_openshift, container_rt,
                  run_name, run_dir)

    print()
    print(_c("32", "━━━ Setup complete ━━━"))
    print()
    cfg_path = REPO_ROOT / "workspace" / "setup_config.json"
    print(f"Setup config:  {cfg_path}")
    print(f"Run name:      {run_name}")
    print(f"Run directory: {run_dir}")
    print()
    print("Next steps:")
    print("  1. Review config/env_defaults.yaml (gateway, vLLM image, fast_iteration)")
    print("  2. Run: python scripts/prepare.py  OR  /sim2real-prepare in Claude")
    return 0
```

- [ ] **Step 7: Full smoke test with `--no-cluster`**

```bash
python scripts/setup.py \
  --no-cluster \
  --namespace test-ns \
  --hf-token dummy-token \
  --registry quay.io/testuser \
  --run smoke-001 \
  --storage-class standard
```
Expected: Steps 1–12 complete with no kubectl calls.

```bash
python3 -c "
import json
from pathlib import Path
c = json.loads(Path('workspace/setup_config.json').read_text())
assert c['namespace'] == 'test-ns', c
assert c['run_name'] == 'smoke-001', c
m = json.loads(Path('workspace/runs/smoke-001/run_metadata.json').read_text())
assert m['stages']['setup']['status'] == 'completed', m
print('All assertions pass')
"
```
Expected: `All assertions pass`

- [ ] **Step 8: Commit**

```bash
git add scripts/setup.py
git commit -m "feat(scripts): add RBAC, PVCs, Tekton, config update, and JSON output steps"
```

---

## Chunk 6: SKILL.md update

### Task 6: Update sim2real-setup SKILL.md to reference the standalone script

**Files:**
- Modify: `.claude/skills/sim2real-setup/SKILL.md`

- [ ] **Step 1: Add "Standalone option" note to the top of the `## Execution` section**

```markdown
> **Standalone option:** Users who prefer not to invoke Claude can run the
> script directly from the repo root:
> ```bash
> python scripts/setup.py --help
> python scripts/setup.py --namespace <NS> --hf-token <TOKEN> --registry <REG>
> ```
> The script is interactive — it prompts for any missing values.
```

- [ ] **Step 2: Commit**

```bash
git add .claude/skills/sim2real-setup/SKILL.md
git commit -m "docs(skill): add standalone script reference to sim2real-setup SKILL.md"
```

---

## Design Decisions for Review

1. **PyYAML comment stripping in `update_env_defaults()`:** PyYAML does not preserve comments. The function warns the user after writing. Alternative: targeted `re.sub()` on the file text for the specific keys (`build.hub`, `build.name`, `upstream.*`). This is more brittle but preserves comments. **Recommendation:** Accept PyYAML for now — the warning + `git diff` review step is sufficient.

2. **`--no-cluster` flag:** Named per Python convention (negative boolean flags). Skips all `kubectl`/`oc`/`tkn` calls but still collects config and writes JSON outputs. Useful for local testing and CI smoke tests.

3. **Not merged into `transfer_cli.py`:** Setup is one-time interactive cluster provisioning. `transfer_cli.py` is stateless pipeline processing (2936 lines). Merging conflates different concerns. The only shared utility (`_kubectl_current_context`) is small enough to duplicate.

4. **`envsubst` still used for RBAC:** `roles.yaml` uses `$NAMESPACE` substitution. Rather than re-implementing this in Python, we keep the `envsubst` subprocess call — it's already a declared prerequisite in the skill.

5. **`setup_venv()` is included but optional in practice:** The script itself can run outside the venv (it only needs stdlib + PyYAML for the config update). The venv setup step is retained to bootstrap the environment for pipeline stages that come after. If it proves confusing, it can be removed.
