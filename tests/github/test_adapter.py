"""Read-only GitHub adapter mapping tests."""

import base64
from typing import Any, cast

import httpx
import pytest

from revio.adapters.scm.github.adapter import GitHubReadAdapter
from revio.adapters.scm.github.client import GitHubClient
from revio.adapters.scm.github.errors import GitHubUnsupportedObjectError
from revio.domain.identifiers import ChangeRequestTarget, InstallationRef, ProviderId, RepositoryRef


class FakeClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(
        self, installation_id: int, path: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        assert installation_id == 9
        self.calls.append((path, params))
        return self.responses.pop(0)


@pytest.fixture
def github_target() -> ChangeRequestTarget:
    installation = InstallationRef(provider_id=ProviderId(value="github"), external_id="9")
    repository = RepositoryRef(
        installation=installation, external_id="10", owner="owner", name="repo"
    )
    return ChangeRequestTarget(repository=repository, external_number=3)


@pytest.mark.asyncio
async def test_current_pr_and_diff_mapping(github_target: ChangeRequestTarget) -> None:
    fake = FakeClient(
        [
            httpx.Response(
                200,
                json={
                    "number": 3,
                    "title": "PR",
                    "body": None,
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "base": {"sha": "base", "ref": "main"},
                    "head": {"sha": "head", "ref": "feature"},
                },
            ),
            httpx.Response(
                200,
                json=[
                    {"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+new"}
                ],
            ),
        ]
    )
    adapter = GitHubReadAdapter(cast(GitHubClient, fake))
    change = await adapter.get_change_request(github_target)
    diff = await adapter.get_diff(github_target)
    assert (change.base_sha, change.head_sha) == ("base", "head")
    assert [line.side for line in diff[0].lines] == ["old", "new"]


@pytest.mark.asyncio
async def test_file_explicit_ref_and_unsupported_object(github_target: ChangeRequestTarget) -> None:
    fake = FakeClient(
        [
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
            httpx.Response(200, json={"type": "dir", "sha": "sha"}),
        ]
    )
    adapter = GitHubReadAdapter(cast(GitHubClient, fake))
    assert await adapter.get_file(github_target, "dir/a.py", "head") == "hello"
    assert fake.calls[0][1] == {"ref": "head"}
    with pytest.raises(GitHubUnsupportedObjectError):
        await adapter.get_file(github_target, "dir", "head")


@pytest.mark.asyncio
async def test_tree_resolves_ref_and_is_non_recursive(github_target: ChangeRequestTarget) -> None:
    fake = FakeClient(
        [
            httpx.Response(200, json={"commit": {"tree": {"sha": "tree-sha"}}}),
            httpx.Response(
                200,
                json={
                    "sha": "tree-sha",
                    "truncated": False,
                    "tree": [
                        {
                            "path": "src",
                            "mode": "040000",
                            "type": "tree",
                            "sha": "src-tree",
                        }
                    ],
                },
            ),
            httpx.Response(
                200,
                json={
                    "sha": "src-tree",
                    "truncated": False,
                    "tree": [
                        {
                            "path": "a.py",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "blob",
                            "size": 3,
                        }
                    ],
                },
            ),
        ]
    )
    entries = await GitHubReadAdapter(cast(GitHubClient, fake)).get_tree(
        github_target, "src", "head"
    )
    assert entries[0].path == "src/a.py"
    assert fake.calls == [
        ("/repos/owner/repo/commits/head", None),
        ("/repos/owner/repo/git/trees/tree-sha", None),
        ("/repos/owner/repo/git/trees/src-tree", None),
    ]


@pytest.mark.asyncio
async def test_changed_files_paginates(github_target: ChangeRequestTarget) -> None:
    first = [{"filename": f"file-{index}.py", "status": "added"} for index in range(100)]
    fake = FakeClient([httpx.Response(200, json=first), httpx.Response(200, json=[])])
    files = await GitHubReadAdapter(cast(GitHubClient, fake)).get_diff(github_target)
    assert len(files) == 100
    assert fake.calls[-1][1] == {"per_page": 100, "page": 2}


@pytest.mark.asyncio
async def test_repository_path_traversal_is_rejected(
    github_target: ChangeRequestTarget,
) -> None:
    from revio.adapters.scm.github.errors import GitHubResponseError

    adapter = GitHubReadAdapter(cast(GitHubClient, FakeClient([])))
    with pytest.raises(GitHubResponseError):
        await adapter.get_file(github_target, "../secret", "head")
