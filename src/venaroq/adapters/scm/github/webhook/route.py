"""Provider-routable signed webhook ingress route.

A single generic endpoint dispatches to any registered SCM webhook provider. The provider owns
authentication, header extraction, normalization, sandbox handling, and the installation hook; this
route owns the shared, provider-neutral flow: route/media-type/byte limits, rate limiting, durable
persistence, and the fail-closed exception mapping.
"""

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from email.message import Message
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from venaroq.api.ratelimit import FixedWindowRateLimiter
from venaroq.domain.queue import IngressDisposition
from venaroq.errors import (
    InvalidJobError,
    InvalidWebhookPayloadError,
    PersistenceIndeterminateError,
    PersistenceIntegrityError,
    PersistenceNotCommittedError,
    PersistenceUnavailableError,
    QueueCapacityError,
)
from venaroq.ports.webhook import WebhookIngressProvider

router = APIRouter()
logger = logging.getLogger(__name__)


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


def _provider(request: Request, name: str) -> WebhookIngressProvider:
    providers: dict[str, WebhookIngressProvider] = getattr(
        request.app.state, "webhook_providers", {}
    )
    provider = providers.get(name)
    if provider is None or provider.mode == "disabled":
        raise HTTPException(status_code=404, detail="not found")
    return provider


@router.post("/webhooks/{provider}")
async def scm_webhook(provider: str, request: Request) -> JSONResponse:
    correlation_id = uuid.uuid4().hex
    handler = _provider(request, provider)
    if not _json_media_type(request.headers.get("content-type")):
        raise HTTPException(status_code=415, detail="unsupported media type")
    limiter: FixedWindowRateLimiter | None = getattr(
        request.app.state, "ingress_rate_limiter", None
    )
    if limiter is not None:
        source = request.client.host if request.client is not None else "unknown"
        if not limiter.allow(source):
            logger.warning(
                "webhook_rate_limited",
                extra={"correlation_id": correlation_id, "provider_id": handler.provider_id},
            )
            return JSONResponse(
                status_code=429,
                content={"status": "rate_limited"},
                headers={"Retry-After": "1"},
            )
    body = await _bounded_body(request, handler.max_bytes)
    if not handler.authenticate(body, request.headers):
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        delivery, event_name = handler.extract(request.headers)
    except InvalidWebhookPayloadError:
        raise HTTPException(status_code=400, detail="invalid webhook headers") from None
    try:
        decoded: Any = json.loads(body)
        if not isinstance(decoded, dict):
            raise ValueError
        payload = cast(dict[str, object], decoded)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid payload") from None
    try:
        if handler.mode == "sandbox":
            status = await handler.sandbox_process(event_name, delivery, payload)
        else:
            durable = handler.durable
            if durable is None:
                raise HTTPException(status_code=404, detail="not found")
            normalization = handler.normalize(event_name, delivery, payload)
            receipt = await durable.persist(
                provider_id=handler.provider_id,
                delivery_identity=f"{handler.provider_id}:{delivery}",
                event_name=event_name,
                payload_sha256=hashlib.sha256(body).hexdigest(),
                normalization=normalization,
                received_at=datetime.now(UTC),
            )
            if receipt.disposition == IngressDisposition.CONFLICT:
                logger.warning(
                    "webhook_delivery_integrity_conflict",
                    extra={"correlation_id": correlation_id, "provider_id": handler.provider_id},
                )
                raise HTTPException(status_code=409, detail="delivery integrity conflict")
            await handler.on_installation_change(normalization)
            status = "ignored" if receipt.disposition == IngressDisposition.IGNORED else "accepted"
    except (InvalidWebhookPayloadError, InvalidJobError):
        raise HTTPException(status_code=400, detail="invalid payload") from None
    except QueueCapacityError:
        logger.warning(
            "webhook_active_capacity_rejected",
            extra={"correlation_id": correlation_id, "provider_id": handler.provider_id},
        )
        return JSONResponse(
            status_code=503, content={"status": "not_accepted"}, headers={"Retry-After": "1"}
        )
    except (PersistenceNotCommittedError, PersistenceUnavailableError):
        logger.warning(
            "webhook_persistence_unavailable",
            extra={"correlation_id": correlation_id, "provider_id": handler.provider_id},
        )
        return JSONResponse(
            status_code=503, content={"status": "not_accepted"}, headers={"Retry-After": "1"}
        )
    except PersistenceIndeterminateError:
        logger.error(
            "webhook_persistence_indeterminate",
            extra={"correlation_id": correlation_id, "provider_id": handler.provider_id},
        )
        return JSONResponse(status_code=500, content={"status": "indeterminate"})
    except PersistenceIntegrityError:
        logger.error(
            "webhook_persistence_integrity_error",
            extra={"correlation_id": correlation_id, "provider_id": handler.provider_id},
        )
        return JSONResponse(status_code=500, content={"status": "integrity_error"})
    logger.info(
        "webhook_%s",
        status,
        extra={"correlation_id": correlation_id, "provider_id": handler.provider_id},
    )
    return JSONResponse(status_code=202, content={"status": status})
