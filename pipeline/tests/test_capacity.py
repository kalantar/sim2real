"""Tests for pipeline/lib/capacity.py — GPU capacity probe."""

import json
from unittest.mock import patch, MagicMock

from pipeline.lib.capacity import probe_free_gpus, derive_gpu_resource_type, gpu_cost_per_pair


class TestProbeFreeGpus:
    def _mock_nodes(self, allocatable_gpus: list[int], resource="nvidia.com/gpu"):
        """Build fake kubectl nodes JSON."""
        nodes = []
        for gpu_count in allocatable_gpus:
            node = {
                "metadata": {"name": f"node-{len(nodes)}"},
                "status": {
                    "allocatable": {resource: str(gpu_count)} if gpu_count > 0 else {}
                },
            }
            nodes.append(node)
        return json.dumps({"items": nodes})

    def _mock_pods(self, requested_gpus: list[int], resource="nvidia.com/gpu",
                   node_names: list[str | None] | None = None):
        """Build fake kubectl pods JSON.

        node_names: per-pod nodeName (None = Pending, no nodeName).
        Defaults to all pods having a nodeName.
        """
        if node_names is None:
            node_names = [f"node-{i}" for i in range(len(requested_gpus))]
        pods = []
        for i, gpu_req in enumerate(requested_gpus):
            spec: dict = {
                "containers": [
                    {"resources": {"requests": {resource: str(gpu_req)}}}
                ]
            }
            if node_names[i] is not None:
                spec["nodeName"] = node_names[i]
            pod = {
                "metadata": {"name": f"pod-{len(pods)}"},
                "spec": spec,
            }
            pods.append(pod)
        return json.dumps({"items": pods})

    @patch("pipeline.lib.capacity.subprocess.run")
    def test_basic_computation(self, mock_run):
        nodes_json = self._mock_nodes([8] * 14)
        pods_json = self._mock_pods([1] * 108)

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=nodes_json),
            MagicMock(returncode=0, stdout=pods_json),
        ]

        result = probe_free_gpus()
        assert result == (4, 112, 108)

    @patch("pipeline.lib.capacity.subprocess.run")
    def test_clamps_to_zero(self, mock_run):
        nodes_json = self._mock_nodes([2])
        pods_json = self._mock_pods([1] * 5)

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=nodes_json),
            MagicMock(returncode=0, stdout=pods_json),
        ]

        result = probe_free_gpus()
        assert result == (0, 2, 5)

    @patch("pipeline.lib.capacity.subprocess.run")
    def test_custom_resource_type(self, mock_run):
        nodes_json = self._mock_nodes([4, 4], resource="habana.ai/gaudi")
        pods_json = self._mock_pods([2], resource="habana.ai/gaudi")

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=nodes_json),
            MagicMock(returncode=0, stdout=pods_json),
        ]

        result = probe_free_gpus(gpu_resource_type="habana.ai/gaudi")
        assert result == (6, 8, 2)

    @patch("pipeline.lib.capacity.subprocess.run")
    def test_kubectl_failure_returns_error_string(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="connection refused")

        result = probe_free_gpus()
        assert isinstance(result, str)
        assert "connection refused" in result

    @patch("pipeline.lib.capacity.subprocess.run")
    def test_nodes_without_gpu_resource_ignored(self, mock_run):
        nodes = {
            "items": [
                {"metadata": {"name": "gpu-0"}, "status": {"allocatable": {"nvidia.com/gpu": "8", "cpu": "64"}}},
                {"metadata": {"name": "cpu-0"}, "status": {"allocatable": {"cpu": "96"}}},
            ]
        }
        pods = {"items": []}
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(nodes)),
            MagicMock(returncode=0, stdout=json.dumps(pods)),
        ]

        result = probe_free_gpus()
        assert result == (8, 8, 0)

    @patch("pipeline.lib.capacity.subprocess.run")
    def test_malformed_gpu_value_returns_error(self, mock_run):
        nodes = {"items": [
            {"metadata": {"name": "n0"}, "status": {"allocatable": {"nvidia.com/gpu": "8000m"}}}
        ]}
        pods = {"items": []}
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(nodes)),
            MagicMock(returncode=0, stdout=json.dumps(pods)),
        ]
        result = probe_free_gpus()
        assert isinstance(result, str)
        assert "8000m" in result

    @patch("pipeline.lib.capacity.subprocess.run")
    def test_malformed_json_returns_error(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="not json"),
            MagicMock(returncode=0, stdout="{}"),
        ]
        result = probe_free_gpus()
        assert isinstance(result, str)
        assert "JSON parse error" in result

    @patch("pipeline.lib.capacity.subprocess.run")
    def test_pending_pods_excluded(self, mock_run):
        """Pending pods (no nodeName) should not count as GPU consumers."""
        nodes_json = self._mock_nodes([8, 8])
        pods_json = self._mock_pods(
            [4, 4, 4],
            node_names=["node-0", "node-1", None],
        )

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=nodes_json),
            MagicMock(returncode=0, stdout=pods_json),
        ]

        result = probe_free_gpus()
        assert result == (8, 16, 8)

    @patch("pipeline.lib.capacity.subprocess.run")
    def test_all_pods_pending_means_zero_requested(self, mock_run):
        """When every pod is Pending, total requested should be zero."""
        nodes_json = self._mock_nodes([8, 8])
        pods_json = self._mock_pods(
            [4, 4],
            node_names=[None, None],
        )

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=nodes_json),
            MagicMock(returncode=0, stdout=pods_json),
        ]

        result = probe_free_gpus()
        assert result == (16, 16, 0)


