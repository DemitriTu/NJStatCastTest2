"""
Streamlit dashboard: NJ high school basketball margin leaderboard.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_CACHE_JSON = SCRIPT_DIR / "data_cache.json"
SCRAPER = SCRIPT_DIR / "scraper.py"
DEFAULT_SEASON = "2025-2026"
ALL_CONFERENCES = "All conferences"
NET_WEIGHT_WIN = 0.4
NET_WEIGHT_SOS = 0.4
NET_WEIGHT_MARGIN = 0.2
NET_COMPONENTS = ("Win_Pct", "SOS", "Avg_Margin")
NET_WEIGHTS = {
    "Win_Pct": NET_WEIGHT_WIN,
    "SOS": NET_WEIGHT_SOS,
    "Avg_Margin": NET_WEIGHT_MARGIN,
}
# Full statewide run: conferences + one schedule page per team for SOS (often 1–3+ hours).
SCRAPER_TIMEOUT_SEC = int(os.environ.get("STREAMLIT_SCRAPER_TIMEOUT_SEC", "10800"))

DARK_CSS = """
<style>
    .stApp {
        background-color: #0d1117;
        color: #f0f6fc;
    }
    .stApp header[data-testid="stHeader"] {
        background-color: #010409;
        border-bottom: 1px solid #30363d;
    }
    [data-testid="stMarkdownContainer"] p, h1, h2, h3 {
        color: #f0f6fc !important;
    }
    div[data-testid="stVerticalBlock"] > div {
        color: #f0f6fc;
    }
    [data-testid="stDataFrame"] {
        border: 1px solid #30363d;
        border-radius: 6px;
    }
    .stButton > button {
        background-color: #238636;
        color: #ffffff;
        border: none;
        font-weight: 600;
    }
    .stButton > button:hover {
        background-color: #2ea043;
        color: #ffffff;
        border: none;
    }
    [data-baseweb="select"] > div {
        background-color: #161b22;
        color: #f0f6fc;
    }
    .stCaption, .stMetric label {
        color: #8b949e !important;
    }
