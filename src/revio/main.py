"""ASGI entry point."""

from revio.adapters.persistence.sqlite import SQLiteStore
from revio.api.app import create_app
from revio.config.database import DatabaseSettings
from revio.config.queue import QueueSettings
from revio.config.review import ReviewSettings

app = create_app(
    database_settings=DatabaseSettings(),
    queue_settings=QueueSettings(),
    persistence=SQLiteStore(DatabaseSettings(), QueueSettings()),
    review_settings=ReviewSettings(),
)
