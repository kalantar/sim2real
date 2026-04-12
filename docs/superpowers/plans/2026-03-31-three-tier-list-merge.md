# Three-Tier List Merge Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **DO NOT commit anything.** All changes should be left unstaged/uncommitted for human review.

**Goal:** Fix `_deep_merge()` in `tools/transfer_cli.py` to merge lists intelligently using a three-tier strategy instead of always replacing, and fix a `setdefault` data-loss bug in `_flatten_gaie_shared()`.

**Architecture:** Add two private helpers (`_detect_list_key`, `_merge_lists`) above `_deep_merge` in `transfer_cli.py`. Add one `elif` branch to `_deep_merge` that delegates list-vs-list handling to `_merge_lists`. Fix `_flatten_gaie_shared` to replace `ie.setdefault("image", img)` with `_deep_merge(img, ie.get("image", {}))`. Unit-test the helpers by direct import; update the existing `test_list_replacement` test and add integration-level `TestMergeValues` tests via the CLI subprocess helper.

**Tech Stack:** Python 3.10+, stdlib only (`copy` via local import already used in `_deep_merge`), pytest

**Spec:** `docs/plans/2026-03-24-deep-merge-named-key-list-merge.md`

---

## File Structure

| File | Change |
|------|--------|
| `tools/transfer_cli.py` | Insert `_LIST_KEY_CANDIDATES`, `_detect_list_key`, `_merge_lists` before `_deep_merge` at line 2037; add one `elif` branch to `_deep_merge`; fix `_flatten_gaie_shared` lines 2092–2094 |
| `tools/test_transfer_cli.py` | Add `TestListMergeHelpers` class with 14 unit tests; rename + update `test_list_replacement`; add 4 new integration tests in `TestMergeValues` |
| `CLAUDE.md` | Update "Lists are replaced" sentence in **Two-Layer Tekton Config Architecture** section |

---

## Chunk 1: List Merge Helpers + `_deep_merge` Update

### Task 1: Write failing unit tests for `_detect_list_key`

**Files:**
- Modify: `tools/test_transfer_cli.py` — append new `TestListMergeHelpers` class at end of file

- [ ] **Step 1: Add `TestListMergeHelpers` class to end of `tools/test_transfer_cli.py`**

```python
class TestListMergeHelpers:
    """Unit tests for _detect_list_key and _merge_lists helpers."""

    def test_detect_list_key_returns_name(self):
        from tools.transfer_cli import _detect_list_key
        base = [{"name": "v", "value": 1}]
        overlay = [{"name": "debug", "value": 0}]
        assert _detect_list_key(base, overlay) == "name"

    def test_detect_list_key_returns_mountpath_when_no_name(self):
        from tools.transfer_cli import _detect_list_key
        base = [{"mountPath": "/data"}]
        overlay = [{"mountPath": "/logs"}]
        assert _detect_list_key(base, overlay) == "mountPath"

    def test_detect_list_key_returns_none_no_common_key(self):
        from tools.transfer_cli import _detect_list_key
        base = [{"image": "old", "modelCommand": "vllmServe"}]
        overlay = [{"image": "new", "extraConfig": {}}]
        assert _detect_list_key(base, overlay) is None

    def test_detect_list_key_empty_lists(self):
        from tools.transfer_cli import _detect_list_key
        assert _detect_list_key([], []) is None

    def test_detect_list_key_priority_name_before_mountpath(self):
        from tools.transfer_cli import _detect_list_key
        base = [{"name": "v", "mountPath": "/a"}]
        overlay = [{"name": "debug", "mountPath": "/b"}]
        # Both candidates present — name has higher priority
        assert _detect_list_key(base, overlay) == "name"

    def test_detect_list_key_partial_key_returns_none(self):
        from tools.transfer_cli import _detect_list_key
        # overlay item is missing 'name' → candidate rejected
        base = [{"name": "v"}]
        overlay = [{"value": 1}]
        assert _detect_list_key(base, overlay) is None
```

- [ ] **Step 2: Run tests to verify they fail with ImportError**

Run: `python -m pytest tools/test_transfer_cli.py::TestListMergeHelpers -v`

Expected: `ImportError: cannot import name '_detect_list_key' from 'tools.transfer_cli'`

---

### Task 2: Write failing unit tests for `_merge_lists`

**Files:**
- Modify: `tools/test_transfer_cli.py` — append to `TestListMergeHelpers` class

- [ ] **Step 1: Add `_merge_lists` tests to `TestListMergeHelpers`**

