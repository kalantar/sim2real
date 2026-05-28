"""Tests for pipeline.lib.health."""
import json
from unittest.mock import patch, MagicMock


def test_pod_state_dataclass():
    from pipeline.lib.health import PodState
    pod = PodState(name="test-pod", phase="Running", ready=True,
                   restart_count=0, reason="", message="")
    assert pod.name == "test-pod"
    assert pod.ready is True


def test_event_record_dataclass():
    from pipeline.lib.health import EventRecord
    evt = EventRecord(reason="FailedScheduling", message="no nodes",
                      count=1, last_timestamp="2026-01-01T00:00:00Z",
                      involved_object="my-pod")
    assert evt.reason == "FailedScheduling"


def test_triage_result_dataclass():
    from pipeline.lib.health import TriageResult
    r = TriageResult(tier=1, message="deleted", suggestion="",
                     needs_logs=False, action="delete_pod")
    assert r.tier == 1


_PODS_JSON = json.dumps({
    "items": [
        {
            "metadata": {"name": "sim2real-ac-decode-0"},
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "False"}],
                "containerStatuses": [{
                    "name": "vllm", "ready": False, "restartCount": 2,
                    "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                }],
            },
        },
        {
            "metadata": {"name": "sim2real-ac-epp-0"},
            "status": {
                "phase": "Pending",
                "conditions": [],
                "containerStatuses": [],
            },
        },
        {
            "metadata": {"name": "sim2real-ac-decode-1"},
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{
                    "name": "vllm", "ready": True, "restartCount": 0,
                    "lastState": {}, "state": {"running": {}},
                }],
            },
        },
        {
            "metadata": {"name": "sim2real-ac-evicted-0"},
            "status": {
                "phase": "Failed",
                "reason": "Evicted",
                "message": "The node was low on resource: memory.",
                "conditions": [],
                "containerStatuses": [{
                    "name": "vllm", "ready": False, "restartCount": 1,
                    "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
                    "state": {"terminated": {"reason": "Error"}},
                }],
            },
        },
    ]
})

_EVENTS_JSON = json.dumps({
    "items": [
        {
            "reason": "FailedScheduling",
            "message": "0/5 nodes available: 5 Insufficient nvidia.com/gpu",
            "count": 3,
            "lastTimestamp": "2026-04-27T14:30:00Z",
            "involvedObject": {"name": "sim2real-ac-epp-0", "kind": "Pod"},
        },
        {
            "reason": "OOMKilling",
            "message": "Memory cgroup out of memory",
            "count": 1,
            "lastTimestamp": "2026-04-27T14:28:00Z",
            "involvedObject": {"name": "sim2real-ac-decode-0", "kind": "Pod"},
        },
    ]
})


def test_parse_pods_count():
    from pipeline.lib.health import parse_pods
    assert len(parse_pods(_PODS_JSON)) == 4


def test_parse_pods_oom_killed():
    from pipeline.lib.health import parse_pods
    pods = parse_pods(_PODS_JSON)
    p = next(x for x in pods if x.name == "sim2real-ac-decode-0")
    assert p.reason == "OOMKilled"
    assert p.restart_count == 2
    assert p.ready is False


def test_parse_pods_pending():
    from pipeline.lib.health import parse_pods
    pods = parse_pods(_PODS_JSON)
    p = next(x for x in pods if x.name == "sim2real-ac-epp-0")
    assert p.phase == "Pending"
    assert p.ready is False


def test_parse_pods_healthy():
    from pipeline.lib.health import parse_pods
    pods = parse_pods(_PODS_JSON)
    p = next(x for x in pods if x.name == "sim2real-ac-decode-1")
    assert p.ready is True
    assert p.reason == ""


def test_parse_pods_evicted_overrides_oom():
    from pipeline.lib.health import parse_pods
    pods = parse_pods(_PODS_JSON)
    p = next(x for x in pods if x.name == "sim2real-ac-evicted-0")
    assert p.reason == "Evicted"
    assert "memory" in p.message.lower()


def test_parse_events_count():
    from pipeline.lib.health import parse_events
    assert len(parse_events(_EVENTS_JSON)) == 2


def test_parse_events_fields():
    from pipeline.lib.health import parse_events
    events = parse_events(_EVENTS_JSON)
    sched = next(e for e in events if e.reason == "FailedScheduling")
    assert sched.involved_object == "sim2real-ac-epp-0"
    assert sched.count == 3


