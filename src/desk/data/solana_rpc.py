"""Minimal Solana JSON-RPC client: balances and wallet activity."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from desk.data.http import DataError, RateLimiter, request_json

PUBLIC_RPC_URL = "https://api.mainnet-beta.solana.com"
HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/?api-key={key}"
LAMPORTS_PER_SOL = 1_000_000_000
SIGNATURE_PAGE = 1000


def rpc_url(helius_api_key: str | None) -> str:
    return HELIUS_RPC_URL.format(key=helius_api_key) if helius_api_key else PUBLIC_RPC_URL


class WalletProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    address: str
    sol_balance: float
    recent_tx_count: int  # capped at SIGNATURE_PAGE
    tx_count_is_capped: bool
    oldest_seen_tx_at: datetime | None
    newest_tx_at: datetime | None


class SolanaRpc:
    def __init__(self, client: httpx.AsyncClient, url: str = PUBLIC_RPC_URL) -> None:
        self._client = client
        self._url = url
        self._limiter = RateLimiter(0.3)

    async def _call(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        raw = await request_json(
            self._client, "POST", self._url, limiter=self._limiter, json=payload
        )
        if "error" in raw:
            raise DataError(f"RPC {method}: {raw['error']}")
        return raw["result"]

    async def sol_balance(self, address: str) -> float:
        result = await self._call("getBalance", [address])
        return result["value"] / LAMPORTS_PER_SOL

    async def token_balance_raw(self, owner: str, mint: str) -> int:
        result = await self._call(
            "getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}]
        )
        return sum(
            int(account["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            for account in result["value"]
        )

    async def wallet_profile(self, address: str) -> WalletProfile:
        """How active and how old a wallet looks, from its latest signatures."""
        signatures = await self._call(
            "getSignaturesForAddress", [address, {"limit": SIGNATURE_PAGE}]
        )
        times = [s["blockTime"] for s in signatures if s.get("blockTime")]

        def as_datetime(timestamp: int | None) -> datetime | None:
            return datetime.fromtimestamp(timestamp, tz=UTC) if timestamp else None

        return WalletProfile(
            address=address,
            sol_balance=await self.sol_balance(address),
            recent_tx_count=len(signatures),
            tx_count_is_capped=len(signatures) >= SIGNATURE_PAGE,
            oldest_seen_tx_at=as_datetime(min(times, default=None)),
            newest_tx_at=as_datetime(max(times, default=None)),
        )
