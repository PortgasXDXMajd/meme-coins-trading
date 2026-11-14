"""Jupiter Swap API V2: quotes, round-trip cost checks and order execution."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from desk.data.dexscreener import SOL_MINT
from desk.data.http import DataError, RateLimiter, request_json
from desk.domain.models import RoundTrip

BASE_URL = "https://api.jup.ag/swap/v2"
LAMPORTS_PER_SOL = 1_000_000_000
KEYLESS_INTERVAL_SECONDS = 2.1  # 0.5 requests/second without a key
KEYED_INTERVAL_SECONDS = 1.05  # 1 request/second on the free key


class Order(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    in_usd: float
    out_usd: float
    price_impact_pct: float | None = None
    request_id: str = ""
    transaction: str | None = None  # base64, present only when a taker was given


class Execution(BaseModel):
    model_config = ConfigDict(frozen=True)

    signature: str
    total_input_amount: int
    total_output_amount: int


def parse_order(raw: dict[str, Any]) -> Order:
    if raw.get("error") or raw.get("errorCode"):
        raise DataError(f"Jupiter order failed: {raw.get('error') or raw.get('errorCode')}")
    try:
        out_amount = int(raw["outAmount"])
        if out_amount <= 0:
            raise DataError("Jupiter returned an empty route")
        return Order(
            input_mint=raw["inputMint"],
            output_mint=raw["outputMint"],
            in_amount=int(raw["inAmount"]),
            out_amount=out_amount,
            in_usd=float(raw["inUsdValue"]),
            out_usd=float(raw["outUsdValue"]),
            price_impact_pct=raw.get("priceImpact"),
            request_id=raw.get("requestId", ""),
            transaction=raw.get("transaction") or None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError(f"Jupiter order had an unexpected shape: {exc}") from exc


def usd_to_lamports(usd: float, sol_price_usd: float) -> int:
    return int(usd / sol_price_usd * LAMPORTS_PER_SOL)


class Jupiter:
    def __init__(self, client: httpx.AsyncClient, api_key: str | None = None) -> None:
        self._client = client
        self._headers = {"x-api-key": api_key} if api_key else {}
        interval = KEYED_INTERVAL_SECONDS if api_key else KEYLESS_INTERVAL_SECONDS
        self._limiter = RateLimiter(interval)

    async def order(
        self, input_mint: str, output_mint: str, amount: int, taker: str | None = None
    ) -> Order:
        """Quote a swap. With `taker`, the response also carries a transaction to sign."""
        params: dict[str, str | int] = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount,
        }
        if taker:
            params["taker"] = taker
        raw = await request_json(
            self._client,
            "GET",
            f"{BASE_URL}/order",
            limiter=self._limiter,
            params=params,
            headers=self._headers,
        )
        return parse_order(raw)

    async def buy_quote(self, mint: str, lamports: int, taker: str | None = None) -> Order:
        return await self.order(SOL_MINT, mint, lamports, taker)

    async def sell_quote(self, mint: str, token_amount: int, taker: str | None = None) -> Order:
        return await self.order(mint, SOL_MINT, token_amount, taker)

    async def round_trip(self, mint: str, size_usd: float, sol_price_usd: float) -> RoundTrip:
        """Quote a buy then selling it straight back. Proves an exit route exists."""
        lamports = usd_to_lamports(size_usd, sol_price_usd)
        buy = await self.buy_quote(mint, lamports)
        sell = await self.sell_quote(mint, buy.out_amount)
        return RoundTrip(
            size_usd=size_usd,
            cost_pct=100 * (1 - sell.out_amount / buy.in_amount),
            buy_price_impact_pct=buy.price_impact_pct,
            sell_price_impact_pct=sell.price_impact_pct,
        )

    async def execute(self, signed_transaction_b64: str, request_id: str) -> Execution:
        raw = await request_json(
            self._client,
            "POST",
            f"{BASE_URL}/execute",
            retries=0,  # never blindly resend a signed swap
            json={"signedTransaction": signed_transaction_b64, "requestId": request_id},
            headers=self._headers,
        )
        if raw.get("status") != "Success":
            raise DataError(
                f"Jupiter execute failed: code={raw.get('code')} error={raw.get('error')} "
                f"signature={raw.get('signature')}"
            )
        return Execution(
            signature=raw["signature"],
            total_input_amount=int(raw["totalInputAmount"]),
            total_output_amount=int(raw["totalOutputAmount"]),
        )
