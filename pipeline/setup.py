#!/usr/bin/env python3
"""sim2real cluster setup — one-time, idempotent environment bootstrap."""

import argparse
import getpass
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

@dataclass
class SetupConfig:
    namespace: str
    namespaces: list[str]
    registry: str
    repo_name: str
    run_name: str
    hf_token: str
    github_token: str
    registry_user: str
    registry_token: str
    storage_class: str
    is_openshift: bool
    no_cluster: bool
    pipeline_yaml: str | None = None
    orchestrator_image: str = ""

# ── Repo layout ──────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
TEKTONC_DIR = REPO_ROOT / "tektonc-data-collection"
_DEFAULT_HF_SECRET_NAME = "hf-secret"

# Overridden in main() when --experiment-root is specified.
EXPERIMENT_ROOT = REPO_ROOT

# ── Color helpers ────────────────────────────────────────────────────
_tty = sys.stdout.isatty()
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text

def info(msg: str)  -> None: print(_c("34", "[INFO]  ") + msg)
def ok(msg: str)    -> None: print(_c("32", "[OK]    ") + msg)
def warn(msg: str)  -> None: print(_c("33", "[WARN]  ") + msg)
def err(msg: str)   -> None: print(_c("31", "[ERROR] ") + msg, file=sys.stderr)
def step(n, total, title: str) -> None:
    print("\n" + _c("36", f"━━━ [{n}/{total}] {title} ━━━"))

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pipeline/setup.py",
        description="One-time cluster and environment setup for the sim2real pipeline.\n"
                    "Idempotent — safe to re-run. 8-step flow: config, namespace, RBAC,\n"
                    "secrets, registry test, PVCs, Tekton, config output.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables (alternatives to --flags):
  NAMESPACE, HF_TOKEN, REGISTRY_USER, REGISTRY_TOKEN, GITHUB_TOKEN

Examples:
  python pipeline/setup.py                         # fully interactive
  python pipeline/setup.py --namespace my-ns \\
    --hf-token hf_xxx --registry quay.io/me       # pre-fill common values
  python pipeline/setup.py --no-cluster            # local config only, no kubectl
  python pipeline/setup.py --redeploy-tasks --namespace my-ns
