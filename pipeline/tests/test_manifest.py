"""Tests for manifest loader."""
import pytest
import yaml
from pathlib import Path

from pipeline.lib.manifest import load_manifest, ManifestError

def _write_manifest(tmp_path, data):
    p = tmp_path / "transfer.yaml"
    p.write_text(yaml.dump(data))
    return p


def test_missing_version_raises(tmp_path):
    data = {k: v for k, v in MINIMAL_V3.items() if k != "version"}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="version"):
        load_manifest(path)


def test_missing_required_field(tmp_path):
    for field in ["scenario", "baselines"]:
        data = {k: v for k, v in MINIMAL_V3.items() if k != field}
        path = _write_manifest(tmp_path, data)
        with pytest.raises(ManifestError, match=field):
            load_manifest(path)


def test_algorithms_section_entirely_optional(tmp_path):
    """Manifest without algorithms is valid (baseline-only mode)."""
    data = {k: v for k, v in MINIMAL_V3.items() if k != "algorithms"}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["algorithms"] == []


def test_optional_context_fields(tmp_path):
    data = {**MINIMAL_V3, "context": {
        "files": ["docs/mapping.md"],
    }}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["context"]["files"] == ["docs/mapping.md"]


def test_workloads_must_be_list(tmp_path):
    data = {**MINIMAL_V3, "workloads": "not_a_list.yaml"}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="workloads.*list"):
        load_manifest(path)


def test_empty_workloads_valid_standby_mode(tmp_path):
    """Empty workloads list is valid — standby mode: stack up, no benchmarks."""
    data = {**MINIMAL_V3, "workloads": []}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["workloads"] == []


def test_absent_workloads_defaults_to_empty(tmp_path):
    """Missing workloads key is valid; defaults to []."""
    data = {k: v for k, v in MINIMAL_V3.items() if k != "workloads"}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["workloads"] == []


def test_null_workloads_defaults_to_empty(tmp_path):
    """workloads: null (YAML null) is valid; defaults to []."""
    data = {**MINIMAL_V3, "workloads": None}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["workloads"] == []


def test_wrong_kind(tmp_path):
    data = {**MINIMAL_V3, "kind": "something-else"}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="kind"):
        load_manifest(path)


def test_unsupported_version(tmp_path):
    data = {**MINIMAL_V3, "version": 99}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="version"):
        load_manifest(path)


def test_v2_rejected(tmp_path):
    """Version 2 manifests are no longer accepted."""
    data = {**MINIMAL_V3, "version": 2}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="Unsupported manifest version: 2"):
        load_manifest(path)


def test_file_not_found():
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(Path("/nonexistent/transfer.yaml"))


# ── Context section ──────────────────────────────────────────────────────────

def test_context_section_optional(tmp_path):
    """Manifest without context loads cleanly; context defaults to empty."""
    path = _write_manifest(tmp_path, MINIMAL_V3)
    m = load_manifest(path)
    ctx = m.get("context", {})
    assert ctx.get("text", "") == ""
    assert ctx.get("files", []) == []


def test_context_text_loaded(tmp_path):
    data = {**MINIMAL_V3, "context": {"text": "Modify precise_prefix_cache.go"}}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["context"]["text"] == "Modify precise_prefix_cache.go"


def test_context_files_loaded(tmp_path):
    data = {**MINIMAL_V3, "context": {"files": ["docs/mapping.md"]}}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["context"]["files"] == ["docs/mapping.md"]


def test_context_rejects_unknown_keys(tmp_path):
    data = {**MINIMAL_V3, "context": {"text": "ok", "notes": "bad"}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="context.*unknown.*notes"):
        load_manifest(path)


def test_context_files_must_be_list(tmp_path):
    data = {**MINIMAL_V3, "context": {"files": "not_a_list"}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="context.files.*list"):
        load_manifest(path)


# ── v3 manifest fixtures ───────────────────────────────────────────────────

MINIMAL_V3 = {
    "kind": "sim2real-transfer",
    "version": 3,
    "scenario": "routing",
    "algorithms": [
        {
            "name": "treatment",
            "source": "sim2real_golden/routers/router_adaptive_v2.go",
            "defaults": "baseline",
        },
    ],
    "baselines": [
        {
            "name": "baseline",
            "scenario": "baseline.yaml",
            "sim": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
        },
    ],
    "workloads": ["sim2real_golden/workloads/wl1.yaml"],
    "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
        "base_image": {
            "hub": "ghcr.io/llm-d",
            "name": "llm-d-inference-scheduler",
            "tag": "v0.7.1",
        },
    },
}


