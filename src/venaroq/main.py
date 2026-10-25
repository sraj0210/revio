"""ASGI entry point."""

from venaroq.adapters.observability.prometheus import PrometheusQueueMetrics
from venaroq.adapters.persistence.sqlite import SQLiteStore
from venaroq.api.app import create_app
from venaroq.config.database import DatabaseSettings
from venaroq.config.observability import ObservabilitySettings
from venaroq.config.queue import QueueSettings
from venaroq.config.review import ReviewSettings

_observability = ObservabilitySettings()
_metrics = PrometheusQueueMetrics() if _observability.metrics_enabled else None

app = create_app(
    database_settings=DatabaseSettings(),
    queue_settings=QueueSettings(),
    persistence=SQLiteStore(DatabaseSettings(), QueueSettings(), metrics=_metrics),
    review_settings=ReviewSettings(),
)
