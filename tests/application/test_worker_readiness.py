"""Worker readiness validates complete network-free composition."""

import sys
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError

from revio.adapters.persistence.sqlite import SQLiteStore
from revio.config.database import DatabaseSettings
from revio.worker.main import main, run_worker


async def _database(path: Path) -> None:
    await SQLiteStore(DatabaseSettings(database_path=path)).initialize()


def _configure_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("REVIO_DATABASE_PATH", str(path))
    monkeypatch.delenv("REVIO_GITHUB_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("REVIO_GITHUB_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.delenv("REVIO_GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("REVIO_GITHUB_ENABLED", raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_app_id", "missing_key", "two_keys"])
async def test_worker_readiness_rejects_incomplete_github_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
    case: str,
) -> None:
    path = tmp_path / "revio.db"
    await _database(path)
    _configure_database(monkeypatch, path)
    monkeypatch.setenv("REVIO_GITHUB_ENABLED", "true")
    if case != "missing_app_id":
        monkeypatch.setenv("REVIO_GITHUB_APP_ID", "1")
    if case != "missing_key":
        monkeypatch.setenv("REVIO_GITHUB_PRIVATE_KEY", rsa_private_key_pem)
    if case == "two_keys":
        key_path = tmp_path / "private.pem"
        key_path.write_text(rsa_private_key_pem)
        monkeypatch.setenv("REVIO_GITHUB_PRIVATE_KEY_FILE", str(key_path))
    with pytest.raises(ValidationError):
        await run_worker(True)


@pytest.mark.asyncio
@pytest.mark.parametrize("key_kind", ["invalid", "non_rsa"])
async def test_worker_readiness_rejects_invalid_key_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_kind: str
) -> None:
    path = tmp_path / "revio.db"
    await _database(path)
    _configure_database(monkeypatch, path)
    monkeypatch.setenv("REVIO_GITHUB_ENABLED", "true")
    monkeypatch.setenv("REVIO_GITHUB_APP_ID", "1")
    if key_kind == "invalid":
        value = "not a private key"
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        value = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
    monkeypatch.setenv("REVIO_GITHUB_PRIVATE_KEY", value)
    with pytest.raises(Exception, match="GitHub private key"):
        await run_worker(True)


@pytest.mark.asyncio
async def test_valid_worker_readiness_is_network_free_and_does_not_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
) -> None:
    path = tmp_path / "revio.db"
    await _database(path)
    _configure_database(monkeypatch, path)
    monkeypatch.setenv("REVIO_GITHUB_ENABLED", "true")
    monkeypatch.setenv("REVIO_GITHUB_APP_ID", "1")
    monkeypatch.setenv("REVIO_GITHUB_PRIVATE_KEY", rsa_private_key_pem)

    async def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("readiness must not perform network or lease operations")

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(SQLiteStore, "lease_next", forbidden)
    assert await run_worker(True) == 0


@pytest.mark.asyncio
async def test_disabled_worker_mode_is_an_explicit_idle_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "revio.db"
    await _database(path)
    _configure_database(monkeypatch, path)
    assert await run_worker(True) == 0


def test_worker_cli_reports_generic_invalid_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure_database(monkeypatch, tmp_path / "revio.db")
    monkeypatch.setenv("REVIO_GITHUB_ENABLED", "true")
    monkeypatch.setattr(sys, "argv", ["revio-worker", "check-ready"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "worker is not ready\n"
    assert str(tmp_path) not in captured.err
