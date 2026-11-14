"""DexScreener: token discovery feeds and pair snapshots (price, mcap, liquidity)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from desk.data.http import DataError, RateLimiter, request_json
from desk.domain.models import Candidate, TxnCounts, Windowed

BASE_URL = "https://api.dexscreener.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
DISCOVERY_PATHS = (
    "/token-profiles/latest/v1",
    "/token-boosts/latest/v1",
    "/token-boosts/top/v1",
)
BATCH_SIZE = 30


def parse_pair(raw: dict[str, Any]) -> Candidate | None:
    """Turn a DexScreener pair into a Candidate, or None if essentials are missing."""
    try:
        if raw.get("chainId") != "solana" or raw["quoteToken"]["address"] != SOL_MINT:
            return None
        created_ms = raw.get("pairCreatedAt")
        market_cap = raw.get("marketCap")
        price_usd = float(raw["priceUsd"])
        price_native = float(raw["priceNative"])
        if not created_ms or not market_cap or price_usd <= 0 or price_native <= 0:
            return None
        txns = raw.get("txns") or {}
        return Candidate(
            mint=raw["baseToken"]["address"],
            symbol=(raw["baseToken"].get("symbol") or "?").strip(),
            name=(raw["baseToken"].get("name") or "?").strip(),
            pair_address=raw["pairAddress"],
            dex_id=raw.get("dexId", "unknown"),
            url=raw.get("url", ""),
            pair_created_at=datetime.fromtimestamp(created_ms / 1000, tz=UTC),
            price_usd=price_usd,
            sol_price_usd=price_usd / price_native,
            market_cap_usd=float(market_cap),
            fdv_usd=raw.get("fdv"),
            liquidity_usd=(raw.get("liquidity") or {}).get("usd"),
            volume_usd=Windowed(**(raw.get("volume") or {})),
            price_change_pct=Windowed(**(raw.get("priceChange") or {})),
            txns_m5=TxnCounts(**txns.get("m5", {})),
            txns_h1=TxnCounts(**txns.get("h1", {})),
            txns_h6=TxnCounts(**txns.get("h6", {})),
            txns_h24=TxnCounts(**txns.get("h24", {})),
        )
    except (KeyError, TypeError, ValueError):
        return None


def best_pair_per_token(pairs: list[dict[str, Any]]) -> dict[str, Candidate]:
    """Keep the deepest SOL-quoted pool for each token."""
    best: dict[str, Candidate] = {}
    for raw in pairs:
        candidate = parse_pair(raw)
        if candidate is None:
            continue
        current = best.get(candidate.mint)
        if current is None or (candidate.liquidity_usd or 0) > (current.liquidity_usd or 0):
            best[candidate.mint] = candidate
    return best


class DexScreener:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._discovery_limiter = RateLimiter(1.1)  # 60/min endpoints
        self._pairs_limiter = RateLimiter(0.25)  # 300/min endpoints

    async def discover_mints(self) -> set[str]:
        """Solana tokens that recently gained a profile or boost. Best effort."""
        mints: set[str] = set()
        for path in DISCOVERY_PATHS:
            try:
                items = await request_json(
                    self._client, "GET", BASE_URL + path, limiter=self._discovery_limiter
                )
            except DataError:
                continue
            mints.update(
                item["tokenAddress"]
                for item in items or []
                if item.get("chainId") == "solana" and item.get("tokenAddress")
            )
        return mints

    async def snapshots(self, mints: list[str]) -> dict[str, Candidate]:
        """Current best-pair snapshot for each mint that has a usable SOL pair."""
        result: dict[str, Candidate] = {}
        for start in range(0, len(mints), BATCH_SIZE):
            batch = mints[start : start + BATCH_SIZE]
            pairs = await request_json(
                self._client,
                "GET",
                f"{BASE_URL}/tokens/v1/solana/{','.join(batch)}",
                limiter=self._pairs_limiter,
            )
            result.update(best_pair_per_token(pairs or []))
        return result

    async def snapshot(self, mint: str) -> Candidate | None:
        return (await self.snapshots([mint])).get(mint)
