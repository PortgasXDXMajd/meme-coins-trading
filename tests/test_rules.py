"""The hard rules: scout window, filter kills, sizing, stop and trail."""

from __future__ import annotations

from datetime import timedelta

import pytest

from desk.domain.models import ExitReason, KillRule, Position, RoundTrip, TxnCounts
from desk.domain.rules import (
    check_stop,
    daily_loss_breached,
    entry_stall,
    fast_move,
    in_scout_window,
    initial_stop_price,
    liquidity_kills,
    momentum_signal,
    position_size_usd,
    round_trip_kills,
    safety_kills,
    take_profit_due,
    volatility_stop_pct,
)
from tests.conftest import NOW, make_candidate, make_safety


def rules_of(kills) -> set[KillRule]:
    return {k.rule for k in kills}


# --- Scout window ------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "market_cap", "expected"),
    [
        (timedelta(minutes=29), 1_000_000, False),  # too young
        (timedelta(minutes=30), 1_000_000, True),
        (timedelta(days=30), 1_000_000, True),
        (timedelta(days=30, minutes=1), 1_000_000, False),  # too old
        (timedelta(hours=2), 299_999, False),  # too small
        (timedelta(hours=2), 300_000, True),
        (timedelta(hours=2), 20_000_000, True),
        (timedelta(hours=2), 20_000_001, False),  # too big
    ],
)
def test_scout_window(settings, age, market_cap, expected):
    candidate = make_candidate(pair_created_at=NOW - age, market_cap_usd=market_cap)
    assert in_scout_window(candidate, settings, NOW) is expected


# --- Filter kills ------------------------------------------------------------


def test_clean_token_has_no_kills(settings):
    assert liquidity_kills(make_candidate(), settings) == []
    assert safety_kills(make_safety(), settings) == []


def test_liquidity_below_40k_is_killed(settings):
    assert rules_of(liquidity_kills(make_candidate(liquidity_usd=39_999), settings)) == {
        KillRule.LOW_LIQUIDITY
    }
    assert liquidity_kills(make_candidate(liquidity_usd=40_000), settings) == []


def test_unknown_liquidity_is_killed(settings):
    assert rules_of(liquidity_kills(make_candidate(liquidity_usd=None), settings)) == {
        KillRule.DATA_UNAVAILABLE
    }


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"mint_authority_revoked": False}, KillRule.MINT_NOT_REVOKED),
        ({"freeze_authority_revoked": False}, KillRule.FREEZE_NOT_REVOKED),
        ({"lp_locked_pct": 98.9}, KillRule.LP_NOT_BURNED),
        ({"top10_holders_pct": 25.1}, KillRule.TOP10_CONCENTRATION),
        ({"dev_holding_pct": 3.1}, KillRule.DEV_HOLDING),
        ({"transfer_fee_pct": 1.0}, KillRule.TRANSFER_FEE),
        ({"dangerous_extensions": ("permanentDelegate",)}, KillRule.DANGEROUS_EXTENSION),
        ({"rugged": True}, KillRule.RUGGED),
    ],
)
def test_each_safety_rule_kills(settings, overrides, rule):
    assert rules_of(safety_kills(make_safety(**overrides), settings)) == {rule}


def test_values_exactly_at_the_limit_pass(settings):
    safety = make_safety(lp_locked_pct=99.0, top10_holders_pct=25.0, dev_holding_pct=3.0)
    assert safety_kills(safety, settings) == []


@pytest.mark.parametrize(
    "field",
    [
        "mint_authority_revoked",
        "freeze_authority_revoked",
        "lp_locked_pct",
        "top10_holders_pct",
        "dev_holding_pct",
        "transfer_fee_pct",
    ],
)
def test_unknown_safety_data_fails_closed(settings, field):
    kills = safety_kills(make_safety(**{field: None}), settings)
    assert rules_of(kills) == {KillRule.DATA_UNAVAILABLE}


def test_all_violations_are_reported_together(settings):
    safety = make_safety(mint_authority_revoked=False, dev_holding_pct=10, lp_locked_pct=0)
    assert rules_of(safety_kills(safety, settings)) == {
        KillRule.MINT_NOT_REVOKED,
        KillRule.DEV_HOLDING,
        KillRule.LP_NOT_BURNED,
    }


