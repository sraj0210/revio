"""GitHub Phase 4 reconciliation and pinned publication contracts."""

from typing import cast

import httpx
import pytest

from revio.adapters.scm.github.client import GitHubClient
from revio.adapters.scm.github.publishing import GitHubReviewWriter
from revio.domain.identifiers import (
    ChangeRequestTarget,
    InstallationRef,
    ProviderId,
    RepositoryRef,
)


class FakeClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.writes: list[tuple[str, str, dict[str, object]]] = []

    async def get(
        self, installation_id: int, path: str, *, params: dict[str, object] | None = None
    ) -> httpx.Response:
        return self.responses.pop(0)

    def pagination_url(self, value: str) -> str:
        return value.replace("https://api.github.com", "")

    async def write(
        self,
        installation_id: int,
        method: str,
        path: str,
        *,
        json_body: dict[str, object],
    ) -> httpx.Response:
        self.writes.append((method, path, json_body))
        return self.responses.pop(0)


@pytest.fixture
def github_target() -> ChangeRequestTarget:
    provider = ProviderId(value="github")
    installation = InstallationRef(provider_id=provider, external_id="1")
    repository = RepositoryRef(
        installation=installation,
        external_id="2",
        owner="owner",
        name="repo",
    )
    return ChangeRequestTarget(repository=repository, external_number=3)


def response(payload: object, *, link: str | None = None) -> httpx.Response:
    headers = {"Link": link} if link else {}
    return httpx.Response(200, json=payload, headers=headers)


@pytest.mark.asyncio
async def test_complete_zero_and_last_page_exact_check_match(
    github_target: ChangeRequestTarget,
) -> None:
    last = '<https://api.github.com/next>; rel="next"'
    fake = FakeClient(
        [
            response({"check_suites": []}, link=last),
            response(
                {
                    "check_suites": [
                        {"id": 5, "app": {"id": 7}},
                    ]
                }
            ),
            response(
                {
                    "check_runs": [
                        {
                            "id": 9,
                            "name": "Revio review",
                            "external_id": "external",
                            "head_sha": "head",
                            "app": {"id": 7},
                        }
                    ]
                }
            ),
        ]
    )
    writer = GitHubReviewWriter(cast(GitHubClient, fake), 7, max_pages=3)
    result = await writer.reconcile_check_run(
        github_target, head_sha="head", external_id="external"
    )
    assert result.collection_complete
    assert result.pages_inspected == 3
    assert result.provider_ids == ("9",)


@pytest.mark.asyncio
async def test_page_cap_is_incomplete_and_zero_is_not_actionable(
    github_target: ChangeRequestTarget,
) -> None:
    fake = FakeClient(
        [response({"check_suites": []}, link='<https://api.github.com/next>; rel="next"')]
    )
    writer = GitHubReviewWriter(cast(GitHubClient, fake), 7, max_pages=1)
    result = await writer.reconcile_check_run(
        github_target, head_sha="head", external_id="external"
    )
    assert not result.collection_complete
    assert result.service_limit_reached
    assert not result.actionable_zero


@pytest.mark.asyncio
async def test_review_post_pins_comment_event_commit_and_right_anchor(
    github_target: ChangeRequestTarget,
) -> None:
    fake = FakeClient([response({"id": 11})])
    writer = GitHubReviewWriter(cast(GitHubClient, fake), 7)
    from revio.domain.models import Finding

    identifier = await writer.publish_review(
        github_target,
        summary="Summary",
        findings=(
            Finding(
                category="correctness",
                title="Issue",
                explanation="Explanation",
                confidence=0.9,
                path="a.py",
                line=4,
            ),
        ),
        marker="<!-- revio:v1:review:abcdefghijklmnopqrstuvwx -->",
        commit_id="validated-head",
    )
    assert identifier == "11"
    payload = fake.writes[0][2]
    assert payload["event"] == "COMMENT"
    assert payload["commit_id"] == "validated-head"
    comments = cast(list[dict[str, object]], payload["comments"])
    assert comments[0]["side"] == "RIGHT"


@pytest.mark.asyncio
async def test_check_suite_complete_zero_is_actionable(
    github_target: ChangeRequestTarget,
) -> None:
    writer = GitHubReviewWriter(
        cast(GitHubClient, FakeClient([response({"total_count": 0, "check_suites": []})])),
        7,
    )
    result = await writer.reconcile_check_run(
        github_target, head_sha="head", external_id="external"
    )
    assert result.actionable_zero and result.collection_complete


@pytest.mark.asyncio
async def test_check_suite_multiple_matches_are_fully_enumerated(
    github_target: ChangeRequestTarget,
) -> None:
    fake = FakeClient(
        [
            response({"total_count": 1, "check_suites": [{"id": 5, "app": {"id": 7}}]}),
            response(
                {
                    "total_count": 2,
                    "check_runs": [
                        {
                            "id": value,
                            "name": "Revio review",
                            "external_id": "external",
                            "head_sha": "head",
                            "app": {"id": 7},
                        }
                        for value in (9, 10)
                    ],
                }
            ),
        ]
    )
    result = await GitHubReviewWriter(cast(GitHubClient, fake), 7).reconcile_check_run(
        github_target, head_sha="head", external_id="external"
    )
    assert result.collection_complete and result.provider_ids == ("9", "10")


@pytest.mark.asyncio
async def test_provider_truncated_suite_collection_is_not_actionable(
    github_target: ChangeRequestTarget,
) -> None:
    fake = FakeClient([response({"total_count": 2, "check_suites": []})])
    result = await GitHubReviewWriter(cast(GitHubClient, fake), 7).reconcile_check_run(
        github_target, head_sha="head", external_id="external"
    )
    assert result.provider_limit_reached and not result.actionable_zero


@pytest.mark.asyncio
async def test_service_item_limit_across_suites_is_not_complete(
    github_target: ChangeRequestTarget,
) -> None:
    fake = FakeClient(
        [
            response(
                {
                    "total_count": 2,
                    "check_suites": [
                        {"id": 5, "app": {"id": 7}},
                        {"id": 6, "app": {"id": 7}},
                    ],
                }
            )
        ]
    )
    result = await GitHubReviewWriter(cast(GitHubClient, fake), 7, max_items=1).reconcile_check_run(
        github_target, head_sha="head", external_id="external"
    )
    assert result.service_limit_reached and not result.collection_complete
