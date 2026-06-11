"""
NJ Stat Cast — Streamlit multipage app entry (homepage).
"""

from __future__ import annotations

import streamlit as st

from dashboard import PAGE_ICON, inject_app_styles, render_home_page

BASKETBALL_PAGE = "pages/1_Basketball.py"


def main() -> None:
    st.set_page_config(
        page_title="NJ Stat Cast",
        page_icon=PAGE_ICON,
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    inject_app_styles()
    render_home_page(BASKETBALL_PAGE)


if __name__ == "__main__":
    main()
