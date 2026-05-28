"""Tests for pipeline/lib/ensure_image.py."""
import json
import subprocess
import yaml
from pathlib import Path

from pipeline.lib.ensure_image import (
    collect_scenario_images,
    compute_baseline_ref,
    compute_source_hash,
    image_needs_build,
    load_source_hashes,
    save_source_hash,
)


def _init_git_repo(path: Path) -> None:
    """Create a git repo with an initial commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True, capture_output=True)
    (path / "file.txt").write_text("hello")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


class TestComputeSourceHash:
    def test_returns_40_char_hex(self, tmp_path):
        _init_git_repo(tmp_path)
        result = compute_source_hash(tmp_path)
        assert len(result) == 40
        assert all(c in "0123456789abcdef" for c in result)

    def test_changes_on_new_commit(self, tmp_path):
        _init_git_repo(tmp_path)
        hash1 = compute_source_hash(tmp_path)
        (tmp_path / "file.txt").write_text("changed")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "edit"], check=True, capture_output=True)
        hash2 = compute_source_hash(tmp_path)
        assert hash1 != hash2


class TestLoadSourceHashes:
    def test_returns_empty_when_no_file(self, tmp_path):
        assert load_source_hashes(tmp_path) == {}

    def test_returns_empty_when_no_key(self, tmp_path):
        (tmp_path / "run_metadata.json").write_text(json.dumps({"version": 1, "stages": {}}))
        assert load_source_hashes(tmp_path) == {}

    def test_returns_stored_hashes(self, tmp_path):
        meta = {"version": 1, "stages": {}, "source_hashes": {"img:tag": "abc123"}}
        (tmp_path / "run_metadata.json").write_text(json.dumps(meta))
        assert load_source_hashes(tmp_path) == {"img:tag": "abc123"}


class TestSaveSourceHash:
    def test_creates_key(self, tmp_path):
        meta = {"version": 1, "stages": {}}
        (tmp_path / "run_metadata.json").write_text(json.dumps(meta))
        save_source_hash(tmp_path, "ghcr.io/me/repo:v1", "deadbeef" * 5)
        loaded = json.loads((tmp_path / "run_metadata.json").read_text())
        assert loaded["source_hashes"]["ghcr.io/me/repo:v1"] == "deadbeef" * 5

    def test_preserves_existing_hashes(self, tmp_path):
        meta = {"version": 1, "stages": {}, "source_hashes": {"old:ref": "aaa"}}
        (tmp_path / "run_metadata.json").write_text(json.dumps(meta))
        save_source_hash(tmp_path, "new:ref", "bbb")
        loaded = json.loads((tmp_path / "run_metadata.json").read_text())
        assert loaded["source_hashes"]["old:ref"] == "aaa"
        assert loaded["source_hashes"]["new:ref"] == "bbb"


class TestImageNeedsBuild:
    def test_needs_build_when_no_stored_hash(self, tmp_path):
        (tmp_path / "run_metadata.json").write_text(json.dumps({"version": 1, "stages": {}}))
        src = tmp_path / "src"
        src.mkdir()
        _init_git_repo(src)
        assert image_needs_build(tmp_path, "img:tag", src) is True

    def test_no_build_when_hash_matches(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        _init_git_repo(src)
        current_hash = compute_source_hash(src)
        meta = {"version": 1, "stages": {}, "source_hashes": {"img:tag": current_hash}}
        (tmp_path / "run_metadata.json").write_text(json.dumps(meta))
        assert image_needs_build(tmp_path, "img:tag", src) is False

    def test_needs_build_when_hash_differs(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        _init_git_repo(src)
        meta = {"version": 1, "stages": {}, "source_hashes": {"img:tag": "stale_hash"}}
        (tmp_path / "run_metadata.json").write_text(json.dumps(meta))
        assert image_needs_build(tmp_path, "img:tag", src) is True


class TestComputeBaselineRef:
    def test_returns_short_sha_tag(self, tmp_path):
        _init_git_repo(tmp_path)
        full_hash = compute_source_hash(tmp_path)
        result = compute_baseline_ref("ghcr.io/org/scheduler", tmp_path)
        assert result == f"ghcr.io/org/scheduler:{full_hash[:8]}"

    def test_uses_8_char_prefix(self, tmp_path):
        _init_git_repo(tmp_path)
        result = compute_baseline_ref("ghcr.io/org/scheduler", tmp_path)
        tag = result.split(":")[-1]
        assert len(tag) == 8
        assert all(c in "0123456789abcdef" for c in tag)


class TestCollectScenarioImages:
    def test_extracts_images_from_scenarios(self, tmp_path):
        cluster_dir = tmp_path / "cluster"
        cluster_dir.mkdir()
        baseline = {
            "scenario": [{"name": "s1", "images": {
                "inferenceScheduler": {"repository": "ghcr.io/org/scheduler", "tag": "abc1234"}
            }}]
        }
        (cluster_dir / "base1.yaml").write_text(yaml.dump(baseline))
        algo = {
            "scenario": [{"name": "s1", "images": {
                "inferenceScheduler": {"repository": "ghcr.io/org/scheduler", "tag": "r1"}
            }}]
        }
        (cluster_dir / "algo1.yaml").write_text(yaml.dump(algo))

        result = collect_scenario_images(cluster_dir)
        refs = {r["image_ref"] for r in result}
        assert "ghcr.io/org/scheduler:abc1234" in refs
        assert "ghcr.io/org/scheduler:r1" in refs

    def test_deduplicates_identical_refs(self, tmp_path):
        cluster_dir = tmp_path / "cluster"
        cluster_dir.mkdir()
        scenario = {
            "scenario": [{"name": "s1", "images": {
                "inferenceScheduler": {"repository": "ghcr.io/org/sched", "tag": "v1"}
            }}]
        }
        (cluster_dir / "base1.yaml").write_text(yaml.dump(scenario))
        (cluster_dir / "base2.yaml").write_text(yaml.dump(scenario))

        result = collect_scenario_images(cluster_dir)
        assert len(result) == 1

    def test_skips_pipelinerun_files(self, tmp_path):
        cluster_dir = tmp_path / "cluster"
        cluster_dir.mkdir()
        pr = {"scenario": [{"name": "s", "images": {
            "inferenceScheduler": {"repository": "r", "tag": "t"}
        }}]}
        (cluster_dir / "pipelinerun-wl1-base.yaml").write_text(yaml.dump(pr))

        result = collect_scenario_images(cluster_dir)
        assert result == []

    def test_skips_scenarios_without_images(self, tmp_path):
        cluster_dir = tmp_path / "cluster"
        cluster_dir.mkdir()
        scenario = {"scenario": [{"name": "s1"}]}
        (cluster_dir / "base1.yaml").write_text(yaml.dump(scenario))

        result = collect_scenario_images(cluster_dir)
        assert result == []

    def test_empty_cluster_dir(self, tmp_path):
        cluster_dir = tmp_path / "cluster"
        cluster_dir.mkdir()
        assert collect_scenario_images(cluster_dir) == []
