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
        self.price = candidate.price_usd
        self.sell_route = True

    def _at_current_price(self) -> Candidate:
        return self.candidate.model_copy(update={"price_usd": self.price})

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
    def __init__(self, settings, store, llm: FakeLLM, *, kill: bool = False) -> None:
        self.candidate = make_candidate()
        self.market = FakeMarket(self.candidate)
        self.store = store
        self.llm = llm
        events = EventLog(settings.log_dir)
        broker = PaperBroker(self.market, store, settings)
        self.portfolio = Portfolio(broker, store)
        self.executor = Executor(broker, self.market, self.portfolio, store, settings, events)
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

    desk.market.price = 0.0015  # +50%: trail arms, stop moves to 0.00141
    assert await desk.monitor.check_positions() == []
    (position,) = store.open_positions()
    assert position.trailing_active
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
