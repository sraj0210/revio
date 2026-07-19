"""GitHub pagination security tests."""

from typing import Any, cast

import httpx
import pytest

from revio.adapters.scm.github.client import GitHubClient
from revio.adapters.scm.github.errors import GitHubResponseError
from revio.adapters.scm.github.pagination import collect_pages


class PagingClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses

    async def get(
        self, installation_id: int, path: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        return self.responses.pop(0)

    def pagination_url(self, value: str) -> str:
        url = httpx.URL(value)
        if url.host != "api.github.com" or url.scheme != "https":
            raise GitHubResponseError("foreign GitHub pagination origin")
        return str(url.copy_with(scheme=None, host=None, port=None))


def parse(response: httpx.Response) -> list[int]:
    value = cast(object, response.json())
    assert isinstance(value, list)
    return [int(item) for item in cast(list[int], value)]


@pytest.mark.asyncio
async def test_foreign_pagination_link_is_rejected() -> None:
    client = PagingClient(
        [
            httpx.Response(
                200,
                json=[1],
                headers={"Link": '<https://evil.invalid/items?page=2>; rel="next"'},
            )
        ]
    )
    with pytest.raises(GitHubResponseError, match="foreign"):
        await collect_pages(
            cast(GitHubClient, client),
            1,
            "/items",
            params=None,
            parse=parse,
            max_pages=2,
            max_items=10,
        )


@pytest.mark.asyncio
async def test_pagination_loop_is_rejected() -> None:
    link = '<https://api.github.com/items>; rel="next"'
    client = PagingClient(
        [
            httpx.Response(200, json=[1], headers={"Link": link}),
            httpx.Response(200, json=[2], headers={"Link": link}),
        ]
    )
    with pytest.raises(GitHubResponseError, match="loop"):
        await collect_pages(
            cast(GitHubClient, client),
            1,
            "/items",
            params=None,
            parse=parse,
            max_pages=3,
            max_items=10,
        )


@pytest.mark.asyncio
async def test_malformed_pagination_link_is_rejected() -> None:
    client = PagingClient(
        [httpx.Response(200, json=[1], headers={"Link": '<not a url>; rel="next"'})]
    )
    with pytest.raises(GitHubResponseError):
        await collect_pages(
            cast(GitHubClient, client),
            1,
            "/items",
            params=None,
            parse=parse,
            max_pages=2,
            max_items=10,
        )


@pytest.mark.asyncio
async def test_same_origin_pagination_completes() -> None:
    client = PagingClient(
        [
            httpx.Response(
                200,
                json=[1],
                headers={"Link": '<https://api.github.com/items?page=2>; rel="next"'},
            ),
            httpx.Response(200, json=[2]),
        ]
    )
    items, completeness = await collect_pages(
        cast(GitHubClient, client),
        1,
        "/items",
        params=None,
        parse=parse,
        max_pages=2,
        max_items=10,
    )
    assert items == [1, 2]
    assert completeness.status == "complete"
