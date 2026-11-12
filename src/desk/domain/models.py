"""Handoff contracts between agents.

Each agent accepts only the previous agent's output type. The models are frozen,
and `TradeApproval` cannot be constructed without a fully rebutted debate, so an
unapproved buy is not representable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

REQUIRED_BEAR_REASONS = 3


def utcnow() -> datetime:
    return datetime.now(UTC)


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# --------------------------------------------------------------------------
# Scout -> Filter
# --------------------------------------------------------------------------


class Windowed(Frozen):
    """A metric reported over DexScreener's standard windows."""

    m5: float | None = None
    h1: float | None = None
    h6: float | None = None
    h24: float | None = None


class TxnCounts(Frozen):
    buys: int = 0
    sells: int = 0


class Candidate(Frozen):
    mint: str
    symbol: str
    name: str
    pair_address: str
    dex_id: str
    url: str
    pair_created_at: datetime
    price_usd: float
    sol_price_usd: float
    market_cap_usd: float
    fdv_usd: float | None = None
    liquidity_usd: float | None = None
    volume_usd: Windowed = Windowed()
    price_change_pct: Windowed = Windowed()
    txns_m5: TxnCounts = TxnCounts()
    txns_h1: TxnCounts = TxnCounts()
    txns_h6: TxnCounts = TxnCounts()
    txns_h24: TxnCounts = TxnCounts()
    observed_at: datetime = Field(default_factory=utcnow)

    def age_hours(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.pair_created_at).total_seconds() / 3600


# --------------------------------------------------------------------------
# Filter -> Debate
# --------------------------------------------------------------------------


class Holder(Frozen):
    owner: str
    pct: float
    insider: bool = False
    label: str | None = None  # e.g. "AMM", "LOCKER", "CREATOR"


class InsiderNetwork(Frozen):
    network_id: str
    wallets: int
    kind: str
    current_holding_pct: float | None = None


class RiskFlag(Frozen):
    name: str
    level: str
    description: str = ""


class TokenSafety(Frozen):
    """On-chain safety facts. `None` always means "could not be determined"."""

    mint: str
    token_program: str | None = None
    supply_raw: int | None = None
    mint_authority_revoked: bool | None = None
    freeze_authority_revoked: bool | None = None
    lp_locked_pct: float | None = None
    top10_holders_pct: float | None = None
    dev_holding_pct: float | None = None
    creator: str | None = None
    transfer_fee_pct: float | None = None
    dangerous_extensions: tuple[str, ...] = ()
    rugged: bool = False
    total_holders: int | None = None
    top_holders: tuple[Holder, ...] = ()
    insiders_detected: int | None = None
    insider_networks: tuple[InsiderNetwork, ...] = ()
    risk_flags: tuple[RiskFlag, ...] = ()
    rugcheck_score: float | None = None
    launchpad: str | None = None
    creator_prior_tokens: int | None = None


class RoundTrip(Frozen):
    """Result of quoting a buy and an immediate sell of the same size."""

    size_usd: float
    cost_pct: float
    buy_price_impact_pct: float | None = None
    sell_price_impact_pct: float | None = None


class KillRule(StrEnum):
    MINT_NOT_REVOKED = "mint_authority_not_revoked"
    FREEZE_NOT_REVOKED = "freeze_authority_not_revoked"
    LP_NOT_BURNED = "lp_not_burned"
    LOW_LIQUIDITY = "liquidity_below_minimum"
    TOP10_CONCENTRATION = "top10_holders_above_limit"
    DEV_HOLDING = "dev_holding_above_limit"
    TRANSFER_FEE = "token_has_transfer_fee"
    DANGEROUS_EXTENSION = "dangerous_token_extension"
    RUGGED = "flagged_as_rugged"
    NO_SELL_ROUTE = "no_sell_route"
    ROUND_TRIP_COST = "round_trip_cost_too_high"
    DATA_UNAVAILABLE = "data_unavailable"


class KillReason(Frozen):
    rule: KillRule
    detail: str


