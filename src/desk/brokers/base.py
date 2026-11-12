"""The Executor trades through this interface, so paper and live are interchangeable."""

from __future__ import annotations

from typing import Protocol

from desk.domain.models import Fill


class BrokerError(Exception):
    """A swap could not be completed. No position change should be assumed."""


class Broker(Protocol):
    async def cash_usd(self, sol_price_usd: float) -> float:
        """Spendable balance, valued in USD."""
        ...

    async def buy(self, mint: str, usd: float, sol_price_usd: float) -> Fill:
        """Swap `usd` worth of SOL into the token."""
        ...

    async def sell(self, mint: str, token_amount_raw: int, sol_price_usd: float) -> Fill:
        """Swap the given raw token amount back to SOL."""
        ...
