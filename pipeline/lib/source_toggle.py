"""Toggle component directory between baseline and treatment states."""
import shutil
import subprocess
from pathlib import Path


def restore_baseline(component_dir: Path, translation_output: dict) -> None:
    """Restore component_dir to its pre-translation (baseline) state.

    - Deletes files listed in translation_output["files_created"]
    - Runs git checkout on files listed in translation_output["files_modified"]
    """
    for rel_path in translation_output.get("files_created", []):
        target = component_dir / rel_path
        if target.exists():
            target.unlink()

    modified = translation_output.get("files_modified", [])
    if modified:
        subprocess.run(
            ["git", "-C", str(component_dir), "checkout", "--"] + modified,
            check=True, capture_output=True,
        )


def restore_treatment(
    component_dir: Path,
    generated_dir: Path,
    translation_output: dict,
) -> None:
    """Restore component_dir to its post-translation (treatment) state.

    Copies files from generated_dir back to their relative paths in component_dir.
    """
    all_files = (
        translation_output.get("files_created", [])
        + translation_output.get("files_modified", [])
    )
    for rel_path in all_files:
        src = generated_dir / Path(rel_path).name
        dst = component_dir / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
