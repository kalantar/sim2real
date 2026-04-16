#!/usr/bin/env python3
"""sim2real deploy — Build EPP, submit packages, collect results.

Fire-and-forget: builds EPP image, applies PipelineRuns per package, exits.
Use `deploy.py collect` to pull results for completed packages.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import yaml

from pathlib import Path

# Ensure repo root is on sys.path when run as a script (python pipeline/deploy.py)
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.lib.manifest import load_manifest, ManifestError
from pipeline.lib.state_machine import StateMachine
from pipeline.lib.values import merge_values

# ── Repo layout ──────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Color helpers ────────────────────────────────────────────────────────────
_tty = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text


def info(msg: str)  -> None: print(_c("34", "[INFO]  ") + msg)
def ok(msg: str)    -> None: print(_c("32", "[OK]    ") + msg)
def warn(msg: str)  -> None: print(_c("33", "[WARN]  ") + msg)
def err(msg: str)   -> None: print(_c("31", "[ERROR] ") + msg, file=sys.stderr)


def step(n, title: str) -> None:
    print("\n" + _c("36", f"━━━ Step {n}: {title} ━━━"))


# ── Subprocess helper ────────────────────────────────────────────────────────

def run(cmd: list[str], *, check: bool = True, capture: bool = False,
        cwd: "Path | None" = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=capture, cwd=cwd)


# ── Package discovery ────────────────────────────────────────────────────────

def _discover_packages(cluster_dir: Path) -> list[str]:
    """A package is any subdirectory of cluster/ containing pipelinerun-*.yaml."""
    if not cluster_dir.exists():
        return []
    return sorted(
        d.name for d in cluster_dir.iterdir()
        if d.is_dir() and any(d.glob("pipelinerun-*.yaml"))
    )


# ── Setup config ─────────────────────────────────────────────────────────────

def _load_setup_config() -> dict:
    path = REPO_ROOT / "workspace" / "setup_config.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


# ── EPP Image build ─────────────────────────────────────────────────────────

def _build_epp_image(run_dir: Path, run_name: str, namespace: str) -> str:
    """Build EPP image on-cluster. Returns the full image reference."""
    step(1, "Build EPP Image")

    # Locate build-epp.sh
    build_script = REPO_ROOT / "pipeline" / "scripts" / "build-epp.sh"
    if not build_script.exists():
        err(f"build-epp.sh not found at {build_script.relative_to(REPO_ROOT)} — ensure pipeline/scripts/build-epp.sh is present in the repo")
        sys.exit(1)

    result = run(
        ["bash", str(build_script),
         "--run-dir", str(run_dir),
         "--run-name", run_name,
         "--namespace", namespace],
        check=False,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        err("EPP build failed — see output above")
        sys.exit(1)

    # Read image reference from run_metadata.json
    meta_path = run_dir / "run_metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        full_image = meta.get("epp_image", "")
        if full_image:
            ok(f"EPP image: {full_image}")
            return full_image

    err("EPP build completed but epp_image not set in run_metadata.json")
    sys.exit(1)


def _inject_image_into_values(run_dir: Path, full_image: str, scenario: str):
    """Re-merge values with the EPP image reference injected."""
    step("1a", "Injecting EPP image into values")

    alg_values_path = run_dir / "algorithm_values.yaml"
    if not alg_values_path.exists():
        warn("algorithm_values.yaml not found, skipping image injection")
        return

    alg_values = yaml.safe_load(alg_values_path.read_text())

    # Read hub+name from env_defaults
    env_path = REPO_ROOT / "config" / "env_defaults.yaml"
    env_data = yaml.safe_load(env_path.read_text())
    common = env_data.get("common", {})
    build_cfg = common.get("stack", {}).get("gaie", {}).get("epp_image", {}).get("build", {})
    epp_hub = build_cfg.get("hub", "")
    epp_name = build_cfg.get("name", "")
    epp_tag = full_image.rsplit(":", 1)[1] if ":" in full_image else ""

    # Inject into algorithm_values
    (alg_values
        .setdefault("stack", {})
        .setdefault("gaie", {})
        .setdefault("treatment", {})
        .setdefault("helmValues", {})
        .setdefault("inferenceExtension", {})
        ["image"]) = {"hub": epp_hub, "name": epp_name, "tag": epp_tag}
    alg_values_path.write_text(yaml.dump(alg_values, default_flow_style=False, sort_keys=False))
    ok("algorithm_values.yaml updated with EPP image")

    # Re-merge values
    values_path = run_dir / "values.yaml"
    try:
        merge_values(env_path, alg_values_path, values_path, scenario=scenario)
    except (FileNotFoundError, yaml.YAMLError, ValueError, OSError) as e:
        err(f"merge-values failed: {e}")
        sys.exit(1)
    ok("values.yaml re-merged with EPP image")


# ── PipelineRun helpers ──────────────────────────────────────────────────────

def _cancel_and_delete_pipelinerun(pr_name: str, namespace: str) -> None:
    """If a PipelineRun with the given name exists, cancel it, wait for it to
    finish cancelling, then delete it so a fresh one can be submitted."""
    exists = run(
        ["kubectl", "get", "pipelinerun", pr_name, "-n", namespace],
        check=False, capture=True,
    )
    if exists.returncode != 0:
        return  # doesn't exist, nothing to cancel

    status = _check_pipelinerun_status(pr_name, namespace)
    info(f"Existing PipelineRun {pr_name!r} found (status: {status}); cancelling …")

    if status in ("Running", "Started"):
        run(
            ["kubectl", "patch", "pipelinerun", pr_name,
             "--type=merge", "-p", '{"spec":{"status":"CancelledRunFinally"}}',
             "-n", namespace],
            check=False, capture=True,
        )
        for _ in range(40):  # wait up to 120 s
            time.sleep(3)
            current = _check_pipelinerun_status(pr_name, namespace)
            if current not in ("Running", "Started"):
                info(f"PipelineRun {pr_name!r} cancelled (now: {current})")
                break
        else:
            warn(f"PipelineRun {pr_name!r} did not cancel within 120 s; deleting anyway")

    run(
        ["kubectl", "delete", "pipelinerun", pr_name, "-n", namespace,
         "--ignore-not-found"],
        check=False, capture=True,
    )


# ── Deploy command ───────────────────────────────────────────────────────────

def _print_dry_run(packages: list[str], cluster_dir: Path):
    """Print what would be applied without actually doing it."""
    print("\n  DRY RUN — would apply:\n")
    for pkg in sorted(packages):
        pipeline_yaml = cluster_dir / pkg / f"{pkg}-pipeline.yaml"
        if pipeline_yaml.exists() and not pipeline_yaml.read_text().startswith("#"):
            print(f"    kubectl apply -f {pipeline_yaml.relative_to(REPO_ROOT)}")
        for pr_path in sorted((cluster_dir / pkg).glob("pipelinerun-*.yaml")):
            print(f"    kubectl apply -f {pr_path.relative_to(REPO_ROOT)}")
    print()


def _print_status(submitted: dict[str, str], namespace: str):
    """Print submitted PipelineRuns and how to check status."""
    print(f"\n  Submitted {len(submitted)} package(s) to namespace '{namespace}':")
    for pkg, pr_name in sorted(submitted.items()):
        print(f"    {pkg:20s} → {pr_name}")
    print(f"\n  Check status:  kubectl get pipelineruns -n {namespace}")
    print("  Collect:       python pipeline/deploy.py collect")
    print()


def _cmd_deploy(args, manifest: dict, run_dir: Path, setup_config: dict):
    """Build EPP + submit packages, then exit."""
    # Pre-flight: check state
    try:
        state = StateMachine.load(run_dir)
    except FileNotFoundError:
        err("No state file. Run prepare.py first.")
        sys.exit(1)

    if not state.is_done("gate"):
        err("Cannot deploy: prepare not complete (gate not passed).")
        sys.exit(1)

    verdict = state.get_phase("gate").get("verdict", "")
    if verdict != "READY TO DEPLOY":
        err(f"Cannot deploy: gate verdict is '{verdict}', expected 'READY TO DEPLOY'.")
        sys.exit(1)

    namespace = os.environ.get("NAMESPACE", setup_config.get("namespace", ""))
    if not namespace:
        err("No namespace configured. Run setup.py or set NAMESPACE env var.")
        sys.exit(1)

    # Discover packages
    cluster_dir = run_dir / "cluster"
    packages = _discover_packages(cluster_dir)
    if not packages:
        err("No packages found in cluster/. Run prepare.py first.")
        sys.exit(1)

    if args.package:
        selected = [p for p in packages if p in args.package]
        missing = set(args.package) - set(packages)
        if missing:
            err(f"Packages not found: {missing}. Available: {packages}")
            sys.exit(1)
        packages = selected
    elif "experiment" in packages:
        packages = ["experiment"]

    if args.dry_run:
        _print_dry_run(packages, cluster_dir)
        return

    # Build EPP image
    if not args.skip_build_epp:
        full_image = _build_epp_image(run_dir, state.run_name, namespace)
        _inject_image_into_values(run_dir, full_image, manifest["scenario"])
    else:
        meta_path = run_dir / "run_metadata.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            full_image = meta.get("epp_image", "")
            if full_image:
                info(f"Skipping EPP build. Using image: {full_image}")
            else:
                warn("--skip-build-epp set but no epp_image in run_metadata.json")
        else:
            warn("--skip-build-epp set, no run_metadata.json found")

    # Apply Pipeline definitions first so PipelineRuns can reference them
    step(2, "Apply Pipeline Definitions")
    for pkg in sorted(packages):
        # Apply all Pipeline YAMLs in the package dir (both raw compiled and sequential wrapper)
        pipeline_yamls = [
            cluster_dir / pkg / f"{pkg}-pipeline.yaml",
            cluster_dir / pkg / f"sim2real-{pkg}-pipeline.yaml",
        ]
        applied_any = False
        for pipeline_yaml in pipeline_yamls:
            if pipeline_yaml.exists() and not pipeline_yaml.read_text().startswith("#"):
                result = run(["kubectl", "apply", "-f", str(pipeline_yaml), "-n", namespace],
                             check=False, capture=True)
                if result.returncode != 0:
                    err(f"kubectl apply failed for {pipeline_yaml.name}: {result.stderr}")
                    sys.exit(1)
                ok(f"Applied: {pipeline_yaml.name}")
                applied_any = True
        if not applied_any:
            warn(f"No Pipeline definition for {pkg} — PipelineRuns may fail")

    # Submit packages
    step(3, "Submit PipelineRuns")
    submitted = {}
    for pkg in sorted(packages):
        for pr_path in sorted((cluster_dir / pkg).glob("pipelinerun-*.yaml")):
            pr_data = yaml.safe_load(pr_path.read_text())
            pr_name = pr_data.get("metadata", {}).get("name", pr_path.stem)

            _cancel_and_delete_pipelinerun(pr_name, namespace)

            result = run(["kubectl", "apply", "-f", str(pr_path), "-n", namespace],
                         check=False, capture=True)
            if result.returncode != 0:
                err(f"kubectl apply failed for {pr_path.name}: {result.stderr}")
                continue
            key = f"{pkg}/{pr_path.stem}"
            submitted[key] = pr_name
            ok(f"Submitted: {key} → {pr_name}")

    _print_status(submitted, namespace)


# ── Collect command ──────────────────────────────────────────────────────────

def _check_pipelinerun_status(pr_name: str, namespace: str) -> str:
    """Return PipelineRun reason string, or 'Unknown' if not found."""
    result = run([
        "kubectl", "get", "pipelinerun", pr_name,
        "-n", namespace,
        "-o", "jsonpath={.status.conditions[0].reason}",
    ], check=False, capture=True)
    if result.returncode != 0:
        return "Unknown"
    return result.stdout.strip() or "Unknown"



def _probe_phase_sizes(pod_name: str, run_name: str, phases: list[str],
                       namespace: str) -> dict[str, int]:
    """Return byte sizes for each phase directory on the PVC."""
    sizes: dict[str, int] = {}
    for phase in phases:
        result = run(
            ["kubectl", "exec", pod_name, f"-n={namespace}", "--",
             "du", "-sb", f"/data/{run_name}/{phase}"],
            check=False, capture=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            sizes[phase] = int(result.stdout.strip().split()[0])
        else:
            sizes[phase] = 0
    return sizes


def _fmt_size(b: int) -> str:
    if b >= 1 << 30:
        return f"{b / (1 << 30):.1f} GB"
    if b >= 1 << 20:
        return f"{b / (1 << 20):.0f} MB"
    return f"{b / (1 << 10):.0f} KB"


def _extract_phases_from_pvc(phases: list[str], run_name: str, namespace: str,
                              run_dir: Path,
                              skip_logs: bool = False) -> dict[str, "Exception | None"]:
    """Extract results for one or more phases from data-pvc using a single pod.

    Data layout on PVC (written by run-workload-blis-observe):
      /data/{runName}/{phase}/{workloadName}/trace_header.yaml
      /data/{runName}/{phase}/{workloadName}/trace_data.csv

    When *skip_logs* is True, only trace files are copied (skipping vLLM and
    EPP log files which typically account for the bulk of the data).

    Returns a dict mapping phase -> None (success) or Exception (failure).
    """
    pod_name = "sim2real-extract"

    # Clean up any leftover pod from a prior failed attempt
    run(["kubectl", "delete", "pod", pod_name, "-n", namespace,
         "--ignore-not-found", "--force", "--grace-period=0"],
        check=False, capture=True)

    overrides = json.dumps({
        "spec": {
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data-pvc"}}],
            "containers": [{
                "name": "e", "image": "alpine:3.19",
                "command": ["sleep", "3600"],
                "volumeMounts": [{"name": "data", "mountPath": "/data"}],
            }],
            "restartPolicy": "Never",
        }
    })
    run(["kubectl", "run", pod_name, "--image=alpine:3.19", "--restart=Never",
         "--overrides", overrides, "-n", namespace])

    result = run(
        ["kubectl", "wait", f"pod/{pod_name}", "--for=condition=Ready",
         "--timeout=60s", f"-n={namespace}"],
        check=False, capture=True,
    )
    if result.returncode != 0:
        run(["kubectl", "delete", "pod", pod_name, "-n", namespace,
             "--ignore-not-found", "--force", "--grace-period=0"],
            check=False, capture=True)
        raise RuntimeError(f"Extractor pod {pod_name} not ready: {result.stderr.strip()}")

    errors: dict[str, "Exception | None"] = {}
    try:
        # ── Size probe ──────────────────────────────────────────────────
        sizes = _probe_phase_sizes(pod_name, run_name, phases, namespace)
        total = sum(sizes.values())

        if total > 1 << 30:  # > 1 GB
            breakdown = ", ".join(f"{p}: {_fmt_size(s)}" for p, s in sizes.items())
            warn(f"Total data size: {_fmt_size(total)} ({breakdown})")
            if not skip_logs:
                print("        Logs make up most of the size. "
                      "Re-run with --skip-logs to collect traces only.")
                answer = input("        Continue with full download? [y/N] ").strip().lower()
                if answer != "y":
                    info("Aborted. Re-run with --skip-logs to collect traces only.")
                    return errors
            else:
                info("--skip-logs: collecting traces only")

        # ── Copy ────────────────────────────────────────────────────────
        for phase in phases:
            dest_dir = run_dir / "results" / phase
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            dest_dir.mkdir(parents=True)

            if skip_logs:
                # Selective copy: trace files + epp_logs via kubectl cp per
                # workload; skips vLLM server_logs which dominate data volume.
                # BusyBox tar doesn't handle large streaming well, so use cp.
                list_result = run(
                    ["kubectl", "exec", pod_name, f"-n={namespace}", "--",
                     "sh", "-c",
                     f"ls /data/{run_name}/{phase}/"],
                    check=False, capture=True,
                )
                if list_result.returncode != 0:
                    errors[phase] = RuntimeError(
                        f"failed to list workloads: {list_result.stderr.strip()}")
                    continue
                workloads = list_result.stdout.strip().split() if list_result.stdout.strip() else []
                phase_errors = []
                for workload in workloads:
                    wl_dest = dest_dir / workload
                    wl_dest.mkdir(parents=True, exist_ok=True)
                    # Copy trace files
                    for fname in ("trace_data.csv", "trace_header.yaml", "epp_stream_done"):
                        src = f"{namespace}/{pod_name}:/data/{run_name}/{phase}/{workload}/{fname}"
                        r = run(["kubectl", "cp", src, str(wl_dest / fname), "--retries=3"],
                                check=False, capture=True)
                        if r.returncode != 0 and "no such file" not in r.stderr.lower():
                            phase_errors.append(f"{workload}/{fname}: {r.stderr.strip()}")
                    # Copy epp_logs directory
                    epp_src = f"{namespace}/{pod_name}:/data/{run_name}/{phase}/{workload}/epp_logs/"
                    epp_dest = wl_dest / "epp_logs"
                    epp_dest.mkdir(exist_ok=True)
                    r = run(["kubectl", "cp", epp_src, str(epp_dest), "--retries=3"],
                            check=False, capture=True)
                    if r.returncode != 0 and "no such file" not in r.stderr.lower():
                        phase_errors.append(f"{workload}/epp_logs: {r.stderr.strip()}")
                if phase_errors:
                    errors[phase] = RuntimeError("; ".join(phase_errors))
                else:
                    errors[phase] = None
            else:
                result = run(
                    ["kubectl", "cp",
                     f"{namespace}/{pod_name}:/data/{run_name}/{phase}/",
                     str(dest_dir), "--retries=3"],
                    check=False, capture=True,
                )
                if result.returncode != 0:
                    errors[phase] = RuntimeError(
                        f"kubectl cp failed: {result.stderr.strip()}")
                else:
                    errors[phase] = None
    finally:
        run(["kubectl", "delete", "pod", pod_name, "-n", namespace,
             "--ignore-not-found", "--force", "--grace-period=0"],
            check=False, capture=True)

    return errors


def _cmd_collect(args, manifest: dict, run_dir: Path, setup_config: dict):
    """Pull results from cluster for completed phases."""
    namespace = os.environ.get("NAMESPACE", setup_config.get("namespace", ""))
    if not namespace:
        err("No namespace configured.")
        sys.exit(1)

    run_name = run_dir.name
    cluster_dir = run_dir / "cluster"
    all_packages = _discover_packages(cluster_dir)
    if not all_packages:
        err("No packages found in cluster/.")
        sys.exit(1)

    # Data phases are baseline and treatment (experiment is the sequencing wrapper).
    data_phases = [p for p in ["baseline", "treatment"] if p in all_packages]

    if args.package:
        # Validate requested packages exist; expand "experiment" to its phases.
        unknown = set(args.package) - set(all_packages)
        if unknown:
            err(f"Unknown packages: {sorted(unknown)}. Available: {all_packages}")
            sys.exit(1)
        phases_to_collect: list[str] = []
        for p in args.package:
            if p == "experiment":
                phases_to_collect.extend(data_phases)
            else:
                phases_to_collect.append(p)
        # Deduplicate while preserving order
        seen: set[str] = set()
        phases_to_collect = [p for p in phases_to_collect
                             if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]
    else:
        phases_to_collect = data_phases

    if not phases_to_collect:
        err("No data phases to collect (expected baseline and/or treatment packages).")
        sys.exit(1)

    step(1, "Collecting Results")

    collected: list[str] = []
    failed: list[str] = []

    if phases_to_collect:
        try:
            skip_logs = getattr(args, "skip_logs", False)
            errors = _extract_phases_from_pvc(
                phases_to_collect, run_name, namespace, run_dir,
                skip_logs=skip_logs)
        except RuntimeError as e:
            warn(f"Extractor pod failed: {e}")
            failed.extend(phases_to_collect)
        else:
            for phase, exc in errors.items():
                if exc is None:
                    ok(f"Collected: {phase}")
                    collected.append(phase)
                else:
                    warn(f"Extraction failed for {phase}: {exc}")
                    failed.append(phase)

    # Print summary
    print(f"\n  Collected: {len(collected)}/{len(phases_to_collect)} phases")
    if failed:
        print(f"  Failed:    {', '.join(failed)}")
    if collected:
        dirs = "  ".join(f"results/{p}/" for p in collected)
        print(f"  Results:   {run_dir.relative_to(REPO_ROOT)}/{dirs}")
        print("\n  Next:      /sim2real-analyze")
    print()


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deploy.py",
        description="sim2real deploy — Build EPP, submit packages, collect results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pipeline/deploy.py                     # Build EPP + submit all packages
  python pipeline/deploy.py --dry-run           # Show what would be applied
  python pipeline/deploy.py --skip-build-epp    # Submit packages (EPP already built)
  python pipeline/deploy.py --package treatment # Deploy only treatment package
  python pipeline/deploy.py collect             # Pull results for completed packages
""",
    )
    p.add_argument("--run", metavar="NAME",
                   help="Run name (overrides current_run in setup_config.json)")
    p.add_argument("--manifest", metavar="PATH",
                   help="Path to transfer.yaml (default: config/transfer.yaml)")
    p.add_argument("--skip-build-epp", action="store_true", dest="skip_build_epp",
                   help="Skip EPP image build")
    p.add_argument("--package", nargs="+", metavar="NAME",
                   help="Deploy only these packages")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="Show what would be applied without actually applying")

    sub = p.add_subparsers(dest="command")
    collect_p = sub.add_parser("collect", help="Pull results for completed packages")
    collect_p.add_argument("--package", nargs="+", metavar="NAME",
                           help="Collect only these packages")
    collect_p.add_argument("--skip-logs", action="store_true", dest="skip_logs",
                           help="Skip vLLM and EPP log files, collect only traces")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    print(_c("36", "\n━━━ sim2real-deploy ━━━\n"))

    manifest_path = args.manifest or str(REPO_ROOT / "config" / "transfer.yaml")
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as e:
        err(str(e))
        sys.exit(1)

    setup_config = _load_setup_config()
    run_name = args.run or setup_config.get("current_run", "")
    if not run_name:
        err("No run name. Use --run NAME or set current_run in setup_config.json.")
        sys.exit(1)
    run_dir = REPO_ROOT / "workspace" / "runs" / run_name

    if not run_dir.exists():
        err(f"Run directory not found: {run_dir.relative_to(REPO_ROOT)}")
        sys.exit(1)

    cmd = args.command
    if cmd == "collect":
        _cmd_collect(args, manifest, run_dir, setup_config)
    else:
        _cmd_deploy(args, manifest, run_dir, setup_config)


if __name__ == "__main__":
    main()
