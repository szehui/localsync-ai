"""Tests for the upstream rate limiter."""
import asyncio

from app.services.rate_limit import AsyncRateLimiter


class FakeClock:
    """Monotonic clock that only advances when the injected sleep is called."""

    def __init__(self, start: float = 100.0):
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


async def test_first_call_never_sleeps():
    clock = FakeClock()
    limiter = AsyncRateLimiter(1.0, clock=clock, sleep=clock.sleep)
    await limiter.wait()
    assert clock.sleeps == []


async def test_second_call_sleeps_one_interval():
    clock = FakeClock()
    limiter = AsyncRateLimiter(1.0, clock=clock, sleep=clock.sleep)
    await limiter.wait()
    await limiter.wait()
    assert clock.sleeps == [1.0]


async def test_call_after_interval_does_not_sleep():
    clock = FakeClock()
    limiter = AsyncRateLimiter(1.0, clock=clock, sleep=clock.sleep)
    await limiter.wait()
    clock.now += 5.0  # real time passed on its own
    await limiter.wait()
    assert clock.sleeps == []


async def test_concurrent_callers_are_spaced():
    clock = FakeClock()
    limiter = AsyncRateLimiter(2.0, clock=clock, sleep=clock.sleep)
    await asyncio.gather(*(limiter.wait() for _ in range(3)))
    # Two of the three had to wait, one interval each.
    assert clock.sleeps == [2.0, 2.0]


async def test_interval_is_configurable():
    clock = FakeClock()
    limiter = AsyncRateLimiter(0.25, clock=clock, sleep=clock.sleep)
    await limiter.wait()
    await limiter.wait()
    assert clock.sleeps == [0.25]
