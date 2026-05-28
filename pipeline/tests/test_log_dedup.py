"""Tests for orchestrator log deduplication (issue #197)."""


def _make_info_collector():
    """Return a list and a mock info() that appends to it."""
    messages = []

    def _info(msg):
        messages.append(msg)

    return messages, _info


class TestCapacityLogDedup:
    """Capacity log should only fire when free_gpus/allocatable/requested changes."""

    def test_repeated_capacity_not_logged(self):
        """Same capacity on consecutive iterations -> only first logs."""
        messages, mock_info = _make_info_collector()
        _last_log_state = {}

        def _log_capacity(free_gpus, allocatable, requested, has_pending):
            key = "capacity"
            state = (free_gpus, allocatable, requested)
            if has_pending and state != _last_log_state.get(key):
                mock_info(f"Capacity: {free_gpus} free GPUs ({allocatable} allocatable - {requested} requested)")
                _last_log_state[key] = state

        _log_capacity(31, 93, 62, True)
        _log_capacity(31, 93, 62, True)
        _log_capacity(31, 93, 62, True)

        assert len(messages) == 1
        assert "31 free GPUs" in messages[0]

    def test_changed_capacity_logs_again(self):
        """Different capacity -> logs again."""
        messages, mock_info = _make_info_collector()
        _last_log_state = {}

        def _log_capacity(free_gpus, allocatable, requested, has_pending):
            key = "capacity"
            state = (free_gpus, allocatable, requested)
            if has_pending and state != _last_log_state.get(key):
                mock_info(f"Capacity: {free_gpus} free GPUs ({allocatable} allocatable - {requested} requested)")
                _last_log_state[key] = state

        _log_capacity(31, 93, 62, True)
        _log_capacity(25, 93, 68, True)

        assert len(messages) == 2


class TestBackoffLogDedup:
    """Backoff-level and backoff-skip logs should deduplicate."""

    def test_repeated_backoff_level_not_logged(self):
        messages, mock_info = _make_info_collector()
        _last_log_state = {}

        def _log_backoff_level(level, interval):
            key = "backoff_level"
            state = (level, interval)
            if state != _last_log_state.get(key):
                mock_info(f"Backoff level {level} - next poll in {interval}s")
                _last_log_state[key] = state

        _log_backoff_level(2, 120)
        _log_backoff_level(2, 120)
        _log_backoff_level(3, 240)

        assert len(messages) == 2

    def test_repeated_backoff_skip_not_logged(self):
        messages, mock_info = _make_info_collector()
        _last_log_state = {}

        def _log_backoff_skip(n_pending, free_gpus):
            key = "backoff_skip"
            state = (n_pending, free_gpus)
            if state != _last_log_state.get(key):
                mock_info(f"Backoff: skipping dispatch ({n_pending} pending, {free_gpus} free GPUs)")
                _last_log_state[key] = state

        _log_backoff_skip(7, 2)
        _log_backoff_skip(7, 2)
        _log_backoff_skip(7, 2)

        assert len(messages) == 1


