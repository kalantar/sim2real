"""Deep-merge logic for Tekton values.yaml generation.

Extracted from tools/transfer_cli.py merge-values subcommand.
"""
import copy
import sys
from pathlib import Path

import yaml


# ── Three-tier list merge strategy ───────────────────────────────────────────

_LIST_KEY_CANDIDATES = ("name", "mountPath", "containerPort")


def _detect_list_key(base_list: list, overlay_list: list):
    """Return the first candidate key present in ALL dicts in both lists, or None."""
    all_items = base_list + overlay_list
    if not all_items:
        return None
    for candidate in _LIST_KEY_CANDIDATES:
        if all(candidate in d for d in all_items):
            return candidate
    return None


def _merge_lists(base_list: list, overlay_list: list) -> list:
    """Merge two lists using three-tier strategy.

    Tier 1: either list contains non-dict items → overlay replaces base.
    Tier 2: all-dict lists with a common key field → merge by key.
    Tier 3: all-dict lists without a common key → positional (index-based) merge.

    Empty overlay → returns [] (explicit clear). Empty base → returns copy of overlay.
    """
    if not overlay_list:
        return []
    if not base_list:
        return copy.deepcopy(overlay_list)

    # Tier 1: any non-dict item → replace
    if not (all(isinstance(x, dict) for x in base_list)
            and all(isinstance(x, dict) for x in overlay_list)):
        return copy.deepcopy(overlay_list)

    # Tier 2: named-key merge
    key_field = _detect_list_key(base_list, overlay_list)
    if key_field is not None:
        overlay_by_key = {item[key_field]: item for item in overlay_list}
        result = []
        seen_keys = set()
        for bitem in base_list:
            k = bitem[key_field]
            seen_keys.add(k)
            if k in overlay_by_key:
                result.append(_deep_merge(bitem, overlay_by_key[k]))
            else:
                result.append(copy.deepcopy(bitem))
        for oitem in overlay_list:
            if oitem[key_field] not in seen_keys:
                result.append(copy.deepcopy(oitem))
        return result

    # Tier 3: positional merge — surplus from either side preserved
    result = []
    for i in range(max(len(base_list), len(overlay_list))):
        if i < len(base_list) and i < len(overlay_list):
            result.append(_deep_merge(base_list[i], overlay_list[i]))
        elif i < len(base_list):
            result.append(copy.deepcopy(base_list[i]))
        else:
            result.append(copy.deepcopy(overlay_list[i]))
    return result


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Deep-merge overlay onto base. Dict keys merged recursively.

    Lists of dicts are merged by named key or positional index (see _merge_lists).
    Lists of scalars are replaced entirely. Returns a new dict (deep copy).
    """
    result = copy.deepcopy(base)
    for key, oval in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(oval, dict):
            result[key] = _deep_merge(result[key], oval)
        elif key in result and isinstance(result[key], list) and isinstance(oval, list):
            result[key] = _merge_lists(result[key], oval)
        else:
            result[key] = copy.deepcopy(oval)
    return result


# ── Post-merge transformations ────────────────────────────────────────────────

def _flatten_gaie_shared(merged: dict) -> dict:
    """Flatten gaie.shared.helmValues into each phase's helmValues, inject EPP images,
    and remove the gaie.shared and gaie.epp_image keys from output.

    For each phase in ['baseline', 'treatment']:
      1. Deep-merge gaie.shared.helmValues (base) with gaie.<phase>.helmValues (overlay)
      2. Inject inferenceExtension.image from gaie.epp_image:
           - baseline/noise: use epp_image.upstream
           - treatment: use epp_image.build tag if set; fall back to upstream otherwise
             (treatment tag is set by build-push-epp; upstream fallback allows pipeline
             testing without a custom build)
    Then delete gaie.shared and gaie.epp_image from the result.

    Returns the modified merged dict (modified in place and also returned).
    """
    gaie = merged.get("stack", {}).get("gaie", {})
    shared = gaie.get("shared", {})
    shared_helm = shared.get("helmValues", {})
    epp_image = gaie.get("epp_image", {})
    upstream_img = epp_image.get("upstream", {})
    build_img = epp_image.get("build", {})

    for phase in ["baseline", "treatment"]:
        if phase not in gaie:
            continue
        phase_helm = gaie[phase].get("helmValues", {})
        gaie[phase]["helmValues"] = _deep_merge(shared_helm, phase_helm)

        # Inject EPP image reference into inferenceExtension.image if epp_image is configured
        if upstream_img:
            if phase == "treatment" and build_img.get("hub") and build_img.get("tag"):
                img = {"hub": build_img["hub"], "name": build_img["name"],
                       "tag": build_img["tag"]}
                if build_img.get("pullPolicy") is not None:
                    img["pullPolicy"] = build_img["pullPolicy"]
            else:
                img = {"hub": upstream_img["hub"], "name": upstream_img["name"],
                       "tag": upstream_img["tag"]}
                if upstream_img.get("pullPolicy") is not None:
                    img["pullPolicy"] = upstream_img["pullPolicy"]
            ie = gaie[phase]["helmValues"].setdefault("inferenceExtension", {})
            # Deep-merge so pullPolicy from epp_image is applied even when image is pre-injected
            ie["image"] = _deep_merge(img, ie.get("image", {}))

    gaie.pop("shared", None)
    gaie.pop("epp_image", None)

    return merged


def _apply_vllm_image_override(merged: dict) -> dict:
    """Apply stack.model.vllm_image override to decode.containers[0].image.

    If stack.model.vllm_image is set, replaces decode.containers[0].image with that
    value and removes the vllm_image key from the output. If not set, no-op.
    """
    model = merged.get("stack", {}).get("model", {})
    override = model.get("vllm_image")
    if not override:
        return merged

    containers = (
        model.get("helmValues", {})
        .get("decode", {})
        .get("containers")
    )
    if containers and isinstance(containers, list) and len(containers) > 0:
        containers[0]["image"] = override

    model.pop("vllm_image", None)
    return merged


def _apply_request_multiplier(merged: dict) -> dict:
    """Scale num_requests in each observe.workloads[].spec by observe.request_multiplier.

    If observe.request_multiplier is present and > 1, parses each workload's spec
    string as YAML, multiplies num_requests (if present), and re-serializes.
    The request_multiplier key is stripped from the output regardless.

    Specs that fail YAML parsing are left unchanged (warning emitted to stderr).
    Specs without a num_requests field are left unchanged.
    """
    observe = merged.get("observe", {})
    multiplier = observe.pop("request_multiplier", None)

    if multiplier is None:
        return merged

    if not isinstance(multiplier, (int, float)):
        print(
            f"ERROR: observe.request_multiplier must be a number, "
            f"got {type(multiplier).__name__!r} (value: {multiplier!r}).",
            file=sys.stderr,
        )
        return merged

    if multiplier <= 1:
        return merged

    workloads = observe.get("workloads", [])
    for wl in workloads:
        spec_str = wl.get("spec")
        if not spec_str or not isinstance(spec_str, str):
            continue
        try:
            spec_data = yaml.safe_load(spec_str)
        except yaml.YAMLError:
            print(
                f"WARNING: could not parse spec YAML for workload "
                f"'{wl.get('name', '?')}', skipping request_multiplier scaling.",
                file=sys.stderr,
            )
            continue
        if not isinstance(spec_data, dict):
            print(
                f"WARNING: workload '{wl.get('name', '?')}' spec parsed to "
                f"{type(spec_data).__name__!r} (expected dict), "
                f"skipping request_multiplier scaling.",
                file=sys.stderr,
            )
            continue
        if "num_requests" in spec_data:
            raw = spec_data["num_requests"]
            if not isinstance(raw, (int, float)):
                print(
                    f"WARNING: workload '{wl.get('name', '?')}' num_requests is not a number "
                    f"(got {type(raw).__name__!r}: {raw!r}), "
                    f"skipping request_multiplier scaling.",
                    file=sys.stderr,
                )
                continue
            spec_data["num_requests"] = int(round(raw * multiplier))
            try:
                wl["spec"] = yaml.dump(spec_data, default_flow_style=False, sort_keys=False)
            except yaml.YAMLError as e:
                print(
                    f"WARNING: could not re-serialize spec YAML for workload "
                    f"'{wl.get('name', '?')}' after scaling, skipping: {e}",
                    file=sys.stderr,
                )

    return merged


# ── Public API ────────────────────────────────────────────────────────────────

def merge_values(
    env_path: Path,
    alg_path: Path,
    out_path: Path,
    scenario: str | None = None,
) -> None:
    """Deep-merge env_defaults and algorithm_values into values.yaml.

    In pipeline usage, scenario is always provided (both prepare.py and
    deploy.py always pass a scenario). The parameter is technically optional
    to allow standalone use without scenario resolution.

    Raises:
        FileNotFoundError: if env_path or alg_path does not exist
        yaml.YAMLError: if either input file fails to parse
        ValueError: if scenario is specified but not found in env_defaults
        OSError: if writing out_path fails
    """
    env_data = yaml.safe_load(env_path.read_text()) or {}
    alg_data = yaml.safe_load(alg_path.read_text()) or {}

    if scenario is not None:
        common = env_data.get("common", {})
        scenarios = env_data.get("scenarios", {})
        if scenario not in scenarios:
            raise ValueError(
                f"scenario '{scenario}' not found in env_defaults. "
                f"Available: {list(scenarios.keys())}"
            )
        env_data = _deep_merge(common, scenarios[scenario])
        for key in ("target", "build", "config"):
            env_data.pop(key, None)

    merged = _deep_merge(env_data, alg_data)

    # Strip pipeline-only keys except sleepDuration (consumed by Tekton templates)
    pipeline_config = merged.pop("pipeline", {})
    template_keys = {k: v for k, v in pipeline_config.items() if k == "sleepDuration"}
    if template_keys:
        merged["pipeline"] = template_keys

    merged = _flatten_gaie_shared(merged)
    merged = _apply_vllm_image_override(merged)
    merged = _apply_request_multiplier(merged)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.dump(merged, default_flow_style=False, sort_keys=False))
