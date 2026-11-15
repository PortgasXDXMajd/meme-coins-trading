"""The hard rules, as pure functions. No I/O, no LLM: these cannot be argued with."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from desk.config import Settings
from desk.domain.models import (
    Candidate,
    ExitReason,
    KillReason,
    KillRule,
    Position,
    RoundTrip,
    TokenSafety,
)

# --------------------------------------------------------------------------
# Scout window
# --------------------------------------------------------------------------


def in_scout_window(candidate: Candidate, settings: Settings, now: datetime) -> bool:
    age_hours = candidate.age_hours(now)
    old_enough = age_hours * 60 >= settings.min_pair_age_minutes
    young_enough = age_hours <= settings.max_pair_age_hours
    cap_ok = settings.min_market_cap_usd <= candidate.market_cap_usd <= settings.max_market_cap_usd
    return old_enough and young_enough and cap_ok


# --------------------------------------------------------------------------
# Momentum trigger. Unknown data never triggers (fail closed).
# --------------------------------------------------------------------------

TRAILING_MINUTES = 55.0  # the hour before the last 5 minutes
WINDOW_MINUTES = 5.0


@dataclass(frozen=True)
class MomentumSignal:
    """How the last 5 minutes compare with the token's own trailing hour."""

    triggered: bool
    volume_surge: float | None  # 5m volume / average 5m volume of the trailing hour
    buy_share_pct: float | None  # share of 5m trades that were buys
    trailing_buy_share_pct: float | None  # the same share over the trailing hour
    misses: tuple[str, ...] = ()  # why it did not trigger

    def describe(self, candidate: Candidate) -> str:
        def number(value: float | None, pattern: str) -> str:
            return "unknown" if value is None else pattern.format(value)

        return (
            f"5m volume ${candidate.volume_usd.m5 or 0:,.0f} is "
            f"{number(self.volume_surge, '{:.1f}x')} its trailing-hour 5-minute average; "
            f"5m buy share {number(self.buy_share_pct, '{:.0f}%')} vs "
            f"{number(self.trailing_buy_share_pct, '{:.0f}%')} over the trailing hour; "
            f"price {number(candidate.price_change_pct.m5, '{:+.1f}%')} in 5m, "
            f"{number(candidate.price_change_pct.h1, '{:+.1f}%')} in 1h"
        )


def _buy_share_pct(buys: int, sells: int) -> float | None:
    total = buys + sells
    return 100 * buys / total if total > 0 else None


def momentum_signal(candidate: Candidate, settings: Settings, now: datetime) -> MomentumSignal:
    """Is this token breaking out right now, measured against its own trailing hour?

    DexScreener's 1h figures include the last 5 minutes, so the baseline is the hour
    minus those 5 minutes. A pair younger than an hour has a shorter baseline.
    """
    misses: list[str] = []
    volume_m5 = candidate.volume_usd.m5 or 0.0
    change_m5 = candidate.price_change_pct.m5
    change_h1 = candidate.price_change_pct.h1
    m5, h1 = candidate.txns_m5, candidate.txns_h1

    if change_m5 is None or change_m5 < settings.momentum_min_price_change_m5_pct:
        misses.append("5m price change below the minimum")
    elif change_m5 > settings.momentum_max_price_change_m5_pct:
        misses.append("5m price change is a vertical candle")
    if change_h1 is None or change_h1 < settings.momentum_min_price_change_h1_pct:
        misses.append("1h price change below the minimum")
    if volume_m5 < settings.momentum_min_volume_m5_usd:
        misses.append("5m volume too small")
    if m5.buys + m5.sells < settings.momentum_min_txns_m5:
        misses.append("too few 5m trades")

    trailing_minutes = min(TRAILING_MINUTES, candidate.age_hours(now) * 60 - WINDOW_MINUTES)
    earlier_volume = max(0.0, (candidate.volume_usd.h1 or 0.0) - volume_m5)
    surge = None
    if trailing_minutes >= WINDOW_MINUTES and earlier_volume > 0:
        surge = volume_m5 / (earlier_volume * WINDOW_MINUTES / trailing_minutes)
    if surge is None:
        misses.append("no trailing-hour volume to compare with")
    elif surge < settings.momentum_volume_surge_multiple:
        misses.append("5m volume is not above the trailing hour")

    buy_share = _buy_share_pct(m5.buys, m5.sells)
    trailing_share = _buy_share_pct(max(0, h1.buys - m5.buys), max(0, h1.sells - m5.sells))
    if buy_share is None or buy_share < settings.momentum_min_buy_share_pct:
        misses.append("5m buy share below the minimum")
    elif trailing_share is not None and buy_share < trailing_share:
        misses.append("5m buy share is below the trailing hour")

    return MomentumSignal(
        triggered=not misses,
        volume_surge=surge,
        buy_share_pct=buy_share,
        trailing_buy_share_pct=trailing_share,
        misses=tuple(misses),
    )


