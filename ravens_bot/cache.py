from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar


K = TypeVar("K")
V = TypeVar("V")


class AsyncTtlCache(Generic[K, V]):
    """A small TTL cache that collapses concurrent misses into one fetch.

    Standings, schedules, and the roster change on the order of minutes, so a
    busy channel should not issue a request per invocation. Each key gets its own
    lock, so a burst on a cold key waits on a single in-flight call while other
    keys stay unblocked.
    """

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = 64,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._entries: dict[K, tuple[float, V]] = {}
        self._locks: dict[K, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get_or_fetch(self, key: K, factory: Callable[[], Awaitable[V]]) -> V:
        cached = self.peek(key)
        if cached is not None:
            return cached[1]

        lock = await self._lock_for(key)
        async with lock:
            # A queued caller re-checks inside the lock so the winner's result is
            # reused instead of triggering a second identical request.
            cached = self.peek(key)
            if cached is not None:
                return cached[1]

            value = await factory()
            self._store(key, value)
            return value

    def peek(self, key: K) -> tuple[float, V] | None:
        """The live entry for a key, or None when missing or expired."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if self._clock() - stored_at >= self.ttl_seconds:
            self._entries.pop(key, None)
            return None
        return stored_at, value

    def invalidate(self, key: K) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    def _store(self, key: K, value: V) -> None:
        if key not in self._entries and len(self._entries) >= self.max_entries:
            self._entries.clear()
        self._entries[key] = (self._clock(), value)

    async def _lock_for(self, key: K) -> asyncio.Lock:
        async with self._guard:
            # Locks are dropped alongside entries so a long-lived bot does not
            # accumulate one per key ever requested.
            if len(self._locks) > self.max_entries:
                self._locks = {
                    existing: lock
                    for existing, lock in self._locks.items()
                    if lock.locked()
                }
            return self._locks.setdefault(key, asyncio.Lock())
