"""Tests for pipeline/lib/backoff.py — exponential backoff controller."""

from pipeline.lib.backoff import BackoffController


class TestBackoffController:
    """Unit tests for BackoffController state machine."""

    def test_initial_state_is_normal(self):
        bc = BackoffController(base_interval=30, max_backoff=600)
        assert bc.state == "normal"
        assert bc.backoff_level == 0
        assert bc.effective_interval == 30

    def test_signal_scarcity_enters_backing_off(self):
        bc = BackoffController(base_interval=30, max_backoff=600)
        bc.signal_scarcity(free_gpus=0, min_cost=8)
        assert bc.state == "backing_off"
        assert bc.backoff_level == 1
        assert bc.effective_interval == 60  # 30 * 2^1

    def test_repeated_scarcity_increases_level(self):
        bc = BackoffController(base_interval=30, max_backoff=600)
        bc.signal_scarcity(free_gpus=0, min_cost=8)
        bc.signal_scarcity(free_gpus=0, min_cost=8)
        assert bc.backoff_level == 2
        assert bc.effective_interval == 120  # 30 * 2^2

    def test_backoff_capped_at_max(self):
        bc = BackoffController(base_interval=30, max_backoff=600)
        for _ in range(20):
            bc.signal_scarcity(free_gpus=0, min_cost=8)
        assert bc.effective_interval == 600

    def test_signal_capacity_resets_to_normal(self):
        bc = BackoffController(base_interval=30, max_backoff=600)
        bc.signal_scarcity(free_gpus=0, min_cost=8)
        bc.signal_scarcity(free_gpus=0, min_cost=8)
        assert bc.state == "backing_off"
        bc.signal_capacity(free_gpus=16, max_cost=16)
        assert bc.state == "normal"
        assert bc.backoff_level == 0
        assert bc.effective_interval == 30

    def test_signal_scheduling_success_resets(self):
        bc = BackoffController(base_interval=30, max_backoff=600)
        bc.signal_scarcity(free_gpus=0, min_cost=8)
        bc.signal_scarcity(free_gpus=0, min_cost=8)
        assert bc.state == "backing_off"
        bc.signal_scheduling_success()
        assert bc.state == "normal"
        assert bc.backoff_level == 0
        assert bc.effective_interval == 30

    def test_should_dispatch_false_during_backoff_with_no_capacity(self):
        bc = BackoffController(base_interval=30, max_backoff=600)
        bc.signal_scarcity(free_gpus=0, min_cost=8)
        assert bc.should_dispatch(free_gpus=4, min_cost=8) is False

    def test_should_dispatch_true_during_backoff_with_capacity(self):
        bc = BackoffController(base_interval=30, max_backoff=600)
        bc.signal_scarcity(free_gpus=0, min_cost=8)
        assert bc.should_dispatch(free_gpus=8, min_cost=8) is True

    def test_should_dispatch_true_when_normal(self):
        bc = BackoffController(base_interval=30, max_backoff=600)
        assert bc.should_dispatch(free_gpus=0, min_cost=8) is True

    def test_signal_scarcity_no_op_when_free_gpus_sufficient(self):
        bc = BackoffController(base_interval=30, max_backoff=600)
        bc.signal_scarcity(free_gpus=8, min_cost=8)
        assert bc.state == "normal"
        assert bc.backoff_level == 0

    def test_to_dict_roundtrip(self):
        bc = BackoffController(base_interval=30, max_backoff=600)
        bc.signal_scarcity(free_gpus=2, min_cost=8)
        bc.signal_scarcity(free_gpus=2, min_cost=8)
        data = bc.to_dict()
        bc2 = BackoffController.from_dict(data, base_interval=30, max_backoff=600)
        assert bc2.state == bc.state
        assert bc2.backoff_level == bc.backoff_level
        assert bc2.effective_interval == bc.effective_interval
        assert bc2.last_scarcity_time == bc.last_scarcity_time
        assert bc2.last_probe_free_gpus == bc.last_probe_free_gpus

    def test_signal_capacity_no_reset_when_insufficient(self):
        bc = BackoffController(base_interval=30, max_backoff=600)
        bc.signal_scarcity(free_gpus=0, min_cost=8)
        bc.signal_capacity(free_gpus=8, max_cost=16)
        assert bc.state == "backing_off"
        assert bc.backoff_level == 1

    def test_reclaim_signal_triggers_backoff_after_threshold(self):
        bc = BackoffController(base_interval=30, max_backoff=600, reclaim_threshold=3, reclaim_window=600)
        bc.signal_reclaim()
        assert bc.state == "normal"
        bc.signal_reclaim()
        assert bc.state == "normal"
        bc.signal_reclaim()
        assert bc.state == "backing_off"

    def test_reclaim_signal_window_expiry(self):
        import datetime as _dt
        bc = BackoffController(base_interval=30, max_backoff=600, reclaim_threshold=3, reclaim_window=600)
        old_time = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=700)).isoformat()
        bc._reclaim_times = [old_time, old_time]
        bc.signal_reclaim()
        assert bc.state == "normal"  # old ones expired, only 1 recent

    def test_reclaim_signal_roundtrip(self):
        bc = BackoffController(base_interval=30, max_backoff=600, reclaim_threshold=3, reclaim_window=600)
        bc.signal_reclaim()
        bc.signal_reclaim()
        data = bc.to_dict()
        bc2 = BackoffController.from_dict(data, base_interval=30, max_backoff=600, reclaim_threshold=3, reclaim_window=600)
        assert len(bc2._reclaim_times) == 2
        bc2.signal_reclaim()
        assert bc2.state == "backing_off"

    def test_zero_base_interval_does_not_loop(self):
        bc = BackoffController(base_interval=0, max_backoff=600)
        bc.signal_scarcity(free_gpus=0, min_cost=4)
        assert bc.backoff_level == 0
        assert bc.effective_interval == 0

    def test_from_dict_unknown_state_resets(self, capsys):
        bc = BackoffController.from_dict(
            {"state": "baking_off", "backoff_level": 2},
            base_interval=30, max_backoff=600,
        )
        assert bc.state == "normal"
        assert bc.backoff_level == 0  # reset with state

    def test_from_dict_corrupt_backoff_level(self, capsys):
        bc = BackoffController.from_dict(
            {"state": "backing_off", "backoff_level": "high"},
            base_interval=30, max_backoff=600,
        )
        assert bc.backoff_level == 0

    def test_from_dict_negative_backoff_level(self, capsys):
        bc = BackoffController.from_dict(
            {"state": "backing_off", "backoff_level": -3},
            base_interval=30, max_backoff=600,
        )
        assert bc.backoff_level == 0

    def test_from_dict_empty_dict(self):
        bc = BackoffController.from_dict({}, base_interval=30, max_backoff=600)
        assert bc.state == "normal"
        assert bc.backoff_level == 0
        assert bc._reclaim_times == []

    def test_from_dict_clamps_level_to_max(self):
        bc = BackoffController.from_dict(
            {"state": "backing_off", "backoff_level": 99},
            base_interval=30, max_backoff=600,
        )
        assert bc.backoff_level == bc._level_for_max()

    def test_from_dict_filters_non_string_reclaim_times(self):
        bc = BackoffController.from_dict(
            {"state": "normal", "reclaim_times": ["2026-05-08T14:00:00+00:00", 42, None]},
            base_interval=30, max_backoff=600,
        )
        assert len(bc._reclaim_times) == 1

    def test_from_dict_drops_unparseable_timestamps(self):
        bc = BackoffController.from_dict(
            {"state": "normal", "reclaim_times": [
                "2026-05-08T14:00:00+00:00", "not-a-date", "also-bad",
            ]},
            base_interval=30, max_backoff=600,
        )
        assert len(bc._reclaim_times) == 1
        assert bc._reclaim_times[0] == "2026-05-08T14:00:00+00:00"
