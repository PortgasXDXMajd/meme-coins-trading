"""Run summary: plain numbers, plus an optional Claude-written morning report."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from desk.config import Settings
from desk.domain.models import Position, utcnow
from desk.llm.client import StructuredLLM, load_prompt, to_json
from desk.storage.db import DebateRecord, Store

TRANSCRIPT_EXCERPT_CHARS = 1500
MAX_DEBATES_IN_PROMPT = 25


class MorningReport(BaseModel):
    markdown: str = Field(description="The full report in Markdown.")


class RunStats(BaseModel):
    mode: str
    window_hours: float
    cash_usd: float
    open_positions_value_usd: float
    equity_usd: float
    starting_equity_usd: float
    closed_trades: int
    wins: int
    realised_pnl_usd: float
    debates: int
    debates_approved: int
    llm_cost_usd: float
    evaluations: dict[str, int]


def collect_stats(store: Store, settings: Settings, since: datetime) -> RunStats:
    closed = store.closed_positions(since)
    open_positions = store.open_positions()
    debates = store.debates_since(since)
    cash = store.get_float("paper_cash_usd") or 0.0
    marked = sum(p.value_at(p.last_price_usd) for p in open_positions)
    return RunStats(
        mode=settings.trading_mode,
        window_hours=(utcnow() - since).total_seconds() / 3600,
        cash_usd=cash,
        open_positions_value_usd=marked,
        equity_usd=cash + marked,
        starting_equity_usd=settings.paper_starting_usd,
        closed_trades=len(closed),
        wins=sum(1 for p in closed if (p.pnl_usd or 0) > 0),
        realised_pnl_usd=sum(p.pnl_usd or 0 for p in closed),
        debates=len(debates),
        debates_approved=sum(1 for d in debates if d.approved),
        llm_cost_usd=sum(d.cost_usd for d in debates),
        evaluations=store.evaluation_counts(since),
    )


def _position_row(position: Position) -> str:
    if position.is_open:
        change = 100 * (position.last_price_usd / position.entry_price_usd - 1)
        return (
            f"  {position.symbol:<12} OPEN    cost ${position.cost_usd:7.2f}  "
            f"price {change:+6.1f}%  stop ${position.stop_price_usd:.8g}"
            f"{'  (trailing)' if position.trailing_active else ''}"
        )
    return (
        f"  {position.symbol:<12} CLOSED  cost ${position.cost_usd:7.2f}  "
        f"P&L {position.pnl_pct or 0:+6.1f}% (${position.pnl_usd or 0:+.2f})  "
        f"{position.exit_reason.value if position.exit_reason else ''}"
    )


def render_text(store: Store, settings: Settings, since: datetime) -> str:
    stats = collect_stats(store, settings, since)
    total_pnl = stats.equity_usd - stats.starting_equity_usd
    lines = [
        f"Memecoin desk — {stats.mode.upper()} mode, last {stats.window_hours:.1f}h",
        "",
        f"Equity ${stats.equity_usd:.2f} (cash ${stats.cash_usd:.2f} + positions "
        f"${stats.open_positions_value_usd:.2f}); since start {total_pnl:+.2f} USD "
        f"({100 * total_pnl / stats.starting_equity_usd:+.1f}%)",
        f"Closed trades: {stats.closed_trades} ({stats.wins} wins), "
        f"realised P&L ${stats.realised_pnl_usd:+.2f}",
        f"Debates: {stats.debates} ({stats.debates_approved} approved, "
        f"{stats.debates - stats.debates_approved} vetoed), Claude spend ${stats.llm_cost_usd:.2f}",
        f"Evaluations: {stats.evaluations or 'none'}",
        "",
        "Positions:",
    ]
    positions = store.open_positions() + store.closed_positions(since)
    lines += [_position_row(p) for p in positions] or ["  (none)"]
    if settings.is_live:
        lines.append("\nNote: cash shown is the paper ledger; check the wallet for live balances.")
    return "\n".join(lines)


def _debate_digest(debates: list[DebateRecord]) -> list[dict[str, object]]:
    digest = []
    for debate in debates[-MAX_DEBATES_IN_PROMPT:]:
        path = Path(debate.transcript_path)
        excerpt = (
            path.read_text(encoding="utf-8")[:TRANSCRIPT_EXCERPT_CHARS] if path.exists() else ""
        )
        digest.append({"symbol": debate.symbol, "approved": debate.approved, "excerpt": excerpt})
    return digest


async def write_ai_report(
    llm: StructuredLLM, store: Store, settings: Settings, since: datetime
) -> Path:
    stats = collect_stats(store, settings, since)
    positions = store.open_positions() + store.closed_positions(since)
    user = (
        f"<stats>\n{to_json(stats.model_dump())}\n</stats>\n\n"
        f"<positions>\n{to_json([p.model_dump(mode='json') for p in positions])}\n</positions>\n\n"
        f"<debates>\n{to_json(_debate_digest(store.debates_since(since)))}\n</debates>\n\n"
        "Write the morning report."
    )
    result = await llm.ask(system=load_prompt("report"), user=user, output_type=MorningReport)
    folder = settings.log_dir / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{utcnow():%Y-%m-%d-%H%M}.md"
    path.write_text(result.output.markdown, encoding="utf-8")
    return path


def default_since(hours: float) -> datetime:
    return utcnow() - timedelta(hours=hours)