def test_load_valid_v3_minimal(tmp_path):
    """v3 minimal manifest loads cleanly."""
    path = _write_manifest(tmp_path, MINIMAL_V3)
    m = load_manifest(path)
    assert len(m["baselines"]) == 1
    assert m["baselines"][0]["name"] == "baseline"
    assert len(m["algorithms"]) == 1
    assert m["algorithms"][0]["name"] == "treatment"




# ── component section ─────────────────────────────────────────────────────────

def test_component_required_when_algorithms_present(tmp_path):
    """Missing component raises when algorithms are specified."""
    data = {k: v for k, v in MINIMAL_V3.items() if k != "component"}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="component.*required.*algorithms"):
        load_manifest(path)


def test_no_algorithms_no_component_valid(tmp_path):
    """Baseline-only: no algorithms, no component → valid."""
    data = {k: v for k, v in MINIMAL_V3.items() if k not in ("algorithms", "component")}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["algorithms"] == []
    assert m.get("component") is None


def test_no_algorithms_with_component_valid(tmp_path):
    """No algorithms but component present → valid (component used for SHA tracking)."""
    data = {k: v for k, v in MINIMAL_V3.items() if k != "algorithms"}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["algorithms"] == []
    assert m["component"]["repo"] == "github.com/llm-d/llm-d-inference-scheduler"


def test_explicit_component_null_normalized(tmp_path):
    """component: null (explicit YAML null) is removed from manifest, not left as None."""
    data = {k: v for k, v in MINIMAL_V3.items() if k not in ("algorithms", "component")}
    data["component"] = None
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert "component" not in m


def test_component_must_be_mapping(tmp_path):
    """component: 'string' raises."""
    data = {**MINIMAL_V3, "component": "string"}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="component.*mapping"):
        load_manifest(path)


def test_component_repo_required(tmp_path):
    """component without repo raises."""
    data = {**MINIMAL_V3, "component": {"kind": "EndpointPickerConfig"}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="component.repo"):
        load_manifest(path)


def test_component_kind_required(tmp_path):
    """component without kind raises."""
    data = {**MINIMAL_V3, "component": {"repo": "github.com/llm-d/llm-d-inference-scheduler"}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="component.kind"):
        load_manifest(path)


def test_component_path_defaults_from_repo(tmp_path):
    """component.path defaults from last segment of repo URL."""
    path = _write_manifest(tmp_path, MINIMAL_V3)
    m = load_manifest(path)
    assert m["component"]["path"] == "llm-d-inference-scheduler"


def test_component_path_explicit(tmp_path):
    """Explicit component.path is preserved."""
    data = {**MINIMAL_V3, "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
        "path": "custom",
    }}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["component"]["path"] == "custom"


def test_component_base_image_optional(tmp_path):
    """MINIMAL_V3 without base_image is valid."""
    data = {**MINIMAL_V3, "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
    }}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert "base_image" not in m["component"]


def test_component_base_image_validates_fields(tmp_path):
    """base_image missing hub/name raises."""
    for field in ("hub", "name"):
        base_image = {"hub": "a", "name": "b", "tag": "c"}
        del base_image[field]
        data = {**MINIMAL_V3, "component": {
            "repo": "github.com/llm-d/llm-d-inference-scheduler",
            "kind": "EndpointPickerConfig",
            "base_image": base_image,
        }}
        path = _write_manifest(tmp_path, data)
        with pytest.raises(ManifestError, match=f"component.base_image.{field}"):
            load_manifest(path)


def test_component_base_image_tag_optional(tmp_path):
    """base_image without tag is valid — tag is informational only."""
    data = {**MINIMAL_V3, "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
        "base_image": {"hub": "ghcr.io/llm-d", "name": "llm-d-inference-scheduler"},
    }}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["component"]["base_image"]["hub"] == "ghcr.io/llm-d"
    assert "tag" not in m["component"]["base_image"]


def test_component_base_image_loaded(tmp_path):
    """Loading MINIMAL_V3 yields correct base_image fields."""
    path = _write_manifest(tmp_path, MINIMAL_V3)
    m = load_manifest(path)
    assert m["component"]["base_image"]["hub"] == "ghcr.io/llm-d"
    assert m["component"]["base_image"]["name"] == "llm-d-inference-scheduler"
    assert m["component"]["base_image"]["tag"] == "v0.7.1"


def test_component_build_optional(tmp_path):
    """MINIMAL_V3 without build is valid."""
    path = _write_manifest(tmp_path, MINIMAL_V3)
    m = load_manifest(path)
    assert "build" not in m["component"]


