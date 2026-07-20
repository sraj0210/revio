"""Provider-neutral terminal-history retention coordination."""

import uuid
from datetime import UTC, datetime, timedelta

from revio.domain.queue import RetentionResult
from revio.ports.persistence import TerminalRetentionPort


async def retain_terminal_history(
    retention: TerminalRetentionPort, *, age_days: int, batch_size: int, dry_run: bool
) -> RetentionResult:
    """Coordinate one bounded operator-requested retention batch."""
    cutoff = datetime.now(UTC) - timedelta(days=age_days)
    return await retention.retain_terminal_history(
        cutoff=cutoff,
        batch_size=batch_size,
        dry_run=dry_run,
        correlation_id=uuid.uuid4().hex,
    )
