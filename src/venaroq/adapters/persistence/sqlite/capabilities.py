"""Mandatory SQLite runtime capabilities."""

import sqlite3

MINIMUM_SQLITE_VERSION = (3, 35, 0)


def require_supported_sqlite() -> None:
    """Reject runtimes that cannot perform atomic leasing with RETURNING."""
    if sqlite3.sqlite_version_info < MINIMUM_SQLITE_VERSION:
        required = ".".join(str(part) for part in MINIMUM_SQLITE_VERSION)
        raise RuntimeError(f"SQLite {required} or newer is required")
