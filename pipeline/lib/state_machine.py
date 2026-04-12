"""Phase state machine with JSON persistence."""
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class StateMachine:
    """Tracks prepare.py phase progress in .state.json."""

    def __init__(self, run_name: str, scenario: str, run_dir: "Path | str"):
        self.run_name = run_name
        self.scenario = scenario
        self._run_dir = Path(run_dir)
        self._phases: dict = {}
        self._save()

    @classmethod
    def load(cls, run_dir: "Path | str") -> "StateMachine":
        path = Path(run_dir) / ".state.json"
        if not path.exists():
            raise FileNotFoundError(f"No state file: {path}")
        data = json.loads(path.read_text())
        inst = object.__new__(cls)
        inst.run_name = data["run_name"]
        inst.scenario = data["scenario"]
        inst._run_dir = Path(run_dir)
        inst._phases = data.get("phases", {})
        return inst

    def is_done(self, phase: str) -> bool:
        return self._phases.get(phase, {}).get("status") == "done"

    def mark_done(self, phase: str, **metadata):
        self._phases[phase] = {
            "status": "done",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **metadata,
        }
        self._save()

    def update(self, phase: str, **kwargs):
        """Update fields in an existing phase without changing other fields or status."""
        if phase not in self._phases:
            self._phases[phase] = {}
        self._phases[phase].update(kwargs)
        self._save()

    def reset(self, phase: str):
        self._phases.pop(phase, None)
        self._save()

    def get_phase(self, phase: str) -> dict:
        return dict(self._phases.get(phase, {}))

    def increment(self, phase: str, key: str):
        if phase not in self._phases:
            self._phases[phase] = {}
        self._phases[phase][key] = self._phases[phase].get(key, 0) + 1
        self._save()

    def _save(self):
        self._run_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "run_name": self.run_name,
            "scenario": self.scenario,
            "phases": self._phases,
        }
        path = self._run_dir / ".state.json"
        fd, tmp = tempfile.mkstemp(dir=self._run_dir, suffix=".tmp")
        try:
            with open(fd, "w") as f:
                json.dump(data, f, indent=2)
            Path(tmp).replace(path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
