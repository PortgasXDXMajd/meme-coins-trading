"""Portfolio: cash plus open positions, and the day's starting equity."""

from __future__ import annotations

from datetime import datetime

from desk.brokers.base import Broker
from desk.storage.db import Store


class Portfolio:
    def __init__(self, broker: Broker, store: Store) -> None:
        self._broker = broker
        self._store = store

    async def cash_usd(self, sol_price_usd: float) -> float:
        return await self._broker.cash_usd(sol_price_usd)

    async def equity_usd(self, sol_price_usd: float) -> float:
        positions = self._store.open_positions()
        marked = sum(p.value_at(p.last_price_usd) for p in positions)
        return await self.cash_usd(sol_price_usd) + marked

    def day_start_equity(self, now: datetime, current_equity: float) -> float:
        """Equity at the first check of the UTC day; recorded on first use."""
        key = f"day_start_equity:{now:%Y-%m-%d}"
        stored = self._store.get_float(key)
        if stored is None:
            self._store.set_float(key, current_equity)
            return current_equity
        return stored
