"""Tests for v2 manifest loader."""
import pytest
import yaml
from pathlib import Path

from pipeline.lib.manifest import load_manifest, ManifestError

MINIMAL_V2 = {
    "kind": "sim2real-transfer",
    "version": 2,
    "scenario": "routing",
    "algorithm": {
        "source": "sim2real_golden/routers/router_adaptive_v2.go",
        "config": "sim2real_golden/routers/policy_adaptive_v2.yaml",
    },
    "baseline": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
    "workloads": ["sim2real_golden/workloads/wl1.yaml"],
    "llm_config": "sim2real_golden/llm_config.yaml",
}


def _write_manifest(tmp_path, data):
    p = tmp_path / "transfer.yaml"
    p.write_text(yaml.dump(data))
    return p


def test_load_valid_v2(tmp_path):
    path = _write_manifest(tmp_path, MINIMAL_V2)
    m = load_manifest(path)
    assert m["scenario"] == "routing"
    assert m["algorithm"]["source"].endswith(".go")


def test_v1_raises_migration_error(tmp_path):
    v1 = {"kind": "sim2real-transfer", "version": 1, "algorithm": {"experiment_dir": "x"}}
    path = _write_manifest(tmp_path, v1)
    with pytest.raises(ManifestError, match="v1.*v2"):
        load_manifest(path)


def test_missing_version_raises(tmp_path):
    data = {k: v for k, v in MINIMAL_V2.items() if k != "version"}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="version"):
        load_manifest(path)


def test_missing_required_field(tmp_path):
    for field in ["scenario", "algorithm", "baseline", "workloads", "llm_config"]:
        data = {k: v for k, v in MINIMAL_V2.items() if k != field}
        path = _write_manifest(tmp_path, data)
        with pytest.raises(ManifestError, match=field):
            load_manifest(path)


def test_missing_algorithm_source(tmp_path):
    data = {**MINIMAL_V2, "algorithm": {"config": "x.yaml"}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="algorithm.source"):
        load_manifest(path)


def test_missing_algorithm_config(tmp_path):
    data = {**MINIMAL_V2, "algorithm": {"source": "x.go"}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="algorithm.config"):
        load_manifest(path)


def test_missing_baseline_config(tmp_path):
    data = {**MINIMAL_V2, "baseline": {}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="baseline.config"):
        load_manifest(path)


def test_optional_context_fields(tmp_path):
    data = {**MINIMAL_V2, "context": {
        "files": ["docs/mapping.md"],
        "notes": "Use regime detection pattern",
    }}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["context"]["notes"] == "Use regime detection pattern"
    assert len(m["context"]["files"]) == 1


def test_workloads_must_be_list(tmp_path):
    data = {**MINIMAL_V2, "workloads": "not_a_list.yaml"}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="workloads.*list"):
        load_manifest(path)


def test_workloads_must_be_nonempty(tmp_path):
    data = {**MINIMAL_V2, "workloads": []}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="workloads.*at least"):
        load_manifest(path)


def test_wrong_kind(tmp_path):
    data = {**MINIMAL_V2, "kind": "something-else"}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="kind"):
        load_manifest(path)


def test_unsupported_version(tmp_path):
    data = {**MINIMAL_V2, "version": 99}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="version"):
        load_manifest(path)


def test_file_not_found():
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(Path("/nonexistent/transfer.yaml"))


# ── Hints section ─────────────────────────────────────────────────────────────

def test_hints_section_optional(tmp_path):
    """Manifest without hints loads cleanly; hints defaults to empty."""
    path = _write_manifest(tmp_path, MINIMAL_V2)
    m = load_manifest(path)
    hints = m.get("hints", {})
    assert hints.get("text", "") == ""
    assert hints.get("files", []) == []


def test_hints_text_loaded(tmp_path):
    data = {**MINIMAL_V2, "hints": {"text": "Modify precise_prefix_cache.go"}}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["hints"]["text"] == "Modify precise_prefix_cache.go"


def test_hints_files_contents_embedded(tmp_path):
    hint_file = tmp_path / "hint.md"
    hint_file.write_text("# Transfer hint\nRewrite scorer")
    data = {**MINIMAL_V2, "hints": {"files": [str(hint_file)]}}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert len(m["hints"]["files"]) == 1
    assert m["hints"]["files"][0]["path"] == str(hint_file)
    assert "Rewrite scorer" in m["hints"]["files"][0]["content"]


def test_hints_file_not_found_raises(tmp_path):
    data = {**MINIMAL_V2, "hints": {"files": ["/nonexistent/hint.md"]}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="hints.files"):
        load_manifest(path)


