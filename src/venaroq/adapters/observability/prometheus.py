"""Low-cardinality Prometheus exporter implementing the queue metrics port.

Metric names and outcome labels are administrator-defined code constants, so cardinality is
bounded by construction. A hard series cap is enforced defensively: once the cap is reached, new
series are dropped and counted in ``venaroq_metrics_dropped_total`` rather than growing unbounded.
Identifiers (repository, pull request, SHA, installation, user, marker, finding, thread) are never
accepted as labels.
"""

import re

_NAME = re.compile(r"[^a-zA-Z0-9_]")
_MAX_SERIES = 512


def _sanitize_name(value: str) -> str:
    cleaned = _NAME.sub("_", value)
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"_{cleaned}"
    return cleaned


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class PrometheusQueueMetrics:
    """In-process counters/gauges rendered as Prometheus text with a bounded series cap."""

    def __init__(self, *, max_series: int = _MAX_SERIES) -> None:
        self._counters: dict[tuple[str, str | None], int] = {}
        self._gauges: dict[str, float] = {}
        self._max_series = max_series
        self._dropped = 0

    def increment(self, metric: str, *, outcome: str | None = None) -> None:
        key = (metric, outcome)
        if key not in self._counters and len(self._counters) >= self._max_series:
            self._dropped += 1
            return
        self._counters[key] = self._counters.get(key, 0) + 1

    def gauge(self, metric: str, value: int | float) -> None:
        if metric not in self._gauges and len(self._gauges) >= self._max_series:
            self._dropped += 1
            return
        self._gauges[metric] = float(value)

    def render(self) -> str:
        lines: list[str] = []
        counter_names = sorted({metric for metric, _ in self._counters})
        for name in counter_names:
            metric_name = f"venaroq_{_sanitize_name(name)}_total"
            lines.append(f"# TYPE {metric_name} counter")
            for (metric, outcome), count in sorted(self._counters.items()):
                if metric != name:
                    continue
                if outcome is None:
                    lines.append(f"{metric_name} {count}")
                else:
                    lines.append(f'{metric_name}{{outcome="{_escape_label(outcome)}"}} {count}')
        for name in sorted(self._gauges):
            metric_name = f"venaroq_{_sanitize_name(name)}"
            lines.append(f"# TYPE {metric_name} gauge")
            lines.append(f"{metric_name} {self._gauges[name]:g}")
        lines.append("# TYPE venaroq_metrics_dropped_total counter")
        lines.append(f"venaroq_metrics_dropped_total {self._dropped}")
        return "\n".join(lines) + "\n"
