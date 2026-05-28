"""Tests for pipeline/lib/values.py — deep-merge logic."""

from pipeline.lib.values import (
    deep_merge,
    _merge_lists,
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


# ── deep_merge ───────────────────────────────────────────────────────────────

class TestDeepMerge:
    def test_nested_dict_merge(self):
        base = {"a": {"b": 1, "c": 2}}
        overlay = {"a": {"b": 99}}
        result = deep_merge(base, overlay)
        assert result == {"a": {"b": 99, "c": 2}}

    def test_overlay_adds_new_key(self):
        result = deep_merge({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_does_not_mutate_base(self):
        base = {"a": {"b": 1}}
        overlay = {"a": {"b": 2}}
        deep_merge(base, overlay)
        assert base == {"a": {"b": 1}}

    def test_does_not_mutate_overlay(self):
        base = {"a": {"b": 1}}
        overlay = {"a": {"b": 2}}
        deep_merge(base, overlay)
        assert overlay == {"a": {"b": 2}}

    def test_list_delegated_to_merge_lists(self):
        base = {"items": [{"name": "x", "v": 1}]}
        overlay = {"items": [{"name": "x", "v": 99}]}
        result = deep_merge(base, overlay)
        assert result["items"] == [{"name": "x", "v": 99}]
