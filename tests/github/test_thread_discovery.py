"""GitHub GraphQL review-thread discovery and resolution (adapter-private)."""

from typing import Any, cast

import pytest

from revio.adapters.scm.github.client import GitHubClient
from revio.adapters.scm.github.errors import GitHubResponseError, GitHubValidationError
from revio.adapters.scm.github.threads import GitHubThreadReader, GitHubThreadResolver
from revio.domain.identifiers import (
    ChangeRequestTarget,
    InstallationRef,
    ProviderId,
    RepositoryRef,
)

MARKER = "<!-- revio:v1:finding:AAAAAAAAAAAAAAAAAAAAAAAA -->"
OTHER = "<!-- revio:v1:finding:BBBBBBBBBBBBBBBBBBBBBBBB -->"


def _target() -> ChangeRequestTarget:
    provider = ProviderId(value="github")
    installation = InstallationRef(provider_id=provider, external_id="174693302")
    repository = RepositoryRef(
        installation=installation, external_id="1328857147", owner="sraj0210", name="sandbox"
    )
    return ChangeRequestTarget(repository=repository, external_number=7)


def _page(
    nodes: list[dict[str, Any]], *, has_next: bool = False, cursor: str = "c1"
) -> dict[str, Any]:
    return {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": nodes,
                }
            }
        }
    }


def _thread(thread_id: str, *, resolved: bool, body: str) -> dict[str, Any]:
    return {
        "id": thread_id,
        "isResolved": resolved,
        "comments": {"nodes": [{"id": "C1", "body": body, "pullRequestReview": {"id": "R1"}}]},
    }


class _FakeGraphQL:
    def __init__(
        self,
        pages_by_cursor: dict[Any, dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.pages_by_cursor = pages_by_cursor or {}
        self.error = error
        self.queries = 0
        self.mutations: list[str] = []

    async def graphql_query(
        self, installation_id: int, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        self.queries += 1
        if self.error is not None:
            raise self.error
        return self.pages_by_cursor[variables.get("cursor")]

    async def graphql_mutation(
        self, installation_id: int, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        self.mutations.append(cast(str, variables["id"]))
        if self.error is not None:
            raise self.error
        return {"resolveReviewThread": {"thread": {"id": variables["id"], "isResolved": True}}}


def _reader(fake: _FakeGraphQL) -> GitHubThreadReader:
    return GitHubThreadReader(cast(GitHubClient, fake), max_pages=5, max_items=100)


@pytest.mark.asyncio
async def test_discovery_exactly_one_match() -> None:
    fake = _FakeGraphQL({None: _page([_thread("T1", resolved=False, body=f"finding\n{MARKER}")])})
    result = await _reader(fake).discover_thread(_target(), marker=MARKER)
    assert result.match_count == 1
    assert result.collection_complete is True
    assert result.thread_node_ids == ("T1",)
    assert result.resolved_states == (False,)
    assert result.review_ids == ("R1",)


@pytest.mark.asyncio
async def test_discovery_zero_complete_is_actionable() -> None:
    fake = _FakeGraphQL({None: _page([_thread("T1", resolved=False, body=f"no marker {OTHER}")])})
    result = await _reader(fake).discover_thread(_target(), marker=MARKER)
    assert result.match_count == 0
    assert result.collection_complete is True
    assert result.actionable_zero is True


@pytest.mark.asyncio
async def test_discovery_multiple_matches_is_conflict() -> None:
    nodes = [
        _thread("T1", resolved=False, body=MARKER),
        _thread("T2", resolved=True, body=MARKER),
    ]
    fake = _FakeGraphQL({None: _page(nodes)})
    result = await _reader(fake).discover_thread(_target(), marker=MARKER)
    assert result.match_count == 2


@pytest.mark.asyncio
async def test_discovery_paginates() -> None:
    pages = {
        None: _page([_thread("T0", resolved=False, body="nope")], has_next=True, cursor="c1"),
        "c1": _page([_thread("T1", resolved=False, body=MARKER)], has_next=False),
    }
    fake = _FakeGraphQL(pages)
    result = await _reader(fake).discover_thread(_target(), marker=MARKER)
    assert result.match_count == 1
    assert result.pages_inspected == 2
    assert result.collection_complete is True


@pytest.mark.asyncio
async def test_discovery_errors_are_indeterminate() -> None:
    fake = _FakeGraphQL(error=GitHubResponseError("boom"))
    result = await _reader(fake).discover_thread(_target(), marker=MARKER)
    assert result.match_count == 0
    assert result.collection_complete is False


@pytest.mark.asyncio
async def test_read_thread_resolved_state() -> None:
    fake = _FakeGraphQL()

    async def read(installation_id: int, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {"node": {"id": variables["id"], "isResolved": True}}

    fake.graphql_query = read  # type: ignore[method-assign]
    resolver = GitHubThreadResolver(cast(GitHubClient, fake))
    result = await resolver.read_thread(_target(), thread_node_id="T1")
    assert result.found is True and result.is_resolved is True and result.collection_complete


@pytest.mark.asyncio
async def test_resolve_thread_issues_single_mutation() -> None:
    fake = _FakeGraphQL()
    resolver = GitHubThreadResolver(cast(GitHubClient, fake))
    await resolver.resolve_thread(_target(), thread_node_id="T1")
    assert fake.mutations == ["T1"]


@pytest.mark.asyncio
async def test_resolve_thread_propagates_rejection() -> None:
    fake = _FakeGraphQL(error=GitHubValidationError("rejected"))
    resolver = GitHubThreadResolver(cast(GitHubClient, fake))
    with pytest.raises(GitHubValidationError):
        await resolver.resolve_thread(_target(), thread_node_id="T1")
