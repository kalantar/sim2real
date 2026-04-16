"""Run management: list, inspect, and switch sim2real pipeline runs."""
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


# ── Exceptions ───────────────────────────────────────────────────────────────

class RunNotFoundError(Exception):
    pass

class TranslationOutputError(Exception):
    pass

class SwitchAborted(Exception):
    """User declined to overwrite uncommitted changes."""


# ── Data types ───────────────────────────────────────────────────────────────

@dataclass
class RunSummary:
    name: str
    scenario: str
    last_phase: str
    verdict: str
    active: bool

@dataclass
class PhaseInfo:
    name: str
    status: str
    notes: str = ""
    verdict: str = ""

@dataclass
class RunDetail:
    name: str
    scenario: str
    active: bool
    phases: list   # list[PhaseInfo]
    files_created: list   # list[str]
    files_modified: list  # list[str]
    deploy_stages: dict   # dict[str, str]
    deploy_last_step: str = ""

@dataclass
class SwitchResult:
    files_written: list  # list[str]
    active_run: str


# ── Conformance helpers ───────────────────────────────────────────────────────

def _load_state(run_dir: Path) -> "dict | None":
    """Load and validate .state.json; return None if nonconforming."""
    path = run_dir / ".state.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not all(k in data for k in ("run_name", "scenario", "phases")):
        return None
    return data

def _load_metadata(run_dir: Path) -> "dict | None":
    """Load and validate run_metadata.json; return None if nonconforming."""
    path = run_dir / "run_metadata.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not all(k in data for k in ("version", "stages")):
        return None
    return data

def _last_done_phase(phases: dict) -> str:
    """Return the name of the last phase with status 'done'."""
    last = ""
    for name, info in phases.items():
        if isinstance(info, dict) and info.get("status") == "done":
            last = name
    return last

def _get_verdict(phases: dict) -> str:
    """Extract verdict string from phases (typically from gate phase)."""
    for info in phases.values():
        if isinstance(info, dict) and "verdict" in info:
            return info["verdict"]
    return ""


def list_runs(workspace_dir: Path, setup_config_path: Path) -> "list[RunSummary]":
    """Return RunSummary for each conforming run, sorted by name. Non-conforming runs are silently skipped."""
    runs_dir = workspace_dir / "runs"
    if not runs_dir.exists():
        return []

    active_run = ""
    if setup_config_path.exists():
        try:
            cfg = json.loads(setup_config_path.read_text())
            active_run = cfg.get("current_run", "")
        except (json.JSONDecodeError, OSError):
            pass

    results = []
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        state = _load_state(run_dir)
        meta = _load_metadata(run_dir)
        if state is None or meta is None:
            continue  # silently skip non-conforming
        name = state["run_name"]
        phases = state.get("phases", {})
        results.append(RunSummary(
            name=name,
            scenario=state["scenario"],
            last_phase=_last_done_phase(phases),
            verdict=_get_verdict(phases),
            active=(name == active_run),
        ))
    return results


def _phase_notes(name: str, info: dict) -> str:
    """Extract human-readable notes from a phase info dict."""
    if name == "translate":
        parts = []
        if "review_rounds" in info:
            parts.append(f"{info['review_rounds']} review rounds")
        if "consensus" in info:
            parts.append(f"consensus {info['consensus']}")
        return ", ".join(parts)
    if name == "baseline_derivation":
        if info.get("user_approved"):
            return "user approved"
    if name == "assembly":
        pkgs = info.get("packages", [])
        if pkgs:
            return f"packages: {', '.join(pkgs)}"
    return ""


def inspect_run(run_dir: Path, active_run: str = "") -> RunDetail:
    """Load full run detail. Raises RunNotFoundError if run_dir doesn't exist or is invalid."""
    if not run_dir.exists():
        raise RunNotFoundError(f"Error: run '{run_dir.name}' not found in workspace/runs/")
    state = _load_state(run_dir)
    if state is None:
        raise RunNotFoundError(f"Error: run '{run_dir.name}' has no valid .state.json")

    meta = _load_metadata(run_dir)

    phases = []
    for name, info in state.get("phases", {}).items():
        if not isinstance(info, dict):
            continue
        phases.append(PhaseInfo(
            name=name,
            status=info.get("status", ""),
            notes=_phase_notes(name, info),
            verdict=info.get("verdict", ""),
        ))

    files_created: list[str] = []
    files_modified: list[str] = []
    to_path = run_dir / "translation_output.json"
    if to_path.exists():
        try:
            to = json.loads(to_path.read_text())
            files_created = to.get("files_created") or []
            files_modified = to.get("files_modified") or []
        except (json.JSONDecodeError, OSError):
            pass

    deploy_stages: dict[str, str] = {}
    deploy_last_step = ""
    if meta:
        for stage_name, stage_info in meta.get("stages", {}).items():
            if isinstance(stage_info, dict):
                deploy_stages[stage_name] = stage_info.get("status", "")
                if stage_name == "deploy":
                    deploy_last_step = stage_info.get("last_completed_step", "")
            else:
                deploy_stages[stage_name] = str(stage_info)

    return RunDetail(
        name=state["run_name"],
        scenario=state["scenario"],
        active=(state["run_name"] == active_run),
        phases=phases,
        files_created=files_created,
        files_modified=files_modified,
        deploy_stages=deploy_stages,
        deploy_last_step=deploy_last_step,
    )