```python
    # --- _merge_lists tests ---

    def test_merge_lists_tier1_scalar_replaced(self):
        from tools.transfer_cli import _merge_lists
        assert _merge_lists(["a", "b"], ["c"]) == ["c"]

    def test_merge_lists_tier1_mixed_dict_and_scalar_replaced(self):
        from tools.transfer_cli import _merge_lists
        # Mixed list (some non-dict items) → overlay replaces
        assert _merge_lists([{"a": 1}, "scalar"], [{"b": 2}]) == [{"b": 2}]

    def test_merge_lists_tier2_named_key_merge(self):
        from tools.transfer_cli import _merge_lists
        base = [{"name": "v", "value": 1}, {"name": "debug", "value": 0}]
        overlay = [{"name": "v", "value": 3}, {"name": "timeout", "value": 30}]
        result = _merge_lists(base, overlay)
        assert result == [
            {"name": "v", "value": 3},        # matched: overlay wins
            {"name": "debug", "value": 0},     # unmatched base: preserved
            {"name": "timeout", "value": 30},  # new overlay item: appended
        ]

    def test_merge_lists_tier3_positional_merge(self):
        from tools.transfer_cli import _merge_lists
        base = [{"modelCommand": "vllmServe"}]
        overlay = [{"image": "vllm/vllm-openai:v0.11.0"}]
        result = _merge_lists(base, overlay)
        assert result == [{"modelCommand": "vllmServe", "image": "vllm/vllm-openai:v0.11.0"}]

    def test_merge_lists_tier3_surplus_from_base_preserved(self):
        from tools.transfer_cli import _merge_lists
        base = [{"a": 1}, {"b": 2}]
        overlay = [{"a": 10}]
        result = _merge_lists(base, overlay)
        assert result == [{"a": 10}, {"b": 2}]

    def test_merge_lists_tier3_surplus_from_overlay_appended(self):
        from tools.transfer_cli import _merge_lists
        base = [{"a": 1}]
        overlay = [{"a": 10}, {"c": 3}]
        result = _merge_lists(base, overlay)
        assert result == [{"a": 10}, {"c": 3}]

    def test_merge_lists_empty_overlay_clears(self):
        from tools.transfer_cli import _merge_lists
        assert _merge_lists([{"a": 1}], []) == []

    def test_merge_lists_empty_base_returns_overlay(self):
        from tools.transfer_cli import _merge_lists
        assert _merge_lists([], [{"a": 1}]) == [{"a": 1}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tools/test_transfer_cli.py::TestListMergeHelpers -v`

Expected: `ImportError: cannot import name '_merge_lists' from 'tools.transfer_cli'`

---

### Task 3: Implement `_LIST_KEY_CANDIDATES`, `_detect_list_key`, `_merge_lists`

**Files:**
- Modify: `tools/transfer_cli.py` — insert new code block immediately before `_deep_merge` at line 2037

- [ ] **Step 1: Insert three helpers above `_deep_merge` in `tools/transfer_cli.py`**

Insert the following block immediately before the line `def _deep_merge(base: dict, overlay: dict) -> dict:` (currently line 2037):

```python
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
    import copy

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
```

- [ ] **Step 2: Run unit tests to verify they now pass**

Run: `python -m pytest tools/test_transfer_cli.py::TestListMergeHelpers -v`

Expected: all 14 tests PASS

---

### Task 4: Update `test_list_replacement`, then wire `_deep_merge`

**Files:**
- Modify: `tools/test_transfer_cli.py:2768` — rename and update assertions
- Modify: `tools/transfer_cli.py` — add `elif` branch to `_deep_merge`

- [ ] **Step 1: Update `test_list_replacement` in `TestMergeValues` (line 2768)**

Replace the entire `test_list_replacement` method with:

```python
def test_keyless_dict_list_positional_merge(self, tmp_path):
    """Keyless dict lists are positionally merged — base fields survive unless overridden."""
    env_file = tmp_path / "env.yaml"
    alg_file = tmp_path / "alg.yaml"
    out_file = tmp_path / "out.yaml"

    env = self._minimal_env_defaults()
    env["stack"]["model"] = {
        "helmValues": {
            "decode": {
                "replicas": 1,
                "containers": [{"image": "old", "modelCommand": "vllmServe"}],
            },
            "modelArtifacts": {"name": "x", "uri": "pvc://x/y"},
        }
    }
    self._write_yaml(env_file, env)

    alg = self._minimal_algorithm_values()
    # Overlay: new image only — modelCommand not present
    alg["stack"]["model"]["helmValues"]["decode"]["containers"] = [{"image": "new"}]
    self._write_yaml(alg_file, alg)

    rc, out, err = _run_cli(
        "merge-values",
        "--env", str(env_file),
        "--algorithm", str(alg_file),
        "--out", str(out_file),
    )
    assert rc == 0, f"exit {rc}: {err}"
    result = self._load_yaml(out_file)
    containers = result["stack"]["model"]["helmValues"]["decode"]["containers"]
    assert containers == [{"image": "new", "modelCommand": "vllmServe"}], (
        f"Expected positional merge preserving modelCommand, got: {containers}"
    )
    assert containers[0]["modelCommand"] == "vllmServe", (
        "modelCommand must survive positional merge from env_defaults"
    )
```