</style>
"""


def _minmax_normalize(series: pd.Series) -> pd.Series:
    valid = series.dropna()
    if valid.empty:
        return pd.Series(0.0, index=series.index)
    lo, hi = valid.min(), valid.max()
    if hi == lo:
        return pd.Series(0.5, index=series.index)
    return ((series - lo) / (hi - lo)).fillna(0.0)


def _add_net_rating(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    net = pd.Series(0.0, index=out.index)
    for col in NET_COMPONENTS:
        if col not in out.columns:
            continue
        normed = _minmax_normalize(pd.to_numeric(out[col], errors="coerce"))
        net = net + normed * NET_WEIGHTS[col]
    out["Net"] = net.round(4)
    return out


def _rank_by_net(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = _add_net_rating(df)
    out = out.sort_values("Net", ascending=False, na_position="last").reset_index(drop=True)
    if "Rank" in out.columns:
        out = out.drop(columns=["Rank"])
    out.insert(0, "Rank", range(1, len(out) + 1))
    return out


def _format_timestamp(ts: str | None) -> str:
    if not ts:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return ts


def load_cached_data() -> tuple[pd.DataFrame | None, str | None]:
    if not DATA_CACHE_JSON.is_file():
        return None, None
    try:
        payload = json.loads(DATA_CACHE_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None
    teams = payload.get("teams")
    if not teams:
        return None, payload.get("last_updated")
    df = pd.DataFrame(teams)
    return _rank_by_net(df), payload.get("last_updated")


def _filter_and_rank(df: pd.DataFrame, conference: str | None) -> pd.DataFrame:
    if not conference or conference == ALL_CONFERENCES:
        return df
    view = df[df["Conference"] == conference].copy()
    if view.empty:
        return view
    return _rank_by_net(view)


def run_scraper(
    *,
    season: str,
    single_url: str | None,
    skip_schedule: bool,
    sos_only: bool,
) -> tuple[bool, str]:
    env = os.environ.copy()
    env["NJ_STANDINGS_SEASON"] = season.strip()
    env.pop("NJ_STANDINGS_URL", None)
    cmd = [sys.executable, str(SCRAPER), "--season", season.strip()]
    if sos_only:
        cmd.append("--sos-only")
        cmd.extend(["--cache-in", str(DATA_CACHE_JSON)])
    else:
        if single_url and single_url.strip():
            cmd.extend(["--url", single_url.strip()])
            env["NJ_STANDINGS_URL"] = single_url.strip()
        if skip_schedule:
            cmd.append("--skip-schedule")
    try:
        r = subprocess.run(
            cmd,
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            text=True,
            timeout=SCRAPER_TIMEOUT_SEC,
            env=env,
        )
        msg = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
        return r.returncode == 0, msg.strip() or ("OK" if r.returncode == 0 else "Scraper failed")
    except subprocess.TimeoutExpired:
        return False, f"Scraper timed out after {SCRAPER_TIMEOUT_SEC}s"
    except Exception as e:
        return False, str(e)


def main() -> None:
    st.set_page_config(
        page_title="NJ Basketball — Avg Win Margin",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(DARK_CSS, unsafe_allow_html=True)

    st.title("NJ High School Basketball — Average Win Margin")
    st.caption(
        "Rankings use Net = 0.4×norm(Win%) + 0.4×norm(SOS) + 0.2×norm(Avg Margin), "
        "with each stat min–max scaled to 0–1 within the current view (statewide or conference). "
        "SOS = (2 × opponents’ avg win% + opponents’ opponents’ avg win%) / 3. "
        "Full refresh can take hours. If the run times out, standings are still saved first; "
        "use sidebar “Resume SOS only” to finish schedules without re-scraping standings."
    )

    with st.sidebar:
        st.subheader("Data source")
        season = st.text_input(
            "Season",
            value=DEFAULT_SEASON,
            help="Season folder on NJ.com, e.g. 2025-2026.",
        )
        single_url = st.text_input(
            "Single conference URL (optional)",
            value="",
            placeholder="Leave empty to scrape all conferences",
            help="If set, only this page is scraped instead of the full conference list.",
        )
        skip_schedule = st.checkbox(
            "Skip schedule / SOS (faster)",
            value=False,
            help="Only scrape standings; leave SOS columns empty.",
        )
        sos_only = st.checkbox(
            "Resume SOS only (from cache)",
            value=False,
            help="Load data_cache.json and run schedule + SOS only. Use after a timeout or to refresh SOS without re-scraping standings.",
        )
        trigger_scrape = st.button("Trigger Fresh Scrape", type="primary", use_container_width=True)

    if trigger_scrape:
        if sos_only and skip_schedule:
            st.error('Uncheck "Skip schedule" or "Resume SOS only" — they cannot be used together.')
        else:
            spin = (
                "Running SOS from cache…"
                if sos_only
                else (
                    "Running scraper (standings only)…"
                    if skip_schedule
                    else "Running scraper (standings + schedules for SOS; can take many minutes)…"
                )
            )
            with st.spinner(spin):
                ok, msg = run_scraper(
                    season=season,
                    single_url=single_url or None,
                    skip_schedule=skip_schedule,
                    sos_only=sos_only,
                )
            if ok:
                st.success(msg)
            else:
                st.error(msg)

    df, last_updated = load_cached_data()
    with st.sidebar:
        st.caption(f"Last Updated: {_format_timestamp(last_updated)}")

    if df is None:
        st.info("No data found. Please run the scraper.")
        return

    if "Conference" in df.columns:
        conferences = sorted(
            c for c in df["Conference"].dropna().astype(str).unique() if c.strip()
        )
    else:
        conferences = []
    selected = st.selectbox(
        "Conference",
        options=[ALL_CONFERENCES] + conferences,
        index=0,
    )

    view = _filter_and_rank(df, selected)
    st.metric("Teams loaded", len(view))

    display_cols = [
        c
        for c in [
            "Rank",
            "Net",
            "Team",
            "Conference",
            "GP",
            "Win_Pct",
            "PF",
            "PA",
            "Avg_Margin",
            "SOS",
            "Opp_Win_Pct",
            "Opp_Opp_Win_Pct",
        ]
        if c in view.columns
    ]
    if view.empty and selected != ALL_CONFERENCES:
        st.warning(f"No teams found for conference: {selected}")
    st.dataframe(
        view[display_cols] if not view.empty else view,
        use_container_width=True,
        hide_index=True,
    )


if __name__ == "__main__":
    main()
