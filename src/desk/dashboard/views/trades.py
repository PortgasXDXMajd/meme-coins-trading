"""Trades: every position the desk opened, and the story of each one from debate to exit."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from desk.config import Settings
from desk.dashboard.common import (
    chart_url,
    duration,
    local,
    md,
    pct,
    remember_selection,
    show,
    table_key,
    usd,
    when,
)
from desk.dashboard.data import (
    Snapshot,
    load_debates,
    load_price_checks,
    realised_equity_curve,
)
from desk.domain.models import ExitReason, Position, utcnow
from desk.storage.db import PriceCheck

EXIT_LABELS = {
    ExitReason.STOP_LOSS: "Stop-loss",
    ExitReason.TRAILING_STOP: "Trailing stop",
    ExitReason.TAKE_PROFIT: "Take-profit",
    ExitReason.MANUAL: "Sold by hand",
}


def change_pct(position: Position, price: float | None) -> float | None:
    if price is None or not position.entry_price_usd:
        return None
    return 100 * (price / position.entry_price_usd - 1)


def trade_row(position: Position) -> dict[str, object]:
    end = position.closed_at or utcnow()
    if position.is_open:
        value = position.value_at(position.last_price_usd)
        pnl = value - position.cost_usd
        status = "🟢 Open"
    else:
        value = position.proceeds_usd
        pnl = position.pnl_usd
        status = "Closed"
    return {
        "Opened": local(position.opened_at),
        "Token": position.symbol,
        "Status": status,
        "Paid": position.cost_usd,
        "Got back / worth": value,
        "P&L $": pnl,
        "P&L %": 100 * pnl / position.cost_usd if pnl is not None else None,
        "Exit": EXIT_LABELS.get(position.exit_reason, "") if position.exit_reason else "",
        "Held": duration((end - position.opened_at).total_seconds()),
    }


def render_table(positions: list[Position]) -> int | None:
    ids = [p.id or 0 for p in positions]
    money = st.column_config.NumberColumn(format="$%.2f", width="small")
    event = st.dataframe(
        pd.DataFrame([trade_row(p) for p in positions]),
        hide_index=True,
        width="stretch",
        height=min(380, 38 + 35 * len(positions)),
        on_select="rerun",
        selection_mode="single-row",
        key=table_key("trades", ids),
        column_config={
            "Opened": st.column_config.DatetimeColumn(format="DD MMM HH:mm"),
            "Paid": money,
            "Got back / worth": money,
            "P&L $": st.column_config.NumberColumn(format="$%+.2f", width="small"),
            "P&L %": st.column_config.NumberColumn(format="%+.1f%%", width="small"),
        },
    )
    st.caption("Click a row to see that trade step by step.")
    chosen = remember_selection(event, ids, "trade_id")
    return int(chosen) if chosen is not None else (ids[0] if ids else None)


def render_steps(position: Position, approved_at: str | None) -> None:
    entry = position.entry_price_usd
    stop_pct = change_pct(position, position.stop_price_usd)
    high_pct = change_pct(position, position.high_water_price_usd)
    steps = []
    if approved_at:
        steps.append(f"**{approved_at}** · The debate approved it: at most one Bear reason stood.")
    steps.append(
        f"**{when(position.opened_at)}** · Bought for {usd(position.cost_usd)} at "
        f"${entry:.8g}. Stop-loss set {position.stop_pct or 6:.1f}% lower."
    )
    if position.took_profit:
        steps.append(
            f"Took profit on part of it for {usd(position.realized_usd)}; "
            "stop moved to at least break-even."
        )
    if position.trailing_active:
        steps.append(
            f"Price peaked at ${position.high_water_price_usd:.8g} ({pct(high_pct)}). "
            f"The trailing stop followed it up to ${position.stop_price_usd:.8g} "
            f"({pct(stop_pct)} from entry)."
        )
    else:
        steps.append(
            f"Highest price ${position.high_water_price_usd:.8g} ({pct(high_pct)}): "
            "not enough to arm the trailing stop (+12%)."
        )
    if position.is_open:
        steps.append(
            f"Now ${position.last_price_usd:.8g} "
            f"({pct(change_pct(position, position.last_price_usd))}). "
            f"Sells if it falls to ${position.stop_price_usd:.8g}."
        )
    elif position.closed_at:
        exit_pct = change_pct(position, position.exit_price_usd)
        reason = EXIT_LABELS.get(position.exit_reason, "Sold") if position.exit_reason else "Sold"
        steps.append(
            f"**{when(position.closed_at)}** · {reason} fired at "
            f"${position.exit_price_usd or 0:.8g} ({pct(exit_pct)} from entry). "
            f"Got back {usd(position.proceeds_usd)}: {pct(position.pnl_pct)} after fees."
        )
        if position.exit_price_usd and position.exit_price_usd < 0.98 * position.stop_price_usd:
            gap = 100 * (1 - position.exit_price_usd / position.stop_price_usd)
            steps.append(
                f"⚠️ The sale happened {gap:.0f}% below the stop. The price fell faster than "
                "one 5-second check, or the price feed was late."
            )
    st.markdown(md("\n".join(f"{n}. {text}" for n, text in enumerate(steps, start=1))))


def price_chart(position: Position, checks: list[PriceCheck]) -> alt.LayerChart | alt.FacetChart:
    rows = []
    for check in checks:
        at = local(check.at)
        rows.append({"Time": at, "Price": check.used_price_usd, "Line": "Price used"})
        if check.dex_price_usd is not None:
            rows.append({"Time": at, "Price": check.dex_price_usd, "Line": "DexScreener"})
        rows.append({"Time": at, "Price": check.stop_price_usd, "Line": "Stop"})
    frame = pd.DataFrame(rows)
    colors = alt.Scale(
        domain=["Price used", "DexScreener", "Stop"], range=["#4c78a8", "#9ecae9", "#e45756"]
    )
    lines = (
        alt.Chart(frame)
        .mark_line(interpolate="step-after", strokeWidth=2)
        .encode(
            x=alt.X("Time:T", title=None),
            y=alt.Y("Price:Q", title="Price (USD)", scale=alt.Scale(zero=False)),
            color=alt.Color("Line:N", scale=colors, title=None, legend=alt.Legend(orient="top")),
            strokeDash=alt.condition(
                alt.datum.Line == "Stop", alt.value([5, 4]), alt.value([1, 0])
            ),
            tooltip=[
                alt.Tooltip("Time:T", format="%H:%M:%S"),
                "Line:N",
                alt.Tooltip("Price:Q", format=".8g"),
            ],
        )
    )
    entry = (
        alt.Chart(pd.DataFrame({"Price": [position.entry_price_usd]}))
        .mark_rule(color="gray", strokeDash=[2, 2])
        .encode(y="Price:Q")
    )
    return alt.layer(lines, entry, height=280)


def render_trade(settings: Settings, snapshot: Snapshot, position: Position) -> None:
    st.divider()
    if position.is_open:
        st.info(f"### {position.symbol}: open")
    else:
        reason = EXIT_LABELS.get(position.exit_reason, "Sold") if position.exit_reason else "Sold"
        heading = f"### {position.symbol}: {reason.lower()}, {pct(position.pnl_pct)}"
        (st.success if (position.pnl_usd or 0) >= 0 else st.error)(heading)
    st.caption(f"[DexScreener chart]({chart_url(position.mint)}) · `{position.mint}`")

    end = position.closed_at or utcnow()
    worth = (
        position.value_at(position.last_price_usd) if position.is_open else position.proceeds_usd
    )
    pnl = (worth or 0) - position.cost_usd
    tiles = st.columns(5)
    tiles[0].metric("Paid", usd(position.cost_usd))
    tiles[1].metric("Worth now" if position.is_open else "Got back", usd(worth))
    tiles[2].metric("P&L", usd(pnl, signed=True), delta=pct(100 * pnl / position.cost_usd))
    tiles[3].metric("Highest", pct(change_pct(position, position.high_water_price_usd)))
    tiles[4].metric("Held", duration((end - position.opened_at).total_seconds()))

    approved_at = None
    if position.debate_id is not None:
        record = next((d for d in snapshot.debates if d.id == position.debate_id), None)
        approved_at = when(record.at) if record else None
    st.markdown("#### What happened")
    render_steps(position, approved_at)

    st.markdown("#### Price while held")
    checks = load_price_checks(settings, position.id or 0)
    if checks:
        st.altair_chart(price_chart(position, checks), width="stretch")
        quoted = sum(1 for c in checks if c.source == "quote")
        st.caption(
            f"{len(checks):,} checks, {quoted:,} priced from a live Jupiter quote. "
            "The dashed red line is the stop; the grey line is the entry price."
        )
    else:
        st.caption(
            "No price checks stored. They are recorded for trades opened from 14 Nov onwards."
        )

    lessons = [
        lesson
        for lesson in snapshot.lessons
        if lesson.symbol == position.symbol
        and (position.closed_at is None or lesson.at >= position.closed_at)
    ]
    if lessons:
        st.markdown("#### Lesson Claude took from it")
        st.markdown(md(lessons[-1].text))

    if position.debate_id is not None:
        outcome = load_debates(settings, [position.debate_id]).get(position.debate_id)
        if outcome is not None:
            with st.expander("Why it was bought: the Bear's reasons and the answers"):
                rebuttals = {r.reason_number: r for r in outcome.rebuttals}
                for number, reason in enumerate(outcome.reasons, start=1):
                    st.markdown(f"**{number}. {reason.title}**")
                    st.caption(md(f"Bear: {reason.claim}"))
                    rebuttal = rebuttals.get(number)
                    if rebuttal:
                        st.caption(md(f"Analyst: {rebuttal.argument}"))
            st.session_state["debate_id"] = position.debate_id
            st.page_link("views/debates.py", label="Read the full debate", icon=":material/forum:")


def render_trades(settings: Settings, snapshot: Snapshot) -> None:
    st.title("Trades")
    st.caption(
        "Every position the desk opened. The stop is sized from the token's one-minute "
        "volatility (6–12% below entry). At +18% half is sold and the stop moves to "
        "break-even; from +12% the stop trails the highest price."
    )
    positions = sorted(
        [*snapshot.open_positions, *snapshot.closed_positions],
        key=lambda p: p.opened_at,
        reverse=True,
    )
    stats = snapshot.stats
    tiles = st.columns(4)
    tiles[0].metric("Open", len(snapshot.open_positions), border=True)
    tiles[1].metric("Closed", stats.closed_trades, border=True)
    tiles[2].metric(
        "Won",
        f"{stats.wins} of {stats.closed_trades}" if stats.closed_trades else "–",
        border=True,
    )
    tiles[3].metric("Realised P&L", usd(stats.realised_pnl_usd, signed=True), border=True)
    if not positions:
        st.info("No trades in this time window. The desk buys only when a debate is won.")
        return

    chosen = render_table(positions)
    position = next((p for p in positions if p.id == chosen), None)
    if position is not None:
        render_trade(settings, snapshot, position)

    if len(snapshot.closed_positions) > 1:
        st.divider()
        st.markdown("#### Equity after each closed trade")
        curve = realised_equity_curve(snapshot.closed_positions, stats.starting_equity_usd)
        frame = pd.DataFrame(
            {"Closed": [local(at) for at, _ in curve], "Equity": [v for _, v in curve]}
        )
        st.altair_chart(
            alt.Chart(frame)
            .mark_line(interpolate="step-after", point=True)
            .encode(
                x=alt.X("Closed:T", title=None),
                y=alt.Y("Equity:Q", title="USD", scale=alt.Scale(zero=False)),
            )
            .properties(height=220),
            width="stretch",
        )


show(render_trades)
