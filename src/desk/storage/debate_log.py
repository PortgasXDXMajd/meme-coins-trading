"""Human-readable debate transcripts and a machine-readable event stream."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from desk.domain.models import DebateOutcome, utcnow


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", text)[:24] or "token"


def render_transcript(outcome: DebateOutcome) -> str:
    candidate = outcome.dossier.candidate
    verdict = "APPROVED" if outcome.approved else "VETO"
    lines = [
        f"# {candidate.symbol} — {verdict}",
        "",
        f"- Token: {candidate.name} (`{candidate.mint}`)",
        f"- Chart: {candidate.url}",
        f"- Debated: {outcome.started_at:%Y-%m-%d %H:%M:%S} UTC "
        f"({(outcome.finished_at - outcome.started_at).total_seconds():.0f}s)",
        f"- LLM cost: ${outcome.usage.cost_usd:.3f} "
        f"({outcome.usage.input_tokens:,} in / {outcome.usage.output_tokens:,} out)",
        "",
    ]
    if outcome.veto_reasons:
        lines += ["## Why it was vetoed", ""]
        lines += [f"- {reason}" for reason in outcome.veto_reasons]
        lines.append("")

    rebuttals = {r.reason_number: r for r in outcome.rebuttals}
    rulings = {r.reason_number: r for r in outcome.rulings}
    for number, reason in enumerate(outcome.reasons, start=1):
        lines += [
            f"## Reason {number}: {reason.title}",
            "",
            f"**Bear** (cites {', '.join(reason.cited_facts) or 'nothing'}): {reason.claim}",
            "",
            f"*Metric:* {reason.metric}",
            "",
        ]
        rebuttal = rebuttals.get(number)
        if rebuttal is None:
            lines += ["**Analyst:** no rebuttal given.", ""]
        else:
            stance = "CONCEDED — " if rebuttal.conceded else ""
            lines += [
                f"**Analyst** (cites {', '.join(rebuttal.cited_facts) or 'nothing'}): "
                f"{stance}{rebuttal.argument}",
                "",
            ]
        ruling = rulings.get(number)
        if ruling is not None:
            lines += [f"**Risk ruling: {ruling.verdict.value}** — {ruling.rationale}", ""]

    lines += ["## Dossier", "", "```", outcome.dossier.render(), "```", ""]
    if outcome.evidence:
        lines += ["## Evidence gathered during the debate", "", "```"]
        lines += [f"[{fact.id}] ({fact.category}) {fact.statement}" for fact in outcome.evidence]
        lines += ["```", ""]
    return "\n".join(lines)


def write_transcript(outcome: DebateOutcome, log_dir: Path) -> Path:
    candidate = outcome.dossier.candidate
    folder = log_dir / "debates" / f"{outcome.started_at:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{outcome.started_at:%H%M%S}-{_safe(candidate.symbol)}-{candidate.mint[:8]}.md"
    path = folder / name
    path.write_text(render_transcript(outcome), encoding="utf-8")
    return path


class EventLog:
    """Append-only JSON lines: one record per thing the desk did."""

    def __init__(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self._path = log_dir / "events.jsonl"

    def emit(self, event: str, **fields: Any) -> None:
        record = {"at": utcnow().isoformat(), "event": event, **fields}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=_json_default) + "\n")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)
