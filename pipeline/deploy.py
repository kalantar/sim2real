#!/usr/bin/env python3
"""sim2real deploy — Ensure images, orchestrate runs, collect results.

Subcommands:
  build    Ensure all scenario images exist (pre-flight for run)
  run      Ensure images + submit PipelineRuns
  status   Show progress of all (workload, package) pairs
  collect  Pull results from cluster for completed phases
  stop     Stop the remote orchestrator Job
  reset    Reset all non-pending pairs to pending (with cluster cleanup)
  wipe     Delete local result files for pairs in scope
  pairs    List available pair keys, workloads, and packages
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


# ── Repo layout ──────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent

# Overridden in main() when --experiment-root is specified.
EXPERIMENT_ROOT = REPO_ROOT


# ── Color helpers ────────────────────────────────────────────────────────────
_tty = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text


def info(msg: str)  -> None: print(_c("34", "[INFO]  ") + msg)
def ok(msg: str)    -> None: print(_c("32", "[OK]    ") + msg)
def warn(msg: str)  -> None: print(_c("33", "[WARN]  ") + msg, file=sys.stderr)
def err(msg: str)   -> None: print(_c("31", "[ERROR] ") + msg, file=sys.stderr)


def _is_pair_key(key: str) -> bool:
    """Return True if key is a real pair entry (not metadata)."""
    return not key.startswith("_")


def step(n, title: str) -> None:
    print("\n" + _c("36", f"━━━ Step {n}: {title} ━━━"))


# ── Subprocess helper ────────────────────────────────────────────────────────

def run(cmd: list[str], *, check: bool = True, capture: bool = False,
        cwd: "Path | None" = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=capture, cwd=cwd)


# ── ConfigMap namespace resolution ──────────────────────────────────────────

def _configmap_namespace(setup_config: dict | None,
                         namespaces: list[str] | None = None) -> str:
    """Return the namespace for the run-scoped progress ConfigMap.

    Checks setup_config["namespace"] first, then falls back to
    namespaces[0] (or setup_config["namespaces"][0] if namespaces
    is not passed).
    """
    cfg = setup_config or {}
    ns = cfg.get("namespace", "")
    if ns:
        return ns
    ns_list = namespaces or cfg.get("namespaces") or []
    if ns_list:
        return ns_list[0]
    return ""


# ── Phase discovery ─────────────────────────────────────────────────────────

def _discover_phases(cluster_dir: "Path") -> list[str]:
    """Discover package phases from pipelinerun-*.yaml filenames in cluster/."""
    phases: set[str] = set()
    for pr_file in cluster_dir.glob("pipelinerun-*.yaml"):
        # Filename pattern: pipelinerun-{workload}-{phase}.yaml
        stem = pr_file.stem
        parts = stem.split("-")
        if len(parts) >= 3:
            phases.add(parts[-1])
    return sorted(phases) if phases else ["baseline", "treatment"]

# ── Setup config ─────────────────────────────────────────────────────────────

def _load_setup_config() -> dict:
    path = EXPERIMENT_ROOT / "workspace" / "setup_config.json"
    if not path.exists():
        path = REPO_ROOT / "workspace" / "setup_config.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


# ── Image build ───────────────────────────────────────────────────────────────

def _cmd_build(run_dir: Path, namespace: str, skip_build: bool) -> str:
    """Ensure all required scenario images exist. Returns 'built', 'skip', or 'current'.

    Iterates over resolved scenarios in cluster/, extracts image refs,
    and builds any that are stale (source hash mismatch).
    """
    from pipeline.lib.ensure_image import (
        collect_scenario_images, compute_source_hash,
        image_needs_build, save_source_hash,
    )

    run_meta_path = run_dir / "run_metadata.json"
    if not run_meta_path.exists():
        err("run_metadata.json not found — run setup.py first.")
        sys.exit(1)
    try:
        run_meta = json.loads(run_meta_path.read_text())
    except json.JSONDecodeError as e:
        err(f"run_metadata.json is not valid JSON: {e}. Re-run setup.py.")
        sys.exit(1)

    component_image = run_meta.get("component_image")
    if component_image is None:
        info("No component_image in run metadata — skipping image build")
        return "skip"
    if not component_image:
        err("component_image is empty in run_metadata.json — re-run setup.py with a valid --registry.")
        sys.exit(1)

    if skip_build:
        info("--skip-build: skipping image build")
        return "skip"

    step(1, "Ensure Images")

    registry = run_meta.get("registry", "")
    if not registry:
        err("registry is empty in run_metadata.json — re-run setup.py with a valid --registry.")
        sys.exit(1)
    repo_name = run_meta.get("repo_name", "llm-d-inference-scheduler")
    run_name = run_dir.name
    source_dir = EXPERIMENT_ROOT / repo_name

    if not source_dir.exists():
        err(f"Component source directory not found: {source_dir}")
        sys.exit(1)

    # Collect all unique image refs from resolved scenarios
    cluster_dir = run_dir / "cluster"
    scenario_images = collect_scenario_images(cluster_dir)

    if not scenario_images:
        if cluster_dir.exists() and any(cluster_dir.glob("*.yaml")):
            warn("cluster/ has scenario files but no images.inferenceScheduler found — "
                 "falling back to treatment image only")
        treatment_ref = f"{registry}/{repo_name}:{run_name}"
        scenario_images = [{"image_ref": treatment_ref, "package": "treatment"}]

    # Determine which images need building
    to_build = []
    for img_info in scenario_images:
        ref = img_info["image_ref"]
        if image_needs_build(run_dir, ref, source_dir):
            to_build.append(img_info)
        else:
            ok(f"Image current (hash unchanged): {ref}")

    if not to_build:
        return "current"

    # Load translation output for source toggle (if available)
    translation_output_path = run_dir / "translation_output.json"
    translation_output = None
    if translation_output_path.exists():
        try:
            translation_output = json.loads(translation_output_path.read_text())
        except json.JSONDecodeError as e:
            err(f"translation_output.json is not valid JSON: {e}. Re-run /sim2real-translate.")
            sys.exit(1)

    build_script = REPO_ROOT / "pipeline" / "scripts" / "build-epp.sh"
    if not build_script.exists():
        err(f"build-epp.sh not found at {build_script.relative_to(REPO_ROOT)}")
        sys.exit(1)

    # Treatment ref for comparison (to distinguish baseline from treatment builds)
    treatment_ref = f"{registry}/{repo_name}:{run_name}"
    generated_dir = run_dir / "generated"

    built_any = False
    for img_info in to_build:
        ref = img_info["image_ref"]
        is_treatment = (ref == treatment_ref)

        # Source toggle: ensure working tree is in correct state for baseline builds
        if translation_output and not is_treatment:
            from pipeline.lib.source_toggle import restore_baseline
            info(f"Restoring baseline state for: {ref}")
            try:
                restore_baseline(source_dir, translation_output)
            except (subprocess.CalledProcessError, OSError) as exc:
                err(f"Failed to restore baseline state in {source_dir}: {exc}")
                sys.exit(1)

        info(f"Building image: {ref}")
        result = run(
            ["bash", str(build_script),
             "--run-dir", str(run_dir),
             "--run-name", run_name,
             "--namespace", namespace,
             "--image-ref", ref,
             "--source-dir", str(source_dir)],
            check=False,
            cwd=REPO_ROOT,
        )

        # Restore treatment state after baseline build
        if translation_output and not is_treatment:
            from pipeline.lib.source_toggle import restore_treatment
            try:
                restore_treatment(source_dir, generated_dir, translation_output)
            except (OSError, FileNotFoundError) as exc:
                err(f"Failed to restore treatment state in {source_dir}: {exc}\n"
                    f"  Working tree may be in baseline state. To recover:\n"
                    f"  cd {source_dir} && git checkout -- .")
                sys.exit(1)

        if result.returncode != 0:
            err(f"Image build failed for {ref} — see output above")
            sys.exit(1)

        current_hash = compute_source_hash(source_dir)
        save_source_hash(run_dir, ref, current_hash)
        ok(f"Image built and hash recorded: {ref}")
        built_any = True

    return "built" if built_any else "current"


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


def _delete_pipelinerun(pr_name: str, namespace: str) -> None:
    """Delete a completed PipelineRun. Best-effort — warns on failure."""
    result = run(
        ["kubectl", "delete", "pipelinerun", pr_name, "-n", namespace,
         "--ignore-not-found"],
        check=False, capture=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() if result.stderr else ""
        warn(f"Failed to delete PipelineRun {pr_name!r} in {namespace}" +
             (f": {detail}" if detail else ""))


# ── Deploy command ───────────────────────────────────────────────────────────

# ── Status command ───────────────────────────────────────────────────────────

def _cmd_status(args, run_dir: Path,
                setup_config: dict | None = None) -> None:
    """Print a snapshot table of all (workload, package) pair statuses."""
    from pipeline.lib.progress import ConfigMapProgressStore
    primary_ns = _configmap_namespace(setup_config)
    if not primary_ns:
        err("No namespace configured. Run setup.py first.")
        sys.exit(1)
    store = ConfigMapProgressStore(primary_ns, run_name=run_dir.name)
    try:
        progress = store.load()
    except (ValueError, RuntimeError) as exc:
        err(f"Failed to load progress: {exc}")
        progress = {}

    if not progress:
        suffix = " (no progress data)"
        filters_given = any([
            getattr(args, "only", None) is not None,
            getattr(args, "workload", None) is not None,
            getattr(args, "package", None) is not None,
            getattr(args, "status", None) is not None,
        ])
        if filters_given:
            suffix += " — filters ignored"
        print(f"  0 pairs{suffix}")
        return

    pairs = {k: progress[k] for k in _resolve_scope(progress, args)}

    if not pairs:
        print("  0 pairs")
        orch = progress.get("_orchestrator")
        if isinstance(orch, dict) and orch.get("state") not in (None, "normal"):
            print(f"  Orchestrator: {orch.get('state', '?')} (level {orch.get('backoff_level', 0)})")
        print()
        return

    pair_w = max(len(k) for k in pairs) + 2
    col_status = 12
    col_slot = 14
    col_retries = 7

    header = (f"{'PAIR':<{pair_w}} {'STATUS':<{col_status}} {'SLOT':<{col_slot}} {'RETRIES':<{col_retries}}")
    print()
    print(header)
    print("-" * len(header))

    counts: dict[str, int] = {}
    for key, entry in sorted(pairs.items()):
        status = entry.get("status", "unknown")
        slot = entry.get("namespace") or "—"
        retries = entry.get("retries", 0)
        counts[status] = counts.get(status, 0) + 1
        print(f"{key:<{pair_w}} {status:<{col_status}} {slot:<{col_slot}} {retries}")

    print()
    summary_parts = [f"{v} {k}" for k, v in sorted(counts.items())]
    print(f"  {len(pairs)} pairs: " + "  ".join(summary_parts))

    orch = progress.get("_orchestrator")
    if isinstance(orch, dict) and orch.get("state") not in (None, "normal"):
        print(f"  Orchestrator: {orch.get('state', '?')} (level {orch.get('backoff_level', 0)}, "
              f"last probe: {orch.get('last_probe_free_gpus', '?')} free GPUs)")

    print()


# ── Pairs command ────────────────────────────────────────────────────────────

def _cmd_pairs(cluster_dir: Path, *, keys_only: bool = False,
               workloads_only: bool = False, packages_only: bool = False) -> None:
    """List available pair keys, workloads, and packages from cluster/ YAML files."""
    pairs = _load_pairs(cluster_dir)

    if not pairs:
        if keys_only or workloads_only or packages_only:
            return
        n = len(list(cluster_dir.glob("pipelinerun-*.yaml"))) if cluster_dir.exists() else 0
        if n == 0:
            print("  0 pairs (no pipelinerun-*.yaml files found)")
        else:
            print(f"  0 pairs ({n} files found but failed to parse — see warnings above)")
        return

    if keys_only:
        for key in sorted(pairs):
            print(key)
        return

    if workloads_only:
        workloads = sorted({v["workload"] for v in pairs.values() if v["workload"]})
        for w in workloads:
            print(w)
        return

    if packages_only:
        packages = sorted({v["package"] for v in pairs.values() if v["package"]})
        for p in packages:
            print(p)
        return

    # Default: human-readable table
    pair_w = max(len(k) for k in pairs) + 2
    col_wl = max(len(v["workload"]) for v in pairs.values()) + 2
    col_wl = max(col_wl, 10)

    header = f"{'PAIR':<{pair_w}} {'WORKLOAD':<{col_wl}} PACKAGE"
    print()
    print(header)
    print("-" * len(header))
    for key in sorted(pairs):
        entry = pairs[key]
        print(f"{key:<{pair_w}} {entry['workload']:<{col_wl}} {entry['package']}")
    print()
    print(f"  {len(pairs)} pairs")


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



def _handle_pending_pods(*, pr_name: str, namespace: str, entry: dict,
                         pending_threshold: int, max_pending_stalls: int) -> bool:
    """Check for pods stuck in Pending and take action.

    Returns True if the slot was reclaimed (caller should free slot).
    Returns False if no action taken (caller should proceed to timeout check).

    Side effects on *entry* (caller must persist):
      On True (non-recoverable):
        - status: "failed", namespace: None, pending_since: None
      On True (recoverable threshold exceeded):
        - status: "pending" or "stalled", namespace: None
        - pending_stalls: incremented, pending_since: None
      On False (no action):
        - pending_since: set on first recoverable detection, cleared when
          pods start running, reset on malformed timestamp
    """
    import datetime as _dt
    import json as _json
    from pipeline.lib.pod_pending import parse_pod_conditions

    result = run(
        ["kubectl", "get", "pods", f"-n={namespace}",
         "-l", f"tekton.dev/pipelineRun={pr_name}",
         "-o", "json"],
        check=False, capture=True,
    )
    if result.returncode != 0:
        warn(f"[{entry.get('workload', '?')}] pod query failed: {(result.stdout or result.stderr or '')[:120]}")
        return False

    try:
        pods_json = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        warn(f"[{entry.get('workload', '?')}] pod query returned invalid JSON: {result.stdout[:120]}")
        return False

    try:
        category, detail = parse_pod_conditions(pods_json)
    except (KeyError, TypeError, AttributeError) as exc:
        warn(f"[{entry.get('workload', '?')}] unexpected pod JSON shape: {exc}")
        return False

    if category is None:
        if entry.get("pending_since") is not None:
            entry["pending_since"] = None
        return False

    if category == "non_recoverable":
        warn(f"[{entry.get('workload', '?')}] non-recoverable pending: {detail}")
        _cancel_and_delete_pipelinerun(pr_name, namespace)
        entry["status"] = "failed"
        entry["namespace"] = None
        entry["pending_since"] = None
        return True

    # category == "recoverable"
    now = _dt.datetime.now(_dt.timezone.utc)
    if entry.get("pending_since") is None:
        entry["pending_since"] = now.isoformat()
        info(f"[{entry.get('workload', '?')}] pending (recoverable): {detail}")
        return False

    try:
        pending_since = _dt.datetime.fromisoformat(entry["pending_since"])
    except (ValueError, TypeError):
        warn(f"[{entry.get('workload', '?')}] malformed pending_since — resetting timer")
        entry["pending_since"] = now.isoformat()
        return False
    elapsed = (now - pending_since).total_seconds()
    if elapsed <= pending_threshold:
        return False

    warn(f"[{entry.get('workload', '?')}] pending {int(elapsed)}s > {pending_threshold}s threshold → reclaim")
    _cancel_and_delete_pipelinerun(pr_name, namespace)
    stalls = entry.get("pending_stalls", 0) + 1
    entry["pending_stalls"] = stalls
    entry["pending_since"] = None
    entry["namespace"] = None
    if stalls >= max_pending_stalls:
        entry["status"] = "stalled"
        warn(f"[{entry.get('workload', '?')}] reached max pending stalls ({max_pending_stalls}) → stalled")
    else:
        entry["status"] = "pending"
    return True


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


def _probe_remote_mtimes(pod_name: str, phase_path: str, namespace: str) -> dict[str, float]:
    """Return {workload_name: mtime_epoch} for workloads that have trace_data.csv.

    Uses a single kubectl exec to stat all trace_data.csv files in the phase
    directory.  Returns empty dict on probe failure — callers should fall back
    to full copy in that case.
    """
    result = run(
        ["kubectl", "exec", pod_name, f"-n={namespace}", "--", "sh", "-c",
         f"find {phase_path} -name 'trace_data.csv'"
         " -exec stat -c '%Y %n' {} \\;"],
        check=False, capture=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        if result.returncode != 0:
            warn(f"mtime probe failed (rc={result.returncode}): "
                 f"{result.stderr.strip()} — falling back to full copy")
        else:
            info(f"mtime probe: no trace_data.csv found in {phase_path}")
        return {}
    if result.stderr.strip():
        warn(f"mtime probe had errors: {result.stderr.strip()}")
    mtimes: dict[str, float] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            warn(f"mtime probe: unparseable line: {line!r}")
            continue
        try:
            mtimes[Path(parts[1]).parent.name] = float(parts[0])
        except ValueError:
            warn(f"mtime probe: unparseable line: {line!r}")
    return mtimes


def _is_up_to_date(local_csv: Path, remote_mtime: "float | None") -> bool:
    """Return True if local trace_data.csv is at least as new as the remote copy."""
    if remote_mtime is None:
        return False
    try:
        return local_csv.exists() and local_csv.stat().st_mtime >= remote_mtime
    except OSError as exc:
        warn(f"stat failed for {local_csv}: {exc} — will re-download")
        return False


def _extract_phases_from_pvc(phases: list[str], run_name: str, namespace: str,
                              run_dir: Path,
                              skip_logs: bool = False,
                              workload: "str | None" = None,
                              allowed_workloads: "dict[str, set[str]] | None" = None,
                              on_workload_done=None) -> dict[str, "Exception | None"]:
    """Extract results for one or more phases from data-pvc using a single pod.

    Data layout on PVC (written by run-workload-blis-observe):
      /data/{runName}/{phase}/{workloadName}/trace_header.yaml
      /data/{runName}/{phase}/{workloadName}/trace_data.csv

    When *workload* is set, only that workload's subdirectory is copied for
    each phase (used by scoped ``collect --only/--workload``).
    When *workload* is None (default), workloads are discovered via ``ls`` and
    copied individually, skipping those whose local ``trace_data.csv`` is
    already up to date (used by unscoped ``deploy.py collect``).

    When *allowed_workloads* is set (a dict mapping phase name to a set of
    workload names), the ``ls``-discovered list for each phase is filtered to
    only include workloads in that phase's set. Used by the parallel/sequential
    callers to scope each slot to the exact (phase, workload) pairs that
    progress assigns to it.

    When *on_workload_done* is set, it is called after each workload completes
    (success or failure) with ``(phase, workload_name, namespace, error)``.
    *error* is None on success, or an Exception on failure. Used by callers to
    report per-workload progress in real time during extraction.

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
         "--overrides", overrides, "-n", namespace], capture=True)

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
            dest_dir.mkdir(parents=True, exist_ok=True)

            remote_mtimes = _probe_remote_mtimes(
                pod_name, f"/data/{run_name}/{phase}", namespace)

            if skip_logs:
                # Selective copy: trace files + epp_logs via kubectl cp per
                # workload; skips vLLM server_logs which dominate data volume.
                # BusyBox tar doesn't handle large streaming well, so use cp.
                if workload:
                    wl_names = [workload]
                else:
                    list_result = run(
                        ["kubectl", "exec", pod_name, f"-n={namespace}", "--",
                         "sh", "-c",
                         f"ls /data/{run_name}/{phase}/"],
                        check=False, capture=True,
                    )
                    if list_result.returncode != 0:
                        ls_err = RuntimeError(
                            f"failed to list workloads: {list_result.stderr.strip()}")
                        errors[phase] = ls_err
                        if on_workload_done and allowed_workloads is not None:
                            for wl in allowed_workloads.get(phase, set()):
                                on_workload_done(phase, wl, namespace, ls_err)
                        continue
                    wl_names = list_result.stdout.strip().split() if list_result.stdout.strip() else []
                    if allowed_workloads is not None:
                        phase_allowed = allowed_workloads.get(phase, set())
                        wl_names = [w for w in wl_names if w in phase_allowed]
                phase_errors = []
                for wl_name in wl_names:
                    if _is_up_to_date(dest_dir / wl_name / "trace_data.csv",
                                      remote_mtimes.get(wl_name)):
                        info(f"[{phase}/{wl_name}] up to date — skipping")
                        if on_workload_done:
                            on_workload_done(phase, wl_name, namespace, None)
                        continue
                    wl_dest = dest_dir / wl_name
                    wl_dest.mkdir(parents=True, exist_ok=True)
                    for log_dir in (wl_dest / "server_logs", wl_dest / "epp_logs"):
                        if log_dir.exists():
                            shutil.rmtree(log_dir)
                    # Copy trace files
                    wl_errors = []
                    for fname in ("trace_data.csv", "trace_header.yaml", "epp_stream_done"):
                        src = f"{namespace}/{pod_name}:/data/{run_name}/{phase}/{wl_name}/{fname}"
                        r = run(["kubectl", "cp", src, str(wl_dest / fname), "--retries=3"],
                                check=False, capture=True)
                        if r.returncode != 0 and "no such file" not in r.stderr.lower():
                            wl_errors.append(f"{wl_name}/{fname}: {r.stderr.strip()}")
                    # Copy epp_logs directory
                    epp_src = f"{namespace}/{pod_name}:/data/{run_name}/{phase}/{wl_name}/epp_logs/"
                    epp_dest = wl_dest / "epp_logs"
                    epp_dest.mkdir(exist_ok=True)
                    r = run(["kubectl", "cp", epp_src, str(epp_dest), "--retries=3"],
                            check=False, capture=True)
                    if r.returncode != 0 and "no such file" not in r.stderr.lower():
                        wl_errors.append(f"{wl_name}/epp_logs: {r.stderr.strip()}")
                    if wl_errors:
                        phase_errors.extend(wl_errors)
                    if on_workload_done:
                        wl_exc = RuntimeError("; ".join(wl_errors)) if wl_errors else None
                        on_workload_done(phase, wl_name, namespace, wl_exc)
                if phase_errors:
                    errors[phase] = RuntimeError("; ".join(phase_errors))
                else:
                    errors[phase] = None
            elif workload:
                if _is_up_to_date(dest_dir / workload / "trace_data.csv",
                                  remote_mtimes.get(workload)):
                    info(f"[{phase}/{workload}] up to date — skipping")
                    errors[phase] = None
                    if on_workload_done:
                        on_workload_done(phase, workload, namespace, None)
                else:
                    wl_dest = dest_dir / workload
                    if wl_dest.exists():
                        shutil.rmtree(wl_dest)
                    wl_dest.mkdir(parents=True, exist_ok=True)
                    result = run(
                        ["kubectl", "cp",
                         f"{namespace}/{pod_name}:/data/{run_name}/{phase}/{workload}/",
                         str(wl_dest), "--retries=3"],
                        check=False, capture=True,
                    )
                    if result.returncode != 0:
                        wl_exc = RuntimeError(
                            f"kubectl cp failed: {result.stderr.strip()}")
                        errors[phase] = wl_exc
                    else:
                        wl_exc = None
                        errors[phase] = None
                    if on_workload_done:
                        on_workload_done(phase, workload, namespace, wl_exc)
            else:
                # Unscoped full copy: discover workloads via ls, skip
                # up-to-date ones using remote_mtimes when available.
                list_result = run(
                    ["kubectl", "exec", pod_name, f"-n={namespace}", "--",
                     "sh", "-c",
                     f"ls /data/{run_name}/{phase}/"],
                    check=False, capture=True,
                )
                if list_result.returncode != 0:
                    ls_err = RuntimeError(
                        f"failed to list workloads: {list_result.stderr.strip()}")
                    errors[phase] = ls_err
                    if on_workload_done and allowed_workloads is not None:
                        for wl in allowed_workloads.get(phase, set()):
                            on_workload_done(phase, wl, namespace, ls_err)
                    continue
                wl_names = list_result.stdout.strip().split() if list_result.stdout.strip() else []
                if allowed_workloads is not None:
                    phase_allowed = allowed_workloads.get(phase, set())
                    wl_names = [w for w in wl_names if w in phase_allowed]
                if not wl_names:
                    errors[phase] = None
                    continue
                phase_errors = []
                for wl_name in wl_names:
                    if _is_up_to_date(dest_dir / wl_name / "trace_data.csv",
                                      remote_mtimes.get(wl_name)):
                        info(f"[{phase}/{wl_name}] up to date — skipping")
                        if on_workload_done:
                            on_workload_done(phase, wl_name, namespace, None)
                        continue
                    wl_dest = dest_dir / wl_name
                    if wl_dest.exists():
                        shutil.rmtree(wl_dest)
                    wl_dest.mkdir(parents=True, exist_ok=True)
                    result = run(
                        ["kubectl", "cp",
                         f"{namespace}/{pod_name}:/data/{run_name}/{phase}/{wl_name}/",
                         str(wl_dest), "--retries=3"],
                        check=False, capture=True,
                    )
                    if result.returncode != 0:
                        wl_exc = RuntimeError(f"{wl_name}: {result.stderr.strip()}")
                        phase_errors.append(f"{wl_name}: {result.stderr.strip()}")
                    else:
                        wl_exc = None
                    if on_workload_done:
                        on_workload_done(phase, wl_name, namespace, wl_exc)
                errors[phase] = RuntimeError("; ".join(phase_errors)) if phase_errors else None
    finally:
        run(["kubectl", "delete", "pod", pod_name, "-n", namespace,
             "--ignore-not-found", "--force", "--grace-period=0"],
            check=False, capture=True)

    return errors


