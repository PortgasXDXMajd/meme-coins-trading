"""Every tunable threshold lives here. Values come from the environment / .env."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    # --- credentials -------------------------------------------------------
    anthropic_api_key: SecretStr
    jupiter_api_key: SecretStr | None = None
    helius_api_key: SecretStr | None = None
    wallet_keyfile: Path = Path("secrets/wallet.json")

    # --- mode --------------------------------------------------------------
    trading_mode: Literal["paper", "live"] = "paper"
    paper_starting_usd: float = Field(default=250.0, gt=0)
    paper_network_fee_usd: float = Field(default=0.05, ge=0)

    # --- scout -------------------------------------------------------------
    min_pair_age_minutes: float = 30
    max_pair_age_hours: float = 720  # 30 days
    min_market_cap_usd: float = 300_000
    max_market_cap_usd: float = 20_000_000
    watchlist_max_size: int = 600
    # A watched token with less 6-hour volume than this is dropped: it cannot trigger,
    # and the feeds bring it back if it starts trading again.
    watchlist_min_volume_h6_usd: float = Field(default=1_000, ge=0)

    # --- momentum trigger --------------------------------------------------
    # A token in the window is only worth the Filter and a debate while it is breaking
    # out against its own trailing hour: the last 5 minutes compared with the 55 before.
    momentum_min_price_change_m5_pct: float = 3
    momentum_max_price_change_m5_pct: float = 15  # most of the move has already happened
    momentum_min_price_change_h1_pct: float = 0  # not a bounce inside a falling hour
    momentum_max_price_change_h1_pct: float = 60  # already extended: the desk would buy the top
    momentum_min_volume_m5_usd: float = Field(default=3_000, ge=0)
    momentum_min_txns_m5: int = Field(default=20, ge=0)
    momentum_volume_surge_multiple: float = Field(default=2.0, gt=0)
    momentum_min_buy_share_pct: float = Field(default=55, ge=0, le=100)
    # The breakout must follow an hour that sellers led: at most this share of the
    # trades before the last 5 minutes were buys. In the desk's first 168 debates,
    # tokens under 50% reached +10% before -10% twice as often as the rest; a breakout
    # after an hour of buying was usually the end of the move. 100 turns the gate off.
    momentum_max_trailing_buy_share_pct: float = Field(default=50, ge=0, le=100)
    # The early trigger. DexScreener's 5-minute figures show a move minutes after it
    # starts, so the desk also keeps its own price trail from every scan: a rise of
    # `momentum_fast_min_price_change_pct` over the last `momentum_fast_window_seconds`,
    # still at its high, triggers on a smaller volume surge.
    momentum_fast_window_seconds: float = Field(default=90, gt=0)
    momentum_fast_min_price_change_pct: float = Field(default=2, gt=0)
    momentum_fast_volume_surge_multiple: float = Field(default=1.3, gt=0)

    # --- clearance ---------------------------------------------------------
    # Tokens that are warming up are filtered and debated before they trigger. An
    # approved one is cleared for `clearance_minutes` and bought by code the moment it
    # triggers. At most `preclear_debates_per_hour` debates an hour go to tokens that
    # have not triggered yet; a token that triggers uncleared is still debated.
    # Off by default: five in six debates went to tokens the desk never bought, and
    # the debates cost as much as the trades lost.
    clearance_minutes: float = Field(default=60, gt=0)
    preclear_debates_per_hour: int = Field(default=0, ge=0)
    warm_min_volume_surge_multiple: float = Field(default=1.0, gt=0)
    warm_min_buy_share_pct: float = Field(default=50, ge=0, le=100)

    # --- filter ------------------------------------------------------------
    min_liquidity_usd: float = 40_000
    max_top10_holders_pct: float = 25
    max_dev_holding_pct: float = 3
    min_lp_locked_pct: float = 99
    max_round_trip_cost_pct: float = 5
    round_trip_probe_usd: float = 20

    # --- executor ----------------------------------------------------------
    max_position_pct: float = Field(default=8, gt=0, le=100)
    min_trade_usd: float = 5
    max_open_positions: int = 5
    max_entry_drift_pct: float = 8
    # The buy is cancelled when the move has stalled since the trigger: the price is
    # more than this far below the trigger price, or buys no longer lead the last 5 minutes.
    max_entry_slip_pct: float = Field(default=0, ge=0)
    min_entry_buy_share_pct: float = Field(default=50, ge=0, le=100)

    # --- risk --------------------------------------------------------------
    # The stop is sized to the token's own noise: `stop_volatility_multiple` times its
    # average one-minute range, never tighter than `stop_loss_pct` and never wider than
    # `max_stop_loss_pct`. Without candle data the widest stop is used.
    stop_loss_pct: float = Field(default=6, gt=0, lt=100)
    max_stop_loss_pct: float = Field(default=12, gt=0, lt=100)
    stop_volatility_multiple: float = Field(default=2.5, gt=0)
    volatility_candles: int = Field(default=15, ge=3)
    # Sell `take_profit_fraction` of the position at +`take_profit_pct`, then hold the
    # rest with the stop moved up to at least break-even.
    take_profit_pct: float = Field(default=18, gt=0)
    take_profit_fraction: float = Field(default=0.5, gt=0, lt=1)
    trail_activation_pct: float = Field(default=12, gt=0)
    trail_distance_pct: float = Field(default=6, gt=0, lt=100)
    daily_loss_halt_pct: float = Field(default=20, gt=0)

    # --- cadence -----------------------------------------------------------
    # The watchlist is re-priced every `scout_interval_seconds` so a breakout is seen
    # quickly; the slower discovery feeds only add tokens to it.
    scout_interval_seconds: float = 20
    discovery_interval_seconds: float = 60
    risk_interval_seconds: float = 5
    clearance_interval_seconds: float = 2  # pause between two filter-and-debate passes
    filter_cooldown_minutes: float = 30
    no_liquidity_cooldown_hours: float = 6  # DexScreener reports no pool liquidity
    veto_cooldown_hours: float = 6
    traded_cooldown_hours: float = 24
    max_debates_per_hour: int = 20

    # --- llm ---------------------------------------------------------------
    debate_model: str = "claude-opus-5-5"  # Analyst, referee, post-mortem, report
    bear_model: str = "claude-sonnet-5-5"
    # A debate approves when at most this many Bear reasons are left unrebutted.
    max_unrebutted_reasons: int = Field(default=1, ge=0)
    llm_effort: Effort = "high"
    max_evidence_tool_rounds: int = 6
    lessons_in_prompt: int = 8

    # --- paths -------------------------------------------------------------
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "desk.db"

    @property
    def halt_file(self) -> Path:
        """Create this file to stop new entries; open positions stay managed."""
        return self.data_dir / "HALT"

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"
