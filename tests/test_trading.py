"""The trade lifecycle on fakes: pipeline handoffs, paper fills, stop and trail exits."""

from __future__ import annotations

from datetime import timedelta

import pytest

from desk.agents.analyst import Analyst
from desk.agents.bear import Bear
from desk.agents.executor import Executor
from desk.agents.risk import RiskOfficer, StopMonitor
from desk.brokers.paper import PaperBroker
from desk.data.http import DataError
from desk.data.jupiter import Order
from desk.debate import Debate
from desk.domain.models import Candidate, ExitReason, FilterVerdict, KillReason, KillRule
from desk.pipeline import Pipeline
from desk.portfolio import Portfolio
from desk.storage.debate_log import EventLog
from tests.conftest import (
    NOW,
    FakeLLM,
    analyst_response,
    bear_case,
    make_candidate,
    make_verdict,
    referee_decision,
)

TOKENS_PER_USD = 1000  # at the entry price of $0.001
FEE_RATE = 0.01  # each simulated swap loses 1%


class FakeMarket:
    """Stands in for DexScreener and Jupiter with one controllable price."""

    def __init__(self, candidate: Candidate) -> None:
        self.candidate = candidate
        self.price = candidate.price_usd  # what the pool pays (Jupiter)
        self.dex_price: float | None = None  # set to make DexScreener lag behind the pool
        self.sell_route = True
        # One-minute ranges of 2.4% give a 2.5 x 2.4 = 6% stop with default settings.
        self.minute_ranges: list[float] | None = [2.4] * 15

    # GeckoTerminal
    async def minute_ranges_pct(self, pool_address: str, count: int) -> list[float]:
        if self.minute_ranges is None:
            raise DataError("no candles")
        return self.minute_ranges[:count]

    def _at_current_price(self) -> Candidate:
        shown = self.price if self.dex_price is None else self.dex_price
        return self.candidate.model_copy(update={"price_usd": shown})

    # DexScreener
    async def snapshot(self, mint: str) -> Candidate | None:
        return self._at_current_price()

    async def snapshots(self, mints: list[str]) -> dict[str, Candidate]:
        return {m: self._at_current_price() for m in mints}

    # Jupiter
    async def buy_quote(self, mint: str, lamports: int, taker: str | None = None) -> Order:
        usd = lamports / 1e9 * self.candidate.sol_price_usd
        tokens = int(usd * (1 - FEE_RATE) / self.price)
        return Order(
            input_mint="SOL",
            output_mint=mint,
            in_amount=lamports,
            out_amount=tokens,
            in_usd=usd,
            out_usd=usd * (1 - FEE_RATE),
            request_id="q",
        )

    async def sell_quote(self, mint: str, token_amount: int, taker: str | None = None) -> Order:
        if not self.sell_route:
            raise DataError("no route")
        value = token_amount * self.price
        return Order(
            input_mint=mint,
            output_mint="SOL",
            in_amount=token_amount,
            out_amount=1,
            in_usd=value,
            out_usd=value * (1 - FEE_RATE),
            request_id="q",
        )


class FakeScout:
    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates

    async def scan(self) -> list[Candidate]:
        return self.candidates


class FakeFilter:
    def __init__(self, kill: bool = False) -> None:
        self.kill = kill
        self.seen: list[str] = []

    async def evaluate(self, candidate: Candidate) -> FilterVerdict:
        self.seen.append(candidate.symbol)
        if self.kill:
            reason = KillReason(rule=KillRule.LOW_LIQUIDITY, detail="test")
            return FilterVerdict(candidate=candidate, kills=(reason,))
        return make_verdict(**candidate.model_dump())


