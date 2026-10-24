"""Strict, provider-neutral Phase 5 repository review configuration."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Hashable, Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    model_validator,
)
from yaml.events import AliasEvent
from yaml.nodes import MappingNode
from yaml.tokens import AliasToken, AnchorToken, TagToken

MAX_CONFIG_BYTES = 32 * 1024
MAX_EXCLUSIONS = 100
MAX_PATTERN_CODEPOINTS = 256
FOCUS_AREAS = ("correctness", "security", "reliability", "maintainability")
CONFIG_WARNING = (
    "Repository review configuration could not be applied; secure service defaults were used."
)
OPTOUT_WARNING = (
    "Repository opt-out is not permitted by administrator policy; the review used service defaults."
)


class ConfigurationWarningCode(StrEnum):
    INVALID = "config_invalid"
    OVERSIZED = "config_oversized"
    UNAVAILABLE = "config_unavailable"
    REF_MISMATCH = "config_ref_mismatch"
    OPTOUT_DENIED = "repository_optout_denied"


class _StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.check_event(AliasEvent):  # pyright: ignore[reportUnknownMemberType]
            raise yaml.constructor.ConstructorError(None, None, "aliases are forbidden", None)
        return super().compose_node(parent, index)

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        result: dict[Hashable, Any] = {}
        for key_node, value_node in node.value:
            if key_node.tag != "tag:yaml.org,2002:str":
                raise yaml.constructor.ConstructorError(
                    None, None, "mapping keys must be strings", key_node.start_mark
                )
            key = cast(
                Hashable,
                self.construct_object(  # pyright: ignore[reportUnknownMemberType]
                    key_node, deep=deep
                ),
            )
            if key == "<<" or key in result:
                raise yaml.constructor.ConstructorError(
                    None, None, "duplicate or merge keys are forbidden", key_node.start_mark
                )
            result[key] = self.construct_object(  # pyright: ignore[reportUnknownMemberType]
                value_node, deep=deep
            )
        return result


_StrictLoader.yaml_implicit_resolvers = {
    key: [entry for entry in entries if entry[0] != "tag:yaml.org,2002:bool"]
    for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictLoader.add_implicit_resolver(  # pyright: ignore[reportUnknownMemberType]
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), list("tf")
)


class ReviewConfidenceV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    summary: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=1)
    inline: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def finite_and_ordered(self) -> ReviewConfidenceV1:
        values = (self.summary, self.inline)
        if any(value is not None and not math.isfinite(float(value)) for value in values):
            raise ValueError("confidence must be finite")
        if self.summary is not None and self.inline is not None and self.summary > self.inline:
            raise ValueError("summary confidence cannot exceed inline confidence")
        return self


class RepositoryReviewV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    focus_areas: tuple[str, ...] | None = None
    exclude: tuple[str, ...] = ()
    confidence: ReviewConfidenceV1 | None = None

    @model_validator(mode="after")
    def validate_values(self) -> RepositoryReviewV1:
        if self.focus_areas is not None:
            if not self.focus_areas or len(self.focus_areas) > len(FOCUS_AREAS):
                raise ValueError("focus areas must be non-empty and bounded")
            if len(set(self.focus_areas)) != len(self.focus_areas):
                raise ValueError("focus areas must be unique")
            if any(value not in FOCUS_AREAS for value in self.focus_areas):
                raise ValueError("unsupported focus area")
        if len(self.exclude) > MAX_EXCLUSIONS or len(set(self.exclude)) != len(self.exclude):
            raise ValueError("exclusions must be bounded and unique")
        for pattern in self.exclude:
            validate_exclusion_pattern(pattern)
        return self


class RepositoryReviewConfigurationV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: StrictInt
    enabled: StrictBool = True
    review: RepositoryReviewV1 = RepositoryReviewV1()

    @model_validator(mode="after")
    def require_version_one(self) -> RepositoryReviewConfigurationV1:
        if self.version != 1:
            raise ValueError("unsupported repository configuration version")
        return self


class EffectiveReviewPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    enabled: bool = True
    focus_areas: tuple[str, ...] = FOCUS_AREAS
    exclusions: tuple[str, ...] = (".revio.yml",)
    summary_confidence: float = Field(ge=0, le=1)
    inline_confidence: float = Field(ge=0, le=1)
    warning_codes: tuple[ConfigurationWarningCode, ...] = ()

    @model_validator(mode="after")
    def ordered_thresholds(self) -> EffectiveReviewPolicy:
        if self.summary_confidence > self.inline_confidence:
            raise ValueError("summary confidence cannot exceed inline confidence")
        if len(self.warning_codes) > 8:
            raise ValueError("too many warning codes")
        return self

    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def warning_text(self) -> str | None:
        if ConfigurationWarningCode.OPTOUT_DENIED in self.warning_codes:
            return OPTOUT_WARNING
        if self.warning_codes:
            return CONFIG_WARNING
        return None


class ConfigurationResolution(BaseModel):
    """Durable normalized policy; raw repository YAML is deliberately absent."""

    model_config = ConfigDict(frozen=True)
    job_id: str
    review_run_id: str | None = None
    provider_id: str
    installation_id: str
    repository_id: str
    base_sha: str
    status: str
    schema_version: int | None = None
    source_digest: str | None = None
    policy: EffectiveReviewPolicy
    created_at: datetime
    updated_at: datetime


def validate_exclusion_pattern(pattern: str) -> None:
    if not pattern or len(pattern) > MAX_PATTERN_CODEPOINTS or pattern.startswith(("/", "!")):
        raise ValueError("invalid exclusion pattern")
    if "\\" in pattern or "\x00" in pattern:
        raise ValueError("invalid exclusion pattern")
    if any(unicodedata.category(character) == "Cc" for character in pattern):
        raise ValueError("invalid exclusion pattern")
    segments = pattern.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("invalid exclusion pattern")


def path_is_excluded(path: str, patterns: tuple[str, ...]) -> bool:
    validate_exclusion_pattern(path)
    value = PurePosixPath(path)
    return any(value.match(pattern) for pattern in patterns)


def parse_repository_configuration(source: bytes) -> RepositoryReviewConfigurationV1:
    if len(source) > MAX_CONFIG_BYTES:
        raise ValueError("repository configuration is oversized")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("repository configuration is not UTF-8") from None
    if "\x00" in text or any(
        unicodedata.category(character) == "Cc" and character not in "\t\n\r" for character in text
    ):
        raise ValueError("repository configuration contains control characters")
    try:
        tokens = cast(
            Iterable[object],
            yaml.scan(text),  # pyright: ignore[reportUnknownMemberType]
        )
        if any(isinstance(token, (AliasToken, AnchorToken, TagToken)) for token in tokens):
            raise ValueError("aliases, anchors, and explicit tags are forbidden")
        documents = list(yaml.load_all(text, Loader=_StrictLoader))
    except ValueError:
        raise
    except yaml.YAMLError:
        raise ValueError("invalid repository configuration") from None
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise ValueError("repository configuration must contain one mapping document")
    return RepositoryReviewConfigurationV1.model_validate(cast(dict[str, object], documents[0]))


def resolve_policy(
    *,
    service_summary: float,
    service_inline: float,
    protected_exclusions: tuple[str, ...] = (),
    repository: RepositoryReviewConfigurationV1 | None = None,
    allow_repository_optout: bool = False,
    warning_codes: tuple[ConfigurationWarningCode, ...] = (),
) -> EffectiveReviewPolicy:
    exclusions = tuple(dict.fromkeys((".revio.yml", *protected_exclusions)))
    focus = FOCUS_AREAS
    enabled = True
    summary, inline = service_summary, service_inline
    warnings = list(warning_codes)
    if repository is not None:
        if not repository.enabled:
            if allow_repository_optout:
                enabled = False
            else:
                warnings.append(ConfigurationWarningCode.OPTOUT_DENIED)
        else:
            focus = repository.review.focus_areas or FOCUS_AREAS
            exclusions = tuple(dict.fromkeys((*exclusions, *repository.review.exclude)))
            if repository.review.confidence is not None:
                confidence = repository.review.confidence
                summary = max(summary, float(confidence.summary or 0))
                inline = max(inline, float(confidence.inline or 0))
                if summary > inline:
                    raise ValueError("effective confidence thresholds are invalid")
    return EffectiveReviewPolicy(
        enabled=enabled,
        focus_areas=focus,
        exclusions=exclusions,
        summary_confidence=summary,
        inline_confidence=inline,
        warning_codes=tuple(dict.fromkeys(warnings)),
    )
