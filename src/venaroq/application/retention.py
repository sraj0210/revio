"""Provider-neutral terminal-history retention coordination."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from venaroq.domain.lifecycle import FindingRetentionResult
from venaroq.domain.queue import RetentionResult
from venaroq.ports.persistence import TerminalRetentionPort


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


class FindingRetentionPort(Protocol):
    async def retain_closed_findings(
        self, *, cutoff: datetime, batch_size: int, dry_run: bool, correlation_id: str
    ) -> FindingRetentionResult: ...


async def retain_closed_findings(
    retention: FindingRetentionPort, *, age_days: int, batch_size: int, dry_run: bool
) -> FindingRetentionResult:
    """Coordinate one bounded operator-requested closed-finding pruning batch."""
    cutoff = datetime.now(UTC) - timedelta(days=age_days)
    return await retention.retain_closed_findings(
        cutoff=cutoff,
        batch_size=batch_size,
        dry_run=dry_run,
        correlation_id=uuid.uuid4().hex,
    )
