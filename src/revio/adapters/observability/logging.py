"""Structured JSON logging with defensive secret/content redaction.

Logs are single-line JSON to stdout. The formatter emits a bounded set of fields: the standard
record fields plus allowlisted structured context passed through ``extra``. Every rendered string
passes a defensive scrubber that redacts common credential and token shapes so that an accidental
log of a secret, authorization header, or provider body never leaks. Revio code is expected not to
log secrets or content in the first place; the scrubber is defense in depth.
"""

import json
import logging
import re
import sys
from typing import Any

from revio.config.observability import ObservabilitySettings

# Structured context keys that may appear on a LogRecord via ``extra`` and are safe to emit.
_CONTEXT_KEYS = (
    "correlation_id",
    "provider_id",
    "delivery_identity",
    "job_id",
    "review_run_id",
    "call_kind",
    "outcome",
    "error_class",
    "duration_ms",
    "metric",
)

_REDACTIONS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9._\-]{10,}"),
    re.compile(r"(?i)(x-api-key|authorization|api[_-]?key|secret|token)\"?\s*[:=]\s*\"?[^\s\",}]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)

_REDACTED = "[redacted]"


def scrub(text: str) -> str:
    """Redact common credential/token shapes from a rendered string."""
    for pattern in _REDACTIONS:
        text = pattern.sub(_REDACTED, text)
    return text


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }
        for key in _CONTEXT_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = scrub(value) if isinstance(value, str) else value
        if record.exc_info:
            # Emit only the exception class, never the message or traceback (may carry detail).
            exc_type = record.exc_info[0]
            payload["error_class"] = exc_type.__name__ if exc_type is not None else "Exception"
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


class PlainRedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def configure_logging(settings: ObservabilitySettings | None = None) -> None:
    """Install a single stdout handler with the configured level and format."""
    resolved = settings or ObservabilitySettings()
    handler = logging.StreamHandler(sys.stdout)
    if resolved.log_format == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            PlainRedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, resolved.log_level.upper()))
