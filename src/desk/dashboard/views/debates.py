"""Debates: every token that passed the Filter, what each side argued and how Risk ruled."""

from __future__ import annotations

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
from desk.dashboard.data import Snapshot, load_debates, stood_titles
from desk.domain.models import DebateOutcome, Position, RulingVerdict
from desk.storage.db import DebateRecord

ALL = "All"
APPROVED = "Approved"
VETOED = "Vetoed"


def debate_row(record: DebateRecord, outcome: DebateOutcome | None) -> dict[str, object]:
    row: dict[str, object] = {
        "Time": local(record.at),
        "Token": record.symbol,
        "Verdict": "✅ Approved" if record.approved else "❌ Vetoed",
        "What stopped it": "",
        "Market cap": None,
        "Liquidity": None,
        "Age (h)": None,
        "Claude $": record.cost_usd,
    }
    if outcome is not None:
        candidate = outcome.dossier.candidate
        row |= {
            "What stopped it": " · ".join(stood_titles(outcome)) if not record.approved else "",
            "Market cap": candidate.market_cap_usd,
            "Liquidity": candidate.liquidity_usd,
            "Age (h)": candidate.age_hours(candidate.observed_at),
        }
    return row


def render_table(
    records: list[DebateRecord], outcomes: dict[int, DebateOutcome | None]
) -> int | None:
    ids = [record.id for record in records]
    event = st.dataframe(
        pd.DataFrame([debate_row(r, outcomes.get(r.id)) for r in records]),
        hide_index=True,
        width="stretch",
        height=min(420, 38 + 35 * len(records)),
        on_select="rerun",
        selection_mode="single-row",
        key=table_key("debates", ids),
        column_config={
            "Time": st.column_config.DatetimeColumn(format="DD MMM HH:mm"),
            "Verdict": st.column_config.TextColumn(width="small"),
            "What stopped it": st.column_config.TextColumn(
                width="large", help="The Bear's reasons that the Analyst could not answer."
            ),
            "Market cap": st.column_config.NumberColumn(format="compact", width="small"),
            "Liquidity": st.column_config.NumberColumn(format="compact", width="small"),
            "Age (h)": st.column_config.NumberColumn(format="%.1f", width="small"),
            "Claude $": st.column_config.NumberColumn(format="$%.2f", width="small"),
        },
    )
    st.caption("Click a row to read that debate. The newest one is shown until you pick another.")
    chosen = remember_selection(event, ids, "debate_id")
    return int(chosen) if chosen is not None else (ids[0] if ids else None)


def render_market(outcome: DebateOutcome) -> None:
    candidate = outcome.dossier.candidate
    safety = outcome.dossier.safety
    tiles = st.columns(6)
    tiles[0].metric("Market cap", usd(candidate.market_cap_usd))
    tiles[1].metric("Liquidity", usd(candidate.liquidity_usd))
    tiles[2].metric("Pair age", duration(candidate.age_hours(candidate.observed_at) * 3600))
    tiles[3].metric("Price, last hour", pct(candidate.price_change_pct.h1))
    tiles[4].metric("Top 10 wallets hold", pct(safety.top10_holders_pct, signed=False))
    tiles[5].metric(
        "Round-trip cost",
        pct(outcome.dossier.round_trip.cost_pct, signed=False),
        help="Lost by buying and selling straight back, from live Jupiter quotes.",
    )


def render_reasons(outcome: DebateOutcome) -> None:
    rebuttals = {r.reason_number: r for r in outcome.rebuttals}
    rulings = {r.reason_number: r for r in outcome.rulings}
    for number, reason in enumerate(outcome.reasons, start=1):
        ruling = rulings.get(number)
        stood = ruling is None or ruling.verdict is RulingVerdict.UNREBUTTED
        icon = ":material/cancel:" if stood else ":material/check_circle:"
        verdict = "stood, so it blocks the buy" if stood else "answered"
        with st.expander(
            f"Reason {number} · {reason.title} — {verdict}",
            icon=icon,
            expanded=stood or outcome.approved,
        ):
            bear, analyst, risk = st.columns(3, gap="medium")
            with bear:
                st.markdown("**🐻 Bear: why not to buy**")
                st.markdown(md(reason.claim))
                st.caption(
                    f"Metric: {reason.metric} · cites {', '.join(reason.cited_facts) or 'nothing'}"
                )
            with analyst:
                rebuttal = rebuttals.get(number)
                if rebuttal is None:
                    st.markdown("**📊 Analyst**")
                    st.write("No answer given.")
                else:
                    st.markdown(
                        "**📊 Analyst: concedes**"
                        if rebuttal.conceded
                        else "**📊 Analyst: answers**"
                    )
                    st.markdown(md(rebuttal.argument))
                    st.caption(f"Cites {', '.join(rebuttal.cited_facts) or 'nothing'}")
            with risk:
                st.markdown("**⚖️ Risk: ruling**")
                if ruling is None:
                    st.write("No ruling recorded.")
                elif ruling.verdict is RulingVerdict.REBUTTED:
                    st.badge("Rebutted", icon=":material/check_circle:", color="green")
                    st.markdown(md(ruling.rationale))
                else:
                    st.badge("Stands", icon=":material/cancel:", color="red")
                    st.markdown(md(ruling.rationale))