- [ ] **Step 2: Run updated test to verify it FAILS (old behavior)**

Run: `python -m pytest "tools/test_transfer_cli.py::TestMergeValues::test_keyless_dict_list_positional_merge" -v`

Expected: FAIL — old `_deep_merge` replaces the list; `containers == [{"image": "new"}]`

- [ ] **Step 3: Update `_deep_merge` to add list-merge branch**

In `tools/transfer_cli.py`, replace the full body of `_deep_merge` (currently lines 2037–2049, now shifted down by the inserted helpers). The function currently looks like:

```python
def _deep_merge(base: dict, overlay: dict) -> dict:
    """Deep-merge overlay onto base. Dict keys merged recursively; non-dict values replaced.

    Lists are replaced entirely (not appended). Returns a new dict (deep copy).
    """
    import copy
    result = copy.deepcopy(base)
    for key, oval in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(oval, dict):
            result[key] = _deep_merge(result[key], oval)
        else:
            result[key] = copy.deepcopy(oval)
    return result
```

Replace with:

```python
def _deep_merge(base: dict, overlay: dict) -> dict:
    """Deep-merge overlay onto base. Dict keys merged recursively.

    Lists of dicts are merged by named key or positional index (see _merge_lists).
    Lists of scalars are replaced entirely. Returns a new dict (deep copy).
    """
    import copy
    result = copy.deepcopy(base)
    for key, oval in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(oval, dict):
            result[key] = _deep_merge(result[key], oval)
        elif key in result and isinstance(result[key], list) and isinstance(oval, list):
            result[key] = _merge_lists(result[key], oval)
        else:
            result[key] = copy.deepcopy(oval)
    return result
```

- [ ] **Step 4: Run updated test to verify it now PASSES**

Run: `python -m pytest "tools/test_transfer_cli.py::TestMergeValues::test_keyless_dict_list_positional_merge" -v`

Expected: PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `python -m pytest tools/test_transfer_cli.py -v -x 2>&1 | tail -30`

Expected: all existing tests pass; no regressions

---

### Task 5: Add integration tests for list merge edge cases

**Files:**
- Modify: `tools/test_transfer_cli.py` — add 3 tests to `TestMergeValues` after `test_keyless_dict_list_positional_merge`

- [ ] **Step 1: Add three new tests to `TestMergeValues`**

