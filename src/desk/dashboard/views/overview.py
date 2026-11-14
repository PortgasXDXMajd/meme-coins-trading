"""Overview: is the desk alive, how is it doing, and where did tokens go."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from desk.config import Settings
from desk.dashboard.common import ago, md, show, usd, when
from desk.dashboard.data import (
    Snapshot,
    kill_rule_counts,
    load_debates,
    rule_label,
    stood_titles,
    token_funnel,
)

LATEST_DEBATES = 8


def render_status(snapshot: Snapshot) -> None:
    beat = snapshot.heartbeat
    badges = st.container(horizontal=True, vertical_alignment="center")
    with badges:
        if snapshot.stats.mode == "live":
            st.badge("Live trading", icon=":material/warning:", color="red")
        else:
            st.badge("Paper trading", icon=":material/science:", color="blue")
        if beat.alive:
            st.badge("Desk running", icon=":material/check_circle:", color="green")
        else:
            st.badge("Desk not running", icon=":material/error:", color="red")
        if snapshot.halted:
            st.badge("New entries paused", icon=":material/pause_circle:", color="orange")
        if beat.seconds_since_activity is not None:
            st.caption(f"Last activity {ago(beat.seconds_since_activity)}")


def render_money(snapshot: Snapshot) -> None:
    stats = snapshot.stats
    total = stats.equity_usd - stats.starting_equity_usd
    tiles = st.columns(4)
    tiles[0].metric(
        "Equity",
        usd(stats.equity_usd),
        delta=f"{usd(total, signed=True)} since start",
        delta_color="off" if total == 0 else "normal",
        border=True,
        help=f"Cash {usd(stats.cash_usd)} plus open positions at their last price.",
    )
    tiles[1].metric(
        "Realised P&L",
        usd(stats.realised_pnl_usd, signed=True),
        delta=f"{stats.wins} of {stats.closed_trades} won",
        delta_color="off",
        delta_arrow="off",
        border=True,
    )
    tiles[2].metric(
        "Open positions",
        len(snapshot.open_positions),
        delta=f"{usd(stats.cash_usd)} cash",
        delta_color="off",
        delta_arrow="off",
        border=True,
    )
    tiles[3].metric(
        "Claude spend",
        usd(stats.llm_cost_usd),
        delta=f"{stats.debates} debates",
        delta_color="off",
        delta_arrow="off",
        border=True,
    )


def render_funnel(snapshot: Snapshot) -> None:
    funnel = token_funnel(snapshot)
    st.subheader("Where tokens went")
    st.caption("Each token counted once, however many times it was checked.")
    stages = st.columns(4)
    stages[0].metric(
        "1 · Checked",
        funnel.checked,
        help="Different tokens the Scout found in range and handed to the Filter.",
    )
    stages[1].metric(
        "2 · Debated",
        funnel.debated,
        help="Passed every Filter rule, so the Bear and the Analyst argued about it.",
    )
    stages[2].metric(
        "3 · Approved",
        funnel.approved,
        help="Every one of the Bear's three reasons was ruled rebutted.",
    )
    stages[3].metric("4 · Bought", funnel.bought)
    with st.container(horizontal=True, gap="medium"):
        st.page_link(
            "views/rejections.py",
            label=f"{funnel.rejected} rejected by the Filter",
            icon=":material/block:",
        )
        st.page_link("views/debates.py", label="Debates", icon=":material/forum:")
        st.page_link("views/trades.py", label="Trades", icon=":material/swap_horiz:")


def render_latest_debates(settings: Settings, snapshot: Snapshot) -> None:
    st.markdown("**Latest debates**")
    records = list(reversed(snapshot.debates))[:LATEST_DEBATES]
    if not records:
        st.info("No debates in this time window.")
        return
    outcomes = load_debates(settings, [r.id for r in records])
    for record in records:
        outcome = outcomes.get(record.id)
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            if record.approved:
                st.badge("Approved", icon=":material/check:", color="green")
            else:
                st.badge("Vetoed", icon=":material/close:", color="red")
            st.markdown(f"**{record.symbol}** · {when(record.at)}")
        if not record.approved and outcome is not None:
            st.caption(md("Stood: " + " · ".join(stood_titles(outcome))))


def render_rejection_summary(snapshot: Snapshot) -> None:
    st.markdown("**Why the Filter rejects tokens**")
    counts = kill_rule_counts(snapshot.evaluations)
    if not counts:
        st.info("No Filter rejections in this time window.")
        return
    frame = pd.DataFrame(
        {"Reason": [rule_label(rule) for rule in counts], "Checks": list(counts.values())}
    )
    chart = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=4, size=16)
        .encode(
            x=alt.X("Checks:Q", title="Rejections", axis=alt.Axis(tickMinStep=1, format="d")),
            y=alt.Y("Reason:N", sort="-x", title=None, axis=alt.Axis(labelLimit=220)),
            tooltip=["Reason", "Checks"],
        )
        .properties(height=alt.Step(30))
    )
    st.altair_chart(chart, width="stretch")
    st.page_link("views/rejections.py", label="Explore every rejection", icon=":material/search:")


def render_overview(settings: Settings, snapshot: Snapshot) -> None:
    st.title("Overview")
    render_status(snapshot)
    render_money(snapshot)
    render_funnel(snapshot)
    st.divider()
    render_latest_debates(settings, snapshot)
    st.divider()
    render_rejection_summary(snapshot)
    if snapshot.open_positions:
        st.divider()
        st.markdown("**Open now:** " + ", ".join(p.symbol for p in snapshot.open_positions))
        st.caption(
            md("Value at the last checked price: " + usd(snapshot.stats.open_positions_value_usd))
        )


show(render_overview)