def _make_pod(**kwargs):
    from pipeline.lib.health import PodState
    defaults = dict(name="p", phase="Running", ready=False,
                    restart_count=0, reason="", message="")
    defaults.update(kwargs)
    return PodState(**defaults)


def _make_event(reason="", message="", involved_object="p"):
    from pipeline.lib.health import EventRecord
    return EventRecord(reason=reason, message=message, count=1,
                       last_timestamp="", involved_object=involved_object)


def test_triage_healthy_pod_returns_none():
    from pipeline.lib.health import triage_pod, RemediationTracker
    pod = _make_pod(phase="Running", ready=True, reason="")
    assert triage_pod(pod, [], RemediationTracker()) is None


def test_triage_evicted():
    from pipeline.lib.health import triage_pod, RemediationTracker
    pod = _make_pod(reason="Evicted", phase="Failed")
    result = triage_pod(pod, [], RemediationTracker())
    assert result.tier == 1
    assert result.action == "delete_pod"


def test_triage_oom_first_attempt():
    from pipeline.lib.health import triage_pod, RemediationTracker
    pod = _make_pod(reason="OOMKilled")
    result = triage_pod(pod, [], RemediationTracker())
    assert result.tier == 1
    assert result.action == "delete_pod"
    assert "1/2" in result.message


def test_triage_oom_second_attempt():
    from pipeline.lib.health import triage_pod, RemediationTracker
    tracker = RemediationTracker()
    tracker.record("p")  # first attempt already recorded
    pod = _make_pod(name="p", reason="OOMKilled")
    result = triage_pod(pod, [], tracker)
    assert result.tier == 1
    assert result.action == "delete_pod"
    assert "2/2" in result.message


def test_triage_oom_escalates_on_third():
    from pipeline.lib.health import triage_pod, RemediationTracker
    tracker = RemediationTracker()
    tracker.record("p")
    tracker.record("p")  # count is now 2; next call sees attempt 3
    pod = _make_pod(name="p", reason="OOMKilled")
    result = triage_pod(pod, [], tracker)
    assert result.tier == 2
    assert result.action == "suggest"
    assert "gpu-memory-utilization" in result.suggestion


def test_triage_image_pull_backoff():
    from pipeline.lib.health import triage_pod, RemediationTracker
    pod = _make_pod(reason="ImagePullBackOff", phase="Pending",
                    message="Back-off pulling ghcr.io/example/bad:tag")
    result = triage_pod(pod, [], RemediationTracker())
    assert result.tier == 2
    assert result.action == "suggest"
    assert "run_metadata.json" in result.suggestion


def test_triage_failed_scheduling_gpu():
    from pipeline.lib.health import triage_pod, RemediationTracker
    pod = _make_pod(phase="Pending")
    events = [_make_event(reason="FailedScheduling",
                          message="0/5 nodes available: 5 Insufficient nvidia.com/gpu")]
    result = triage_pod(pod, events, RemediationTracker())
    assert result.tier == 2
    assert "affinity" in result.suggestion.lower() or "nvidia" in result.suggestion.lower()


def test_triage_quota_exceeded():
    from pipeline.lib.health import triage_pod, RemediationTracker
    pod = _make_pod(phase="Pending")
    events = [_make_event(reason="FailedScheduling",
                          message="exceeded quota: requests.nvidia.com/gpu=4")]
    result = triage_pod(pod, events, RemediationTracker())
    assert result.tier == 2
    assert "quota" in result.suggestion.lower()


def test_triage_crash_loop_tier3():
    from pipeline.lib.health import triage_pod, RemediationTracker
    pod = _make_pod(reason="CrashLoopBackOff", restart_count=5)
    result = triage_pod(pod, [], RemediationTracker())
    assert result.tier == 3
    assert result.needs_logs is True


def test_triage_does_not_modify_tracker():
    from pipeline.lib.health import triage_pod, RemediationTracker
    tracker = RemediationTracker()
    tracker.record("p")
    tracker.record("p")
    before = tracker.count("p")
    pod = _make_pod(name="p", reason="OOMKilled")
    triage_pod(pod, [], tracker)
    assert tracker.count("p") == before, "triage_pod must not modify tracker"


def test_triage_startup_probe_unhealthy():
    from pipeline.lib.health import triage_pod, RemediationTracker
    pod = _make_pod(phase="Running", ready=False)
    events = [_make_event(reason="Unhealthy",
                          message="Startup probe failed: connection refused")]
    result = triage_pod(pod, events, RemediationTracker())
    assert result is not None
    assert result.tier == 2
    assert "startup probe" in result.message.lower()
    assert "failureThreshold" in result.suggestion


