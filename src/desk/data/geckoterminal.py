"""GeckoTerminal: trending and newly created Solana pools, used only for discovery."""

from __future__ import annotations

from typing import Any

import httpx

from desk.data.http import DataError, RateLimiter, request_json

BASE_URL = "https://api.geckoterminal.com/api/v2/networks/solana"
DISCOVERY_PATHS = ("/trending_pools?page=1", "/trending_pools?page=2", "/new_pools?page=1")
TOKEN_ID_PREFIX = "solana_"


def base_token_mints(payload: dict[str, Any]) -> set[str]:
    mints: set[str] = set()
    for pool in payload.get("data") or []:
        token_id = (
            pool.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "")
        )
        if token_id.startswith(TOKEN_ID_PREFIX):
            mints.add(token_id.removeprefix(TOKEN_ID_PREFIX))
    return mints


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