def test_context_notes_deprecated_warns(tmp_path):
    data = {**MINIMAL_V2, "context": {"notes": "old style note", "files": []}}
    path = _write_manifest(tmp_path, data)
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        m = load_manifest(path)
    dep_warnings = [warning for warning in w if issubclass(warning.category, DeprecationWarning)]
    assert dep_warnings, "No DeprecationWarning emitted"
    texts = [str(warning.message) for warning in dep_warnings]
    assert any("context.notes" in t and "deprecated" in t for t in texts)
    # Value is ignored (not migrated to hints.text)
    assert m.get("hints", {}).get("text", "") == ""


# ── v3 manifest fixtures ───────────────────────────────────────────────────

MINIMAL_V3 = {
    "kind": "sim2real-transfer",
    "version": 3,
    "scenario": "routing",
    "algorithm": {
        "source": "sim2real_golden/routers/router_adaptive_v2.go",
        "config": "sim2real_golden/routers/policy_adaptive_v2.yaml",
    },
    "baseline": {
        "sim": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
    },
    "workloads": ["sim2real_golden/workloads/wl1.yaml"],
    "llm_config": "sim2real_golden/llm_config.yaml",
}


def test_load_valid_v3_minimal(tmp_path):
    """v3 with just baseline.sim.config loads cleanly."""
    path = _write_manifest(tmp_path, MINIMAL_V3)
    m = load_manifest(path)
    assert m["baseline"]["sim"]["config"] == "sim2real_golden/routers/policy_baseline_211.yaml"
    assert m["baseline"]["real"]["config"] is None
    assert m["baseline"]["real"]["notes"] == ""


def test_v3_with_real_config_and_notes(tmp_path):
    """v3 with baseline.real.config + notes loads and preserves both."""
    data = {
        **MINIMAL_V3,
        "baseline": {
            "sim": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
            "real": {
                "config": "sim2real_golden/routers/baseline_epp_template.yaml",
                "notes": "Use EndpointPickerConfig.Scorers[]",
            },
        },
    }
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["baseline"]["real"]["config"] == "sim2real_golden/routers/baseline_epp_template.yaml"
    assert "EndpointPickerConfig" in m["baseline"]["real"]["notes"]


def test_v3_missing_sim_config_raises(tmp_path):
    """v3 without baseline.sim.config raises ManifestError."""
    data = {**MINIMAL_V3, "baseline": {"real": {"config": "x.yaml"}}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="baseline.sim.config"):
        load_manifest(path)


def test_v3_missing_sim_section_raises(tmp_path):
    """v3 baseline without sim key raises ManifestError."""
    data = {**MINIMAL_V3, "baseline": {}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="baseline.sim.config"):
        load_manifest(path)


def test_v3_real_section_entirely_optional(tmp_path):
    """v3 without baseline.real at all is valid; defaults applied."""
    data = {**MINIMAL_V3}  # no baseline.real
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["baseline"]["real"] == {"config": None, "notes": ""}


def test_v3_real_partial_defaults_applied(tmp_path):
    """v3 with baseline.real present but missing notes gets default."""
    data = {
        **MINIMAL_V3,
        "baseline": {
            "sim": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
            "real": {"config": "x.yaml"},  # no notes
        },
    }
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["baseline"]["real"]["notes"] == ""


def test_v2_normalizes_to_v3_shape(tmp_path):
    """v2 manifest: baseline.config is mapped to baseline.sim.config in output."""
    path = _write_manifest(tmp_path, MINIMAL_V2)
    m = load_manifest(path)
    assert "sim" in m["baseline"]
    assert m["baseline"]["sim"]["config"] == "sim2real_golden/routers/policy_baseline_211.yaml"
    assert m["baseline"]["real"]["config"] is None
    assert m["baseline"]["real"]["notes"] == ""


def test_v2_baseline_config_missing_raises(tmp_path):
    """v2 without baseline.config raises ManifestError."""
    data = {**MINIMAL_V2, "baseline": {}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="baseline.config"):
        load_manifest(path)


def test_v3_accepted_alongside_v2(tmp_path):
    """Version 3 is accepted; version 2 is still accepted."""
    for ver, data in [(2, MINIMAL_V2), (3, MINIMAL_V3)]:
        ver_dir = tmp_path / f"v{ver}"
        ver_dir.mkdir()
        path = _write_manifest(ver_dir, data)
        m = load_manifest(path)
        assert m["version"] == ver
