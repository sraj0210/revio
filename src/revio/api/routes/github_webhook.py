"""Non-durable local/sandbox GitHub webhook route."""

import json
import re
from email.message import Message
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from revio.adapters.scm.github.errors import GitHubResponseError
from revio.adapters.scm.github.webhook.signature import verify_signature
from revio.application.webhook.service import GitHubSandboxWebhookService
from revio.config.github import GitHubSettings

router = APIRouter()
_HEADER = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _header(value: str | None, name: str) -> str:
    if value is None or len(value) > 128 or not _HEADER.fullmatch(value):
        raise HTTPException(status_code=400, detail=f"invalid {name} header")
    return value


def _json_media_type(value: str | None) -> bool:
    if value is None:
        return False
    message = Message()
    message["content-type"] = value
    if message.defects:
        return False
    if message.get_content_type().lower() != "application/json":
        return False
    charset = message.get_param("charset")
    return charset is None or str(charset).lower() == "utf-8"


async def _bounded_body(request: Request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="request body too large")
    return bytes(body)


@router.post("/webhooks/github")
async def github_webhook(request: Request) -> JSONResponse:
    settings: GitHubSettings = request.app.state.github_settings
    if not settings.github_sandbox_webhook_enabled:
        raise HTTPException(status_code=404, detail="not found")
    if not _json_media_type(request.headers.get("content-type")):
        raise HTTPException(status_code=415, detail="unsupported media type")
    body = await _bounded_body(request, settings.github_webhook_max_bytes)
    signature = request.headers.get("x-hub-signature-256") or ""
    secret = settings.github_webhook_secret
    if secret is None or not verify_signature(body, signature, secret.get_secret_value()):
        raise HTTPException(status_code=401, detail="invalid signature")
    delivery = _header(request.headers.get("x-github-delivery"), "delivery")
    event_name = _header(request.headers.get("x-github-event"), "event").lower()
    try:
        decoded: Any = json.loads(body)
        if not isinstance(decoded, dict):
            raise ValueError
        payload = cast(dict[str, Any], decoded)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="invalid payload") from error
    service: GitHubSandboxWebhookService = request.app.state.github_webhook_service
    try:
        result = await service.process(event_name, delivery, payload)
    except GitHubResponseError as error:
        raise HTTPException(status_code=400, detail="invalid payload") from error
    return JSONResponse(status_code=202, content={"status": result.disposition})
