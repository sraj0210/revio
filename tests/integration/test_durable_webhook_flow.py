"""Signed HTTP ingress commits the delivery and queue job before returning 202."""

import hashlib
import hmac
import json
from pathlib import Path

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from revio.adapters.persistence.sqlite import SQLiteStore
from revio.api.app import create_app
from revio.config.database import DatabaseSettings
from revio.config.github import GitHubSettings


def _headers(body: bytes, delivery: str = "delivery-1") -> dict[str, str]:
    digest = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-hub-signature-256": f"sha256={digest}",
        "x-github-delivery": delivery,
        "x-github-event": "pull_request",
    }


@pytest.mark.asyncio
async def test_durable_acceptance_duplicate_conflict_and_readiness(
    tmp_path: Path, rsa_private_key_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    database = DatabaseSettings(database_path=tmp_path / "revio.db")
    store = SQLiteStore(database)
    await store.initialize()
    settings = GitHubSettings(
        environment="sandbox",
        github_enabled=True,
        github_app_id=1,
        github_private_key=SecretStr(rsa_private_key_pem),
        github_webhook_mode="durable",
        github_webhook_secret=SecretStr("webhook-secret"),
    )
    app = create_app(settings, persistence=store)
    payload = {
        "action": "opened",
        "installation": {"id": 9},
        "repository": {"id": 10, "name": "repo", "full_name": "owner/repo"},
        "pull_request": {
            "number": 3,
            "base": {"sha": "base"},
            "head": {"sha": "head"},
        },
    }
    body = json.dumps(payload).encode()
    changed = json.dumps({**payload, "action": "synchronize"}).encode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        accepted = await client.post("/webhooks/github", content=body, headers=_headers(body))
        duplicate = await client.post("/webhooks/github", content=body, headers=_headers(body))
        conflict = await client.post("/webhooks/github", content=changed, headers=_headers(changed))
        ready = await client.get("/ready")
    assert accepted.status_code == duplicate.status_code == 202
    assert accepted.json() == duplicate.json() == {"status": "accepted"}
    assert conflict.status_code == 409
    assert "delivery-1" not in conflict.text
    assert hashlib.sha256(body).hexdigest() not in conflict.text
    assert "delivery-1" not in caplog.text
    assert hashlib.sha256(body).hexdigest() not in caplog.text
    assert ready.status_code == 200 and ready.json() == {"status": "ready"}
    status = await store.status()
    assert status["pending"] == status["active_jobs"] == 1
    assert status["deliveries"] == 1 and status["tombstones"] == 0


@pytest.mark.asyncio
async def test_durable_ingress_failure_is_sanitized_503_and_rolls_back(
    tmp_path: Path, rsa_private_key_pem: str
) -> None:
    enabled = False

    def fail(stage: str) -> None:
        if enabled and stage == "after_delivery_job_link":
            raise aiosqlite.OperationalError("sensitive injected SQL detail")

    database = DatabaseSettings(database_path=tmp_path / "revio.db")
    store = SQLiteStore(database, failure_hook=fail)
    await store.initialize()
    enabled = True
    settings = GitHubSettings(
        environment="sandbox",
        github_enabled=True,
        github_app_id=1,
        github_private_key=SecretStr(rsa_private_key_pem),
        github_webhook_mode="durable",
        github_webhook_secret=SecretStr("webhook-secret"),
    )
    app = create_app(settings, persistence=store)
    payload = {
        "action": "opened",
        "installation": {"id": 9},
        "repository": {"id": 10, "name": "repo", "full_name": "owner/repo"},
        "pull_request": {
            "number": 3,
            "base": {"sha": "base"},
            "head": {"sha": "head"},
        },
    }
    body = json.dumps(payload).encode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/webhooks/github", content=body, headers=_headers(body))
    assert response.status_code == 503
    assert response.json() == {"detail": "durable ingress unavailable"}
    assert "sensitive" not in response.text
    assert (await store.status())["deliveries"] == 0
    assert (await store.status())["active_jobs"] == 0
