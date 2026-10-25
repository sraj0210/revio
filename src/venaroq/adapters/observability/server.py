"""A minimal internal metrics HTTP server.

Serves ``GET /metrics`` with the Prometheus text exposition on a dedicated internal bind only.
It is never mounted on the public webhook listener and exposes nothing but the bounded, label-safe
metric catalog. Any other path or method returns 404/405.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from venaroq.adapters.observability.prometheus import PrometheusQueueMetrics

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _response(status: str, body: bytes, content_type: str = _CONTENT_TYPE) -> bytes:
    headers = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    return headers.encode("ascii") + body


async def handle_metrics_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, metrics: PrometheusQueueMetrics
) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5)
        parts = request_line.decode("latin-1").split()
        if len(parts) < 2:
            writer.write(_response("400 Bad Request", b"bad request\n", "text/plain"))
        else:
            method, path = parts[0], parts[1].split("?", 1)[0]
            if path != "/metrics":
                writer.write(_response("404 Not Found", b"not found\n", "text/plain"))
            elif method != "GET":
                writer.write(
                    _response("405 Method Not Allowed", b"method not allowed\n", "text/plain")
                )
            else:
                writer.write(_response("200 OK", metrics.render().encode("utf-8")))
        await writer.drain()
    except (TimeoutError, ConnectionError, OSError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


@asynccontextmanager
async def serve_metrics(
    metrics: PrometheusQueueMetrics, *, bind: str, port: int
) -> AsyncGenerator[asyncio.base_events.Server]:
    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_metrics_request(reader, writer, metrics)

    server = await asyncio.start_server(client, host=bind, port=port)
    try:
        yield server
    finally:
        server.close()
        await server.wait_closed()
