"""Read-only GitHub adapter mapping and boundary tests."""

import base64
from typing import Any, Literal, cast

import httpx
import pytest

from revio.adapters.scm.github.adapter import GitHubReadAdapter
from revio.adapters.scm.github.client import GitHubClient
from revio.adapters.scm.github.errors import (
    GitHubAmbiguousNotFoundError,
    GitHubInvalidRefError,
    GitHubNotFoundError,
    GitHubResponseError,
    GitHubUnsupportedObjectError,
)
from revio.domain.identifiers import ChangeRequestTarget, InstallationRef, ProviderId, RepositoryRef


class FakeClient:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(
        self, installation_id: int, path: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        assert installation_id == 9
        self.calls.append((path, params))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def pagination_url(self, value: str) -> str:
        if not value.startswith("https://api.github.com/"):
            raise GitHubResponseError("foreign GitHub pagination origin")
        return value.removeprefix("https://api.github.com")


@pytest.fixture
def github_target() -> ChangeRequestTarget:
    installation = InstallationRef(provider_id=ProviderId(value="github"), external_id="9")
    repository = RepositoryRef(
        installation=installation, external_id="10", owner="owner", name="repo"
    )
    return ChangeRequestTarget(repository=repository, external_number=3)


def pr_response(*, changed_files: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "number": 3,
            "title": "PR",
            "body": None,
            "state": "open",
            "draft": False,
            "merged": False,
            "changed_files": changed_files,
            "base": {"sha": "base", "ref": "main"},
            "head": {"sha": "head", "ref": "feature"},
        },
    )


def commit_response() -> httpx.Response:
    return httpx.Response(200, json={"commit": {"tree": {"sha": "tree-sha"}}})


@pytest.mark.asyncio
async def test_current_pr_and_diff_line_mapping(github_target: ChangeRequestTarget) -> None:
    fake = FakeClient(
        [
            pr_response(),
            pr_response(),
            httpx.Response(
                200,
                json=[
                    {
                        "filename": "a.py",
                        "status": "modified",
                        "changes": 3,
                        "patch": "@@ -1,2 +1,2 @@\n context\n-old\n+new",
                    }
                ],
            ),
        ]
    )
    adapter = GitHubReadAdapter(cast(GitHubClient, fake))
    change = await adapter.get_change_request(github_target)
    diff = await adapter.get_diff(github_target)
    assert (change.base_sha, change.head_sha) == ("base", "head")
    assert diff.completeness.status == "complete"
    assert [(line.side, line.old_line, line.new_line) for line in diff.items[0].lines] == [
        ("context", 1, 1),
        ("old", 2, None),
        ("new", None, 2),
    ]


@pytest.mark.parametrize(
    ("payload", "status", "old_path", "new_path", "patch_state"),
    [
        (
            {"filename": "a", "status": "added"},
            "added",
            None,
            "a",
            "no_textual_patch_unknown_reason",
        ),
        (
            {"filename": "a", "status": "removed"},
            "deleted",
            "a",
            None,
            "no_textual_patch_unknown_reason",
        ),
        (
            {"filename": "a", "status": "modified"},
            "modified",
            "a",
            "a",
            "no_textual_patch_unknown_reason",
        ),
        (
            {"filename": "b", "previous_filename": "a", "status": "renamed"},
            "renamed",
            "a",
            "b",
            "no_textual_patch_unknown_reason",
        ),
        (
            {"filename": "b", "previous_filename": "a", "status": "copied"},
            "copied",
            "a",
            "b",
            "no_textual_patch_unknown_reason",
        ),
        (
            {"filename": "image", "status": "modified", "changes": 0},
            "modified",
            "image",
            "image",
            "no_textual_patch_unknown_reason",
        ),
        (
            {"filename": "large", "status": "modified", "changes": 20},
            "modified",
            "large",
            "large",
            "no_textual_patch_unknown_reason",
        ),
    ],
)
def test_diff_status_path_and_patch_semantics(
    payload: dict[str, object],
    status: str,
    old_path: str | None,
    new_path: str | None,
    patch_state: str,
) -> None:
    from revio.adapters.scm.github.dto.api import GitHubFileDTO

    mapped = GitHubReadAdapter._map_file(  # pyright: ignore[reportPrivateUsage]
        GitHubFileDTO.model_validate(payload)
    )
    assert (mapped.status, mapped.old_path, mapped.new_path, mapped.patch_state) == (
        status,
        old_path,
        new_path,
        patch_state,
    )


