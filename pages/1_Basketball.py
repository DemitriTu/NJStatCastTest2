"""NJ high school basketball margin leaderboard."""

from __future__ import annotations

import streamlit as st

from dashboard import DARK_CSS, render_basketball_page

st.set_page_config(
    page_title="NJ Basketball — Avg Win Margin",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(DARK_CSS, unsafe_allow_html=True)
render_basketball_page()