def _cmd_collect(args, run_dir: Path, setup_config: dict):
    """Pull results from cluster for completed phases."""
    namespace = os.environ.get("NAMESPACE", setup_config.get("namespace", ""))
    if not namespace:
        err("No namespace configured.")
        sys.exit(1)

    run_name = run_dir.name

    # Derive known phases from ConfigMap
    from pipeline.lib.progress import ConfigMapProgressStore
    primary_ns = _configmap_namespace(setup_config)
    if not primary_ns:
        err("No namespace configured. Run setup.py first.")
        sys.exit(1)
    store = ConfigMapProgressStore(primary_ns, run_name=run_dir.name)
    try:
        progress = store.load() or None
    except (ValueError, RuntimeError) as exc:
        warn(f"Failed to load progress: {exc}")
        progress = None

    # ── Pair-level scoping (--only / --workload) ──────────────────────────
    scope_only = getattr(args, "only", None)
    scope_workload = getattr(args, "workload", None)
    scoped = scope_only is not None or scope_workload is not None

    if scoped and not progress:
        err("--only/--workload require progress data to resolve pairs, but none was found.")
        sys.exit(1)

    if scoped and progress:
        # Build a lightweight args namespace for _resolve_scope with only
        # pair-level filters (--only, --workload).  Collect's --package is
        # a phase-level filter (nargs="+") and must NOT be mixed in.
        class _ScopeArgs:
            only = scope_only
            workload = scope_workload
            package = None
            status = None

        in_scope = _resolve_scope(progress, _ScopeArgs())

        # Filter to collectible pairs (done) and warn about the rest
        collectible = {
            k for k in in_scope
            if isinstance(progress[k], dict) and progress[k].get("status") == "done"
        }
        for key in sorted(in_scope - collectible):
            entry = progress[key]
            st = entry.get("status", "") if isinstance(entry, dict) else str(entry)
            warn(f"Scoped pair {key} has status '{st}' — skipping")

        scoped_phases = sorted({
            progress[k].get("package", "") for k in collectible
        } - {""})
        scoped_workloads = sorted({
            progress[k].get("workload", "") for k in collectible
        } - {""})

        if not scoped_phases:
            warn("No done phases for scoped pairs.")
            phases_to_collect: list[str] = []
        elif (pkg_filter := _parse_list(args.package)):
            valid = set(scoped_phases) | {"experiment"}
            unknown = set(pkg_filter) - valid
            if unknown:
                err(f"Unknown packages: {sorted(unknown)}. Valid: {sorted(valid)}")
                sys.exit(1)
            phases_to_collect = []
            for p in pkg_filter:
                if p == "experiment":
                    phases_to_collect.extend(scoped_phases)
                else:
                    phases_to_collect.append(p)
            seen: set[str] = set()
            phases_to_collect = [p for p in phases_to_collect
                                 if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]
        else:
            phases_to_collect = list(scoped_phases)

        step(1, "Collecting Results")
        collected: list[str] = []
        failed: list[str] = []
        collected_pairs: list[str] = []
        failed_pairs: list[str] = []
        total_pairs = len(collectible)

        skip_logs = getattr(args, "skip_logs", False)
        for wl_name in scoped_workloads:
            wl_ns = next(
                (progress[k].get("completed_namespace")
                 for k in collectible
                 if isinstance(progress[k], dict)
                 and progress[k].get("workload") == wl_name
                 and progress[k].get("completed_namespace")),
                None,
            )
            if not wl_ns:
                warn(f"{wl_name}: completed_namespace missing — skipping (re-run the workload with a newer orchestrator to collect results)")
                for p in phases_to_collect:
                    if p not in failed:
                        failed.append(p)
                    failed_pairs.append(f"{p}/{wl_name}")
                continue
            try:
                errors = _extract_phases_from_pvc(
                    phases_to_collect, run_name, wl_ns, run_dir,
                    skip_logs=skip_logs, workload=wl_name)
            except RuntimeError as e:
                warn(f"Extractor pod failed for workload {wl_name}: {e}")
                for p in phases_to_collect:
                    if p not in failed:
                        failed.append(p)
                    failed_pairs.append(f"{p}/{wl_name}")
            else:
                for phase, exc in errors.items():
                    if exc is None:
                        ok(f"{phase}/{wl_name}    ({wl_ns})")
                        if phase not in collected:
                            collected.append(phase)
                        collected_pairs.append(f"{phase}/{wl_name}")
                    else:
                        warn(f"{phase}/{wl_name}    ({wl_ns}): {exc}")
                        if phase not in failed:
                            failed.append(phase)
                        failed_pairs.append(f"{phase}/{wl_name}")

        collected = [p for p in collected if p not in failed]
    else:
        # ── Unscoped path (no --only/--workload) ─────────────────────────
        if progress:
            known_phases = sorted({
                entry.get("package", "")
                for entry in progress.values()
                if isinstance(entry, dict) and entry.get("status") == "done"
            } - {""})
        else:
            known_phases = []

        if not known_phases:
            cluster_dir = run_dir / "cluster"
            known_phases = _discover_phases(cluster_dir)
            if progress is None:
                warn(f"No progress data found — discovered phases from cluster/: {known_phases}")
            else:
                warn(f"No done phases in progress — discovered from cluster/: {known_phases}")

        pkg_filter = _parse_list(args.package)
        if pkg_filter:
            valid = set(known_phases) | {"experiment"}
            unknown = set(pkg_filter) - valid
            if unknown:
                err(f"Unknown packages: {sorted(unknown)}. Valid: {sorted(valid)}")
                sys.exit(1)
            phases_to_collect = []
            for p in pkg_filter:
                if p == "experiment":
                    phases_to_collect.extend(known_phases)
                else:
                    phases_to_collect.append(p)
            seen = set()
            phases_to_collect = [p for p in phases_to_collect
                                 if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]
        else:
            phases_to_collect = list(known_phases)

        collected = []
        failed = []
        collected_pairs: list[str] = []
        failed_pairs: list[str] = []

        if phases_to_collect:
            skip_logs = getattr(args, "skip_logs", False)
            if progress:
                # Group done entries by completed_namespace.
                # Entries without completed_namespace were written by an older
                # version of the orchestrator that did not record it.
                ns_phase_map: dict[str, list[str]] = {}
                ns_pair_map: dict[str, set[tuple[str, str]]] = {}
                missing_ns_keys: list[str] = []
                for key, pentry in progress.items():
                    if not isinstance(pentry, dict):
                        continue
                    if pentry.get("status") != "done":
                        continue
                    pkg = pentry.get("package", "")
                    if pkg not in phases_to_collect:
                        continue
                    ns = pentry.get("completed_namespace")
                    if not ns:
                        missing_ns_keys.append(key)
                        continue
                    if pkg not in ns_phase_map.setdefault(ns, []):
                        ns_phase_map[ns].append(pkg)
                    wl = pentry.get("workload", "")
                    if wl:
                        ns_pair_map.setdefault(ns, set()).add((pkg, wl))

                total_pairs = sum(
                    1 for pentry in progress.values()
                    if isinstance(pentry, dict)
                    and pentry.get("status") == "done"
                    and pentry.get("package", "") in phases_to_collect
                    and pentry.get("completed_namespace")
                )

                ns_items = sorted(ns_phase_map.items())
                if len(ns_items) > 1:
                    step(1, f"Collecting Results ({len(ns_items)} slots in parallel)")
                else:
                    step(1, "Collecting Results")

                for key in missing_ns_keys:
                    warn(f"{key}: completed_namespace missing — skipping (re-run the workload with a newer orchestrator to collect results)")

                def _on_workload_done(phase, wl_name, ns, error):
                    """Report per-workload progress as it happens."""
                    if error is None:
                        ok(f"{phase}/{wl_name}    ({ns})")
                        collected_pairs.append(f"{phase}/{wl_name}")
                        if phase not in collected:
                            collected.append(phase)
                    else:
                        warn(f"{phase}/{wl_name}    ({ns}): {error}")
                        failed_pairs.append(f"{phase}/{wl_name}")
                        if phase not in failed:
                            failed.append(phase)

                def _handle_slot_failure(ns, pairs_in_ns):
                    """Handle pod-level failure where callback never fired."""
                    ns_phases_local = ns_phase_map[ns]
                    for p in ns_phases_local:
                        if p not in failed:
                            failed.append(p)
                        for pkg, wl in sorted(pairs_in_ns):
                            if pkg == p:
                                warn(f"{p}/{wl}    ({ns})")
                                failed_pairs.append(f"{p}/{wl}")

                if len(ns_items) > 1:
                    import concurrent.futures

                    def _extract_one_slot(ns, ns_phases):
                        pairs_in_ns = ns_pair_map.get(ns, set())
                        allowed = {}
                        for pkg, wl in pairs_in_ns:
                            allowed.setdefault(pkg, set()).add(wl)
                        try:
                            _extract_phases_from_pvc(
                                sorted(ns_phases), run_name, ns, run_dir,
                                skip_logs=skip_logs,
                                allowed_workloads=allowed,
                                on_workload_done=_on_workload_done)
                        except Exception as e:
                            return (ns, pairs_in_ns, e)
                        return (ns, pairs_in_ns, None)

                    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ns_items)) as executor:
                        futures = {
                            executor.submit(_extract_one_slot, ns, ns_phases): ns
                            for ns, ns_phases in ns_items
                        }
                        for future in concurrent.futures.as_completed(futures):
                            try:
                                ns, pairs_in_ns, result = future.result()
                            except Exception as e:
                                ns = futures[future]
                                pairs_in_ns = ns_pair_map.get(ns, set())
                                result = e
                            if isinstance(result, Exception):
                                warn(f"Extractor pod failed in {ns}: {result}")
                                _handle_slot_failure(ns, pairs_in_ns)
                else:
                    for ns, ns_phases in ns_items:
                        pairs_in_ns = ns_pair_map.get(ns, set())
                        allowed = {}
                        for pkg, wl in pairs_in_ns:
                            allowed.setdefault(pkg, set()).add(wl)
                        try:
                            _extract_phases_from_pvc(
                                sorted(ns_phases), run_name, ns, run_dir,
                                skip_logs=skip_logs,
                                allowed_workloads=allowed,
                                on_workload_done=_on_workload_done)
                        except Exception as e:
                            warn(f"Extractor pod failed in {ns}: {e}")
                            _handle_slot_failure(ns, pairs_in_ns)
            else:
                # No progress data — fallback to primary namespace.
                step(1, "Collecting Results")
                total_pairs = len(phases_to_collect)
                try:
                    errors = _extract_phases_from_pvc(
                        phases_to_collect, run_name, namespace, run_dir,
                        skip_logs=skip_logs)
                except RuntimeError as e:
                    warn(f"Extractor pod failed: {e}")
                    failed.extend(phases_to_collect)
                    failed_pairs.extend(phases_to_collect)
                else:
                    for phase, exc in errors.items():
                        if exc is None:
                            ok(f"{phase}    ({namespace})")
                            collected.append(phase)
                            collected_pairs.append(phase)
                        else:
                            warn(f"{phase}    ({namespace}): {exc}")
                            failed.append(phase)
                            failed_pairs.append(phase)
        else:
            total_pairs = 0

    # Print summary
    print(f"\n  Collected: {len(collected_pairs)}/{total_pairs} pairs")
    if failed_pairs:
        print(f"  Failed:    {len(failed_pairs)} pairs")
    if collected_pairs:
        print(f"  Results:   {run_dir / 'results'}/")
        print("\n  Next:      /sim2real-analyze")
    print()


