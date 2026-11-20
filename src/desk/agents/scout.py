"""Scout: finds pairs 30 minutes to 30 days old with a $300k-$20M market cap."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from datetime import datetime

from desk.config import Settings
from desk.domain.models import Candidate, utcnow
from desk.domain.rules import in_scout_window
from desk.ports import MintFeed, PriceFeed
from desk.storage.db import Store

log = logging.getLogger(__name__)

WATCHLIST_KEY = "scout_watchlist"
# A token this far above the window is not coming back into it soon.
OVERSIZE_MULTIPLE = 2


def _recent_volume_usd(candidate: Candidate) -> float:
    """Volume over the last 6 hours, or the last hour when that is all that is reported."""
    return max(candidate.volume_usd.h6 or 0.0, candidate.volume_usd.h1 or 0.0)


class Scout:
    """Keeps a watchlist of recently surfaced tokens and reports those in the window.

    Tokens usually surface before they qualify (too young, or too small), so they
    stay on the watchlist and are re-priced every scan until they age out or stop
    trading. That is
    how the desk sees a move as it starts instead of when a trending list shows it.

    `discover` (slow feeds) and `scan` (one batch of price snapshots) run on separate
    loops. The watchlist is saved, so a restart does not forget tokens found at launch.
    """

    def __init__(
        self,
        prices: PriceFeed,
        feeds: Sequence[MintFeed],
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
        store: Store | None = None,
    ) -> None:
        self._prices = prices
        self._feeds = feeds
        self._settings = settings
        self._clock = clock
        self._store = store
        self._watchlist: set[str] = self._load()
        self._discovered = 0

    async def discover(self) -> int:
        """Add what the feeds currently show to the watchlist. Returns how many they showed."""
        found: set[str] = set()
        for feed in self._feeds:
            found |= await feed.discover_mints()
        self._watchlist |= found
        self._discovered = len(found)
        self._save()
        return len(found)

    async def scan(self) -> list[Candidate]:
        """Re-price the watchlist and return the tokens in the window, busiest first."""
        watched = sorted(self._watchlist)
        snapshots = await self._prices.snapshots(watched)
        now = self._clock()
        # Only drop what this scan looked at: `discover` may have added tokens meanwhile.
        self._watchlist -= set(watched) - self._keep(snapshots, now)

        in_window = [c for c in snapshots.values() if in_scout_window(c, self._settings, now)]
        in_window.sort(key=lambda c: c.volume_usd.h1 or 0, reverse=True)
        log.info(
            "scout: %d discovered, %d watched, %d in window",
            self._discovered,
            len(self._watchlist),
            len(in_window),
        )
        return in_window

    def _keep(self, snapshots: dict[str, Candidate], now: datetime) -> set[str]:
        """Tokens worth another look: still trading, young enough and not far too big.

        A token nobody has traded for hours cannot trigger, so it gives up its place;
        the feeds add it again if it wakes up. When the list is full, tokens already in
        the window stay first, then the busiest.
        """
        ceiling = self._settings.max_market_cap_usd * OVERSIZE_MULTIPLE
        alive = [
            c
            for c in snapshots.values()
            if c.age_hours(now) <= self._settings.max_pair_age_hours
            and c.market_cap_usd <= ceiling
            and _recent_volume_usd(c) >= self._settings.watchlist_min_volume_h6_usd
        ]
        alive.sort(
            key=lambda c: (in_scout_window(c, self._settings, now), _recent_volume_usd(c)),
            reverse=True,
        )
        return {c.mint for c in alive[: self._settings.watchlist_max_size]}

    def _load(self) -> set[str]:
        if self._store is None:
            return set()
        try:
            saved = json.loads(self._store.get_text(WATCHLIST_KEY) or "[]")
        except ValueError:
            return set()
        return {mint for mint in saved if isinstance(mint, str)}

    def _save(self) -> None:
        if self._store is not None:
            self._store.set_text(WATCHLIST_KEY, json.dumps(sorted(self._watchlist)))
