"""Exponential backoff controller for the deploy orchestrator."""
from __future__ import annotations

import datetime as _dt


class BackoffController:
    """Manages poll-interval backoff during sustained GPU scarcity."""

    def __init__(self, base_interval: int, max_backoff: int, *,
                 reclaim_threshold: int = 3, reclaim_window: int = 600) -> None:
        self._base = base_interval
        self._max = max_backoff
        self._reclaim_threshold = reclaim_threshold
        self._reclaim_window = reclaim_window
        self.state: str = "normal"
        self.backoff_level: int = 0
        self.last_scarcity_time: str | None = None
        self.last_probe_free_gpus: int | None = None
        self._reclaim_times: list[str] = []

    @property
    def effective_interval(self) -> int:
        if self.state == "normal":
            return self._base
        raw = self._base * (2 ** self.backoff_level)
        return min(raw, self._max)

    def signal_scarcity(self, *, free_gpus: int, min_cost: int) -> None:
        if free_gpus >= min_cost:
            return
        self.state = "backing_off"
        max_level = self._level_for_max()
        if self.backoff_level < max_level:
            self.backoff_level += 1
        self.last_scarcity_time = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.last_probe_free_gpus = free_gpus

    def signal_capacity(self, *, free_gpus: int, max_cost: int) -> None:
        if free_gpus >= max_cost:
            self._reset()
        self.last_probe_free_gpus = free_gpus

    def signal_scheduling_success(self) -> None:
        self._reset()

    def signal_reclaim(self) -> None:
        now = _dt.datetime.now(_dt.timezone.utc)
        self._reclaim_times.append(now.isoformat())
        cutoff = now - _dt.timedelta(seconds=self._reclaim_window)
        valid = []
        for t in self._reclaim_times:
            try:
                if _dt.datetime.fromisoformat(t.replace("Z", "+00:00")) >= cutoff:
                    valid.append(t)
            except (ValueError, TypeError):
                pass
        self._reclaim_times = valid
        if len(self._reclaim_times) >= self._reclaim_threshold:
            self.state = "backing_off"
            max_level = self._level_for_max()
            if self.backoff_level < max_level:
                self.backoff_level += 1
            self.last_scarcity_time = now.isoformat()
            self._reclaim_times = []

    def should_dispatch(self, *, free_gpus: int, min_cost: int) -> bool:
        if self.state == "normal":
            return True
        return free_gpus >= min_cost

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "backoff_level": self.backoff_level,
            "last_scarcity_time": self.last_scarcity_time,
            "last_probe_free_gpus": self.last_probe_free_gpus,
            "reclaim_times": self._reclaim_times,
        }

    _VALID_STATES = ("normal", "backing_off")

    @classmethod
    def from_dict(cls, data: dict, *, base_interval: int, max_backoff: int,
                  reclaim_threshold: int = 3, reclaim_window: int = 600) -> BackoffController:
        bc = cls(base_interval=base_interval, max_backoff=max_backoff,
                 reclaim_threshold=reclaim_threshold, reclaim_window=reclaim_window)
        state = data.get("state", "normal")
        state_corrupted = state not in cls._VALID_STATES
        if state_corrupted:
            import sys
            print(f"[WARN]  Unknown backoff state {state!r} in progress — resetting to normal",
                  file=sys.stderr)
            state = "normal"
        bc.state = state
        raw_level = 0 if state_corrupted else data.get("backoff_level", 0)
        if not isinstance(raw_level, int) or raw_level < 0:
            import sys
            print(f"[WARN]  Invalid backoff_level {raw_level!r} in progress — resetting to 0",
                  file=sys.stderr)
            raw_level = 0
        bc.backoff_level = min(raw_level, bc._level_for_max())
        bc.last_scarcity_time = data.get("last_scarcity_time")
        bc.last_probe_free_gpus = data.get("last_probe_free_gpus")
        raw_times = data.get("reclaim_times", [])
        if isinstance(raw_times, list):
            valid = []
            for t in raw_times:
                if not isinstance(t, str):
                    continue
                try:
                    _dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
                    valid.append(t)
                except (ValueError, TypeError):
                    pass
            bc._reclaim_times = valid
        else:
            bc._reclaim_times = []
        return bc

    def _reset(self) -> None:
        self.state = "normal"
        self.backoff_level = 0
        self._reclaim_times = []

    def _level_for_max(self) -> int:
        """Return the smallest level where base * 2^level >= max_backoff."""
        if self._base <= 0:
            return 0
        level = 0
        while self._base * (2 ** level) < self._max:
            level += 1
        return level
