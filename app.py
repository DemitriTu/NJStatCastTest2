"""
NJ Stat Cast — Streamlit multipage app entry.
"""

from __future__ import annotations

import streamlit as st

from dashboard import (
    BASKETBALL_CONFIG,
    FOOTBALL_CONFIG,
    PAGE_ICON,
    inject_app_styles,
    render_home_page,
)

HOME_SPORTS = [BASKETBALL_CONFIG, FOOTBALL_CONFIG]


def _home() -> None:
    inject_app_styles()
    render_home_page(HOME_SPORTS)


st.set_page_config(
    page_title="NJ Stat Cast",
    page_icon=PAGE_ICON,
    layout="wide",
    initial_sidebar_state="collapsed",
)

pg = st.navigation(
    [
        st.Page(_home, title="Home", default=True),
        st.Page(BASKETBALL_CONFIG.page_path, title="Basketball"),
        st.Page(FOOTBALL_CONFIG.page_path, title="Football"),
    ],
    position="top",
)
pg.run()
