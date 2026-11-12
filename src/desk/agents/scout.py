"""Scout: finds pairs 30 minutes to 72 hours old with a $300k-$20M market cap."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from desk.config import Settings
from desk.data.dexscreener import DexScreener
from desk.data.geckoterminal import GeckoTerminal
from desk.domain.models import Candidate, utcnow
from desk.domain.rules import in_scout_window

log = logging.getLogger(__name__)


class Scout:
    """Keeps a watchlist of recently surfaced tokens and reports those in the window.

    Tokens usually surface before they qualify (too young, or too small), so they
    stay on the watchlist and are re-checked every scan until they age out.
    """

    def __init__(
        self,
        dex: DexScreener,
        gecko: GeckoTerminal,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._dex = dex
        self._gecko = gecko
        self._settings = settings
        self._clock = clock
        self._watchlist: set[str] = set()

    async def scan(self) -> list[Candidate]:
        discovered = await self._dex.discover_mints() | await self._gecko.discover_mints()
        self._watchlist |= discovered

        snapshots = await self._dex.snapshots(sorted(self._watchlist))
        now = self._clock()
        self._watchlist = self._prune(snapshots, now)

        in_window = [c for c in snapshots.values() if in_scout_window(c, self._settings, now)]
        in_window.sort(key=lambda c: c.volume_usd.h1 or 0, reverse=True)
        log.info(
            "scout: %d discovered, %d watched, %d in window",
            len(discovered),
            len(self._watchlist),
            len(in_window),
        )
        return in_window

    def _prune(self, snapshots: dict[str, Candidate], now: datetime) -> set[str]:
        """Drop tokens with no usable pair or past the age limit; cap the list size."""
        alive = [
            c for c in snapshots.values() if c.age_hours(now) <= self._settings.max_pair_age_hours
        ]
        alive.sort(key=lambda c: c.market_cap_usd, reverse=True)
        return {c.mint for c in alive[: self._settings.watchlist_max_size]}
