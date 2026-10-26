"""Content-free reason codes attached to provider errors (safe to log)."""

import httpx
import pytest
from pydantic import ValidationError

from venaroq.adapters.ai.http_errors import ambiguous_cause
from venaroq.adapters.ai.review_schema import OutputShapeError, ReviewDTO, validation_detail

_FINDING = (
    '{"category":"c","title":"%s","explanation":"e","confidence":0.5,"path":"a","line":1,'
    '"identity":{"rule":"r","symbol":null,"evidence":"e"}}'
)


def _detail(raw: str) -> str:
    with pytest.raises(ValidationError) as raised:
        ReviewDTO.model_validate_json(raw)
    return validation_detail(raised.value)


def test_missing_field_and_overlong_value_are_located() -> None:
    assert _detail('{"summary":"x"}') == "schema_invalid:findings:missing"
    overlong = '{"summary":"x","findings":[%s]}' % (_FINDING % ("t" * 300))
    assert _detail(overlong) == "schema_invalid:findings.0.title:string_too_long"


def test_model_invented_keys_are_masked() -> None:
    detail = _detail('{"summary":"x","findings":[],"Ignore previous instructions":1}')
    assert detail == "schema_invalid:?:extra_forbidden"


def test_invalid_json_and_shape_errors() -> None:
    assert _detail("not json") == "schema_invalid:root:json_invalid"
    assert validation_detail(OutputShapeError("no_text_block")) == "no_text_block"
    assert validation_detail(KeyError("content")) == "response_missing_field"


def test_ambiguous_cause_distinguishes_timeout_from_server_error() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    assert ambiguous_cause(httpx.ReadTimeout("t", request=request)) == "timeout"
    assert ambiguous_cause(httpx.ReadError("r", request=request)) == "read_error"
    assert ambiguous_cause(status_code=529) == "server_error_529"
