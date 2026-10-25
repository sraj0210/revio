"""Bounded, process-local fixed-window ingress rate limiting.

A single-node control that sheds webhook load per source before body reading and JSON parsing.
Memory is bounded: the number of tracked sources is capped, and the table is cleared when the cap
is exceeded (a new window then starts for all sources). It is fail-closed in the sense that
exceeding the configured per-window limit rejects the request rather than dropping it silently.
"""

import time
from collections.abc import Callable


class FixedWindowRateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        max_sources: int = 10_000,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._max_sources = max_sources
        self._clock = clock or time.monotonic
        self._windows: dict[str, tuple[float, int]] = {}

    @property
    def tracked_sources(self) -> int:
        return len(self._windows)

    def allow(self, source: str) -> bool:
        """Return True if the source is within its per-window limit, else False."""
        if self._limit <= 0:
            return False
        now = self._clock()
        start, count = self._windows.get(source, (now, 0))
        if now - start >= self._window:
            start, count = now, 0
        count += 1
        if len(self._windows) >= self._max_sources and source not in self._windows:
            # Bound memory: start a fresh window table rather than grow unbounded.
            self._windows = {}
            start, count = now, 1
        self._windows[source] = (start, count)
        return count <= self._limit
