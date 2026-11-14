"""Lessons Claude wrote after each trade, and the desk's own log."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from desk.config import Settings
from desk.dashboard.common import local, show
from desk.dashboard.data import Snapshot

LOG_LINES_SHOWN = 300
LEVELS = ["INFO", "WARNING", "ERROR"]
NOISE = ("desk.agents.scout: scout:", "killed:")


def render_lessons(snapshot: Snapshot) -> None:
    st.subheader("Lessons")
    st.caption("After each closed trade Claude writes one. The Bear reads them in later debates.")
    if not snapshot.lessons:
        st.info("No lessons yet.")
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
        row_height=90,
        column_config={
            "When": st.column_config.DatetimeColumn(format="DD MMM HH:mm", width="small"),
            "Token": st.column_config.TextColumn(width="small"),
            "Lesson": st.column_config.TextColumn(width="large"),
        },
    )


def render_log(snapshot: Snapshot) -> None:
    st.subheader("Log")
    if not snapshot.log_tail:
        st.info("The desk has not written a log yet.")
        return
    with st.container(horizontal=True, vertical_alignment="bottom", gap="medium"):
        query = st.text_input("Search", placeholder="Token, word or agent", key="log_query")
        levels = st.pills(
            "Levels", LEVELS, selection_mode="multi", default=LEVELS, key="log_levels"
        )
    hide_noise = st.toggle(
        "Hide scout passes and Filter rejections", value=True, key="log_hide_noise"
    )
    wanted = set(levels or LEVELS)
    lines = [
        line
        for line in reversed(snapshot.log_tail)
        if any(f" {level} " in line for level in wanted)
        and not (hide_noise and any(marker in line for marker in NOISE))
        and query.strip().lower() in line.lower()
    ][:LOG_LINES_SHOWN]
    st.caption(f"Newest first · {len(lines)} lines from logs/desk.log")
    st.code("\n".join(lines) or "Nothing matches.", language=None, height=520, wrap_lines=True)


def render_activity(settings: Settings, snapshot: Snapshot) -> None:
    st.title("Lessons and log")
    render_lessons(snapshot)
    st.divider()
    render_log(snapshot)


show(render_activity)
