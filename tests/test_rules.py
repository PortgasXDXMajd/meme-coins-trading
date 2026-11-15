"""The hard rules: scout window, filter kills, sizing, stop and trail."""

from __future__ import annotations

from datetime import timedelta

import pytest

from desk.domain.models import ExitReason, KillRule, Position, RoundTrip
from desk.domain.rules import (
    check_stop,
    daily_loss_breached,
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
        (timedelta(hours=72), 1_000_000, True),
        (timedelta(hours=72, minutes=1), 1_000_000, False),  # too old
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
    assert signal.trailing_buy_share_pct == pytest.approx(100 * 240 / 460)


@pytest.mark.parametrize(
    ("overrides", "miss"),
    [
        ({"price_change_pct": {"m5": 2.9, "h1": 20}}, "5m price change below the minimum"),
        ({"price_change_pct": {"m5": 41, "h1": 20}}, "5m price change is a vertical candle"),
        ({"price_change_pct": {"m5": 6, "h1": -1}}, "1h price change below the minimum"),
        ({"price_change_pct": {}}, "5m price change below the minimum"),  # unknown: fail closed
        ({"volume_usd": {"m5": 2_999, "h1": 4_000}}, "5m volume too small"),
        ({"txns_m5": {"buys": 12, "sells": 7}}, "too few 5m trades"),
        ({"volume_usd": {"m5": 8_000, "h1": 60_000}}, "5m volume is not above the trailing hour"),
        ({"volume_usd": {"m5": 8_000, "h1": 8_000}}, "no trailing-hour volume to compare with"),
        ({"txns_m5": {"buys": 48, "sells": 42}}, "5m buy share below the minimum"),
        ({"txns_h1": {"buys": 500, "sells": 100}}, "5m buy share is below the trailing hour"),
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
