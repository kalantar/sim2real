"""Deep-merge logic for values assembly."""
import copy


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
                result.append(deep_merge(bitem, overlay_by_key[k]))
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
            result.append(deep_merge(base_list[i], overlay_list[i]))
        elif i < len(base_list):
            result.append(copy.deepcopy(base_list[i]))
        else:
            result.append(copy.deepcopy(overlay_list[i]))
    return result


def deep_merge(base: dict, overlay: dict) -> dict:
    """Deep-merge overlay onto base. Dict keys merged recursively.

    Lists of dicts are merged by named key or positional index (see _merge_lists).
    Lists of scalars are replaced entirely. Returns a new dict (deep copy).
    """
    result = copy.deepcopy(base)
    for key, oval in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(oval, dict):
            result[key] = deep_merge(result[key], oval)
        elif key in result and isinstance(result[key], list) and isinstance(oval, list):
            result[key] = _merge_lists(result[key], oval)
        else:
            result[key] = copy.deepcopy(oval)
    return result