def test_get_pods_calls_kubectl():
    from pipeline.lib.health import get_pods
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _PODS_JSON
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        pods = get_pods("kalantar-0", "ac")
    cmd = mock_run.call_args[0][0]
    assert "kubectl" in cmd
    assert "kalantar-0" in " ".join(cmd)
    assert len(pods) == 4


def test_get_pods_filters_by_experiment_id():
    from pipeline.lib.health import get_pods
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _PODS_JSON
    with patch("subprocess.run", return_value=mock_result):
        pods = get_pods("kalantar-0", "ac")
    assert all("ac" in p.name for p in pods)


def test_get_pods_returns_empty_on_error():
    from pipeline.lib.health import get_pods
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    with patch("subprocess.run", return_value=mock_result):
        assert get_pods("kalantar-0", "ac") == []


def test_get_events_calls_kubectl():
    from pipeline.lib.health import get_events
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = _EVENTS_JSON
    with patch("subprocess.run", return_value=mock_result):
        events = get_events("kalantar-0")
    assert len(events) == 2


def test_delete_pod_calls_kubectl():
    from pipeline.lib.health import delete_pod
    mock_result = MagicMock()
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        result = delete_pod("kalantar-0", "sim2real-ac-decode-0")
    assert result is True
    cmd = mock_run.call_args[0][0]
    assert "delete" in cmd
    assert "sim2real-ac-decode-0" in cmd


def test_get_pod_logs_previous_flag():
    from pipeline.lib.health import get_pod_logs
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "some logs"
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        logs = get_pod_logs("kalantar-0", "sim2real-ac-decode-0",
                            tail=200, previous=True)
    assert logs == "some logs"
    cmd = mock_run.call_args[0][0]
    assert "--previous" in cmd
    assert "--tail=200" in cmd


def test_get_events_returns_empty_on_error():
    from pipeline.lib.health import get_events
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    with patch("subprocess.run", return_value=mock_result):
        assert get_events("kalantar-0") == []


def test_get_pod_logs_returns_empty_on_error():
    from pipeline.lib.health import get_pod_logs
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    with patch("subprocess.run", return_value=mock_result):
        assert get_pod_logs("kalantar-0", "sim2real-ac-decode-0") == ""


def test_delete_pod_returns_false_on_error():
    from pipeline.lib.health import delete_pod
    mock_result = MagicMock()
    mock_result.returncode = 1
    with patch("subprocess.run", return_value=mock_result):
        assert delete_pod("kalantar-0", "sim2real-ac-decode-0") is False


def test_triage_failed_phase_no_reason_is_tier3():
    from pipeline.lib.health import triage_pod, PodState, RemediationTracker
    pod = PodState(name="sim2real-ac-decode-0", phase="Failed",
                   reason="", message="", ready=False, restart_count=0)
    result = triage_pod(pod, [], RemediationTracker())
    assert result is not None
    assert result.tier == 3


def test_triage_unknown_phase_is_tier3():
    from pipeline.lib.health import triage_pod, PodState, RemediationTracker
    pod = PodState(name="sim2real-ac-decode-0", phase="Unknown",
                   reason="", message="", ready=False, restart_count=0)
    result = triage_pod(pod, [], RemediationTracker())
    assert result is not None
    assert result.tier == 3


def test_triage_err_image_pull_is_tier2():
    from pipeline.lib.health import triage_pod, PodState, RemediationTracker
    pod = PodState(name="sim2real-ac-epp-0", phase="Pending",
                   reason="ErrImagePull", message="", ready=False, restart_count=0)
    result = triage_pod(pod, [], RemediationTracker())
    assert result is not None
    assert result.tier == 2
    assert "ErrImagePull" in result.message


def test_kubectl_timeout_returns_error():
    import subprocess
    from pipeline.lib.health import _kubectl
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("kubectl", 30)):
        rc, stdout = _kubectl("get", "pods")
    assert rc != 0
    assert stdout == ""


def test_parse_pods_bad_json_returns_empty():
    from pipeline.lib.health import parse_pods
    assert parse_pods("not json") == []


def test_parse_events_bad_json_returns_empty():
    from pipeline.lib.health import parse_events
    assert parse_events("{invalid}") == []