def render_trade_link(position: Position | None) -> None:
    if position is None:
        return
    if position.is_open:
        st.info(
            md(f"Bought for {usd(position.cost_usd)} on {when(position.opened_at)}. Still open.")
        )
    else:
        st.info(
            md(
                f"Bought for {usd(position.cost_usd)} on {when(position.opened_at)}, "
                f"sold for {usd(position.proceeds_usd)}: {pct(position.pnl_pct)}."
            )
        )
    st.page_link("views/trades.py", label="Open the trade", icon=":material/swap_horiz:")


def render_debate(
    record: DebateRecord, outcome: DebateOutcome | None, position: Position | None
) -> None:
    st.divider()
    if outcome is None:
        st.warning("This debate's stored record could not be read. See the transcript file.")
        st.code(record.transcript_path)
        return
    candidate = outcome.dossier.candidate
    if outcome.approved:
        standing = len(outcome.unrebutted)
        st.success(
            f"### {candidate.symbol}: approved\n"
            f"{len(outcome.reasons) - standing} of the Bear's {len(outcome.reasons)} "
            f"reasons were answered; {standing} stood, within the desk's limit."
        )
    else:
        stood = stood_titles(outcome)
        st.error(
            md(
                f"### {candidate.symbol}: vetoed\n"
                f"{len(stood)} of the Bear's reasons stood: " + " · ".join(stood)
            )
        )
    seconds = (outcome.finished_at - outcome.started_at).total_seconds()
    st.caption(
        md(f"{candidate.name} · {when(record.at)} · debated for {duration(seconds)} · Claude cost ")
        + md(f"${outcome.usage.cost_usd:.2f}")
        + f" · [DexScreener chart]({chart_url(candidate.mint)}) · `{candidate.mint}`"
    )
    render_trade_link(position)
    render_market(outcome)
    st.markdown("#### The reasons not to buy")
    render_reasons(outcome)
    with st.expander("Dossier: the facts both sides argued from", icon=":material/description:"):
        st.code(outcome.dossier.render(), language=None, wrap_lines=True)
    if outcome.evidence:
        with st.expander(
            f"Evidence the Bear gathered during the debate ({len(outcome.evidence)})",
            icon=":material/manage_search:",
        ):
            st.code(
                "\n".join(f"[{f.id}] ({f.category}) {f.statement}" for f in outcome.evidence),
                language=None,
                wrap_lines=True,
            )
    st.caption(f"Full transcript: {record.transcript_path}")


def render_debates(settings: Settings, snapshot: Snapshot) -> None:
    st.title("Debates")
    st.caption(
        "Every token that passed the Filter. The Bear gives up to three reasons not to buy, "
        "the Analyst answers each with data, and Risk rules. More than "
        f"{settings.max_unrebutted_reasons} unanswered reason(s) block the buy."
    )
    if not snapshot.debates:
        st.info("No debates in this time window. A token must pass every Filter rule first.")
        return

    approved = sum(1 for d in snapshot.debates if d.approved)
    tiles = st.columns(4)
    tiles[0].metric("Debates", len(snapshot.debates), border=True)
    tiles[1].metric("Approved", approved, border=True)
    tiles[2].metric("Vetoed", len(snapshot.debates) - approved, border=True)
    tiles[3].metric(
        "Claude cost", f"${sum(d.cost_usd for d in snapshot.debates):,.2f}", border=True
    )

    with st.container(horizontal=True, vertical_alignment="bottom", gap="medium"):
        verdict = st.segmented_control(
            "Show", [ALL, APPROVED, VETOED], default=ALL, key="debate_verdict"
        )
        query = st.text_input("Find a token", placeholder="Symbol or mint", key="debate_query")
    query = query.strip().lower()
    records = [
        r
        for r in reversed(snapshot.debates)
        if (verdict in (None, ALL) or r.approved == (verdict == APPROVED))
        and (not query or query in r.symbol.lower() or query == r.mint.lower())
    ]
    if not records:
        st.info("No debates match these filters.")
        return

    outcomes = load_debates(settings, [r.id for r in records])
    chosen = render_table(records, outcomes)
    record = next((r for r in records if r.id == chosen), None)
    if record is None:
        return
    positions = [*snapshot.open_positions, *snapshot.closed_positions]
    position = next((p for p in positions if p.debate_id == record.id), None)
    render_debate(record, outcomes.get(record.id), position)


show(render_debates)
