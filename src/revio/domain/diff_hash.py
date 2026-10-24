"""Deterministic ``diff-hash-v1`` over a validated, bounded diff.

The caller computes this only after provider completeness validation and Phase 5 exclusions
and before service/token truncation. An incomplete provider collection or incomplete patch
may still produce a diagnostic hash, but such inputs are never eligible for unchanged-run
generation reuse; that eligibility decision lives in the application layer.
"""

import hashlib

from revio.domain.models import DiffFile

DIFF_HASH_VERSION = "diff-hash-v1"


def _framed(parts: list[str]) -> bytes:
    out = bytearray()
    out += len(parts).to_bytes(4, "big")
    for part in parts:
        encoded = part.encode("utf-8")
        out += len(encoded).to_bytes(8, "big")
        out += encoded
    return bytes(out)


def _optional(value: str | None) -> list[str]:
    return ["1", value] if value is not None else ["0", ""]


def diff_hash_v1(files: tuple[DiffFile, ...]) -> str:
    """Return the lower-case hex SHA-256 of the canonical diff encoding in provider order."""
    parts: list[str] = [DIFF_HASH_VERSION, str(len(files))]
    for item in files:
        parts.extend(_optional(item.old_path))
        parts.extend(_optional(item.new_path))
        parts.append(item.status)
        parts.append(item.patch_state)
        parts.append(str(len(item.lines)))
        for line in item.lines:
            parts.append(line.side)
            parts.append("" if line.old_line is None else str(line.old_line))
            parts.append("" if line.new_line is None else str(line.new_line))
            parts.append(line.content.replace("\r\n", "\n").replace("\r", "\n"))
    return hashlib.sha256(_framed(parts)).hexdigest()