```python
def test_scalar_list_still_replaced(self, tmp_path):
    """Scalar lists (non-dict items) continue to be replaced entirely."""
    env_file = tmp_path / "env.yaml"
    alg_file = tmp_path / "alg.yaml"
    out_file = tmp_path / "out.yaml"

    env = self._minimal_env_defaults()
    env["observe"] = {
        "image": "ghcr.io/x:v1",
        "tags": ["v1", "v2"],
        "workloads": [],
    }
    self._write_yaml(env_file, env)

    alg = self._minimal_algorithm_values()
    alg["observe"]["tags"] = ["v3"]
    self._write_yaml(alg_file, alg)

    rc, out, err = _run_cli(
        "merge-values",
        "--env", str(env_file),
        "--algorithm", str(alg_file),
        "--out", str(out_file),
    )
    assert rc == 0, f"exit {rc}: {err}"
    result = self._load_yaml(out_file)
    assert result["observe"]["tags"] == ["v3"], (
        "Scalar lists must still be replaced entirely"
    )


def test_named_key_list_merge_via_containers_with_name(self, tmp_path):
    """When containers have a 'name' field, named-key (Tier 2) merge applies."""
    env_file = tmp_path / "env.yaml"
    alg_file = tmp_path / "alg.yaml"
    out_file = tmp_path / "out.yaml"

    env = self._minimal_env_defaults()
    env["stack"]["model"] = {
        "helmValues": {
            "decode": {
                "replicas": 1,
                "containers": [{"name": "vllm", "modelCommand": "vllmServe"}],
            },
            "modelArtifacts": {"name": "x", "uri": "pvc://x/y"},
        }
    }
    self._write_yaml(env_file, env)

    alg = self._minimal_algorithm_values()
    alg["stack"]["model"]["helmValues"]["decode"]["containers"] = [
        {"name": "vllm", "image": "vllm/vllm-openai:v0.11.0"}
    ]
    self._write_yaml(alg_file, alg)

    rc, out, err = _run_cli(
        "merge-values",
        "--env", str(env_file),
        "--algorithm", str(alg_file),
        "--out", str(out_file),
    )
    assert rc == 0, f"exit {rc}: {err}"
    result = self._load_yaml(out_file)
    containers = result["stack"]["model"]["helmValues"]["decode"]["containers"]
    assert len(containers) == 1
    assert containers[0]["name"] == "vllm"
    assert containers[0]["image"] == "vllm/vllm-openai:v0.11.0"
    assert containers[0]["modelCommand"] == "vllmServe", (
        "Named-key merge: base fields not in overlay must be preserved"
    )


def test_positional_merge_surplus_base_items_preserved(self, tmp_path):
    """Positional merge: surplus items beyond overlay length are kept from base."""
    env_file = tmp_path / "env.yaml"
    alg_file = tmp_path / "alg.yaml"
    out_file = tmp_path / "out.yaml"

    env = self._minimal_env_defaults()
    env["stack"]["model"] = {
        "helmValues": {
            "decode": {
                "replicas": 1,
                "containers": [
                    {"modelCommand": "vllmServe"},
                    {"sidecar": "proxy"},
                ],
            },
            "modelArtifacts": {"name": "x", "uri": "pvc://x/y"},
        }
    }
    self._write_yaml(env_file, env)

    alg = self._minimal_algorithm_values()
    # Overlay has only 1 container — base has 2
    alg["stack"]["model"]["helmValues"]["decode"]["containers"] = [{"image": "new"}]
    self._write_yaml(alg_file, alg)

    rc, out, err = _run_cli(
        "merge-values",
        "--env", str(env_file),
        "--algorithm", str(alg_file),
        "--out", str(out_file),
    )
    assert rc == 0, f"exit {rc}: {err}"
    result = self._load_yaml(out_file)
    containers = result["stack"]["model"]["helmValues"]["decode"]["containers"]
    assert len(containers) == 2, "Surplus base item must be preserved"
    assert containers[0] == {"image": "new", "modelCommand": "vllmServe"}
    assert containers[1] == {"sidecar": "proxy"}
```

- [ ] **Step 2: Run new tests to verify they pass**

Run: `python -m pytest "tools/test_transfer_cli.py::TestMergeValues::test_scalar_list_still_replaced" "tools/test_transfer_cli.py::TestMergeValues::test_named_key_list_merge_via_containers_with_name" "tools/test_transfer_cli.py::TestMergeValues::test_positional_merge_surplus_base_items_preserved" -v`

Expected: all 3 PASS

---

## Chunk 2: `setdefault` Fix + CLAUDE.md

### Task 6: Write failing test for `setdefault` / `pullPolicy` data-loss bug

**Files:**
- Modify: `tools/test_transfer_cli.py` — add 1 test to `TestMergeValues`

**Background:** `_flatten_gaie_shared` at line 2094 uses `ie.setdefault("image", img)`. When `inferenceExtension.image` was pre-injected by `build-push-epp` (as happens in practice), `setdefault` short-circuits and `pullPolicy` from `epp_image` is silently discarded. This fix is not in the original spec doc (`2026-03-24-deep-merge-named-key-list-merge.md`) but is explicitly called out in kalantar/sim2real#18 comment 3 as related work that uses the same `_deep_merge` fix.

- [ ] **Step 1: Add test for pullPolicy preservation when image is pre-injected**