class Desk:
    def __init__(
        self, settings, store, llm: FakeLLM, *, kill: bool = False, max_unrebutted: int = 0
    ) -> None:
        self.candidate = make_candidate()
        self.market = FakeMarket(self.candidate)
        self.store = store
        self.llm = llm
        events = EventLog(settings.log_dir)
        broker = PaperBroker(self.market, store, settings)
        self.portfolio = Portfolio(broker, store)
        self.executor = Executor(
            broker,
            self.market,
            self.portfolio,
            store,
            settings,
            events,
            quotes=self.market,
            candles=self.market,
        )
        self.risk = RiskOfficer(llm, self.portfolio, store, settings, clock=lambda: NOW)
        self.closed: list = []

        async def on_close(position) -> None:
            self.closed.append(position)

        self.monitor = StopMonitor(
            self.market, self.market, self.executor, store, settings, events, on_close=on_close
        )
        self.filter = FakeFilter(kill)
        self.pipeline = Pipeline(
            FakeScout([self.candidate]),
            self.filter,
            Debate(
                Bear(llm, settings),
                Analyst(llm, settings),
                self.risk,
                tool_factory=lambda d, ledger: [],
                max_unrebutted=max_unrebutted,
            ),
            self.risk,
            self.executor,
            store,
            settings,
            events,
            clock=lambda: NOW,
        )


def winning_llm() -> FakeLLM:
    return FakeLLM(bear_case(), analyst_response(), referee_decision())


def vetoing_llm() -> FakeLLM:
    return FakeLLM(bear_case(), analyst_response(), referee_decision(unrebutted=(1,)))


async def test_approved_debate_opens_an_eight_percent_position(settings, store):
    desk = Desk(settings, store, winning_llm())
    (position,) = await desk.pipeline.run_cycle()

    assert position.cost_usd == pytest.approx(20 + settings.paper_network_fee_usd)  # 8% of $250
    assert position.entry_price_usd == pytest.approx(0.001)
    assert position.stop_price_usd == pytest.approx(0.00094)  # -6%, set at entry
    assert position.debate_id is not None
    assert await desk.portfolio.cash_usd(80) == pytest.approx(250 - position.cost_usd)
    assert store.open_positions() == [position]


async def test_vetoed_debate_buys_nothing_and_cools_down(settings, store):
    desk = Desk(settings, store, vetoing_llm())
    assert await desk.pipeline.run_cycle() == []
    assert store.open_positions() == []
    assert await desk.portfolio.cash_usd(80) == 250
    assert store.on_cooldown(desk.candidate.mint, NOW + timedelta(hours=5))
    assert not store.on_cooldown(desk.candidate.mint, NOW + timedelta(hours=7))

    desk.llm.calls.clear()
    await desk.pipeline.run_cycle()
    assert desk.llm.calls == []  # not re-debated during the cooldown


async def test_filter_kill_never_reaches_the_debate(settings, store):
    desk = Desk(settings, store, winning_llm(), kill=True)
    assert await desk.pipeline.run_cycle() == []
    assert desk.llm.calls == []
    assert store.evaluation_counts(NOW - timedelta(days=1)) == {"filtered": 1}


async def test_debate_transcript_is_written_to_disk(settings, store):
    desk = Desk(settings, store, vetoing_llm())
    await desk.pipeline.run_cycle()
    (transcript,) = (settings.log_dir / "debates").rglob("*.md")
    assert "VETO" in transcript.read_text()


async def test_held_token_is_not_bought_twice(settings, store):
    desk = Desk(settings, store, winning_llm())
    await desk.pipeline.run_cycle()
    assert await desk.pipeline.run_cycle() == []
    assert len(store.open_positions()) == 1


async def test_executor_refuses_anything_but_an_approval(settings, store):
    desk = Desk(settings, store, winning_llm())
    with pytest.raises(TypeError):
        await desk.executor.buy("just buy it")  # type: ignore[arg-type]


async def test_price_drift_during_debate_cancels_the_buy(settings, store):
    desk = Desk(settings, store, winning_llm())
    desk.market.price = desk.candidate.price_usd * 1.2
    assert await desk.pipeline.run_cycle() == []
    assert store.open_positions() == []


async def test_stop_loss_sells_at_minus_six_percent(settings, store):
    desk = Desk(settings, store, winning_llm())
    (position,) = await desk.pipeline.run_cycle()

    desk.market.price = 0.00095
    assert await desk.monitor.check_positions() == []

    desk.market.price = 0.00094
    (closed,) = await desk.monitor.check_positions()
    assert closed.exit_reason is ExitReason.STOP_LOSS
    assert closed.pnl_pct is not None and closed.pnl_pct < -6  # price loss plus costs
    assert store.open_positions() == []
    assert desk.closed == [closed]
    assert closed.proceeds_usd is not None
    assert await desk.portfolio.cash_usd(80) == pytest.approx(
        250 - position.cost_usd + closed.proceeds_usd
    )


