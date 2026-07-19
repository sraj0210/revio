"""GitHub adapter capability composition."""

from revio.adapters.scm.github.adapter import GitHubReadAdapter
from revio.domain.capabilities import SCMCapabilities
from revio.registries import SCMAdapterBundle


def github_adapter_bundle(adapter: GitHubReadAdapter) -> SCMAdapterBundle:
    return SCMAdapterBundle(
        reader=adapter,
        repository_content=adapter,
        capabilities=SCMCapabilities(
            repository_file_access=True,
            tree_access=True,
            webhook_event_uuids=True,
            installation_authentication=True,
        ),
    )
