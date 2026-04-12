"""Tests for pipeline/lib/values.py — deep-merge logic."""

import pytest
import yaml

from pipeline.lib.values import (
    _apply_request_multiplier,
    _apply_vllm_image_override,
    _deep_merge,
    _flatten_gaie_shared,
    _merge_lists,
    merge_values,
)


# ── _merge_lists ──────────────────────────────────────────────────────────────

class TestMergeLists:
    def test_scalar_list_replaced(self):
        assert _merge_lists(["a", "b"], ["c"]) == ["c"]

    def test_scalar_overlay_replaces_dict_base(self):
        assert _merge_lists([{"name": "x"}], ["c"]) == ["c"]

    def test_explicit_clear_returns_empty(self):
        assert _merge_lists([{"name": "x"}], []) == []

    def test_named_key_merge_by_name(self):
        base = [{"name": "x", "value": 1}, {"name": "y", "value": 2}]
        overlay = [{"name": "x", "value": 99}]
        result = _merge_lists(base, overlay)
        assert result == [{"name": "x", "value": 99}, {"name": "y", "value": 2}]

    def test_named_key_merge_adds_new_entry(self):
        base = [{"name": "x", "v": 1}]
        overlay = [{"name": "x", "v": 1}, {"name": "z", "v": 3}]
        result = _merge_lists(base, overlay)
        assert len(result) == 2
        assert any(item["name"] == "z" for item in result)

    def test_positional_merge_no_common_key(self):
        base = [{"a": 1, "b": 2}]
        overlay = [{"a": 99}]
        result = _merge_lists(base, overlay)
        assert result == [{"a": 99, "b": 2}]

    def test_positional_preserves_surplus_from_base(self):
        base = [{"a": 1}, {"a": 2}]
        overlay = [{"a": 9}]
        result = _merge_lists(base, overlay)
        assert len(result) == 2
        assert result[0]["a"] == 9
        assert result[1]["a"] == 2


# ── _deep_merge ───────────────────────────────────────────────────────────────

