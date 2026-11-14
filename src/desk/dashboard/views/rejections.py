"""Filter rejections: which tokens were turned away before any debate, and why."""

from __future__ import annotations

from collections import Counter

import pandas as pd
import streamlit as st

from desk.config import Settings
from desk.dashboard.common import chart_url, local, remember_selection, show, table_key, when
from desk.dashboard.data import (
    MISSING_DATA,
    Snapshot,
    TokenRejections,
    is_missing_data,
    kills_of,
    rejections_by_token,
    rule_label,
)
from desk.storage.db import EvaluationRecord

BY_TOKEN = "One row per token"
EVERY_CHECK = "Every check"


def matches(symbol: str, mint: str, query: str) -> bool:
    query = query.strip().lower()
    return not query or query in symbol.lower() or query == mint.lower()


def render_controls(evaluations: list[EvaluationRecord]) -> tuple[str, bool, list[str], str]:
    counts: Counter[str] = Counter(
        kill.rule for evaluation in evaluations for kill in kills_of(evaluation)
    )
    with st.container(horizontal=True, vertical_alignment="bottom", gap="medium"):
        view = st.segmented_control(
            "Show", [BY_TOKEN, EVERY_CHECK], default=BY_TOKEN, key="rej_view"
        )
        query = st.text_input("Find a token", placeholder="Symbol or mint", key="rej_query")
    hidden = sum(1 for e in evaluations if is_missing_data(e))
    hide_missing = st.toggle(
        f"Hide rejections that were only missing data ({hidden:,} checks)",
        value=True,
        key="rej_hide_missing",
        help="DexScreener reported no liquidity for these, so the Filter could not judge them. "
        "They come back every 30 minutes and crowd out the real reasons.",
    )
    options = [
        rule for rule, _ in counts.most_common() if not (hide_missing and rule == MISSING_DATA)
    ]
    rules = st.pills(
        "Only these reasons",
        options,
        selection_mode="multi",
        format_func=lambda rule: f"{rule_label(rule)} · {counts[rule]:,}",
        key="rej_rules",
    )
    return view or BY_TOKEN, hide_missing, list(rules or []), query


def keep(evaluation: EvaluationRecord, hide_missing: bool, rules: list[str], query: str) -> bool:
    if hide_missing and is_missing_data(evaluation):
        return False
    if rules and not any(kill.rule in rules for kill in kills_of(evaluation)):
        return False
    return matches(evaluation.symbol, evaluation.mint, query)


def render_by_token(rows: list[TokenRejections], checks: list[EvaluationRecord]) -> None:
    frame = pd.DataFrame(
        [
            {
                "Token": row.symbol,
                "Rejected": row.checks,
                "Why": " · ".join(rule_label(rule) for rule in row.rules),
                "Latest detail": row.latest_detail,
                "Last checked": local(row.last_at),
                "Checked again": local(row.recheck_at),
                "Chart": chart_url(row.mint),
            }
            for row in rows
        ]
    )
    mints = [row.mint for row in rows]
    event = st.dataframe(
        frame,
        hide_index=True,
        width="stretch",
        height=min(560, 38 + 35 * len(rows)),
        on_select="rerun",
        selection_mode="single-row",
        key=table_key("rej-tokens", mints),
        column_config={
            "Rejected": st.column_config.NumberColumn("Times", format="%d×", width="small"),
            "Why": st.column_config.TextColumn(width="medium"),
            "Latest detail": st.column_config.TextColumn(width="large"),
            "Last checked": st.column_config.DatetimeColumn(format="DD MMM HH:mm"),
            "Checked again": st.column_config.DatetimeColumn(
                format="HH:mm", help="The Filter looks at it again after a cooldown."
            ),
            "Chart": st.column_config.LinkColumn(display_text="DexScreener", width="small"),
        },
    )
    st.caption("Click a row to see every check of that token.")
    chosen = remember_selection(event, mints, "rej_token")
    if chosen is not None:
        render_token_history(next(row for row in rows if row.mint == chosen), checks)


def render_token_history(row: TokenRejections, history: list[EvaluationRecord]) -> None:
    checks = sorted((e for e in history if e.mint == row.mint), key=lambda e: e.at, reverse=True)
    with st.container(border=True):
        st.markdown(f"#### {row.symbol}: rejected {row.checks} times")
        st.caption(
            f"First rejected {when(row.first_at)} · last {when(row.last_at)} · "
            f"[DexScreener chart]({chart_url(row.mint)}) · `{row.mint}`"
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Time": local(e.at),
                        "Reason": " · ".join(kill.label for kill in kills_of(e)),
                        "Detail": " · ".join(kill.detail for kill in kills_of(e)),
                    }
                    for e in checks
                ]
            ),
            hide_index=True,
            width="stretch",
            height=min(320, 38 + 35 * len(checks)),
            column_config={
                "Time": st.column_config.DatetimeColumn(format="DD MMM HH:mm:ss"),
                "Detail": st.column_config.TextColumn(width="large"),
            },
        )


def render_every_check(checks: list[EvaluationRecord]) -> None:
    frame = pd.DataFrame(
        [
            {
                "Time": local(e.at),
                "Token": e.symbol,
                "Reason": " · ".join(kill.label for kill in kills_of(e)),
                "Detail": " · ".join(kill.detail for kill in kills_of(e)),
                "Chart": chart_url(e.mint),
            }
            for e in sorted(checks, key=lambda e: e.at, reverse=True)
        ]
    )
    st.dataframe(
        frame,
        hide_index=True,
        width="stretch",
        height=560,
        column_config={
            "Time": st.column_config.DatetimeColumn(format="DD MMM HH:mm:ss"),
            "Detail": st.column_config.TextColumn(width="large"),
            "Chart": st.column_config.LinkColumn(display_text="DexScreener", width="small"),
        },
    )


def render_rejections(settings: Settings, snapshot: Snapshot) -> None:
    st.title("Filter rejections")
    st.caption(
        "Tokens turned away by the hard rules before any debate. The Filter re-checks a "
        "rejected token after 30 minutes, so one token can be rejected many times."
    )
    rejected = [e for e in snapshot.evaluations if e.outcome == "filtered"]
    if not rejected:
        st.info("No Filter rejections in this time window.")
        return
    view, hide_missing, rules, query = render_controls(rejected)
    shown = [e for e in rejected if keep(e, hide_missing, rules, query)]
    tokens = rejections_by_token(shown)

    tiles = st.columns(3)
    tiles[0].metric("Tokens rejected", f"{len(tokens):,}", border=True)
    tiles[1].metric("Rejections", f"{len(shown):,}", border=True)
    top = Counter(kill.rule for e in shown for kill in kills_of(e)).most_common(1)
    tiles[2].metric("Most common reason", rule_label(top[0][0]) if top else "–", border=True)

    if not shown:
        st.info("Nothing matches these filters.")
        return
    if view == BY_TOKEN:
        render_by_token(tokens, shown)
    else:
        render_every_check(shown)


show(render_rejections)