class TestDispatchLogDedup:
    """Dispatch-intent logs should deduplicate."""

    def test_repeated_capacity_limited_not_logged(self):
        messages, mock_info = _make_info_collector()
        _last_log_state = {}

        def _log_dispatch(dispatchable, pending, free_gpus, free_slots):
            if len(dispatchable) == 0 and pending:
                key = "dispatch"
                state = ("zero", len(pending), free_gpus)
                if state != _last_log_state.get(key):
                    mock_info(f"Dispatching 0/{len(pending)} pending pairs")
                    _last_log_state[key] = state
            elif len(dispatchable) < len(pending):
                key = "dispatch"
                state = ("cap_limited", len(dispatchable), len(pending), free_gpus)
                if state != _last_log_state.get(key):
                    mock_info(f"Dispatching {len(dispatchable)}/{len(pending)} pending pairs (capacity-limited)")
                    _last_log_state[key] = state
            elif free_slots < len(dispatchable):
                key = "dispatch"
                state = ("slot_limited", free_slots, len(pending))
                if state != _last_log_state.get(key):
                    mock_info(f"Dispatching {free_slots}/{len(pending)} pending pairs (slot-limited)")
                    _last_log_state[key] = state

        _log_dispatch(["a", "b"], ["a", "b", "c", "d"], 10, 5)
        _log_dispatch(["a", "b"], ["a", "b", "c", "d"], 10, 5)
        _log_dispatch(["a", "b"], ["a", "b", "c", "d"], 10, 5)
        assert len(messages) == 1

    def test_zero_dispatch_warning_reemits_periodically(self):
        """Stuck-dispatch warning re-emits every 10 iterations."""
        messages, mock_warn = _make_info_collector()
        _last_log_state = {}
        _zero_dispatch_count = 0

        def _log_zero_dispatch(n_pending, free_gpus, smallest):
            nonlocal _zero_dispatch_count
            state = ("zero", n_pending, free_gpus, smallest)
            _zero_dispatch_count += 1
            if state != _last_log_state.get("dispatch") or _zero_dispatch_count % 10 == 0:
                mock_warn(f"Dispatching 0/{n_pending} pending pairs — smallest cost ({smallest}) exceeds free GPUs ({free_gpus})")
                _last_log_state["dispatch"] = state

        # First emission
        _log_zero_dispatch(7, 2, 4)
        assert len(messages) == 1

        # Iterations 2-9: suppressed
        for _ in range(8):
            _log_zero_dispatch(7, 2, 4)
        assert len(messages) == 1

        # Iteration 10: re-emits
        _log_zero_dispatch(7, 2, 4)
        assert len(messages) == 2

    def test_zero_dispatch_includes_smallest_in_state(self):
        """Changed smallest GPU cost triggers new warning even if count/free unchanged."""
        messages, mock_warn = _make_info_collector()
        _last_log_state = {}
        _zero_dispatch_count = 0

        def _log_zero_dispatch(n_pending, free_gpus, smallest):
            nonlocal _zero_dispatch_count
            state = ("zero", n_pending, free_gpus, smallest)
            _zero_dispatch_count += 1
            if state != _last_log_state.get("dispatch") or _zero_dispatch_count % 10 == 0:
                mock_warn(f"smallest cost ({smallest}) exceeds free GPUs ({free_gpus})")
                _last_log_state["dispatch"] = state

        _log_zero_dispatch(7, 2, 4)
        _log_zero_dispatch(7, 2, 8)  # smallest changed
        assert len(messages) == 2

    def test_dispatch_logs_on_change(self):
        messages, mock_info = _make_info_collector()
        _last_log_state = {}

        def _log_dispatch(dispatchable, pending, free_gpus, free_slots):
            if len(dispatchable) == 0 and pending:
                key = "dispatch"
                state = ("zero", len(pending), free_gpus)
                if state != _last_log_state.get(key):
                    mock_info(f"Dispatching 0/{len(pending)} pending pairs")
                    _last_log_state[key] = state
            elif len(dispatchable) < len(pending):
                key = "dispatch"
                state = ("cap_limited", len(dispatchable), len(pending), free_gpus)
                if state != _last_log_state.get(key):
                    mock_info(f"Dispatching {len(dispatchable)}/{len(pending)} pending pairs (capacity-limited)")
                    _last_log_state[key] = state

        _log_dispatch(["a", "b"], ["a", "b", "c", "d"], 10, 5)
        _log_dispatch(["a", "b", "c"], ["a", "b", "c", "d"], 15, 5)
        assert len(messages) == 2


class TestStateClearOnEvent:
    """Dedup state should reset when an actual event occurs."""

    def test_dispatch_clears_capacity_state(self):
        """After a successful dispatch, capacity should log again even if unchanged."""
        messages, mock_info = _make_info_collector()
        _last_log_state = {}

        def _log_capacity(free_gpus, allocatable, requested, has_pending):
            key = "capacity"
            state = (free_gpus, allocatable, requested)
            if has_pending and state != _last_log_state.get(key):
                mock_info(f"Capacity: {free_gpus} free GPUs")
                _last_log_state[key] = state

        def _on_dispatch():
            _last_log_state.pop("capacity", None)
            _last_log_state.pop("dispatch", None)

        _log_capacity(31, 93, 62, True)
        _log_capacity(31, 93, 62, True)
        _on_dispatch()
        _log_capacity(31, 93, 62, True)

        assert len(messages) == 2
