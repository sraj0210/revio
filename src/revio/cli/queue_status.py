"""Bounded operator queue status command."""

import asyncio
import json

from revio.adapters.persistence.sqlite import SQLiteStore
from revio.config.database import DatabaseSettings


async def _status() -> None:
    store = SQLiteStore(DatabaseSettings())
    print(json.dumps(await store.status(), sort_keys=True))


def main() -> None:
    asyncio.run(_status())