def test_component_build_defaults_commands(tmp_path):
    """component with build: {} gets commands=[]."""
    data = {**MINIMAL_V3, "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
        "build": {},
    }}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["component"]["build"]["commands"] == []


def test_component_build_commands_loaded(tmp_path):
    """Explicit commands are preserved."""
    data = {**MINIMAL_V3, "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
        "build": {"commands": [["go", "build", "./..."]]},
    }}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["component"]["build"]["commands"] == [["go", "build", "./..."]]


def test_component_build_commands_must_be_list(tmp_path):
    """build.commands as string raises."""
    data = {**MINIMAL_V3, "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
        "build": {"commands": "go build"},
    }}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="component.build.commands.*list"):
        load_manifest(path)


def test_component_build_image_validates_hub(tmp_path):
    """build.image without hub raises."""
    data = {**MINIMAL_V3, "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
        "build": {"image": {}},
    }}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="component.build.image.hub"):
        load_manifest(path)


def test_component_ref_optional(tmp_path):
    """component without ref is valid."""
    path = _write_manifest(tmp_path, MINIMAL_V3)
    m = load_manifest(path)
    assert "ref" not in m["component"]


def test_component_ref_loaded(tmp_path):
    """component.ref string is preserved."""
    data = {**MINIMAL_V3, "component": {
        **MINIMAL_V3["component"],
        "ref": "abc123def",
    }}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["component"]["ref"] == "abc123def"


def test_component_ref_must_be_string(tmp_path):
    """component.ref as non-string raises."""
    data = {**MINIMAL_V3, "component": {
        **MINIMAL_V3["component"],
        "ref": 123,
    }}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="component.ref.*string"):
        load_manifest(path)


def test_component_ref_must_be_nonempty(tmp_path):
    """component.ref as empty string raises."""
    data = {**MINIMAL_V3, "component": {
        **MINIMAL_V3["component"],
        "ref": "",
    }}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="component.ref.*non-empty"):
        load_manifest(path)


def test_build_image_defaults_hub_from_base_image(tmp_path):
    """build.image without hub inherits from base_image.hub."""
    data = {**MINIMAL_V3, "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
        "base_image": {"hub": "ghcr.io/llm-d", "name": "llm-d-inference-scheduler"},
        "build": {"image": {"name": "custom-name"}},
    }}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["component"]["build"]["image"]["hub"] == "ghcr.io/llm-d"
    assert m["component"]["build"]["image"]["name"] == "custom-name"


def test_build_image_defaults_name_from_base_image(tmp_path):
    """build.image without name inherits from base_image.name."""
    data = {**MINIMAL_V3, "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
        "base_image": {"hub": "ghcr.io/llm-d", "name": "llm-d-inference-scheduler"},
        "build": {"image": {"hub": "quay.io/me"}},
    }}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["component"]["build"]["image"]["hub"] == "quay.io/me"
    assert m["component"]["build"]["image"]["name"] == "llm-d-inference-scheduler"


def test_build_image_defaults_both_from_base_image(tmp_path):
    """build.image: {} inherits both hub and name from base_image."""
    data = {**MINIMAL_V3, "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
        "base_image": {"hub": "ghcr.io/llm-d", "name": "llm-d-inference-scheduler"},
        "build": {"image": {}},
    }}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["component"]["build"]["image"]["hub"] == "ghcr.io/llm-d"
    assert m["component"]["build"]["image"]["name"] == "llm-d-inference-scheduler"


def test_build_image_no_base_image_requires_hub(tmp_path):
    """Without base_image, build.image still requires hub."""
    data = {**MINIMAL_V3, "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
        "build": {"image": {"name": "foo"}},
    }}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="component.build.image.hub"):
        load_manifest(path)


def test_build_image_explicit_overrides_base_image(tmp_path):
    """Explicit build.image fields are not overwritten by base_image."""
    data = {**MINIMAL_V3, "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
        "base_image": {"hub": "ghcr.io/llm-d", "name": "llm-d-inference-scheduler"},
        "build": {"image": {"hub": "quay.io/me", "name": "my-image"}},
    }}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["component"]["build"]["image"]["hub"] == "quay.io/me"
    assert m["component"]["build"]["image"]["name"] == "my-image"


# ── pipeline field (optional, v3 only) ─────────────────────────────────────

def test_v3_pipeline_defaults_when_absent(tmp_path):
    """v3 without pipeline section gets defaults: name='sim2real', yaml='pipeline/pipeline.yaml'."""
    data = {k: v for k, v in MINIMAL_V3.items() if k != "pipeline"}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["pipeline"]["name"] == "sim2real"
    assert m["pipeline"]["yaml"] == "pipeline/pipeline.yaml"


