"""Bounded low-cardinality Phase 3 metrics hook."""

from typing import Protocol


class QueueMetricsPort(Protocol):
    def increment(self, metric: str, *, outcome: str | None = None) -> None: ...

    def gauge(self, metric: str, value: int | float) -> None: ...
