"""SQLite persistence: positions survive restarts, every decision is queryable."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from desk.domain.models import Clearance, DebateOutcome, ExitReason, Position, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL,
    cooldown_until TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluations_mint ON evaluations (mint, cooldown_until);
CREATE TABLE IF NOT EXISTS debates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    approved INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    transcript_path TEXT NOT NULL,
    outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clearances (
    mint TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    is_open INTEGER NOT NULL,
    closed_at TEXT,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_checks_position ON price_checks (position_id);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    text TEXT NOT NULL
);
"""


class DebateRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    at: datetime
    mint: str
    symbol: str
    approved: bool
    cost_usd: float
    transcript_path: str


class EvaluationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    at: datetime
    mint: str
    symbol: str
    outcome: str
    detail: dict[str, Any]
    cooldown_until: datetime


class LessonRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    at: datetime
    symbol: str
    text: str


class PriceCheck(BaseModel):
    """One stop-monitor pass over one position: what each source said and what was used."""

    model_config = ConfigDict(frozen=True)

    at: datetime
    position_id: int
    symbol: str
    source: Literal["quote", "dex"]
    used_price_usd: float
    quote_price_usd: float | None
    dex_price_usd: float | None
    stop_price_usd: float
    high_water_price_usd: float
    trailing_active: bool
    exit_reason: ExitReason | None = None


