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
    max_pair_age_hours: float = 72
    min_market_cap_usd: float = 300_000
    max_market_cap_usd: float = 20_000_000
    watchlist_max_size: int = 300

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
    scout_interval_seconds: float = 60
    risk_interval_seconds: float = 5
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
