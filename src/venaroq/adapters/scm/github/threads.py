"""Adapter-private GitHub GraphQL review-thread discovery and resolution.

Discovery is a bounded, cursor-paginated GraphQL read that matches only the exact per-finding
hidden marker. Resolution is one `resolveReviewThread` mutation reconciled by complete rereads.
Both keep the REST same-origin, authentication, and redaction posture.
"""

import re
from typing import Any, cast

from venaroq.adapters.scm.github.adapter import GitHubReadAdapter
from venaroq.adapters.scm.github.client import GitHubClient
from venaroq.adapters.scm.github.errors import GitHubResponseError
from venaroq.application.review.markers import marker_matches
from venaroq.domain.identifiers import ChangeRequestTarget
from venaroq.domain.lifecycle import ThreadDiscoveryResult, ThreadReadResult

_FINDING_MARKER = re.compile(r"<!-- venaroq:v1:finding:[A-Za-z0-9_-]{24} -->")

_DISCOVERY_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:100, after:$cursor){
        pageInfo{ hasNextPage endCursor }
        nodes{
          id
          isResolved
          comments(first:20){ nodes{ id body pullRequestReview{ id } } }
        }
      }
    }
  }
}
"""

_READ_QUERY = """
query($id:ID!){
  node(id:$id){
    ... on PullRequestReviewThread{ id isResolved }
  }
}
"""

_RESOLVE_MUTATION = """
mutation($id:ID!){
  resolveReviewThread(input:{threadId:$id}){
    thread{ id isResolved }
  }
}
"""


def _as_dict(value: object) -> dict[str, Any] | None:
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _as_list(value: object) -> list[Any] | None:
    return cast(list[Any], value) if isinstance(value, list) else None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _review_threads(data: dict[str, Any]) -> dict[str, Any]:
    repository = _as_dict(data.get("repository"))
    pull_request = _as_dict(repository.get("pullRequest")) if repository is not None else None
    threads = _as_dict(pull_request.get("reviewThreads")) if pull_request is not None else None
    if threads is None:
        raise GitHubResponseError("GitHub GraphQL review-thread shape is unexpected")
    return threads


class GitHubThreadReader:
    def __init__(
        self, client: GitHubClient, *, max_pages: int = 30, max_items: int = 3_000
    ) -> None:
        self._client = client
        self._max_pages = max_pages
        self._max_items = max_items

    async def discover_thread(
        self, target: ChangeRequestTarget, *, marker: str
    ) -> ThreadDiscoveryResult:
        installation, owner, repo = GitHubReadAdapter.coordinates(target)
        cursor: str | None = None
        pages = 0
        items = 0
        thread_ids: list[str] = []
        resolved_states: list[bool] = []
        review_ids: list[str] = []
        comment_ids: list[str] = []
        complete = False
        service_limit = False
        try:
            for _ in range(self._max_pages):
                pages += 1
                data = await self._client.graphql_query(
                    installation,
                    _DISCOVERY_QUERY,
                    {
                        "owner": owner,
                        "name": repo,
                        "number": target.external_number,
                        "cursor": cursor,
                    },
                )
                threads = _review_threads(data)
                nodes = _as_list(threads.get("nodes"))
                if nodes is None:
                    raise GitHubResponseError("GitHub GraphQL review-thread nodes are invalid")
                for raw in nodes:
                    node = _as_dict(raw)
                    if node is None:
                        continue
                    items += 1
                    if items > self._max_items:
                        service_limit = True
                        break
                    matched = _match_thread(node, marker)
                    if matched is not None:
                        thread_id, is_resolved, review_id, comment_id = matched
                        thread_ids.append(thread_id)
                        resolved_states.append(is_resolved)
                        if review_id is not None:
                            review_ids.append(review_id)
                        if comment_id is not None:
                            comment_ids.append(comment_id)
                page_info = _as_dict(threads.get("pageInfo"))
                if service_limit:
                    break
                if page_info is None or not page_info.get("hasNextPage"):
                    complete = True
                    break
                cursor = _as_str(page_info.get("endCursor"))
        except GitHubResponseError:
            # A malformed or errored enumeration is indeterminate; it never guesses absence.
            return ThreadDiscoveryResult(
                match_count=0,
                collection_complete=False,
                pages_inspected=pages,
                items_inspected=items,
            )
        return ThreadDiscoveryResult(
            match_count=len(thread_ids),
            collection_complete=complete and not service_limit,
            pages_inspected=pages,
            items_inspected=items,
            service_limit_reached=service_limit or (not complete),
            thread_node_ids=tuple(thread_ids),
            resolved_states=tuple(resolved_states),
            review_ids=tuple(review_ids),
            comment_ids=tuple(comment_ids),
        )


def _match_thread(
    node: dict[str, Any], marker: str
) -> tuple[str, bool, str | None, str | None] | None:
    thread_id = node.get("id")
    is_resolved = node.get("isResolved")
    comment_nodes = None
    comments = _as_dict(node.get("comments"))
    if comments is not None:
        comment_nodes = _as_list(comments.get("nodes"))
    if not isinstance(thread_id, str) or not isinstance(is_resolved, bool):
        return None
    if comment_nodes is None:
        return None
    for raw in comment_nodes:
        comment = _as_dict(raw)
        if comment is None:
            continue
        body = _as_str(comment.get("body"))
        if body is None:
            continue
        if any(marker_matches(marker, candidate) for candidate in _FINDING_MARKER.findall(body)):
            review = _as_dict(comment.get("pullRequestReview"))
            review_id = _as_str(review.get("id")) if review is not None else None
            return (thread_id, is_resolved, review_id, _as_str(comment.get("id")))
    return None


class GitHubThreadResolver:
    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    async def read_thread(
        self, target: ChangeRequestTarget, *, thread_node_id: str
    ) -> ThreadReadResult:
        installation, _owner, _repo = GitHubReadAdapter.coordinates(target)
        try:
            data = await self._client.graphql_query(
                installation, _READ_QUERY, {"id": thread_node_id}
            )
        except GitHubResponseError:
            return ThreadReadResult(found=False, is_resolved=False, collection_complete=False)
        if "node" not in data:
            return ThreadReadResult(found=False, is_resolved=False, collection_complete=False)
        node = _as_dict(data.get("node"))
        if node is None:
            # A null node is a complete, authoritative "thread not found" answer.
            return ThreadReadResult(
                found=False, is_resolved=False, collection_complete=True, thread_node_id=None
            )
        is_resolved = node.get("isResolved")
        node_id = _as_str(node.get("id"))
        if not isinstance(is_resolved, bool) or node_id is None:
            return ThreadReadResult(found=False, is_resolved=False, collection_complete=False)
        return ThreadReadResult(
            found=True,
            is_resolved=is_resolved,
            collection_complete=True,
            thread_node_id=node_id,
        )

    async def resolve_thread(self, target: ChangeRequestTarget, *, thread_node_id: str) -> None:
        installation, _owner, _repo = GitHubReadAdapter.coordinates(target)
        await self._client.graphql_mutation(installation, _RESOLVE_MUTATION, {"id": thread_node_id})
