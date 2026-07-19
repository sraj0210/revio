"""GitHub capability composition tests."""

from typing import cast

from revio.adapters.scm.github.adapter import GitHubReadAdapter
from revio.adapters.scm.github.client import GitHubClient
from revio.adapters.scm.github.factory import github_adapter_bundle
from tests.github.test_adapter import FakeClient


def test_bundle_declares_only_phase_2_capabilities() -> None:
    bundle = github_adapter_bundle(GitHubReadAdapter(cast(GitHubClient, FakeClient([]))))
    assert bundle.capabilities.repository_file_access
    assert bundle.capabilities.tree_access
    assert bundle.capabilities.installation_authentication
    assert bundle.publisher is None
    assert bundle.status is None
