# pipeline/tests/test_pod_pending.py
"""Tests for pod pending reason classification."""


def test_classify_insufficient_gpu():
    from pipeline.lib.pod_pending import classify_pending_reason
    result = classify_pending_reason(
        "0/8 nodes are available: 8 Insufficient nvidia.com/gpu. "
        "preemption: 0/8 nodes are available: 8 No preemption victims found."
    )
    assert result == "recoverable"


def test_classify_insufficient_memory():
    from pipeline.lib.pod_pending import classify_pending_reason
    result = classify_pending_reason(
        "0/8 nodes are available: 8 Insufficient memory."
    )
    assert result == "recoverable"


def test_classify_insufficient_cpu():
    from pipeline.lib.pod_pending import classify_pending_reason
    result = classify_pending_reason(
        "0/4 nodes are available: 4 Insufficient cpu."
    )
    assert result == "recoverable"


def test_classify_nodes_unavailable_capacity():
    from pipeline.lib.pod_pending import classify_pending_reason
    result = classify_pending_reason(
        "0/6 nodes are available: 2 Insufficient nvidia.com/gpu, "
        "4 node(s) had untolerated taint {nvidia.com/gpu.deploy.container-toolkit=true}."
    )
    assert result == "recoverable"


def test_classify_node_affinity_mismatch():
    from pipeline.lib.pod_pending import classify_pending_reason
    result = classify_pending_reason(
        "0/8 nodes are available: 8 node(s) didn't match Pod's node affinity/selector."
    )
    assert result == "non_recoverable"


def test_classify_pvc_not_found():
    from pipeline.lib.pod_pending import classify_pending_reason
    result = classify_pending_reason(
        'persistentvolumeclaim "data-pvc" not found'
    )
    assert result == "non_recoverable"


def test_classify_taint_toleration_only():
    from pipeline.lib.pod_pending import classify_pending_reason
    result = classify_pending_reason(
        "0/4 nodes are available: 4 node(s) had untolerated taint "
        "{node.kubernetes.io/unschedulable: }."
    )
    assert result == "non_recoverable"


def test_classify_empty_message():
    from pipeline.lib.pod_pending import classify_pending_reason
    result = classify_pending_reason("")
    assert result == "non_recoverable"


def test_classify_unknown_message_defaults_recoverable():
    """Unrecognized messages default to recoverable (safer: wait vs cancel)."""
    from pipeline.lib.pod_pending import classify_pending_reason
    result = classify_pending_reason(
        "0/3 nodes are available: 3 custom-scheduler-reason."
    )
    assert result == "recoverable"


def test_parse_pending_pod_recoverable():
    from pipeline.lib.pod_pending import parse_pod_conditions
    pods_json = {
        "items": [{
            "status": {
                "phase": "Pending",
                "conditions": [{
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": "0/8 nodes are available: 8 Insufficient nvidia.com/gpu.",
                }],
            },
        }],
    }
    category, detail = parse_pod_conditions(pods_json)
    assert category == "recoverable"
    assert "Insufficient nvidia.com/gpu" in detail


def test_parse_pending_pod_non_recoverable():
    from pipeline.lib.pod_pending import parse_pod_conditions
    pods_json = {
        "items": [{
            "status": {
                "phase": "Pending",
                "conditions": [{
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": "0/8 nodes are available: 8 node(s) didn't match Pod's node affinity/selector.",
                }],
            },
        }],
    }
    category, detail = parse_pod_conditions(pods_json)
    assert category == "non_recoverable"
    assert "node affinity" in detail


def test_parse_running_pod_returns_none():
    from pipeline.lib.pod_pending import parse_pod_conditions
    pods_json = {
        "items": [{
            "status": {
                "phase": "Running",
                "conditions": [{
                    "type": "Ready",
                    "status": "True",
                }],
            },
        }],
    }
    category, detail = parse_pod_conditions(pods_json)
    assert category is None
    assert detail == ""


def test_parse_no_pods_returns_none():
    from pipeline.lib.pod_pending import parse_pod_conditions
    category, detail = parse_pod_conditions({"items": []})
    assert category is None
    assert detail == ""


def test_parse_mixed_pods_pending_wins():
    """If any pod is Pending+Unschedulable, report it even if others are Running."""
    from pipeline.lib.pod_pending import parse_pod_conditions
    pods_json = {
        "items": [
            {
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            },
            {
                "status": {
                    "phase": "Pending",
                    "conditions": [{
                        "type": "PodScheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": "0/4 nodes are available: 4 Insufficient memory.",
                    }],
                },
            },
        ],
    }
    category, detail = parse_pod_conditions(pods_json)
    assert category == "recoverable"


def test_parse_pending_without_conditions_returns_none():
    """Pending pod with no conditions (e.g. image pull) is not flagged."""
    from pipeline.lib.pod_pending import parse_pod_conditions
    pods_json = {
        "items": [{
            "status": {
                "phase": "Pending",
            },
        }],
    }
    category, detail = parse_pod_conditions(pods_json)
    assert category is None


def test_parse_pending_scheduled_true_returns_none():
    """Pending pod that IS scheduled (e.g. pulling image) is not flagged."""
    from pipeline.lib.pod_pending import parse_pod_conditions
    pods_json = {
        "items": [{
            "status": {
                "phase": "Pending",
                "conditions": [{
                    "type": "PodScheduled",
                    "status": "True",
                }],
            },
        }],
    }
    category, detail = parse_pod_conditions(pods_json)
    assert category is None


def test_parse_multi_pod_worst_severity_wins():
    """Non-recoverable on a later pod overrides recoverable on earlier pod."""
    from pipeline.lib.pod_pending import parse_pod_conditions
    pods_json = {
        "items": [
            {
                "status": {
                    "phase": "Pending",
                    "conditions": [{
                        "type": "PodScheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": "0/8 nodes are available: 8 Insufficient nvidia.com/gpu.",
                    }],
                },
            },
            {
                "status": {
                    "phase": "Pending",
                    "conditions": [{
                        "type": "PodScheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": "0/8 nodes are available: 8 node(s) didn't match Pod's node affinity/selector.",
                    }],
                },
            },
        ],
    }
    category, detail = parse_pod_conditions(pods_json)
    assert category == "non_recoverable"
    assert "node affinity" in detail