def test_round_trip_kills(settings):
    assert rules_of(round_trip_kills(None, settings)) == {KillRule.NO_SELL_ROUTE}
    costly = RoundTrip(size_usd=20, cost_pct=5.1)
    assert rules_of(round_trip_kills(costly, settings)) == {KillRule.ROUND_TRIP_COST}
    assert round_trip_kills(RoundTrip(size_usd=20, cost_pct=2.0), settings) == []


# --- Executor sizing -----------------------------------------------------------


def test_size_is_eight_percent_of_equity(settings):
    assert position_size_usd(equity_usd=250, cash_usd=250, settings=settings) == pytest.approx(20)


def test_size_is_capped_by_cash(settings):
    assert position_size_usd(equity_usd=250, cash_usd=12, settings=settings) == pytest.approx(12)


def test_size_below_minimum_means_no_trade(settings):
    assert position_size_usd(equity_usd=50, cash_usd=50, settings=settings) == 0  # 8% = $4
    assert position_size_usd(equity_usd=250, cash_usd=3, settings=settings) == 0


# --- Risk: stop and trail -------------------------------------------------------


def open_position(entry: float = 1.0, **overrides) -> Position:
    defaults = {
        "id": 1,
        "mint": "Mint",
        "symbol": "TEST",
        "pair_address": "Pair",
        "token_amount_raw": 1000,
        "cost_usd": 20.0,
        "entry_price_usd": entry,
        "stop_price_usd": entry * 0.94,
        "high_water_price_usd": entry,
        "last_price_usd": entry,
    }
    return Position(**(defaults | overrides))


def test_initial_stop_is_six_percent_below_entry(settings):
    assert initial_stop_price(1.0, settings) == pytest.approx(0.94)


def test_holds_above_the_stop(settings):
    check = check_stop(open_position(), 0.95, settings)
    assert check.exit_reason is None
    assert not check.position.trailing_active


def test_exits_at_minus_six_percent(settings):
    assert check_stop(open_position(), 0.94, settings).exit_reason is ExitReason.STOP_LOSS
    assert check_stop(open_position(), 0.50, settings).exit_reason is ExitReason.STOP_LOSS


def test_trail_stays_off_below_plus_twelve_percent(settings):
    check = check_stop(open_position(), 1.119, settings)
    assert not check.position.trailing_active
    assert check.position.stop_price_usd == pytest.approx(0.94)


def test_trail_activates_at_plus_twelve_percent(settings):
    check = check_stop(open_position(), 1.12, settings)
    assert check.exit_reason is None
    assert check.position.trailing_active
    assert check.position.stop_price_usd == pytest.approx(1.12 * 0.94)


def test_trailing_stop_follows_the_high_and_never_drops(settings):
    position = check_stop(open_position(), 1.50, settings).position
    assert position.stop_price_usd == pytest.approx(1.41)

    dipped = check_stop(position, 1.45, settings)
    assert dipped.exit_reason is None
    assert dipped.position.stop_price_usd == pytest.approx(1.41)
    assert dipped.position.high_water_price_usd == pytest.approx(1.50)


def test_trailing_stop_exit_locks_in_profit(settings):
    position = check_stop(open_position(), 1.50, settings).position
    check = check_stop(position, 1.40, settings)
    assert check.exit_reason is ExitReason.TRAILING_STOP


def test_daily_loss_halt(settings):
    assert not daily_loss_breached(250, 201, settings)
    assert daily_loss_breached(250, 200, settings)  # -20%
    assert daily_loss_breached(0, 0, settings)


# --- Volatility stop and take-profit -------------------------------------------


@pytest.mark.parametrize(
    ("ranges", "expected"),
    [
        ([4.0] * 15, 10.0),  # 2.5 x 4%
        ([1.0] * 15, 6.0),  # floor
        ([8.0] * 15, 12.0),  # cap
        ([], 12.0),  # no data: widest
    ],
)
def test_volatility_stop_pct(settings, ranges, expected):
    assert volatility_stop_pct(ranges, settings) == pytest.approx(expected)


def test_initial_stop_uses_the_volatility_stop(settings):
    assert initial_stop_price(1.0, settings, 10) == pytest.approx(0.90)


def test_trail_is_as_wide_as_the_entry_stop(settings):
    position = check_stop(open_position(stop_pct=10, stop_price_usd=0.9), 1.50, settings).position
    assert position.stop_price_usd == pytest.approx(1.35)


def test_take_profit_fires_once_at_plus_eighteen(settings):
    assert not take_profit_due(open_position(), 1.17, settings)
    assert take_profit_due(open_position(), 1.19, settings)
    assert not take_profit_due(open_position(took_profit=True), 1.50, settings)


