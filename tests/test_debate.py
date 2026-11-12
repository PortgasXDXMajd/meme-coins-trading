"""The debate: only three REBUTTED rulings approve; everything else is a veto."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from desk.agents.analyst import Analyst
from desk.agents.bear import Bear
from desk.agents.risk import RiskOfficer
from desk.debate import Debate
from desk.domain.models import (
    AnalystResponse,
    BearCase,
    DebateOutcome,
    RefereeDecision,
    RulingVerdict,
    TradeApproval,
)
from desk.dossier import build_dossier
from desk.portfolio import Portfolio
from desk.storage.debate_log import render_transcript
from tests.conftest import (
    NOW,
    CashOnlyBroker,
    FakeLLM,
    analyst_response,
    bear_case,
    make_verdict,
    referee_decision,
)


@pytest.fixture
def dossier(settings):
    return build_dossier(make_verdict(), settings, NOW)


def make_debate(llm: FakeLLM, settings, store) -> Debate:
    referee = RiskOfficer(llm, Portfolio(CashOnlyBroker(), store), store, settings)
    return Debate(
        Bear(llm, settings),
        Analyst(llm, settings),
        referee,
        tool_factory=lambda dossier, ledger: [],
    )


async def run(llm: FakeLLM, dossier, settings, store) -> DebateOutcome:
    return await make_debate(llm, settings, store).run(dossier)


async def test_all_three_rebutted_is_approved(dossier, settings, store):
    llm = FakeLLM(bear_case(), analyst_response(), referee_decision())
    outcome = await run(llm, dossier, settings, store)
    assert outcome.approved
    assert outcome.veto_reasons == ()
    assert llm.calls == ["BearCase", "AnalystResponse", "RefereeDecision"]
    assert outcome.usage.cost_usd == pytest.approx(0.3)


@pytest.mark.parametrize("unrebutted", [(1,), (2,), (3,), (1, 2, 3)])
async def test_any_unrebutted_reason_is_a_veto(dossier, settings, store, unrebutted):
    llm = FakeLLM(bear_case(), analyst_response(), referee_decision(unrebutted=unrebutted))
    outcome = await run(llm, dossier, settings, store)
    assert not outcome.approved
    assert len(outcome.veto_reasons) == len(unrebutted)


async def test_conceded_reason_vetoes_without_asking_the_referee(dossier, settings, store):
    llm = FakeLLM(bear_case(), analyst_response(conceded=(2,)), referee_decision())
    outcome = await run(llm, dossier, settings, store)
    assert not outcome.approved
    assert "conceded" in outcome.veto_reasons[0]
    assert "RefereeDecision" not in llm.calls


async def test_missing_rebuttal_is_a_veto(dossier, settings, store):
    llm = FakeLLM(bear_case(), analyst_response(skip=(3,)), referee_decision())
    outcome = await run(llm, dossier, settings, store)
    assert not outcome.approved
    assert "no rebuttal" in outcome.veto_reasons[0]


@pytest.mark.parametrize("cited", [(), ("F999",), ("made-up",)])
async def test_rebuttal_without_real_data_is_a_veto(dossier, settings, store, cited):
    llm = FakeLLM(bear_case(), analyst_response(cited=cited), referee_decision())
    outcome = await run(llm, dossier, settings, store)
    assert not outcome.approved
    assert len(outcome.veto_reasons) == 3


async def test_citations_are_matched_loosely_on_format(dossier, settings, store):
    llm = FakeLLM(bear_case(cited=("[f1]",)), analyst_response(cited=(" F2 ",)), referee_decision())
    assert (await run(llm, dossier, settings, store)).approved


@pytest.mark.parametrize("count", [0, 2, 4])
async def test_bear_must_give_exactly_three_reasons(dossier, settings, store, count):
    llm = FakeLLM(bear_case(count=count), analyst_response(), referee_decision())
    outcome = await run(llm, dossier, settings, store)
    assert not outcome.approved
    assert llm.calls == ["BearCase"]


async def test_bear_citing_nonexistent_facts_is_a_veto(dossier, settings, store):
    llm = FakeLLM(bear_case(cited=("F404",)), analyst_response(), referee_decision())
    assert not (await run(llm, dossier, settings, store)).approved


async def test_referee_skipping_a_ruling_is_a_veto(dossier, settings, store):
    llm = FakeLLM(bear_case(), analyst_response(), referee_decision(skip=(2,)))
    outcome = await run(llm, dossier, settings, store)
    assert not outcome.approved
    assert outcome.rulings[1].verdict is RulingVerdict.UNREBUTTED


@pytest.mark.parametrize("failing", [BearCase, AnalystResponse, RefereeDecision])
async def test_any_llm_failure_fails_closed(dossier, settings, store, failing):
    llm = FakeLLM(bear_case(), analyst_response(), referee_decision(), failing=[failing])
    outcome = await run(llm, dossier, settings, store)
    assert not outcome.approved
    assert outcome.veto_reasons


async def test_trade_approval_cannot_be_built_from_a_veto(dossier, settings, store):
    llm = FakeLLM(bear_case(), analyst_response(), referee_decision(unrebutted=(1,)))
    vetoed = await run(llm, dossier, settings, store)
    with pytest.raises(ValidationError):
        TradeApproval(outcome=vetoed, stop_loss_pct=6, max_size_usd=20)

    referee = RiskOfficer(llm, Portfolio(CashOnlyBroker(), store), store, settings)
    assert await referee.approve(vetoed) is None


async def test_risk_sets_the_stop_before_entry(dossier, settings, store):
    llm = FakeLLM(bear_case(), analyst_response(), referee_decision())
    outcome = await run(llm, dossier, settings, store)
    referee = RiskOfficer(llm, Portfolio(CashOnlyBroker(), store), store, settings)
    approval = await referee.approve(outcome)
    assert approval is not None
    assert approval.stop_loss_pct == 6
    assert approval.max_size_usd == pytest.approx(20)  # 8% of $250


async def test_transcript_logs_the_full_debate(dossier, settings, store):
    llm = FakeLLM(bear_case(), analyst_response(), referee_decision(unrebutted=(2,)))
    text = render_transcript(await run(llm, dossier, settings, store))
    assert "# TEST — VETO" in text
    for expected in ("Claim 1", "Claim 3", "Rebuttal 2", "Rationale 3", "UNREBUTTED", "[F1]"):
        assert expected in text


def test_dossier_refuses_a_killed_candidate(settings):
    from desk.domain.models import FilterVerdict
    from tests.conftest import make_candidate

    with pytest.raises(ValueError):
        build_dossier(FilterVerdict(candidate=make_candidate()), settings, NOW)
