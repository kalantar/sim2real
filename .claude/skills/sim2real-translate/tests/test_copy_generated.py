"""Tests for copy_generated.py — git-based file discovery and copy."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import copy_generated as cg


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo with an initial commit."""
    repo = tmp_path / "target"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, check=True, capture_output=True,
    )
    (repo / "existing.go").write_text("package main")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo


@pytest.fixture
def run_dir(tmp_path):
    """Create a run directory with a minimal translation_output.json."""
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "translation_output.json").write_text(json.dumps({
        "plugin_type": "scorer",
        "files_created": [],
        "files_modified": [],
        "package": "pkg",
        "register_file": "register.go",
        "test_commands": ["go test ./..."],
        "config_kind": "ScorerConfig",
        "treatment_config_generated": True,
        "description": "test plugin",
    }))
    return rd


def test_modified_tracked_file(git_repo, run_dir):
    """Modified tracked files appear in files_modified."""
    (git_repo / "existing.go").write_text("package main\n// changed")
    created, modified = cg.copy_generated(str(git_repo), str(run_dir))
    assert modified == ["existing.go"]
    assert created == []


def test_new_untracked_file(git_repo, run_dir):
    """New untracked files appear in files_created."""
    (git_repo / "pkg" / "plugins").mkdir(parents=True)
    (git_repo / "pkg" / "plugins" / "new_plugin.go").write_text("package plugins")
    created, modified = cg.copy_generated(str(git_repo), str(run_dir))
    assert "pkg/plugins/new_plugin.go" in created
    assert modified == []


def test_files_copied_to_generated(git_repo, run_dir):
    """All listed files exist in generated/ after copy."""
    (git_repo / "existing.go").write_text("package main\n// changed")
    (git_repo / "new_file.go").write_text("package main\n// new")
    cg.copy_generated(str(git_repo), str(run_dir))
    gen = run_dir / "generated"
    assert (gen / "existing.go").exists()
    assert (gen / "new_file.go").exists()


def test_translation_output_updated(git_repo, run_dir):
    """translation_output.json lists are overwritten with git state."""
    o = json.loads((run_dir / "translation_output.json").read_text())
    o["files_created"] = ["stale.go"]
    o["files_modified"] = ["also_stale.go"]
    (run_dir / "translation_output.json").write_text(json.dumps(o))

    (git_repo / "existing.go").write_text("package main\n// changed")
    cg.copy_generated(str(git_repo), str(run_dir))

    result = json.loads((run_dir / "translation_output.json").read_text())
    assert result["files_modified"] == ["existing.go"]
    assert result["files_created"] == []
    assert "stale.go" not in result["files_created"]


def test_empty_diff_empty_lists(git_repo, run_dir):
    """No changes -> empty lists, no files in generated/."""
    created, modified = cg.copy_generated(str(git_repo), str(run_dir))
    assert created == []
    assert modified == []
    gen = run_dir / "generated"
    if gen.exists():
        assert list(gen.iterdir()) == []


def test_nested_path_uses_basename(git_repo, run_dir):
    """Files in subdirectories use basename in generated/."""
    (git_repo / "pkg" / "deep").mkdir(parents=True)
    (git_repo / "pkg" / "deep" / "nested.go").write_text("package deep")
    cg.copy_generated(str(git_repo), str(run_dir))
    gen = run_dir / "generated"
    assert (gen / "nested.go").exists()


def test_mixed_created_and_modified(git_repo, run_dir):
    """Both created and modified files are correctly classified in one call."""
    (git_repo / "existing.go").write_text("package main\n// changed")
    (git_repo / "pkg").mkdir()
    (git_repo / "pkg" / "new_plugin.go").write_text("package pkg")
    created, modified = cg.copy_generated(str(git_repo), str(run_dir))
    assert modified == ["existing.go"]
    assert "pkg/new_plugin.go" in created
    result = json.loads((run_dir / "translation_output.json").read_text())
    assert result["files_modified"] == ["existing.go"]
    assert "pkg/new_plugin.go" in result["files_created"]
    gen = run_dir / "generated"
    assert (gen / "existing.go").exists()
    assert (gen / "new_plugin.go").exists()


def test_config_yamls_preserved(git_repo, run_dir):
    """Config yamls placed by writer agent survive the copy step."""
    gen = run_dir / "generated"
    gen.mkdir()
    (gen / "baseline_config.yaml").write_text("baseline: true")
    (gen / "treatment_config.yaml").write_text("treatment: true")

    (git_repo / "existing.go").write_text("package main\n// changed")
    cg.copy_generated(str(git_repo), str(run_dir))

    assert (gen / "baseline_config.yaml").read_text() == "baseline: true"
    assert (gen / "treatment_config.yaml").read_text() == "treatment: true"
    assert (gen / "existing.go").exists()


def test_basename_collision_raises(git_repo, run_dir):
    """Basename collision between different paths raises ValueError."""
    (git_repo / "pkg" / "scorer").mkdir(parents=True)
    (git_repo / "pkg" / "admission").mkdir(parents=True)
    (git_repo / "pkg" / "scorer" / "config.go").write_text("package scorer")
    (git_repo / "pkg" / "admission" / "config.go").write_text("package admission")
    with pytest.raises(ValueError, match="Basename collision.*config.go"):
        cg.copy_generated(str(git_repo), str(run_dir))


def test_deleted_file_excluded(git_repo, run_dir):
    """Deleted tracked files do not appear in files_modified."""
    (git_repo / "existing.go").unlink()
    created, modified = cg.copy_generated(str(git_repo), str(run_dir))
    assert "existing.go" not in modified
    assert created == []


def test_preserves_other_json_fields(git_repo, run_dir):
    """Other fields in translation_output.json are not disturbed."""
    (git_repo / "existing.go").write_text("package main\n// changed")
    cg.copy_generated(str(git_repo), str(run_dir))
    result = json.loads((run_dir / "translation_output.json").read_text())
    assert result["plugin_type"] == "scorer"
    assert result["description"] == "test plugin"
