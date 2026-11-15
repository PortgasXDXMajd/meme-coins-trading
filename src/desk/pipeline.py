"""The desk's assembly line. This is the only place agents are called, and always in
the same order: Scout -> momentum trigger -> Filter -> Bear -> Analyst -> Risk -> Executor.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from desk.agents.executor import Executor
from desk.agents.risk import RiskOfficer
from desk.config import Settings
from desk.debate import Debate
from desk.domain.models import (
    Candidate,
    DebateOutcome,
    FilterVerdict,
    KillRule,
    Position,
    utcnow,
)
from desk.domain.rules import LIQUIDITY_UNKNOWN, momentum_signal
from desk.dossier import build_dossier
from desk.ports import TokenFilter, TokenScout
from desk.storage.db import Store
from desk.storage.debate_log import EventLog, write_transcript

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        scout: TokenScout,
        filter_: TokenFilter,
        debate: Debate,
        risk: RiskOfficer,
        executor: Executor,
        store: Store,
        settings: Settings,
        events: EventLog,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._scout = scout
        self._filter = filter_
        self._debate = debate
        self._risk = risk
        self._executor = executor
        self._store = store
        self._settings = settings
        self._events = events
        self._clock = clock

    async def run_cycle(self, *, debate: bool = True) -> list[Position]:
        """One discovery pass. Returns positions opened. `debate=False` stops after Filter."""
        opened: list[Position] = []
        held = {p.mint for p in self._store.open_positions()}
        for candidate in self._moving(await self._scout.scan(), held):
            verdict = await self._filter.evaluate(candidate)
            if not verdict.passed:
                self._record_kill(verdict)
                continue
            log.info("filter: %s PASSED", candidate.symbol)
            if not debate:
                continue

            block = await self._risk.entry_block(candidate.sol_price_usd)
            if block:
                log.info("pipeline: not debating, entries blocked: %s", block)
                break
            if self._debate_budget_spent():
                log.info("pipeline: hourly debate budget spent")
                break

            position = await self._debate_and_trade(verdict)
            if position:
                opened.append(position)
                held.add(position.mint)
        return opened

    def _moving(self, in_window: list[Candidate], held: set[str]) -> list[Candidate]:
        """The tokens breaking out right now, strongest volume surge first.

        A token that is not moving costs nothing and is not put on cooldown: it is
        simply looked at again on the next scan.
        """
        now = self._clock()
        triggered = []
        for candidate in in_window:
            if candidate.mint in held or self._store.on_cooldown(candidate.mint, now):
                continue
            signal = momentum_signal(candidate, self._settings, now)
            if signal.triggered:
                triggered.append((signal, candidate))
        triggered.sort(key=lambda pair: pair[0].volume_surge or 0, reverse=True)
        for signal, candidate in triggered:
            log.info("momentum: %s TRIGGERED: %s", candidate.symbol, signal.describe(candidate))
            self._events.emit(
                "momentum_trigger",
                symbol=candidate.symbol,
                mint=candidate.mint,
                price_usd=candidate.price_usd,
                volume_surge=signal.volume_surge,
                buy_share_pct=signal.buy_share_pct,
                trailing_buy_share_pct=signal.trailing_buy_share_pct,
                price_change_m5_pct=candidate.price_change_pct.m5,
                price_change_h1_pct=candidate.price_change_pct.h1,
                volume_m5_usd=candidate.volume_usd.m5,
            )
        return [candidate for _, candidate in triggered]

    async def _debate_and_trade(self, verdict: FilterVerdict) -> Position | None:
        candidate = verdict.candidate
        dossier = build_dossier(verdict, self._settings, self._clock())
        log.info("debate: %s started", candidate.symbol)
        outcome = await self._debate.run(dossier)
        transcript = write_transcript(outcome, self._settings.log_dir)
        debate_id = self._store.save_debate(outcome, transcript)
        self._events.emit(
            "debate",
            symbol=candidate.symbol,
            mint=candidate.mint,
            approved=outcome.approved,
            veto_reasons=outcome.veto_reasons,
            cost_usd=outcome.usage.cost_usd,
            transcript=transcript,
        )

        if not outcome.approved:
            log.info("debate: %s VETOED: %s", candidate.symbol, "; ".join(outcome.veto_reasons))
            self._cooldown(candidate, "vetoed", outcome, hours=self._settings.veto_cooldown_hours)
            return None

        log.info(
            "debate: %s APPROVED, %d of %d reasons left standing",
            candidate.symbol,
            len(outcome.unrebutted),
            len(outcome.reasons),
        )
        self._cooldown(candidate, "approved", outcome, hours=self._settings.traded_cooldown_hours)
        approval = await self._risk.approve(outcome)
        if approval is None:
            return None
        return await self._executor.buy(approval, debate_id)

    def _record_kill(self, verdict: FilterVerdict) -> None:
        candidate = verdict.candidate
        kills = [f"{k.rule.value}: {k.detail}" for k in verdict.kills]
        log.info("filter: %s killed: %s", candidate.symbol, "; ".join(kills))
        self._events.emit("filter_kill", symbol=candidate.symbol, mint=candidate.mint, kills=kills)
        self._store.record_evaluation(
            candidate.mint,
            candidate.symbol,
            "filtered",
            {"kills": kills},
            self._clock() + self._kill_cooldown(verdict),
        )

    def _kill_cooldown(self, verdict: FilterVerdict) -> timedelta:
        """Tokens DexScreener shows no liquidity for rarely gain it soon; skip them longer."""
        no_liquidity = any(
            k.rule is KillRule.DATA_UNAVAILABLE and k.detail == LIQUIDITY_UNKNOWN
            for k in verdict.kills
        )
        if no_liquidity:
            return timedelta(hours=self._settings.no_liquidity_cooldown_hours)
        return timedelta(minutes=self._settings.filter_cooldown_minutes)

    def _cooldown(
        self, candidate: Candidate, outcome_name: str, outcome: DebateOutcome, *, hours: float
    ) -> None:
        self._store.record_evaluation(
            candidate.mint,
            candidate.symbol,
            outcome_name,
            {"veto_reasons": list(outcome.veto_reasons)},
            self._clock() + timedelta(hours=hours),
        )

    def _debate_budget_spent(self) -> bool:
        recent = self._store.debates_since(self._clock() - timedelta(hours=1))
        return len(recent) >= self._settings.max_debates_per_hour
