"""Sandbox validation CLI safety tests."""

import argparse

import pytest

from revio.cli.github_sandbox import run_validation
from revio.config.github import GitHubSettings


@pytest.mark.asyncio
async def test_cli_rejects_production_before_network_access() -> None:
    args = argparse.Namespace(
        installation_id=1,
        repository="owner/repo",
        pull_request=1,
        path=None,
        ref=None,
        tree_path="",
    )
    with pytest.raises(SystemExit, match="local or sandbox"):
        await run_validation(args, GitHubSettings(environment="production"))
