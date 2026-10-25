"""Prometheus exporter rendering, cardinality guard, and internal metrics server."""

import asyncio

import pytest

from revio.adapters.observability.prometheus import PrometheusQueueMetrics
from revio.adapters.observability.server import serve_metrics


def test_render_counters_and_gauges() -> None:
    m = PrometheusQueueMetrics()
    m.increment("lease_lost")
    m.increment("installation_state_block", outcome="suspended")
    m.increment("installation_state_block", outcome="suspended")
    m.gauge("active_jobs", 3)
    text = m.render()
    assert "# TYPE revio_lease_lost_total counter" in text
    assert "revio_lease_lost_total 1" in text
    assert 'revio_installation_state_block_total{outcome="suspended"} 2' in text
    assert "# TYPE revio_active_jobs gauge" in text
    assert "revio_active_jobs 3" in text
    assert "revio_metrics_dropped_total 0" in text


def test_label_values_are_escaped() -> None:
    m = PrometheusQueueMetrics()
    m.increment("x", outcome='weird"value\nhere')
    text = m.render()
    assert '\\"' in text and "\n    " not in text  # quote escaped, newline flattened


def test_cardinality_guard_caps_series_and_counts_drops() -> None:
    m = PrometheusQueueMetrics(max_series=3)
    for i in range(10):
        m.increment("metric", outcome=f"o{i}")
    # Only 3 distinct series are retained; the rest are dropped and counted.
    text = m.render()
    series = [ln for ln in text.splitlines() if ln.startswith("revio_metric_total")]
    assert len(series) == 3
    assert "revio_metrics_dropped_total 7" in text


def test_identifier_like_values_never_become_metric_names() -> None:
    m = PrometheusQueueMetrics()
    m.gauge("weird name/with:chars", 1)
    text = m.render()
    assert "revio_weird_name_with_chars 1" in text


@pytest.mark.asyncio
async def test_metrics_server_serves_only_metrics_path() -> None:
    m = PrometheusQueueMetrics()
    m.increment("served")
    async with serve_metrics(m, bind="127.0.0.1", port=0) as server:
        port = server.sockets[0].getsockname()[1]

        async def request(path: str, method: str = "GET") -> str:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            await writer.drain()
            data = await reader.read()
            writer.close()
            await writer.wait_closed()
            return data.decode("utf-8", "replace")

        ok = await request("/metrics")
        assert "200 OK" in ok and "revio_served_total 1" in ok
        missing = await request("/other")
        assert "404 Not Found" in missing
        bad_method = await request("/metrics", method="POST")
        assert "405 Method Not Allowed" in bad_method