# --------------------------------------------------------------------------
# Filter kills. Unknown data is always a kill (fail closed).
# --------------------------------------------------------------------------


LIQUIDITY_UNKNOWN = "liquidity unknown"


def liquidity_kills(candidate: Candidate, settings: Settings) -> list[KillReason]:
    liquidity = candidate.liquidity_usd
    if liquidity is None:
        return [KillReason(rule=KillRule.DATA_UNAVAILABLE, detail=LIQUIDITY_UNKNOWN)]
    if liquidity < settings.min_liquidity_usd:
        return [
            KillReason(
                rule=KillRule.LOW_LIQUIDITY,
                detail=f"liquidity ${liquidity:,.0f} < ${settings.min_liquidity_usd:,.0f}",
            )
        ]
    return []


def safety_kills(safety: TokenSafety, settings: Settings) -> list[KillReason]:
    kills: list[KillReason] = []

    def unknown(what: str) -> None:
        kills.append(KillReason(rule=KillRule.DATA_UNAVAILABLE, detail=f"{what} unknown"))

    if safety.rugged:
        kills.append(KillReason(rule=KillRule.RUGGED, detail="RugCheck marks this token rugged"))

    if safety.mint_authority_revoked is None:
        unknown("mint authority")
    elif not safety.mint_authority_revoked:
        kills.append(KillReason(rule=KillRule.MINT_NOT_REVOKED, detail="mint authority is set"))

    if safety.freeze_authority_revoked is None:
        unknown("freeze authority")
    elif not safety.freeze_authority_revoked:
        kills.append(KillReason(rule=KillRule.FREEZE_NOT_REVOKED, detail="freeze authority is set"))

    if safety.lp_locked_pct is None:
        unknown("LP burn status")
    elif safety.lp_locked_pct < settings.min_lp_locked_pct:
        kills.append(
            KillReason(
                rule=KillRule.LP_NOT_BURNED,
                detail=f"LP burned/locked {safety.lp_locked_pct:.1f}% "
                f"< {settings.min_lp_locked_pct:.0f}%",
            )
        )

    if safety.top10_holders_pct is None:
        unknown("top-10 holders")
    elif safety.top10_holders_pct > settings.max_top10_holders_pct:
        kills.append(
            KillReason(
                rule=KillRule.TOP10_CONCENTRATION,
                detail=f"top 10 hold {safety.top10_holders_pct:.1f}% "
                f"> {settings.max_top10_holders_pct:.0f}%",
            )
        )

    if safety.dev_holding_pct is None:
        unknown("dev holding")
    elif safety.dev_holding_pct > settings.max_dev_holding_pct:
        kills.append(
            KillReason(
                rule=KillRule.DEV_HOLDING,
                detail=f"dev holds {safety.dev_holding_pct:.2f}% "
                f"> {settings.max_dev_holding_pct:.0f}%",
            )
        )

    if safety.transfer_fee_pct is None:
        unknown("transfer fee")
    elif safety.transfer_fee_pct > 0:
        kills.append(
            KillReason(
                rule=KillRule.TRANSFER_FEE,
                detail=f"transfer fee {safety.transfer_fee_pct:.2f}%",
            )
        )

    if safety.dangerous_extensions:
        kills.append(
            KillReason(
                rule=KillRule.DANGEROUS_EXTENSION,
                detail="token extensions: " + ", ".join(safety.dangerous_extensions),
            )
        )
    return kills


