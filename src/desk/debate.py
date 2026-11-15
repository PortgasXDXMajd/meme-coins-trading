"""The debate: Bear argues, Analyst rebuts, Risk rules.

The Bear gives one to three reasons. The trade is approved when no more than
`max_unrebutted` of them are left standing; reasons the Bear did not give count as
answered. Every failure along the way (an LLM error, an invalid case) fails closed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Protocol

from desk.agents.analyst import Analyst
from desk.agents.bear import Bear
from desk.domain.models import (
    MAX_BEAR_REASONS,
    BearReason,
    DebateOutcome,
    Dossier,
    Fact,
    LLMUsage,
    Rebuttal,
    RefereeDecision,
    Ruling,
    RulingVerdict,
    utcnow,
)
from desk.evidence import EvidenceLedger
from desk.llm.client import LLMFailure, LLMResult, Tool

log = logging.getLogger(__name__)

ToolFactory = Callable[[Dossier, EvidenceLedger], list[Tool]]


class Referee(Protocol):
    async def rule(
        self,
        dossier: Dossier,
        evidence: Sequence[Fact],
        reasons: Sequence[BearReason],
        rebuttals: Sequence[Rebuttal],
    ) -> LLMResult[RefereeDecision]: ...


def normalise_citations(cited: Sequence[str]) -> list[str]:
    return [c.strip().strip("[]()").upper() for c in cited if c.strip()]


def citation_problem(cited: Sequence[str], known_ids: set[str]) -> str | None:
    ids = normalise_citations(cited)
    if not ids:
        return "cites no facts"
    unknown = [i for i in ids if i not in known_ids]
    if unknown:
        return f"cites facts that do not exist: {', '.join(unknown)}"
    return None


def bear_case_problem(reasons: Sequence[BearReason], known_ids: set[str]) -> str | None:
    if not reasons:
        return "gave no reasons"
    if len(reasons) > MAX_BEAR_REASONS:
        return f"gave {len(reasons)} reasons, more than {MAX_BEAR_REASONS}"
    for number, reason in enumerate(reasons, start=1):
        problem = citation_problem(reason.cited_facts, known_ids)
        if problem:
            return f"reason {number} {problem}"
    return None


def automatic_rulings(
    rebuttals: dict[int, Rebuttal], known_ids: set[str], reason_count: int
) -> list[Ruling]:
    """Rulings that need no judgement: missing, conceded or uncited rebuttals lose."""
    rulings = []
    for number in range(1, reason_count + 1):
        rebuttal = rebuttals.get(number)
        if rebuttal is None:
            why = "the Analyst gave no rebuttal"
        elif rebuttal.conceded:
            why = "the Analyst conceded"
        else:
            problem = citation_problem(rebuttal.cited_facts, known_ids)
            why = f"the rebuttal {problem}" if problem else None
        if why:
            rulings.append(
                Ruling(
                    reason_number=number,
                    verdict=RulingVerdict.UNREBUTTED,
                    rationale=f"Automatic: {why}.",
                )
            )
    return rulings


def complete_rulings(
    decision: RefereeDecision, reason_count: int, automatic: Sequence[Ruling] = ()
) -> list[Ruling]:
    """Exactly one ruling per reason. Automatic rulings win over the referee's, and
    anything the referee skipped counts as UNREBUTTED."""
    by_number: dict[int, Ruling] = {r.reason_number: r for r in automatic}
    for ruling in decision.rulings:
        by_number.setdefault(ruling.reason_number, ruling)
    return [
        by_number.get(
            number,
            Ruling(
                reason_number=number,
                verdict=RulingVerdict.UNREBUTTED,
                rationale="Automatic: the referee gave no ruling.",
            ),
        )
        for number in range(1, reason_count + 1)
    ]


class Debate:
    def __init__(
        self,
        bear: Bear,
        analyst: Analyst,
        referee: Referee,
        tool_factory: ToolFactory,
        lessons: Callable[[], Sequence[str]] = lambda: (),
        max_unrebutted: int = 0,
    ) -> None:
        self._bear = bear
        self._analyst = analyst
        self._referee = referee
        self._tool_factory = tool_factory
        self._lessons = lessons
        self._max_unrebutted = max_unrebutted

    async def run(self, dossier: Dossier) -> DebateOutcome:
        started_at = utcnow()
        ledger = EvidenceLedger()
        tools = self._tool_factory(dossier, ledger)
        usage = LLMUsage()
        reasons: tuple[BearReason, ...] = ()
        rebuttals: tuple[Rebuttal, ...] = ()
        rulings: tuple[Ruling, ...] = ()

        def finish(*veto_reasons: str) -> DebateOutcome:
            return DebateOutcome(
                dossier=dossier,
                evidence=ledger.facts,
                reasons=reasons,
                rebuttals=rebuttals,
                rulings=rulings,
                veto_reasons=veto_reasons,
                usage=usage,
                started_at=started_at,
                finished_at=utcnow(),
                max_unrebutted=self._max_unrebutted,
            )

        def known_ids() -> set[str]:
            return {f.id for f in dossier.facts} | {f.id for f in ledger.facts}

        # 1. Bear
        try:
            bear = await self._bear.argue(dossier, ledger, tools, self._lessons())
        except LLMFailure as exc:
            return finish(f"Bear could not make its case: {exc}")
        usage += bear.usage
        reasons = tuple(bear.output.reasons)
        problem = bear_case_problem(reasons, known_ids())
        if problem:
            return finish(f"Bear's case was invalid ({problem}); no trade without a tested case")

        # 2. Analyst
        try:
            analyst = await self._analyst.rebut(dossier, ledger, tools, reasons)
        except LLMFailure as exc:
            return finish(f"Analyst could not respond, so nothing was rebutted: {exc}")
        usage += analyst.usage
        by_number: dict[int, Rebuttal] = {}
        for rebuttal in analyst.output.rebuttals:
            by_number.setdefault(rebuttal.reason_number, rebuttal)
        rebuttals = tuple(by_number[n] for n in sorted(by_number))

        automatic = automatic_rulings(by_number, known_ids(), len(reasons))
        if len(automatic) > self._max_unrebutted or len(automatic) == len(reasons):
            # Already decided: too many reasons stand, or there is nothing left to judge.
            rulings = tuple(automatic)
            return finish(*self._veto(reasons, rulings))

        # 3. Risk referee
        try:
            decision = await self._referee.rule(dossier, ledger.facts, reasons, rebuttals)
        except LLMFailure as exc:
            return finish(f"Risk could not rule on the debate: {exc}")
        usage += decision.usage
        rulings = tuple(complete_rulings(decision.output, len(reasons), automatic))
        return finish(*self._veto(reasons, rulings))

    def _veto(self, reasons: Sequence[BearReason], rulings: Sequence[Ruling]) -> list[str]:
        """The veto lines when too many reasons stand; none when the trade survives."""
        standing = self._unrebutted(reasons, rulings)
        return standing if len(standing) > self._max_unrebutted else []

    @staticmethod
    def _unrebutted(reasons: Sequence[BearReason], rulings: Sequence[Ruling]) -> list[str]:
        return [
            f"Reason {r.reason_number} ({reasons[r.reason_number - 1].title}) "
            f"left unrebutted: {r.rationale}"
            for r in rulings
            if r.verdict is RulingVerdict.UNREBUTTED
        ]
