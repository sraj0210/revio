"""Sandbox webhook security and normalization tests."""

import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from revio.api.app import create_app
from revio.config.github import GitHubSettings


def settings(pem: str, *, enabled: bool = True, limit: int = 1_048_576) -> GitHubSettings:
    return GitHubSettings(
        environment="sandbox",
        github_enabled=True,
        github_app_id=1,
        github_private_key=SecretStr(pem),
        github_sandbox_webhook_enabled=enabled,
        github_webhook_secret=SecretStr("webhook-secret") if enabled else None,
        github_webhook_max_bytes=limit,
    )


def headers(
    body: bytes, *, content_type: str = "application/json", event: str = "pull_request"
) -> dict[str, str]:
    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    return {
        "content-type": content_type,
        "x-hub-signature-256": f"sha256={signature}",
        "x-github-delivery": "delivery-1",
        "x-github-event": event,
    }


def pull_payload(action: str = "opened") -> dict[str, object]:
    return {
        "action": action,
        "installation": {"id": 9},
        "repository": {"id": 10, "name": "repo", "full_name": "owner/repo"},
        "pull_request": {"number": 3, "base": {"sha": "base"}, "head": {"sha": "head"}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/json; charset=utf-8", "Application/JSON; Charset=UTF-8"],
)
async def test_accepts_utf8_json_media_types(rsa_private_key_pem: str, content_type: str) -> None:
    body = json.dumps(pull_payload()).encode()
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings(rsa_private_key_pem))),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/webhooks/github", content=body, headers=headers(body, content_type=content_type)
        )
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}


@pytest.mark.asyncio
async def test_rejects_non_utf8_and_bad_signature(rsa_private_key_pem: str) -> None:
    body = json.dumps(pull_payload()).encode()
    app = create_app(settings(rsa_private_key_pem))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.post(
                "/webhooks/github",
                content=body,
                headers=headers(body, content_type="application/json; charset=latin-1"),
            )
        ).status_code == 415
        bad = headers(body)
        bad["x-hub-signature-256"] = "sha256=" + "0" * 64
        assert (await client.post("/webhooks/github", content=body, headers=bad)).status_code == 401


@pytest.mark.asyncio
async def test_disabled_route_oversize_and_headers(rsa_private_key_pem: str) -> None:
    disabled = create_app(settings(rsa_private_key_pem, enabled=False))
    async with AsyncClient(transport=ASGITransport(app=disabled), base_url="http://test") as client:
        assert (await client.post("/webhooks/github")).status_code == 404
    body = json.dumps(pull_payload()).encode()
    app = create_app(settings(rsa_private_key_pem, limit=len(body) - 1))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.post("/webhooks/github", content=body, headers=headers(body))
        ).status_code == 413
    app = create_app(settings(rsa_private_key_pem))
    bad_headers = headers(body)
    bad_headers["x-github-delivery"] = "x" * 129
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.post("/webhooks/github", content=body, headers=bad_headers)
        ).status_code == 400
        missing_delivery = headers(body)
        missing_delivery.pop("x-github-delivery")
        assert (
            await client.post("/webhooks/github", content=body, headers=missing_delivery)
        ).status_code == 400


@pytest.mark.asyncio
async def test_malformed_json_and_sha1_only_are_rejected(rsa_private_key_pem: str) -> None:
    body = b"{broken"
    request_headers = headers(body)
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings(rsa_private_key_pem))),
        base_url="http://test",
    ) as client:
        assert (
            await client.post("/webhooks/github", content=body, headers=request_headers)
        ).status_code == 400
        request_headers.pop("x-hub-signature-256")
        request_headers["x-hub-signature"] = "sha1=" + "0" * 40
        assert (
            await client.post("/webhooks/github", content=body, headers=request_headers)
        ).status_code == 401


@pytest.mark.asyncio
async def test_unsupported_action_returns_generic_ignored(rsa_private_key_pem: str) -> None:
    body = json.dumps(pull_payload("closed")).encode()
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings(rsa_private_key_pem))),
        base_url="http://test",
    ) as client:
        response = await client.post("/webhooks/github", content=body, headers=headers(body))
    assert response.status_code == 202
    assert response.json() == {"status": "ignored"}
