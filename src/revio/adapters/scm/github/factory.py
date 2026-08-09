"""GitHub adapter capability composition."""

from revio.adapters.scm.github.adapter import GitHubReadAdapter
from revio.adapters.scm.github.publishing import GitHubReviewWriter
from revio.domain.capabilities import SCMCapabilities
from revio.registries import SCMAdapterBundle


def github_adapter_bundle(
    adapter: GitHubReadAdapter, writer: GitHubReviewWriter | None = None
) -> SCMAdapterBundle:
    return SCMAdapterBundle(
        reader=adapter,
        repository_content=adapter,
        review_writer=writer,
        capabilities=SCMCapabilities(
            inline_comments=writer is not None,
            summary_comments=writer is not None,
            check_runs=writer is not None,
            repository_file_access=True,
            tree_access=True,
            webhook_event_uuids=True,
            installation_authentication=True,
        ),
    )
