"""Filter: applies the hard kill rules. Cheapest checks first; missing data kills."""

from __future__ import annotations

import logging

from desk.config import Settings
from desk.data.http import DataError
from desk.data.jupiter import Jupiter
from desk.data.rugcheck import RugCheck
from desk.domain.models import Candidate, FilterVerdict, KillReason, KillRule
from desk.domain.rules import liquidity_kills, round_trip_kills, safety_kills

log = logging.getLogger(__name__)


class Filter:
    def __init__(self, rugcheck: RugCheck, jupiter: Jupiter, settings: Settings) -> None:
        self._rugcheck = rugcheck
        self._jupiter = jupiter
        self._settings = settings

    async def evaluate(self, candidate: Candidate) -> FilterVerdict:
        # Stage 1: liquidity, from data the Scout already has.
        kills = liquidity_kills(candidate, self._settings)
        if kills:
            return FilterVerdict(candidate=candidate, kills=tuple(kills))

        # Stage 2: on-chain safety report.
        try:
            safety = await self._rugcheck.safety(candidate.mint)
        except DataError as exc:
            log.warning("filter: no safety report for %s: %s", candidate.symbol, exc)
            unavailable = KillReason(rule=KillRule.DATA_UNAVAILABLE, detail="safety report failed")
            return FilterVerdict(candidate=candidate, kills=(unavailable,))
        kills = safety_kills(safety, self._settings)
        if kills:
            return FilterVerdict(candidate=candidate, safety=safety, kills=tuple(kills))

        # Stage 3: can it actually be sold, and at what cost?
        try:
            round_trip = await self._jupiter.round_trip(
                candidate.mint, self._settings.round_trip_probe_usd, candidate.sol_price_usd
            )
        except DataError as exc:
            log.warning("filter: no round-trip quote for %s: %s", candidate.symbol, exc)
            round_trip = None
        return FilterVerdict(
            candidate=candidate,
            safety=safety,
            round_trip=round_trip,
            kills=tuple(round_trip_kills(round_trip, self._settings)),
        )
