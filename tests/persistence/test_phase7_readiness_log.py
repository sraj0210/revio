"""Readiness-transition observability (Phase 7)."""

import pytest

from tests.conftest import AlembicDatabase
from venaroq.adapters.observability import InMemoryQueueMetrics


@pytest.mark.asyncio
async def test_readiness_transition_is_recorded_once(alembic_database: AlembicDatabase) -> None:
    metrics = InMemoryQueueMetrics()
    store = alembic_database.store()
    store.metrics = metrics
    store._connections.metrics = metrics  # type: ignore[attr-defined]
    assert await store.check_ready() is True
    # A second call at the same state does not re-record the transition.
    assert await store.check_ready() is True
    assert metrics.counters[("readiness_change", "ready")] == 1
