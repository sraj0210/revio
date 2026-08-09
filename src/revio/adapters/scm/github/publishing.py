"""GitHub Check Run and pull-request review publishing with bounded reconciliation."""

import re
from typing import Any, cast

import httpx

from revio.adapters.scm.github.adapter import GitHubReadAdapter
from revio.adapters.scm.github.client import GitHubClient
from revio.adapters.scm.github.errors import GitHubResponseError
from revio.adapters.scm.github.pagination import collect_pages
from revio.application.review.markers import marker_matches
from revio.domain.identifiers import ChangeRequestTarget
from revio.domain.models import DiffCollection, Finding
from revio.domain.reviews import ReconciliationResult, ReviewStatusDetails

_MARKER = re.compile(r"<!-- revio:v1:review:[A-Za-z0-9_-]{24} -->")


def _list(response: httpx.Response) -> list[dict[str, Any]]:
    try:
        value = cast(object, response.json())
    except ValueError:
        raise GitHubResponseError("invalid GitHub reconciliation response") from None
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        check_runs = mapping.get("check_runs")
        if isinstance(check_runs, list):
            value = cast(list[object], check_runs)
    if not isinstance(value, list):
        raise GitHubResponseError("invalid GitHub reconciliation response")
    values = cast(list[object], value)
    if not all(isinstance(item, dict) for item in values):
        raise GitHubResponseError("invalid GitHub reconciliation response")
    return cast(list[dict[str, Any]], values)


class GitHubReviewWriter:
    def __init__(
        self,
        client: GitHubClient,
        app_id: int,
        *,
        max_pages: int = 30,
        max_items: int = 3_000,
    ) -> None:
        self._client = client
        self._app_id = app_id
        self._max_pages = max_pages
        self._max_items = max_items

    @staticmethod
    def _target(target: ChangeRequestTarget) -> tuple[int, str, int]:
        installation, owner, repo = GitHubReadAdapter.coordinates(target)
        return installation, GitHubReadAdapter.repo_path(owner, repo), target.external_number

    @staticmethod
    def _reconciliation(
        matches: list[str], completeness: str, pages: int, items: int
    ) -> ReconciliationResult:
        complete = completeness == "complete"
        return ReconciliationResult(
            match_count=len(matches),
            collection_complete=complete,
            pages_inspected=pages,
            items_inspected=items,
            provider_limit_reached=completeness == "provider_truncated",
            service_limit_reached=completeness in {"service_page_limit", "service_item_limit"},
            provider_ids=tuple(matches),
        )

    async def reconcile_check_run(
        self,
        target: ChangeRequestTarget,
        *,
        head_sha: str,
        external_id: str,
        name: str = "Revio review",
    ) -> ReconciliationResult:
        installation, root, _ = self._target(target)
        pages = 0

        def parse(response: httpx.Response) -> list[dict[str, Any]]:
            nonlocal pages
            pages += 1
            return _list(response)

        items, completeness = await collect_pages(
            self._client,
            installation,
            f"{root}/commits/{head_sha}/check-runs",
            params={"filter": "all", "per_page": 100},
            parse=parse,
            max_pages=self._max_pages,
            max_items=self._max_items,
        )
        matches: list[str] = []
        for item in items:
            app = cast(object, item.get("app"))
            if (
                item.get("name") == name
                and item.get("external_id") == external_id
                and isinstance(app, dict)
                and cast(dict[object, object], app).get("id") == self._app_id
                and str(item.get("head_sha", head_sha)) == head_sha
            ):
                identifier = item.get("id")
                if isinstance(identifier, int | str):
                    matches.append(str(identifier))
        return self._reconciliation(matches, completeness.status, pages, len(items))

    async def create_check_run(
        self, target: ChangeRequestTarget, details: ReviewStatusDetails
    ) -> str:
        installation, root, _ = self._target(target)
        payload: dict[str, Any] = {
            "name": details.name,
            "head_sha": details.head_sha,
            "external_id": details.external_id,
            "status": "queued" if details.status == "queued" else details.status,
            "output": {"title": details.name, "summary": details.summary},
        }
        response = await self._client.write(
            installation, "POST", f"{root}/check-runs", json_body=payload
        )
        try:
            identifier = cast(dict[str, Any], response.json())["id"]
            return str(identifier)
        except (ValueError, TypeError, KeyError):
            raise GitHubResponseError("invalid GitHub Check Run response") from None

    async def update_check_run(
        self,
        target: ChangeRequestTarget,
        provider_id: str,
        details: ReviewStatusDetails,
    ) -> None:
        installation, root, _ = self._target(target)
        payload: dict[str, Any] = {
            "status": details.status,
            "output": {"title": details.name, "summary": details.summary},
        }
        if details.status == "completed":
            payload["conclusion"] = details.conclusion
        await self._client.write(
            installation, "PATCH", f"{root}/check-runs/{provider_id}", json_body=payload
        )

    async def get_check_run(
        self, target: ChangeRequestTarget, provider_id: str
    ) -> dict[str, object]:
        installation, root, _ = self._target(target)
        response = await self._client.get(installation, f"{root}/check-runs/{provider_id}")
        try:
            value = cast(object, response.json())
        except ValueError:
            raise GitHubResponseError("invalid GitHub Check Run response") from None
        if not isinstance(value, dict):
            raise GitHubResponseError("invalid GitHub Check Run response")
        return cast(dict[str, object], value)

    async def reconcile_review(
        self, target: ChangeRequestTarget, *, marker: str
    ) -> ReconciliationResult:
        installation, root, number = self._target(target)
        pages = 0

        def parse(response: httpx.Response) -> list[dict[str, Any]]:
            nonlocal pages
            pages += 1
            return _list(response)

        items, completeness = await collect_pages(
            self._client,
            installation,
            f"{root}/pulls/{number}/reviews",
            params={"per_page": 100},
            parse=parse,
            max_pages=self._max_pages,
            max_items=self._max_items,
        )
        matches: list[str] = []
        for item in items:
            body = item.get("body")
            identifier = item.get("id")
            if isinstance(body, str) and isinstance(identifier, int | str):
                candidates = _MARKER.findall(body)
                if any(marker_matches(marker, candidate) for candidate in candidates):
                    matches.append(str(identifier))
        return self._reconciliation(matches, completeness.status, pages, len(items))

    @staticmethod
    def validate_inline_findings(
        findings: tuple[Finding, ...], diff: DiffCollection
    ) -> tuple[Finding, ...]:
        valid = {
            (item.new_path, line.new_line)
            for item in diff.items
            if item.new_path is not None
            for line in item.lines
            if line.new_line is not None and line.side in {"new", "context"}
        }
        return tuple(
            item for item in findings if item.line is not None and (item.path, item.line) in valid
        )

    async def publish_review(
        self,
        target: ChangeRequestTarget,
        *,
        summary: str,
        findings: tuple[Finding, ...],
        marker: str,
        commit_id: str,
    ) -> str:
        installation, root, number = self._target(target)
        comments = [
            {
                "path": finding.path,
                "line": finding.line,
                "side": "RIGHT",
                "body": f"**{finding.title}**\n\n{finding.explanation}",
            }
            for finding in findings
            if finding.line is not None
        ]
        response = await self._client.write(
            installation,
            "POST",
            f"{root}/pulls/{number}/reviews",
            json_body={
                "event": "COMMENT",
                "commit_id": commit_id,
                "body": f"{summary}\n\n{marker}",
                "comments": comments,
            },
        )
        try:
            return str(cast(dict[str, Any], response.json())["id"])
        except (ValueError, TypeError, KeyError):
            raise GitHubResponseError("invalid GitHub review response") from None
