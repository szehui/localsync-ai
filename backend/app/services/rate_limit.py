"""Spacing for upstream APIs that enforce a sustained request rate.

MusicBrainz allows roughly one request per second sustained and answers 503 to
bursts. The Cover Art Archive and ListenBrainz sit behind the same operators and
are happiest with the same shape. Doing the spacing client-side is far cheaper
than discovering the limit in production, where a burst shows up as a confusing
"MusicBrainz is busy" for a user who searched once.

The limiter is deliberately dumb: one timestamp, one lock. It does not know
about concurrency limits or queues, because every caller here is a serial
pipeline of lookups.
"""
import asyncio
import time


class AsyncRateLimiter:
    """Space calls at least ``min_interval`` seconds apart, sharing one lock.

    The clock and sleep are injectable so tests can assert the spacing without
    actually waiting a second per call.
    """

    def __init__(self, min_interval: float = 1.0, *, clock=time.monotonic,
                 sleep=asyncio.sleep):
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        """Block until this caller may proceed, then reserve the next slot.

        The timestamp is advanced *before* the sleep returns, so two callers
        queued behind the same lock wake up one interval apart rather than
        together.
        """
        async with self._lock:
            now = self._clock()
            if now < self._next_at:
                await self._sleep(self._next_at - now)
                now = self._clock()
            self._next_at = now + self._min_interval

    @property
    def min_interval(self) -> float:
        return self._min_interval
