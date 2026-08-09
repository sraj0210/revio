"""Bounded exponential retry behavior."""

import math

from revio.application.queue.retry import retry_delay
from revio.config.queue import QueueSettings


def test_retry_delay_applies_jitter_cap_and_bounded_retry_after() -> None:
    settings = QueueSettings(
        queue_retry_base_seconds=5,
        queue_retry_max_seconds=20,
        queue_retry_jitter_ratio=0.2,
    )
    assert retry_delay(1, settings, random_value=0) == 4
    assert math.isclose(retry_delay(3, settings, random_value=1), 24)
    assert retry_delay(9, settings, random_value=0.5) == 20
    assert retry_delay(1, settings, retry_after=901, random_value=0.5) == 900
