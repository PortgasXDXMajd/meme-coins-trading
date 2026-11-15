"""GeckoTerminal: trending and new Solana pools for discovery, and one-minute candles."""

from __future__ import annotations

from typing import Any

import httpx

from desk.data.http import DataError, RateLimiter, request_json

BASE_URL = "https://api.geckoterminal.com/api/v2/networks/solana"
# The short trending windows surface a pool while it is moving; the default 24-hour
# list mostly shows moves that are already over. Five calls a minute stays well
# inside the free tier.
DISCOVERY_PATHS = (
    "/trending_pools?page=1&duration=5m",
    "/trending_pools?page=1&duration=1h",
    "/trending_pools?page=1",
    "/new_pools?page=1",
    "/new_pools?page=2",
)
TOKEN_ID_PREFIX = "solana_"


def base_token_mints(payload: dict[str, Any]) -> set[str]:
    mints: set[str] = set()
    for pool in payload.get("data") or []:
        token_id = pool.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
        if token_id.startswith(TOKEN_ID_PREFIX):
            mints.add(token_id.removeprefix(TOKEN_ID_PREFIX))
    return mints


def candle_ranges_pct(payload: dict[str, Any]) -> list[float]:
    """(high - low) / open per candle, in %, from an OHLCV response."""
    rows = ((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    ranges = []
    for row in rows:
        try:
            _, open_, high, low = (float(v) for v in row[:4])
        except (TypeError, ValueError):
            continue
        if open_ > 0 and high >= low:
            ranges.append((high - low) / open_ * 100)
    return ranges


class GeckoTerminal:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._limiter = RateLimiter(2.5)  # free tier: 30 calls/min

    async def discover_mints(self) -> set[str]:
        mints: set[str] = set()
        for path in DISCOVERY_PATHS:
            try:
                payload = await request_json(
                    self._client, "GET", BASE_URL + path, limiter=self._limiter
                )
            except DataError:
                continue
            mints |= base_token_mints(payload)
        return mints

    async def minute_ranges_pct(self, pool_address: str, count: int) -> list[float]:
        payload = await request_json(
            self._client,
            "GET",
            f"{BASE_URL}/pools/{pool_address}/ohlcv/minute?aggregate=1&limit={count}",
            limiter=self._limiter,
        )
        return candle_ranges_pct(payload)
