"""Bounded operator queue status command."""

import asyncio
import json
import sys

from venaroq.adapters.persistence.sqlite import SQLiteStore
from venaroq.config.database import DatabaseSettings
from venaroq.errors import PersistenceError


async def _status() -> None:
    store = SQLiteStore(DatabaseSettings())
    print(json.dumps(await store.status(), sort_keys=True))


def main() -> None:
    try:
        asyncio.run(_status())
    except PersistenceError:
        print("queue status is unavailable", file=sys.stderr)
        raise SystemExit(1) from None
