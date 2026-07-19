"""Exact-byte GitHub webhook signature verification."""

import hashlib
import hmac
import re

_SIGNATURE = re.compile(r"^sha256=[0-9a-f]{64}$")


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    if not _SIGNATURE.fullmatch(signature):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