def test_v3_pipeline_explicit_values(tmp_path):
    """v3 with explicit pipeline.name and pipeline.yaml preserves values."""
    data = {**MINIMAL_V3, "pipeline": {"name": "custom-pipe", "yaml": "custom/my-pipeline.yaml"}}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["pipeline"]["name"] == "custom-pipe"
    assert m["pipeline"]["yaml"] == "custom/my-pipeline.yaml"


def test_v3_pipeline_partial_name_only(tmp_path):
    """v3 with only pipeline.name gets default yaml."""
    data = {**MINIMAL_V3, "pipeline": {"name": "other"}}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["pipeline"]["name"] == "other"
    assert m["pipeline"]["yaml"] == "pipeline/pipeline.yaml"


def test_v3_pipeline_partial_yaml_only(tmp_path):
    """v3 with only pipeline.yaml gets default name."""
    data = {**MINIMAL_V3, "pipeline": {"yaml": "other/pipe.yaml"}}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["pipeline"]["name"] == "sim2real"
    assert m["pipeline"]["yaml"] == "other/pipe.yaml"


def test_v3_pipeline_not_mapping_raises(tmp_path):
    """pipeline must be a mapping if present."""
    data = {**MINIMAL_V3, "pipeline": "not-a-dict"}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="pipeline must be a mapping"):
        load_manifest(path)


# ── Multi-baseline (v3 extension) ─────────────────────────────────────────

MULTI_BASELINE_V3 = {
    "kind": "sim2real-transfer",
    "version": 3,
    "scenario": "routing",
    "baselines": [
        {"name": "b1", "scenario": "baseline_1.yaml"},
        {"name": "b2", "scenario": "baseline_2.yaml"},
    ],
    "workloads": ["sim2real_golden/workloads/wl1.yaml"],
    "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
    },
}


def test_baselines_list_loaded(tmp_path):
    """v3 with baselines: list loads and normalizes each entry."""
    path = _write_manifest(tmp_path, MULTI_BASELINE_V3)
    m = load_manifest(path)
    assert "baselines" in m
    assert len(m["baselines"]) == 2
    assert m["baselines"][0]["name"] == "b1"
    assert m["baselines"][1]["name"] == "b2"


def test_baselines_must_be_list(tmp_path):
    data = {**MULTI_BASELINE_V3, "baselines": "not-a-list"}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="baselines.*list"):
        load_manifest(path)


def test_baselines_entry_requires_name(tmp_path):
    data = {**MULTI_BASELINE_V3, "baselines": [{"scenario": "x.yaml"}]}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="baselines.*name"):
        load_manifest(path)


def test_baselines_entry_requires_scenario(tmp_path):
    data = {**MULTI_BASELINE_V3, "baselines": [{"name": "b1"}]}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="baselines.*scenario"):
        load_manifest(path)


def test_baselines_name_must_be_valid(tmp_path):
    data = {**MULTI_BASELINE_V3, "baselines": [{"name": "Bad_Name!", "scenario": "x.yaml"}]}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="invalid"):
        load_manifest(path)


def test_baselines_name_rejects_hyphens(tmp_path):
    data = {**MULTI_BASELINE_V3, "baselines": [{"name": "my-algo", "scenario": "x.yaml"}]}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="invalid"):
        load_manifest(path)


def test_baselines_duplicate_name_raises(tmp_path):
    data = {**MULTI_BASELINE_V3, "baselines": [
        {"name": "b1", "scenario": "x.yaml"},
        {"name": "b1", "scenario": "y.yaml"},
    ]}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="duplicate.*b1"):
        load_manifest(path)


def test_baselines_with_defaults_field(tmp_path):
    data = {**MULTI_BASELINE_V3, "baselines": [
        {"name": "b1", "scenario": "x.yaml", "defaults": "defaults.yaml"},
    ]}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["baselines"][0]["defaults"] == "defaults.yaml"


def test_algorithms_list_loaded(tmp_path):
    data = {**MULTI_BASELINE_V3, "algorithms": [
        {"name": "ac1", "source": "algo.go", "scenario": "treatment.yaml", "defaults": "b1"},
    ]}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert len(m["algorithms"]) == 1
    assert m["algorithms"][0]["name"] == "ac1"
    assert m["algorithms"][0]["defaults"] == "b1"


def test_no_algorithm_no_algorithms_is_baseline_only(tmp_path):
    data = {k: v for k, v in MULTI_BASELINE_V3.items() if k != "algorithms"}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m.get("algorithms", []) == []
