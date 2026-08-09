"""Injectable time and randomness contracts."""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class RandomSource(Protocol):
    def uniform(self, lower: float, upper: float) -> float: ...
