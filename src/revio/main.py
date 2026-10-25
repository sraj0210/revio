"""ASGI entry point."""

from revio.adapters.observability.prometheus import PrometheusQueueMetrics
from revio.adapters.persistence.sqlite import SQLiteStore
from revio.api.app import create_app
from revio.config.database import DatabaseSettings
from revio.config.observability import ObservabilitySettings
from revio.config.queue import QueueSettings
from revio.config.review import ReviewSettings

_observability = ObservabilitySettings()
_metrics = PrometheusQueueMetrics() if _observability.metrics_enabled else None

app = create_app(
    database_settings=DatabaseSettings(),
    queue_settings=QueueSettings(),
    persistence=SQLiteStore(DatabaseSettings(), QueueSettings(), metrics=_metrics),
    review_settings=ReviewSettings(),
)
