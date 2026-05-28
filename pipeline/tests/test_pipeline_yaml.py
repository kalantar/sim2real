"""Tests for pipeline.yaml structural correctness."""
import yaml
from pathlib import Path

PIPELINE_YAML = Path(__file__).resolve().parents[2] / "pipeline" / "pipeline.yaml"


class TestCollectResultsParams:
    """collect-results task invocation uses correct params."""

    def _get_collect_results_task(self):
        pipeline = yaml.safe_load(PIPELINE_YAML.read_text())
        tasks = pipeline["spec"]["tasks"]
        cr = [t for t in tasks if t["name"] == "collect-results"]
        assert len(cr) == 1, "Expected exactly one collect-results task"
        return cr[0]

    def test_no_model_label_param(self):
        """modelLabel should not be passed to collect-results."""
        task = self._get_collect_results_task()
        param_names = [p["name"] for p in task["params"]]
        assert "modelLabel" not in param_names

    def test_has_namespace_param(self):
        """collect-results must receive a namespace param."""
        task = self._get_collect_results_task()
        param_names = [p["name"] for p in task["params"]]
        assert "namespace" in param_names

    def test_has_results_dir_param(self):
        """collect-results must receive a resultsDir param."""
        task = self._get_collect_results_task()
        param_names = [p["name"] for p in task["params"]]
        assert "resultsDir" in param_names