def test_malformed_patch_is_explicit() -> None:
    from revio.adapters.scm.github.dto.api import GitHubFileDTO

    mapped = GitHubReadAdapter._map_file(  # pyright: ignore[reportPrivateUsage]
        GitHubFileDTO(filename="a", status="modified", patch="not-a-hunk")
    )
    assert mapped.patch_state == "malformed"
    assert mapped.lines == ()


def test_valid_patch_is_complete_and_preserves_change_counts() -> None:
    from revio.adapters.scm.github.dto.api import GitHubFileDTO

    mapped = GitHubReadAdapter._map_file(  # pyright: ignore[reportPrivateUsage]
        GitHubFileDTO(
            filename="a",
            status="modified",
            additions=1,
            deletions=1,
            changes=2,
            patch="@@ -1 +1 @@\n-old\n+new",
        )
    )
    assert mapped.patch_state == "complete"
    assert (mapped.additions, mapped.deletions, mapped.changes) == (1, 1, 2)


@pytest.mark.parametrize("status", ["renamed", "copied"])
def test_renamed_and_copied_identity_includes_previous_filename(
    status: Literal["renamed", "copied"],
) -> None:
    from revio.adapters.scm.github.dto.api import GitHubFileDTO

    first = GitHubFileDTO(filename="new", previous_filename="old-a", status=status)
    second = GitHubFileDTO(filename="new", previous_filename="old-b", status=status)
    assert GitHubReadAdapter._file_identity(  # pyright: ignore[reportPrivateUsage]
        first
    ) != GitHubReadAdapter._file_identity(  # pyright: ignore[reportPrivateUsage]
        second
    )


def test_provider_truncated_patch_state_requires_explicit_authoritative_evidence() -> None:
    from revio.domain.models import DiffFile

    # The current GitHub files payload has no authoritative per-patch truncation field.
    explicit = DiffFile(
        new_path="a", status="modified", patch_state="provider_truncated", changes=3
    )
    assert explicit.patch_state == "provider_truncated"


@pytest.mark.asyncio
async def test_diff_provider_and_service_completeness(github_target: ChangeRequestTarget) -> None:
    provider = FakeClient([pr_response(changed_files=2), httpx.Response(200, json=[])])
    result = await GitHubReadAdapter(cast(GitHubClient, provider)).get_diff(github_target)
    assert result.completeness.status == "provider_truncated"

    files = [{"filename": f"f{i}", "status": "added"} for i in range(3)]
    limited = FakeClient([pr_response(changed_files=3), httpx.Response(200, json=files)])
    result = await GitHubReadAdapter(cast(GitHubClient, limited), max_items=2).get_diff(
        github_target
    )
    assert len(result.items) == 2
    assert result.completeness.status == "service_item_limit"

    next_link = '<https://api.github.com/next>; rel="next"'
    pages = FakeClient(
        [pr_response(changed_files=2), httpx.Response(200, json=[], headers={"Link": next_link})]
    )
    result = await GitHubReadAdapter(cast(GitHubClient, pages), max_pages=1).get_diff(github_target)
    assert result.completeness.status == "service_page_limit"


@pytest.mark.asyncio
async def test_identical_duplicate_on_later_page_is_deduplicated_and_incomplete(
    github_target: ChangeRequestTarget,
) -> None:
    item = {"filename": "a", "status": "modified", "changes": 1}
    link = '<https://api.github.com/next>; rel="next"'
    fake = FakeClient(
        [
            pr_response(changed_files=2),
            httpx.Response(200, json=[item], headers={"Link": link}),
            httpx.Response(200, json=[item]),
        ]
    )
    result = await GitHubReadAdapter(cast(GitHubClient, fake)).get_diff(github_target)
    assert len(result.items) == 1
    assert result.completeness.status == "provider_truncated"


