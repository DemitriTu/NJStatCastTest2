"""Shared dashboard logic for NJ Stat Cast basketball rankings."""

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
NET_WEIGHT_WIN = 0.3
NET_WEIGHT_SOS = 0.5
NET_WEIGHT_MARGIN = 0.2
NET_COMPONENTS = ("Win_Pct", "SOS", "Avg_Margin")
NET_WEIGHTS = {
    "Win_Pct": NET_WEIGHT_WIN,
    "SOS": NET_WEIGHT_SOS,
    "Avg_Margin": NET_WEIGHT_MARGIN,
}
# Full statewide run: conferences + one schedule page per team for SOS (often 1–3+ hours).
SCRAPER_TIMEOUT_SEC = int(os.environ.get("STREAMLIT_SCRAPER_TIMEOUT_SEC", "10800"))

# (display label, header tooltip) for st.dataframe column_config
COLUMN_HELP: dict[str, tuple[str, str]] = {
    "Rank": ("Rank", "Order by Net rating within the current view (statewide or selected conference)."),
    "Net": (
        "Net",
        "Composite rating: 0.5×norm(SOS) + 0.3×norm(Win%) + 0.2×norm(Avg Margin). "
        "Each input is min–max scaled to 0–1 in the current view.",
    ),
    "Team": ("Team", "School name from NJ.com standings."),
    "Conference": ("Conference", "NJ.com conference assignment for this season."),
    "Conf_Strength": (
        "Conf Strength",
        "Average win% of all teams in this conference (statewide). "
        "Higher means a stronger league by win record.",
    ),
    "GP": ("GP", "Games played vs in-state opponents (from schedule when available)."),
    "Win_Pct": (
        "Win%",
        "Winning percentage vs in-state opponents only (Opponent_Slug present). "
        "Out-of-state/national games excluded when schedule data exists.",
    ),
    "PF": ("PF", "Total points scored vs in-state opponents (when schedule data exists)."),
    "PA": ("PA", "Total points allowed vs in-state opponents (when schedule data exists)."),
    "Pace": (
        "Pace",
        "Average of points for and points against per game: ((PF/GP) + (PA/GP)) ÷ 2.",
    ),
    "Avg_Margin": (
        "Avg Margin",
        "Average scoring margin vs in-state opponents: (PF − PA) ÷ GP. "
        "National/out-of-state games excluded when schedule data exists.",
    ),
    "SOS": (
        "SOS",
        "Strength of schedule vs in-state opponents: "
        "(2 × opponents' avg win% + opponents' opponents' avg win%) ÷ 3.",
    ),
    "Opp_Win_Pct": (
        "Opp Win%",
        "Average win% of in-state opponents on this team's schedule.",
    ),
    "Opp_Opp_Win_Pct": (
        "Opp Opp Win%",
        "Average win% of in-state opponents' opponents.",
    ),
}