# ── derive_gpu_resource_type tests ─────────────────────────────────────────────


class TestDeriveGpuResourceType:
    def test_derives_from_defaults(self):
        defaults = {"accelerator": {"resource": "nvidia.com/gpu"}}
        scenario = {"scenario": [{"name": "test"}]}
        assert derive_gpu_resource_type(scenario, defaults) == "nvidia.com/gpu"

    def test_scenario_overrides_defaults(self):
        defaults = {"accelerator": {"resource": "nvidia.com/gpu"}}
        scenario = {"scenario": [{"name": "test", "accelerator": {"resource": "habana.ai/gaudi"}}]}
        assert derive_gpu_resource_type(scenario, defaults) == "habana.ai/gaudi"

    def test_missing_accelerator_falls_back(self):
        defaults = {}
        scenario = {"scenario": [{"name": "test"}]}
        assert derive_gpu_resource_type(scenario, defaults) == "nvidia.com/gpu"


# ── gpu_cost_per_pair tests ────────────────────────────────────────────────────


class TestGpuCostPerPair:
    def test_default_single_decode_replica(self):
        defaults = {
            "accelerator": {"resource": "nvidia.com/gpu"},
            "decode": {
                "enabled": True,
                "replicas": 1,
                "parallelism": {"tensor": 1, "dataLocal": 1},
            },
            "prefill": {"enabled": False, "replicas": 0},
        }
        scenario = {
            "scenario": [{"name": "test", "decode": {"replicas": 4}}]
        }
        cost = gpu_cost_per_pair(scenario, defaults)
        assert cost == 4

    def test_tensor_parallel_derivation(self):
        defaults = {
            "decode": {
                "enabled": True,
                "replicas": 1,
                "parallelism": {"tensor": 4, "dataLocal": 1},
            },
            "prefill": {"enabled": False, "replicas": 0},
        }
        scenario = {"scenario": [{"name": "test", "decode": {"replicas": 2}}]}
        cost = gpu_cost_per_pair(scenario, defaults)
        assert cost == 8

    def test_role_accelerator_count_overrides_parallelism(self):
        defaults = {
            "decode": {
                "enabled": True,
                "replicas": 1,
                "parallelism": {"tensor": 4, "dataLocal": 1},
            },
            "prefill": {"enabled": False, "replicas": 0},
        }
        scenario = {
            "scenario": [{"name": "test", "decode": {"replicas": 2, "accelerator": {"count": 2}}}]
        }
        cost = gpu_cost_per_pair(scenario, defaults)
        assert cost == 4

    def test_top_level_accelerator_count_zero_means_cpu_only(self):
        defaults = {
            "accelerator": {"count": 0},
            "decode": {"enabled": True, "replicas": 4, "parallelism": {"tensor": 4, "dataLocal": 1}},
            "prefill": {"enabled": False, "replicas": 0},
        }
        scenario = {"scenario": [{"name": "test"}]}
        cost = gpu_cost_per_pair(scenario, defaults)
        assert cost == 0

    def test_prefill_enabled_adds_cost(self):
        defaults = {
            "decode": {
                "enabled": True,
                "replicas": 2,
                "parallelism": {"tensor": 1, "dataLocal": 1},
            },
            "prefill": {
                "enabled": False,
                "replicas": 0,
                "parallelism": {"tensor": 1, "dataLocal": 1},
            },
        }
        scenario = {
            "scenario": [{"name": "test", "prefill": {"enabled": True, "replicas": 1}}]
        }
        cost = gpu_cost_per_pair(scenario, defaults)
        assert cost == 3

    def test_top_level_accelerator_count_as_fallback(self):
        defaults = {
            "accelerator": {"count": 8},
            "decode": {"enabled": True, "replicas": 1, "parallelism": {"tensor": 2, "dataLocal": 2}},
            "prefill": {"enabled": False, "replicas": 0},
        }
        scenario = {"scenario": [{"name": "test"}]}
        cost = gpu_cost_per_pair(scenario, defaults)
        assert cost == 8

    def test_empty_scenario_returns_defaults_cost(self):
        defaults = {
            "decode": {"enabled": True, "replicas": 1, "parallelism": {"tensor": 1, "dataLocal": 1}},
            "prefill": {"enabled": False, "replicas": 0},
        }
        scenario = {"scenario": [{"name": "test"}]}
        cost = gpu_cost_per_pair(scenario, defaults)
        assert cost == 1

    def test_non_numeric_accelerator_count_returns_error(self):
        defaults = {
            "decode": {"enabled": True, "replicas": 1, "parallelism": {"tensor": 1, "dataLocal": 1}},
            "prefill": {"enabled": False, "replicas": 0},
        }
        scenario = {"scenario": [{"name": "test", "accelerator": {"count": "auto"}}]}
        cost = gpu_cost_per_pair(scenario, defaults)
        assert isinstance(cost, str)
        assert "auto" in cost
        assert "accelerator.count" in cost

    def test_non_numeric_role_accelerator_count_returns_error(self):
        defaults = {
            "decode": {"enabled": True, "replicas": 1, "parallelism": {"tensor": 1, "dataLocal": 1}},
            "prefill": {"enabled": False, "replicas": 0},
        }
        scenario = {"scenario": [{"name": "test", "decode": {"accelerator": {"count": "bad"}}}]}
        cost = gpu_cost_per_pair(scenario, defaults)
        assert isinstance(cost, str)
        assert "bad" in cost
        assert "decode.accelerator.count" in cost