# ── Run helpers ─────────────────────────────────────────────────────────────

def _load_pairs(cluster_dir: Path) -> dict:
    """Discover all (workload, package) pairs from pipelinerun-*.yaml at cluster/ root.

    Returns dict keyed by "wl-" + filename stem (minus "pipelinerun-" prefix).
    """
    pairs = {}
    if not cluster_dir.exists():
        return pairs
    for pr_path in sorted(cluster_dir.glob("pipelinerun-*.yaml")):
        try:
            pr_data = yaml.safe_load(pr_path.read_text())
            pr_name = pr_data.get("metadata", {}).get("name", pr_path.stem)
            params = {p["name"]: p["value"] for p in pr_data.get("spec", {}).get("params", [])}
            workload = params.get("workloadName", "")
            package = params.get("phase", "")
            key = "wl-" + pr_path.stem.removeprefix("pipelinerun-")
            pairs[key] = {
                "workload": workload,
                "package": package,
                "pr_name": pr_name,
                "pr_path": str(pr_path),
                "namespace": pr_data.get("metadata", {}).get("namespace", ""),
                "scenario_content": params.get("scenarioContent"),
            }
        except Exception as e:
            warn(f"Skipping {pr_path.name}: {e}")
            continue
    return pairs


def _uninstall_orphaned_helm(key: str, namespace: str) -> None:
    """Check a namespace for lingering helm releases and uninstall them."""
    result = run(["helm", "list", "-n", namespace, "-q"],
                 check=False, capture=True)
    if result.returncode != 0:
        warn(f"{key}: helm list failed in {namespace}")
    elif result.stdout.strip():
        for release in result.stdout.strip().splitlines():
            ur = run(["helm", "uninstall", release, "-n", namespace],
                     check=False, capture=True)
            if ur.returncode == 0:
                ok(f"Uninstalled: {release} (ns: {namespace})")
            else:
                warn(f"Failed to uninstall {release} in {namespace}")