APP_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

    :root {
        --nj-bg: #09090b;
        --nj-surface: #18181b;
        --nj-surface-raised: #1f1f23;
        --nj-border: #27272a;
        --nj-border-subtle: #3f3f46;
        --nj-text: #fafafa;
        --nj-text-muted: #a1a1aa;
        --nj-text-faint: #71717a;
        --nj-accent: #2563eb;
        --nj-accent-hover: #1d4ed8;
        --nj-radius: 12px;
        --nj-radius-sm: 8px;
    }

    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }

    .stApp {
        background: linear-gradient(180deg, #09090b 0%, #0c0c0f 100%);
        color: var(--nj-text);
    }

    .block-container {
        max-width: 1200px;
        padding-top: 2.5rem;
        padding-bottom: 3rem;
    }

    header[data-testid="stHeader"] {
        background: rgba(9, 9, 11, 0.85);
        backdrop-filter: blur(8px);
        border-bottom: 1px solid var(--nj-border);
    }

    [data-testid="stSidebar"] {
        background: var(--nj-surface);
        border-right: 1px solid var(--nj-border);
    }

    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] .stCaption {
        color: var(--nj-text-muted) !important;
    }

    [data-testid="stSidebar"] h1,
    [data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3 {
        color: var(--nj-text) !important;
        font-size: 0.875rem !important;
        font-weight: 600 !important;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }

    #MainMenu, footer, header[data-testid="stHeader"] nav {
        visibility: hidden;
    }

    [data-testid="stMarkdownContainer"] h1,
    [data-testid="stMarkdownContainer"] h2,
    [data-testid="stMarkdownContainer"] h3 {
        color: var(--nj-text) !important;
        letter-spacing: -0.02em;
    }

    .stCaption, [data-testid="stMarkdownContainer"] p {
        color: var(--nj-text-muted) !important;
        line-height: 1.6;
    }

    .stButton > button[kind="primary"],
    .stButton > button[data-testid="stBaseButton-primary"] {
        background: var(--nj-accent) !important;
        color: #fff !important;
        border: none !important;
        border-radius: var(--nj-radius-sm) !important;
        font-weight: 600 !important;
        padding: 0.625rem 1.25rem !important;
        transition: background 0.15s ease, transform 0.15s ease;
    }

    .stButton > button[kind="primary"]:hover,
    .stButton > button[data-testid="stBaseButton-primary"]:hover {
        background: var(--nj-accent-hover) !important;
        border: none !important;
        color: #fff !important;
    }

    .stButton > button {
        border-radius: var(--nj-radius-sm) !important;
        font-weight: 500 !important;
    }

    [data-testid="stMetric"] {
        background: var(--nj-surface);
        border: 1px solid var(--nj-border);
        border-radius: var(--nj-radius);
        padding: 1rem 1.25rem;
    }

    [data-testid="stMetric"] label {
        color: var(--nj-text-faint) !important;
        font-size: 0.75rem !important;
        font-weight: 600 !important;
        letter-spacing: 0.06em;
        text-transform: uppercase;
    }

    [data-testid="stMetric"] div[data-testid="stMetricValue"] {
        color: var(--nj-text) !important;
        font-weight: 700 !important;
        letter-spacing: -0.02em;
    }

    [data-testid="stDataFrame"] {
        border: 1px solid var(--nj-border);
        border-radius: var(--nj-radius);
        overflow: hidden;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.24);
    }

    [data-testid="stDataFrame"] div[role="columnheader"] {
        background: #f4f4f5 !important;
        font-weight: 600 !important;
        font-size: 0.8125rem !important;
    }

    [data-testid="stDataFrame"] div[role="gridcell"],
    [data-testid="stDataFrame"] span {
        font-size: 0.875rem !important;
    }

    [data-baseweb="select"] > div,
    [data-testid="stTextInput"] input,
    [data-testid="stTextArea"] textarea {
        background: var(--nj-surface-raised) !important;
        border: 1px solid var(--nj-border) !important;
        border-radius: var(--nj-radius-sm) !important;
        color: var(--nj-text) !important;
    }

    [data-testid="stExpander"] {
        background: var(--nj-surface);
        border: 1px solid var(--nj-border);
        border-radius: var(--nj-radius);
    }

    hr {
        border-color: var(--nj-border) !important;
        margin: 2rem 0 !important;
    }

    .nj-hero {
        text-align: center;
        padding: 3.5rem 1rem 2.5rem;
        margin-bottom: 1rem;
    }

    .nj-eyebrow {
        display: inline-block;
        margin: 0 0 1rem;
        padding: 0.35rem 0.75rem;
        font-size: 0.75rem;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #93c5fd !important;
        background: rgba(37, 99, 235, 0.12);
        border: 1px solid rgba(37, 99, 235, 0.25);
        border-radius: 999px;
    }

    .nj-hero h1 {
        margin: 0 0 0.75rem;
        font-size: clamp(2.25rem, 5vw, 3.25rem);
        font-weight: 700;
        letter-spacing: -0.04em;
        line-height: 1.1;
        color: var(--nj-text) !important;
    }

    .nj-hero-sub {
        max-width: 34rem;
        margin: 0 auto;
        font-size: 1.0625rem;
        color: var(--nj-text-muted) !important;
    }

    .nj-card {
        background: var(--nj-surface);
        border: 1px solid var(--nj-border);
        border-radius: var(--nj-radius);
        padding: 1.5rem;
        transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }

    .nj-card:hover {
        border-color: var(--nj-border-subtle);
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
    }

    .nj-card-label {
        margin: 0 0 0.35rem;
        font-size: 0.6875rem;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--nj-text-faint) !important;
    }

    .nj-card h3 {
        margin: 0 0 0.5rem;
        font-size: 1.25rem;
        font-weight: 600;
        color: var(--nj-text) !important;
    }

    .nj-card p {
        margin: 0;
        font-size: 0.9375rem;
        color: var(--nj-text-muted) !important;
    }

    .nj-page-header {
        margin-bottom: 1.75rem;
        padding-bottom: 1.5rem;
        border-bottom: 1px solid var(--nj-border);
    }

    .nj-page-header h1 {
        margin: 0.5rem 0 0.5rem;
        font-size: 2rem;
        font-weight: 700;
        letter-spacing: -0.03em;
    }

    .nj-page-sub {
        margin: 0;
        max-width: 42rem;
        font-size: 1rem;
        color: var(--nj-text-muted) !important;
    }

    .nj-section-title {
        margin: 0 0 0.35rem;
        font-size: 1.125rem;
        font-weight: 600;
        color: var(--nj-text) !important;
        letter-spacing: -0.01em;
    }

    .nj-section-desc {
        margin: 0 0 1rem;
        font-size: 0.875rem;
        color: var(--nj-text-faint) !important;
    }

    .nj-sidebar-meta {
        margin: 0 0 1rem;
        padding: 0.75rem 0.875rem;
        background: var(--nj-surface-raised);
        border: 1px solid var(--nj-border);
        border-radius: var(--nj-radius-sm);
        font-size: 0.8125rem;
        color: var(--nj-text-muted) !important;
    }

    .nj-sidebar-meta strong {
        display: block;
        margin-bottom: 0.15rem;
        font-size: 0.6875rem;
        font-weight: 600;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: var(--nj-text-faint);
    }