class FilterVerdict(Frozen):
    candidate: Candidate
    safety: TokenSafety | None = None
    round_trip: RoundTrip | None = None
    kills: tuple[KillReason, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.kills and self.safety is not None and self.round_trip is not None


# --------------------------------------------------------------------------
# Debate
# --------------------------------------------------------------------------


class Fact(Frozen):
    """One citable piece of data. Dossier facts are F1.., tool evidence is E1.."""

    id: str
    category: str
    statement: str


class Dossier(Frozen):
    candidate: Candidate
    safety: TokenSafety
    round_trip: RoundTrip
    facts: tuple[Fact, ...]

    def render(self) -> str:
        return "\n".join(f"[{f.id}] ({f.category}) {f.statement}" for f in self.facts)


class BearReason(BaseModel):
    title: str = Field(description="Short name for the risk.")
    claim: str = Field(description="The concrete on-chain reason not to buy, with numbers.")
    metric: str = Field(description="The single on-chain metric and value the claim rests on.")
    cited_facts: list[str] = Field(description="IDs of the facts relied on, e.g. ['F12', 'E2'].")


class BearCase(BaseModel):
    reasons: list[BearReason] = Field(
        description="Exactly three distinct reasons, strongest first."
    )


class Rebuttal(BaseModel):
    reason_number: int = Field(description="Which Bear reason this answers: 1, 2 or 3.")
    conceded: bool = Field(description="True if the data does not support a rebuttal.")
    argument: str = Field(description="The rebuttal, quoting numbers from the cited facts.")
    cited_facts: list[str] = Field(description="IDs of the facts relied on.")


class AnalystResponse(BaseModel):
    rebuttals: list[Rebuttal] = Field(description="One rebuttal per Bear reason, in order.")


class RulingVerdict(StrEnum):
    REBUTTED = "REBUTTED"
    UNREBUTTED = "UNREBUTTED"


class Ruling(BaseModel):
    reason_number: int
    verdict: RulingVerdict
    rationale: str


class RefereeDecision(BaseModel):
    rulings: list[Ruling] = Field(description="One ruling per Bear reason, in order.")


class LLMUsage(Frozen):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


class DebateOutcome(Frozen):
    dossier: Dossier
    evidence: tuple[Fact, ...] = ()
    reasons: tuple[BearReason, ...] = ()
    rebuttals: tuple[Rebuttal, ...] = ()
    rulings: tuple[Ruling, ...] = ()
    veto_reasons: tuple[str, ...] = ()
    usage: LLMUsage = LLMUsage()
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime = Field(default_factory=utcnow)

    @property
    def approved(self) -> bool:
        return (
            not self.veto_reasons
            and len(self.reasons) == REQUIRED_BEAR_REASONS
            and len(self.rulings) == REQUIRED_BEAR_REASONS
            and all(r.verdict is RulingVerdict.REBUTTED for r in self.rulings)
        )


# --------------------------------------------------------------------------
# Risk -> Executor
# --------------------------------------------------------------------------


class TradeApproval(Frozen):
    """Risk's sign-off. Only a fully rebutted debate can produce one."""

    outcome: DebateOutcome
    stop_loss_pct: float = Field(gt=0, lt=100)
    max_size_usd: float = Field(gt=0)
    approved_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _debate_must_be_won(self) -> TradeApproval:
        if not self.outcome.approved:
            raise ValueError("TradeApproval requires all three Bear reasons to be rebutted")
        return self

    @property
    def candidate(self) -> Candidate:
        return self.outcome.dossier.candidate


class Fill(Frozen):
    side: Literal["buy", "sell"]
    mint: str
    token_amount_raw: int
    usd_value: float  # what was paid (buy) or received (sell), fees included
    tx_id: str
    simulated: bool
    at: datetime = Field(default_factory=utcnow)


class ExitReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    MANUAL = "manual"


class Position(Frozen):
    id: int | None = None
    mint: str
    symbol: str
    pair_address: str
    token_amount_raw: int
    cost_usd: float
    entry_price_usd: float  # market price when the buy filled; stops key off this
    stop_price_usd: float
    high_water_price_usd: float
    last_price_usd: float
    trailing_active: bool = False
    opened_at: datetime = Field(default_factory=utcnow)
    closed_at: datetime | None = None
    proceeds_usd: float | None = None
    exit_price_usd: float | None = None
    exit_reason: ExitReason | None = None
    debate_id: int | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def pnl_usd(self) -> float | None:
        return None if self.proceeds_usd is None else self.proceeds_usd - self.cost_usd

    @property
    def pnl_pct(self) -> float | None:
        pnl = self.pnl_usd
        return None if pnl is None else 100 * pnl / self.cost_usd

    def value_at(self, price_usd: float) -> float:
        """Mark-to-market value, scaled from the entry price (decimals-agnostic)."""
        return self.cost_usd * price_usd / self.entry_price_usd