def _reset_pair(key: str, entry: dict, discovered: dict, *,
                dry_run: bool = False, namespaces: list[str] | None = None,
                preserve_done_status: bool = False) -> bool:
    """Delete PipelineRun and Helm releases for a pair, then reset state to pending.

    For done pairs with preserve_done_status=True: cluster cleanup only,
    status stays done.

    Returns True if reset was performed, False if it failed and state was NOT reset.
    """
    ns = entry.get("namespace")
    pr_name = discovered.get(key, {}).get("pr_name", "")
    is_done = entry.get("status") == "done"

    # No namespace and no pr_name — just reset state
    if not ns and not pr_name:
        if is_done:
            completed_ns = entry.get("completed_namespace")
            if completed_ns:
                if dry_run:
                    info(f"[DRY-RUN] {key}: would check for orphaned helm releases in {completed_ns}")
                else:
                    _uninstall_orphaned_helm(key, completed_ns)
        if not dry_run and not (is_done and preserve_done_status):
            entry["status"] = "pending"
            entry["retries"] = 0
            entry["pending_stalls"] = 0
            entry["pending_since"] = None
        return True

    if dry_run:
        target = ns or "all namespace slots"
        info(f"[DRY-RUN] {key}: would delete pipelinerun {pr_name or '(unknown)'} in {target}")
        if not is_done:
            info(f"[DRY-RUN] {key}: would uninstall all helm releases in {target}")
        else:
            completed_ns = entry.get("completed_namespace")
            if completed_ns:
                info(f"[DRY-RUN] {key}: would check for orphaned helm releases in {completed_ns}")
        return True

    # Delete PipelineRun
    pr_deleted = False
    if not pr_name:
        warn(f"{key}: no PipelineRun name found — skipping PR deletion (manual check needed)")
    elif ns:
        if entry.get("status") == "running":
            _cancel_and_delete_pipelinerun(pr_name, ns)
            pr_deleted = True
        else:
            result = run(["kubectl", "delete", "pipelinerun", pr_name, "-n", ns,
                         "--ignore-not-found"], check=False, capture=True)
            if result.returncode == 0:
                pr_deleted = True
            else:
                warn(f"{key}: kubectl delete pipelinerun failed in {ns}")
    elif namespaces:
        # Namespace already freed — search all slots
        for slot_ns in namespaces:
            result = run(["kubectl", "delete", "pipelinerun", pr_name, "-n", slot_ns,
                         "--ignore-not-found"], check=False, capture=True)
            if result.returncode == 0:
                pr_deleted = True
        if not pr_deleted:
            warn(f"{key}: kubectl delete pipelinerun failed across all namespace slots")
    else:
        warn(f"{key}: no namespace and no namespace slots — cannot delete pipelinerun {pr_name}")

    # For done pairs, Tekton finally task should have torn down Helm releases,
    # but check completed_namespace for orphans in case teardown failed.
    if is_done:
        completed_ns = entry.get("completed_namespace")
        if completed_ns:
            _uninstall_orphaned_helm(key, completed_ns)
        if not preserve_done_status:
            entry["status"] = "pending"
            entry["namespace"] = None
            entry["retries"] = 0
            entry["pending_stalls"] = 0
            entry["pending_since"] = None
        return True

    if ns and not pr_deleted and pr_name:
        warn(f"{key}: PipelineRun not deleted — state NOT reset")
        return False

    # Discover and uninstall all Helm releases in the namespace
    if ns:
        result = run(["helm", "list", "-n", ns, "-q"], check=False, capture=True)
        if result.returncode != 0:
            warn(f"{key}: helm list failed in {ns} — skipping reset (manual intervention needed)")
            return False
        if result.stdout.strip():
            helm_failed = False
            for release in result.stdout.strip().splitlines():
                ur = run(["helm", "uninstall", release, "-n", ns], check=False, capture=True)
                if ur.returncode == 0:
                    ok(f"Uninstalled: {release} (ns: {ns})")
                else:
                    warn(f"Failed to uninstall {release} in {ns}")
                    helm_failed = True
            if helm_failed:
                warn(f"{key}: some releases failed to uninstall — state NOT reset")
                return False

    # Reset state
    entry["status"] = "pending"
    entry["namespace"] = None
    entry["retries"] = 0
    entry["pending_stalls"] = 0
    entry["pending_since"] = None
    return True


def _force_reset(progress: dict, scope: set, discovered: dict | None = None,
                 namespaces: list[str] | None = None) -> int:
    """Reset all non-pending pairs in scope to pending.

    Calls _reset_pair for cluster teardown when possible. Pairs where
    reset fails are skipped (not counted, state preserved).
    """
    reset = 0
    for key in scope:
        entry = progress.get(key, {})
        if entry.get("status") not in (None, "pending"):
            try:
                if _reset_pair(key, entry, discovered or {},
                              namespaces=namespaces):
                    reset += 1
            except Exception as e:
                warn(f"{key}: reset failed during --force: {e}")
    return reset


def _parse_list(value) -> "list[str] | None":
    """Flatten a CLI flag value (possibly a list from nargs='+') by splitting on commas."""
    if value is None:
        return None
    if isinstance(value, list):
        result = [v.strip() for item in value for v in item.split(",") if v.strip()]
    else:
        result = [v.strip() for v in value.split(",") if v.strip()]
    return result if result else None


def _apply_run_filters(progress: dict, args) -> set:
    """Return the set of pair keys in scope for this invocation.

    With no flags: returns empty set (caller interprets as all pairs in scope).
    With flags: returns only matching pairs.
    """
    only = _parse_list(getattr(args, "only", None))
    workload = _parse_list(getattr(args, "workload", None))
    package = _parse_list(getattr(args, "package", None))
    status_filter = getattr(args, "status", None)

    if only:
        result = set()
        unresolved = []
        for key in only:
            if key in progress and _is_pair_key(key):
                result.add(key)
            else:
                prefixed = "wl-" + key
                if prefixed in progress:
                    info(f"--only: resolved '{key}' → '{prefixed}'")
                    result.add(prefixed)
                else:
                    unresolved.append(key)
        if unresolved:
            err(f"--only: no match for {unresolved}")
            valid = sorted(k for k in progress.keys() if _is_pair_key(k))
            print(f"  Valid pair keys: {valid}", file=sys.stderr)
            sys.exit(1)
        return result

    if not any([workload, package, status_filter]):
        return set()

    pair_entries = {k: v for k, v in progress.items() if _is_pair_key(k)}

    if workload:
        valid_workloads = {v.get("workload", "") for v in pair_entries.values()} - {""}
        unknown = set(workload) - valid_workloads
        if unknown:
            err(f"--workload: unrecognized values {sorted(unknown)}")
            print(f"  Valid --workload values: {', '.join(sorted(valid_workloads))}", file=sys.stderr)
            sys.exit(1)

    if package:
        valid_packages = {v.get("package", "") for v in pair_entries.values()} - {""}
        unknown = set(package) - valid_packages
        if unknown:
            err(f"--package: unrecognized values {sorted(unknown)}")
            print(f"  Valid --package values: {', '.join(sorted(valid_packages))}", file=sys.stderr)
            sys.exit(1)

    candidates = set(pair_entries.keys())
    if workload:
        candidates = {k for k in candidates if pair_entries[k].get("workload") in workload}
    if package:
        candidates = {k for k in candidates if pair_entries[k].get("package") in package}
    if status_filter:
        candidates = {k for k in candidates if pair_entries[k].get("status") == status_filter}
    return candidates


def _resolve_scope(progress: dict, args) -> set:
    """Apply filter args and return the set of pair keys in scope.

    No flags → all pairs. Flags + match → narrowed set. Flags + no match → abort
    with valid values printed.
    """
    filters_given = any([
        getattr(args, "only", None) is not None,
        getattr(args, "workload", None) is not None,
        getattr(args, "package", None) is not None,
        getattr(args, "status", None) is not None,
    ])
    filtered = _apply_run_filters(progress, args)
    if filters_given and not filtered:
        _report_filter_mismatch(progress, args)
        sys.exit(1)
    return filtered or {k for k in progress.keys() if _is_pair_key(k)}


