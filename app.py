"""
NJ Stat Cast — Streamlit multipage app entry.
"""

from __future__ import annotations

import streamlit as st

from dashboard import PAGE_ICON, inject_app_styles, render_home_page

BASKETBALL_PAGE = "pages/1_Basketball.py"


def _home() -> None:
    inject_app_styles()
    render_home_page(BASKETBALL_PAGE)


st.set_page_config(
    page_title="NJ Stat Cast",
    page_icon=PAGE_ICON,
    layout="wide",
    initial_sidebar_state="collapsed",
)

pg = st.navigation(
    [
        st.Page(_home, title="Home", default=True),
        st.Page(BASKETBALL_PAGE, title="Basketball"),
    ]
)
pg.run()
