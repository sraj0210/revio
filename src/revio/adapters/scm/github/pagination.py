"""Bounded, same-origin GitHub REST pagination."""

from collections.abc import Callable

import httpx

from revio.adapters.scm.github.client import GitHubClient
from revio.adapters.scm.github.errors import GitHubResponseError
from revio.domain.models import CollectionCompleteness


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
    visited: set[str] = set()
    for _ in range(max_pages):
        identity = f"{next_path}?{next_params!r}"
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
