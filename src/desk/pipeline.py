"""The desk's assembly line. This is the only place agents are called.

Two passes run side by side:

- `watch` (every scan): Scout -> momentum trigger -> buy what is already cleared.
  No LLM is involved, so a cleared token is bought within the scan that triggers it.
- `clear_next` (continuously): Filter -> Bear -> Analyst -> Risk for one queued token,
  which earns it a clearance. Tokens that triggered without one go first and are
  bought as soon as they win; after them, tokens that are warming up are cleared in
  advance.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from desk.agents.executor import Executor
from desk.agents.risk import RiskOfficer
from desk.config import Settings
from desk.debate import Debate
from desk.domain.models import (
    Candidate,
    Clearance,
    DebateOutcome,
    FilterVerdict,
    KillRule,
    Position,
    utcnow,
)
from desk.domain.rules import LIQUIDITY_UNKNOWN, MomentumSignal, PriceSample, momentum_signal
from desk.dossier import build_dossier
from desk.ports import TokenFilter, TokenScout
from desk.storage.db import Store
from desk.storage.debate_log import EventLog, write_transcript

log = logging.getLogger(__name__)

TRAIL_SAMPLES = 30  # ten minutes of scans
ANNOUNCE_EVERY = timedelta(minutes=5)  # a token that stays triggered is logged this often
# How long a token is left alone after a pass that settled nothing about it.
RETRY_BLOCKED = timedelta(minutes=2)  # entries blocked, or the debate budget is spent
RETRY_STALLED = timedelta(minutes=1)  # the buy was skipped; its clearance still stands


@dataclass(frozen=True)
class Lead:
    """A token worth acting on, with the momentum reading that made it so."""

    candidate: Candidate
    signal: MomentumSignal

    @property
    def mint(self) -> str:
        return self.candidate.mint


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
        self._trails: dict[str, deque[PriceSample]] = {}
        self._queue: list[Lead] = []  # rebuilt by every scan, most urgent first
        self._clearing: set[str] = set()
        self._wait_until: dict[str, datetime] = {}
        self._announced: dict[str, datetime] = {}
        self._blocked_until = clock()  # no clearing while the desk may not open positions
        self._buying = asyncio.Lock()  # one entry at a time, so the position limit holds

    async def run_cycle(self, *, debate: bool = True) -> list[Position]:
        """One whole pass: scan, then settle every token that triggered uncleared.

        `debate=False` stops after the Filter. The running desk calls `watch` and
        `clear_next` on separate loops instead.
        """
        opened = await self.watch()
        while lead := self._next(triggered_only=True):
            position = await self._clear(lead, debate=debate)
            if position:
                opened.append(position)
        return opened

    # --- the scan ------------------------------------------------------------

    async def watch(self) -> list[Position]:
        """One scan. Buys cleared tokens that trigger and queues the rest for clearance."""
        in_window = await self._scout.scan()
        now = self._clock()
        self._remember_prices(in_window, now)
        self._wait_until = {m: until for m, until in self._wait_until.items() if until > now}
        held = {p.mint for p in self._store.open_positions()}

        triggered: list[Lead] = []
        warm: list[Lead] = []
        for candidate in in_window:
            mint = candidate.mint
            if mint in held or mint in self._wait_until or self._store.on_cooldown(mint, now):
                continue
            signal = momentum_signal(candidate, self._settings, now, self._trails[mint])
            if signal.triggered:
                triggered.append(Lead(candidate, signal))
            elif signal.warm:
                warm.append(Lead(candidate, signal))
        for leads in (triggered, warm):
            leads.sort(key=lambda lead: lead.signal.volume_surge or 0, reverse=True)

        opened: list[Position] = []
        uncleared: list[Lead] = []
        for lead in triggered:
            clearance = self._store.clearance(lead.mint, now)
            self._announce(lead, clearance, now)
            if clearance is None:
                uncleared.append(lead)
                continue
            position = await self._buy(clearance, lead)
            if position:
                opened.append(position)
        uncleared += [lead for lead in warm if self._store.clearance(lead.mint, now) is None]
        self._queue = uncleared
        return opened

    def _remember_prices(self, in_window: list[Candidate], now: datetime) -> None:
        """Add this scan to each token's price trail; forget tokens that left the window."""
        seen = {c.mint for c in in_window}
        self._trails = {m: trail for m, trail in self._trails.items() if m in seen}
        for candidate in in_window:
            trail = self._trails.setdefault(candidate.mint, deque(maxlen=TRAIL_SAMPLES))
            trail.append((now, candidate.price_usd))

    def _announce(self, lead: Lead, clearance: Clearance | None, now: datetime) -> None:
        last = self._announced.get(lead.mint)
        if last is not None and now - last < ANNOUNCE_EVERY:
            return
        self._announced = {m: at for m, at in self._announced.items() if now - at < ANNOUNCE_EVERY}
        self._announced[lead.mint] = now
        candidate, signal = lead.candidate, lead.signal
        log.info(
            "momentum: %s TRIGGERED%s (%s): %s",
            candidate.symbol,
            " early" if signal.early else "",
            "cleared, buying now" if clearance else "not cleared, debating first",
            signal.describe(candidate),
        )
        self._events.emit(
            "momentum_trigger",
            symbol=candidate.symbol,
            mint=candidate.mint,
            price_usd=candidate.price_usd,
            early=signal.early,
            cleared=clearance is not None,
            fast_change_pct=signal.fast_change_pct,
            volume_surge=signal.volume_surge,
            buy_share_pct=signal.buy_share_pct,
            trailing_buy_share_pct=signal.trailing_buy_share_pct,
            price_change_m5_pct=candidate.price_change_pct.m5,
            price_change_h1_pct=candidate.price_change_pct.h1,
            volume_m5_usd=candidate.volume_usd.m5,
        )

    # --- clearance -----------------------------------------------------------

    async def clear_next(self) -> Position | None:
        """Filter and debate the most urgent queued token. Runs beside `watch`.

        Returns the position when a token that had triggered wins its debate and is
        bought; a token cleared in advance is bought later, by `watch`.
        """
        if self._clock() < self._blocked_until:
            return None
        lead = self._next(triggered_only=self._debates_last_hour() >= self._preclear_budget())
        return await self._clear(lead) if lead else None

    def _next(self, *, triggered_only: bool) -> Lead | None:
        while self._queue:
            lead = self._queue.pop(0)
            if lead.mint in self._clearing or lead.mint in self._wait_until:
                continue
            if lead.signal.triggered or not triggered_only:
                return lead
        return None

    async def _clear(self, lead: Lead, *, debate: bool = True) -> Position | None:
        candidate = lead.candidate
        self._clearing.add(lead.mint)
        try:
            # Nothing is spent on a token the desk could not buy anyway.
            block = await self._risk.entry_block(candidate.sol_price_usd) if debate else None
            if block:
                log.info("pipeline: not clearing %s, entries blocked: %s", candidate.symbol, block)
                self._blocked_until = self._clock() + RETRY_BLOCKED
                return self._leave(lead, RETRY_BLOCKED)

            verdict = await self._filter.evaluate(candidate)
            if not verdict.passed:
                self._record_kill(verdict)
                return None
            log.info("filter: %s PASSED", candidate.symbol)
            if not debate:
                return self._leave(lead, RETRY_BLOCKED)
            if self._debates_last_hour() >= self._settings.max_debates_per_hour:
                log.info("pipeline: hourly debate budget spent")
                return self._leave(lead, RETRY_BLOCKED)

            clearance = await self._debate_for_clearance(verdict, lead)
            if clearance is None or not lead.signal.triggered:
                return None
            return await self._buy(clearance, lead)
        finally:
            self._clearing.discard(lead.mint)

    async def _debate_for_clearance(self, verdict: FilterVerdict, lead: Lead) -> Clearance | None:
        candidate = verdict.candidate
        dossier = build_dossier(verdict, self._settings, self._clock(), lead.signal)
        log.info(
            "debate: %s started (%s)",
            candidate.symbol,
            "triggered" if lead.signal.triggered else "warming up",
        )
        outcome = await self._debate.run(dossier)
        transcript = write_transcript(outcome, self._settings.log_dir)
        debate_id = self._store.save_debate(outcome, transcript)
        self._events.emit(
            "debate",
            symbol=candidate.symbol,
            mint=candidate.mint,
            approved=outcome.approved,
            triggered=lead.signal.triggered,
            veto_reasons=outcome.veto_reasons,
            cost_usd=outcome.usage.cost_usd,
            transcript=transcript,
        )
        now = self._clock()
        if not outcome.approved:
            log.info("debate: %s VETOED: %s", candidate.symbol, "; ".join(outcome.veto_reasons))
            self._record_debate(
                candidate,
                "vetoed",
                outcome,
                now + timedelta(hours=self._settings.veto_cooldown_hours),
            )
            return None

        clearance = Clearance(
            mint=candidate.mint,
            symbol=candidate.symbol,
            debate_id=debate_id,
            cleared_at=now,
            expires_at=now + timedelta(minutes=self._settings.clearance_minutes),
        )
        self._store.save_clearance(clearance)
        self._record_debate(candidate, "approved", outcome, now)  # no cooldown: it may trigger
        log.info(
            "debate: %s APPROVED, %d of %d reasons left standing; cleared for %.0f minutes",
            candidate.symbol,
            len(outcome.unrebutted),
            len(outcome.reasons),
            self._settings.clearance_minutes,
        )
        self._events.emit(
            "clearance",
            symbol=candidate.symbol,
            mint=candidate.mint,
            debate_id=debate_id,
            expires_at=clearance.expires_at,
        )
        return clearance

    # --- the buy -------------------------------------------------------------

    async def _buy(self, clearance: Clearance, lead: Lead) -> Position | None:
        """Buy a cleared token that has triggered. No LLM call happens here."""
        candidate = lead.candidate
        async with self._buying:
            if any(p.mint == lead.mint for p in self._store.open_positions()):
                return None
            outcome = self._store.debate_outcome(clearance.debate_id)
            if outcome is None or not outcome.approved:
                self._store.drop_clearance(lead.mint)
                return None
            approval = await self._risk.approve(outcome, candidate)
            if approval is None:
                return self._leave(lead, RETRY_BLOCKED)
            position = await self._executor.buy(approval, clearance.debate_id)
            if position is None:
                return self._leave(lead, RETRY_STALLED)
            self._store.drop_clearance(lead.mint)
            self._store.record_evaluation(
                candidate.mint,
                candidate.symbol,
                "bought",
                {"debate_id": clearance.debate_id, "early": lead.signal.early},
                self._clock() + timedelta(hours=self._settings.traded_cooldown_hours),
            )
            return position

    def _leave(self, lead: Lead, wait: timedelta) -> None:
        """Nothing was settled about this token: look at it again after `wait`."""
        self._wait_until[lead.mint] = self._clock() + wait
        return None

    # --- records -------------------------------------------------------------

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

    def _record_debate(
        self, candidate: Candidate, outcome_name: str, outcome: DebateOutcome, until: datetime
    ) -> None:
        self._store.record_evaluation(
            candidate.mint,
            candidate.symbol,
            outcome_name,
            {"veto_reasons": list(outcome.veto_reasons)},
            until,
        )

    def _debates_last_hour(self) -> int:
        return len(self._store.debates_since(self._clock() - timedelta(hours=1)))

    def _preclear_budget(self) -> int:
        """Debates an hour that may go to tokens that have not triggered yet."""
        return min(self._settings.preclear_debates_per_hour, self._settings.max_debates_per_hour)
