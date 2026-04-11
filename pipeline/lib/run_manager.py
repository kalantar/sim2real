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
