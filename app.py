"""
NJ Stat Cast — Streamlit multipage app entry (homepage).
"""

from __future__ import annotations

import streamlit as st

from dashboard import DARK_CSS

BASKETBALL_PAGE = "pages/1_Basketball.py"


def main() -> None:
    st.set_page_config(
        page_title="NJ Stat Cast",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(DARK_CSS, unsafe_allow_html=True)

    st.title("NJ Stat Cast")

    if st.button("Basketball", type="primary"):
        st.switch_page(BASKETBALL_PAGE)


if __name__ == "__main__":
    main()
