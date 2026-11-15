"""The narrow interfaces the agents depend on.

Real adapters and test fakes both satisfy them, so neither needs to know the other.
"""

from __future__ import annotations

from typing import Protocol

from desk.data.jupiter import Order
from desk.domain.models import Candidate, FilterVerdict


class PriceFeed(Protocol):
    """Current market snapshots (DexScreener in production)."""

    async def snapshot(self, mint: str) -> Candidate | None: ...

    async def snapshots(self, mints: list[str]) -> dict[str, Candidate]: ...


class QuoteSource(Protocol):
    """Executable swap quotes (Jupiter in production)."""

    async def buy_quote(self, mint: str, lamports: int, taker: str | None = None) -> Order: ...

    async def sell_quote(self, mint: str, token_amount: int, taker: str | None = None) -> Order: ...


class CandleSource(Protocol):
    """Recent one-minute candles (GeckoTerminal in production)."""

    async def minute_ranges_pct(self, pool_address: str, count: int) -> list[float]:
        """High-to-low range of each of the last `count` one-minute candles, in %."""
        ...


class TokenScout(Protocol):
    async def scan(self) -> list[Candidate]: ...


class TokenFilter(Protocol):
    async def evaluate(self, candidate: Candidate) -> FilterVerdict: ...