</style>
"""

DARK_CSS = APP_CSS


def inject_app_styles() -> None:
    st.markdown(APP_CSS, unsafe_allow_html=True)


def render_home_page(basketball_page: str) -> None:
    st.markdown(
        """
        <div class="nj-hero">
            <p class="nj-eyebrow">New Jersey High School Sports</p>
            <h1>NJ Stat Cast</h1>
            <p class="nj-hero-sub">
                Rankings and analytics built from NJ.com standings and schedules.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    _, center, _ = st.columns([1, 1.4, 1])
    with center:
        st.markdown(
            """
            <div class="nj-card">
                <p class="nj-card-label">Season analytics</p>
                <h3>Basketball</h3>
                <p>Statewide Net ratings, conference filters, strength of schedule, and league strength.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.write("")
        if st.button("Open basketball rankings", type="primary", use_container_width=True):
            st.switch_page(basketball_page)


def _minmax_normalize(series: pd.Series) -> pd.Series:
    valid = series.dropna()
    if valid.empty:
        return pd.Series(0.0, index=series.index)
    lo, hi = valid.min(), valid.max()
    if hi == lo:
        return pd.Series(0.5, index=series.index)
    return ((series - lo) / (hi - lo)).fillna(0.0)


def _leaderboard_column_config(columns: list[str]) -> dict[str, st.column_config.Column]:
    configs: dict[str, st.column_config.Column] = {}
    for col in columns:
        meta = COLUMN_HELP.get(col)
        if not meta:
            continue
        label, help_text = meta
        if col in ("Team", "Conference"):
            configs[col] = st.column_config.TextColumn(label, help=help_text)
        elif col == "Rank":
            configs[col] = st.column_config.NumberColumn(label, help=help_text, format="%d")
        elif col in ("PF", "PA", "GP"):
            configs[col] = st.column_config.NumberColumn(label, help=help_text, format="%d")
        elif col in ("Win_Pct", "SOS", "Opp_Win_Pct", "Opp_Opp_Win_Pct", "Net", "Conf_Strength"):
            configs[col] = st.column_config.NumberColumn(label, help=help_text, format="%.4f")
        else:
            configs[col] = st.column_config.NumberColumn(label, help=help_text, format="%.3f")
    return configs


def _is_nj_game(game: object) -> bool:
    if not isinstance(game, dict):
        return False
    return bool(str(game.get("Opponent_Slug") or "").strip())


def _nj_record_from_games(games: list) -> dict[str, int | float] | None:
    completed = [
        g
        for g in games
        if isinstance(g, dict) and _is_nj_game(g) and g.get("Won") is not None
    ]
    if not completed:
        return None
    wins = sum(1 for g in completed if g["Won"])
    gp = len(completed)
    losses = gp - wins
    pf = sum(int(g["PF"]) for g in completed)
    pa = sum(int(g["PA"]) for g in completed)
    return {
        "Wins": wins,
        "Losses": losses,
        "GP": gp,
        "PF": pf,
        "PA": pa,
        "Win_Pct": round(wins / gp, 4) if gp else 0.0,
        "Avg_Margin": round((pf - pa) / gp, 3) if gp else 0.0,
    }


def _nj_opponent_slugs(games: list, self_slug: str) -> list[str]:
    slugs: list[str] = []
    seen: set[str] = set()
    for game in games:
        if not isinstance(game, dict) or not _is_nj_game(game):
            continue
        opp = str(game.get("Opponent_Slug") or "").strip()
        if not opp or opp == self_slug or opp in seen:
            continue
        seen.add(opp)
        slugs.append(opp)
    return slugs


def _avg_win_pct(slugs: list[str], win_pct: dict[str, float]) -> float | None:
    vals = [win_pct[s] for s in slugs if s in win_pct]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _recompute_sos_on_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "School_Slug" not in out.columns:
        return out

    win_pct: dict[str, float] = {}
    for _, row in out.iterrows():
        slug = str(row.get("School_Slug") or "").strip()
        if not slug:
            continue
        wp = row.get("Win_Pct")
        if wp is not None and not pd.isna(wp):
            win_pct[slug] = float(wp)

    opponents_by_slug: dict[str, list[str]] = {}
    for _, row in out.iterrows():
        slug = str(row.get("School_Slug") or "").strip()
        if not slug:
            continue
        games = row.get("Games")
        opponents_by_slug[slug] = _nj_opponent_slugs(games, slug) if isinstance(games, list) else []

    opp_win: list[float | None] = []
    opp_opp_win: list[float | None] = []
    sos_vals: list[float | None] = []
    for _, row in out.iterrows():
        slug = str(row.get("School_Slug") or "").strip()
        if not slug:
            opp_win.append(None)
            opp_opp_win.append(None)
            sos_vals.append(None)
            continue
        opps = opponents_by_slug.get(slug, [])
        ow = _avg_win_pct(opps, win_pct)
        oow_parts: list[float] = []
        for opp in opps:
            sub = _avg_win_pct(opponents_by_slug.get(opp, []), win_pct)
            if sub is not None:
                oow_parts.append(sub)
        oow = sum(oow_parts) / len(oow_parts) if oow_parts else None
        opp_win.append(round(ow, 4) if ow is not None else None)
        opp_opp_win.append(round(oow, 4) if oow is not None else None)
        if ow is not None and oow is not None:
            sos_vals.append(round((2 * ow + oow) / 3, 4))
        else:
            sos_vals.append(None)

    out["Opp_Win_Pct"] = opp_win
    out["Opp_Opp_Win_Pct"] = opp_opp_win
    out["SOS"] = sos_vals
    return out


def _apply_nj_only_stats(df: pd.DataFrame) -> pd.DataFrame:
    """Replace season totals with stats from in-state games only (Opponent_Slug set)."""
    out = df.copy()
    if "Games" not in out.columns:
        return out

    any_updated = False
    for idx, row in out.iterrows():
        games = row.get("Games")
        if not isinstance(games, list) or not games:
            continue
        record = _nj_record_from_games(games)
        if record is None:
            continue
        for key, val in record.items():
            out.at[idx, key] = val
        any_updated = True

    if any_updated:
        out = _recompute_sos_on_dataframe(out)
    return out


def _add_pace(df: pd.DataFrame) -> pd.DataFrame:
    """Pace = average of PF/GP and PA/GP (points per game for and against)."""
    out = df.copy()
    if not {"PF", "PA", "GP"}.issubset(out.columns):
        return out
    gp = pd.to_numeric(out["GP"], errors="coerce")
    pf = pd.to_numeric(out["PF"], errors="coerce")
    pa = pd.to_numeric(out["PA"], errors="coerce")
    out["Pace"] = ((pf + pa) / (2 * gp)).where(gp.gt(0)).round(1)
    return out


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


def _build_h2h_winners(df: pd.DataFrame) -> dict[tuple[str, str], str]:
    """Decisive head-to-head series winner keyed by sorted slug pair."""
    win_counts: dict[tuple[str, str], dict[str, int]] = {}
    if "School_Slug" not in df.columns or "Games" not in df.columns:
        return {}

    for _, row in df.iterrows():
        slug = str(row.get("School_Slug") or "").strip()
        games = row.get("Games")
        if not slug or not isinstance(games, list):
            continue
        for game in games:
            if not isinstance(game, dict):
                continue
            opp = str(game.get("Opponent_Slug") or "").strip()
            won = game.get("Won")
            if not opp or won is None:
                continue
            pair = tuple(sorted((slug, opp)))
            bucket = win_counts.setdefault(pair, {})
            winner = slug if won else opp
            bucket[winner] = bucket.get(winner, 0) + 1

    winners: dict[tuple[str, str], str] = {}
    for pair, counts in win_counts.items():
        a, b = pair
        ca, cb = counts.get(a, 0), counts.get(b, 0)
        if ca > cb:
            winners[pair] = a
        elif cb > ca:
            winners[pair] = b
    return winners


def _h2h_winner(slug_a: str, slug_b: str, h2h: dict[tuple[str, str], str]) -> str | None:
    if not slug_a or not slug_b:
        return None
    return h2h.get(tuple(sorted((slug_a, slug_b))))


def _apply_h2h_adjacent_swaps(df: pd.DataFrame, h2h: dict[tuple[str, str], str]) -> pd.DataFrame:
    if df.empty or not h2h:
        return df
    rows = df.to_dict("records")
    changed = True
    while changed:
        changed = False
        for i in range(len(rows) - 1):
            slug_a = str(rows[i].get("School_Slug") or "").strip()
            slug_b = str(rows[i + 1].get("School_Slug") or "").strip()
            if _h2h_winner(slug_a, slug_b, h2h) == slug_b:
                rows[i], rows[i + 1] = rows[i + 1], rows[i]
                changed = True
    return pd.DataFrame(rows)


def _rank_by_net(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = _add_net_rating(df)
    out = out.sort_values("Net", ascending=False, na_position="last").reset_index(drop=True)
    h2h = _build_h2h_winners(out)
    out = _apply_h2h_adjacent_swaps(out, h2h).reset_index(drop=True)
    if "Rank" in out.columns:
        out = out.drop(columns=["Rank"])
    out.insert(0, "Rank", range(1, len(out) + 1))
    return out


def _add_conference_strength(df: pd.DataFrame) -> pd.DataFrame:
    """Statewide mean Win_Pct by conference, mapped to each team row."""
    out = df.copy()
    if "Conference" not in out.columns or "Win_Pct" not in out.columns:
        return out
    out["Conf_Strength"] = (
        out.groupby("Conference", dropna=False)["Win_Pct"].transform("mean").round(4)
    )
    return out


def _conference_strength_chart_df(df: pd.DataFrame) -> pd.DataFrame | None:
    """One row per conference, highest Conf Strength first (top of horizontal chart)."""
    if "Conference" not in df.columns or "Conf_Strength" not in df.columns:
        return None
    chart = (
        df.dropna(subset=["Conference", "Conf_Strength"])
        .groupby("Conference", as_index=False)["Conf_Strength"]
        .first()
        .sort_values("Conf_Strength", ascending=False)
    )
    if chart.empty:
        return None
    order = chart["Conference"].tolist()
    chart["Conference"] = pd.Categorical(chart["Conference"], categories=order, ordered=True)
    return chart[["Conference", "Conf_Strength"]].reset_index(drop=True)


def _render_conference_strength_chart(chart: pd.DataFrame) -> None:
    chart_args = {
        "data": chart,
        "x": "Conference",
        "y": "Conf_Strength",
        "horizontal": True,
        "color": "#3b82f6",
    }
    try:
        st.bar_chart(**chart_args, sort="-Conf_Strength")
    except TypeError:
        st.bar_chart(**chart_args, sort=False)


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
    df = _apply_nj_only_stats(df)
    df = _add_pace(df)
    df = _rank_by_net(df)
    df = _add_conference_strength(df)
    return df, payload.get("last_updated")


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


def render_basketball_page() -> None:
    st.markdown(
        """
        <div class="nj-page-header">
            <p class="nj-eyebrow">Basketball</p>
            <h1>Statewide Rankings</h1>
            <p class="nj-page-sub">
                Net rating leaderboard with in-state schedule adjustments and head-to-head tiebreakers.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.expander("How rankings are calculated"):
        st.markdown(
            "Rankings use **Net = 0.5×norm(SOS) + 0.3×norm(Win%) + 0.2×norm(Avg Margin)**, "
            "with each stat min–max scaled to 0–1 within the current view (statewide or conference). "
            "Win%, margin, and SOS use in-state opponents only; out-of-state games are excluded when "
            "schedule data exists. Adjacent teams may swap when the lower-Net team won the head-to-head series."
        )

    with st.sidebar:
        df_preview, last_updated = load_cached_data()
        st.markdown(
            f'<div class="nj-sidebar-meta"><strong>Last updated</strong>{_format_timestamp(last_updated)}</div>',
            unsafe_allow_html=True,
        )
        with st.expander("Refresh data", expanded=False):
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
                help="Load data_cache.json and run schedule + SOS only.",
            )
            trigger_scrape = st.button(
                "Run scraper",
                type="primary",
                use_container_width=True,
            )

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
                        else "Running scraper (standings + schedules for SOS)…"
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
                    df_preview, last_updated = load_cached_data()
                else:
                    st.error(msg)

    df = df_preview
    if df is None:
        st.info("No data found. Open **Refresh data** in the sidebar to run the scraper.")
        return

    if "Conference" in df.columns:
        conferences = sorted(
            c for c in df["Conference"].dropna().astype(str).unique() if c.strip()
        )
    else:
        conferences = []

    filter_col, metric_col = st.columns([3, 1])
    with filter_col:
        selected = st.selectbox(
            "Conference",
            options=[ALL_CONFERENCES] + conferences,
            index=0,
        )
    with metric_col:
        view = _filter_and_rank(df, selected)
        st.metric("Teams", len(view))

    st.markdown(
        '<p class="nj-section-title">Leaderboard</p>'
        '<p class="nj-section-desc">Sorted by Net within the selected view.</p>',
        unsafe_allow_html=True,
    )

    display_cols = [
        c
        for c in [
            "Rank",
            "Net",
            "Team",
            "Conference",
            "Conf_Strength",
            "GP",
            "Win_Pct",
            "PF",
            "PA",
            "Pace",
            "Avg_Margin",
            "SOS",
        ]
        if c in view.columns
    ]
    if view.empty and selected != ALL_CONFERENCES:
        st.warning(f"No teams found for conference: {selected}")
    table = view[display_cols] if not view.empty else view
    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config=_leaderboard_column_config(display_cols),
    )

    conf_chart = _conference_strength_chart_df(df)
    if conf_chart is not None:
        st.divider()
        st.markdown(
            '<p class="nj-section-title">Conference strength</p>'
            '<p class="nj-section-desc">Average statewide win% by conference.</p>',
            unsafe_allow_html=True,
        )
        _render_conference_strength_chart(conf_chart)
