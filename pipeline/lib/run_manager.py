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
