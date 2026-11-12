"""Shared builders and fakes. No test touches the network or the Claude API."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr
from pydantic_settings import SettingsConfigDict

from desk.config import Settings
from desk.domain.models import (
    AnalystResponse,
    BearCase,
    BearReason,
    Candidate,
    Fill,
    FilterVerdict,
    LLMUsage,
    Rebuttal,
    RefereeDecision,
    RoundTrip,
    Ruling,
    RulingVerdict,
    TokenSafety,
)
from desk.llm.client import LLMFailure, LLMResult, Tool
from desk.storage.db import Store

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 12, 18, 0, tzinfo=UTC)


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class IsolatedSettings(Settings):
    """Settings that never read the developer's real .env file."""

    model_config = SettingsConfigDict(env_file=None)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return IsolatedSettings(
        anthropic_api_key=SecretStr("test-key"),
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )


class CashOnlyBroker:
    """A broker for tests that only need a balance; it refuses to trade."""

    async def cash_usd(self, sol_price_usd: float) -> float:
        return 250.0

    async def buy(self, mint: str, usd: float, sol_price_usd: float) -> Fill:
        raise AssertionError("this test should not buy")

    async def sell(self, mint: str, token_amount_raw: int, sol_price_usd: float) -> Fill:
        raise AssertionError("this test should not sell")


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


def make_candidate(**overrides: Any) -> Candidate:
    defaults: dict[str, Any] = {
        "mint": "Mint111111111111111111111111111111111111111",
        "symbol": "TEST",
        "name": "Test Token",
        "pair_address": "Pair11111111111111111111111111111111111111",
        "dex_id": "pumpswap",
        "url": "https://dexscreener.com/solana/test",
        "pair_created_at": NOW - timedelta(hours=2),
        "price_usd": 0.001,
        "sol_price_usd": 80.0,
        "market_cap_usd": 1_000_000,
        "liquidity_usd": 60_000,
        "observed_at": NOW,
    }
    return Candidate(**(defaults | overrides))


def make_safety(**overrides: Any) -> TokenSafety:
    defaults: dict[str, Any] = {
        "mint": "Mint111111111111111111111111111111111111111",
        "supply_raw": 1_000_000_000_000_000,
        "mint_authority_revoked": True,
        "freeze_authority_revoked": True,
        "lp_locked_pct": 100.0,
        "top10_holders_pct": 18.0,
        "dev_holding_pct": 0.0,
        "creator": "Creator1111111111111111111111111111111111111",
        "transfer_fee_pct": 0.0,
    }
    return TokenSafety(**(defaults | overrides))


def make_verdict(**candidate_overrides: Any) -> FilterVerdict:
    return FilterVerdict(
        candidate=make_candidate(**candidate_overrides),
        safety=make_safety(),
        round_trip=RoundTrip(size_usd=20, cost_pct=2.0),
    )


def bear_case(cited: Sequence[str] = ("F1",), count: int = 3) -> BearCase:
    return BearCase(
        reasons=[
            BearReason(
                title=f"Risk {n}", claim=f"Claim {n}", metric=f"Metric {n}", cited_facts=list(cited)
            )
            for n in range(1, count + 1)
        ]
    )


def analyst_response(
    conceded: Sequence[int] = (), cited: Sequence[str] = ("F2",), skip: Sequence[int] = ()
) -> AnalystResponse:
    return AnalystResponse(
        rebuttals=[
            Rebuttal(
                reason_number=n,
                conceded=n in conceded,
                argument=f"Rebuttal {n}",
                cited_facts=list(cited),
            )
            for n in (1, 2, 3)
            if n not in skip
        ]
    )


def referee_decision(unrebutted: Sequence[int] = (), skip: Sequence[int] = ()) -> RefereeDecision:
    return RefereeDecision(
        rulings=[
            Ruling(
                reason_number=n,
                verdict=RulingVerdict.UNREBUTTED if n in unrebutted else RulingVerdict.REBUTTED,
                rationale=f"Rationale {n}",
            )
            for n in (1, 2, 3)
            if n not in skip
        ]
    )


class FakeLLM:
    """Answers each question with the scripted object of the requested type."""

    def __init__(self, *answers: BaseModel, failing: Sequence[type] = ()) -> None:
        self._answers = answers
        self._failing = tuple(failing)
        self.calls: list[str] = []

    async def ask(
        self, *, system: str, user: str, output_type: type, tools: Sequence[Tool] = ()
    ) -> LLMResult:
        self.calls.append(output_type.__name__)
        if output_type in self._failing:
            raise LLMFailure(f"scripted failure for {output_type.__name__}")
        for answer in self._answers:
            if isinstance(answer, output_type):
                return LLMResult(output=answer, usage=LLMUsage(cost_usd=0.1))
        raise LLMFailure(f"no scripted answer for {output_type.__name__}")