""",
    )
    p.add_argument("--namespace",      metavar="NS",    help="Kubernetes namespace")
    p.add_argument("--namespaces",     metavar="NS1,NS2,...",
                                       help="Comma-separated list of namespaces to provision (overrides --namespace)")
    p.add_argument("--hf-token",       metavar="TOKEN", help="HuggingFace API token")
    p.add_argument("--github-token",   metavar="TOKEN", help="GitHub token for private repo access")
    p.add_argument("--registry",       metavar="REG",   help="Container registry host (e.g. quay.io/username)")
    p.add_argument("--repo-name",      metavar="NAME",  default=None,
                                                        help="Registry repository name [llm-d-inference-scheduler]")
    p.add_argument("--registry-user",  metavar="USER",  help="Registry username")
    p.add_argument("--registry-token", metavar="TOKEN", help="Registry token")
    p.add_argument("--storage-class",  metavar="SC",    help="PVC storageClassName (auto-detected for OpenShift)")
    p.add_argument("--run",            metavar="NAME",  help="Run name [sim2real-YYYY-MM-DD]")
    p.add_argument("--experiment-root", metavar="PATH", dest="experiment_root",
                   help="Root of the experiment repo (default: framework directory)")
    p.add_argument("--no-cluster",      action="store_true",
                                       help="Skip all kubectl/tkn steps (config + JSON output only)")
    p.add_argument("--pipeline-yaml",  metavar="PATH",
                                       help="Path to Tekton Pipeline YAML to apply "
                                            "(default: <repo-root>/pipeline/pipeline.yaml)")
    p.add_argument("--redeploy-tasks", action="store_true",
                                       help="Re-apply Tekton step/task YAMLs only (requires --namespace)")
    p.add_argument("--test-push",      action="store_true",
                                       help="Auto-accept test push prompt")
    p.add_argument("--test-push-tag",  metavar="TAG",   default="_test-image-push",
                                       help="Image tag for test push [%(default)s]")
    p.add_argument("--orchestrator-image", metavar="IMAGE",
                                       help="Orchestrator container image for --remote mode "
                                            "(e.g. ghcr.io/inference-sim/sim2real/orchestrator:latest)")
    return p

def run(cmd: list[str], *, check: bool = True, capture: bool = False,
        input: str | None = None) -> subprocess.CompletedProcess:
    """Run a command, raise on non-zero unless check=False."""
    return subprocess.run(cmd, check=check, text=True, capture_output=capture, input=input)

def which(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None

def prompt(var_name: str, message: str, default: str = "", env_var: str = "") -> str:
    """Return env_var value, or prompt interactively with optional default."""
    value = os.environ.get(env_var or var_name.upper(), "")
    if value:
        return value
    suffix = f" [{default}]" if default else ""
    raw = input(f"{message}{suffix}: ").strip()
    return raw or default

def prompt_secret(message: str, env_var: str = "") -> str:
    """Return env var value, or prompt with hidden input.

    At the prompt the user may also type an env var name (e.g. REGISTRY_TOKEN)
    instead of the secret itself — the script will resolve it from the environment.
    Character count is printed after entry so the user can confirm input was received.
    """
    import re
    value = os.environ.get(env_var, "") if env_var else ""
    if value:
        return value
    hint = f"  (or type an env var name, e.g. {env_var})" if env_var else ""
    print(hint)
    raw = getpass.getpass(f"{message}: ")
    # If input looks like an env var name, try resolving it
    if re.match(r'^[A-Z][A-Z0-9_]{1,}$', raw):
        resolved = os.environ.get(raw, "")
        if resolved:
            ok(f"Read from env var {raw} ({len(resolved)} characters)")
            return resolved
    if raw:
        ok(f"Token received ({len(raw)} characters)")
    return raw

# ── Helpers ──────────────────────────────────────────────────────────

def _detect_container_runtime() -> str:
    """Auto-detect podman or docker. Returns empty string if neither found."""
    return next((rt for rt in ["podman", "docker"] if which(rt)), "")


def _resolve_storage_class(args: argparse.Namespace, is_openshift: bool) -> str:
    """Resolve storage class: flag > OpenShift auto-detect > interactive prompt."""
    if args.storage_class:
        return args.storage_class
    if is_openshift:
        sc = "ibm-spectrum-scale-fileset"
        info(f"OpenShift detected — using storageClass: {sc}")
        return sc
    if not args.no_cluster:
        result = run(["kubectl", "get", "storageclass", "--no-headers"],
                     check=False, capture=True)
        if result.returncode == 0 and result.stdout.strip():
            info("Available storage classes:")
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                marker = "  [default]" if "(default)" in line else ""
                print(f"  • {parts[0]}{marker}")
    return prompt("storage_class",
                  "Enter PVC storageClassName (leave blank for cluster default)", default="")


def _resolve_run_name(args: argparse.Namespace) -> tuple[str, Path]:
    """Resolve run name, handle directory collision."""
    default = f"sim2real-{datetime.now().strftime('%Y-%m-%d')}"
    run_name = args.run or prompt("run_name", "Enter a name for this run", default=default)

    while True:
        run_dir = EXPERIMENT_ROOT / "workspace" / "runs" / run_name
        if run_dir.exists():
            warn(f"Run '{run_name}' already exists at {run_dir}")
            answer = prompt("overwrite", "Overwrite it, or enter a new name? [overwrite/NAME]",
                            default="overwrite")
            if answer.strip().lower() in ("overwrite", "o", "y", "yes", ""):
                break
            run_name = answer.strip()
        else:
            break

    run_dir.mkdir(parents=True, exist_ok=True)
    return run_name, run_dir


def detect_openshift() -> bool:
    if not which("oc"):
        return False
    return run(["oc", "whoami"], check=False, capture=True).returncode == 0


def check_cluster_reachable() -> None:
    """Verify the cluster is reachable before running any kubectl commands. Exits with a
    friendly message on common connectivity failures."""
    result = run(["kubectl", "cluster-info"], check=False, capture=True)
    if result.returncode == 0:
        return

    combined = (result.stdout + result.stderr).lower()

    # Classify the failure and give an actionable suggestion
    if "no such host" in combined or "name resolution" in combined or "lookup" in combined:
        reason = "DNS resolution failed — the cluster hostname is not reachable."
        hint   = "Check your VPN, network connection, or kubeconfig server address."
    elif "connection refused" in combined:
        reason = "Connection refused — nothing is listening on the cluster API endpoint."
        hint   = "Ensure the cluster is running and the API server port is accessible."
    elif "i/o timeout" in combined or "timeout" in combined:
        reason = "Connection timed out — the cluster API server did not respond."
        hint   = "Check your VPN or firewall; the cluster may be stopped or unreachable."
    elif "unauthorized" in combined or "forbidden" in combined:
        reason = "Authentication failed — your credentials or kubeconfig may be expired."
        hint   = "Try: kubectl config view  or re-run your cluster login command."
    elif "no configuration" in combined or "no such file" in combined:
        reason = "No kubeconfig found — kubectl has no cluster to connect to."
        hint   = "Set KUBECONFIG or run your cluster login command first."
    else:
        reason = "kubectl cluster-info failed."
        hint   = result.stderr.strip() or result.stdout.strip()

    err(f"Cluster unreachable: {reason}")
    print(f"  → {hint}")
    print()
    print("  If you meant to run without a cluster, re-run with --no-cluster.")
    sys.exit(1)


def secret_exists(name: str, namespace: str) -> bool:
    """Return True if the named Kubernetes secret exists in the namespace."""
    return run(["kubectl", "get", "secret", name, f"-n={namespace}"],
               check=False, capture=True).returncode == 0

# ── Step 1: Configuration ────────────────────────────────────────────

def collect_config(args: argparse.Namespace) -> tuple[SetupConfig, Path, str]:
    """Step 1: Collect all config upfront. Returns (config, run_dir, container_runtime).

    Three modes:
    - Fresh install (no setup_config.json): prompt for each value
    - Reuse (existing setup_config.json): show current values as defaults
    - Fully scripted (all flags): no prompts
    """
    step(1, 8, "Configuration")

    # Load prior config for defaults
    config_path = EXPERIMENT_ROOT / "workspace" / "setup_config.json"
    defaults = json.loads(config_path.read_text()) if config_path.exists() else {}
    if defaults:
        info("Loading defaults from previous setup_config.json")

    # Resolve namespace list
    namespaces_raw = args.namespaces or os.environ.get("NAMESPACES", "")
    if namespaces_raw:
        namespaces = [n.strip() for n in namespaces_raw.split(",") if n.strip()]
        if not namespaces:
            err("--namespaces produced an empty list"); sys.exit(1)
        namespace = namespaces[0]
    else:
        ns_default = defaults.get("namespace", "sim2real-" + os.environ.get("USER", "dev"))
        ns_input = args.namespace or prompt("namespace",
                                            "Kubernetes namespace(s) (comma-separated for multiple)",
                                            default=ns_default, env_var="NAMESPACE")
        if not ns_input:
            err("NAMESPACE is required"); sys.exit(1)
        namespaces = [n.strip() for n in ns_input.split(",") if n.strip()]
        namespace = namespaces[0]

    # Registry
    reg_default = defaults.get("registry", "")
    registry = args.registry or prompt("registry",
        "Container registry (e.g. quay.io/username)", default=reg_default)

    # Repo name (argparse default is None so we can detect "not passed")
    repo_default = defaults.get("repo_name", "llm-d-inference-scheduler")
    if args.repo_name is not None:
        repo_name = args.repo_name
    else:
        repo_name = prompt("repo_name", "Registry repo name", default=repo_default)

    # Detect OpenShift early (feeds storage class + RBAC)
    is_openshift = detect_openshift()

    # Storage class
    storage_class = _resolve_storage_class(args, is_openshift)

    # Run name + directory
    run_name, run_dir = _resolve_run_name(args)

    # HuggingFace token — empty string means "reuse existing secret" (checked in step_secrets)
    hf_token = args.hf_token or prompt_secret("HuggingFace token", env_var="HF_TOKEN")

    # GitHub token — used by install-llmdbenchmark to clone repos
    gh_token = args.github_token or os.environ.get("GITHUB_TOKEN", "")

    # Registry credentials — resolve from args > env > ghcr.io fallback > prompt
    docker_server = registry.split("/")[0] if registry else ""
    reg_user = args.registry_user or os.environ.get("REGISTRY_USER", "")
    reg_token = args.registry_token or os.environ.get("REGISTRY_TOKEN", "")
    if not reg_user and not reg_token and docker_server == "ghcr.io":
        github_token = os.environ.get("GITHUB_TOKEN", "")
        if github_token:
            reg_user = registry.split("/")[1] if "/" in registry else "github"
            reg_token = github_token
            info("Using GITHUB_TOKEN for ghcr.io authentication.")
    if registry and not reg_user:
        reg_user = prompt("registry_user",
            "Registry username (or press Enter to use container login)", default="")
    if registry and reg_user and not reg_token:
        reg_token = prompt_secret("Registry token", env_var="REGISTRY_TOKEN")

    # Auto-detect container runtime (not prompted)
    container_rt = _detect_container_runtime()

    # Orchestrator image — only required for --remote
    _orch_default = (
        defaults.get("orchestrator_image", "")
        or "ghcr.io/inference-sim/sim2real/orchestrator:latest"
    )
    orchestrator_image = args.orchestrator_image or prompt(
        "orchestrator_image",
        "Orchestrator image for --remote mode (press Enter to accept default)",
        default=_orch_default,
        env_var="ORCHESTRATOR_IMAGE",
    )

    cfg = SetupConfig(
        namespace=namespace, namespaces=namespaces, registry=registry, repo_name=repo_name,
        run_name=run_name, hf_token=hf_token, github_token=gh_token,
        registry_user=reg_user, registry_token=reg_token,
        storage_class=storage_class, is_openshift=is_openshift,
        no_cluster=args.no_cluster,
        pipeline_yaml=args.pipeline_yaml,
        orchestrator_image=orchestrator_image,
    )
    ns_display = ",".join(namespaces) if len(namespaces) > 1 else namespace
    ok(f"Configuration complete (namespace={ns_display}, registry={registry or '(none)'})")
    return cfg, run_dir, container_rt

# ── Step 2: Namespace ────────────────────────────────────────────────

def step_namespace(cfg: SetupConfig) -> None:
    """Step 2: Create/verify namespace, set kubectl context."""
    step(2, 8, "Namespace")
    if cfg.no_cluster:
        ok(f"Namespace {cfg.namespace} (skipped — --no-cluster)"); return

    check_cluster_reachable()
    exists = run(["kubectl", "get", "ns", cfg.namespace],
                 check=False, capture=True).returncode == 0
    if exists:
        ok(f"Namespace {cfg.namespace} already exists")
    else:
        if cfg.is_openshift:
            proc = run(["oc", "new-project", cfg.namespace], check=False, capture=True)
            if proc.returncode != 0 and "AlreadyExists" not in (proc.stderr or ""):
                proc.check_returncode()
        else:
            run(["kubectl", "create", "ns", cfg.namespace])
        ok(f"Namespace {cfg.namespace} ready")

    result = run(["kubectl", "config", "set-context", "--current",
                  f"--namespace={cfg.namespace}"], check=False, capture=True)
    if result.returncode == 0:
        ok(f"kubectl context set to namespace {cfg.namespace}")
    else:
        warn("Could not set kubectl context namespace")

# ── Step 3: RBAC ─────────────────────────────────────────────────────

def step_rbac(cfg: SetupConfig) -> None:
    """Step 3: Apply RBAC roles and OpenShift SCC policies."""
    step(3, 8, "RBAC")
    if cfg.no_cluster:
        ok("RBAC (skipped — --no-cluster)"); return

    primary_ns = cfg.namespaces[0]
    env = {**os.environ, "NAMESPACE": cfg.namespace, "PRIMARY_NAMESPACE": primary_ns}
    for yaml_path in [
        TEKTONC_DIR / "tekton" / "roles.yaml",
        TEKTONC_DIR / "tekton" / "rbac" / "sim2real-runner.yaml",
        REPO_ROOT / "pipeline" / "rbac" / "sim2real-runner-cluster.yaml",
        REPO_ROOT / "pipeline" / "rbac" / "sim2real-runner-ns.yaml",
    ]:
        if not yaml_path.exists():
            err(f"{yaml_path.name} not found at {yaml_path} — did submodule init fail?"); sys.exit(1)
        subst = subprocess.run(
            ["envsubst", "$NAMESPACE $PRIMARY_NAMESPACE"],
            input=yaml_path.read_text(), capture_output=True, text=True, env=env, check=True,
        )
        run(["kubectl", "apply", "-f", "-"], input=subst.stdout)

    if cfg.is_openshift:
        warn("OpenShift: adding SCC policies")
        for policy_args in [
            ["add-scc-to-user",          "anyuid",       "-z", "default",        "-n", cfg.namespace],
            ["add-scc-to-user",          "anyuid",       "-z", "helm-installer", "-n", cfg.namespace],
            ["add-scc-to-user",          "privileged",   "-z", "helm-installer", "-n", cfg.namespace],
            ["add-cluster-role-to-user", "cluster-admin", "-z", "helm-installer"],
        ]:
            run(["oc", "adm", "policy"] + policy_args, check=False)
    ok("RBAC roles applied")

# ── Step 4: Secrets ──────────────────────────────────────────────────

def step_secrets(cfg: SetupConfig, container_rt: str) -> None:
    """Step 4: Create/update hf-secret and registry-secret."""
    step(4, 8, "Secrets")
    if cfg.no_cluster:
        ok("Secrets (skipped — --no-cluster)"); return

    hf_secret_name = _DEFAULT_HF_SECRET_NAME

    if not cfg.hf_token and secret_exists(hf_secret_name, cfg.namespace):
        ok(f"{hf_secret_name} already exists (reusing)")
    elif not cfg.hf_token:
        err(f"HF_TOKEN is required and {hf_secret_name} does not exist in namespace"); sys.exit(1)
    else:
        yaml_out = run(
            ["kubectl", "create", "secret", "generic", hf_secret_name,
             f"--namespace={cfg.namespace}",
             f"--from-literal=HF_TOKEN={cfg.hf_token}",
             "--dry-run=client", "-o", "yaml"],
            capture=True,
        ).stdout
        run(["kubectl", "apply", "-f", "-"], input=yaml_out)
        ok(f"{hf_secret_name} created/updated")

    # github-token — used by install-llmdbenchmark to clone private repos
    github_token = cfg.github_token
    if not github_token and secret_exists("github-token", cfg.namespace):
        ok("github-token already exists (reusing)")
    elif not github_token:
        warn("GITHUB_TOKEN not set and github-token secret does not exist — "
             "install-llmdbenchmark will fail if repo requires auth")
    else:
        yaml_out = run(
            ["kubectl", "create", "secret", "generic", "github-token",
             f"--namespace={cfg.namespace}",
             f"--from-literal=token={github_token}",
             "--dry-run=client", "-o", "yaml"],
            capture=True,
        ).stdout
        run(["kubectl", "apply", "-f", "-"], input=yaml_out)
        ok("github-token created/updated")

    # registry-secret
    if not cfg.registry:
        warn("No registry — skipping registry-secret"); return

    docker_server = cfg.registry.split("/")[0]
    if cfg.registry_user and cfg.registry_token:
        yaml_out = run(
            ["kubectl", "create", "secret", "docker-registry", "registry-secret",
             f"--namespace={cfg.namespace}",
             f"--docker-server={docker_server}",
             f"--docker-username={cfg.registry_user}",
             f"--docker-password={cfg.registry_token}",
             "--dry-run=client", "-o", "yaml"],
            capture=True,
        ).stdout
        run(["kubectl", "apply", "-f", "-"], input=yaml_out)
        ok("registry-secret created/updated")
    else:
        if not container_rt:
            warn("No container runtime and no registry credentials — skipping registry-secret")
            return
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
             f"--namespace={cfg.namespace}",
             f"--from-file=.dockerconfigjson={config_path}",
             "--dry-run=client", "-o", "yaml"],
            capture=True,
        ).stdout
        run(["kubectl", "apply", "-f", "-"], input=yaml_out)
        ok("registry-secret created/updated from container login")

# ── Step 5: Registry Credential Test ─────────────────────────────────

def _do_test_push(container_rt: str, full_image: str, docker_server: str,
                  reg_user: str, reg_token: str) -> bool:
    """Execute pull-tag-push-pull sequence. Returns True on success."""
    base_image = "busybox:latest"

    info(f"Pulling {base_image}...")
    if run([container_rt, "pull", base_image], check=False, capture=True).returncode != 0:
        err(f"Could not pull {base_image}")
        return False

    if run([container_rt, "tag", base_image, full_image], check=False, capture=True).returncode != 0:
        err(f"Could not tag {base_image} as {full_image}")
        return False

    if reg_user and reg_token:
        run([container_rt, "login", docker_server,
             "--username", reg_user, "--password-stdin"],
            input=reg_token, check=False, capture=True)

    info(f"Pushing {full_image}...")
    push_ok = run([container_rt, "push", full_image], check=False, capture=True).returncode == 0

    # Clean up local test tag
    run([container_rt, "rmi", full_image], check=False, capture=True)

    if not push_ok:
        return False

    # Pull-back verification
    info("Verifying pull-back...")
    pull_ok = run([container_rt, "pull", full_image], check=False, capture=True).returncode == 0
    run([container_rt, "rmi", full_image], check=False, capture=True)

    if pull_ok:
        ok(f"Registry credentials verified (push + pull) → {full_image}")
    else:
        ok(f"Push succeeded → {full_image}")
        warn("Pull-back failed — may be a propagation delay. Credentials likely OK.")
    return True


def step_test_push(cfg: SetupConfig, container_rt: str, test_push_tag: str,
                   auto_push: bool) -> None:
    """Step 5: Optional registry credential test with retry on failure."""
    step(5, 8, "Registry Credential Test")
    if cfg.no_cluster:
        ok("Registry test (skipped — --no-cluster)"); return
    if not cfg.registry:
        ok("Registry test (skipped — no registry configured)"); return
    if not container_rt:
        info("No podman/docker found — skipping test push.")
        info("Credentials will be verified during in-cluster EPP build.")
        return

    # Prompt to test (--test-push auto-accepts)
    if not auto_push:
        full_image = f"{cfg.registry}/{cfg.repo_name}:{test_push_tag}"
        answer = prompt("test_push",
            f"Push test image to {full_image}? [y]es / [s]kip", default="s")
        if answer.strip().lower() not in ("y", "yes"):
            info("Skipped registry credential test"); return

    full_image = f"{cfg.registry}/{cfg.repo_name}:{test_push_tag}"
    docker_server = cfg.registry.split("/")[0]
    interactive = sys.stdout.isatty()

    while True:
        success = _do_test_push(container_rt, full_image, docker_server,
                                cfg.registry_user, cfg.registry_token)
        if success:
            return

        err("Registry push failed")
        if not interactive:
            sys.exit(1)

        answer = prompt("retry", "[r]etry / [s]kip / [q]uit", default="q")
        choice = answer.strip().lower()
        if choice in ("r", "retry"):
            continue
        elif choice in ("s", "skip"):
            warn("Registry credentials unverified — will be tested during in-cluster EPP build")
            return
        else:
            sys.exit(1)

# ── Step 6: PVCs ─────────────────────────────────────────────────────

def step_pvcs(cfg: SetupConfig) -> None:
    """Step 6: Create PVCs for data and source."""
    step(6, 8, "PVCs")
    pvcs = [("data-pvc", "50Gi"), ("source-pvc", "50Gi")]
    sc_line = f"  storageClassName: {cfg.storage_class}" if cfg.storage_class else ""

    for name, size in pvcs:
        if cfg.no_cluster:
            ok(f"PVC {name} (skipped — --no-cluster)"); continue
        if run(["kubectl", "get", "pvc", name, f"-n={cfg.namespace}"],
               check=False, capture=True).returncode == 0:
            ok(f"PVC {name} already exists"); continue
        manifest = (
            f"apiVersion: v1\nkind: PersistentVolumeClaim\n"
            f"metadata:\n  name: {name}\n  namespace: {cfg.namespace}\n"
            f"spec:\n{sc_line}\n  accessModes:\n    - ReadWriteMany\n"
            f"  resources:\n    requests:\n      storage: {size}\n"
        )
        run(["kubectl", "apply", "-f", "-"], input=manifest)
        ok(f"Created PVC {name} ({size})")

    if not cfg.no_cluster:
        info("Waiting for PVCs to bind (timeout 120s)...")
        for name, _ in pvcs:
            result = run(
                ["kubectl", "wait", "--for=jsonpath={.status.phase}=Bound",
                 f"pvc/{name}", f"-n={cfg.namespace}", "--timeout=120s"],
                check=False, capture=True,
            )
            if result.returncode == 0:
                ok(f"PVC {name} is Bound")
            else:
                warn(f"PVC {name} not yet Bound — check storageClass: "
                     f"{cfg.storage_class or '(default)'}")

# ── Step 7: Tekton ───────────────────────────────────────────────────

def step_tekton(cfg: SetupConfig) -> None:
    """Step 7: Verify Tekton operator and deploy steps/tasks + Pipeline."""
    step(7, 8, "Tekton")
    if cfg.no_cluster:
        ok("Tekton (skipped — --no-cluster)"); return

    # Soft verification — warn only
    result = run(["kubectl", "get", "pods", "-n", "tekton-pipelines", "--no-headers"],
                 check=False, capture=True)
    if result.returncode == 0 and "Running" in result.stdout:
        ok("Tekton operator is running")
    else:
        warn("Tekton operator not detected — install: "
             "https://tekton.dev/docs/installation/pipelines/")

    # Deploy steps and tasks
    for subdir in ["steps", "tasks"]:
        tekton_dir = TEKTONC_DIR / "tekton" / subdir
        if not tekton_dir.exists():
            warn(f"{tekton_dir} not found — skipping"); continue
        for yaml_file in sorted(tekton_dir.glob("*.yaml")):
            run(["kubectl", "apply", "-f", str(yaml_file), f"-n={cfg.namespace}"])
    ok("Tekton steps and tasks deployed")

    # Deploy Pipeline YAML
    pipeline_path = Path(cfg.pipeline_yaml) if cfg.pipeline_yaml else REPO_ROOT / "pipeline" / "pipeline.yaml"
    if not pipeline_path.exists():
        if cfg.pipeline_yaml:
            warn(f"Custom pipeline YAML not found at {pipeline_path} — skipping")
        else:
            err(f"Pipeline YAML not found at {pipeline_path}")
            sys.exit(1)
    else:
        r = run(["kubectl", "apply", "-f", str(pipeline_path), f"-n={cfg.namespace}"],
                check=False, capture=True)
        if r.returncode == 0:
            ok(f"Pipeline applied from {pipeline_path}")
        else:
            warn(f"Failed to apply Pipeline to {cfg.namespace}: {r.stderr.strip()}")

# ── Step 8: Config Output ────────────────────────────────────────────

def step_config_output(cfg: SetupConfig, run_dir: Path, container_rt: str) -> None:
    """Step 8: Write setup_config.json and run_metadata.json."""
    step(8, 8, "Config")

    workspace = EXPERIMENT_ROOT / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    commit = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip() or "unknown"

    # setup_config.json
    setup_config = {
        "namespace": cfg.namespace,
        "namespaces": cfg.namespaces,
        "registry": cfg.registry,
        "repo_name": cfg.repo_name,
        "storage_class": cfg.storage_class,
        "is_openshift": cfg.is_openshift,
        "tektonc_dir": str(TEKTONC_DIR),
        "sim2real_root": str(REPO_ROOT),
        "pipeline_yaml": cfg.pipeline_yaml,
        "orchestrator_image": cfg.orchestrator_image,
        "container_runtime": container_rt,
        "current_run": cfg.run_name,
        "setup_timestamp": now_iso,
        "hf_secret_name": _DEFAULT_HF_SECRET_NAME,
        "workspaces": {
            "data-storage":   {"persistentVolumeClaim": {"claimName": "data-pvc"}},
            "source":         {"persistentVolumeClaim": {"claimName": "source-pvc"}},
        },
    }
    setup_path = workspace / "setup_config.json"
    setup_path.write_text(json.dumps(setup_config, indent=2))
    ok(f"Setup config → {setup_path}")

    # run_metadata.json (with version: 1 per spec)
    metadata = {
        "version": 1,
        "namespace": cfg.namespace,
        "registry": cfg.registry,
        "repo_name": cfg.repo_name,
        "storage_class": cfg.storage_class,
        "is_openshift": cfg.is_openshift,
        "container_runtime": container_rt,
        "created_at": now_iso,
        "pipeline_commit": commit,
        **({"component_image": f"{cfg.registry}/{cfg.repo_name}:{cfg.run_name}"} if cfg.registry else {}),
        "stages": {
            "setup":   {"status": "completed", "completed_at": now_iso,
                        "summary": f"Namespace {cfg.namespace} configured, "
                                   f"PVCs created, Tekton tasks deployed"},
            "prepare": {"status": "pending"},
            "deploy":  {"status": "pending"},
            "results": {"status": "pending"},
        },
    }
    meta_path = run_dir / "run_metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2))
    ok(f"Run metadata → {meta_path}")


# ── main ─────────────────────────────────────────────────────────────

def main() -> int:
    args = build_parser().parse_args()

    global EXPERIMENT_ROOT
    EXPERIMENT_ROOT = Path(args.experiment_root).resolve() if getattr(args, "experiment_root", None) else Path.cwd()

    # --redeploy-tasks shortcut
    if args.redeploy_tasks:
        if not args.namespace:
            err("--namespace is required with --redeploy-tasks"); return 1
        # Minimal step 7 only
        step(7, 8, "Tekton (redeploy)")
        for subdir in ["steps", "tasks"]:
            tekton_dir = TEKTONC_DIR / "tekton" / subdir
            if not tekton_dir.exists():
                warn(f"{tekton_dir} not found — skipping"); continue
            for yaml_file in sorted(tekton_dir.glob("*.yaml")):
                run(["kubectl", "apply", "-f", str(yaml_file), f"-n={args.namespace}"])
        ok("Tekton steps and tasks redeployed")
        # Also redeploy Pipeline YAML
        pipeline_path = Path(args.pipeline_yaml) if args.pipeline_yaml else REPO_ROOT / "pipeline" / "pipeline.yaml"
        if not pipeline_path.exists():
            if args.pipeline_yaml:
                warn(f"Custom pipeline YAML not found at {pipeline_path} — skipping")
            else:
                err(f"Pipeline YAML not found at {pipeline_path}")
                return 1
        else:
            r = run(["kubectl", "apply", "-f", str(pipeline_path), f"-n={args.namespace}"],
                    check=False, capture=True)
            if r.returncode == 0:
                ok(f"Pipeline redeployed from {pipeline_path}")
            else:
                err(f"Failed to apply Pipeline: {r.stderr.strip()}")
                return 1
        return 0

    # 8-step flow
    cfg, run_dir, container_rt = collect_config(args)

    step_test_push(cfg, container_rt, args.test_push_tag, args.test_push)
    for ns in cfg.namespaces:
        ns_cfg = SetupConfig(
            namespace=ns,
            namespaces=cfg.namespaces,
            registry=cfg.registry,
            repo_name=cfg.repo_name,
            run_name=cfg.run_name,
            hf_token=cfg.hf_token,
            github_token=cfg.github_token,
            registry_user=cfg.registry_user,
            registry_token=cfg.registry_token,
            storage_class=cfg.storage_class,
            is_openshift=cfg.is_openshift,
            no_cluster=cfg.no_cluster,
            pipeline_yaml=cfg.pipeline_yaml,
            orchestrator_image=cfg.orchestrator_image,
        )
        if len(cfg.namespaces) > 1:
            print(_c("36", f"\n  ── Provisioning namespace: {ns} ──"))
        step_namespace(ns_cfg)
        step_rbac(ns_cfg)
        step_secrets(ns_cfg, container_rt)
        step_pvcs(ns_cfg)
        step_tekton(ns_cfg)
    step_config_output(cfg, run_dir, container_rt)

    # Completion
    print()
    print(_c("32", "━━━ Setup complete ━━━"))
    print()
    cfg_path = EXPERIMENT_ROOT / "workspace" / "setup_config.json"
    print(f"Setup config:  {cfg_path}")
    print(f"Run name:      {cfg.run_name}")
    print(f"Run directory: {run_dir}")
    print()
    print("Next steps:")
    print("  1. Edit <experiment-root>/transfer.yaml (algorithm source, workloads, context)")
    print("  2. Run: python <repo-root>/pipeline/prepare.py")
    return 0

if __name__ == "__main__":
    sys.exit(main())
