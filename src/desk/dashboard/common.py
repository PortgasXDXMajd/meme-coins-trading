"""Shared by every dashboard page: settings, the time window, auto-refresh and formatting."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

import streamlit as st

from desk.config import Settings
from desk.dashboard.data import Snapshot, load_snapshot
from desk.domain.models import utcnow

WINDOWS: dict[str, float | None] = {
    "Last 6 hours": 6,
    "Last 12 hours": 12,
    "Last 24 hours": 24,
    "Last 3 days": 72,
    "All time": None,
}
DEFAULT_WINDOW = "Last 24 hours"
REFRESH_CHOICES = [10, 30, 60, 120]
DEFAULT_REFRESH = 30

STYLE = """
<style>
[data-testid="stMetricValue"] { font-size: 1.55rem; }
[data-testid="stMetricLabel"] p { font-size: 0.85rem; opacity: 0.8; }
[data-testid="stExpander"] summary p { font-size: 0.98rem; }
.block-container { padding-top: 2.2rem; }
/* Let side-by-side tiles and columns wrap on narrow screens instead of truncating. */
[data-testid="stHorizontalBlock"] { flex-wrap: wrap; row-gap: 0.75rem; }
[data-testid="stColumn"] { min-width: 150px; }
</style>
"""

Render = Callable[[Settings, Snapshot], None]


def show(render: Render) -> None:
    """Load the desk's state for the chosen window and draw one page, refreshing it on a timer."""
    settings = Settings()  # type: ignore[call-arg]  # values come from the environment
    auto = bool(st.session_state.get("auto_refresh", True))
    interval = int(st.session_state.get("refresh_seconds", DEFAULT_REFRESH))

    @st.fragment(run_every=interval if auto else None)
    def body() -> None:
        now = utcnow()
        hours = WINDOWS.get(str(st.session_state.get("window", DEFAULT_WINDOW)))
        since = now - timedelta(hours=hours) if hours else datetime.min.replace(tzinfo=now.tzinfo)
        snapshot = load_snapshot(settings, since, now)
        if snapshot is None:
            st.info("No data yet. Start the desk with `uv run desk run`, then come back.")
            return
        render(settings, snapshot)

    body()


def remember_selection(event: object, ids: Sequence[int | str], key: str) -> int | str | None:
    """The id of the row picked in a table, kept after the table refreshes."""
    rows = getattr(getattr(event, "selection", None), "rows", None) or []
    if rows and rows[0] < len(ids):
        st.session_state[key] = ids[rows[0]]
    chosen = st.session_state.get(key)
    return chosen if chosen in ids else None


def table_key(name: str, ids: Sequence[object]) -> str:
    """A table key that changes with its rows, so a stale row selection is dropped."""
    return f"{name}-{hash(tuple(ids))}"


# --- formatting --------------------------------------------------------------


def md(text: str) -> str:
    """Escape dollar signs: Streamlit reads text between two of them as a math formula."""
    return text.replace("$", "\\$")


def local(moment: datetime) -> datetime:
    """Stored times are UTC; show them in this machine's timezone."""
    return moment.astimezone().replace(tzinfo=None)


def when(moment: datetime) -> str:
    return f"{local(moment):%d %b %H:%M}"


def ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


def duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def usd(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "–"
    sign = "-" if value < 0 else "+" if signed and value > 0 else ""
    size = abs(value)
    if size >= 1_000_000:
        return f"{sign}${size / 1_000_000:,.2f}M"
    if size >= 10_000:
        return f"{sign}${size / 1_000:,.0f}k"
    return f"{sign}${size:,.2f}"


def pct(value: float | None, *, signed: bool = True) -> str:
    if value is None:
        return "–"
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def chart_url(mint: str) -> str:
    return f"https://dexscreener.com/solana/{mint}"
