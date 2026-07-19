"""Sandbox validation CLI safety tests."""

import argparse
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

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