```python
def test_epp_pullpolicy_survives_when_image_preinjected(self, tmp_path):
    """pullPolicy from epp_image is applied even when treatment already has inferenceExtension.image.

    Regression test for the setdefault short-circuit bug in _flatten_gaie_shared.
    """
    env_file = tmp_path / "env.yaml"
    alg_file = tmp_path / "alg.yaml"
    out_file = tmp_path / "out.yaml"

    env = self._minimal_env_defaults()
    env["stack"]["gaie"] = {
        "epp_image": {
            "upstream": {
                "hub": "ghcr.io", "name": "epp", "tag": "v1",
                "pullPolicy": "IfNotPresent",
            },
            "build": {
                "hub": "registry.io", "name": "epp", "tag": "sha-abc123",
                "pullPolicy": "Always",
            },
        },
        "baseline": {"helmValues": {}},
        "treatment": {"helmValues": {}},
    }
    self._write_yaml(env_file, env)

    alg = self._minimal_algorithm_values()
    # Simulate build-push-epp pre-injecting image — no pullPolicy set
    alg["stack"]["gaie"]["treatment"]["helmValues"]["inferenceExtension"] = {
        "image": {"hub": "registry.io", "name": "epp", "tag": "sha-abc123"}
    }
    self._write_yaml(alg_file, alg)

    rc, out, err = _run_cli(
        "merge-values",
        "--env", str(env_file),
        "--algorithm", str(alg_file),
        "--out", str(out_file),
    )
    assert rc == 0, f"exit {rc}: {err}"
    result = self._load_yaml(out_file)
    treatment_ie = result["stack"]["gaie"]["treatment"]["helmValues"]["inferenceExtension"]
    assert treatment_ie["image"]["tag"] == "sha-abc123", (
        "Pre-injected tag must survive"
    )
    assert treatment_ie["image"]["hub"] == "registry.io", (
        "Pre-injected hub must survive"
    )
    assert treatment_ie["image"]["pullPolicy"] == "Always", (
        "pullPolicy from epp_image.build must be applied when absent from pre-injected image"
    )
```

- [ ] **Step 2: Run test to verify it FAILS**

Run: `python -m pytest "tools/test_transfer_cli.py::TestMergeValues::test_epp_pullpolicy_survives_when_image_preinjected" -v`

Expected: FAIL — `pullPolicy` key absent from `treatment_ie["image"]`

---

### Task 7: Fix `setdefault` in `_flatten_gaie_shared`

**Files:**
- Modify: `tools/transfer_cli.py:2092–2094`

- [ ] **Step 1: Replace `ie.setdefault("image", img)` with `_deep_merge` call**

Find this block in `_flatten_gaie_shared` (around line 2092):

```python
            ie = gaie[phase]["helmValues"].setdefault("inferenceExtension", {})
            # Only set if not already explicitly provided in algorithm values
            ie.setdefault("image", img)
```

Replace with:

```python
            ie = gaie[phase]["helmValues"].setdefault("inferenceExtension", {})
            # Deep-merge so pullPolicy from epp_image is applied even when image is pre-injected
            ie["image"] = _deep_merge(img, ie.get("image", {}))
```

Explanation: `img` is the base (carries `pullPolicy`), `ie.get("image", {})` is the overlay (pre-injected values win). When no pre-injected image exists, `ie.get("image", {})` is `{}` and the result is a copy of `img`. When a pre-injected image exists, its keys override `img` — but any field missing from the pre-injected image (like `pullPolicy`) is inherited from `img`.

- [ ] **Step 2: Run test to verify it now PASSES**

Run: `python -m pytest "tools/test_transfer_cli.py::TestMergeValues::test_epp_pullpolicy_survives_when_image_preinjected" -v`

Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tools/test_transfer_cli.py -v 2>&1 | tail -30`

Expected: all tests pass; no regressions

---

### Task 8: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` — **Two-Layer Tekton Config Architecture** section, `_deep_merge` merge step description

- [ ] **Step 1: Update the list-merge description in CLAUDE.md**

Find the sentence in the **Merge step** paragraph:

```
Deep-merges algorithm values over env defaults. `gaie.shared.helmValues` (connection pool, provider, flags) is flattened into both `gaie.baseline` and `gaie.treatment` phases; `gaie.shared` is removed from the output. Lists are replaced (not appended). The merged `workspace/tekton/values.yaml` is what `compile-pipeline` consumes — same shape as before the split.
```

Replace `Lists are replaced (not appended).` with:

```
Lists of scalars are replaced entirely. Lists of dicts are merged using a three-tier strategy: named-key merge (by `name`, `mountPath`, or `containerPort`) when all items share a key field; positional deep-merge otherwise. An explicit `[]` in the overlay clears the base list.
```

- [ ] **Step 2: Verify updated text is correct**

Run: `grep -A2 "Lists of scalars" CLAUDE.md`

Expected: the updated sentence visible in output

---

## Summary

After all tasks complete, the following behaviors are fixed:

1. **`containers` list merge** — `env_defaults.yaml` fields (e.g. `modelCommand: vllmServe`) survive when `algorithm_values.yaml` contributes the same-index container with different fields.
2. **Scalar lists** — still replaced entirely (no behavioral change).
3. **Named-key lists** — items matched by `name`/`mountPath`/`containerPort` are deep-merged; unmatched items from either side are preserved.
4. **`pullPolicy` data loss** — `_flatten_gaie_shared` now uses `_deep_merge` instead of `setdefault`, so `pullPolicy` from `epp_image` is applied even when `inferenceExtension.image` was pre-injected.
