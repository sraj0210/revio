"""Deterministic, versioned finding identity and fingerprints.

Primary fingerprints hash only stable, normalized identity fields: repository identity,
path, category, rule, optional symbol, and a bounded evidence snippet. Generated title,
explanation, remediation, confidence wording, and line number are deliberately excluded.
Secondary fingerprints add a conservative normalized title and are used only to rank
candidates for revalidation; they never authorize suppression, linkage, or resolution.

Canonical serialization uses explicit byte-length framing (never delimiter concatenation)
so field-boundary variations cannot collide.
"""

import hashlib
import re
import unicodedata
import uuid

from revio.domain.models import DiffFile, FindingIdentity

FINGERPRINT_VERSION = "finding-v1"
PRIMARY_VERSION = "finding-primary-v1"
SECONDARY_VERSION = "finding-secondary-v1"

# Absent optional symbol is encoded as a zero-length framed field. A present symbol is
# always non-empty after normalization, so the two are unambiguous under length framing.
_SYMBOL_ABSENT = ""

_HORIZONTAL_WHITESPACE = re.compile(r"[ \t]+")


def _is_disallowed_control(character: str, *, allow: str) -> bool:
    return character == "\x00" or (
        unicodedata.category(character) == "Cc" and character not in allow
    )


def normalize_multiline(value: str) -> str:
    """Normalize a multi-line text value (evidence) deterministically.

    Applies NFC, LF newlines, rejects NUL/control characters (other than tab and newline),
    collapses horizontal whitespace runs within each line, strips each line, and trims
    leading and trailing blank lines while preserving interior line order and case.
    """
    normalized = unicodedata.normalize("NFC", value)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    if any(_is_disallowed_control(character, allow="\t\n") for character in normalized):
        raise ValueError("value contains disallowed control characters")
    lines = [_HORIZONTAL_WHITESPACE.sub(" ", line).strip(" ") for line in normalized.split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def normalize_symbol(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    if any(_is_disallowed_control(character, allow="\t") for character in normalized):
        raise ValueError("symbol contains disallowed control characters")
    normalized = _HORIZONTAL_WHITESPACE.sub(" ", normalized.replace("\n", " ")).strip(" ")
    if not normalized:
        raise ValueError("symbol normalizes to an empty value")
    return normalized


def normalize_path(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or "\x00" in normalized or "\\" in normalized:
        raise ValueError("invalid repository-relative path")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ValueError("invalid repository-relative path")
    if normalized.startswith("/"):
        raise ValueError("absolute paths are not permitted")
    segments = normalized.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("path traversal and empty segments are not permitted")
    return normalized


def normalize_rule(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip().lower()
    if not re.fullmatch(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", normalized):
        raise ValueError("invalid rule identifier")
    if len(normalized) > 64:
        raise ValueError("rule identifier is too long")
    return normalized


def normalize_category(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if any(_is_disallowed_control(character, allow="\t") for character in normalized):
        raise ValueError("category contains disallowed control characters")
    normalized = _HORIZONTAL_WHITESPACE.sub(" ", normalized.replace("\n", " ")).strip(" ").lower()
    if not normalized:
        raise ValueError("category normalizes to an empty value")
    return normalized


def normalize_title(value: str) -> str:
    """Conservative normalized title for the secondary fingerprint only."""
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    normalized = _HORIZONTAL_WHITESPACE.sub(" ", normalized.replace("\n", " ")).strip(" ").lower()
    return normalized


def _framed(parts: list[str]) -> bytes:
    out = bytearray()
    out += len(parts).to_bytes(4, "big")
    for part in parts:
        encoded = part.encode("utf-8")
        out += len(encoded).to_bytes(8, "big")
        out += encoded
    return bytes(out)


def _digest(parts: list[str]) -> str:
    return hashlib.sha256(_framed(parts)).hexdigest()


def primary_fingerprint_v1(
    *,
    provider_id: str,
    installation_id: str,
    repository_id: str,
    path: str,
    category: str,
    rule: str,
    symbol: str | None,
    evidence: str,
) -> str:
    """Return the lower-case hex SHA-256 primary fingerprint."""
    return _digest(
        [
            PRIMARY_VERSION,
            provider_id,
            installation_id,
            repository_id,
            normalize_path(path),
            normalize_category(category),
            normalize_rule(rule),
            normalize_symbol(symbol) if symbol is not None else _SYMBOL_ABSENT,
            normalize_multiline(evidence),
        ]
    )


def secondary_fingerprint_v1(
    *,
    stable_repository_identity: str,
    path: str,
    category: str,
    rule: str,
    title: str,
) -> str:
    """Return the lower-case hex SHA-256 secondary fingerprint (ranking only)."""
    return _digest(
        [
            SECONDARY_VERSION,
            stable_repository_identity,
            normalize_path(path),
            normalize_category(category),
            normalize_rule(rule),
            normalize_title(title),
        ]
    )


def _diff_corpus(path: str, diff_files: tuple[DiffFile, ...]) -> str | None:
    normalized_path = normalize_path(path)
    fragments: list[str] = []
    found = False
    for item in diff_files:
        candidate = item.new_path or item.old_path
        if candidate is None:
            continue
        try:
            if normalize_path(candidate) != normalized_path:
                continue
        except ValueError:
            continue
        found = True
        fragments.append("\n".join(line.content for line in item.lines))
    if not found:
        return None
    return normalize_multiline("\n".join(fragments)) if fragments else ""


def evidence_occurs_in_diff(*, path: str, evidence: str, diff_files: tuple[DiffFile, ...]) -> bool:
    """Verify that normalized evidence occurs in the submitted diff for ``path``."""
    corpus = _diff_corpus(path, diff_files)
    if corpus is None:
        return False
    normalized_evidence = normalize_multiline(evidence)
    if not normalized_evidence:
        return False
    return normalized_evidence in corpus


def finding_identity_is_valid(
    identity: FindingIdentity, *, path: str, diff_files: tuple[DiffFile, ...]
) -> bool:
    """Return True when every identity field normalizes and evidence occurs in the diff."""
    try:
        normalize_path(path)
        normalize_rule(identity.rule)
        if identity.symbol is not None:
            normalize_symbol(identity.symbol)
        if not normalize_multiline(identity.evidence):
            return False
    except ValueError:
        return False
    return evidence_occurs_in_diff(path=path, evidence=identity.evidence, diff_files=diff_files)


def finding_record_id(stable_repository_identity: str, primary_fingerprint: str) -> str:
    """Deterministic UUID derived from repository identity and primary fingerprint."""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"revio:finding:{stable_repository_identity}:{primary_fingerprint}",
        )
    )