class Store:
    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        """`read_only` opens an existing database without ever writing to it."""
        if read_only:
            self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
        else:
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if not read_only:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # --- key/value -----------------------------------------------------------

    def get_float(self, key: str) -> float | None:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else float(row["value"])

    def set_float(self, key: str, value: float) -> None:
        self.set_text(key, repr(value))

    def get_text(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_text(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --- evaluations and cooldowns -------------------------------------------

    def record_evaluation(
        self,
        mint: str,
        symbol: str,
        outcome: str,
        detail: dict[str, Any],
        cooldown_until: datetime,
    ) -> None:
        self._db.execute(
            "INSERT INTO evaluations (at, mint, symbol, outcome, detail, cooldown_until) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                utcnow().isoformat(),
                mint,
                symbol,
                outcome,
                json.dumps(detail),
                cooldown_until.isoformat(),
            ),
        )

    def on_cooldown(self, mint: str, now: datetime) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM evaluations WHERE mint = ? AND cooldown_until > ? LIMIT 1",
            (mint, now.isoformat()),
        ).fetchone()
        return row is not None

    def evaluation_counts(self, since: datetime) -> dict[str, int]:
        rows = self._db.execute(
            "SELECT outcome, COUNT(*) AS n FROM evaluations WHERE at >= ? GROUP BY outcome",
            (since.isoformat(),),
        ).fetchall()
        return {row["outcome"]: row["n"] for row in rows}

    def evaluations_since(self, since: datetime) -> list[EvaluationRecord]:
        rows = self._db.execute(
            "SELECT at, mint, symbol, outcome, detail, cooldown_until "
            "FROM evaluations WHERE at >= ? ORDER BY at",
            (since.isoformat(),),
        ).fetchall()
        return [
            EvaluationRecord(**{**dict(row), "detail": json.loads(row["detail"])}) for row in rows
        ]

    # --- debates -------------------------------------------------------------

    def save_debate(self, outcome: DebateOutcome, transcript_path: Path) -> int:
        candidate = outcome.dossier.candidate
        cursor = self._db.execute(
            "INSERT INTO debates (at, mint, symbol, approved, cost_usd, transcript_path, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                outcome.finished_at.isoformat(),
                candidate.mint,
                candidate.symbol,
                int(outcome.approved),
                outcome.usage.cost_usd,
                str(transcript_path),
                outcome.model_dump_json(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def debates_since(self, since: datetime) -> list[DebateRecord]:
        rows = self._db.execute(
            "SELECT id, at, mint, symbol, approved, cost_usd, transcript_path "
            "FROM debates WHERE at >= ? ORDER BY at",
            (since.isoformat(),),
        ).fetchall()
        return [DebateRecord(**dict(row)) for row in rows]

    def debate_outcome(self, debate_id: int) -> DebateOutcome | None:
        row = self._db.execute("SELECT outcome FROM debates WHERE id = ?", (debate_id,)).fetchone()
        return None if row is None else DebateOutcome.model_validate_json(row["outcome"])

    def debate_transcript_path(self, debate_id: int) -> str | None:
        row = self._db.execute(
            "SELECT transcript_path FROM debates WHERE id = ?", (debate_id,)
        ).fetchone()
        return None if row is None else row["transcript_path"]

    # --- clearances ----------------------------------------------------------

    def save_clearance(self, clearance: Clearance) -> None:
        self._db.execute(
            "INSERT INTO clearances (mint, expires_at, data) VALUES (?, ?, ?) "
            "ON CONFLICT (mint) DO UPDATE SET expires_at = excluded.expires_at, "
            "data = excluded.data",
            (clearance.mint, clearance.expires_at.isoformat(), clearance.model_dump_json()),
        )

    def clearance(self, mint: str, now: datetime) -> Clearance | None:
        """The token's clearance, if it has one that has not expired."""
        row = self._db.execute(
            "SELECT data FROM clearances WHERE mint = ? AND expires_at > ?",
            (mint, now.isoformat()),
        ).fetchone()
        return None if row is None else Clearance.model_validate_json(row["data"])

    def clearances(self, now: datetime) -> list[Clearance]:
        rows = self._db.execute(
            "SELECT data FROM clearances WHERE expires_at > ? ORDER BY expires_at",
            (now.isoformat(),),
        ).fetchall()
        return [Clearance.model_validate_json(row["data"]) for row in rows]

    def drop_clearance(self, mint: str) -> None:
        self._db.execute("DELETE FROM clearances WHERE mint = ?", (mint,))

    # --- positions -----------------------------------------------------------

    def insert_position(self, position: Position) -> Position:
        cursor = self._db.execute(
            "INSERT INTO positions (mint, is_open, closed_at, data) VALUES (?, 1, NULL, '{}')",
            (position.mint,),
        )
        saved = position.model_copy(update={"id": int(cursor.lastrowid or 0)})
        self.update_position(saved)
        return saved

    def update_position(self, position: Position) -> None:
        if position.id is None:
            raise ValueError("position has no id; insert it first")
        self._db.execute(
            "UPDATE positions SET is_open = ?, closed_at = ?, data = ? WHERE id = ?",
            (
                int(position.is_open),
                position.closed_at.isoformat() if position.closed_at else None,
                position.model_dump_json(),
                position.id,
            ),
        )

    def open_positions(self) -> list[Position]:
        rows = self._db.execute("SELECT data FROM positions WHERE is_open = 1 ORDER BY id")
        return [Position.model_validate_json(row["data"]) for row in rows.fetchall()]

    def closed_positions(self, since: datetime | None = None) -> list[Position]:
        rows = self._db.execute(
            "SELECT data FROM positions WHERE is_open = 0 AND closed_at >= ? ORDER BY closed_at",
            (since.isoformat() if since else "",),
        )
        return [Position.model_validate_json(row["data"]) for row in rows.fetchall()]

    # --- price checks --------------------------------------------------------

    def record_price_check(self, check: PriceCheck) -> None:
        self._db.execute(
            "INSERT INTO price_checks (position_id, data) VALUES (?, ?)",
            (check.position_id, check.model_dump_json()),
        )

    def price_checks(self, position_id: int | None = None) -> list[PriceCheck]:
        if position_id is None:
            rows = self._db.execute("SELECT data FROM price_checks ORDER BY id")
        else:
            rows = self._db.execute(
                "SELECT data FROM price_checks WHERE position_id = ? ORDER BY id", (position_id,)
            )
        return [PriceCheck.model_validate_json(row["data"]) for row in rows.fetchall()]

    # --- lessons -------------------------------------------------------------

    def add_lesson(self, symbol: str, text: str) -> None:
        self._db.execute(
            "INSERT INTO lessons (at, symbol, text) VALUES (?, ?, ?)",
            (utcnow().isoformat(), symbol, text),
        )

    def recent_lessons(self, limit: int) -> list[str]:
        rows = self._db.execute(
            "SELECT symbol, text FROM lessons ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [f"{row['symbol']}: {row['text']}" for row in reversed(rows)]

    def lessons(self) -> list[LessonRecord]:
        rows = self._db.execute("SELECT at, symbol, text FROM lessons ORDER BY id DESC").fetchall()
        return [LessonRecord(**dict(row)) for row in rows]