def test_value_counts_only_what_is_still_held(settings):
    assert open_position(open_fraction=0.5).value_at(2.0) == pytest.approx(20.0)


# --- Momentum trigger --------------------------------------------------------


def test_a_breakout_against_the_trailing_hour_triggers(settings):
    signal = momentum_signal(make_candidate(), settings, NOW)
    assert signal.triggered
    assert signal.misses == ()
    # $8k in 5m against ($30k - $8k) spread over the 11 earlier 5-minute windows.
    assert signal.volume_surge == pytest.approx(4.0)
    assert signal.buy_share_pct == pytest.approx(100 * 60 / 90)
    assert signal.trailing_buy_share_pct == pytest.approx(100 * 220 / 500)


@pytest.mark.parametrize(
    ("overrides", "miss"),
    [
        ({"price_change_pct": {"m5": 2.9, "h1": 20}}, "5m price change below the minimum"),
        (
            {"price_change_pct": {"m5": 16, "h1": 20}},
            "5m price change: most of the move has already happened",
        ),
        ({"price_change_pct": {"m5": 6, "h1": 61}}, "1h price change: already extended"),
        ({"price_change_pct": {"m5": 6, "h1": -1}}, "1h price change below the minimum"),
        ({"price_change_pct": {}}, "5m price change below the minimum"),  # unknown: fail closed
        ({"volume_usd": {"m5": 2_999, "h1": 4_000}}, "5m volume too small"),
        ({"txns_m5": {"buys": 12, "sells": 7}}, "too few 5m trades"),
        ({"volume_usd": {"m5": 8_000, "h1": 60_000}}, "5m volume is not above the trailing hour"),
        ({"volume_usd": {"m5": 8_000, "h1": 8_000}}, "no trailing-hour volume to compare with"),
        ({"txns_m5": {"buys": 48, "sells": 42}}, "5m buy share below the minimum"),
        ({"txns_h1": {"buys": 500, "sells": 100}}, "5m buy share is below the trailing hour"),
        # Buyers already led the hour before the breakout: 240 of 460 trades (52%).
        ({"txns_h1": {"buys": 300, "sells": 250}}, "trailing hour was not seller-led"),
        # No trades before the last 5 minutes: unknown, fail closed.
        ({"txns_h1": {"buys": 60, "sells": 30}}, "trailing hour was not seller-led"),
    ],
)
def test_momentum_misses(settings, overrides, miss):
    signal = momentum_signal(make_candidate(**overrides), settings, NOW)
    assert not signal.triggered
    assert miss in signal.misses


def test_a_young_pair_is_compared_with_the_minutes_it_has_traded(settings):
    # 35 minutes old: the baseline is 30 minutes (6 windows), not 55.
    young = make_candidate(pair_created_at=NOW - timedelta(minutes=35))
    assert momentum_signal(young, settings, NOW).volume_surge == pytest.approx(8_000 / (22_000 / 6))


def test_momentum_thresholds_come_from_settings(settings):
    strict = settings.model_copy(update={"momentum_volume_surge_multiple": 5.0})
    assert not momentum_signal(make_candidate(), strict, NOW).triggered


def test_the_seller_led_hour_gate_comes_from_settings(settings):
    buyer_led = make_candidate(txns_h1={"buys": 300, "sells": 250})  # 52% buys before the 5m
    assert settings.momentum_max_trailing_buy_share_pct == 50
    assert not momentum_signal(buyer_led, settings, NOW).triggered
    looser = settings.model_copy(update={"momentum_max_trailing_buy_share_pct": 55})
    assert momentum_signal(buyer_led, looser, NOW).triggered
    # 100 turns the gate off, even when the trailing hour's flow is unknown.
    off = settings.model_copy(update={"momentum_max_trailing_buy_share_pct": 100})
    assert (
        "trailing hour was not seller-led"
        not in momentum_signal(make_candidate(txns_h1={"buys": 60, "sells": 30}), off, NOW).misses
    )


def test_tokens_are_not_debated_before_they_trigger_by_default(settings):
    assert settings.preclear_debates_per_hour == 0


# --- Early trigger: the desk's own price trail ---------------------------------

# Too little for the 5-minute trigger: +1% in 5m on a 1.7x volume surge.
BUILDING = {
    "price_change_pct": {"m5": 1.0, "h1": 20.0},
    "volume_usd": {"m5": 4_000, "h1": 30_000},
}


