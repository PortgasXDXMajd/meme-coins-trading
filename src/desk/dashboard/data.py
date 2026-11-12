"""Everything the dashboard shows, gathered in one read-only pass.

Nothing here writes to the database or the logs, so the dashboard can run
beside the desk without interfering with it.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from desk.config import Settings
from desk.domain.models import DebateOutcome, Position
from desk.report import RunStats, collect_stats
from desk.storage.db import DebateRecord, EvaluationRecord, LessonRecord, Store

# The desk logs a scout line every cycle; a debate can hold that up for a few minutes.
STALE_AFTER_SECONDS = 300
LOG_TAIL_BYTES = 200_000
SCOUT_LINE = re.compile(r"scout: (\d+) discovered, (\d+) watched, (\d+) in window")


@dataclass(frozen=True)
class ScoutPulse:
    discovered: int
    watched: int
    in_window: int


@dataclass(frozen=True)
class Heartbeat:
    """Whether the desk looks alive, judged by when it last wrote to its log."""

    seconds_since_activity: float | None

    @property
    def alive(self) -> bool:
        return (
            self.seconds_since_activity is not None
            and self.seconds_since_activity <= STALE_AFTER_SECONDS
        )


@dataclass(frozen=True)
class Snapshot:
    stats: RunStats
    open_positions: list[Position]
    closed_positions: list[Position]
    debates: list[DebateRecord]
    evaluations: list[EvaluationRecord]
    lessons: list[LessonRecord]
    scout: ScoutPulse | None
    heartbeat: Heartbeat
    halted: bool
    log_tail: list[str]


def tail_lines(path: Path, limit: int) -> list[str]:
    """The last `limit` lines of a text file, without reading all of a large one."""
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - LOG_TAIL_BYTES))
        text = handle.read().decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > LOG_TAIL_BYTES:
        lines = lines[1:]  # the first line is probably cut in half
    return lines[-limit:]


def latest_scout_pulse(lines: Sequence[str]) -> ScoutPulse | None:
    for line in reversed(lines):
        match = SCOUT_LINE.search(line)
        if match:
            discovered, watched, in_window = (int(group) for group in match.groups())
            return ScoutPulse(discovered, watched, in_window)
    return None


def read_heartbeat(log_path: Path, now: datetime) -> Heartbeat:
    if not log_path.exists():
        return Heartbeat(None)
    modified = datetime.fromtimestamp(log_path.stat().st_mtime, tz=UTC)
    return Heartbeat(max(0.0, (now - modified).total_seconds()))


def kill_rule_counts(evaluations: Sequence[EvaluationRecord]) -> dict[str, int]:
    """How many Filter kills each rule caused, most frequent first."""
    counts: Counter[str] = Counter()
    for evaluation in evaluations:
        for kill in evaluation.detail.get("kills", []):
            counts[kill.split(":", 1)[0].strip()] += 1
    return dict(counts.most_common())


def decision_reasons(evaluation: EvaluationRecord) -> list[str]:
    """Why a token stopped where it did: its Filter kills or its debate vetoes."""
    return list(evaluation.detail.get("kills") or evaluation.detail.get("veto_reasons") or [])


def realised_equity_curve(
    closed: Sequence[Position], starting_usd: float
) -> list[tuple[datetime, float]]:
    """Equity after each closed trade, counting realised P&L only."""
    equity = starting_usd
    curve = []
    for position in sorted(closed, key=lambda p: p.closed_at or p.opened_at):
        equity += position.pnl_usd or 0.0
        curve.append((position.closed_at or position.opened_at, equity))
    return curve


def load_snapshot(settings: Settings, since: datetime, now: datetime) -> Snapshot | None:
    """Read the desk's current state, or None if the desk has never run here."""
    if not settings.db_path.exists():
        return None
    try:
        store = Store(settings.db_path, read_only=True)
    except sqlite3.OperationalError:
        return None
    try:
        log_tail = tail_lines(settings.log_dir / "desk.log", limit=400)
        return Snapshot(
            stats=collect_stats(store, settings, since),
            open_positions=store.open_positions(),
            closed_positions=store.closed_positions(since),
            debates=store.debates_since(since),
            evaluations=store.evaluations_since(since),
            lessons=store.lessons(),
            scout=latest_scout_pulse(log_tail),
            heartbeat=read_heartbeat(settings.log_dir / "desk.log", now),
            halted=settings.halt_file.exists(),
            log_tail=log_tail,
        )
    finally:
        store.close()


def load_debate(settings: Settings, debate_id: int) -> DebateOutcome | None:
    store = Store(settings.db_path, read_only=True)
    try:
        return store.debate_outcome(debate_id)
    finally:
        store.close()
