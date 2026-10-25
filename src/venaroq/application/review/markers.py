"""Non-sensitive deterministic Phase 4 publication reconciliation identities."""

import base64
import hashlib
import hmac

from venaroq.config.review import ReviewSettings


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def marker_key_id(marker_key: bytes) -> str:
    if len(marker_key) < 32:
        raise ValueError("marker key must contain at least 32 bytes")
    return _base64url(hashlib.sha256(marker_key).digest()[:12])


def review_marker(marker_key: bytes, operation_key: str) -> str:
    if len(marker_key) < 32:
        raise ValueError("marker key must contain at least 32 bytes")
    digest = hmac.new(marker_key, operation_key.encode("utf-8"), hashlib.sha256).digest()[:18]
    return f"<!-- venaroq:v1:review:{_base64url(digest)} -->"


FINDING_MARKER_VERSION = "finding-marker-v1"


def finding_marker_token(marker_key: bytes, finding_record_id: str) -> str:
    """Return the non-secret Base64URL token for a per-finding hidden marker.

    The token is derived from the finding record ID through the active marker key. It reveals
    no raw fingerprint, tenant identity, source evidence, or secret.
    """
    if len(marker_key) < 32:
        raise ValueError("marker key must contain at least 32 bytes")
    material = f"finding:{finding_record_id}".encode()
    digest = hmac.new(marker_key, material, hashlib.sha256).digest()[:18]
    return _base64url(digest)


def finding_marker(token: str) -> str:
    """Render the hidden per-finding marker comment from its stored token."""
    return f"<!-- venaroq:v1:finding:{token} -->"


def marker_matches(expected: str, candidate: str) -> bool:
    return hmac.compare_digest(expected.encode("utf-8"), candidate.encode("utf-8"))


def load_marker_key(settings: ReviewSettings) -> bytes:
    if settings.publish_marker_key is not None:
        value = settings.publish_marker_key.get_secret_value().encode()
    elif settings.publish_marker_key_file is not None:
        try:
            if settings.publish_marker_key_file.stat().st_size > 16_384:
                raise ValueError("marker-key file is too large")
            value = settings.publish_marker_key_file.read_bytes().strip()
        except OSError:
            raise ValueError("marker-key file is unavailable") from None
    else:
        raise ValueError("marker key is unavailable")
    if len(value) < 32:
        raise ValueError("marker key must contain at least 32 bytes")
    if len(value) > 16_384:
        raise ValueError("marker key is too large")
    return value
