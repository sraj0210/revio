"""GitHub adapter implements only the Phase 2 read contracts."""

from typing import cast

from revio.adapters.scm.github.adapter import GitHubReadAdapter
from revio.adapters.scm.github.client import GitHubClient
from revio.ports.scm import (
    ChangeRequestReadPort,
    RepositoryContentReadPort,
)
from tests.github.test_adapter import FakeClient


def test_github_adapter_exposes_read_ports_only() -> None:
    adapter = GitHubReadAdapter(cast(GitHubClient, FakeClient([])))
    assert isinstance(adapter, ChangeRequestReadPort)
    assert isinstance(adapter, RepositoryContentReadPort)
    assert not hasattr(adapter, "publish_review")
    assert not hasattr(adapter, "set_review_status")
