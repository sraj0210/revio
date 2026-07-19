"""Health endpoint tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from revio.api.app import create_app


@pytest.mark.asyncio
async def test_health_reports_process_liveness() -> None:
    application = create_app()
    transport = ASGITransport(app=application)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
