"""The dashboard page. Start it with `desk ui`.

Read-only: it shows what the desk is doing and never changes it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import altair as alt
import pandas as pd
import streamlit as st
from pydantic import ValidationError

from desk.config import Settings
from desk.dashboard.data import (
    Snapshot,
    decision_reasons,
    kill_rule_counts,
    load_debate,
    load_snapshot,
    realised_equity_curve,
)
from desk.domain.models import DebateOutcome, Position, RulingVerdict, utcnow
from desk.storage.db import DebateRecord, EvaluationRecord

WINDOWS = {"Last 6 hours": 6, "Last 12 hours": 12, "Last 24 hours": 24, "Last 3 days": 72}
ALL_TIME = "All time"
OUTCOME_LABELS = {
    "filtered": "Killed by Filter",
    "vetoed": "Vetoed in debate",
    "approved": "Approved",
}
LOG_LINES_SHOWN = 200


def local(moment: datetime) -> datetime:
    """Stored times are UTC; show them in this machine's timezone."""
    return moment.astimezone().replace(tzinfo=None)


def ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


def rule_label(rule: str) -> str:
    return rule.replace("_", " ").capitalize()


# --- header ----------------------------------------------------------------


def render_status(snapshot: Snapshot) -> None:
    beat = snapshot.heartbeat
    columns = st.columns([1, 1, 1, 3], vertical_alignment="center")
    with columns[0]:
        if snapshot.stats.mode == "live":
            st.badge("Live trading", icon=":material/warning:", color="red")
        else:
            st.badge("Paper trading", icon=":material/science:", color="blue")
    with columns[1]:
        if beat.alive:
            st.badge("Desk running", icon=":material/check_circle:", color="green")
        else:
            st.badge("Desk not running", icon=":material/error:", color="red")
    with columns[2]:
        if snapshot.halted:
            st.badge("New entries paused", icon=":material/pause_circle:", color="orange")
    with columns[3]:
        if beat.seconds_since_activity is not None:
            st.caption(f"Last desk activity {ago(beat.seconds_since_activity)}")


def render_headline(snapshot: Snapshot) -> None:
    stats = snapshot.stats
    total = stats.equity_usd - stats.starting_equity_usd
    vetoed = stats.debates - stats.debates_approved
    tiles = st.columns(5)
    tiles[0].metric(
        "Equity",
        f"${stats.equity_usd:,.2f}",
        delta=f"{total:+,.2f} USD since start",
        delta_color="off" if total == 0 else "normal",
        delta_arrow="off" if total == 0 else "auto",
        border=True,
    )
    tiles[1].metric("Cash", f"${stats.cash_usd:,.2f}", border=True)
    tiles[2].metric(
        "Open positions",
        len(snapshot.open_positions),
        delta=f"${stats.open_positions_value_usd:,.2f} at market",
        delta_color="off",
        delta_arrow="off",
        border=True,
    )
    tiles[3].metric(
        "Realised P&L",
        f"${stats.realised_pnl_usd:+,.2f}",
        delta=f"{stats.wins} wins of {stats.closed_trades} closed",
        delta_color="off",
        delta_arrow="off",
        border=True,
    )
    tiles[4].metric(
        "Claude spend on debates",
        f"${stats.llm_cost_usd:,.2f}",
        delta=f"{stats.debates} debates, {vetoed} vetoed",
        delta_color="off",
        delta_arrow="off",
        border=True,
    )


def render_pipeline(snapshot: Snapshot) -> None:
    stats = snapshot.stats
    bought = len(snapshot.open_positions) + stats.closed_trades
    st.subheader("Where tokens stop")
    stages = st.columns(5)
    scout = snapshot.scout
    stages[0].metric(
        "1 · Scout: in window now",
        scout.in_window if scout else "–",
        delta=f"{scout.watched} on watchlist" if scout else None,
        delta_color="off",
        delta_arrow="off",
        help="Pairs 30 min to 72 h old with a market cap inside the configured range.",
    )
    stages[1].metric(
        "2 · Filter: killed",
        stats.evaluations.get("filtered", 0),
        help="Evaluations that failed a hard rule. A token is re-checked after its cooldown.",
    )
    stages[2].metric(
        "3 · Bear vs Analyst: debated",
        stats.debates,
        help="Tokens that passed every Filter rule and went to a debate.",
    )
    stages[3].metric(
        "4 · Risk: approved",
        stats.debates_approved,
        help="Debates where all three Bear reasons were ruled rebutted.",
    )
    stages[4].metric("5 · Executor: bought", bought)


