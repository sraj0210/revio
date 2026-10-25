"""GitHub capability composition tests."""

from typing import cast

import pytest
from pydantic import SecretStr

from tests.github.test_adapter import FakeClient
from venaroq.adapters.scm.github.adapter import GitHubReadAdapter
from venaroq.adapters.scm.github.client import GitHubClient
from venaroq.adapters.scm.github.composition import compose_github
from venaroq.adapters.scm.github.factory import github_adapter_bundle
from venaroq.config.github import GitHubSettings


def test_bundle_declares_only_phase_2_capabilities() -> None:
    bundle = github_adapter_bundle(GitHubReadAdapter(cast(GitHubClient, FakeClient([]))))
    assert bundle.capabilities.repository_file_access
    assert bundle.capabilities.tree_access
    assert bundle.capabilities.installation_authentication
    assert bundle.publisher is None
    assert bundle.status is None


@pytest.mark.asyncio
async def test_composition_sets_all_timeout_phases(rsa_private_key_pem: str) -> None:
    composition = compose_github(
        GitHubSettings(
            github_enabled=True,
            github_app_id=1,
            github_private_key=SecretStr(rsa_private_key_pem),
            github_http_timeout_seconds=7,
        )
    )
    try:
        timeout = composition.http.timeout
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (7, 7, 7, 7)
    finally:
        await composition.close()
