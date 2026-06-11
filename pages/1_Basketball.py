"""NJ high school basketball margin leaderboard."""

from __future__ import annotations

import streamlit as st

from dashboard import inject_app_styles, render_basketball_page

st.set_page_config(
    page_title="Basketball Rankings | NJ Stat Cast",
    page_icon="🏀",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_app_styles()
render_basketball_page()
