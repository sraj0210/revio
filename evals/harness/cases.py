"""Evaluation case schema and the lightweight types the scorer works with.

A case is a realistic diff plus ground-truth labels: either one or more expected defects, or an
empty label list meaning the diff is clean. Cases are provider- and
mode-agnostic; the runner replays each through whichever reviewer configuration is under test.
"""

from pydantic import BaseModel, ConfigDict, Field

from venaroq.domain.identifiers import InstallationRef, ProviderId, RepositoryRef
from venaroq.domain.models import ChangeRequest, ChangeRequestTarget, DiffFile

Category = str


class Label(BaseModel):
    """A ground-truth defect expected to be reported for a case."""

    model_config = ConfigDict(frozen=True)
    path: str = Field(min_length=1)
    line: int = Field(gt=0)
    category: Category | None = None
    note: str = ""


class ChangeRequestSpec(BaseModel):
    """The review-relevant pull-request metadata a case supplies (target is synthesized)."""

    model_config = ConfigDict(frozen=True)
    title: str = ""
    description: str = ""
    base_sha: str = "base"
    head_sha: str = "head"


class EvalCase(BaseModel):
    """One evaluation case: a diff, optional full-file context, and ground-truth labels."""

    model_config = ConfigDict(frozen=True)
    id: str = Field(min_length=1)
    description: str = ""
    requires_context: bool = False
    change_request: ChangeRequestSpec = ChangeRequestSpec()
    diff_files: tuple[DiffFile, ...] = ()
    # Full file contents keyed by path, used only when evaluating agent mode (the content port).
    repository_files: dict[str, str] = Field(default_factory=dict)
    labels: tuple[Label, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not self.labels

    def build_change_request(self) -> ChangeRequest:
        return ChangeRequest(
            target=_case_target(),
            title=self.change_request.title,
            description=self.change_request.description,
            base_sha=self.change_request.base_sha,
            head_sha=self.change_request.head_sha,
        )


class FindingLite(BaseModel):
    """The minimal finding projection the matcher scores (path + line + category)."""

    model_config = ConfigDict(frozen=True)
    path: str
    line: int | None = None
    category: Category | None = None


def _case_target() -> ChangeRequestTarget:
    provider = ProviderId(value="github")
    installation = InstallationRef(provider_id=provider, external_id="eval-installation")
    repository = RepositoryRef(installation=installation, external_id="eval-repository")
    return ChangeRequestTarget(repository=repository, external_number=1)
