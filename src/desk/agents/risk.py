"""Risk: referees the debate, signs off entries, and enforces the stop and the trail.

Two roles, one agent:
- `RiskOfficer` (pre-trade) rules each Bear reason REBUTTED/UNREBUTTED and issues
  the `TradeApproval`, with the stop fixed before any order exists.
- `StopMonitor` (post-trade) watches prices and exits at -6%, or at the trailing
  stop once +12% has been reached.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from desk.agents.bear import render_reasons, render_token_header
from desk.agents.executor import Executor
from desk.brokers.base import BrokerError
from desk.config import Settings
from desk.data.dexscreener import DexScreener
from desk.data.http import DataError
from desk.data.jupiter import Jupiter
from desk.domain.models import (
    BearReason,
    DebateOutcome,
    Dossier,
    Fact,
    Position,
    Rebuttal,
    RefereeDecision,
    TradeApproval,
    utcnow,
)
from desk.domain.rules import check_stop, daily_loss_breached, position_size_usd
from desk.llm.client import LLMResult, StructuredLLM, debate_prompt
from desk.portfolio import Portfolio
from desk.storage.db import Store
from desk.storage.debate_log import EventLog

log = logging.getLogger(__name__)


def render_rebuttals(rebuttals: Sequence[Rebuttal]) -> str:
    return "\n\n".join(
        f"Rebuttal to reason {r.reason_number}"
        f"{' (CONCEDED)' if r.conceded else ''}: {r.argument}\n"
        f"Cites: {', '.join(r.cited_facts)}"
        for r in rebuttals
    )


class RiskOfficer:
    def __init__(
        self,
        llm: StructuredLLM,
        portfolio: Portfolio,
        store: Store,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._llm = llm
        self._portfolio = portfolio
        self._store = store
        self._settings = settings
        self._clock = clock
        self._system = debate_prompt("referee", settings)

    async def rule(
        self,
        dossier: Dossier,
        evidence: Sequence[Fact],
        reasons: Sequence[BearReason],
        rebuttals: Sequence[Rebuttal],
    ) -> LLMResult[RefereeDecision]:
        evidence_text = (
            "\n".join(f"[{f.id}] ({f.category}) {f.statement}" for f in evidence) or "(none)"
        )
        user = (
            f"<token>{render_token_header(dossier)}</token>\n\n"
            f"<dossier>\n{dossier.render()}\n</dossier>\n\n"
            f"<evidence>\n{evidence_text}\n</evidence>\n\n"
            f"<bear_case>\n{render_reasons(reasons)}\n</bear_case>\n\n"
            f"<analyst_rebuttals>\n{render_rebuttals(rebuttals)}\n</analyst_rebuttals>\n\n"
            "Rule on each of the three reasons, in order."
        )
        return await self._llm.ask(system=self._system, user=user, output_type=RefereeDecision)

    async def entry_block(self, sol_price_usd: float) -> str | None:
        """Why the desk may not open a new position right now, or None if it may."""
        if self._settings.halt_file.exists():
            return f"halt file present ({self._settings.halt_file})"
        open_count = len(self._store.open_positions())
        if open_count >= self._settings.max_open_positions:
            return f"{open_count} positions open (max {self._settings.max_open_positions})"
        equity = await self._portfolio.equity_usd(sol_price_usd)
        day_start = self._portfolio.day_start_equity(self._clock(), equity)
        if daily_loss_breached(day_start, equity, self._settings):
            return f"daily loss limit hit: equity ${equity:.2f} vs ${day_start:.2f} at day start"
        cash = await self._portfolio.cash_usd(sol_price_usd)
        if position_size_usd(equity, cash, self._settings) <= 0:
            return f"not enough cash for a minimum trade (${cash:.2f})"
        return None

    async def approve(self, outcome: DebateOutcome) -> TradeApproval | None:
        """Issue the approval for a won debate, with the stop set before entry."""
        if not outcome.approved:
            return None
        sol_price = outcome.dossier.candidate.sol_price_usd
        block = await self.entry_block(sol_price)
        if block:
            log.info("risk: entry blocked for %s: %s", outcome.dossier.candidate.symbol, block)
            return None
        equity = await self._portfolio.equity_usd(sol_price)
        cash = await self._portfolio.cash_usd(sol_price)
        return TradeApproval(
            outcome=outcome,
            stop_loss_pct=self._settings.stop_loss_pct,
            max_size_usd=position_size_usd(equity, cash, self._settings),
        )


class StopMonitor:
    def __init__(
        self,
        dex: DexScreener,
        jupiter: Jupiter,
        executor: Executor,
        store: Store,
        settings: Settings,
        events: EventLog,
        on_close: Callable[[Position], Awaitable[None]] | None = None,
    ) -> None:
        self._dex = dex
        self._jupiter = jupiter
        self._executor = executor
        self._store = store
        self._settings = settings
        self._events = events
        self._on_close = on_close
        self._sol_price_usd: float | None = None

    async def check_positions(self) -> list[Position]:
        """One monitoring pass. Returns the positions closed during it."""
        positions = self._store.open_positions()
        if not positions:
            return []
        try:
            snapshots = await self._dex.snapshots([p.mint for p in positions])
        except DataError as exc:
            log.warning("risk: price feed failed, will retry: %s", exc)
            snapshots = {}
        for snapshot in snapshots.values():
            self._sol_price_usd = snapshot.sol_price_usd

        closed = []
        for position in positions:
            snapshot = snapshots.get(position.mint)
            price = snapshot.price_usd if snapshot else await self._price_from_quote(position)
            if price is None or self._sol_price_usd is None:
                log.warning("risk: no price for %s this pass", position.symbol)
                continue
            result = await self._apply_price(position, price, self._sol_price_usd)
            if not result.is_open:
                closed.append(result)
        return closed

    async def _apply_price(self, position: Position, price: float, sol_price: float) -> Position:
        check = check_stop(position, price, self._settings)
        updated = check.position.model_copy(update={"last_price_usd": price})
        if check.exit_reason is None:
            self._store.update_position(updated)
            return updated
        try:
            closed = await self._executor.sell(updated, price, sol_price, check.exit_reason)
        except BrokerError as exc:
            # Keep the position open and retry next pass; the stop stays triggered.
            log.error("risk: SELL FAILED for %s, retrying: %s", position.symbol, exc)
            self._events.emit("sell_failed", symbol=position.symbol, error=str(exc))
            self._store.update_position(updated)
            return updated
        if self._on_close:
            try:
                await self._on_close(closed)
            except Exception:  # a failed review must never disturb risk management
                log.exception("risk: post-close hook failed for %s", closed.symbol)
        return closed

    async def _price_from_quote(self, position: Position) -> float | None:
        """Fallback when the price feed has no data: imply a price from a sell quote."""
        try:
            order = await self._jupiter.sell_quote(position.mint, position.token_amount_raw)
        except DataError:
            return None
        return position.entry_price_usd * order.out_usd / position.cost_usd
