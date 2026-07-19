"""GitHub webhook normalization identity tests."""

import pytest

from revio.adapters.scm.github.webhook.normalizer import normalize_webhook


@pytest.mark.parametrize("action", ["opened", "reopened", "synchronize"])
def test_supported_pull_request_actions_normalize(action: str) -> None:
    result = normalize_webhook(
        "pull_request",
        "delivery",
        {
            "action": action,
            "installation": {"id": 9},
            "repository": {"id": 10, "name": "repo", "full_name": "owner/repo"},
            "pull_request": {"number": 3, "base": {"sha": "base"}, "head": {"sha": "head"}},
        },
    )
    assert result.event is not None
    assert result.event.trigger == action
    assert result.event.delivery_identity == "github:delivery"
    assert f":head:{action}" in result.event.semantic_identity


@pytest.mark.parametrize("action", ["created", "deleted", "suspend", "unsuspend"])
def test_supported_installation_actions_normalize(action: str) -> None:
    result = normalize_webhook(
        "installation", "delivery", {"action": action, "installation": {"id": 9}}
    )
    assert result.disposition == "accepted"
    assert result.event is not None and result.event.event_type == "installation"


def test_unknown_event_is_ignored_without_payload_data() -> None:
    result = normalize_webhook("issues", "delivery", {"secret": "not-retained"})
    assert result.disposition == "ignored"
    assert result.event is None
