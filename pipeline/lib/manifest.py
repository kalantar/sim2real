"""Manifest loader for sim2real pipeline (v2 schema)."""
import warnings
import yaml
from pathlib import Path


class ManifestError(Exception):
    """Manifest validation error."""


_REQUIRED_TOP = ["kind", "version", "scenario", "algorithm", "baseline", "workloads", "llm_config"]
_REQUIRED_ALGORITHM = ["source", "config"]


def load_manifest(path: "Path | str") -> dict:
    """Load and validate a v2 sim2real transfer manifest."""
    path = Path(path)
    if not path.exists():
        raise ManifestError(f"Manifest not found: {path}")

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ManifestError(f"YAML parse error in {path}: {e}") from e

    if data.get("kind") != "sim2real-transfer":
        raise ManifestError(f"Expected kind: sim2real-transfer, got: {data.get('kind')}")

    version = data.get("version")
    if version is None:
        raise ManifestError("Missing required field: version")
    if version == 1:
        raise ManifestError(
            "This is a v1 manifest. v2 is required.\n"
            "Migration: rename algorithm.policy \u2192 algorithm.config, "
            "add scenario field, move target/config to env_defaults.yaml.\n"
            "See docs/transfer/migration-v1-to-v2.md for details."
        )
    if version not in (2, 3):
        raise ManifestError(f"Unsupported manifest version: {version}")

    for field in _REQUIRED_TOP:
        if field not in data:
            raise ManifestError(f"Missing required field: {field}")

    algo = data["algorithm"]
    if not isinstance(algo, dict):
        raise ManifestError("algorithm must be a mapping")
    for f in _REQUIRED_ALGORITHM:
        if f not in algo:
            raise ManifestError(f"Missing required field: algorithm.{f}")

    bl = data["baseline"]
    if not isinstance(bl, dict):
        raise ManifestError("baseline must be a mapping")

    if version == 2:
        # v2: flat baseline.config — normalize to v3 shape for uniform downstream access
        if "config" not in bl:
            raise ManifestError("Missing required field: baseline.config")
        data["baseline"] = {
            "sim": {"config": bl["config"]},
            "real": {"config": None, "notes": ""},
        }
    else:
        # v3: baseline.sim.config required; baseline.real.* optional with defaults
        sim_section = bl.get("sim")
        if not isinstance(sim_section, dict) or "config" not in sim_section:
            raise ManifestError("Missing required field: baseline.sim.config")
        real_section = bl.get("real")
        if real_section is None:
            data["baseline"]["real"] = {"config": None, "notes": ""}
        elif not isinstance(real_section, dict):
            raise ManifestError("baseline.real must be a mapping")
        else:
            data["baseline"]["real"].setdefault("config", None)
            data["baseline"]["real"].setdefault("notes", "")

    if not isinstance(data["workloads"], list):
        raise ManifestError("workloads must be a list")
    if len(data["workloads"]) == 0:
        raise ManifestError("workloads must contain at least one path")

    # Hints section (optional)
    hints_raw = data.get("hints", {}) or {}
    hints_text = hints_raw.get("text", "") or ""
    hints_files_raw = hints_raw.get("files", []) or []
    hints_files = []
    for fpath in hints_files_raw:
        fp = Path(fpath)
        if not fp.exists():
            raise ManifestError(f"hints.files entry not found: {fpath}")
        hints_files.append({"path": str(fp), "content": fp.read_text()})
    data["hints"] = {"text": hints_text, "files": hints_files}

    # Deprecation warning for context.notes
    if data.get("context", {}).get("notes"):
        warnings.warn(
            "context.notes is deprecated; use hints.text instead. "
            "The value is currently ignored.",
            DeprecationWarning,
            stacklevel=2,
        )

    return data
