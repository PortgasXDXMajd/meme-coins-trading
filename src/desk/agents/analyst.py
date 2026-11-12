"""Analyst: rebuts each Bear reason with data, or concedes."""

from __future__ import annotations

from collections.abc import Sequence

from desk.agents.bear import render_reasons, render_token_header
from desk.config import Settings
from desk.domain.models import AnalystResponse, BearReason, Dossier
from desk.evidence import EvidenceLedger
from desk.llm.client import LLMResult, StructuredLLM, Tool, debate_prompt


class Analyst:
    def __init__(self, llm: StructuredLLM, settings: Settings) -> None:
        self._llm = llm
        self._system = debate_prompt("analyst", settings)

    async def rebut(
        self,
        dossier: Dossier,
        ledger: EvidenceLedger,
        tools: Sequence[Tool],
        reasons: Sequence[BearReason],
    ) -> LLMResult[AnalystResponse]:
        user = (
            f"<token>{render_token_header(dossier)}</token>\n\n"
            f"<dossier>\n{dossier.render()}\n</dossier>\n\n"
            f"<evidence>\n{ledger.render()}\n</evidence>\n\n"
            f"<bear_case>\n{render_reasons(reasons)}\n</bear_case>\n\n"
            "Rebut or concede each of the three reasons, in order."
        )
        return await self._llm.ask(
            system=self._system, user=user, output_type=AnalystResponse, tools=tools
        )
