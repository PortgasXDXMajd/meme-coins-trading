"""Executor: places the trade. It will not buy without Risk's TradeApproval."""

from __future__ import annotations

import logging

from desk.brokers.base import Broker, BrokerError
from desk.config import Settings
from desk.data.http import DataError
from desk.domain.models import ExitReason, Position, TradeApproval, utcnow
from desk.domain.rules import (
    entry_drift_pct,
    initial_stop_price,
    liquidity_kills,
    position_size_usd,
)
from desk.portfolio import Portfolio
from desk.ports import PriceFeed
from desk.storage.db import Store
from desk.storage.debate_log import EventLog

log = logging.getLogger(__name__)


class Executor:
    def __init__(
        self,
        broker: Broker,
        dex: PriceFeed,
        portfolio: Portfolio,
        store: Store,
        settings: Settings,
        events: EventLog,
    ) -> None:
        self._broker = broker
        self._dex = dex
        self._portfolio = portfolio
        self._store = store
        self._settings = settings
        self._events = events

    async def buy(self, approval: TradeApproval, debate_id: int | None = None) -> Position | None:
        """Buy up to 8% of equity. Returns None when the trade is skipped."""
        if not isinstance(approval, TradeApproval):
            raise TypeError("Executor.buy requires a TradeApproval from Risk")
        approved = approval.candidate

        # The debate took minutes; make sure the market is still the one that was debated.
        try:
            fresh = await self._dex.snapshot(approved.mint)
        except DataError as exc:
            return self._skip(approved.symbol, f"no fresh price: {exc}")
        if fresh is None:
            return self._skip(approved.symbol, "pair no longer returns data")
        drift = entry_drift_pct(approved.price_usd, fresh.price_usd)
        if drift > self._settings.max_entry_drift_pct:
            return self._skip(approved.symbol, f"price moved {drift:.1f}% during the debate")
        if liquidity_kills(fresh, self._settings):
            return self._skip(approved.symbol, "liquidity fell below the minimum")

        equity = await self._portfolio.equity_usd(fresh.sol_price_usd)
        cash = await self._portfolio.cash_usd(fresh.sol_price_usd)
        size = min(approval.max_size_usd, position_size_usd(equity, cash, self._settings))
        if size < self._settings.min_trade_usd:
            return self._skip(approved.symbol, f"size ${size:.2f} below minimum trade")

        try:
            fill = await self._broker.buy(approved.mint, size, fresh.sol_price_usd)
        except BrokerError as exc:
            return self._skip(approved.symbol, f"buy failed: {exc}")

        entry = fresh.price_usd
        position = self._store.insert_position(
            Position(
                mint=approved.mint,
                symbol=approved.symbol,
                pair_address=fresh.pair_address,
                token_amount_raw=fill.token_amount_raw,
                cost_usd=fill.usd_value,
                entry_price_usd=entry,
                stop_price_usd=initial_stop_price(entry, self._settings),
                high_water_price_usd=entry,
                last_price_usd=entry,
                debate_id=debate_id,
            )
        )
        log.info(
            "BUY %s $%.2f at $%.10g, stop $%.10g (%s)",
            position.symbol,
            position.cost_usd,
            entry,
            position.stop_price_usd,
            "paper" if fill.simulated else "LIVE",
        )
        self._events.emit("buy", position=position.model_dump(mode="json"), tx_id=fill.tx_id)
        return position

    async def sell(
        self, position: Position, price_usd: float, sol_price_usd: float, reason: ExitReason
    ) -> Position:
        """Sell the whole position. Raises BrokerError if the swap fails."""
        fill = await self._broker.sell(position.mint, position.token_amount_raw, sol_price_usd)
        closed = position.model_copy(
            update={
                "closed_at": utcnow(),
                "proceeds_usd": fill.usd_value,
                "exit_price_usd": price_usd,
                "exit_reason": reason,
                "last_price_usd": price_usd,
            }
        )
        self._store.update_position(closed)
        log.info(
            "SELL %s (%s): $%.2f -> $%.2f, P&L %+.1f%%",
            closed.symbol,
            reason.value,
            closed.cost_usd,
            fill.usd_value,
            closed.pnl_pct or 0,
        )
        self._events.emit("sell", position=closed.model_dump(mode="json"), tx_id=fill.tx_id)
        return closed

    def _skip(self, symbol: str, why: str) -> None:
        log.info("executor: skipped %s: %s", symbol, why)
        self._events.emit("buy_skipped", symbol=symbol, reason=why)
        return None
