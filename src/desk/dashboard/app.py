"""The dashboard. Start it with `desk ui`.

Read-only: it shows what the desk is doing and never changes it. Each page lives in
`views/`; this file holds the navigation and the controls every page shares.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from desk.dashboard.common import (
    DEFAULT_REFRESH,
    DEFAULT_WINDOW,
    REFRESH_CHOICES,
    STYLE,
    WINDOWS,
)

VIEWS = Path(__file__).parent / "views"


def main() -> None:
    st.set_page_config(page_title="Memecoin Desk", page_icon=":material/monitoring:", layout="wide")
    st.html(STYLE)

    pages = {
        "Desk": [
            st.Page(VIEWS / "overview.py", title="Overview", icon=":material/dashboard:"),
            st.Page(VIEWS / "trades.py", title="Trades", icon=":material/swap_horiz:"),
        ],
        "Decisions": [
            st.Page(VIEWS / "debates.py", title="Debates", icon=":material/forum:"),
            st.Page(VIEWS / "rejections.py", title="Filter rejections", icon=":material/block:"),
        ],
        "Records": [
            st.Page(VIEWS / "activity.py", title="Lessons and log", icon=":material/article:"),
        ],
    }
    page = st.navigation(pages)

    with st.sidebar:
        st.selectbox(
            "Time window", list(WINDOWS), index=list(WINDOWS).index(DEFAULT_WINDOW), key="window"
        )
        st.toggle("Refresh automatically", value=True, key="auto_refresh")
        st.select_slider(
            "Every (seconds)",
            REFRESH_CHOICES,
            value=DEFAULT_REFRESH,
            key="refresh_seconds",
            disabled=not st.session_state.get("auto_refresh", True),
        )
        st.caption("Read-only. To pause new entries, create the file `data/HALT`.")

    page.run()


main()
