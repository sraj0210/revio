"""Bounded, same-origin GitHub REST pagination."""

import re
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import httpx

from revio.adapters.scm.github.client import GitHubClient
from revio.adapters.scm.github.errors import GitHubResponseError
from revio.domain.models import CollectionCompleteness

_PERCENT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _normalize_percent_encoding(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        decoded = chr(int(match.group(1), 16))
        return decoded if decoded in _UNRESERVED else match.group(0).upper()

    return _PERCENT_ESCAPE.sub(replace, value)


def _page_identity(path: str, params: dict[str, object] | None) -> tuple[object, ...]:
    absolute = urlsplit(urljoin("https://api.github.com", path))
    query = list(parse_qsl(absolute.query, keep_blank_values=True))
    if params is not None:
        query.extend(parse_qsl(urlencode(params, doseq=True), keep_blank_values=True))
    return (
        absolute.scheme.lower(),
        (absolute.hostname or "").lower(),
        absolute.port or (443 if absolute.scheme.lower() == "https" else 80),
        _normalize_percent_encoding(absolute.path),
        tuple(
            sorted(
                (_normalize_percent_encoding(key), _normalize_percent_encoding(value))
                for key, value in query
            )
        ),
    )


async def collect_pages[T](
    client: GitHubClient,
    installation_id: int,
    path: str,
    *,
    params: dict[str, object] | None,
    parse: Callable[[httpx.Response], list[T]],
    max_pages: int,
    max_items: int,
) -> tuple[list[T], CollectionCompleteness]:
    items: list[T] = []
    next_path = path
    next_params = params
    visited: set[tuple[object, ...]] = set()
    for _ in range(max_pages):
        identity = _page_identity(next_path, next_params)
        if identity in visited:
            raise GitHubResponseError("GitHub pagination loop detected")
        visited.add(identity)
        response = await client.get(
            installation_id,
            next_path,
            params=dict(next_params) if next_params is not None else None,
        )
        batch = parse(response)
        available = max_items - len(items)
        if len(batch) > available:
            items.extend(batch[:available])
            return items, CollectionCompleteness(status="service_item_limit")
        items.extend(batch)
        try:
            link = response.links.get("next")
        except (KeyError, ValueError):
            raise GitHubResponseError("invalid GitHub pagination link") from None
        if link is None:
            return items, CollectionCompleteness(status="complete")
        url = link.get("url")
        if not isinstance(url, str):
            raise GitHubResponseError("invalid GitHub pagination link")
        next_path = client.pagination_url(url)
        next_params = None
    return items, CollectionCompleteness(status="service_page_limit")
