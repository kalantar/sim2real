"""EPP image injection utilities."""


def inject_epp_image(scenario_dict: dict, registry: str, repo_name: str, tag: str) -> bool:
    """Inject EPP image into all scenario entries.

    Returns True if injection occurred, False if skipped (empty registry or no scenarios).
    """
    if not registry:
        return False
    scenario_list = scenario_dict.get("scenario", [])
    if not scenario_list:
        return False
    epp_img = {
        "repository": f"{registry}/{repo_name}",
        "tag": tag,
        "pullPolicy": "Always",
    }
    for entry in scenario_list:
        entry.setdefault("images", {})["inferenceScheduler"] = epp_img
    return True


def inject_image_ref(scenario_dict: dict, repository: str, tag: str) -> bool:
    """Inject inferenceScheduler image into all scenario entries.

    Returns True if injection occurred, False if skipped (no scenarios).
    """
    scenario_list = scenario_dict.get("scenario", [])
    if not scenario_list:
        return False
    img = {
        "repository": repository,
        "tag": tag,
        "pullPolicy": "Always",
    }
    for entry in scenario_list:
        entry.setdefault("images", {})["inferenceScheduler"] = img
    return True