async def test_trailing_stop_takes_profit_after_plus_twelve(settings, store):
    desk = Desk(settings, store, winning_llm())
    await desk.pipeline.run_cycle()

    desk.market.price = 0.0015  # +50%: trail arms, stop moves to 0.00141, half is sold
    assert await desk.monitor.check_positions() == []
    (position,) = store.open_positions()
    assert position.trailing_active
    assert position.took_profit
    assert position.stop_price_usd == pytest.approx(0.00141)

    desk.market.price = 0.0014
    (closed,) = await desk.monitor.check_positions()
    assert closed.exit_reason is ExitReason.TRAILING_STOP
    assert closed.pnl_usd is not None and closed.pnl_usd > 0


async def test_failed_sell_keeps_the_position_and_retries(settings, store):
    desk = Desk(settings, store, winning_llm())
    await desk.pipeline.run_cycle()
    desk.market.price = 0.0005
    desk.market.sell_route = False

    assert await desk.monitor.check_positions() == []
    assert len(store.open_positions()) == 1

    desk.market.sell_route = True
    (closed,) = await desk.monitor.check_positions()
    assert closed.exit_reason is ExitReason.STOP_LOSS


async def test_positions_survive_a_restart(settings, tmp_path):
    from desk.storage.db import Store

    path = tmp_path / "desk.db"
    first = Desk(settings, Store(path), winning_llm())
    (position,) = await first.pipeline.run_cycle()
    first.store.close()

    reopened = Store(path)
    assert reopened.open_positions() == [position]
    assert reopened.get_float("paper_cash_usd") == pytest.approx(250 - position.cost_usd)


async def test_max_open_positions_blocks_new_entries(settings, store):
    desk = Desk(settings.model_copy(update={"max_open_positions": 1}), store, winning_llm())
    await desk.pipeline.run_cycle()
    assert "positions open" in (await desk.risk.entry_block(80) or "")


async def test_halt_file_blocks_new_entries(settings, store):
    desk = Desk(settings, store, winning_llm())
    settings.halt_file.parent.mkdir(parents=True, exist_ok=True)
    settings.halt_file.touch()
    assert await desk.pipeline.run_cycle() == []
    assert desk.llm.calls == []  # no money spent debating while halted


async def test_stop_fires_on_the_sell_quote_when_the_price_feed_lags(settings, store):
    desk = Desk(settings, store, winning_llm())
    await desk.pipeline.run_cycle()
    desk.market.dex_price = 0.001  # DexScreener still shows the entry price
    desk.market.price = 0.0009  # but the pool would only pay 10% less

    (closed,) = await desk.monitor.check_positions()
    assert closed.exit_reason is ExitReason.STOP_LOSS
    assert closed.exit_price_usd == pytest.approx(0.0009)


async def test_price_feed_is_the_fallback_when_no_quote_comes_back(settings, store):
    desk = Desk(settings, store, winning_llm())
    await desk.pipeline.run_cycle()
    desk.market.sell_route = False
    desk.market.price = 0.00097

    assert await desk.monitor.check_positions() == []
    (check,) = store.price_checks()
    assert check.source == "dex"
    assert check.quote_price_usd is None
    assert check.used_price_usd == pytest.approx(0.00097)


async def test_every_price_check_is_recorded(settings, store):
    desk = Desk(settings, store, winning_llm())
    (position,) = await desk.pipeline.run_cycle()
    desk.market.price = 0.00098
    await desk.monitor.check_positions()
    desk.market.price = 0.0009
    await desk.monitor.check_positions()

    first, second = store.price_checks(position.id)
    assert first.source == "quote"
    assert first.quote_price_usd == pytest.approx(0.00098)
    assert first.dex_price_usd == pytest.approx(0.00098)
    assert first.exit_reason is None
    assert second.exit_reason is ExitReason.STOP_LOSS
    assert second.stop_price_usd == pytest.approx(0.00094)


