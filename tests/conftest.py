"""Shared test fixtures, including production-migrated SQLite databases."""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from revio.adapters.persistence.sqlite.connection import CoordinationHook, FailureHook
from revio.adapters.persistence.sqlite.schema import SCHEMA_REVISION
from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.config.database import DatabaseSettings
from revio.config.queue import QueueSettings
from revio.domain.capabilities import ResolvedModelProfile
from revio.domain.identifiers import (
    ChangeRequestTarget,
    InstallationRef,
    ModelAlias,
    ProviderId,
    RepositoryRef,
)
from revio.domain.models import ChangeRequest


@dataclass(frozen=True)
class AlembicDatabase:
    """A file-backed database created only through the production migration."""

    path: Path

    def store(
        self,
        queue: QueueSettings | None = None,
        *,
        failure_hook: FailureHook | None = None,
        coordination_hook: CoordinationHook | None = None,
    ) -> SQLiteStore:
        return SQLiteStore(
            DatabaseSettings(database_path=self.path),
            queue,
            failure_hook=failure_hook,
            coordination_hook=coordination_hook,
        )


@pytest.fixture
def alembic_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AlembicDatabase]:
    """Apply Alembic and verify the runtime revision and connection policy."""
    path = tmp_path / "alembic-runtime.db"
    monkeypatch.setenv("REVIO_DATABASE_PATH", str(path))
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")

    connection = sqlite3.connect(path)
    try:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert revision == (SCHEMA_REVISION,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        connection.close()
    yield AlembicDatabase(path)


@pytest.fixture
def target() -> ChangeRequestTarget:
    provider = ProviderId(value="example-scm")
    installation = InstallationRef(provider_id=provider, external_id="installation-1")
    repository = RepositoryRef(installation=installation, external_id="repository-1")
    return ChangeRequestTarget(repository=repository, external_number=7)


@pytest.fixture
def change_request(target: ChangeRequestTarget) -> ChangeRequest:
    return ChangeRequest(target=target, title="Change", base_sha="base", head_sha="head")


@pytest.fixture
def model_profile() -> ResolvedModelProfile:
    return ResolvedModelProfile(
        alias=ModelAlias(value="review-default"),
        provider_id=ProviderId(value="example-ai"),
        provider_model_id="model-1",
        context_tokens=100_000,
        max_output_tokens=4_000,
        structured_output="json_schema",
    )


@pytest.fixture(scope="session")
def rsa_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