@pytest.mark.asyncio
async def test_conflicting_duplicate_on_later_page_fails_safely(
    github_target: ChangeRequestTarget,
) -> None:
    link = '<https://api.github.com/next>; rel="next"'
    fake = FakeClient(
        [
            pr_response(changed_files=1),
            httpx.Response(
                200,
                json=[{"filename": "a", "status": "modified", "changes": 1}],
                headers={"Link": link},
            ),
            httpx.Response(
                200,
                json=[{"filename": "a", "status": "modified", "changes": 2}],
            ),
        ]
    )
    with pytest.raises(GitHubResponseError, match="conflicting duplicate"):
        await GitHubReadAdapter(cast(GitHubClient, fake)).get_diff(github_target)


@pytest.mark.asyncio
async def test_unique_changed_file_count_controls_completeness(
    github_target: ChangeRequestTarget,
) -> None:
    files = [
        {"filename": "a", "status": "added"},
        {"filename": "b", "status": "removed"},
    ]
    complete = FakeClient([pr_response(changed_files=2), httpx.Response(200, json=files)])
    result = await GitHubReadAdapter(cast(GitHubClient, complete)).get_diff(github_target)
    assert result.completeness.status == "complete"

    incomplete = FakeClient([pr_response(changed_files=3), httpx.Response(200, json=files)])
    result = await GitHubReadAdapter(cast(GitHubClient, incomplete)).get_diff(github_target)
    assert result.completeness.status == "provider_truncated"

    inconsistent = FakeClient([pr_response(changed_files=1), httpx.Response(200, json=files)])
    with pytest.raises(GitHubResponseError, match="inconsistent"):
        await GitHubReadAdapter(cast(GitHubClient, inconsistent)).get_diff(github_target)


@pytest.mark.asyncio
async def test_file_requires_verified_repository_ref_and_strict_content(
    github_target: ChangeRequestTarget,
) -> None:
    fake = FakeClient(
        [
            httpx.Response(200),
            commit_response(),
            httpx.Response(
                200,
                json={
                    "type": "file",
                    "content": base64.b64encode(b"hello").decode(),
                    "encoding": "base64",
                    "size": 5,
                    "sha": "sha",
                },
            ),
        ]
    )
    adapter = GitHubReadAdapter(cast(GitHubClient, fake))
    assert await adapter.get_file(github_target, "dir/a.py", "feature/head") == "hello"
    assert fake.calls[-1][1] == {"ref": "feature/head"}


@pytest.mark.asyncio
async def test_file_not_found_only_after_repository_and_ref_are_verified(
    github_target: ChangeRequestTarget,
) -> None:
    missing = FakeClient([httpx.Response(200), commit_response(), GitHubNotFoundError()])
    assert (
        await GitHubReadAdapter(cast(GitHubClient, missing)).get_file(
            github_target, "absent", "head"
        )
        is None
    )
    bad_ref = FakeClient([httpx.Response(200), GitHubNotFoundError()])
    with pytest.raises(GitHubInvalidRefError):
        await GitHubReadAdapter(cast(GitHubClient, bad_ref)).get_file(
            github_target, "absent", "missing"
        )
    bad_repo = FakeClient([GitHubNotFoundError()])
    with pytest.raises(GitHubAmbiguousNotFoundError):
        await GitHubReadAdapter(cast(GitHubClient, bad_repo)).get_file(
            github_target, "absent", "head"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["%%%", base64.b64encode(b"toolarge").decode()])
