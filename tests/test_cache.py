from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from ravens_bot.cache import AsyncTtlCache


T = TypeVar("T")


def async_test(func: Callable[..., Awaitable[T]]) -> Callable[..., T]:
    """Run a coroutine test on its own loop, so pytest-asyncio isn't needed."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        return asyncio.run(func(*args, **kwargs))

    return wrapper


class FakeClock:
    """A hand-cranked clock, so TTL tests never wait on real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@async_test
async def test_second_call_is_served_from_the_cache() -> None:
    calls = 0

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        return "standings"

    cache: AsyncTtlCache[str, str] = AsyncTtlCache(ttl_seconds=60, clock=FakeClock())

    assert await cache.get_or_fetch("afc-north", fetch) == "standings"
    assert await cache.get_or_fetch("afc-north", fetch) == "standings"
    assert calls == 1


@async_test
async def test_expired_entry_is_refetched() -> None:
    calls = 0

    async def fetch() -> int:
        nonlocal calls
        calls += 1
        return calls

    clock = FakeClock()
    cache: AsyncTtlCache[str, int] = AsyncTtlCache(ttl_seconds=60, clock=clock)

    assert await cache.get_or_fetch("key", fetch) == 1
    clock.advance(59)
    assert await cache.get_or_fetch("key", fetch) == 1
    clock.advance(1)
    assert await cache.get_or_fetch("key", fetch) == 2


@async_test
async def test_concurrent_misses_collapse_into_one_request() -> None:
    """A burst of commands on a cold key should hit ESPN once, not five times."""
    calls = 0
    release = asyncio.Event()

    async def fetch() -> str:
        nonlocal calls
        calls += 1
        await release.wait()
        return "value"

    cache: AsyncTtlCache[str, str] = AsyncTtlCache(ttl_seconds=60, clock=FakeClock())
    waiters = [asyncio.create_task(cache.get_or_fetch("key", fetch)) for _ in range(5)]
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(*waiters) == ["value"] * 5
    assert calls == 1


@async_test
async def test_separate_keys_do_not_block_each_other() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> str:
        started.set()
        await release.wait()
        return "slow"

    async def quick() -> str:
        return "quick"

    cache: AsyncTtlCache[str, str] = AsyncTtlCache(ttl_seconds=60, clock=FakeClock())
    blocked = asyncio.create_task(cache.get_or_fetch("slow", slow))
    await started.wait()

    assert await cache.get_or_fetch("quick", quick) == "quick"

    release.set()
    assert await blocked == "slow"


@async_test
async def test_invalidate_forces_the_next_call_to_refetch() -> None:
    calls = 0

    async def fetch() -> int:
        nonlocal calls
        calls += 1
        return calls

    cache: AsyncTtlCache[str, int] = AsyncTtlCache(ttl_seconds=60, clock=FakeClock())

    assert await cache.get_or_fetch("key", fetch) == 1
    cache.invalidate("key")
    assert await cache.get_or_fetch("key", fetch) == 2


@async_test
async def test_cache_does_not_grow_past_its_limit() -> None:
    async def fetch() -> str:
        return "value"

    cache: AsyncTtlCache[int, str] = AsyncTtlCache(
        ttl_seconds=60, max_entries=4, clock=FakeClock()
    )
    for key in range(10):
        await cache.get_or_fetch(key, fetch)
        assert cache.peek(key) is not None

    assert len(cache._entries) <= 4


def test_peek_reports_a_missing_key() -> None:
    cache: AsyncTtlCache[str, str] = AsyncTtlCache(ttl_seconds=60, clock=FakeClock())

    assert cache.peek("absent") is None
