"""Structured JSON logging and defensive redaction."""

import json
import logging

from venaroq.adapters.observability.logging import JsonLogFormatter, configure_logging, scrub
from venaroq.config.observability import ObservabilitySettings


def test_scrub_redacts_common_secret_shapes() -> None:
    cases = [
        "Authorization: Bearer ghs_abcdefghijklmnopqrstuvwxyz0123456789",
        "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "x-api-key: sk-ant-api03-abcdefghijklmnop",
        'api_key="sk-ant-secret-value-here-123456"',
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    ]
    for value in cases:
        assert "[redacted]" in scrub(value)
    for leaked in ("ghs_abcdefghijklmnopqrstuvwxyz0123456789", "sk-ant-api03-abcdefghijklmnop"):
        assert leaked not in "".join(scrub(c) for c in cases)


def test_json_formatter_emits_allowlisted_context() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="venaroq.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="review completed",
        args=(),
        exc_info=None,
    )
    record.review_run_id = "run-1"
    record.outcome = "completed"
    record.repository_secret = "should-not-appear"  # not in the allowlist
    rendered = formatter.format(record)
    payload = json.loads(rendered)
    assert payload["message"] == "review completed"
    assert payload["review_run_id"] == "run-1"
    assert payload["outcome"] == "completed"
    assert payload["level"] == "info"
    assert "repository_secret" not in payload


def test_json_formatter_scrubs_message_and_exception_class() -> None:
    formatter = JsonLogFormatter()
    try:
        raise ValueError("boom with Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="venaroq.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failure: Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            args=(),
            exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert "ghp_" not in payload["message"]
    assert payload["error_class"] == "ValueError"
    # The exception message/traceback is never emitted.
    assert "traceback" not in payload and "exc_info" not in payload


def test_configure_logging_installs_single_stdout_handler() -> None:
    configure_logging(ObservabilitySettings(log_level="warning", log_format="json"))
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonLogFormatter)
    assert root.level == logging.WARNING
    # Restore a sane default for subsequent tests.
    configure_logging(ObservabilitySettings())
