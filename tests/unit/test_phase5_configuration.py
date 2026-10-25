"""Phase 5 strict schema, narrowing policy, and matching tests."""

import math

import pytest
from pydantic import ValidationError

from venaroq.application.review.execution import DiffLimiter
from venaroq.config.review import ReviewSettings
from venaroq.domain.configuration import (
    CONFIG_WARNING,
    ConfigurationWarningCode,
    parse_repository_configuration,
    path_is_excluded,
    resolve_policy,
)
from venaroq.domain.models import CollectionCompleteness, DiffCollection, DiffFile, DiffLine


def test_minimal_and_complete_configuration_are_normalized() -> None:
    minimal = parse_repository_configuration(b"version: 1\n")
    assert minimal.enabled and minimal.review.focus_areas is None
    complete = parse_repository_configuration(
        b"""version: 1
enabled: true
review:
  focus_areas: [correctness, security]
  exclude: [docs/**, '**/*.generated.py']
  confidence: {summary: 0.7, inline: 0.85}
"""
    )
    policy = resolve_policy(
        service_summary=0.6,
        service_inline=0.8,
        protected_exclusions=("vendor/**",),
        repository=complete,
    )
    assert policy.focus_areas == ("correctness", "security")
    assert policy.exclusions == (
        ".venaroq.yml",
        "vendor/**",
        "docs/**",
        "**/*.generated.py",
    )
    assert (policy.summary_confidence, policy.inline_confidence) == (0.7, 0.85)
    assert len(policy.digest) == 64


@pytest.mark.parametrize(
    "source",
    [
        b"version: 1\nunknown: true\n",
        b"version: true\n",
        b"version: 1\nenabled: yes\n",
        b"version: 1\nversion: 1\n",
        b"version: &v 1\n",
        b"version: 1\nreview: &r {}\n",
        b"version: !!int 1\n",
        b"version: 1\n---\nversion: 1\n",
        b"version: 2\n",
        b"version: 1\nreview: {focus_areas: []}\n",
        b"version: 1\nreview: {exclude: ['../secret']}\n",
        b"version: 1\nreview: {exclude: ['!src/**']}\n",
        b"version: 1\nreview: {confidence: {summary: .nan, inline: 1.0}}\n",
    ],
)
def test_malicious_or_non_strict_documents_are_rejected(source: bytes) -> None:
    with pytest.raises((ValueError, ValidationError)):
        parse_repository_configuration(source)


def test_bounds_and_invalid_encoding_are_rejected() -> None:
    with pytest.raises(ValueError):
        parse_repository_configuration(b"x" * (32 * 1024 + 1))
    with pytest.raises(ValueError):
        parse_repository_configuration(b"version: 1\n# \xff")
    with pytest.raises(ValueError):
        parse_repository_configuration(b"version: 1\n# \x00")


def test_repository_policy_only_narrows_and_optout_is_admin_controlled() -> None:
    lower = parse_repository_configuration(
        b"version: 1\nreview: {confidence: {summary: 0.1, inline: 0.2}}\n"
    )
    policy = resolve_policy(service_summary=0.6, service_inline=0.8, repository=lower)
    assert (policy.summary_confidence, policy.inline_confidence) == (0.6, 0.8)

    disabled = parse_repository_configuration(b"version: 1\nenabled: false\n")
    denied = resolve_policy(service_summary=0.6, service_inline=0.8, repository=disabled)
    assert denied.enabled
    assert denied.warning_codes == (ConfigurationWarningCode.OPTOUT_DENIED,)
    allowed = resolve_policy(
        service_summary=0.6,
        service_inline=0.8,
        repository=disabled,
        allow_repository_optout=True,
    )
    assert not allowed.enabled


def test_posix_exclusion_matching_is_case_sensitive() -> None:
    patterns = ("docs/**", "**/*.generated.py")
    assert path_is_excluded("docs/guide.md", patterns)
    assert path_is_excluded("src/a.generated.py", patterns)
    assert not path_is_excluded("Docs/guide.md", patterns)


def test_warning_is_bounded_and_digest_is_deterministic() -> None:
    first = resolve_policy(
        service_summary=0.6,
        service_inline=0.8,
        warning_codes=(ConfigurationWarningCode.INVALID,),
    )
    second = resolve_policy(
        service_summary=0.6,
        service_inline=0.8,
        warning_codes=(ConfigurationWarningCode.INVALID,),
    )
    assert first.warning_text == CONFIG_WARNING
    assert len(first.warning_text or "") <= 512
    assert first.digest == second.digest
    assert math.isfinite(first.summary_confidence)


def test_exclusions_apply_after_provider_completeness_validation() -> None:
    excluded = DiffFile(
        new_path="docs/a.md",
        status="added",
        lines=(DiffLine(content="text", side="new", new_line=1),),
    )
    files, envelope = DiffLimiter(ReviewSettings()).limit(
        DiffCollection(items=(excluded,), expected_file_count=1), ("docs/**",)
    )
    assert files == () and not envelope.partial

    incomplete = excluded.model_copy(update={"patch_state": "provider_truncated", "lines": ()})
    _, incomplete_envelope = DiffLimiter(ReviewSettings()).limit(
        DiffCollection(
            items=(incomplete,),
            completeness=CollectionCompleteness(status="provider_truncated"),
            expected_file_count=1,
        ),
        ("docs/**",),
    )
    assert incomplete_envelope.partial