async def test_position_without_a_quote_basis_calibrates_on_its_first_check(settings, store):
    desk = Desk(settings, store, winning_llm())
    (position,) = await desk.pipeline.run_cycle()
    store.update_position(position.model_copy(update={"quote_basis_usd": None}))
    desk.market.price = 0.0011

    assert await desk.monitor.check_positions() == []
    (held,) = store.open_positions()
    assert held.quote_basis_usd is not None
    assert held.last_price_usd == pytest.approx(0.0011)  # matches the feed at calibration

    desk.market.dex_price = 0.0011  # feed freezes; the quote now leads
    desk.market.price = 0.001
    await desk.monitor.check_positions()
    (held,) = store.open_positions()
    assert held.last_price_usd == pytest.approx(0.001)


# --- volatility stop, take-profit, debate threshold, liquidity cooldown ------------


async def test_stop_is_sized_from_one_minute_volatility(settings, store):
    desk = Desk(settings, store, winning_llm())
    desk.market.minute_ranges = [4.0] * 15  # 2.5 x 4% = 10% stop
    (position,) = await desk.pipeline.run_cycle()
    assert position.stop_pct == pytest.approx(10)
    assert position.stop_price_usd == pytest.approx(0.0009)
    assert position.cost_usd == pytest.approx(20 + settings.paper_network_fee_usd)  # size kept

    desk.market.price = 0.00094  # -6%: inside the noise, so it holds
    assert await desk.monitor.check_positions() == []


async def test_stop_is_capped_and_falls_back_to_the_widest_without_candles(settings, store):
    desk = Desk(settings, store, winning_llm())
    desk.market.minute_ranges = None
    (position,) = await desk.pipeline.run_cycle()
    assert position.stop_pct == pytest.approx(settings.max_stop_loss_pct)


async def test_take_profit_sells_half_and_moves_the_stop_to_break_even(settings, store):
    desk = Desk(settings, store, winning_llm())
    (position,) = await desk.pipeline.run_cycle()
    cash_after_buy = await desk.portfolio.cash_usd(80)

    desk.market.price = 0.00119  # +19%, past the +18% take-profit
    assert await desk.monitor.check_positions() == []
    (held,) = store.open_positions()
    assert held.took_profit
    assert held.token_amount_raw == position.token_amount_raw // 2
    assert held.open_fraction == pytest.approx(0.5, abs=1e-6)
    assert held.realized_usd > 0
    assert held.stop_price_usd >= held.entry_price_usd
    assert await desk.portfolio.cash_usd(80) == pytest.approx(cash_after_buy + held.realized_usd)

    desk.market.price = 0.00125  # no second take-profit
    await desk.monitor.check_positions()
    (again,) = store.open_positions()
    assert again.realized_usd == pytest.approx(held.realized_usd)

    desk.market.price = 0.001  # back to entry: the rest goes at break-even
    (closed,) = await desk.monitor.check_positions()
    rest = held.token_amount_raw * 0.001 * (1 - FEE_RATE) - settings.paper_network_fee_usd
    assert closed.proceeds_usd == pytest.approx(held.realized_usd + rest)
    assert closed.pnl_usd is not None and closed.pnl_usd > 0


async def test_one_standing_reason_still_trades_when_allowed(settings, store):
    llm = FakeLLM(bear_case(), analyst_response(), referee_decision(unrebutted=(2,)))
    desk = Desk(settings, store, llm, max_unrebutted=1)
    assert len(await desk.pipeline.run_cycle()) == 1


async def test_tokens_without_liquidity_data_are_skipped_for_hours(settings, store):
    desk = Desk(settings, store, winning_llm())
    desk.pipeline._scout = FakeScout([desk.candidate.model_copy(update={"liquidity_usd": None})])

    class LiquidityFilter(FakeFilter):
        async def evaluate(self, candidate):
            from desk.domain.rules import liquidity_kills

            return FilterVerdict(
                candidate=candidate, kills=tuple(liquidity_kills(candidate, settings))
            )

    desk.pipeline._filter = LiquidityFilter()
    await desk.pipeline.run_cycle()
    assert store.on_cooldown(desk.candidate.mint, NOW + timedelta(hours=5))
    assert not store.on_cooldown(desk.candidate.mint, NOW + timedelta(hours=7))


async def test_other_filter_kills_cool_down_for_thirty_minutes(settings, store):
    desk = Desk(settings, store, winning_llm(), kill=True)
    await desk.pipeline.run_cycle()
    assert store.on_cooldown(desk.candidate.mint, NOW + timedelta(minutes=29))
    assert not store.on_cooldown(desk.candidate.mint, NOW + timedelta(minutes=31))
