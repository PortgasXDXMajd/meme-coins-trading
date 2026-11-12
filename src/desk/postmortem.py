"""After a position closes, Claude reviews the debate against the outcome and
records one lesson. Recent lessons are shown to the Bear in later debates.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from desk.domain.models import Position
from desk.llm.client import LLMFailure, StructuredLLM, load_prompt
from desk.storage.db import Store

log = logging.getLogger(__name__)


class Lesson(BaseModel):
    lesson: str = Field(description="At most two sentences, specific to this trade.")


def describe_outcome(position: Position) -> str:
    held_minutes = (
        (position.closed_at - position.opened_at).total_seconds() / 60 if position.closed_at else 0
    )
    peak_pct = 100 * (position.high_water_price_usd / position.entry_price_usd - 1)
    exit_pct = 100 * ((position.exit_price_usd or 0) / position.entry_price_usd - 1)
    return (
        f"Bought ${position.cost_usd:.2f}, sold for ${position.proceeds_usd or 0:.2f} "
        f"({position.pnl_pct or 0:+.1f}% after costs). Exit: "
        f"{position.exit_reason.value if position.exit_reason else 'unknown'} after "
        f"{held_minutes:.0f} minutes. Market price peaked {peak_pct:+.1f}% above entry "
        f"and was {exit_pct:+.1f}% vs entry at exit."
    )


class PostMortem:
    def __init__(self, llm: StructuredLLM, store: Store) -> None:
        self._llm = llm
        self._store = store
        self._system = load_prompt("postmortem")

    async def review(self, position: Position) -> None:
        transcript = self._transcript(position)
        if transcript is None:
            return
        user = (
            f"<outcome>\n{describe_outcome(position)}\n</outcome>\n\n"
            f"<debate_transcript>\n{transcript}\n</debate_transcript>\n\n"
            "Write the lesson."
        )
        try:
            result = await self._llm.ask(system=self._system, user=user, output_type=Lesson)
        except LLMFailure as exc:
            log.warning("post-mortem for %s failed: %s", position.symbol, exc)
            return
        self._store.add_lesson(position.symbol, result.output.lesson.strip())
        log.info("lesson (%s): %s", position.symbol, result.output.lesson.strip())

    def _transcript(self, position: Position) -> str | None:
        if position.debate_id is None:
            return None
        path = self._store.debate_transcript_path(position.debate_id)
        if path is None or not Path(path).exists():
            return None
        return Path(path).read_text(encoding="utf-8")