def _report_filter_mismatch(progress: dict, args) -> None:
    """Print all valid filter values to help the user correct their filter flags."""
    only = _parse_list(getattr(args, "only", None))
    workload = _parse_list(getattr(args, "workload", None))
    package = _parse_list(getattr(args, "package", None))
    status_filter = getattr(args, "status", None)

    parts = []
    if only is not None:
        parts.append(f"--only '{','.join(only)}'")
    if workload is not None:
        parts.append(f"--workload '{','.join(workload)}'")
    if package is not None:
        parts.append(f"--package '{','.join(package)}'")
    if status_filter is not None:
        parts.append(f"--status '{status_filter}'")

    err(f"No pairs matched {', '.join(parts)}.\n")

    keys = sorted(k for k in progress.keys() if _is_pair_key(k))
    print(f"  Valid pair keys ({len(keys)}):", file=sys.stderr)
    for k in keys:
        print(f"    {k}", file=sys.stderr)

    pair_values = [v for k, v in progress.items() if _is_pair_key(k)]
    valid_workloads = sorted({v.get("workload", "") for v in pair_values} - {""})
    valid_packages = sorted({v.get("package", "") for v in pair_values} - {""})
    valid_statuses = sorted({v.get("status", "") for v in pair_values} - {""})

    print(f"\n  Valid --workload values: {', '.join(valid_workloads)}", file=sys.stderr)
    print(f"  Valid --package values:  {', '.join(valid_packages)}", file=sys.stderr)
    print(f"  Valid --status values:   {', '.join(valid_statuses)}", file=sys.stderr)


def _check_slot_ready(namespace: str, hf_secret_name: str = "hf-secret") -> tuple[bool, list[str]]:
    """Check that a namespace slot is ready to accept a new PipelineRun.

    Checks: PVCs bound, HF secret present.
    Returns (ready, list_of_failure_reasons).

    Note: Tekton tasks presence check is not yet implemented; assumes
    ``setup.py`` has been run.
    """
    failures = []

    for pvc in ["data-pvc", "source-pvc"]:
        result = run(
            ["kubectl", "get", "pvc", pvc, f"-n={namespace}",
             "-o", "jsonpath={.status.phase}"],
            check=False, capture=True,
        )
        if result.returncode != 0 or result.stdout.strip() != "Bound":
            hint = " — re-run setup.py to provision it" if pvc == "source-pvc" else ""
            failures.append(f"PVC {pvc} not Bound in {namespace}{hint}")

    result = run(
        ["kubectl", "get", "secret", hf_secret_name, f"-n={namespace}"],
        check=False, capture=True,
    )
    if result.returncode != 0:
        failures.append(f"Secret {hf_secret_name} missing in {namespace}")

    return len(failures) == 0, failures


def _reconcile_on_resume(progress: dict, discovered: dict, *,
                         preserve_pipelineruns: bool = False) -> None:
    """Reconcile pair statuses against cluster state when resuming an interrupted run.

    - running pairs: check PipelineRun status on cluster and update accordingly
    - unrecognized statuses (e.g. 'collecting' or 'collect-failed' from a
      pre-#120 progress data): reset to pending so they are re-dispatched.
      This is safe because both historical statuses imply the PipelineRun
      already succeeded.
    """
    _known = ("pending", "running", "done", "failed", "timed-out", "stalled")
    for key, entry in progress.items():
        if not _is_pair_key(key):
            continue
        if entry["status"] == "running":
            pr_meta = discovered.get(key, {})
            pr_name = pr_meta.get("pr_name", "")
            ns = entry.get("namespace") or ""
            if pr_name and ns:
                try:
                    actual = _check_pipelinerun_status(pr_name, ns)
                except Exception as exc:
                    warn(f"[{key}] failed to check PipelineRun status: {exc}")
                    continue
                if actual in ("Succeeded", "Completed"):
                    entry["status"] = "done"
                    entry["pending_since"] = None
                    entry["completed_namespace"] = ns
                    if not preserve_pipelineruns:
                        try:
                            _delete_pipelinerun(pr_name, ns)
                        except Exception as exc:
                            warn(f"Failed to delete PipelineRun {pr_name!r} in {ns}: {exc}")
                    entry["namespace"] = None
                elif actual in ("Failed", "PipelineRunCancelled",
                               "PipelineRunCouldntGetPipeline",
                               "PipelineRunTimeout", "CreateRunFailed",
                               "PipelineRunStopping",
                               "PipelineRunStoppingTimeout"):
                    entry["status"] = "failed"
                    # Retain namespace so reset/cleanup can find the resources
                    entry["pending_since"] = None
                elif actual == "Unknown":
                    warn(f"[{key}] PipelineRun not found on cluster → resetting to pending")
                    entry["status"] = "pending"
                    entry["namespace"] = None
                    entry["pending_since"] = None
            else:
                entry["status"] = "pending"
                entry["namespace"] = None
                entry["pending_since"] = None
        elif entry["status"] not in _known:
            warn(f"[{key}] unrecognized status '{entry['status']}' → resetting to pending")
            entry["status"] = "pending"
            entry["namespace"] = None
            entry["pending_since"] = None


def _derive_pair_gpu_costs(
    discovered: dict,
    *,
    defaults: dict | None,
    fallback_cost: int,
) -> dict[str, int]:
    """Compute GPU cost per pair from its scenarioContent.

    If defaults is None, returns fallback_cost for all pairs immediately.

    Otherwise, per pair:
      1. Parse scenarioContent → gpu_cost_per_pair(scenario, defaults)
      2. If scenarioContent missing/invalid → gpu_cost_per_pair({}, defaults)
      3. If derivation returns an error → fallback_cost
    """
    from pipeline.lib.capacity import gpu_cost_per_pair

    costs = {}
    for key, meta in discovered.items():
        if defaults is None:
            costs[key] = fallback_cost
            continue

        scenario_content = meta.get("scenario_content")
        resolved = None
        if scenario_content:
            try:
                resolved = yaml.safe_load(scenario_content)
            except yaml.YAMLError as e:
                warn(f"{key}: scenarioContent is invalid YAML ({e}) — deriving cost from defaults")

        if resolved and isinstance(resolved, dict):
            result = gpu_cost_per_pair(resolved, defaults)
        else:
            result = gpu_cost_per_pair({}, defaults)

        if isinstance(result, int):
            costs[key] = result
        else:
            warn(f"{key}: GPU cost derivation failed: {result} — using fallback ({fallback_cost})")
            costs[key] = fallback_cost

    return costs


def _capacity_limited_pairs(
    pending: list[str],
    progress: dict,
    *,
    free_gpus: int,
    default_gpu_cost: int,
) -> list[str]:
    """Select pending pairs that fit within available GPU capacity.

    Sorts by gpu_cost ascending to maximize dispatch count.
    """
    sorted_pending = sorted(
        pending,
        key=lambda k: progress[k].get("gpu_cost", default_gpu_cost),
    )
    result = []
    budget = free_gpus
    for pair in sorted_pending:
        cost = progress[pair].get("gpu_cost", default_gpu_cost)
        if budget >= cost:
            budget -= cost
            result.append(pair)
    return result


