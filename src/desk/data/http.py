"""Shared HTTP plumbing for the market-data adapters: pacing, retries, one error type."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import httpx

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class DataError(Exception):
    """A data source could not answer. Callers must treat the data as unknown."""


class RateLimiter:
    """Spaces calls at least `min_interval` seconds apart.

    Urgent callers (stop-loss quotes) are served before any normal caller that is
    still waiting, so they wait at most one interval. The spacing applies to both.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._changed = asyncio.Condition()
        self._next_allowed = 0.0
        self._urgent_waiting = 0

    async def wait(self, *, urgent: bool = False) -> None:
        async with self._changed:
            if urgent:
                self._urgent_waiting += 1
            try:
                while True:
                    if not urgent and self._urgent_waiting:
                        await self._changed.wait()
                        continue
                    delay = self._next_allowed - time.monotonic()
                    if delay <= 0:
                        break
                    with contextlib.suppress(TimeoutError):  # woken early or slot reached
                        await asyncio.wait_for(self._changed.wait(), delay)
                self._next_allowed = time.monotonic() + self._min_interval
            finally:
                if urgent:
                    self._urgent_waiting -= 1
                self._changed.notify_all()


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    limiter: RateLimiter | None = None,
    urgent: bool = False,
    retries: int = 2,
    **kwargs: Any,
) -> Any:
    """Send a request and return parsed JSON, retrying transient failures."""
    last_error = "no attempt made"
    for attempt in range(retries + 1):
        if limiter:
            await limiter.wait(urgent=urgent)
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code < 400:
                try:
                    return response.json()
                except ValueError as exc:
                    raise DataError(f"{url}: response was not JSON") from exc
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            if response.status_code not in RETRYABLE_STATUS:
                break
        if attempt < retries:
            await asyncio.sleep(1.5 * (attempt + 1))
    raise DataError(f"{url}: {last_error}")
