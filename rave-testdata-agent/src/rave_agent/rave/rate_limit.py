"""Client-side rate limiting (C-5, FR-7.5)."""
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Sliding-window limiter: at most *per_minute* acquisitions in any 60s window.

    Thread-safe, so it holds across the threads of one process. It cannot hold
    across *processes* - concurrent subjects each run their own - so the caller
    divides the budget between them instead, overriding
    `rave.requests_per_minute` per child.
    """

    def __init__(self, per_minute: int, window_seconds: float = 60.0):
        if per_minute < 1:
            raise ValueError("per_minute must be >= 1")
        self.per_minute = per_minute
        self.window = window_seconds
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a slot is free. Returns how long it waited, in seconds."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                while self._events and now - self._events[0] >= self.window:
                    self._events.popleft()
                if len(self._events) < self.per_minute:
                    self._events.append(now)
                    return waited
                sleep_for = self.window - (now - self._events[0])
            sleep_for = max(sleep_for, 0.01)
            time.sleep(sleep_for)
            waited += sleep_for
