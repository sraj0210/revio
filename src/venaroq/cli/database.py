"""Exclusive database migration and retention commands."""

import argparse
import asyncio
import json
import sys

from alembic import command
from alembic.config import Config

from venaroq.adapters.persistence.sqlite import SQLiteStore
from venaroq.adapters.persistence.sqlite.locks import MaintenanceLock
from venaroq.application.retention import retain_closed_findings, retain_terminal_history
from venaroq.config.database import DatabaseSettings
from venaroq.errors import PersistenceUnavailableError, RetentionIntegrityError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="venaroq-db")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("upgrade")
    subparsers.add_parser("current")
    subparsers.add_parser("check")
    retention = subparsers.add_parser("prune-terminal")
    mode = retention.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    retention.add_argument("--backup-confirmed", action="store_true")
    findings = subparsers.add_parser("prune-findings")
    findings_mode = findings.add_mutually_exclusive_group(required=True)
    findings_mode.add_argument("--dry-run", action="store_true")
    findings_mode.add_argument("--execute", action="store_true")
    findings.add_argument("--backup-confirmed", action="store_true")
    return parser


async def _retain(settings: DatabaseSettings, dry_run: bool) -> None:
    store = SQLiteStore(settings)
    result = await retain_terminal_history(
        store,
        age_days=settings.retention_terminal_age_days,
        batch_size=settings.retention_batch_size,
        dry_run=dry_run,
    )
    print(
        json.dumps(
            {"jobs": result.jobs, "attempts": result.attempts, "deliveries": result.deliveries}
        )
    )


async def _retain_findings(settings: DatabaseSettings, dry_run: bool) -> None:
    store = SQLiteStore(settings)
    result = await retain_closed_findings(
        store,
        age_days=settings.retention_closed_finding_age_days,
        batch_size=settings.retention_batch_size,
        dry_run=dry_run,
    )
    print(
        json.dumps(
            {
                "findings": result.findings,
                "occurrences": result.occurrences,
                "links": result.links,
                "operations": result.operations,
                "results": result.results,
            }
        )
    )


async def _check(settings: DatabaseSettings) -> int:
    return 0 if await SQLiteStore(settings).check_ready() else 1


async def _checkpoint(settings: DatabaseSettings) -> None:
    store = SQLiteStore(settings)
    async with store.connect() as connection:
        result = list(await connection.execute_fetchall("PRAGMA wal_checkpoint(TRUNCATE)"))
        if not result or result[0][0] != 0:
            raise RuntimeError("WAL checkpoint did not complete")


def main() -> None:
    args = _parser().parse_args()
    settings = DatabaseSettings()
    directory_existed = settings.database_path.parent.exists()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    if not directory_existed:
        settings.database_path.parent.chmod(0o700)
    lock = MaintenanceLock(
        settings.database_path.with_name("venaroq.maintenance.lock"), exclusive=True
    )
    with lock:
        if args.command == "upgrade":
            command.upgrade(Config("alembic.ini"), "head")
            settings.database_path.chmod(0o600)
        elif args.command == "current":
            command.current(Config("alembic.ini"), verbose=True)
        elif args.command == "check":
            raise SystemExit(asyncio.run(_check(settings)))
        elif args.command == "prune-findings":
            if args.execute and not args.backup_confirmed:
                raise SystemExit("--backup-confirmed is required for execution")
            try:
                if args.execute:
                    asyncio.run(_checkpoint(settings))
                asyncio.run(_retain_findings(settings, args.dry_run))
            except (PersistenceUnavailableError, RetentionIntegrityError):
                print("closed-finding retention failed safely", file=sys.stderr)
                raise SystemExit(1) from None
        else:
            if args.execute and not args.backup_confirmed:
                raise SystemExit("--backup-confirmed is required for execution")
            try:
                if args.execute:
                    asyncio.run(_checkpoint(settings))
                asyncio.run(_retain(settings, args.dry_run))
            except (PersistenceUnavailableError, RetentionIntegrityError):
                print("terminal retention failed safely", file=sys.stderr)
                raise SystemExit(1) from None
