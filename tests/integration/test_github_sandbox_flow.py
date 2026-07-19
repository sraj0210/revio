"""Signed webhook normalization and local cache-eviction integration."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from revio.adapters.scm.github.auth import InstallationToken, InstallationTokenCache
from revio.adapters.scm.github.webhook.ingress import GitHubSandboxWebhookIngress


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["suspend", "deleted"])
async def test_suspend_and_delete_events_evict_process_local_token(action: str) -> None:
    cache = InstallationTokenCache(timedelta(seconds=60), timedelta(seconds=30))
    calls = 0

    async def refresh(_: int) -> InstallationToken:
        nonlocal calls
        calls += 1
        return InstallationToken(
            SecretStr(f"token-{calls}"), datetime.now(UTC) + timedelta(hours=1)
        )

    await cache.get(9, refresh)
    result = await GitHubSandboxWebhookIngress(cache).process(
        "installation", "delivery", {"action": action, "installation": {"id": 9}}
    )
    await cache.get(9, refresh)
    assert result.disposition == "accepted"
    assert calls == 2