def _cmd_run(args, run_dir: Path, setup_config: dict) -> None:
    """Orchestrate parallel pool execution across namespace slots."""
    import datetime as _dt
    import tempfile as _tmp
    from pipeline.lib.progress import ConfigMapProgressStore
    from pipeline.lib.backoff import BackoffController
    from pipeline.lib.capacity import (
        probe_free_gpus, derive_gpu_resource_type, load_defaults
    )

    namespaces = setup_config.get("namespaces") or [setup_config.get("namespace", "")]
    if not namespaces or not namespaces[0]:
        err("No namespaces configured. Run setup.py with --namespaces."); sys.exit(1)

    _cmd_build(run_dir, namespace=namespaces[0], skip_build=args.skip_build)

    max_retries = getattr(args, "max_retries", 2)
    poll_interval = getattr(args, "poll_interval", 30)
    pending_threshold = getattr(args, "pending_threshold", 600)
    max_pending_stalls = getattr(args, "max_pending_stalls", 10)

    cluster_dir = run_dir / "cluster"
    primary_ns = _configmap_namespace(setup_config, namespaces)
    if not primary_ns:
        err("No namespace configured. Run setup.py first."); sys.exit(1)
    store = ConfigMapProgressStore(primary_ns, run_name=run_dir.name)

    # Derive GPU resource type from baseline scenario
    # CLI --gpu-resource-type overrides auto-derivation when explicitly set
    gpu_resource_type = args.gpu_resource_type  # None means auto-derive
    fallback_cost = args.default_gpu_cost
    defaults_result = load_defaults(REPO_ROOT)
    if isinstance(defaults_result, str):
        warn(defaults_result)
        defaults_result = None
    scenario_path = cluster_dir / "baseline.yaml"
    if not scenario_path.exists():
        for p in sorted(cluster_dir.glob("*.yaml")):
            if not p.name.startswith("pipelinerun-"):
                scenario_path = p
                info(f"Deriving GPU config from: {scenario_path.name}")
                break
    if defaults_result and scenario_path.exists():
        try:
            resolved = yaml.safe_load(scenario_path.read_text()) or {}
        except yaml.YAMLError as e:
            warn(f"Could not parse {scenario_path.name}: {e}")
            resolved = None
        if resolved:
            if gpu_resource_type is None:
                gpu_resource_type = derive_gpu_resource_type(resolved, defaults_result)
    elif defaults_result is None and not scenario_path.exists():
        info("Defaults or scenario not found — using CLI defaults")
    if gpu_resource_type is None:
        gpu_resource_type = "nvidia.com/gpu"
    if gpu_resource_type != "nvidia.com/gpu":
        info(f"GPU resource type: {gpu_resource_type}")
    _probe_fail_count = 0
    _last_probe_error = ""
    _last_log_state: dict[str, object] = {}
    _zero_dispatch_count = 0

    # Load or initialize progress
    progress = store.load()

    max_backoff = getattr(args, "max_backoff", 600)
    existing_orch = progress.get("_orchestrator")
    if isinstance(existing_orch, dict):
        backoff = BackoffController.from_dict(existing_orch, base_interval=poll_interval, max_backoff=max_backoff)
        if backoff.state != "normal":
            info(f"Resuming in {backoff.state} state (level {backoff.backoff_level})")
    else:
        backoff = BackoffController(base_interval=poll_interval, max_backoff=max_backoff)

    discovered = _load_pairs(cluster_dir)
    if not discovered:
        err("No pairs found in cluster/. Run prepare.py first."); sys.exit(1)

    pair_costs = _derive_pair_gpu_costs(
        discovered, defaults=defaults_result, fallback_cost=fallback_cost,
    )

    # Initialize new entries (first run or new pairs added)
    for key, meta in discovered.items():
        if key not in progress:
            progress[key] = {
                "workload": meta["workload"],
                "package":  meta["package"],
                "status":   "pending",
                "namespace": None,
                "completed_namespace": None,
                "retries":  0,
                "gpu_cost": pair_costs[key],
                "pending_stalls": 0,
                "pending_since": None,
            }

    _scope = _resolve_scope(progress, args)
    total_pairs = sum(1 for k in progress if _is_pair_key(k))
    if len(_scope) < total_pairs:
        info(f"Scope: {len(_scope)}/{total_pairs} pairs")

    if getattr(args, "force", False):
        n = _force_reset(progress, _scope, discovered, namespaces=namespaces)
        if n:
            info(f"--force: reset {n} non-pending pair(s) to pending")
        else:
            info("--force: no non-pending pairs found in scope — nothing reset")
        store.save(progress)

    preserve_pipelineruns = getattr(args, "preserve_pipelineruns", False)
    _reconcile_on_resume(progress, discovered,
                         preserve_pipelineruns=preserve_pipelineruns)
    store.save(progress)

    # Track which namespace is assigned to which pair
    slots_busy: dict[str, str] = {
        entry["namespace"]: key
        for key, entry in progress.items()
        if _is_pair_key(key) and entry.get("status") == "running" and entry.get("namespace")
    }

    def _pending_pairs() -> list[str]:
        return [k for k, v in progress.items()
                if _is_pair_key(k) and v.get("status") == "pending" and k in _scope]

    def _work_remaining() -> bool:
        return any(v.get("status") in ("pending", "running")
                   for k, v in progress.items() if _is_pair_key(k) and k in _scope)

    timeout_hours = 4
    info(f"Orchestrator: {len(_scope)} pairs in scope, {len(namespaces)} slot(s)")
    if not _work_remaining() and not slots_busy:
        info(f"All {len(_scope)} pairs in scope already done — nothing to dispatch (use --force to reset)")
        return

    while _work_remaining() or slots_busy:

        # ── Process completed/failed slots ───────────────────────────────
        for ns in list(slots_busy):
            pair_key = slots_busy[ns]
            entry = progress[pair_key]
            pr_meta = discovered.get(pair_key, {})
            pr_name = pr_meta.get("pr_name", "")

            status = _check_pipelinerun_status(pr_name, ns) if pr_name else "Unknown"

            if status in ("Succeeded", "Completed"):
                ok(f"[{pair_key}] {status} → done")
                entry["status"] = "done"
                entry["completed_namespace"] = ns
                entry["namespace"] = None
                store.save(progress)
                if not preserve_pipelineruns:
                    try:
                        _delete_pipelinerun(pr_name, ns)
                    except Exception as exc:
                        warn(f"Failed to delete PipelineRun {pr_name!r} in {ns}: {exc}")
                del slots_busy[ns]
                _last_log_state.pop("capacity", None)
                _last_log_state.pop("dispatch", None)
                _last_log_state.pop("backoff_skip", None)
                _zero_dispatch_count = 0

            elif status in ("Failed", "PipelineRunCancelled", "PipelineRunCouldntGetPipeline",
                            "PipelineRunTimeout", "CreateRunFailed", "PipelineRunStopping",
                            "PipelineRunStoppingTimeout"):
                warn(f"[{pair_key}] hard failure ({status}) → failed")
                entry["status"] = "failed"
                entry["namespace"] = None
                store.save(progress)
                del slots_busy[ns]
                _last_log_state.pop("capacity", None)
                _last_log_state.pop("dispatch", None)
                _last_log_state.pop("backoff_skip", None)
                _zero_dispatch_count = 0

            elif status in ("Running", "Started"):
                # Check for pending pods (before timeout)
                had_pending = entry.get("pending_since") is not None
                try:
                    reclaimed = _handle_pending_pods(
                        pr_name=pr_name, namespace=ns, entry=entry,
                        pending_threshold=pending_threshold,
                        max_pending_stalls=max_pending_stalls,
                    )
                except Exception as exc:
                    import traceback as _tb
                    err(f"[{pair_key}] pending check failed: {exc}")
                    _tb.print_exc(file=sys.stderr)
                    reclaimed = False
                if reclaimed:
                    if entry.get("status") == "pending":
                        try:
                            prev_state = backoff.state
                            backoff.signal_reclaim()
                            if prev_state == "normal" and backoff.state == "backing_off":
                                warn(f"Reclaims triggered backoff (next poll: {backoff.effective_interval}s)")
                        except (ValueError, TypeError) as exc:
                            warn(f"Backoff signal_reclaim failed: {exc} — ignoring")
                    del slots_busy[ns]
                    _last_log_state.pop("capacity", None)
                    _last_log_state.pop("dispatch", None)
                    _last_log_state.pop("backoff_skip", None)
                    _zero_dispatch_count = 0
                    progress["_orchestrator"] = backoff.to_dict()
                    store.save(progress)
                    continue

                if had_pending and entry.get("pending_since") is None:
                    if backoff.state != "normal":
                        backoff.signal_scheduling_success()
                        info(f"[{pair_key}] Pending→Running → backoff reset")
                        _last_log_state.pop("capacity", None)
                        _last_log_state.pop("dispatch", None)
                        _last_log_state.pop("backoff_skip", None)
                        _last_log_state.pop("backoff_level", None)
                        _zero_dispatch_count = 0
                        progress["_orchestrator"] = backoff.to_dict()
                        store.save(progress)

                # Check for timeout
                ts_result = run(
                    ["kubectl", "get", "pipelinerun", pr_name, f"-n={ns}",
                     "-o", "jsonpath={.metadata.creationTimestamp}"],
                    check=False, capture=True,
                )
                if ts_result.returncode == 0 and ts_result.stdout.strip():
                    try:
                        created = _dt.datetime.fromisoformat(
                            ts_result.stdout.strip().replace("Z", "+00:00"))
                        age_h = (_dt.datetime.now(_dt.timezone.utc) - created).total_seconds() / 3600
                        if age_h > timeout_hours:
                            retries = entry.get("retries", 0)
                            _cancel_and_delete_pipelinerun(pr_name, ns)
                            if retries < max_retries:
                                warn(f"[{pair_key}] timed out → requeue (attempt {retries + 1}/{max_retries})")
                                entry["status"] = "pending"
                                entry["retries"] = retries + 1
                            else:
                                warn(f"[{pair_key}] timed out, max retries → timed-out")
                                entry["status"] = "timed-out"
                            entry["namespace"] = None
                            entry["pending_since"] = None
                            del slots_busy[ns]
                            _last_log_state.pop("capacity", None)
                            _last_log_state.pop("dispatch", None)
                            _last_log_state.pop("backoff_skip", None)
                            _zero_dispatch_count = 0
                            store.save(progress)
                    except ValueError:
                        pass

        # ── Capacity probe ───────────────────────────────────────────────
        capacity = probe_free_gpus(gpu_resource_type=gpu_resource_type)
        if isinstance(capacity, tuple):
            free_gpus, allocatable, requested = capacity
            _cap_state = (free_gpus, allocatable, requested)
            if _pending_pairs() and _cap_state != _last_log_state.get("capacity"):
                info(f"Capacity: {free_gpus} free GPUs ({allocatable} allocatable − {requested} requested)")
                _last_log_state["capacity"] = _cap_state
            if _probe_fail_count > 0:
                info(f"Capacity probe recovered after {_probe_fail_count} failure(s)")
            _probe_fail_count = 0
            _last_probe_error = ""
        else:
            free_gpus = None
            _probe_fail_count += 1
            if capacity != _last_probe_error or _probe_fail_count % 10 == 0:
                warn(f"Capacity probe failed: {capacity} — dispatching without GPU gating")
            _last_probe_error = capacity

        # ── Backoff signals ──────────────────────────────────────────────
        if free_gpus is not None:
            backoff.last_probe_free_gpus = free_gpus
        pending = _pending_pairs()
        if free_gpus is not None and pending:
            min_cost = min(progress[k].get("gpu_cost", fallback_cost) for k in pending)
            max_cost = max(progress[k].get("gpu_cost", fallback_cost) for k in pending)
            if free_gpus < min_cost:
                prev_state = backoff.state
                backoff.signal_scarcity(free_gpus=free_gpus, min_cost=min_cost)
                if prev_state == "normal":
                    warn(f"Scarcity detected: {free_gpus} free GPUs, entering backoff (next poll: {backoff.effective_interval}s)")
                else:
                    _bo_state = (backoff.backoff_level, backoff.effective_interval)
                    if _bo_state != _last_log_state.get("backoff_level"):
                        info(f"Backoff level {backoff.backoff_level} — next poll in {backoff.effective_interval}s")
                        _last_log_state["backoff_level"] = _bo_state
            elif free_gpus >= max_cost:
                if backoff.state != "normal":
                    info(f"Backoff probe: {free_gpus} free GPUs available → resuming normal dispatch")
                    _last_log_state.pop("backoff_level", None)
                    _last_log_state.pop("backoff_skip", None)
                backoff.signal_capacity(free_gpus=free_gpus, max_cost=max_cost)

        # ── Assign pending work to free slots ────────────────────────────
        free_slots = [ns for ns in namespaces if ns not in slots_busy]
        pending = _pending_pairs()
        if free_gpus is not None and pending:
            min_cost = min(progress[k].get("gpu_cost", fallback_cost) for k in pending)
            if not backoff.should_dispatch(free_gpus=free_gpus, min_cost=min_cost):
                _skip_state = (len(pending), free_gpus)
                if _skip_state != _last_log_state.get("backoff_skip"):
                    info(f"Backoff: skipping dispatch ({len(pending)} pending, {free_gpus} free GPUs)")
                    _last_log_state["backoff_skip"] = _skip_state
                dispatchable = []
            else:
                dispatchable = _capacity_limited_pairs(
                    pending, progress,
                    free_gpus=free_gpus, default_gpu_cost=fallback_cost,
                )
                if len(dispatchable) == 0 and pending:
                    smallest = min(progress[k].get("gpu_cost", fallback_cost) for k in pending)
                    _disp_state = ("zero", len(pending), free_gpus, smallest)
                    _zero_dispatch_count += 1
                    if _disp_state != _last_log_state.get("dispatch") or _zero_dispatch_count % 10 == 0:
                        warn(f"Dispatching 0/{len(pending)} pending pairs — smallest cost ({smallest}) exceeds free GPUs ({free_gpus})")
                        _last_log_state["dispatch"] = _disp_state
                elif len(dispatchable) < len(pending):
                    _disp_state = ("cap_limited", len(dispatchable), len(pending), free_gpus)
                    if _disp_state != _last_log_state.get("dispatch"):
                        info(f"Dispatching {len(dispatchable)}/{len(pending)} pending pairs (capacity-limited: {free_gpus} free GPUs)")
                        _last_log_state["dispatch"] = _disp_state
                elif len(free_slots) < len(dispatchable):
                    _disp_state = ("slot_limited", len(free_slots), len(pending))
                    if _disp_state != _last_log_state.get("dispatch"):
                        info(f"Dispatching {len(free_slots)}/{len(pending)} pending pairs (slot-limited)")
                        _last_log_state["dispatch"] = _disp_state
        else:
            dispatchable = pending

        for ns, pair_key in zip(free_slots, dispatchable):
            hf_secret_name = setup_config.get("hf_secret_name", "hf-secret")
            ready, reasons = _check_slot_ready(ns, hf_secret_name=hf_secret_name)
            if not ready:
                warn(f"Slot {ns} not ready: {'; '.join(reasons)}")
                continue

            entry = progress[pair_key]
            pair_cost = entry.get("gpu_cost", fallback_cost)
            info(f"{pair_key} requires {pair_cost} GPUs")
            pr_meta = discovered.get(pair_key, {})
            pr_path_str = pr_meta.get("pr_path", "")
            if not pr_path_str:
                warn(f"No PipelineRun path for {pair_key}"); continue

            pr_data = yaml.safe_load(Path(pr_path_str).read_text())

            # Rewrite namespace in the PipelineRun before applying
            pr_data["metadata"]["namespace"] = ns
            for param in pr_data.get("spec", {}).get("params", []):
                if param["name"] == "namespace":
                    param["value"] = ns

            if getattr(args, "skip_teardown", False):
                params = pr_data.setdefault("spec", {}).setdefault("params", [])
                for param in params:
                    if param["name"] == "skipTeardown":
                        param["value"] = "true"
                        break
                else:
                    params.append({"name": "skipTeardown", "value": "true"})

            tf_path = None
            try:
                with _tmp.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tf:
                    yaml.dump(pr_data, tf, default_flow_style=False)
                    tf_path = tf.name
                pr_name = pr_data.get("metadata", {}).get("name", "")
                # Delete any prior completed/failed PipelineRun before re-applying
                if pr_name:
                    run(["kubectl", "delete", "pipelinerun", pr_name, f"-n={ns}",
                         "--ignore-not-found=true"],
                        check=False, capture=True)
                result = run(["kubectl", "apply", "-f", tf_path, "-n", ns],
                             check=False, capture=True)
            finally:
                if tf_path:
                    Path(tf_path).unlink(missing_ok=True)

            if result.returncode != 0:
                warn(f"[{pair_key}] kubectl apply failed: {result.stderr.strip()}")
                continue

            entry["status"] = "running"
            entry["namespace"] = ns
            entry["pending_since"] = None
            slots_busy[ns] = pair_key
            store.save(progress)
            ok(f"[{pair_key}] → {ns} ({pr_name})")
            _last_log_state.pop("capacity", None)
            _last_log_state.pop("dispatch", None)
            _last_log_state.pop("backoff_skip", None)
            _zero_dispatch_count = 0

        # Persist backoff state
        progress["_orchestrator"] = backoff.to_dict()
        store.save(progress)

        if _work_remaining() or slots_busy:
            time.sleep(backoff.effective_interval)

    # Final summary
    counts: dict[str, int] = {}
    for k, v in progress.items():
        if k in _scope:
            counts[v["status"]] = counts.get(v["status"], 0) + 1
    print()
    ok("Run complete: " + "  ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    print(f"  Progress: ConfigMap {store.configmap_name} in {primary_ns}")
    print()


def _cmd_reset(args, run_dir: Path, discovered: dict,
               namespaces: list[str] | None = None,
               setup_config: dict | None = None) -> None:
    """Reset all non-pending pairs to pending (with cluster cleanup)."""
    from pipeline.lib.progress import ConfigMapProgressStore
    primary_ns = _configmap_namespace(setup_config, namespaces)
    if not primary_ns:
        err("No namespace configured. Run setup.py first.")
        sys.exit(1)
    store = ConfigMapProgressStore(primary_ns, run_name=run_dir.name)
    progress = store.load()

    if not progress:
        info("No progress data found — nothing to reset")
        return

    _scope = _resolve_scope(progress, args)

    # Exclude pending (nothing to reset)
    actionable = {k for k in _scope
                  if progress[k].get("status") not in (None, "pending")}

    if not actionable:
        info("No pairs need reset (all pending)")
        return

    dry_run = getattr(args, "dry_run", False)
    preserve_done = getattr(args, "preserve_done_status", False)
    total_pairs = sum(1 for k in progress if _is_pair_key(k))
    info(f"Scope: {len(actionable)}/{total_pairs} pairs"
         + (" [DRY-RUN]" if dry_run else ""))

    cleaned = 0
    errors = 0
    for key in sorted(actionable):
        entry = progress[key]
        try:
            if _reset_pair(key, entry, discovered, dry_run=dry_run,
                          namespaces=namespaces,
                          preserve_done_status=preserve_done):
                cleaned += 1
            else:
                errors += 1
        except Exception as e:
            err(f"{key}: reset failed — {e}")
            errors += 1

    if not dry_run:
        store.save(progress)

    msg = f"{cleaned} pair(s) reset"
    if errors:
        msg += f" ({errors} failed — manual intervention needed)"
    ok(msg)


def _cmd_wipe(args, run_dir: Path,
              setup_config: dict | None = None) -> None:
    """Delete local result files for pairs in scope."""
    from pipeline.lib.progress import ConfigMapProgressStore
    primary_ns = _configmap_namespace(setup_config)
    if not primary_ns:
        err("No namespace configured. Run setup.py first.")
        sys.exit(1)
    store = ConfigMapProgressStore(primary_ns, run_name=run_dir.name)
    progress = store.load()

    if not progress:
        info("No progress data found — nothing to wipe")
        return

    _scope = _resolve_scope(progress, args)

    total_pairs = sum(1 for k in progress if _is_pair_key(k))
    results_dir = run_dir / "results"

    targets = []
    for key in sorted(_scope):
        entry = progress[key]
        pkg = entry.get("package", "")
        wl = entry.get("workload", "")
        if not pkg or not wl:
            warn(f"{key}: missing package/workload fields — skipping")
            continue
        target_dir = results_dir / pkg / wl
        targets.append((key, pkg, wl, target_dir))

    info(f"Scope: {len(_scope)}/{total_pairs} pairs"
         + (" [DRY-RUN]" if args.dry_run else ""))

    if args.dry_run:
        for key, pkg, wl, target_dir in targets:
            exists = target_dir.exists()
            info(f"[DRY-RUN] {key}: would delete results/{pkg}/{wl}/"
                 + (" (exists)" if exists else " (not on disk)"))
        return

    if not args.yes:
        dirs_on_disk = sum(1 for _, _, _, p in targets if p.exists())
        prompt = f"Wipe {len(targets)} pair(s) ({dirs_on_disk} with results on disk)? [y/N] "
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            info("Aborted (non-interactive — use --yes to skip confirmation)")
            return
        if answer != "y":
            info("Aborted")
            return

    wiped = 0
    errors = 0
    for key, pkg, wl, target_dir in targets:
        if target_dir.exists():
            try:
                shutil.rmtree(target_dir)
            except OSError as e:
                warn(f"{key}: failed to delete results/{pkg}/{wl}/: {e}")
                errors += 1
                continue
            ok(f"Deleted: results/{pkg}/{wl}/")
            pkg_dir = results_dir / pkg
            try:
                pkg_dir.rmdir()
            except OSError:
                pass
            wiped += 1
        else:
            info(f"{key}: no results on disk — skipped")

    msg = f"{wiped} pair(s) wiped"
    if errors:
        msg += f" ({errors} failed — check permissions)"
        warn(msg)
        sys.exit(1)
    ok(msg)


# ── Stop remote orchestrator ────────────────────────────────────────────────

from pipeline.lib.remote import JOB_NAME


def _cmd_stop(namespace: str) -> None:
    """Stop the remote orchestrator Job."""
    result = run(["kubectl", "get", "job", JOB_NAME, "-n", namespace],
                 check=False, capture=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "(NotFound)" in stderr:
            info(f"No remote orchestrator started in {namespace}")
            return
        err(f"Failed to check for orchestrator Job in {namespace}: {stderr}")
        sys.exit(1)

    result = run(["kubectl", "delete", "job", JOB_NAME, "-n", namespace,
                   "--cascade=foreground"], check=False, capture=True)
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        err(f"Failed to delete {JOB_NAME} in {namespace}"
            + (f": {detail}" if detail else ""))
        sys.exit(1)
    ok(f"Stopped {JOB_NAME} in {namespace}")


# ── Remote run ──────────────────────────────────────────────────────────────

_FAIL_FAST_REASONS = {
    "ImagePullBackOff",
    "ErrImagePull",
    "CrashLoopBackOff",
    "CreateContainerConfigError",
    "InvalidImageName",
    "RunContainerError",
    "ContainerCannotRun",
}


def _collect_run_flags(args) -> list[str]:
    """Collect run subcommand flags to forward to the in-cluster Job."""
    flags: list[str] = []
    for name in ("only", "workload", "package", "status"):
        val = getattr(args, name)
        if val is not None:
            if isinstance(val, list):
                flags.extend([f"--{name}"] + val)
            else:
                flags.extend([f"--{name}", str(val)])
    if getattr(args, "force"):
        flags.append("--force")
    if getattr(args, "skip_teardown", False):
        flags.append("--skip-teardown")
    if getattr(args, "preserve_pipelineruns", False):
        flags.append("--preserve-pipelineruns")
    _defaults = {
        "max_retries": 2,
        "poll_interval": 30,
        "gpu_resource_type": None,
        "default_gpu_cost": 1,
        "pending_threshold": 600,
        "max_pending_stalls": 10,
        "max_backoff": 600,
    }
    for attr, default in _defaults.items():
        val = getattr(args, attr)
        if val != default:
            flag = f"--{attr.replace('_', '-')}"
            flags.extend([flag, str(val)])
    return flags


def _check_existing_job(namespace: str) -> "str | None":
    """Check whether the orchestrator Job already exists.

    Returns "active" if the Job has active pods, "completed" if it exists
    but is not active, or None if the Job doesn't exist.
    """
    result = run(["kubectl", "get", "job", JOB_NAME, "-n", namespace,
                   "-o", "json"], check=False, capture=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "(NotFound)" in stderr:
            return None
        err(f"Failed to check for orchestrator Job: {stderr}")
        sys.exit(1)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        err(f"kubectl get job returned invalid JSON: {result.stdout[:200]}")
        sys.exit(1)
    if data.get("status", {}).get("active", 0) > 0:
        return "active"
    return "completed"


def _wait_for_job_pod(namespace: str, *, timeout: int = 120, poll: int = 5) -> None:
    """Poll until the orchestrator pod reaches Running or Succeeded.

    Fails fast on unrecoverable container states (ImagePullBackOff, etc.)
    and on pod phase Failed. Exits early if kubectl fails 3 times in a row.
    """
    deadline = time.time() + timeout
    consecutive_failures = 0
    last_error = ""
    while True:
        result = run(
            ["kubectl", "get", "pods",
             "-l", f"job-name={JOB_NAME}",
             "-n", namespace, "-o", "json"],
            check=False, capture=True,
        )
        if result.returncode != 0:
            consecutive_failures += 1
            last_error = (result.stderr or "").strip()
            if consecutive_failures >= 3:
                err(f"kubectl failed {consecutive_failures} times: {last_error}")
                sys.exit(1)
        else:
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    err("kubectl returned invalid JSON 3 times in a row")
                    sys.exit(1)
                warn("kubectl returned invalid JSON — retrying")
                if time.time() >= deadline:
                    err(f"Timed out waiting for {JOB_NAME} pod in {namespace}")
                    sys.exit(1)
                time.sleep(poll)
                continue
            consecutive_failures = 0
            for pod in data.get("items", []):
                phase = pod.get("status", {}).get("phase", "")
                if phase in ("Running", "Succeeded"):
                    return
                if phase == "Failed":
                    msg = pod.get("status", {}).get("message", "unknown reason")
                    err(f"Orchestrator pod failed: {msg}")
                    sys.exit(1)
                all_statuses = (pod.get("status", {}).get("initContainerStatuses", [])
                                + pod.get("status", {}).get("containerStatuses", []))
                for cs in all_statuses:
                    waiting = cs.get("state", {}).get("waiting", {})
                    reason = waiting.get("reason", "")
                    if reason in _FAIL_FAST_REASONS:
                        err(f"Pod failed: {reason} — {waiting.get('message', '')}")
                        sys.exit(1)
        if time.time() >= deadline:
            err(f"Timed out waiting for {JOB_NAME} pod in {namespace}")
            sys.exit(1)
        time.sleep(poll)


def _cmd_run_remote(args, run_dir: "Path", setup_config: dict) -> None:
    """Submit the orchestrator as an in-cluster Job."""
    from pipeline.lib.remote import (
        build_run_inputs_configmap, build_orchestrator_job,
    )

    namespaces = setup_config.get("namespaces") or [setup_config.get("namespace", "")]
    namespace = namespaces[0] if namespaces else ""
    if not namespace:
        err("No namespaces configured. Run setup.py with --namespaces.")
        sys.exit(1)

    orchestrator_image = setup_config.get("orchestrator_image")
    if not orchestrator_image:
        err("orchestrator_image not set in setup_config.json — add it before using --remote.")
        sys.exit(1)

    status = _check_existing_job(namespace)
    if status == "active":
        err(f"Orchestrator Job already running in {namespace}. Use 'deploy.py stop' first.")
        sys.exit(1)
    elif status == "completed":
        info(f"Deleting completed orchestrator Job in {namespace}")
        result = run(["kubectl", "delete", "job", JOB_NAME, "-n", namespace],
                     check=False, capture=True)
        if result.returncode != 0:
            detail = (result.stderr or "").strip()
            err(f"Failed to delete completed Job: {detail}")
            sys.exit(1)

    # Validate filter flags before building images (fail fast)
    cluster_dir = run_dir / "cluster"
    discovered = _load_pairs(cluster_dir)
    if discovered:
        progress = {k: v for k, v in discovered.items()}
        _resolve_scope(progress, args)

    _cmd_build(run_dir, namespace=namespace, skip_build=args.skip_build)

    workspace_dir = EXPERIMENT_ROOT / "workspace"
    run_name = run_dir.name

    try:
        cm = build_run_inputs_configmap(
            run_dir=run_dir, workspace_dir=workspace_dir,
            namespace=namespace, run_name=run_name,
        )
    except OSError as exc:
        err(f"{exc} — run setup.py and prepare.py first")
        sys.exit(1)
    # subprocess.run used directly because the module's run() helper doesn't
    # support stdin input, which kubectl apply -f - requires.
    info("Applying run-inputs ConfigMap")
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=json.dumps(cm), text=True, check=False, capture_output=True,
    )
    if result.returncode != 0:
        err(f"Failed to apply ConfigMap: {(result.stderr or '').strip()}")
        sys.exit(1)

    run_flags = _collect_run_flags(args)
    job = build_orchestrator_job(
        namespace=namespace, image=orchestrator_image,
        run_name=run_name, run_flags=run_flags,
        configmap_data=cm["data"],
    )
    info("Applying orchestrator Job")
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=json.dumps(job), text=True, check=False, capture_output=True,
    )
    if result.returncode != 0:
        err(f"Failed to apply Job: {(result.stderr or '').strip()}")
        sys.exit(1)

    _wait_for_job_pod(namespace)
    ok("Orchestrator pod is running")
    info(f"Tail logs: kubectl logs -f job/{JOB_NAME} -n {namespace}")


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deploy.py",
        description="sim2real deploy — Ensure images, orchestrate runs, collect results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pipeline/deploy.py build                      # Ensure all scenario images exist
  python pipeline/deploy.py run                        # Ensure images + orchestrate all pairs
  python pipeline/deploy.py run --remote               # Submit orchestrator as in-cluster Job
  python pipeline/deploy.py run --skip-build           # Orchestrate without image build
  python pipeline/deploy.py status                     # Show progress snapshot
  python pipeline/deploy.py collect                    # Pull results for completed phases
  python pipeline/deploy.py collect --skip-logs        # Collect traces only (skip large logs)
  python pipeline/deploy.py stop                         # Stop remote orchestrator Job
  python pipeline/deploy.py reset                       # Reset stalled/failed pairs
  python pipeline/deploy.py reset --dry-run             # Preview what would be reset
  python pipeline/deploy.py wipe                          # Wipe all results for current run
  python pipeline/deploy.py wipe --workload sharegpt-32   # Wipe results for one workload
  python pipeline/deploy.py wipe --dry-run                # Preview what would be wiped
  python pipeline/deploy.py pairs                       # List pairs with workloads and packages
  python pipeline/deploy.py pairs --keys-only           # Machine-readable: keys only
""",
    )
    p.add_argument("--run", metavar="NAME",
                   help="Run name (overrides current_run in setup_config.json)")
    p.add_argument("--experiment-root", metavar="PATH", dest="experiment_root",
                   help="Root of the experiment repo (default: framework directory)")

    sub = p.add_subparsers(dest="command")
    collect_p = sub.add_parser("collect", help="Pull results for completed packages")
    collect_p.add_argument("--only",     nargs="+", metavar="PAIR",
                           help="Scope to specific pair keys (comma or space-separated, wl- prefix optional)")
    collect_p.add_argument("--workload", nargs="+", metavar="NAME",
                           help="Scope to pairs matching these workloads (comma or space-separated)")
    collect_p.add_argument("--package", nargs="+", metavar="NAME",
                           help="Collect only these packages (comma or space-separated)")
    collect_p.add_argument("--skip-logs", action="store_true", dest="skip_logs",
                           help="Skip vLLM and EPP log files, collect only traces")

    status_p = sub.add_parser("status", help="Show progress of all (workload, package) pairs")
    status_p.add_argument("--only",     nargs="+", metavar="PAIR",  help="Scope to specific pair keys (comma or space-separated, wl- prefix optional)")
    status_p.add_argument("--workload", nargs="+", metavar="NAME",  help="Scope to pairs matching these workloads (comma or space-separated)")
    status_p.add_argument("--package",  nargs="+", metavar="NAME",  help="Scope to pairs matching these packages (comma or space-separated)")
    status_p.add_argument("--status",   metavar="STATE", help="Scope to pairs with this status (e.g. running, done, failed)")

    build_p = sub.add_parser("build", help="Ensure all scenario images exist (pre-flight for run)")
    build_p.add_argument("--skip-build", action="store_true", dest="skip_build",
                         help="Skip image build entirely")

    run_p = sub.add_parser("run", help="Orchestrate parallel pool execution")
    run_p.add_argument("--remote", action="store_true", default=False,
                       help="Submit orchestrator as in-cluster Job instead of running locally")
    run_p.add_argument("--skip-build", action="store_true", dest="skip_build",
                       help="Skip image build")
    run_p.add_argument("--only",         nargs="+", metavar="PAIR",  help="Scope execution to specific pair keys (comma or space-separated, wl- prefix optional)")
    run_p.add_argument("--workload",     nargs="+", metavar="NAME",  help="Scope execution to pairs matching these workloads (comma or space-separated)")
    run_p.add_argument("--package",      nargs="+", metavar="NAME",  help="Scope execution to pairs matching these packages (comma or space-separated)")
    run_p.add_argument("--status",       metavar="STATE", help="Scope execution to pairs with this status (e.g. failed, timed-out)")
    run_p.add_argument("--force",        action="store_true",
                       help="Reset non-pending pairs to pending, cleaning cluster resources for pairs with assigned namespaces")
    run_p.add_argument("--skip-teardown", action="store_true", dest="skip_teardown",
                       help="Skip teardown after PipelineRun completes (keeps namespace intact for debugging)")
    run_p.add_argument("--preserve-pipelineruns", action="store_true", dest="preserve_pipelineruns",
                       help="Do not delete PipelineRun objects after completion (keeps TaskRun logs for debugging)")
    run_p.add_argument("--max-retries",  type=int, default=2, dest="max_retries",
                       help="Max retries for timed-out pairs [2]")
    run_p.add_argument("--poll-interval", type=int, default=30, dest="poll_interval",
                       help="Seconds between status polls [30]")
    run_p.add_argument("--gpu-resource-type", default=None, dest="gpu_resource_type",
                       help="Override GPU resource name (default: derived from scenario, else nvidia.com/gpu)")
    run_p.add_argument("--default-gpu-cost", type=int, default=1, dest="default_gpu_cost",
                       help="Fallback GPU cost per pair when not derivable from scenario [1]")
    run_p.add_argument("--pending-threshold", type=int, default=600, dest="pending_threshold",
                       help="Seconds a pod may remain Pending (recoverable) before early reclaim [600]")
    run_p.add_argument("--max-pending-stalls", type=int, default=10, dest="max_pending_stalls",
                       help="Max early reclaims before marking pair stalled [10]")
    run_p.add_argument("--max-backoff", type=int, default=600, dest="max_backoff",
                       help="Maximum backoff interval in seconds during GPU scarcity [600]")

    sub.add_parser("stop", help="Stop the remote orchestrator Job")

    reset_p = sub.add_parser("reset", help="Reset all non-pending pairs to pending (with cluster cleanup)")
    reset_p.add_argument("--only",     nargs="+", metavar="PAIR",  help="Scope to specific pair keys (comma or space-separated, wl- prefix optional)")
    reset_p.add_argument("--workload", nargs="+", metavar="NAME",  help="Scope to pairs matching these workloads (comma or space-separated)")
    reset_p.add_argument("--package",  nargs="+", metavar="NAME",  help="Scope to pairs matching these packages (comma or space-separated)")
    reset_p.add_argument("--status",   metavar="STATE", help="Scope to pairs with this status")
    reset_p.add_argument("--preserve-done-status", action="store_true", dest="preserve_done_status",
                         help="Keep done pairs' status unchanged (cluster cleanup only)")
    reset_p.add_argument("--dry-run",  action="store_true", dest="dry_run",
                         help="Print what would be reset without doing it")

    wipe_p = sub.add_parser("wipe", help="Delete local result files for pairs in scope")
    wipe_p.add_argument("--only",     nargs="+", metavar="PAIR",  help="Scope to specific pair keys (comma or space-separated, wl- prefix optional)")
    wipe_p.add_argument("--workload", nargs="+", metavar="NAME",  help="Scope to pairs matching these workloads (comma or space-separated)")
    wipe_p.add_argument("--package",  nargs="+", metavar="NAME",  help="Scope to pairs matching these packages (comma or space-separated)")
    wipe_p.add_argument("--dry-run",  action="store_true", dest="dry_run",
                         help="Print what would be wiped without doing it")
    wipe_p.add_argument("--yes", "-y", action="store_true",
                         help="Skip confirmation prompt")

    pairs_p = sub.add_parser("pairs", help="List available pair keys, workloads, and packages")
    pairs_group = pairs_p.add_mutually_exclusive_group()
    pairs_group.add_argument("--keys-only", action="store_true", dest="keys_only",
                             help="Print pair keys only (one per line)")
    pairs_group.add_argument("--workloads-only", action="store_true", dest="workloads_only",
                             help="Print distinct workload names only (one per line)")
    pairs_group.add_argument("--packages-only", action="store_true", dest="packages_only",
                             help="Print distinct package names only (one per line)")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    machine_readable = (args.command == "pairs" and
                         any(getattr(args, f, False)
                             for f in ("keys_only", "workloads_only", "packages_only")))
    if not machine_readable:
        print(_c("36", "\n━━━ sim2real-deploy ━━━\n"))

    global EXPERIMENT_ROOT
    EXPERIMENT_ROOT = Path(args.experiment_root).resolve() if args.experiment_root else Path.cwd()

    setup_config = _load_setup_config()

    cmd = args.command

    if cmd == "stop":
        namespaces = [ns for ns in (setup_config.get("namespaces") or
                      [setup_config.get("namespace", "")]) if ns]
        if not namespaces:
            err("No namespaces configured. Run setup.py first.")
            sys.exit(1)
        _cmd_stop(namespace=namespaces[0])
        return

    run_name = args.run or setup_config.get("current_run", "")
    if not run_name:
        err("No run name. Use --run NAME or set current_run in setup_config.json.")
        sys.exit(1)
    run_dir = EXPERIMENT_ROOT / "workspace" / "runs" / run_name

    if not run_dir.exists():
        err(f"Run directory not found: {run_dir}")
        sys.exit(1)

    if cmd == "build":
        namespaces = setup_config.get("namespaces") or [setup_config.get("namespace", "")]
        if not namespaces or not namespaces[0]:
            err("No namespaces configured. Run setup.py with --namespaces."); sys.exit(1)
        _cmd_build(run_dir, namespace=namespaces[0],
                   skip_build=getattr(args, "skip_build", False))
        return

    if cmd == "run":
        if getattr(args, "remote", False):
            _cmd_run_remote(args, run_dir, setup_config)
        else:
            _cmd_run(args, run_dir, setup_config)
    elif cmd == "status":
        _cmd_status(args, run_dir, setup_config=setup_config)
    elif cmd == "collect":
        _cmd_collect(args, run_dir, setup_config)
    elif cmd == "reset":
        cluster_dir = run_dir / "cluster"
        discovered = _load_pairs(cluster_dir)
        namespaces = [ns for ns in (setup_config.get("namespaces") or
                      [setup_config.get("namespace", "")]) if ns]
        if not namespaces:
            warn("No namespaces in setup_config — PipelineRun deletion for done pairs may be incomplete")
        _cmd_reset(args, run_dir, discovered,
                   namespaces=namespaces or None,
                   setup_config=setup_config)
    elif cmd == "wipe":
        _cmd_wipe(args, run_dir, setup_config=setup_config)
    elif cmd == "pairs":
        cluster_dir = run_dir / "cluster"
        _cmd_pairs(cluster_dir, keys_only=args.keys_only,
                   workloads_only=args.workloads_only,
                   packages_only=args.packages_only)
    else:
        err("No subcommand specified. Use: deploy.py build | run | status | collect | stop | reset | wipe | pairs")
        sys.exit(1)


if __name__ == "__main__":
    main()