def _load_translation_output(run_dir: Path, run_name: str) -> "tuple[list[str], list[str]]":
    """Load and validate translation_output.json. Returns (files_created, files_modified)."""
    to_path = run_dir / "translation_output.json"
    if not to_path.exists():
        raise TranslationOutputError(
            f"Error: run '{run_name}' has no translation_output.json — was Phase 3 completed?"
        )
    try:
        data = json.loads(to_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        raise TranslationOutputError(
            f"Error: translation_output.json is malformed — {e}"
        )
    fc = data.get("files_created")
    fm = data.get("files_modified")
    if (not isinstance(fc, list) or not isinstance(fm, list)
            or not all(isinstance(x, str) for x in fc)
            or not all(isinstance(x, str) for x in fm)):
        raise TranslationOutputError(
            "Error: translation_output.json is malformed — expected 'files_created' and "
            "'files_modified' as lists of strings"
        )
    return fc, fm


def _git_submodule_is_dirty(submodule_dir: Path) -> bool:
    """Return True if the submodule has any uncommitted or staged changes."""
    result = subprocess.run(
        ["git", "-C", str(submodule_dir), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def _git_reset_submodule(submodule_dir: Path) -> None:
    """Discard all staged and unstaged changes in the submodule."""
    subprocess.run(
        ["git", "-C", str(submodule_dir), "restore", "--staged", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(submodule_dir), "restore", "."],
        check=True, capture_output=True,
    )


def switch_run(
    run_name: str,
    workspace_dir: Path,
    submodule_dir: Path,
    setup_config_path: Path,
    confirm_fn: "callable",
    _is_dirty: "callable | None" = None,
    _reset: "callable | None" = None,
) -> SwitchResult:
    """
    Switch the active run: reset the submodule, delete stale files from the
    previous run, copy all generated files from the target run, update setup_config.

    confirm_fn(dirty: bool) -> bool  — called when submodule has uncommitted changes;
        return True to proceed with the reset.
    _is_dirty(submodule_dir) -> bool  — injectable for tests.
    _reset(submodule_dir) -> None    — injectable for tests.

    Raises: RunNotFoundError, TranslationOutputError, ValueError, SwitchAborted, OSError.
    """
    if _is_dirty is None:
        _is_dirty = _git_submodule_is_dirty
    if _reset is None:
        _reset = _git_reset_submodule

    run_dir = workspace_dir / "runs" / run_name

    # Step 1: validate and load target run
    if not run_dir.exists():
        raise RunNotFoundError(f"Error: run '{run_name}' not found in workspace/runs/")
    if not setup_config_path.exists():
        raise RunNotFoundError("Error: workspace/setup_config.json not found")

    files_created, files_modified = _load_translation_output(run_dir, run_name)
    target_files = set(files_created + files_modified)

    # Step 2: basename collision check
    seen: set[str] = set()
    for rel_path in target_files:
        basename = Path(rel_path).name
        if basename in seen:
            raise ValueError(
                f"Error: basename collision in translation_output.json: "
                f"'{basename}' maps to multiple paths"
            )
        seen.add(basename)

    # Step 3: pre-validate all source files exist in generated/
    generated_dir = run_dir / "generated"
    missing = [Path(f).name for f in target_files
               if not (generated_dir / Path(f).name).exists()]
    if missing:
        raise ValueError(
            f"Error: missing source files in workspace/runs/{run_name}/generated/: "
            + ", ".join(missing)
        )

    # Step 4: check submodule exists
    if not submodule_dir.exists():
        raise RunNotFoundError(
            "Error: submodule directory llm-d-inference-scheduler not found"
        )

    # Step 5: load previous run's files so we can delete stale ones after reset
    prev_files: set[str] = set()
    cfg = json.loads(setup_config_path.read_text())
    prev_run_name = cfg.get("current_run", "")
    if prev_run_name and prev_run_name != run_name:
        prev_run_dir = workspace_dir / "runs" / prev_run_name
        try:
            pfc, pfm = _load_translation_output(prev_run_dir, prev_run_name)
            prev_files = set(pfc + pfm)
        except (RunNotFoundError, TranslationOutputError):
            pass  # previous run missing or malformed — skip stale cleanup

    # Step 6: confirm and reset all uncommitted/staged changes
    if _is_dirty(submodule_dir) and not confirm_fn(True):
        raise SwitchAborted()
    _reset(submodule_dir)

    # Step 7: delete files from the previous run that are not in the target run.
    # These are new/untracked files that git restore won't have cleaned up.
    for rel_path in prev_files - target_files:
        stale = submodule_dir / rel_path
        if stale.exists():
            stale.unlink()

    # Step 8: copy all generated files to their destinations in the submodule
    files_written: list[str] = []
    for rel_path in sorted(target_files):
        src = generated_dir / Path(rel_path).name
        dst = submodule_dir / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            files_written.append(rel_path)
        except OSError as e:
            raise OSError(f"Error: failed to copy {rel_path}: {e}") from e

    # Step 9: update setup_config only after all copies succeed
    cfg["current_run"] = run_name
    setup_config_path.write_text(json.dumps(cfg, indent=2))

    return SwitchResult(files_written=files_written, active_run=run_name)
