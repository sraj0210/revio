"""Deterministic bounded retry delay calculation."""

import random

from revio.config.queue import QueueSettings


def retry_delay(
    attempt: int,
    settings: QueueSettings,
    *,
    retry_after: float | None = None,
    random_value: float | None = None,
) -> float:
    raw = min(
        settings.queue_retry_max_seconds,
        settings.queue_retry_base_seconds * (2 ** max(0, attempt - 1)),
    )
    sample = random.random() if random_value is None else random_value
    factor = 1 - settings.queue_retry_jitter_ratio + 2 * settings.queue_retry_jitter_ratio * sample
    provider_delay = min(max(0.0, retry_after or 0.0), 900.0)
    return max(raw * factor, provider_delay)
