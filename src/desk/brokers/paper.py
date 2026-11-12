"""Paper broker: fills at live Jupiter quotes, so fees and price impact are real."""

from __future__ import annotations

import itertools

from desk.brokers.base import BrokerError
from desk.config import Settings
from desk.data.http import DataError
from desk.data.jupiter import Jupiter, usd_to_lamports
from desk.domain.models import Fill
from desk.storage.db import Store

CASH_KEY = "paper_cash_usd"


class PaperBroker:
    def __init__(self, jupiter: Jupiter, store: Store, settings: Settings) -> None:
        self._jupiter = jupiter
        self._store = store
        self._network_fee_usd = settings.paper_network_fee_usd
        self._ids = itertools.count(1)
        if store.get_float(CASH_KEY) is None:
            store.set_float(CASH_KEY, settings.paper_starting_usd)

    async def cash_usd(self, sol_price_usd: float) -> float:
        return self._store.get_float(CASH_KEY) or 0.0

    async def buy(self, mint: str, usd: float, sol_price_usd: float) -> Fill:
        cash = await self.cash_usd(sol_price_usd)
        if usd + self._network_fee_usd > cash:
            raise BrokerError(f"insufficient paper cash: need ${usd:.2f}, have ${cash:.2f}")
        try:
            order = await self._jupiter.buy_quote(mint, usd_to_lamports(usd, sol_price_usd))
        except DataError as exc:
            raise BrokerError(f"no buy quote: {exc}") from exc
        cost = order.in_usd + self._network_fee_usd
        self._store.set_float(CASH_KEY, cash - cost)
        return Fill(
            side="buy",
            mint=mint,
            token_amount_raw=order.out_amount,
            usd_value=cost,
            tx_id=f"paper-{next(self._ids)}-{order.request_id}",
            simulated=True,
        )

    async def sell(self, mint: str, token_amount_raw: int, sol_price_usd: float) -> Fill:
        try:
            order = await self._jupiter.sell_quote(mint, token_amount_raw)
        except DataError as exc:
            raise BrokerError(f"no sell quote: {exc}") from exc
        proceeds = max(0.0, order.out_usd - self._network_fee_usd)
        self._store.set_float(CASH_KEY, await self.cash_usd(sol_price_usd) + proceeds)
        return Fill(
            side="sell",
            mint=mint,
            token_amount_raw=token_amount_raw,
            usd_value=proceeds,
            tx_id=f"paper-{next(self._ids)}-{order.request_id}",
            simulated=True,
        )
