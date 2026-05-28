"""Manifest loader for sim2real pipeline (v3 schema)."""
import re
import yaml
from pathlib import Path


class ManifestError(Exception):
    """Manifest validation error."""


_PACKAGE_NAME_RE = re.compile(r'^[a-z0-9]{1,20}$')


def _validate_package_name(name: str, context: str) -> None:
    if not _PACKAGE_NAME_RE.match(name):
        raise ManifestError(
            f"{context} name '{name}' is invalid "
            "(lowercase alphanumeric only, 1-20 chars, no hyphens or underscores)"
        )


_REQUIRED_TOP = ["kind", "version", "scenario"]


def load_manifest(path: "Path | str") -> dict:
    """Load and validate a sim2real transfer manifest."""
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
    if version != 3:
        raise ManifestError(f"Unsupported manifest version: {version}")

    for field in _REQUIRED_TOP:
        if field not in data:
            raise ManifestError(f"Missing required field: {field}")

    # Normalize workloads: absent/null → [] (standby mode — stack up, no benchmarks)
    wl = data.get("workloads")
    if wl is None:
        data["workloads"] = []
    elif not isinstance(wl, list):
        raise ManifestError("workloads must be a list")

    # ── Validate list-form sections ─────────────────────────────────────────
    if "baselines" not in data:
        raise ManifestError("Missing required field: baselines")
    bls = data["baselines"]
    if not isinstance(bls, list):
        raise ManifestError("baselines must be a list")
    seen_names: set[str] = set()
    for i, entry in enumerate(bls):
        if not isinstance(entry, dict):
            raise ManifestError(f"baselines[{i}] must be a mapping")
        if "name" not in entry:
            raise ManifestError(f"baselines[{i}] missing required field: name")
        if "scenario" not in entry:
            raise ManifestError(f"baselines[{i}] missing required field: scenario")
        _validate_package_name(entry["name"], f"baselines[{i}]")
        if entry["name"] in seen_names:
            raise ManifestError(f"baselines: duplicate name '{entry['name']}'")
        seen_names.add(entry["name"])

    if "algorithms" in data:
        algos = data["algorithms"]
        if not isinstance(algos, list):
            raise ManifestError("algorithms must be a list")
        seen_algo_names: set[str] = set()
        for i, entry in enumerate(algos):
            if not isinstance(entry, dict):
                raise ManifestError(f"algorithms[{i}] must be a mapping")
            for f in ("name", "source", "defaults"):
                if f not in entry:
                    raise ManifestError(f"algorithms[{i}] missing required field: {f}")
            _validate_package_name(entry["name"], f"algorithms[{i}]")
            if entry["name"] in seen_algo_names:
                raise ManifestError(f"algorithms: duplicate name '{entry['name']}'")
            seen_algo_names.add(entry["name"])
    else:
        data["algorithms"] = []

    # Cross-reference check: algorithm.defaults must name an existing baseline
    baseline_names = {bl["name"] for bl in data["baselines"]}
    for i, algo in enumerate(data["algorithms"]):
        if algo["defaults"] not in baseline_names:
            raise ManifestError(
                f"algorithms[{i}].defaults references unknown baseline "
                f"'{algo['defaults']}'. Available: {sorted(baseline_names)}"
            )

    # Cross-collision check: all package names must be globally unique
    all_names = [bl["name"] for bl in data["baselines"]] + [a["name"] for a in data["algorithms"]]
    if len(all_names) != len(set(all_names)):
        dupes = [n for n in all_names if all_names.count(n) > 1]
        raise ManifestError(
            f"Package names must be globally unique across baselines and algorithms; "
            f"duplicates: {sorted(set(dupes))}"
        )

    # Context section (optional): only 'text' and 'files' are valid keys
    ctx_raw = data.get("context", {}) or {}
    _valid_context_keys = {"text", "files"}
    unknown = set(ctx_raw.keys()) - _valid_context_keys
    if unknown:
        raise ManifestError(
            f"context contains unknown keys: {sorted(unknown)}. "
            f"Valid keys: text, files"
        )
    ctx_text = ctx_raw.get("text", "") or ""
    ctx_files = ctx_raw.get("files", []) or []
    if not isinstance(ctx_files, list):
        raise ManifestError("context.files must be a list")
    data["context"] = {"text": ctx_text, "files": ctx_files}

    _validate_v3_fields(data)

    return data


def _validate_v3_fields(data: dict) -> None:
    """Validate and apply defaults for v3-specific fields."""

    # component (required only when algorithms are present)
    component = data.get("component")
    has_algorithms = bool(data.get("algorithms"))

    if component is None:
        if has_algorithms:
            raise ManifestError(
                "component is required when algorithms are specified"
            )
        # Remove explicit null so downstream .get("component", {}) returns {} not None
        data.pop("component", None)
    elif not isinstance(component, dict):
        raise ManifestError("component must be a mapping")
    else:
        # component.repo (required)
        repo = component.get("repo")
        if not repo or not isinstance(repo, str):
            raise ManifestError("Missing required field: component.repo")

        # component.kind (required)
        kind = component.get("kind")
        if not kind or not isinstance(kind, str):
            raise ManifestError("Missing required field: component.kind")

        # component.path (optional, defaults from last segment of repo)
        if "path" not in component:
            component["path"] = repo.rstrip("/").rsplit("/", 1)[-1]

        # component.ref (optional)
        ref = component.get("ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref.strip():
                raise ManifestError("component.ref must be a non-empty string")

        # component.base_image (optional)
        base_image = component.get("base_image")
        if base_image is not None:
            if not isinstance(base_image, dict):
                raise ManifestError("component.base_image must be a mapping")
            for f in ("hub", "name"):
                if f not in base_image:
                    raise ManifestError(f"Missing required field: component.base_image.{f}")

        # component.build (optional)
        build = component.get("build")
        if build is not None:
            if not isinstance(build, dict):
                raise ManifestError("component.build must be a mapping")
            build.setdefault("commands", [])
            cmds = build["commands"]
            if not isinstance(cmds, list):
                raise ManifestError("component.build.commands must be a list")
            build_image = build.get("image")
            if build_image is not None:
                if not isinstance(build_image, dict):
                    raise ManifestError("component.build.image must be a mapping")
                if base_image is not None:
                    build_image.setdefault("hub", base_image["hub"])
                    build_image.setdefault("name", base_image["name"])
                if "hub" not in build_image:
                    raise ManifestError("Missing required field: component.build.image.hub")

    # pipeline (optional, defaults applied)
    pipeline = data.get("pipeline")
    if pipeline is None:
        data["pipeline"] = {"name": "sim2real", "yaml": "pipeline/pipeline.yaml"}
    elif not isinstance(pipeline, dict):
        raise ManifestError("pipeline must be a mapping")
    else:
        pipeline.setdefault("name", "sim2real")
        pipeline.setdefault("yaml", "pipeline/pipeline.yaml")
    pipeline = data["pipeline"]
    if not isinstance(pipeline["name"], str) or not pipeline["name"].strip():
        raise ManifestError("pipeline.name must be a non-empty string")
    if not isinstance(pipeline["yaml"], str) or not pipeline["yaml"].strip():
        raise ManifestError("pipeline.yaml must be a non-empty string")
    if Path(pipeline["yaml"]).is_absolute():
        raise ManifestError(
            f"pipeline.yaml must be a relative path, got: {pipeline['yaml']}"
        )
