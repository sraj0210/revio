"""Signed sandbox or durable GitHub webhook ingress route."""

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from email.message import Message
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from revio.adapters.scm.github.errors import GitHubResponseError
from revio.adapters.scm.github.webhook.ingress import GitHubSandboxWebhookIngress
from revio.adapters.scm.github.webhook.normalizer import normalize_webhook
from revio.adapters.scm.github.webhook.signature import verify_signature
from revio.config.github import GitHubSettings
from revio.domain.queue import IngressDisposition
from revio.errors import (
    InvalidJobError,
    PersistenceIntegrityError,
    PersistenceUnavailableError,
    QueueCapacityError,
)
from revio.ports.persistence import DurableIngressPort

router = APIRouter()
logger = logging.getLogger(__name__)
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
    correlation_id = uuid.uuid4().hex
    settings: GitHubSettings = request.app.state.github_settings
    if settings.github_webhook_mode == "disabled":
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
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid payload") from None
    try:
        if settings.github_webhook_mode == "sandbox":
            ingress: GitHubSandboxWebhookIngress = request.app.state.github_webhook_ingress
            result = await ingress.process(event_name, delivery, payload)
            status = result.disposition
        else:
            normalization = normalize_webhook(event_name, delivery, payload)
            durable: DurableIngressPort = request.app.state.durable_ingress
            receipt = await durable.persist(
                provider_id="github",
                delivery_identity=f"github:{delivery}",
                event_name=event_name,
                payload_sha256=hashlib.sha256(body).hexdigest(),
                normalization=normalization,
                received_at=datetime.now(UTC),
            )
            if receipt.disposition == IngressDisposition.CONFLICT:
                logger.warning(
                    "github_webhook_delivery_integrity_conflict",
                    extra={"correlation_id": correlation_id},
                )
                raise HTTPException(status_code=409, detail="delivery integrity conflict")
            event = normalization.event
            if (
                event is not None
                and event.event_type == "installation"
                and event.trigger in {"suspend", "deleted"}
            ):
                composition = request.app.state.github_composition
                await composition.token_cache.invalidate(int(event.installation.external_id))
            status = "ignored" if receipt.disposition == IngressDisposition.IGNORED else "accepted"
    except (GitHubResponseError, InvalidJobError):
        raise HTTPException(status_code=400, detail="invalid payload") from None
    except QueueCapacityError:
        logger.warning(
            "github_webhook_active_capacity_rejected",
            extra={"correlation_id": correlation_id},
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "durable ingress unavailable"},
            headers={"Retry-After": "1"},
        )
    except (PersistenceUnavailableError, PersistenceIntegrityError):
        logger.warning(
            "github_webhook_persistence_unavailable",
            extra={"correlation_id": correlation_id},
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "durable ingress unavailable"},
            headers={"Retry-After": "1"},
        )
    logger.info("github_webhook_%s", status, extra={"correlation_id": correlation_id})
    return JSONResponse(status_code=202, content={"status": status})
