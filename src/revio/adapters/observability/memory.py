"""Process-local bounded Phase 3 metrics hook; no exporter is provided."""

from collections import Counter


class InMemoryQueueMetrics:
    """Retain only administrator-defined metric and outcome names."""

    def __init__(self) -> None:
        self.counters: Counter[tuple[str, str | None]] = Counter()
        self.gauges: dict[str, int | float] = {}

    def increment(self, metric: str, *, outcome: str | None = None) -> None:
        self.counters[(metric, outcome)] += 1

    def gauge(self, metric: str, value: int | float) -> None:
        self.gauges[metric] = value
