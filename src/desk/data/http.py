"""Shared HTTP plumbing for the market-data adapters: pacing, retries, one error type."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class DataError(Exception):
    """A data source could not answer. Callers must treat the data as unknown."""


class RateLimiter:
    """Spaces calls at least `min_interval` seconds apart."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next_allowed - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_allowed = time.monotonic() + self._min_interval


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    limiter: RateLimiter | None = None,
    retries: int = 2,
    **kwargs: Any,
) -> Any:
    """Send a request and return parsed JSON, retrying transient failures."""
    last_error = "no attempt made"
    for attempt in range(retries + 1):
        if limiter:
            await limiter.wait()
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
