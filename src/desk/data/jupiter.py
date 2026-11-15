"""Jupiter Swap API V2: quotes, round-trip cost checks and order execution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from desk.data.dexscreener import SOL_MINT
from desk.data.http import DataError, RateLimiter, request_json
from desk.domain.models import RoundTrip

BASE_URL = "https://api.jup.ag/swap/v2"
TOKENS_URL = "https://api.jup.ag/tokens/v2"
ACTIVE_TOKEN_PATHS = ("/toporganicscore/5m?limit=100", "/toptraded/5m?limit=100")
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

    def urgent(self) -> UrgentQuotes:
        """Quotes that go ahead of queued normal requests. For exits only."""
        return UrgentQuotes(self)

    async def order(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        taker: str | None = None,
        *,
        urgent: bool = False,
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
            urgent=urgent,
            params=params,
            headers=self._headers,
        )
        return parse_order(raw)

    async def buy_quote(
        self, mint: str, lamports: int, taker: str | None = None, *, urgent: bool = False
    ) -> Order:
        return await self.order(SOL_MINT, mint, lamports, taker, urgent=urgent)

    async def sell_quote(
        self, mint: str, token_amount: int, taker: str | None = None, *, urgent: bool = False
    ) -> Order:
        return await self.order(mint, SOL_MINT, token_amount, taker, urgent=urgent)

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

    async def token_list(self, path: str) -> list[dict[str, Any]]:
        """One of the Tokens API lists, e.g. the most traded tokens of the last 5 minutes."""
        raw = await request_json(
            self._client,
            "GET",
            TOKENS_URL + path,
            limiter=self._limiter,
            headers=self._headers,
        )
        return raw if isinstance(raw, list) else []

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


def young_token_mints(
    tokens: list[dict[str, Any]], now: datetime, max_age_hours: float, max_market_cap_usd: float
) -> set[str]:
    """Mints from a Jupiter token list that could still enter the desk's window.

    The lists are mostly established tokens; only ones whose first pool is recent and
    whose market cap is not already too large are worth watching.
    """
    mints: set[str] = set()
    for token in tokens:
        try:
            created = datetime.fromisoformat(token["firstPool"]["createdAt"].replace("Z", "+00:00"))
            age_hours = (now - created).total_seconds() / 3600
            if age_hours <= max_age_hours and float(token.get("mcap") or 0) <= max_market_cap_usd:
                mints.add(token["id"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return mints


class JupiterActiveTokens:
    """Discovery feed: young tokens among Jupiter's most active of the last 5 minutes.

    Shares Jupiter's rate limit as a normal caller, so stop quotes still go first.
    """

    def __init__(
        self,
        jupiter: Jupiter,
        max_age_hours: float,
        max_market_cap_usd: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._jupiter = jupiter
        self._max_age_hours = max_age_hours
        self._max_market_cap_usd = max_market_cap_usd
        self._clock = clock

    async def discover_mints(self) -> set[str]:
        mints: set[str] = set()
        for path in ACTIVE_TOKEN_PATHS:
            try:
                tokens = await self._jupiter.token_list(path)
            except DataError:
                continue
            mints |= young_token_mints(
                tokens, self._clock(), self._max_age_hours, self._max_market_cap_usd
            )
        return mints


class UrgentQuotes:
    """A view of `Jupiter` whose quotes jump the queue: stop checks and the sells they trigger.

    It shares Jupiter's rate limit, so it never exceeds the API key's budget.
    """

    def __init__(self, jupiter: Jupiter) -> None:
        self._jupiter = jupiter

    async def buy_quote(self, mint: str, lamports: int, taker: str | None = None) -> Order:
        return await self._jupiter.buy_quote(mint, lamports, taker, urgent=True)

    async def sell_quote(self, mint: str, token_amount: int, taker: str | None = None) -> Order:
        return await self._jupiter.sell_quote(mint, token_amount, taker, urgent=True)