def trail(*prices: float, step_seconds: float = 25):
    """Prices the desk saw, one scan apart, the last of them at NOW."""
    last = len(prices) - 1
    return [
        (NOW - timedelta(seconds=step_seconds * (last - n)), price)
        for n, price in enumerate(prices)
    ]


def test_a_rise_over_the_desks_own_prices_triggers_early(settings):
    candidate = make_candidate(**BUILDING)
    assert not momentum_signal(candidate, settings, NOW).triggered

    signal = momentum_signal(candidate, settings, NOW, trail(1.00, 1.01, 1.02, 1.03))
    assert signal.triggered and signal.early
    assert signal.fast_change_pct == pytest.approx(3.0)
    assert "+3.0% over the desk's own last 75s of prices" in signal.describe(candidate)


@pytest.mark.parametrize(
    ("prices", "why"),
    [
        ((1.00, 1.01, 1.015, 1.018), "up less than 2%"),
        ((1.00, 1.04, 1.05, 1.03), "no longer at its high"),
        ((1.00, 1.03), "too few samples to cover the window"),
    ],
)
def test_the_early_trigger_needs_a_rise_that_is_still_going(settings, prices, why):
    signal = momentum_signal(make_candidate(**BUILDING), settings, NOW, trail(*prices))
    assert not signal.triggered, why


def test_the_early_trigger_still_needs_volume_buyers_and_room(settings):
    rising = trail(1.00, 1.01, 1.02, 1.03)

    def triggered(**overrides) -> bool:
        return momentum_signal(
            make_candidate(**(BUILDING | overrides)), settings, NOW, rising
        ).triggered

    assert not triggered(volume_usd={"m5": 3_000, "h1": 30_000})  # 1.2x: below 1.3x
    assert not triggered(txns_m5={"buys": 40, "sells": 50})
    assert not triggered(price_change_pct={"m5": 1.0, "h1": 80.0})  # already extended
    assert not triggered(price_change_pct={"m5": -2.0, "h1": 20.0})  # a bounce in a falling 5m


def test_old_or_stale_samples_are_not_a_move(settings):
    old = [(at - timedelta(minutes=10), price) for at, price in trail(1.00, 1.01, 1.02, 1.03)]
    assert fast_move(old, NOW, settings) is None
    stale = [(at - timedelta(seconds=60), price) for at, price in trail(1.00, 1.01, 1.02)]
    assert fast_move(stale, NOW, settings) is None


def test_a_token_that_is_building_but_has_not_broken_out_is_warm(settings):
    assert momentum_signal(make_candidate(**BUILDING), settings, NOW).warm
    assert not momentum_signal(make_candidate(), settings, NOW).warm  # triggered, not warm


@pytest.mark.parametrize(
    "overrides",
    [
        {"price_change_pct": {"m5": 1.0, "h1": 80.0}},  # already extended
        {"price_change_pct": {"m5": 1.0, "h1": -3.0}},  # falling hour
        {"price_change_pct": {"m5": -1.0, "h1": 20.0}},  # falling now
        {"volume_usd": {"m5": 1_000, "h1": 30_000}},  # hardly trading
        {"volume_usd": {"m5": 2_000, "h1": 30_000}},  # below its own hour
        {"txns_m5": {"buys": 40, "sells": 50}},  # sellers lead
    ],
)
def test_a_token_that_is_not_building_is_not_warm(settings, overrides):
    assert not momentum_signal(make_candidate(**(BUILDING | overrides)), settings, NOW).warm


# --- Stall check at the buy ----------------------------------------------------


def test_a_move_that_is_still_alive_is_not_stalled(settings):
    assert entry_stall(0.001, make_candidate(price_usd=0.001), settings) is None
    assert entry_stall(0.001, make_candidate(price_usd=0.00104), settings) is None


def test_a_price_back_below_the_trigger_is_a_stall(settings):
    assert "below the trigger price" in (
        entry_stall(0.001, make_candidate(price_usd=0.000995), settings) or ""
    )
    lenient = settings.model_copy(update={"max_entry_slip_pct": 1.0})
    assert entry_stall(0.001, make_candidate(price_usd=0.000995), lenient) is None


def test_sellers_taking_over_is_a_stall(settings):
    selling = make_candidate(txns_m5=TxnCounts(buys=20, sells=40))
    assert "33% of the last 5 minutes' trades are buys" in (
        entry_stall(0.001, selling, settings) or ""
    )
    silent = make_candidate(txns_m5=TxnCounts())
    assert "no trades" in (entry_stall(0.001, silent, settings) or "")
