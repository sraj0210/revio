"""Validation for values used to construct GitHub API requests."""

import re
from urllib.parse import unquote

from revio.adapters.scm.github.errors import GitHubResponseError

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def validate_coordinate(value: str, role: str) -> str:
    if not value or "/" in value or "\\" in value or _CONTROL.search(value):
        raise GitHubResponseError(f"invalid repository {role}")
    return value


def validate_ref(value: str) -> str:
    if not value or _CONTROL.search(value) or "\\" in value:
        raise GitHubResponseError("invalid repository ref")
    return value


def validate_repository_path(path: str, *, allow_empty: bool = False) -> list[str]:
    if path.startswith("/") or "\\" in path or _CONTROL.search(path):
        raise GitHubResponseError("invalid repository path")
    parts = path.split("/") if path else []
    if not parts and not allow_empty:
        raise GitHubResponseError("repository path is required")
    for part in parts:
        decoded = part
        if not part:
            raise GitHubResponseError("invalid repository path")
        for _ in range(4):
            if (
                decoded in {".", ".."}
                or "/" in decoded
                or "\\" in decoded
                or _CONTROL.search(decoded)
            ):
                raise GitHubResponseError("invalid repository path")
            expanded = unquote(decoded)
            if expanded == decoded:
                break
            decoded = expanded
    return parts


def validate_positive_identifier(value: int, role: str) -> int:
    if value <= 0:
        raise GitHubResponseError(f"invalid {role}")
    return value
