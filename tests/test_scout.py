"""The Scout's watchlist: what the feeds add, what a scan keeps, and what survives a restart."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from desk.agents.scout import Scout
from desk.data.jupiter import young_token_mints
from desk.domain.models import Candidate
from tests.conftest import NOW, make_candidate


class FakeFeed:
    def __init__(self, *mints: str) -> None:
        self.mints = set(mints)

    async def discover_mints(self) -> set[str]:
        return set(self.mints)


class FakePrices:
    def __init__(self, *candidates: Candidate) -> None:
        self.by_mint = {c.mint: c for c in candidates}
        self.asked: list[list[str]] = []
        self.during_scan = None

    async def snapshot(self, mint: str) -> Candidate | None:
        return self.by_mint.get(mint)

    async def snapshots(self, mints: list[str]) -> dict[str, Candidate]:
        self.asked.append(list(mints))
        if self.during_scan:
            await self.during_scan()
        return {m: self.by_mint[m] for m in mints if m in self.by_mint}


def scout(settings, prices, *feeds, store=None) -> Scout:
    return Scout(prices, feeds, settings, clock=lambda: NOW, store=store)


async def test_every_feed_adds_to_the_watchlist(settings):
    prices = FakePrices(make_candidate(mint="A"), make_candidate(mint="B"))
    desk_scout = scout(settings, prices, FakeFeed("A"), FakeFeed("B", "A"))

    assert await desk_scout.discover() == 2
    assert {c.mint for c in await desk_scout.scan()} == {"A", "B"}


async def test_a_small_token_stays_watched_until_it_grows_into_the_window(settings):
    prices = FakePrices(make_candidate(mint="A", market_cap_usd=80_000))
    desk_scout = scout(settings, prices, FakeFeed("A"))
    await desk_scout.discover()
    assert await desk_scout.scan() == []

    desk_scout._feeds = [FakeFeed()]  # the feeds no longer show it
    await desk_scout.discover()
    prices.by_mint["A"] = make_candidate(mint="A", market_cap_usd=400_000)
    assert [c.mint for c in await desk_scout.scan()] == ["A"]


async def test_scan_drops_tokens_that_are_too_old_too_big_or_have_no_pair(settings):
    prices = FakePrices(
        make_candidate(mint="Old", pair_created_at=NOW - timedelta(days=31)),
        make_candidate(mint="Huge", market_cap_usd=50_000_000),
        make_candidate(mint="Big", market_cap_usd=30_000_000),  # may fall back into the window
        make_candidate(mint="Fine"),
    )
    desk_scout = scout(settings, prices, FakeFeed("Old", "Huge", "Big", "Fine", "NoPair"))
    await desk_scout.discover()
    await desk_scout.scan()
    assert desk_scout._watchlist == {"Big", "Fine"}


async def test_a_full_watchlist_keeps_tokens_in_the_window_first_then_the_busiest(settings):
    small = settings.model_copy(update={"watchlist_max_size": 2})
    quiet, busy = {"h1": 2_000, "h6": 9_000}, {"h1": 40_000, "h6": 200_000}
    prices = FakePrices(
        make_candidate(mint="InWindow", market_cap_usd=400_000, volume_usd=quiet),
        make_candidate(mint="BusySmall", market_cap_usd=90_000, volume_usd=busy),
        make_candidate(mint="QuietBig", market_cap_usd=30_000_000, volume_usd=quiet),
    )
    desk_scout = scout(small, prices, FakeFeed("InWindow", "BusySmall", "QuietBig"))
    await desk_scout.discover()
    await desk_scout.scan()
    assert desk_scout._watchlist == {"InWindow", "BusySmall"}


async def test_a_token_that_stopped_trading_gives_up_its_place(settings):
    prices = FakePrices(
        make_candidate(mint="Dead", volume_usd={"h1": 0, "h6": 40, "h24": 500_000}),
        make_candidate(mint="NoFigures", volume_usd={}),
        make_candidate(mint="Trading", volume_usd={"h1": 0, "h6": 5_000}),
        make_candidate(mint="JustWoke", volume_usd={"h1": 3_000}),
    )
    desk_scout = scout(settings, prices, FakeFeed("Dead", "NoFigures", "Trading", "JustWoke"))
    await desk_scout.discover()
    await desk_scout.scan()
    assert desk_scout._watchlist == {"Trading", "JustWoke"}


async def test_tokens_discovered_during_a_scan_are_not_lost(settings):
    prices = FakePrices(make_candidate(mint="A"))
    desk_scout = scout(settings, prices, FakeFeed("A"))
    await desk_scout.discover()

    async def discover_meanwhile() -> None:
        desk_scout._feeds = [FakeFeed("B")]
        await desk_scout.discover()

    prices.during_scan = discover_meanwhile
    await desk_scout.scan()
    assert desk_scout._watchlist == {"A", "B"}


async def test_the_watchlist_survives_a_restart(settings, store):
    prices = FakePrices(make_candidate(mint="A", market_cap_usd=80_000))
    await scout(settings, prices, FakeFeed("A"), store=store).discover()

    restarted = scout(settings, prices, store=store)
    await restarted.scan()
    assert prices.asked[-1] == ["A"]


def test_jupiter_lists_keep_only_young_tokens_that_are_not_too_big():
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    tokens = [
        {"id": "Young", "mcap": 500_000, "firstPool": {"createdAt": "2026-11-15T06:00:00Z"}},
        {"id": "Old", "mcap": 500_000, "firstPool": {"createdAt": "2026-10-01T00:00:00Z"}},
        {"id": "Huge", "mcap": 90_000_000, "firstPool": {"createdAt": "2026-11-15T06:00:00Z"}},
        {"id": "NoPool", "mcap": 500_000},
        {"mcap": 1, "firstPool": {"createdAt": "not a date"}},
    ]
    assert young_token_mints(tokens, now, 72, 20_000_000) == {"Young"}
