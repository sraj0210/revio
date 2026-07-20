"""Sandbox validation CLI safety tests."""

import argparse
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from revio.adapters.scm.github.adapter import GitHubReadAdapter
from revio.adapters.scm.github.auth import GitHubAppJWT, InstallationTokenCache, load_private_key
from revio.adapters.scm.github.client import GitHubClient
from revio.cli import github_sandbox
from revio.config.github import GitHubSettings
from revio.domain.models import (
    ChangeRequest,
    DiffCollection,
    DiffFile,
    RepositoryEntry,
    TreeCollection,
)


def args(**updates: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "installation_id": 1,
        "repository": "owner/repo",
        "pull_request": 1,
        "path": None,
        "ref": None,
        "tree_path": "",
    }
    values.update(updates)
    return argparse.Namespace(**values)


@pytest.mark.asyncio
async def test_cli_rejects_production_before_network_access() -> None:
    with pytest.raises(ValueError, match="local or sandbox"):
        await github_sandbox.run_validation(args(), GitHubSettings(environment="production"))


@pytest.mark.parametrize(
    "arguments",
    [
        ["--installation-id", "0", "--repository", "o/r", "--pull-request", "1"],
        ["--installation-id", "1", "--repository", "o/r", "--pull-request", "-1"],
    ],
)
def test_cli_requires_positive_identifiers(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        github_sandbox._parser().parse_args(  # pyright: ignore[reportPrivateUsage]
            arguments
        )


@pytest.mark.asyncio
async def test_cli_output_excludes_source_content_and_exposes_completeness(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rsa_private_key_pem: str,
) -> None:
    sentinel = "SENTINEL-SOURCE-CONTENT"

    class Adapter:
        async def get_change_request(self, target: object) -> ChangeRequest:
            return ChangeRequest(
                target=target,  # type: ignore[arg-type]
                title="title",
                base_sha="base",
                head_sha="head",
            )

        async def get_diff(self, target: object) -> DiffCollection:
            return DiffCollection(
                items=(DiffFile(new_path="a", status="added", patch_state="missing"),)
            )

        async def get_file(self, target: object, path: str, ref: str) -> str:
            return sentinel

        async def get_tree(self, target: object, path: str, ref: str) -> TreeCollection:
            return TreeCollection(items=(RepositoryEntry(path="a", entry_type="blob", sha="sha"),))

    class Composition:
        adapter = Adapter()

        async def close(self) -> None:
            return None

    def compose(_: GitHubSettings) -> Composition:
        return Composition()

    monkeypatch.setattr(github_sandbox, "compose_github", compose)
    settings = GitHubSettings(
        environment="sandbox",
        github_enabled=True,
        github_app_id=1,
        github_private_key=SecretStr(rsa_private_key_pem),
    )
    assert await github_sandbox.run_validation(args(path="a", ref="head"), settings) == 0
    output = capsys.readouterr().out
    assert sentinel not in output
    assert '"changed_files_completeness": "complete"' in output
    assert '"completeness": "complete"' in output


def test_cli_operational_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "SENTINEL-SECRET"

    async def fail(*_: object) -> int:
        raise ValueError(sentinel)

    monkeypatch.setattr(github_sandbox, "run_validation", fail)
    monkeypatch.setattr(github_sandbox, "GitHubSettings", lambda: SimpleNamespace())
    monkeypatch.setattr(
        github_sandbox, "_parser", lambda: SimpleNamespace(parse_args=lambda: args())
    )
    assert github_sandbox.main() == 1
    captured = capsys.readouterr()
    assert sentinel not in captured.out + captured.err
    assert "failed safely" in captured.err


def test_cli_malformed_redirect_is_sanitized_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rsa_private_key_pem: str,
) -> None:
    sentinel = "SENTINEL-CLI-REDIRECT"
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("access_tokens"):
            return httpx.Response(
                201,
                json={"token": "opaque", "expires_at": "2099-01-01T00:00:00Z"},
            )
        return httpx.Response(302, headers={"Location": f"//{sentinel}@:bad/steal"})

    settings = GitHubSettings(
        environment="sandbox",
        github_enabled=True,
        github_app_id=1,
        github_private_key=SecretStr(rsa_private_key_pem),
    )
    http = httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    client = GitHubClient(
        http,
        GitHubAppJWT(1, load_private_key(settings)),
        InstallationTokenCache(timedelta(seconds=60), timedelta(seconds=30)),
    )

    class Composition:
        adapter = GitHubReadAdapter(client)

        async def close(self) -> None:
            await http.aclose()

    def compose(_: GitHubSettings) -> Composition:
        return Composition()

    monkeypatch.setattr(github_sandbox, "compose_github", compose)
    monkeypatch.setattr(github_sandbox, "GitHubSettings", lambda: settings)
    monkeypatch.setattr(
        github_sandbox, "_parser", lambda: SimpleNamespace(parse_args=lambda: args())
    )
    assert github_sandbox.main() == 1
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert sentinel not in output
    assert "Traceback" not in output
    assert "failed safely" in captured.err
    assert len(seen) == 2
    assert {request.url.host for request in seen} == {"api.github.com"}
    reads = [request for request in seen if not request.url.path.endswith("access_tokens")]
    assert len(reads) == 1
    assert reads[0].headers["Authorization"] == "Bearer opaque"