class TestDeepMerge:
    def test_nested_dict_merge(self):
        base = {"a": {"b": 1, "c": 2}}
        overlay = {"a": {"b": 99}}
        result = _deep_merge(base, overlay)
        assert result == {"a": {"b": 99, "c": 2}}

    def test_overlay_adds_new_key(self):
        result = _deep_merge({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_does_not_mutate_base(self):
        base = {"a": {"b": 1}}
        overlay = {"a": {"b": 2}}
        _deep_merge(base, overlay)
        assert base == {"a": {"b": 1}}

    def test_does_not_mutate_overlay(self):
        base = {"a": {"b": 1}}
        overlay = {"a": {"b": 2}}
        _deep_merge(base, overlay)
        assert overlay == {"a": {"b": 2}}

    def test_list_delegated_to_merge_lists(self):
        base = {"items": [{"name": "x", "v": 1}]}
        overlay = {"items": [{"name": "x", "v": 99}]}
        result = _deep_merge(base, overlay)
        assert result["items"] == [{"name": "x", "v": 99}]


# ── _flatten_gaie_shared ──────────────────────────────────────────────────────

class TestFlattenGaieShared:
    def test_shared_helmvalues_flattened_into_phases(self):
        data = {
            "stack": {
                "gaie": {
                    "shared": {"helmValues": {"conn": 10}},
                    "baseline": {"helmValues": {"foo": "bar"}},
                    "treatment": {"helmValues": {"baz": "qux"}},
                }
            }
        }
        result = _flatten_gaie_shared(data)
        gaie = result["stack"]["gaie"]
        assert gaie["baseline"]["helmValues"] == {"conn": 10, "foo": "bar"}
        assert gaie["treatment"]["helmValues"] == {"conn": 10, "baz": "qux"}

    def test_shared_key_removed(self):
        data = {
            "stack": {
                "gaie": {
                    "shared": {"helmValues": {}},
                    "baseline": {},
                    "treatment": {},
                }
            }
        }
        result = _flatten_gaie_shared(data)
        assert "shared" not in result["stack"]["gaie"]

    def test_epp_image_key_removed(self):
        data = {
            "stack": {
                "gaie": {
                    "shared": {},
                    "epp_image": {
                        "upstream": {"hub": "ghcr.io", "name": "epp", "tag": "v1"},
                    },
                    "baseline": {},
                    "treatment": {},
                }
            }
        }
        result = _flatten_gaie_shared(data)
        assert "epp_image" not in result["stack"]["gaie"]

    def test_treatment_uses_build_image(self):
        data = {
            "stack": {
                "gaie": {
                    "shared": {},
                    "epp_image": {
                        "upstream": {"hub": "upstream.io", "name": "epp", "tag": "v1"},
                        "build": {"hub": "registry.io", "name": "custom", "tag": "abc123"},
                    },
                    "baseline": {},
                    "treatment": {},
                }
            }
        }
        result = _flatten_gaie_shared(data)
        gaie = result["stack"]["gaie"]
        t_img = gaie["treatment"]["helmValues"]["inferenceExtension"]["image"]
        b_img = gaie["baseline"]["helmValues"]["inferenceExtension"]["image"]
        assert t_img["hub"] == "registry.io"
        assert t_img["tag"] == "abc123"
        assert b_img["hub"] == "upstream.io"

    def test_noop_when_no_gaie(self):
        data = {"stack": {"model": {"name": "llama"}}}
        result = _flatten_gaie_shared(data)
        assert result == {"stack": {"model": {"name": "llama"}}}


# ── _apply_vllm_image_override ────────────────────────────────────────────────

class TestApplyVllmImageOverride:
    def test_replaces_container_image(self):
        data = {
            "stack": {
                "model": {
                    "vllm_image": "custom/vllm:latest",
                    "helmValues": {
                        "decode": {"containers": [{"image": "old/vllm:v1"}]}
                    },
                }
            }
        }
        result = _apply_vllm_image_override(data)
        containers = result["stack"]["model"]["helmValues"]["decode"]["containers"]
        assert containers[0]["image"] == "custom/vllm:latest"

    def test_strips_vllm_image_key(self):
        data = {
            "stack": {
                "model": {
                    "vllm_image": "custom/vllm:latest",
                    "helmValues": {"decode": {"containers": [{"image": "old"}]}},
                }
            }
        }
        result = _apply_vllm_image_override(data)
        assert "vllm_image" not in result["stack"]["model"]

    def test_noop_when_key_absent(self):
        data = {"stack": {"model": {"modelName": "llama"}}}
        result = _apply_vllm_image_override(data)
        assert result == {"stack": {"model": {"modelName": "llama"}}}


# ── _apply_request_multiplier ─────────────────────────────────────────────────

class TestApplyRequestMultiplier:
    def test_scales_num_requests(self):
        data = {
            "observe": {
                "request_multiplier": 2,
                "workloads": [{"name": "w1", "spec": "num_requests: 100\n"}],
            }
        }
        result = _apply_request_multiplier(data)
        spec = yaml.safe_load(result["observe"]["workloads"][0]["spec"])
        assert spec["num_requests"] == 200

    def test_strips_multiplier_key(self):
        data = {
            "observe": {
                "request_multiplier": 2,
                "workloads": [{"name": "w1", "spec": "num_requests: 10\n"}],
            }
        }
        result = _apply_request_multiplier(data)
        assert "request_multiplier" not in result["observe"]

    def test_multiplier_lte_one_no_scaling(self):
        data = {
            "observe": {
                "request_multiplier": 1,
                "workloads": [{"name": "w1", "spec": "num_requests: 100\n"}],
            }
        }
        result = _apply_request_multiplier(data)
        spec = yaml.safe_load(result["observe"]["workloads"][0]["spec"])
        assert spec["num_requests"] == 100

    def test_missing_multiplier_noop(self):
        data = {"observe": {"workloads": []}}
        result = _apply_request_multiplier(data)
        assert result == {"observe": {"workloads": []}}

    def test_non_numeric_spec_left_unchanged(self, capsys):
        data = {
            "observe": {
                "request_multiplier": 2,
                "workloads": [{"name": "w1", "spec": "{invalid yaml: ["}],
            }
        }
        result = _apply_request_multiplier(data)
        assert result["observe"]["workloads"][0]["spec"] == "{invalid yaml: ["


# ── merge_values() end-to-end ─────────────────────────────────────────────────

class TestMergeValues:
    def test_basic_merge_with_scenario(self, tmp_path):
        env = tmp_path / "env.yaml"
        env.write_text(yaml.dump({
            "common": {"x": 1},
            "scenarios": {"s1": {"y": 2}},
        }))
        alg = tmp_path / "alg.yaml"
        alg.write_text(yaml.dump({"z": 3}))
        out = tmp_path / "values.yaml"

        merge_values(env, alg, out, scenario="s1")

        result = yaml.safe_load(out.read_text())
        assert result["x"] == 1
        assert result["y"] == 2
        assert result["z"] == 3

    def test_scenario_strips_pipeline_keys(self, tmp_path):
        env = tmp_path / "env.yaml"
        env.write_text(yaml.dump({
            "common": {},
            "scenarios": {
                "s1": {
                    "target": "x",
                    "build": "y",
                    "config": "z",
                    "stack": {"a": 1},
                }
            },
        }))
        alg = tmp_path / "alg.yaml"
        alg.write_text("{}")
        out = tmp_path / "values.yaml"

        merge_values(env, alg, out, scenario="s1")

        result = yaml.safe_load(out.read_text())
        assert "target" not in result
        assert "build" not in result
        assert "config" not in result
        assert result["stack"]["a"] == 1

    def test_strips_fast_iteration_from_pipeline(self, tmp_path):
        env = tmp_path / "env.yaml"
        env.write_text(yaml.dump({
            "common": {"pipeline": {"fast_iteration": True, "sleepDuration": "30s"}},
            "scenarios": {"s1": {}},
        }))
        alg = tmp_path / "alg.yaml"
        alg.write_text("{}")
        out = tmp_path / "values.yaml"

        merge_values(env, alg, out, scenario="s1")

        result = yaml.safe_load(out.read_text())
        assert result.get("pipeline", {}).get("fast_iteration") is None
        assert result.get("pipeline", {}).get("sleepDuration") == "30s"

    def test_raises_file_not_found_on_missing_env(self, tmp_path):
        alg = tmp_path / "alg.yaml"
        alg.write_text("{}")
        with pytest.raises(FileNotFoundError):
            merge_values(tmp_path / "missing.yaml", alg, tmp_path / "out.yaml")

    def test_raises_file_not_found_on_missing_alg(self, tmp_path):
        env = tmp_path / "env.yaml"
        env.write_text(yaml.dump({"common": {}, "scenarios": {}}))
        with pytest.raises(FileNotFoundError):
            merge_values(env, tmp_path / "missing.yaml", tmp_path / "out.yaml")

    def test_raises_value_error_on_unknown_scenario(self, tmp_path):
        env = tmp_path / "env.yaml"
        env.write_text(yaml.dump({"common": {}, "scenarios": {"s1": {}}}))
        alg = tmp_path / "alg.yaml"
        alg.write_text("{}")
        with pytest.raises(ValueError, match="s999"):
            merge_values(env, alg, tmp_path / "out.yaml", scenario="s999")

    def test_creates_output_parent_dirs(self, tmp_path):
        env = tmp_path / "env.yaml"
        env.write_text(yaml.dump({"common": {}, "scenarios": {"s1": {}}}))
        alg = tmp_path / "alg.yaml"
        alg.write_text("{}")
        out = tmp_path / "subdir" / "values.yaml"

        merge_values(env, alg, out, scenario="s1")
        assert out.exists()
