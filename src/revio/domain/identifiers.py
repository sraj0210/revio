"""Validated, provider-neutral identity value objects."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, max_length=255)]


class ProviderId(BaseModel):
    """Extensible registry identifier; deliberately not a closed enum."""

    model_config = ConfigDict(frozen=True)
    value: Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=64)]

    def __str__(self) -> str:
        return self.value


class ModelAlias(BaseModel):
    model_config = ConfigDict(frozen=True)
    value: Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$", max_length=64)]


class InstallationRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider_id: ProviderId
    external_id: Identifier


class RepositoryRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    installation: InstallationRef
    external_id: Identifier


class ChangeRequestTarget(BaseModel):
    model_config = ConfigDict(frozen=True)
    repository: RepositoryRef
    external_number: Annotated[int, Field(gt=0)]
