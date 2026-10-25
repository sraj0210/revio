"""GitHub adapter capability composition."""

from venaroq.adapters.scm.github.adapter import GitHubReadAdapter
from venaroq.adapters.scm.github.publishing import GitHubReviewWriter
from venaroq.adapters.scm.github.threads import GitHubThreadReader, GitHubThreadResolver
from venaroq.domain.capabilities import SCMCapabilities
from venaroq.registries import SCMAdapterBundle


def github_adapter_bundle(
    adapter: GitHubReadAdapter,
    writer: GitHubReviewWriter | None = None,
    thread_reader: GitHubThreadReader | None = None,
    thread_resolver: GitHubThreadResolver | None = None,
) -> SCMAdapterBundle:
    resolvable = thread_reader is not None and thread_resolver is not None
    return SCMAdapterBundle(
        reader=adapter,
        repository_content=adapter,
        review_writer=writer,
        thread_reader=thread_reader,
        thread_resolver=thread_resolver,
        capabilities=SCMCapabilities(
            inline_comments=writer is not None,
            summary_comments=writer is not None,
            check_runs=writer is not None,
            resolvable_threads=resolvable,
            repository_file_access=True,
            tree_access=True,
            webhook_event_uuids=True,
            installation_authentication=True,
        ),
    )