# --- decisions -------------------------------------------------------------


def plain_reasons(evaluation: EvaluationRecord) -> list[str]:
    """Filter kills are stored as "rule: detail"; the detail alone reads better."""
    if evaluation.outcome != "filtered":
        return decision_reasons(evaluation)
    return [reason.split(": ", 1)[-1] for reason in decision_reasons(evaluation)]


def render_decisions(snapshot: Snapshot) -> None:
    st.markdown("**Every decision, newest first**")
    if not snapshot.evaluations:
        st.info("No token has reached the Filter in this time window yet.")
        return
    rows = [
        {
            "Time": local(evaluation.at),
            "Token": evaluation.symbol,
            "Decision": OUTCOME_LABELS.get(evaluation.outcome, evaluation.outcome),
            "Re-check after": local(evaluation.cooldown_until),
            "Why": " · ".join(plain_reasons(evaluation)) or "All reasons rebutted",
        }
        for evaluation in reversed(snapshot.evaluations)
    ]
    st.dataframe(
        pd.DataFrame(rows),
        hide_index=True,
        width="stretch",
        height=320,
        column_config={
            "Time": st.column_config.DatetimeColumn(format="DD MMM HH:mm:ss"),
            "Re-check after": st.column_config.DatetimeColumn(format="DD MMM HH:mm"),
            "Why": st.column_config.TextColumn(width="large"),
        },
    )
    counts = kill_rule_counts(snapshot.evaluations)
    if counts:
        st.markdown("**Filter kills by rule**")
        st.altair_chart(kill_chart(counts), width="stretch")


