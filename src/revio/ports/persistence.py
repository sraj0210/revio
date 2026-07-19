"""Persistence ports; no database implementation exists in Phase 1."""

from typing import Protocol


class ReviewRunRepository(Protocol):
    async def record_started(self, run_id: str) -> None: ...
    async def record_completed(self, run_id: str) -> None: ...
