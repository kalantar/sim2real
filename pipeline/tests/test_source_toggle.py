"""Tests for pipeline/lib/source_toggle.py."""
import subprocess
from pathlib import Path

from pipeline.lib.source_toggle import restore_baseline, restore_treatment


def _init_repo(path: Path) -> None:
    """Create a git repo with an initial commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True, capture_output=True)
    (path / "base.txt").write_text("original")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


class TestRestoreBaseline:
    def test_removes_created_files(self, tmp_path):
        component_dir = tmp_path / "component"
        component_dir.mkdir()
        _init_repo(component_dir)

        (component_dir / "pkg").mkdir()
        (component_dir / "pkg" / "scorer.go").write_text("generated")

        translation_output = {
            "files_created": ["pkg/scorer.go"],
            "files_modified": [],
        }

        restore_baseline(component_dir, translation_output)
        assert not (component_dir / "pkg" / "scorer.go").exists()

    def test_restores_modified_files_to_git_state(self, tmp_path):
        component_dir = tmp_path / "component"
        component_dir.mkdir()
        _init_repo(component_dir)

        (component_dir / "base.txt").write_text("modified by translation")

        translation_output = {
            "files_created": [],
            "files_modified": ["base.txt"],
        }

        restore_baseline(component_dir, translation_output)
        assert (component_dir / "base.txt").read_text() == "original"

    def test_handles_both_created_and_modified(self, tmp_path):
        component_dir = tmp_path / "component"
        component_dir.mkdir()
        _init_repo(component_dir)

        (component_dir / "new_file.go").write_text("new")
        (component_dir / "base.txt").write_text("changed")

        translation_output = {
            "files_created": ["new_file.go"],
            "files_modified": ["base.txt"],
        }

        restore_baseline(component_dir, translation_output)
        assert not (component_dir / "new_file.go").exists()
        assert (component_dir / "base.txt").read_text() == "original"

    def test_skips_missing_created_files(self, tmp_path):
        component_dir = tmp_path / "component"
        component_dir.mkdir()
        _init_repo(component_dir)

        translation_output = {
            "files_created": ["nonexistent.go"],
            "files_modified": [],
        }

        restore_baseline(component_dir, translation_output)  # should not raise

    def test_noop_with_empty_lists(self, tmp_path):
        component_dir = tmp_path / "component"
        component_dir.mkdir()
        _init_repo(component_dir)

        translation_output = {"files_created": [], "files_modified": []}
        restore_baseline(component_dir, translation_output)
        assert (component_dir / "base.txt").read_text() == "original"


class TestRestoreTreatment:
    def test_copies_created_files(self, tmp_path):
        component_dir = tmp_path / "component"
        component_dir.mkdir()
        _init_repo(component_dir)

        generated_dir = tmp_path / "generated"
        generated_dir.mkdir()
        (generated_dir / "scorer.go").write_text("generated content")

        translation_output = {
            "files_created": ["pkg/scorer.go"],
            "files_modified": [],
        }

        restore_treatment(component_dir, generated_dir, translation_output)
        assert (component_dir / "pkg" / "scorer.go").read_text() == "generated content"

    def test_copies_modified_files(self, tmp_path):
        component_dir = tmp_path / "component"
        component_dir.mkdir()
        _init_repo(component_dir)

        generated_dir = tmp_path / "generated"
        generated_dir.mkdir()
        (generated_dir / "base.txt").write_text("treatment version")

        translation_output = {
            "files_created": [],
            "files_modified": ["base.txt"],
        }

        restore_treatment(component_dir, generated_dir, translation_output)
        assert (component_dir / "base.txt").read_text() == "treatment version"

    def test_creates_parent_directories(self, tmp_path):
        component_dir = tmp_path / "component"
        component_dir.mkdir()
        _init_repo(component_dir)

        generated_dir = tmp_path / "generated"
        generated_dir.mkdir()
        (generated_dir / "deep.go").write_text("deep file")

        translation_output = {
            "files_created": ["a/b/c/deep.go"],
            "files_modified": [],
        }

        restore_treatment(component_dir, generated_dir, translation_output)
        assert (component_dir / "a" / "b" / "c" / "deep.go").read_text() == "deep file"


class TestRoundTrip:
    def test_baseline_then_treatment_restores_state(self, tmp_path):
        """Full round-trip: start with treatment state, restore baseline, restore treatment."""
        component_dir = tmp_path / "component"
        component_dir.mkdir()
        _init_repo(component_dir)

        generated_dir = tmp_path / "generated"
        generated_dir.mkdir()
        (generated_dir / "scorer.go").write_text("generated scorer")
        (generated_dir / "base.txt").write_text("modified base")

        translation_output = {
            "files_created": ["pkg/scorer.go"],
            "files_modified": ["base.txt"],
        }

        # Start in treatment state
        restore_treatment(component_dir, generated_dir, translation_output)
        assert (component_dir / "pkg" / "scorer.go").exists()
        assert (component_dir / "base.txt").read_text() == "modified base"

        # Restore to baseline
        restore_baseline(component_dir, translation_output)
        assert not (component_dir / "pkg" / "scorer.go").exists()
        assert (component_dir / "base.txt").read_text() == "original"

        # Restore back to treatment
        restore_treatment(component_dir, generated_dir, translation_output)
        assert (component_dir / "pkg" / "scorer.go").read_text() == "generated scorer"
        assert (component_dir / "base.txt").read_text() == "modified base"