def kill_chart(counts: dict[str, int]) -> alt.Chart:
    frame = pd.DataFrame(
        {"Rule": [rule_label(rule) for rule in counts], "Kills": list(counts.values())}
    )
    base = alt.Chart(frame).encode(
        x=alt.X("Kills:Q", title="Kills", axis=alt.Axis(tickMinStep=1, format="d")),
        y=alt.Y("Rule:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260)),
        tooltip=["Rule", "Kills"],
    )
    return base.mark_bar(cornerRadiusEnd=4, size=18).properties(height=max(120, 44 * len(frame)))


# --- debates ---------------------------------------------------------------


def debate_label(record: DebateRecord) -> str:
    verdict = "APPROVED" if record.approved else "VETO"
    return f"{local(record.at):%d %b %H:%M} · {record.symbol} · {verdict}"


def render_debates(settings: Settings, snapshot: Snapshot) -> None:
    if not snapshot.debates:
        st.info("No debates in this time window. A token must pass every Filter rule first.")
        return
    records = {record.id: record for record in reversed(snapshot.debates)}
    chosen = st.selectbox(
        "Debate",
        list(records),
        format_func=lambda debate_id: debate_label(records[debate_id]),
        key="debate_choice",
    )
    record = records[chosen]
    try:
        outcome = load_debate(settings, record.id)
    except ValidationError:
        outcome = None
    if outcome is None:
        st.warning("This debate's stored record could not be read. See the transcript file.")
        st.code(record.transcript_path)
        return
    render_debate(outcome, record)


def render_debate(outcome: DebateOutcome, record: DebateRecord) -> None:
    candidate = outcome.dossier.candidate
    seconds = (outcome.finished_at - outcome.started_at).total_seconds()
    if outcome.approved:
        st.success(f"**{candidate.symbol} approved.** All three reasons were rebutted.")
    else:
        st.error(f"**{candidate.symbol} vetoed.** {len(outcome.veto_reasons)} reason(s) stood.")
    st.caption(
        f"{candidate.name} · debated for {seconds:.0f}s · Claude cost "
        f"${outcome.usage.cost_usd:.3f} · [chart]({candidate.url}) · `{candidate.mint}`"
    )
    for reason in outcome.veto_reasons:
        st.markdown(f"- {reason}")

    rebuttals = {rebuttal.reason_number: rebuttal for rebuttal in outcome.rebuttals}
    rulings = {ruling.reason_number: ruling for ruling in outcome.rulings}
    for number, reason in enumerate(outcome.reasons, start=1):
        with st.container(border=True):
            st.markdown(f"#### Reason {number}: {reason.title}")
            bear, analyst, risk = st.columns(3, gap="medium")
            with bear:
                st.markdown(f"**Bear** · cites {', '.join(reason.cited_facts) or 'nothing'}")
                st.write(reason.claim)
                st.caption(f"Metric: {reason.metric}")
            with analyst:
                rebuttal = rebuttals.get(number)
                if rebuttal is None:
                    st.markdown("**Analyst**")
                    st.write("No rebuttal given.")
                else:
                    stance = "conceded" if rebuttal.conceded else "rebuts"
                    cited = ", ".join(rebuttal.cited_facts) or "nothing"
                    st.markdown(f"**Analyst** · {stance} · cites {cited}")
                    st.write(rebuttal.argument)
            with risk:
                st.markdown("**Risk ruling**")
                ruling = rulings.get(number)
                if ruling is None:
                    st.write("No ruling recorded.")
                elif ruling.verdict is RulingVerdict.REBUTTED:
                    st.badge("Rebutted", icon=":material/check_circle:", color="green")
                    st.write(ruling.rationale)
                else:
                    st.badge("Unrebutted", icon=":material/cancel:", color="red")
                    st.write(ruling.rationale)

    with st.expander("Dossier: the facts both sides argued from"):
        st.code(outcome.dossier.render(), language=None, wrap_lines=True)
    if outcome.evidence:
        with st.expander(f"Evidence gathered during the debate ({len(outcome.evidence)})"):
            st.code(
                "\n".join(f"[{f.id}] ({f.category}) {f.statement}" for f in outcome.evidence),
                language=None,
                wrap_lines=True,
            )
    st.caption(f"Transcript file: {record.transcript_path}")


# --- positions -------------------------------------------------------------


def open_row(position: Position) -> dict[str, object]:
    change = 100 * (position.last_price_usd / position.entry_price_usd - 1)
    to_stop = 100 * (position.stop_price_usd / position.last_price_usd - 1)
    return {
        "Token": position.symbol,
        "Cost $": position.cost_usd,
        "Value $": position.value_at(position.last_price_usd),
        "Price change %": change,
        "Stop is % away": to_stop,
        "Stop": "Trailing" if position.trailing_active else "Initial",
        "Opened": local(position.opened_at),
    }


def closed_row(position: Position) -> dict[str, object]:
    return {
        "Token": position.symbol,
        "Cost $": position.cost_usd,
        "Proceeds $": position.proceeds_usd,
        "P&L $": position.pnl_usd,
        "P&L %": position.pnl_pct,
        "Exit": position.exit_reason.value.replace("_", " ") if position.exit_reason else "",
        "Opened": local(position.opened_at),
        "Closed": local(position.closed_at) if position.closed_at else None,
    }


def render_positions(snapshot: Snapshot) -> None:
    money = st.column_config.NumberColumn(format="%.2f")
    percent = st.column_config.NumberColumn(format="%+.1f%%")
    when = st.column_config.DatetimeColumn(format="DD MMM HH:mm")

    st.markdown("**Open**")
    if not snapshot.open_positions:
        st.info("No open positions. The desk buys only when a debate ends with no reason standing.")
    else:
        st.dataframe(
            pd.DataFrame([open_row(p) for p in snapshot.open_positions]),
            hide_index=True,
            width="stretch",
            column_config={
                "Cost $": money,
                "Value $": money,
                "Price change %": percent,
                "Stop is % away": percent,
                "Opened": when,
            },
        )

    st.markdown("**Closed**")
    if not snapshot.closed_positions:
        st.info("No closed trades in this time window.")
        return
    st.dataframe(
        pd.DataFrame([closed_row(p) for p in reversed(snapshot.closed_positions)]),
        hide_index=True,
        width="stretch",
        column_config={
            "Cost $": money,
            "Proceeds $": money,
            "P&L $": st.column_config.NumberColumn(format="%+.2f"),
            "P&L %": percent,
            "Opened": when,
            "Closed": when,
        },
    )
    st.markdown("**Equity after each closed trade** (realised P&L only)")
    curve = realised_equity_curve(snapshot.closed_positions, snapshot.stats.starting_equity_usd)
    st.altair_chart(equity_chart(curve), width="stretch")


def equity_chart(curve: list[tuple[datetime, float]]) -> alt.LayerChart:
    frame = pd.DataFrame(
        {"Closed": [local(at) for at, _ in curve], "Equity": [v for _, v in curve]}
    )
    base = alt.Chart(frame).encode(
        x=alt.X("Closed:T", title=None),
        y=alt.Y("Equity:Q", title="Equity (USD)", scale=alt.Scale(zero=False)),
        tooltip=[
            alt.Tooltip("Closed:T", format="%d %b %H:%M"),
            alt.Tooltip("Equity:Q", format="$,.2f"),
        ],
    )
    line = base.mark_line(interpolate="step-after", strokeWidth=2)
    points = base.mark_point(size=70, filled=True)
    return (line + points).properties(height=260)


# --- lessons and log -------------------------------------------------------


def render_lessons(snapshot: Snapshot) -> None:
    if not snapshot.lessons:
        st.info(
            "No lessons yet. After each closed trade Claude writes one, "
            "and the Bear reads them in later debates."
        )
        return
    st.dataframe(
        pd.DataFrame(
            [
                {"When": local(lesson.at), "Token": lesson.symbol, "Lesson": lesson.text}
                for lesson in snapshot.lessons
            ]
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "When": st.column_config.DatetimeColumn(format="DD MMM HH:mm"),
            "Lesson": st.column_config.TextColumn(width="large"),
        },
    )


def render_log(snapshot: Snapshot) -> None:
    if not snapshot.log_tail:
        st.info("The desk has not written a log yet.")
        return
    newest_first = list(reversed(snapshot.log_tail[-LOG_LINES_SHOWN:]))
    st.caption(f"Newest first, last {len(newest_first)} lines of logs/desk.log.")
    st.code("\n".join(newest_first), language=None, height=520)


# --- page ------------------------------------------------------------------


def render_dashboard(settings: Settings, hours: float | None) -> None:
    now = utcnow()
    since = now - timedelta(hours=hours) if hours else datetime.min.replace(tzinfo=now.tzinfo)
    snapshot = load_snapshot(settings, since, now)
    if snapshot is None:
        st.info("No data yet. Start the desk with `uv run desk run`, then come back.")
        return

    render_status(snapshot)
    render_headline(snapshot)
    render_pipeline(snapshot)

    decisions, debates, positions, lessons, log = st.tabs(
        ["Decisions", "Debates", "Positions", "Lessons", "Log"]
    )
    with decisions:
        render_decisions(snapshot)
    with debates:
        render_debates(settings, snapshot)
    with positions:
        render_positions(snapshot)
    with lessons:
        render_lessons(snapshot)
    with log:
        render_log(snapshot)


def main() -> None:
    st.set_page_config(page_title="Memecoin Desk", layout="wide")
    settings = Settings()  # type: ignore[call-arg]  # values come from the environment

    with st.sidebar:
        st.header("Memecoin Desk")
        choice = st.selectbox("Time window", [*WINDOWS, ALL_TIME], index=2)
        auto = st.toggle("Refresh automatically", value=True)
        interval = st.select_slider("Every (seconds)", [5, 10, 30, 60], value=10, disabled=not auto)
        st.caption("Read-only. To pause new entries, create the file `data/HALT`.")

    refreshing = st.fragment(run_every=interval if auto else None)(render_dashboard)
    refreshing(settings, WINDOWS.get(choice))


main()
