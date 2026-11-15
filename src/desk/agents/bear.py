"""Bear: the devil's advocate. Up to three concrete reasons not to buy now."""

from __future__ import annotations

from collections.abc import Sequence

from desk.config import Settings
from desk.domain.models import BearCase, BearReason, Dossier
from desk.evidence import EvidenceLedger
from desk.llm.client import LLMResult, StructuredLLM, Tool, debate_prompt


def render_token_header(dossier: Dossier) -> str:
    candidate = dossier.candidate
    return f"{candidate.symbol} ({candidate.name}), mint {candidate.mint}"


def render_reasons(reasons: Sequence[BearReason]) -> str:
    return "\n\n".join(
        f"Reason {number}: {reason.title}\n"
        f"Claim: {reason.claim}\n"
        f"Metric: {reason.metric}\n"
        f"Cites: {', '.join(reason.cited_facts)}"
        for number, reason in enumerate(reasons, start=1)
    )


class Bear:
    def __init__(self, llm: StructuredLLM, settings: Settings) -> None:
        self._llm = llm
        self._system = debate_prompt("bear", settings)

    async def argue(
        self,
        dossier: Dossier,
        ledger: EvidenceLedger,
        tools: Sequence[Tool],
        lessons: Sequence[str],
    ) -> LLMResult[BearCase]:
        lesson_text = "\n".join(f"- {lesson}" for lesson in lessons) or "(none recorded yet)"
        user = (
            f"<token>{render_token_header(dossier)}</token>\n\n"
            f"<dossier>\n{dossier.render()}\n</dossier>\n\n"
            f"<evidence>\n{ledger.render()}\n</evidence>\n\n"
            f"<desk_lessons>\n{lesson_text}\n</desk_lessons>\n\n"
            "Give up to three reasons not to buy this token now, fewer if fewer are real."
        )
        return await self._llm.ask(
            system=self._system, user=user, output_type=BearCase, tools=tools
        )
