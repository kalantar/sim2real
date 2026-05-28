"""Tests for deploy build subcommand."""
import json
import subprocess

import pytest
import yaml

from pipeline.deploy import build_parser


def test_build_parser_exists():
    """build subcommand is registered."""
    parser = build_parser()
    args = parser.parse_args(["build"])
    assert args.command == "build"


def test_build_parser_skip_flag():
    """build subcommand has --skip-build flag."""
    parser = build_parser()
    args = parser.parse_args(["build", "--skip-build"])
    assert args.skip_build is True


def test_run_parser_skip_build_flag():
    """run subcommand has --skip-build (not --skip-build-epp)."""
    parser = build_parser()
    args = parser.parse_args(["run", "--skip-build"])
    assert args.skip_build is True


def test_run_parser_no_skip_build_epp():
    """--skip-build-epp is no longer accepted."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--skip-build-epp"])


class TestCmdBuildScenarioIteration:
    """Integration tests for multi-scenario image build logic."""

    def _make_cluster(self, tmp_path, scenarios: dict):
        """Create cluster/ dir with named scenario YAML files."""
        cluster_dir = tmp_path / "cluster"
        cluster_dir.mkdir()
        for name, content in scenarios.items():
            (cluster_dir / f"{name}.yaml").write_text(yaml.dump(content))
        return cluster_dir

    def test_collects_both_baseline_and_treatment_refs(self, tmp_path):
        from pipeline.lib.ensure_image import collect_scenario_images

        cluster_dir = self._make_cluster(tmp_path, {
            "base1": {"scenario": [{"name": "s", "images": {
                "inferenceScheduler": {"repository": "ghcr.io/org/sched", "tag": "abc12345"}
            }}]},
            "algo1": {"scenario": [{"name": "s", "images": {
                "inferenceScheduler": {"repository": "ghcr.io/org/sched", "tag": "r1"}
            }}]},
        })
        images = collect_scenario_images(cluster_dir)
        refs = {i["image_ref"] for i in images}
        assert "ghcr.io/org/sched:abc12345" in refs
        assert "ghcr.io/org/sched:r1" in refs
        assert len(refs) == 2

    def test_deduplicates_shared_baseline_image(self, tmp_path):
        """Multiple baselines sharing the same image are deduplicated."""
        from pipeline.lib.ensure_image import collect_scenario_images

        cluster_dir = self._make_cluster(tmp_path, {
            "base1": {"scenario": [{"name": "s", "images": {
                "inferenceScheduler": {"repository": "ghcr.io/org/sched", "tag": "abc12345"}
            }}]},
            "base2": {"scenario": [{"name": "s", "images": {
                "inferenceScheduler": {"repository": "ghcr.io/org/sched", "tag": "abc12345"}
            }}]},
        })
        images = collect_scenario_images(cluster_dir)
        assert len(images) == 1

    def test_skipped_when_hash_matches(self, tmp_path):
        """Images with matching source hashes are reported as current."""
        from pipeline.lib.ensure_image import compute_source_hash, image_needs_build

        src = tmp_path / "src"
        src.mkdir()
        subprocess.run(["git", "init", str(src)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(src), "config", "user.name", "T"], check=True, capture_output=True)
        (src / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(src), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(src), "commit", "-m", "i"], check=True, capture_output=True)

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        current = compute_source_hash(src)
        meta = {"version": 1, "stages": {}, "source_hashes": {"ghcr.io/org/sched:abc12345": current}}
        (run_dir / "run_metadata.json").write_text(json.dumps(meta))

        assert image_needs_build(run_dir, "ghcr.io/org/sched:abc12345", src) is False

    def test_needs_build_when_no_stored_hash(self, tmp_path):
        """Images with no stored hash need building."""
        from pipeline.lib.ensure_image import image_needs_build

        src = tmp_path / "src"
        src.mkdir()
        subprocess.run(["git", "init", str(src)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(src), "config", "user.name", "T"], check=True, capture_output=True)
        (src / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(src), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(src), "commit", "-m", "i"], check=True, capture_output=True)

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "run_metadata.json").write_text(json.dumps({"version": 1, "stages": {}}))

        assert image_needs_build(run_dir, "ghcr.io/org/sched:abc12345", src) is True

    def test_treatment_ref_identified_correctly(self, tmp_path):
        """Treatment ref matches {registry}/{repo_name}:{run_name} pattern."""
        from pipeline.lib.ensure_image import collect_scenario_images

        registry = "ghcr.io/org"
        repo_name = "sched"
        run_name = "r1"
        treatment_ref = f"{registry}/{repo_name}:{run_name}"
        assert treatment_ref == "ghcr.io/org/sched:r1"

        cluster_dir = self._make_cluster(tmp_path, {
            "algo1": {"scenario": [{"name": "s", "images": {
                "inferenceScheduler": {"repository": "ghcr.io/org/sched", "tag": "r1"}
            }}]},
        })
        images = collect_scenario_images(cluster_dir)
        assert images[0]["image_ref"] == treatment_ref