def round_trip_kills(round_trip: RoundTrip | None, settings: Settings) -> list[KillReason]:
    if round_trip is None:
        return [KillReason(rule=KillRule.NO_SELL_ROUTE, detail="no buy+sell route quoted")]
    if round_trip.cost_pct > settings.max_round_trip_cost_pct:
        return [
            KillReason(
                rule=KillRule.ROUND_TRIP_COST,
                detail=f"round trip costs {round_trip.cost_pct:.2f}% "
                f"> {settings.max_round_trip_cost_pct:.0f}%",
            )
        ]
    return []


# --------------------------------------------------------------------------
# Executor sizing
# --------------------------------------------------------------------------


def position_size_usd(equity_usd: float, cash_usd: float, settings: Settings) -> float:
    """Largest allowed trade: 8% of equity, capped by cash. 0 means "do not trade"."""
    size = min(equity_usd * settings.max_position_pct / 100, cash_usd)
    return size if size >= settings.min_trade_usd else 0.0


def entry_drift_pct(reference_price: float, current_price: float) -> float:
    return abs(current_price - reference_price) / reference_price * 100


# --------------------------------------------------------------------------
# Risk: stop and trail
# --------------------------------------------------------------------------


def volatility_stop_pct(minute_ranges_pct: list[float], settings: Settings) -> float:
    """Stop distance for a new position, from its recent one-minute candle ranges.

    A stop inside the token's normal one-minute swing gets hit by noise, so the stop
    sits `stop_volatility_multiple` average ranges below entry, clamped to
    [stop_loss_pct, max_stop_loss_pct]. No candle data means the widest stop.
    """
    ranges = [r for r in minute_ranges_pct if r >= 0]
    if not ranges:
        return settings.max_stop_loss_pct
    pct = settings.stop_volatility_multiple * sum(ranges) / len(ranges)
    return min(max(pct, settings.stop_loss_pct), settings.max_stop_loss_pct)


def initial_stop_price(
    entry_price: float, settings: Settings, stop_pct: float | None = None
) -> float:
    return entry_price * (1 - (stop_pct or settings.stop_loss_pct) / 100)


def trail_distance_pct(position: Position, settings: Settings) -> float:
    """The trail is never tighter than the noise the entry stop was sized for."""
    return max(settings.trail_distance_pct, position.stop_pct or 0)


@dataclass(frozen=True)
class StopCheck:
    position: Position  # with high-water mark, trailing flag and stop updated
    exit_reason: ExitReason | None


def check_stop(position: Position, price: float, settings: Settings) -> StopCheck:
    """Advance a position's stop state for a new price and say whether to exit.

    The stop starts at the entry stop. Once price has reached entry +12% the stop
    trails the high by the trail distance. The stop never moves down.
    """
    high = max(position.high_water_price_usd, price)
    activation_price = position.entry_price_usd * (1 + settings.trail_activation_pct / 100)
    trailing = position.trailing_active or high >= activation_price

    stop = position.stop_price_usd
    if trailing:
        stop = max(stop, high * (1 - trail_distance_pct(position, settings) / 100))

    updated = position.model_copy(
        update={"high_water_price_usd": high, "trailing_active": trailing, "stop_price_usd": stop}
    )
    if price > stop:
        return StopCheck(updated, None)
    reason = ExitReason.TRAILING_STOP if trailing else ExitReason.STOP_LOSS
    return StopCheck(updated, reason)


def take_profit_due(position: Position, price: float, settings: Settings) -> bool:
    """True the first time price reaches entry +take_profit_pct."""
    target = position.entry_price_usd * (1 + settings.take_profit_pct / 100)
    return not position.took_profit and price >= target


def daily_loss_breached(day_start_equity: float, equity: float, settings: Settings) -> bool:
    if day_start_equity <= 0:
        return True
    drawdown_pct = (day_start_equity - equity) / day_start_equity * 100
    return drawdown_pct >= settings.daily_loss_halt_pct
