"""Fixed-window ingress rate limiter."""

from venaroq.api.ratelimit import FixedWindowRateLimiter


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_limit_then_rejects() -> None:
    clock = _Clock()
    limiter = FixedWindowRateLimiter(limit=3, window_seconds=60, clock=clock)
    assert [limiter.allow("1.2.3.4") for _ in range(3)] == [True, True, True]
    assert limiter.allow("1.2.3.4") is False


def test_window_resets() -> None:
    clock = _Clock()
    limiter = FixedWindowRateLimiter(limit=2, window_seconds=10, clock=clock)
    assert limiter.allow("s") and limiter.allow("s")
    assert limiter.allow("s") is False
    clock.now = 10.0
    assert limiter.allow("s") is True


def test_sources_are_independent() -> None:
    clock = _Clock()
    limiter = FixedWindowRateLimiter(limit=1, window_seconds=60, clock=clock)
    assert limiter.allow("a") is True
    assert limiter.allow("b") is True
    assert limiter.allow("a") is False


def test_memory_is_bounded() -> None:
    clock = _Clock()
    limiter = FixedWindowRateLimiter(limit=5, window_seconds=60, max_sources=4, clock=clock)
    for i in range(10):
        assert limiter.allow(f"source-{i}") is True
    # The table was cleared when the cap was exceeded; it never grows unbounded.
    assert limiter.tracked_sources <= 4