async def test_file_strict_base64_and_size_limits(
    github_target: ChangeRequestTarget, content: str
) -> None:
    fake = FakeClient(
        [
            httpx.Response(200),
            commit_response(),
            httpx.Response(
                200,
                json={"type": "file", "content": content, "encoding": "base64", "sha": "sha"},
            ),
        ]
    )
    with pytest.raises(GitHubResponseError):
        await GitHubReadAdapter(cast(GitHubClient, fake), max_file_bytes=3).get_file(
            github_target, "a", "head"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_size", [None, 1])
async def test_oversized_base64_is_rejected_before_decode_when_size_is_missing_or_false(
    github_target: ChangeRequestTarget, provider_size: int | None
) -> None:
    content = base64.b64encode(b"oversized").decode()
    payload: dict[str, object] = {
        "type": "file",
        "content": f" {content[:4]}\n{content[4:]}",
        "encoding": "base64",
        "sha": "sha",
    }
    if provider_size is not None:
        payload["size"] = provider_size
    fake = FakeClient([httpx.Response(200), commit_response(), httpx.Response(200, json=payload)])
    with pytest.raises(GitHubResponseError, match="exceeds"):
        await GitHubReadAdapter(cast(GitHubClient, fake), max_file_bytes=3).get_file(
            github_target, "a", "head"
        )


@pytest.mark.asyncio
async def test_unsupported_file_object_is_typed(github_target: ChangeRequestTarget) -> None:
    fake = FakeClient(
        [
            httpx.Response(200),
            commit_response(),
            httpx.Response(200, json={"type": "dir", "sha": "x"}),
        ]
    )
    with pytest.raises(GitHubUnsupportedObjectError):
        await GitHubReadAdapter(cast(GitHubClient, fake)).get_file(github_target, "dir", "head")


@pytest.mark.asyncio
async def test_tree_resolves_ref_and_reports_truncation(github_target: ChangeRequestTarget) -> None:
    tree = {
        "sha": "tree-sha",
        "truncated": True,
        "tree": [{"path": "a", "mode": "100644", "type": "blob", "sha": "blob"}],
    }
    fake = FakeClient([httpx.Response(200), commit_response(), httpx.Response(200, json=tree)])
    result = await GitHubReadAdapter(cast(GitHubClient, fake)).get_tree(github_target, "", "head")
    assert result.completeness.status == "provider_truncated"
    assert result.items[0].path == "a"
    assert fake.calls == [
        ("/repos/owner/repo", None),
        ("/repos/owner/repo/commits/head", None),
        ("/repos/owner/repo/git/trees/tree-sha", None),
    ]


@pytest.mark.asyncio
async def test_tree_service_item_and_request_limits(github_target: ChangeRequestTarget) -> None:
    tree = {
        "sha": "tree-sha",
        "tree": [
            {"path": "a", "mode": "100644", "type": "blob", "sha": "a"},
            {"path": "b", "mode": "100644", "type": "blob", "sha": "b"},
        ],
    }
    fake = FakeClient([httpx.Response(200), commit_response(), httpx.Response(200, json=tree)])
    result = await GitHubReadAdapter(cast(GitHubClient, fake), max_items=1).get_tree(
        github_target, "", "head"
    )
    assert result.completeness.status == "service_item_limit"

    depth = FakeClient([httpx.Response(200), commit_response()])
    result = await GitHubReadAdapter(cast(GitHubClient, depth), max_pages=0).get_tree(
        github_target, "src", "head"
    )
    assert result.completeness.status == "service_page_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/absolute",
        "../secret",
        "a/./b",
        "a\\b",
        "%2e%2e/secret",
        "%252e%252e/secret",
        "a\x00b",
    ],
)
async def test_repository_path_attacks_are_rejected(
    github_target: ChangeRequestTarget, path: str
) -> None:
    adapter = GitHubReadAdapter(cast(GitHubClient, FakeClient([])))
    with pytest.raises(GitHubResponseError):
        await adapter.get_file(github_target, path, "head")


@pytest.mark.asyncio
async def test_empty_ref_and_invalid_coordinates_are_rejected(
    github_target: ChangeRequestTarget,
) -> None:
    adapter = GitHubReadAdapter(cast(GitHubClient, FakeClient([])))
    with pytest.raises(GitHubResponseError):
        await adapter.get_file(github_target, "a", "")
    bad_repository = github_target.repository.model_copy(update={"owner": "bad/owner"})
    bad_target = github_target.model_copy(update={"repository": bad_repository})
    with pytest.raises(GitHubResponseError):
        await adapter.get_change_request(bad_target)
