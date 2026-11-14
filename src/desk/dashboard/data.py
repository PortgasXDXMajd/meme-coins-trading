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

from pydantic import ValidationError

from desk.config import Settings
from desk.domain.models import DebateOutcome, Position, RulingVerdict
from desk.report import RunStats, collect_stats
from desk.storage.db import (
    DebateRecord,
    EvaluationRecord,
    LessonRecord,
    PriceCheck,
    Store,
)

# The desk logs a scout line every cycle; a debate can hold that up for a few minutes.
STALE_AFTER_SECONDS = 300
LOG_TAIL_BYTES = 1_500_000
SCOUT_LINE = re.compile(r"scout: (\d+) discovered, (\d+) watched, (\d+) in window")
VETO_LINE = re.compile(r"^Reason (\d+) \((.*?)\) left unrebutted")
MISSING_DATA = "data_unavailable"

# Filter rules in plain words, for people who don't read the code.
RULE_LABELS = {
    "data_unavailable": "Missing data",
    "liquidity_below_minimum": "Pool too small",
    "lp_not_burned": "Liquidity not locked",
    "top10_holders_above_limit": "Top 10 wallets hold too much",
    "dev_holding_above_limit": "Creator holds too much",
    "mint_authority_not_revoked": "Creator can mint more",
    "freeze_authority_not_revoked": "Creator can freeze wallets",
    "token_has_transfer_fee": "Has a transfer fee",
    "dangerous_token_extension": "Dangerous token feature",
    "flagged_as_rugged": "Flagged as a rug",
    "no_sell_route": "Cannot be sold",
    "round_trip_cost_too_high": "Too costly to trade",
}


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


def rule_label(rule: str) -> str:
    return RULE_LABELS.get(rule, rule.replace("_", " ").capitalize())


@dataclass(frozen=True)
class Kill:
    rule: str
    detail: str

    @property
    def label(self) -> str:
        return rule_label(self.rule)


def kills_of(evaluation: EvaluationRecord) -> list[Kill]:
    """A Filter rejection's reasons. They are stored as "rule: detail"."""
    kills = []
    for raw in evaluation.detail.get("kills", []):
        rule, _, detail = str(raw).partition(":")
        kills.append(Kill(rule.strip(), detail.strip() or rule.strip()))
    return kills


def is_missing_data(evaluation: EvaluationRecord) -> bool:
    """True when the Filter rejected a token only because some data could not be read."""
    kills = kills_of(evaluation)
    return bool(kills) and all(kill.rule == MISSING_DATA for kill in kills)


@dataclass(frozen=True)
class TokenRejections:
    """Every Filter rejection of one token, folded into one row."""

    mint: str
    symbol: str
    checks: int
    rules: tuple[str, ...]  # most frequent first
    latest_detail: str
    first_at: datetime
    last_at: datetime
    recheck_at: datetime

    @property
    def missing_data_only(self) -> bool:
        return self.rules == (MISSING_DATA,)


def rejections_by_token(evaluations: Sequence[EvaluationRecord]) -> list[TokenRejections]:
    """Filter rejections grouped per token, most recently rejected first."""
    grouped: dict[str, list[EvaluationRecord]] = {}
    for evaluation in evaluations:
        if evaluation.outcome == "filtered":
            grouped.setdefault(evaluation.mint, []).append(evaluation)
    rows = []
    for mint, checks in grouped.items():
        checks.sort(key=lambda e: e.at)
        rules: Counter[str] = Counter(kill.rule for e in checks for kill in kills_of(e))
        latest = checks[-1]
        rows.append(
            TokenRejections(
                mint=mint,
                symbol=latest.symbol,
                checks=len(checks),
                rules=tuple(rule for rule, _ in rules.most_common()),
                latest_detail=" · ".join(kill.detail for kill in kills_of(latest)),
                first_at=checks[0].at,
                last_at=latest.at,
                recheck_at=latest.cooldown_until,
            )
        )
    return sorted(rows, key=lambda row: row.last_at, reverse=True)


@dataclass(frozen=True)
class Funnel:
    """How many different tokens reached each stage."""

    checked: int
    rejected: int
    debated: int
    approved: int
    bought: int


def token_funnel(snapshot: Snapshot) -> Funnel:
    checked = {e.mint for e in snapshot.evaluations}
    debated = {d.mint for d in snapshot.debates}
    rejected = {e.mint for e in snapshot.evaluations if e.outcome == "filtered"} - debated
    approved = {d.mint for d in snapshot.debates if d.approved}
    bought = {p.mint for p in [*snapshot.open_positions, *snapshot.closed_positions]}
    return Funnel(len(checked | debated), len(rejected), len(debated), len(approved), len(bought))


def stood_titles(outcome: DebateOutcome) -> list[str]:
    """The Bear reasons that stood and vetoed the buy, by title."""
    titles = []
    for line in outcome.veto_reasons:
        match = VETO_LINE.match(line)
        titles.append(match.group(2) if match else line)
    if not titles:
        by_number = {n: r.title for n, r in enumerate(outcome.reasons, start=1)}
        titles = [
            by_number.get(ruling.reason_number, f"Reason {ruling.reason_number}")
            for ruling in outcome.rulings
            if ruling.verdict is RulingVerdict.UNREBUTTED
        ]
    return titles


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
        log_tail = tail_lines(settings.log_dir / "desk.log", limit=8000)
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


# Debates never change once stored, so each is parsed once per dashboard process.
_debate_cache: dict[tuple[str, int], DebateOutcome | None] = {}


def load_debates(settings: Settings, debate_ids: Sequence[int]) -> dict[int, DebateOutcome | None]:
    """Stored debates by id. None for a record that no longer parses."""
    db = str(settings.db_path)
    missing = [i for i in debate_ids if (db, i) not in _debate_cache]
    if missing:
        store = Store(settings.db_path, read_only=True)
        try:
            for debate_id in missing:
                try:
                    _debate_cache[(db, debate_id)] = store.debate_outcome(debate_id)
                except ValidationError:
                    _debate_cache[(db, debate_id)] = None
        finally:
            store.close()
    return {i: _debate_cache[(db, i)] for i in debate_ids}


def load_price_checks(settings: Settings, position_id: int) -> list[PriceCheck]:
    """The stop monitor's record for one position. Empty for trades from before it existed."""
    store = Store(settings.db_path, read_only=True)
    try:
        return store.price_checks(position_id)
    except sqlite3.OperationalError:
        return []
    finally:
        store.close()
