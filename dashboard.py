"""Shared dashboard logic for NJ Stat Cast sports rankings."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import base64

import pandas as pd
import streamlit as st
import altair as alt
from PIL import Image

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_CACHE_JSON = SCRIPT_DIR / "data_cache.json"
FOOTBALL_DATA_CACHE_JSON = SCRIPT_DIR / "football_data_cache.json"
PLAYER_DATA_CACHE_JSON = SCRIPT_DIR / "player_data_cache.json"
FOOTBALL_PLAYER_DATA_CACHE_JSON = SCRIPT_DIR / "football_player_data_cache.json"
def _image_path(*candidates: Path) -> Path:
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


LOGO_PATH = _image_path(
    SCRIPT_DIR / "Images" / "WhiteNJStatCast_nobackground.png",
    SCRIPT_DIR / "Logo" / "WhiteNJStatCast_nobackground.png",
)
LOGO_WORDMARK_PATH = _image_path(
    SCRIPT_DIR / "Images" / "WhiteNJStatCastWords_noBackground.png",
    SCRIPT_DIR / "Logo" / "WhiteNJStatCastWords_noBackground.png",
)
TAB_ICON_PATH = _image_path(
    SCRIPT_DIR / "Images" / "NJStatCast_nobackground_png.png",
    SCRIPT_DIR / "Logo" / "NJStatCast_nobackground_png.png",
)
BASKETBALL_HOME_IMAGE = _image_path(
    SCRIPT_DIR / "Images" / "basketball_black and white.png",
    SCRIPT_DIR / "Logo" / "basketball_black and white.png",
)
PAGE_ICON = str(TAB_ICON_PATH) if TAB_ICON_PATH.is_file() else "📊"

# Seasons available in the UI (oldest → newest).
AVAILABLE_SEASONS: tuple[str, ...] = (
    "2023-2024",
    "2024-2025",
    "2025-2026",
    "2026-2027",
)
DEFAULT_UI_SEASON = "2026-2027"


@dataclass(frozen=True)
class SportPageConfig:
    key: str
    label: str
    cache_path: Path
    page_path: str
    description: str
    home_image: Path | None = None
    player_cache_path: Path | None = None


BASKETBALL_CONFIG = SportPageConfig(
    key="basketball",
    label="Basketball",
    cache_path=DATA_CACHE_JSON,
    page_path="pages/1_Basketball.py",
    description="Statewide Net ratings, conference filters, strength of schedule, and league strength.",
    home_image=BASKETBALL_HOME_IMAGE if BASKETBALL_HOME_IMAGE.is_file() else None,
    player_cache_path=PLAYER_DATA_CACHE_JSON,
)

FOOTBALL_CONFIG = SportPageConfig(
    key="football",
    label="Football",
    cache_path=FOOTBALL_DATA_CACHE_JSON,
    page_path="pages/2_Football.py",
    description="Statewide Net ratings, conference filters, strength of schedule, and league strength.",
    player_cache_path=FOOTBALL_PLAYER_DATA_CACHE_JSON,
)

ALL_POSITIONS = "All positions"
FOOTBALL_POSITION_OPTIONS: tuple[str, ...] = (
    ALL_POSITIONS,
    "QB",
    "RB",
    "WR / TE",
    "OL",
    "DL",
    "LB",
    "DB",
    "Specialists",
    "Other",
)
FOOTBALL_STAT_COLUMNS: dict[str, tuple[str, ...]] = {
    "passing": ("COMP", "ATT", "YDS", "TD", "INT", "LNG"),
    "rushing": ("ATT", "YDS", "TD", "LNG"),
    "receiving": ("REC", "YDS", "TD", "LNG"),
    "defense": ("S", "TFL", "T_SOLO", "T_AST", "T_TOT", "FF", "FR", "INT"),
    "kicking": ("FGM", "FGA", "FG_LNG", "XPM", "XPA", "2PT"),
    "punting": ("PUNTS", "YDS", "LNG", "IN_20"),
    "specialists": ("FGM", "FGA", "FG_LNG", "XPM", "XPA", "2PT", "PUNTS", "YDS", "LNG", "IN_20"),
}
FOOTBALL_SPECIALIST_STAT_CATEGORIES: frozenset[str] = frozenset({"kicking", "punting"})
FOOTBALL_SPECIALTY_LABELS: dict[str, str] = {
    "kicking": "Kicking",
    "punting": "Punting",
}
FOOTBALL_STAT_CATEGORY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Passing", "passing"),
    ("Rushing", "rushing"),
    ("Receiving", "receiving"),
    ("Defense", "defense"),
    ("Specialists", "specialists"),
)
FOOTBALL_POSITION_CODE_TO_GROUP: dict[str, str] = {
    "QB": "QB",
    "RB": "RB",
    "FB": "RB",
    "WR": "WR / TE",
    "TE": "WR / TE",
    "OL": "OL",
    "OT": "OL",
    "OG": "OL",
    "C": "OL",
    "DL": "DL",
    "DE": "DL",
    "DT": "DL",
    "NG": "DL",
    "LB": "LB",
    "DB": "DB",
    "CB": "DB",
    "S": "DB",
    "FS": "DB",
    "SS": "DB",
    "K": "Specialists",
    "P": "Specialists",
    "LS": "Specialists",
}

ALL_CONFERENCES = "All conferences"
FULL_SEASON = "Full season"
NET_WEIGHT_WIN = 0.3
NET_WEIGHT_SOS = 0.5
NET_WEIGHT_MARGIN = 0.2
NET_COMPONENTS = ("Win_Pct", "SOS", "Avg_Margin")
NET_WEIGHTS = {
    "Win_Pct": NET_WEIGHT_WIN,
    "SOS": NET_WEIGHT_SOS,
    "Avg_Margin": NET_WEIGHT_MARGIN,
}


def _norm_opponent_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _parse_game_date(value: object) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _calendar_week_meta(dt: datetime) -> tuple[str, str]:
    iso = dt.isocalendar()
    key = f"{iso.year}-W{iso.week:02d}"
    monday = datetime.fromisocalendar(iso.year, iso.week, 1)
    sunday = monday + timedelta(days=6)
    if monday.year == sunday.year and monday.month == sunday.month:
        label = (
            f"Week of {monday.strftime('%b')} {monday.day}–{sunday.day}, {monday.year}"
        )
    elif monday.year == sunday.year:
        label = (
            f"Week of {monday.strftime('%b')} {monday.day}–"
            f"{sunday.strftime('%b')} {sunday.day}, {monday.year}"
        )
    else:
        label = (
            f"Week of {monday.strftime('%b')} {monday.day}, {monday.year}–"
            f"{sunday.strftime('%b')} {sunday.day}, {sunday.year}"
        )
    return key, label


def _sequential_week_meta(n: int) -> tuple[str, str]:
    return f"SEQ-{n:02d}", f"Week {n}"


def _week_key_sort_tuple(key: str) -> tuple[int, int, int]:
    if key.startswith("SEQ-"):
        try:
            return (1, int(key.split("-", 1)[1]), 0)
        except ValueError:
            return (1, 0, 0)
    if "-W" in key:
        year_s, week_s = key.split("-W", 1)
        try:
            return (0, int(year_s), int(week_s))
        except ValueError:
            return (0, 0, 0)
    return (2, 0, 0)


def _opponent_dates_by_school(players: list[dict]) -> dict[str, dict[str, list[datetime]]]:
    """Map School_Slug -> normalized opponent -> sorted unique game dates."""
    out: dict[str, dict[str, list[datetime]]] = {}
    for player in players:
        if not isinstance(player, dict):
            continue
        slug = str(player.get("School_Slug") or "").strip()
        if not slug:
            continue
        school = out.setdefault(slug, {})
        for game in player.get("Games") or []:
            if not isinstance(game, dict):
                continue
            dt = _parse_game_date(game.get("Date"))
            if dt is None:
                continue
            opp = _norm_opponent_name(game.get("Opponent"))
            if not opp:
                continue
            school.setdefault(opp, []).append(dt)
    for school in out.values():
        for opp, dates in list(school.items()):
            school[opp] = sorted(set(dates))
    return out


def _annotate_game_list(
    games: list,
    *,
    opp_dates: dict[str, list[datetime]] | None = None,
    allow_sequential: bool = True,
) -> list[dict]:
    """Copy games and attach Week_Key / Week labels (calendar when dated)."""
    annotated: list[dict] = []
    used_dates: dict[str, set[int]] = {}
    completed_idx = 0
    for raw in games:
        if not isinstance(raw, dict):
            continue
        game = dict(raw)
        dt = _parse_game_date(game.get("Date"))
        if dt is None and opp_dates:
            opp = _norm_opponent_name(game.get("Opponent"))
            candidates = opp_dates.get(opp) or []
            used = used_dates.setdefault(opp, set())
            for cand in candidates:
                ts = int(cand.timestamp())
                if ts in used:
                    continue
                dt = cand
                used.add(ts)
                game["Date"] = cand.strftime("%m/%d/%Y")
                break
        if dt is not None:
            key, label = _calendar_week_meta(dt)
            game["Week_Key"] = key
            game["Week"] = label
        elif allow_sequential and (
            game.get("Won") is not None or game.get("PF") is not None
        ):
            completed_idx += 1
            key, label = _sequential_week_meta(completed_idx)
            game["Week_Key"] = key
            game["Week"] = label
        annotated.append(game)
    return annotated


def _annotate_player_games(players: list[dict]) -> list[dict]:
    annotated_players: list[dict] = []
    for player in players:
        if not isinstance(player, dict):
            continue
        copy = dict(player)
        games = player.get("Games") or []
        copy["Games"] = _annotate_game_list(
            games if isinstance(games, list) else [],
            allow_sequential=False,
        )
        annotated_players.append(copy)
    return annotated_players


def _annotate_teams_dataframe_weeks(
    df: pd.DataFrame,
    players: list[dict] | None = None,
) -> pd.DataFrame:
    """Attach week labels to team games (calendar dates when possible)."""
    lookup = _opponent_dates_by_school(players or [])
    allow_sequential = not bool(lookup)
    out = df.copy()
    annotated_games: list[object] = []
    for _, row in out.iterrows():
        games = row.get("Games")
        if not isinstance(games, list):
            annotated_games.append(games)
            continue
        slug = str(row.get("School_Slug") or "").strip()
        annotated_games.append(
            _annotate_game_list(
                games,
                opp_dates=lookup.get(slug),
                allow_sequential=allow_sequential,
            )
        )

    def _has_calendar(games_lists: list[object]) -> bool:
        for games in games_lists:
            if not isinstance(games, list):
                continue
            for game in games:
                key = str(game.get("Week_Key") or "") if isinstance(game, dict) else ""
                if key and not key.startswith("SEQ-"):
                    return True
        return False

    if _has_calendar(annotated_games):
        cleaned: list[object] = []
        for games in annotated_games:
            if not isinstance(games, list):
                cleaned.append(games)
                continue
            cleaned_games = []
            for game in games:
                if not isinstance(game, dict):
                    continue
                g = dict(game)
                if str(g.get("Week_Key") or "").startswith("SEQ-"):
                    g.pop("Week_Key", None)
                    g.pop("Week", None)
                cleaned_games.append(g)
            cleaned.append(cleaned_games)
        annotated_games = cleaned
    elif not allow_sequential:
        # Player dates existed but nothing matched; fall back to game-week order.
        rebuilt: list[object] = []
        for _, row in out.iterrows():
            games = row.get("Games")
            if not isinstance(games, list):
                rebuilt.append(games)
                continue
            rebuilt.append(_annotate_game_list(games, allow_sequential=True))
        annotated_games = rebuilt

    out["Games"] = annotated_games
    return out


def _week_options_from_games(games_iter) -> tuple[list[str], dict[str, str | None]]:
    """Return (select labels, label -> Week_Key). Full season is first label."""
    key_to_label: dict[str, str] = {}
    for games in games_iter:
        if not isinstance(games, list):
            continue
        for game in games:
            if not isinstance(game, dict):
                continue
            key = game.get("Week_Key")
            label = game.get("Week")
            if not key or not label:
                continue
            key_to_label[str(key)] = str(label)
    # Prefer calendar weeks when both styles somehow coexist.
    calendar_keys = [k for k in key_to_label if not k.startswith("SEQ-")]
    keys = calendar_keys if calendar_keys else list(key_to_label.keys())
    keys = sorted(keys, key=_week_key_sort_tuple)
    labels = [FULL_SEASON] + [key_to_label[k] for k in keys]
    label_to_key: dict[str, str | None] = {
        FULL_SEASON: None,
        **{key_to_label[k]: k for k in keys},
    }
    return labels, label_to_key


def _week_options_from_teams_df(df: pd.DataFrame) -> tuple[list[str], dict[str, str | None]]:
    if df is None or df.empty or "Games" not in df.columns:
        return [FULL_SEASON], {FULL_SEASON: None}
    return _week_options_from_games(df["Games"].tolist())


def _week_options_from_players(players: list[dict]) -> tuple[list[str], dict[str, str | None]]:
    return _week_options_from_games(
        (p.get("Games") if isinstance(p, dict) else None) for p in players
    )


def _filter_games_by_week(games: list, week_key: str | None) -> list[dict]:
    if not week_key:
        return [dict(g) for g in games if isinstance(g, dict)]
    return [
        dict(g)
        for g in games
        if isinstance(g, dict) and g.get("Week_Key") == week_key
    ]


def _finalize_team_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Recompute NJ-only stats, pace, Net rank, and conference strength."""
    if df is None or df.empty:
        return df
    out = df.copy()
    keep_idx: list = []
    for idx, row in out.iterrows():
        games = row.get("Games")
        if not isinstance(games, list) or not games:
            continue
        record = _nj_record_from_games(games)
        if record is None:
            continue
        for key, val in record.items():
            out.at[idx, key] = val
        keep_idx.append(idx)
    if not keep_idx:
        return out.iloc[0:0].copy()
    out = out.loc[keep_idx].copy()
    out = _recompute_sos_on_dataframe(out)
    out = _add_pace(out)
    out = _rank_by_net(out)
    out = _add_conference_strength(out)
    return out


def _teams_dataframe_for_week(df: pd.DataFrame, week_key: str | None) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if not week_key:
        return df
    rows: list[dict] = []
    for _, row in df.iterrows():
        games = row.get("Games")
        if not isinstance(games, list):
            continue
        filtered = _filter_games_by_week(games, week_key)
        if not filtered:
            continue
        new_row = row.to_dict()
        new_row["Games"] = filtered
        rows.append(new_row)
    if not rows:
        return df.iloc[0:0].copy()
    return _finalize_team_frame(pd.DataFrame(rows))


def _aggregate_player_games(games: list[dict]) -> dict[str, int] | None:
    completed = [
        g
        for g in games
        if isinstance(g, dict)
        and (
            g.get("GP")
            or g.get("PTS") is not None
            or g.get("REB") is not None
            or g.get("AST") is not None
        )
    ]
    if not completed:
        return None
    gp = 0
    pts = reb = ast = 0
    for g in completed:
        gp_val = pd.to_numeric(g.get("GP"), errors="coerce")
        gp += int(gp_val) if pd.notna(gp_val) and float(gp_val) > 0 else 1
        for key, bucket in (("PTS", "pts"), ("REB", "reb"), ("AST", "ast")):
            val = pd.to_numeric(g.get(key), errors="coerce")
            add = int(val) if pd.notna(val) else 0
            if bucket == "pts":
                pts += add
            elif bucket == "reb":
                reb += add
            else:
                ast += add
    return {"GP": gp, "PTS": pts, "REB": reb, "AST": ast}


def load_raw_players(cache_path: Path | None) -> tuple[list[dict], str | None]:
    if cache_path is None or not cache_path.is_file():
        return [], None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], None
    players = payload.get("players") or []
    if not isinstance(players, list):
        return [], payload.get("last_updated")
    return [p for p in players if isinstance(p, dict)], payload.get("last_updated")


def _week_selectbox(
    *,
    labels: list[str],
    label_to_key: dict[str, str | None],
    widget_key: str,
) -> str | None:
    selected_label = st.selectbox(
        "Week",
        options=labels,
        index=0,
        key=widget_key,
        help="Filter leaderboard stats to a single week, or keep the full season.",
    )
    return label_to_key.get(selected_label)


def _season_key(season: str) -> str:
    return season.replace("-", "_")


def team_cache_path_for_season(sport: SportPageConfig, season: str) -> Path:
    """Resolve team rankings cache for a season (legacy unversioned file for current)."""
    if sport.key == "basketball":
        versioned = SCRIPT_DIR / f"data_cache_{season}.json"
        legacy = DATA_CACHE_JSON
    elif sport.key == "football":
        versioned = SCRIPT_DIR / f"football_data_cache_{season}.json"
        legacy = FOOTBALL_DATA_CACHE_JSON
    else:
        return sport.cache_path

    if versioned.is_file():
        return versioned
    if season == DEFAULT_UI_SEASON and legacy.is_file():
        return legacy
    return versioned


def player_cache_path_for_season(sport: SportPageConfig, season: str) -> Path | None:
    """Resolve player stats cache for a season; None if sport has no players."""
    if sport.player_cache_path is None:
        return None
    if sport.key == "football":
        versioned = SCRIPT_DIR / f"football_player_data_cache_{season}.json"
        legacy = FOOTBALL_PLAYER_DATA_CACHE_JSON
    else:
        versioned = SCRIPT_DIR / f"player_data_cache_{season}.json"
        legacy = PLAYER_DATA_CACHE_JSON
    if versioned.is_file():
        return versioned
    if season == DEFAULT_UI_SEASON and legacy.is_file():
        return legacy
    return versioned


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
    "Player": ("Player", "Athlete name from NJ.com roster / player page."),
    "Class": ("Class", "Grade/class year from the roster."),
    "Positions": ("Pos", "Listed position(s) from the roster."),
    "PTS": ("PTS", "Season points total from NJ.com game logs."),
    "REB": ("REB", "Season rebounds total from NJ.com game logs."),
    "AST": ("AST", "Season assists total from NJ.com game logs."),
    "PPG": ("PPG", "Points per game: PTS ÷ GP."),
    "RPG": ("RPG", "Rebounds per game: REB ÷ GP."),
    "APG": ("APG", "Assists per game: AST ÷ GP."),
    "Number": ("#", "Jersey number from the roster."),
    "Stat_Category": ("Category", "Stat table from NJ.com (passing, rushing, receiving, defense)."),
    "Position_Group": ("Pos Group", "Primary position group derived from roster positions."),
    "COMP": ("COMP", "Pass completions."),
    "ATT": ("ATT", "Pass or rush attempts."),
    "YDS": ("YDS", "Yards gained."),
    "TD": ("TD", "Touchdowns."),
    "INT": ("INT", "Interceptions thrown (passing) or caught (defense)."),
    "LNG": ("LNG", "Longest play."),
    "REC": ("REC", "Receptions."),
    "S": ("S", "Sacks."),
    "TFL": ("TFL", "Tackles for loss."),
    "T_SOLO": ("T/SOLO", "Solo tackles."),
    "T_AST": ("T/AST", "Assisted tackles."),
    "T_TOT": ("T/TOT", "Total tackles."),
    "FF": ("FF", "Forced fumbles."),
    "FR": ("FR", "Fumble recoveries."),
    "Specialty": ("Specialty", "Kicking or punting stat line from NJ.com."),
    "FGM": ("FGM", "Field goals made."),
    "FGA": ("FGA", "Field goals attempted."),
    "FG_LNG": ("FG LNG", "Longest field goal made (yards)."),
    "XPM": ("XPM", "Extra points made."),
    "XPA": ("XPA", "Extra points attempted."),
    "2PT": ("2PT", "Two-point conversions made."),
    "PUNTS": ("Punts", "Punts."),
    "IN_20": ("IN 20", "Punts downed inside the 20."),
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
        padding-top: 3.75rem;
        padding-bottom: 3rem;
    }

    header[data-testid="stHeader"] {
        background: rgba(9, 9, 11, 0.85);
        backdrop-filter: blur(8px);
        border-bottom: 1px solid var(--nj-border);
    }

    header[data-testid="stHeader"] a {
        color: var(--nj-text-muted) !important;
        font-weight: 500;
        text-decoration: none !important;
    }

    header[data-testid="stHeader"] a[aria-current="page"] {
        color: var(--nj-text) !important;
        font-weight: 600;
    }

    [data-testid="stSidebar"],
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"],
    section[data-testid="stSidebar"] {
        display: none !important;
        width: 0 !important;
        min-width: 0 !important;
        transform: translateX(-100%) !important;
    }

    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] .stCaption,
    [data-testid="stSidebar"] h1,
    [data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3 {
        display: none !important;
    }

    [data-testid="stSidebar"],
    [data-testid="stSidebarCollapsedControl"],
    button[kind="headerNoPadding"] {
        display: none !important;
    }

    section[data-testid="stSidebar"] {
        display: none !important;
        width: 0 !important;
        min-width: 0 !important;
    }

    #MainMenu, footer {
        visibility: hidden;
    }

    [data-testid="stHeader"] [data-testid="stDecoration"] {
        background: var(--nj-border);
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

    [data-testid="stVerticalBlockBorderWrapper"]:has(.nj-sport-card-wrap) {
        background: var(--nj-surface) !important;
        border-color: var(--nj-border) !important;
        border-radius: var(--nj-radius) !important;
        overflow: hidden;
        padding: 0 !important;
        transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }

    [data-testid="stVerticalBlockBorderWrapper"]:has(.nj-sport-card-wrap):hover {
        border-color: var(--nj-border-subtle) !important;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
    }

    [data-testid="stVerticalBlockBorderWrapper"]:has(.nj-sport-card-wrap) [data-testid="stImage"] {
        margin: 0;
    }

    [data-testid="stVerticalBlockBorderWrapper"]:has(.nj-sport-card-wrap) [data-testid="stImage"] img {
        display: block;
        width: 100%;
        max-height: 240px;
        object-fit: cover;
        object-position: center 35%;
        border-bottom: 1px solid var(--nj-border);
    }

    .nj-sport-card-body {
        padding: 1.5rem 1.5rem 0;
    }

    [data-testid="stVerticalBlockBorderWrapper"]:has(.nj-sport-card-wrap) .stButton {
        padding: 0.75rem 1.5rem 1.5rem;
    }

    .nj-sport-card-body h3 {
        margin: 0 0 0.5rem;
        font-size: 1.25rem;
        font-weight: 600;
        color: var(--nj-text) !important;
    }

    .nj-page-header {
        margin-bottom: 1.75rem;
        padding-bottom: 1.5rem;
        border-bottom: 1px solid var(--nj-border);
    }

    .nj-sport-hero {
        display: flex;
        align-items: stretch;
        gap: 1.25rem;
        margin-top: 0.75rem;
        margin-bottom: 1.75rem;
        padding-bottom: 1.5rem;
        border-bottom: 1px solid var(--nj-border);
    }

    .nj-sport-hero .nj-eyebrow {
        margin-top: 0.35rem;
    }

    .nj-sport-hero .nj-page-header {
        flex: 1;
        min-width: 0;
        margin-bottom: 0;
        padding-bottom: 0;
        border-bottom: none;
    }

    .nj-sport-hero-logo-wrap {
        flex: 0 0 auto;
        display: flex;
        align-items: stretch;
        max-width: 12rem;
    }

    .nj-sport-hero-logo {
        height: 100%;
        width: auto;
        max-width: 100%;
        object-fit: contain;
        object-position: left center;
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

    .nj-last-updated {
        margin: -0.75rem 0 1.25rem;
        font-size: 0.8125rem;
        color: var(--nj-text-faint) !important;
    }
</style>
"""

DARK_CSS = APP_CSS


def inject_app_styles() -> None:
    st.markdown(APP_CSS, unsafe_allow_html=True)


@lru_cache(maxsize=4)
def _logo_cropped_sides(path: str, side_crop: float = 0.2) -> Image.Image:
    """Crop `side_crop` fraction from left and right (keep center width)."""
    img = Image.open(path).convert("RGBA")
    w, h = img.size
    left = int(round(w * side_crop))
    right = int(round(w * (1.0 - side_crop)))
    if right <= left:
        return img
    return img.crop((left, 0, right, h))


def _logo_png_data_uri() -> str | None:
    if not LOGO_PATH.is_file():
        return None
    img = _logo_cropped_sides(str(LOGO_PATH), 0.2)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _render_wordmark(*, use_container_width: bool = True) -> None:
    path = LOGO_WORDMARK_PATH if LOGO_WORDMARK_PATH.is_file() else LOGO_PATH
    if path.is_file():
        st.image(str(path), use_container_width=use_container_width)


def _render_home_sport_card(sport: SportPageConfig) -> None:
    _, center, _ = st.columns([1, 1.4, 1])
    with center:
        with st.container(border=True):
            st.markdown('<div class="nj-sport-card-wrap" aria-hidden="true"></div>', unsafe_allow_html=True)
            if sport.home_image and sport.home_image.is_file():
                st.image(str(sport.home_image), use_container_width=True)
            st.markdown(
                f"""
                <div class="nj-sport-card-body">
                    <p class="nj-card-label">Season analytics</p>
                    <h3>{sport.label}</h3>
                    <p>{sport.description}</p>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button(
                f"Open {sport.label.lower()} rankings",
                type="primary",
                use_container_width=True,
                key=f"open_{sport.key}",
            ):
                st.switch_page(sport.page_path)


def render_home_page(sports: list[SportPageConfig]) -> None:
    _, logo_col, _ = st.columns([0.75, 2.5, 0.75])
    with logo_col:
        _render_wordmark()

    st.markdown(
        """
        <div class="nj-hero">
            <p class="nj-eyebrow">New Jersey High School Sports</p>
            <p class="nj-hero-sub">
                Rankings and analytics built from NJ.com standings and schedules.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    for sport in sports:
        _render_home_sport_card(sport)


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
        if col in ("Team", "Conference", "Player", "Class", "Positions", "Stat_Category", "Position_Group", "Specialty"):
            configs[col] = st.column_config.TextColumn(label, help=help_text)
        elif col == "Rank":
            configs[col] = st.column_config.NumberColumn(label, help=help_text, format="%d")
        elif col in (
            "PF", "PA", "GP", "PTS", "REB", "AST", "COMP", "ATT", "YDS", "TD", "INT", "LNG", "REC",
            "T_SOLO", "T_AST", "T_TOT", "FF", "FR", "FGM", "FGA", "FG_LNG", "XPM", "XPA", "2PT", "PUNTS", "IN_20",
        ):
            configs[col] = st.column_config.NumberColumn(label, help=help_text, format="%d")
        elif col in ("S", "TFL"):
            configs[col] = st.column_config.NumberColumn(label, help=help_text, format="%.1f")
        elif col in ("Win_Pct", "SOS", "Opp_Win_Pct", "Opp_Opp_Win_Pct", "Net", "Conf_Strength"):
            configs[col] = st.column_config.NumberColumn(label, help=help_text, format="%.4f")
        elif col in ("PPG", "RPG", "APG"):
            configs[col] = st.column_config.NumberColumn(label, help=help_text, format="%.1f")
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
    """Pace = average of PF/GP and PA/GP; also expose PF_PG and PA_PG for charts."""
    out = df.copy()
    if not {"PF", "PA", "GP"}.issubset(out.columns):
        return out
    gp = pd.to_numeric(out["GP"], errors="coerce")
    pf = pd.to_numeric(out["PF"], errors="coerce")
    pa = pd.to_numeric(out["PA"], errors="coerce")
    valid_gp = gp.gt(0)
    out["PF_PG"] = (pf / gp).where(valid_gp).round(1)
    out["PA_PG"] = (pa / gp).where(valid_gp).round(1)
    out["Pace"] = ((pf + pa) / (2 * gp)).where(valid_gp).round(1)
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


SCATTER_PLOTS: tuple[dict[str, str | float], ...] = (
    {
        "label": "Pace vs Net",
        "desc": "Each point is a team in the current view.",
        "x_col": "Pace",
        "y_col": "Net",
        "x_label": "Pace",
        "y_label": "Net",
        "x_pad": 5,
        "y_pad": 0.1,
        "x_format": ".1f",
        "y_format": ".4f",
        "key": "pace_net",
    },
    {
        "label": "SOS vs Win%",
        "desc": "Schedule strength vs winning percentage for teams in the current view.",
        "x_col": "SOS",
        "y_col": "Win_Pct",
        "x_label": "SOS",
        "y_label": "Win%",
        "x_pad": 0.05,
        "y_pad": 0.05,
        "x_format": ".4f",
        "y_format": ".4f",
        "key": "sos_win",
    },
    {
        "label": "Net vs PF/G",
        "desc": "Points scored per game vs Net rating for teams in the current view.",
        "x_col": "PF_PG",
        "y_col": "Net",
        "x_label": "PF/G",
        "y_label": "Net",
        "x_pad": 5,
        "y_pad": 0.1,
        "x_format": ".1f",
        "y_format": ".4f",
        "key": "net_pf",
    },
    {
        "label": "Net vs PA/G",
        "desc": "Points allowed per game vs Net rating for teams in the current view.",
        "x_col": "PA_PG",
        "y_col": "Net",
        "x_label": "PA/G",
        "y_label": "Net",
        "x_pad": 5,
        "y_pad": 0.1,
        "x_format": ".1f",
        "y_format": ".4f",
        "key": "net_pa",
    },
)


def _scatter_df(df: pd.DataFrame, x_col: str, y_col: str) -> pd.DataFrame | None:
    """One point per team with valid x/y values for a scatter chart."""
    if df.empty or not {x_col, y_col}.issubset(df.columns):
        return None
    cols = [x_col, y_col]
    if "Team" in df.columns:
        cols.append("Team")
    if "Conference" in df.columns:
        cols.append("Conference")
    chart = df[cols].copy()
    chart[x_col] = pd.to_numeric(chart[x_col], errors="coerce")
    chart[y_col] = pd.to_numeric(chart[y_col], errors="coerce")
    chart = chart.dropna(subset=[x_col, y_col]).reset_index(drop=True)
    if chart.empty:
        return None
    return chart


def _render_team_scatter(
    chart: pd.DataFrame,
    *,
    x_col: str,
    y_col: str,
    x_label: str,
    y_label: str,
    x_pad: float,
    y_pad: float,
    x_format: str,
    y_format: str,
    widget_key: str,
) -> None:
    selected_team: str | None = None
    if "Team" in chart.columns:
        team_names = sorted(chart["Team"].dropna().astype(str).unique().tolist())
        selected_team = st.selectbox(
            "Find a team",
            options=[""] + team_names,
            format_func=lambda t: "Search teams…" if t == "" else t,
            key=f"scatter_team_{widget_key}",
        )
        if not selected_team:
            selected_team = None

    x_field = f"{x_col}:Q"
    y_field = f"{y_col}:Q"
    tooltip_fields = [
        alt.Tooltip("Team:N", title="Team") if "Team" in chart.columns else None,
        alt.Tooltip(x_field, title=x_label, format=x_format),
        alt.Tooltip(y_field, title=y_label, format=y_format),
    ]
    if "Conference" in chart.columns:
        tooltip_fields.append(alt.Tooltip("Conference:N", title="Conference"))
    tooltip = [t for t in tooltip_fields if t is not None]

    x_min = float(chart[x_col].min())
    x_max = float(chart[x_col].max())
    y_min = float(chart[y_col].min())
    y_max = float(chart[y_col].max())
    x_domain = [x_min - x_pad, x_max + x_pad]
    y_domain = [y_min - y_pad, y_max + y_pad]

    x_enc = alt.X(
        x_field,
        title=x_label,
        scale=alt.Scale(domain=x_domain, nice=False, zero=False, clamp=True),
    )
    y_enc = alt.Y(
        y_field,
        title=y_label,
        scale=alt.Scale(domain=y_domain, nice=False, zero=False, clamp=True),
    )

    base_encode: dict = {"x": x_enc, "y": y_enc, "tooltip": tooltip}
    if "Conference" in chart.columns:
        base_encode["color"] = alt.Color(
            "Conference:N",
            title="Conference",
            legend=alt.Legend(orient="right"),
        )

    if selected_team and "Team" in chart.columns:
        others = chart[chart["Team"] != selected_team]
        highlight = chart[chart["Team"] == selected_team]
        background = (
            alt.Chart(others)
            .mark_circle(size=55, opacity=0.35, clip=True)
            .encode(**base_encode)
        )
        ring = (
            alt.Chart(highlight)
            .mark_circle(size=280, opacity=1, color="#fafafa", clip=True)
            .encode(x=x_enc, y=y_enc, tooltip=tooltip)
        )
        point = (
            alt.Chart(highlight)
            .mark_circle(size=140, opacity=1, color="#2563eb", clip=True)
            .encode(x=x_enc, y=y_enc, tooltip=tooltip)
        )
        label = (
            alt.Chart(highlight)
            .mark_text(
                align="left",
                dx=10,
                dy=-10,
                fontSize=13,
                fontWeight="bold",
                color="#fafafa",
            )
            .encode(x=x_enc, y=y_enc, text="Team:N")
        )
        plot = (background + ring + point + label).properties(height=420)
    else:
        plot = (
            alt.Chart(chart)
            .mark_circle(size=60, opacity=0.85, clip=True)
            .encode(**base_encode)
            .properties(height=420)
        )

    # theme=None keeps Altair axis domains; Streamlit's theme can force axes to start at 0.
    st.altair_chart(plot, use_container_width=True, theme=None)


def _render_scatter_section(view: pd.DataFrame, sport_key: str) -> None:
    available: list[tuple[dict[str, str | float], pd.DataFrame]] = []
    for cfg in SCATTER_PLOTS:
        chart = _scatter_df(view, str(cfg["x_col"]), str(cfg["y_col"]))
        if chart is not None:
            available.append((cfg, chart))
    if not available:
        return

    st.divider()
    st.markdown(
        '<p class="nj-section-title">Team scatter</p>',
        unsafe_allow_html=True,
    )

    labels = [str(cfg["label"]) for cfg, _ in available]
    selected_label = st.selectbox(
        "Chart",
        options=labels,
        key=f"scatter_plot_{sport_key}",
    )
    cfg, chart = next(item for item in available if item[0]["label"] == selected_label)

    st.markdown(
        f'<p class="nj-section-desc">{cfg["desc"]}</p>',
        unsafe_allow_html=True,
    )
    _render_team_scatter(
        chart,
        x_col=str(cfg["x_col"]),
        y_col=str(cfg["y_col"]),
        x_label=str(cfg["x_label"]),
        y_label=str(cfg["y_label"]),
        x_pad=float(cfg["x_pad"]),
        y_pad=float(cfg["y_pad"]),
        x_format=str(cfg["x_format"]),
        y_format=str(cfg["y_format"]),
        widget_key=f"{sport_key}_{cfg['key']}",
    )


def _format_timestamp(ts: str | None) -> str:
    if not ts:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return ts


def load_cached_data(cache_path: Path = DATA_CACHE_JSON) -> tuple[pd.DataFrame | None, str | None]:
    if not cache_path.is_file():
        return None, None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
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


def _players_frame_from_records(
    players: list[dict],
    teams_df: pd.DataFrame | None = None,
    *,
    week_key: str | None = None,
) -> pd.DataFrame | None:
    """Build a ranked player leaderboard from raw player records."""
    if not players:
        return None

    rows: list[dict] = []
    for p in players:
        if not isinstance(p, dict):
            continue
        if week_key:
            games = _filter_games_by_week(
                p.get("Games") if isinstance(p.get("Games"), list) else [],
                week_key,
            )
            totals = _aggregate_player_games(games)
            if totals is None:
                continue
            gp = float(totals["GP"])
            pts = float(totals["PTS"])
            reb = float(totals["REB"])
            ast = float(totals["AST"])
        else:
            totals = p.get("Season_Totals") or {}
            if not isinstance(totals, dict):
                totals = {}
            gp = pd.to_numeric(totals.get("GP"), errors="coerce")
            pts = pd.to_numeric(totals.get("PTS"), errors="coerce")
            reb = pd.to_numeric(totals.get("REB"), errors="coerce")
            ast = pd.to_numeric(totals.get("AST"), errors="coerce")
            gp = float(gp) if pd.notna(gp) else None
            pts = float(pts) if pd.notna(pts) else None
            reb = float(reb) if pd.notna(reb) else None
            ast = float(ast) if pd.notna(ast) else None

        row = {
            "Player": p.get("Name"),
            "Team": p.get("Team"),
            "School_Slug": p.get("School_Slug"),
            "Class": p.get("Class"),
            "Positions": p.get("Positions"),
            "GP": int(gp) if gp is not None else None,
            "PTS": int(pts) if pts is not None else None,
            "REB": int(reb) if reb is not None else None,
            "AST": int(ast) if ast is not None else None,
        }
        if gp is not None and gp > 0:
            row["PPG"] = round(float(pts) / float(gp), 1) if pts is not None else None
            row["RPG"] = round(float(reb) / float(gp), 1) if reb is not None else None
            row["APG"] = round(float(ast) / float(gp), 1) if ast is not None else None
        else:
            row["PPG"] = None
            row["RPG"] = None
            row["APG"] = None
        rows.append(row)

    if not rows:
        return None

    pdf = pd.DataFrame(rows)
    if (
        teams_df is not None
        and not teams_df.empty
        and "School_Slug" in teams_df.columns
        and "Conference" in teams_df.columns
    ):
        conf_map = (
            teams_df[["School_Slug", "Conference"]]
            .dropna(subset=["School_Slug"])
            .drop_duplicates(subset=["School_Slug"])
        )
        pdf = pdf.merge(conf_map, on="School_Slug", how="left")

    pdf = pdf.sort_values(
        by=["PTS", "REB", "AST", "Player"],
        ascending=[False, False, False, True],
        na_position="last",
    ).reset_index(drop=True)
    pdf.insert(0, "Rank", range(1, len(pdf) + 1))
    return pdf


def load_cached_players(
    cache_path: Path | None,
    teams_df: pd.DataFrame | None = None,
    *,
    week_key: str | None = None,
) -> tuple[pd.DataFrame | None, str | None]:
    """Flatten player_data_cache.json into a season or weekly leaderboard frame."""
    players, last_updated = load_raw_players(cache_path)
    if not players:
        return None, last_updated
    if week_key:
        players = _annotate_player_games(players)
    pdf = _players_frame_from_records(players, teams_df, week_key=week_key)
    return pdf, last_updated


def _filter_players(pdf: pd.DataFrame, conference: str | None) -> pd.DataFrame:
    if not conference or conference == ALL_CONFERENCES or "Conference" not in pdf.columns:
        return pdf
    view = pdf[pdf["Conference"] == conference].copy()
    if view.empty:
        return view
    view = view.sort_values(
        by=["PTS", "REB", "AST", "Player"],
        ascending=[False, False, False, True],
        na_position="last",
    ).reset_index(drop=True)
    view["Rank"] = range(1, len(view) + 1)
    return view


def _football_position_groups(positions: str | None, fallback: str | None = None) -> set[str]:
    """Map roster position codes to all applicable UI position groups."""
    if not positions or not str(positions).strip():
        return {fallback or "Other"}
    codes = {c.strip().upper() for c in re.split(r"[,/]", str(positions)) if c.strip()}
    groups = {
        FOOTBALL_POSITION_CODE_TO_GROUP.get(code, "Other")
        for code in codes
    }
    if groups == {"Other"} and fallback:
        groups.add(fallback)
    return groups or {fallback or "Other"}


def _player_matches_football_position(
    positions: str | None,
    position_group: str,
    *,
    fallback_group: str | None = None,
) -> bool:
    if position_group == ALL_POSITIONS:
        return True
    return position_group in _football_position_groups(positions, fallback_group)


def _aggregate_football_player_games(
    games: list[dict],
    stat_cols: tuple[str, ...],
) -> dict[str, int | float | None] | None:
    if not games:
        return None
    totals: dict[str, float] = {col: 0.0 for col in stat_cols}
    has_value = {col: False for col in stat_cols}
    for game in games:
        if not isinstance(game, dict):
            continue
        for col in stat_cols:
            val = game.get(col)
            if isinstance(val, bool) or val is None:
                continue
            num = pd.to_numeric(val, errors="coerce")
            if pd.isna(num):
                continue
            totals[col] += float(num)
            has_value[col] = True
    if not any(has_value.values()):
        return None
    out: dict[str, int | float | None] = {}
    for col in stat_cols:
        if not has_value[col]:
            out[col] = None
        elif col in ("S", "TFL"):
            out[col] = totals[col]
        elif float(totals[col]).is_integer():
            out[col] = int(totals[col])
        else:
            out[col] = totals[col]
    return out


def _football_stat_cols_for_record(stat_category: str, player_category: str) -> tuple[str, ...]:
    if stat_category == "specialists":
        return FOOTBALL_STAT_COLUMNS.get(player_category, ())
    return FOOTBALL_STAT_COLUMNS.get(stat_category, ())


def _football_primary_sort_key(row: pd.Series) -> tuple[float, float, float, str]:
    category = str(row.get("Stat_Category") or "")
    if category == "passing":
        return (
            float(row.get("YDS") or 0),
            float(row.get("TD") or 0),
            float(row.get("COMP") or 0),
            str(row.get("Player") or ""),
        )
    if category == "rushing":
        return (
            float(row.get("YDS") or 0),
            float(row.get("TD") or 0),
            float(row.get("ATT") or 0),
            str(row.get("Player") or ""),
        )
    if category == "receiving":
        return (
            float(row.get("YDS") or 0),
            float(row.get("TD") or 0),
            float(row.get("REC") or 0),
            str(row.get("Player") or ""),
        )
    if category == "defense":
        return (
            float(row.get("T_TOT") or 0),
            float(row.get("INT") or 0),
            float(row.get("S") or 0),
            str(row.get("Player") or ""),
        )
    if category == "kicking":
        return (
            float(row.get("FGM") or 0),
            float(row.get("FGA") or 0),
            float(row.get("XPM") or 0),
            str(row.get("Player") or ""),
        )
    if category == "punting":
        return (
            float(row.get("PUNTS") or 0),
            float(row.get("YDS") or 0),
            float(row.get("LNG") or 0),
            str(row.get("Player") or ""),
        )
    if category == "kicking":
        return (
            float(row.get("FGM") or 0),
            float(row.get("FGA") or 0),
            float(row.get("XPM") or 0),
            str(row.get("Player") or ""),
        )
    return (0.0, 0.0, 0.0, str(row.get("Player") or ""))


def _football_players_frame_from_records(
    players: list[dict],
    teams_df: pd.DataFrame | None = None,
    *,
    stat_category: str,
    week_key: str | None = None,
) -> pd.DataFrame | None:
    if not players:
        return None

    if stat_category == "specialists":
        categories_to_include = FOOTBALL_SPECIALIST_STAT_CATEGORIES
        display_stat_cols = FOOTBALL_STAT_COLUMNS["specialists"]
    else:
        categories_to_include = {stat_category}
        display_stat_cols = FOOTBALL_STAT_COLUMNS.get(stat_category, ())

    rows: list[dict] = []
    for player in players:
        if not isinstance(player, dict):
            continue
        player_category = str(player.get("Stat_Category") or "")
        if player_category not in categories_to_include:
            continue

        record_stat_cols = _football_stat_cols_for_record(stat_category, player_category)
        if week_key:
            games = _filter_games_by_week(
                player.get("Games") if isinstance(player.get("Games"), list) else [],
                week_key,
            )
            stats = _aggregate_football_player_games(games, record_stat_cols)
            if stats is None:
                continue
        else:
            stats = player.get("Stats") if isinstance(player.get("Stats"), dict) else {}

        row: dict[str, object] = {
            "Player": player.get("Name"),
            "Number": player.get("Number"),
            "Team": player.get("Team"),
            "School_Slug": player.get("School_Slug"),
            "Class": player.get("Class"),
            "Positions": player.get("Positions"),
            "Position_Group": player.get("Position_Group"),
            "Stat_Category": player_category,
        }
        if stat_category == "specialists":
            row["Specialty"] = FOOTBALL_SPECIALTY_LABELS.get(
                player_category, player_category.title()
            )
        for col in display_stat_cols:
            row[col] = stats.get(col) if isinstance(stats, dict) else None
        rows.append(row)

    if not rows:
        return None

    pdf = pd.DataFrame(rows)
    if (
        teams_df is not None
        and not teams_df.empty
        and "School_Slug" in teams_df.columns
        and "Conference" in teams_df.columns
    ):
        conf_map = (
            teams_df[["School_Slug", "Conference"]]
            .dropna(subset=["School_Slug"])
            .drop_duplicates(subset=["School_Slug"])
        )
        pdf = pdf.merge(conf_map, on="School_Slug", how="left")

    sort_keys = pdf.apply(_football_primary_sort_key, axis=1, result_type="expand")
    pdf = pdf.assign(_s0=sort_keys[0], _s1=sort_keys[1], _s2=sort_keys[2], _s3=sort_keys[3])
    pdf = pdf.sort_values(
        by=["_s0", "_s1", "_s2", "_s3"],
        ascending=[False, False, False, True],
    ).drop(columns=["_s0", "_s1", "_s2", "_s3"]).reset_index(drop=True)
    pdf.insert(0, "Rank", range(1, len(pdf) + 1))
    return pdf


def _filter_football_players(
    pdf: pd.DataFrame,
    conference: str | None,
    position_group: str,
) -> pd.DataFrame:
    view = pdf
    if conference and conference != ALL_CONFERENCES and "Conference" in view.columns:
        view = view[view["Conference"] == conference].copy()
    if view.empty:
        return view
    if position_group != ALL_POSITIONS:
        mask = view.apply(
            lambda row: _player_matches_football_position(
                row.get("Positions"),
                position_group,
                fallback_group=str(row.get("Position_Group") or "Other"),
            ),
            axis=1,
        )
        view = view[mask].copy()
    if view.empty:
        return view

    sort_keys = view.apply(_football_primary_sort_key, axis=1, result_type="expand")
    view = view.assign(_s0=sort_keys[0], _s1=sort_keys[1], _s2=sort_keys[2], _s3=sort_keys[3])
    view = view.sort_values(
        by=["_s0", "_s1", "_s2", "_s3"],
        ascending=[False, False, False, True],
    ).drop(columns=["_s0", "_s1", "_s2", "_s3"]).reset_index(drop=True)
    view["Rank"] = range(1, len(view) + 1)
    return view


def _football_display_columns(stat_category: str) -> list[str]:
    base = ["Rank", "Player", "Number", "Team", "Conference", "Class", "Positions"]
    if stat_category == "specialists":
        return base + ["Specialty"] + list(FOOTBALL_STAT_COLUMNS["specialists"])
    stat_cols = list(FOOTBALL_STAT_COLUMNS.get(stat_category, ()))
    return base + stat_cols


def _render_football_players_section(
    sport: SportPageConfig,
    teams_df: pd.DataFrame,
    *,
    season: str,
    player_cache: Path | None,
    annotated_players: list[dict] | None = None,
) -> None:
    players = annotated_players
    last_updated = None
    if players is None:
        players, last_updated = load_raw_players(player_cache)
        players = _annotate_player_games(players) if players else []
    if not players:
        st.info(
            f"No player data found for {season}. Run the football player scraper with "
            f"`py -3 football_player_scraper.py --season {season}` to populate the cache."
        )
        return

    season_suffix = _season_key(season)
    if "Conference" in teams_df.columns:
        conferences = sorted(
            c for c in teams_df["Conference"].dropna().astype(str).unique() if c.strip()
        )
    else:
        conferences = []

    week_labels, week_label_to_key = _week_options_from_players(players)

    filter_col, position_col, stat_col, week_col, metric_col = st.columns([2, 2, 2, 2, 1])
    with filter_col:
        selected_conf = st.selectbox(
            "Conference",
            options=[ALL_CONFERENCES] + conferences,
            index=0,
            key=f"player_conference_{sport.key}_{season_suffix}",
        )
    with position_col:
        selected_pos = st.selectbox(
            "Position",
            options=list(FOOTBALL_POSITION_OPTIONS),
            index=0,
            key=f"player_position_{sport.key}_{season_suffix}",
            help="Players with multiple listed positions appear in each matching group.",
        )
    with stat_col:
        stat_labels = [label for label, _ in FOOTBALL_STAT_CATEGORY_OPTIONS]
        stat_label_to_key = {label: key for label, key in FOOTBALL_STAT_CATEGORY_OPTIONS}
        selected_stat_label = st.selectbox(
            "Stat category",
            options=stat_labels,
            index=0,
            key=f"player_stat_category_{sport.key}_{season_suffix}",
        )
        selected_stat = stat_label_to_key[selected_stat_label]
    with week_col:
        week_key = _week_selectbox(
            labels=week_labels,
            label_to_key=week_label_to_key,
            widget_key=f"player_week_{sport.key}_{season_suffix}",
        )

    pdf = _football_players_frame_from_records(
        players,
        teams_df,
        stat_category=selected_stat,
        week_key=week_key,
    )
    if pdf is None or pdf.empty:
        st.info(
            f"No {selected_stat_label.lower()} stats found for the selected week."
            if week_key
            else f"No {selected_stat_label.lower()} stats found for this season."
        )
        return

    view = _filter_football_players(pdf, selected_conf, selected_pos)
    with metric_col:
        st.metric("Players", len(view))

    pos_desc = (
        f"{selected_stat_label} leaders"
        + (f" — {selected_pos}" if selected_pos != ALL_POSITIONS else "")
        + (
            ", weekly totals from game logs in the selected week."
            if week_key
            else ", season totals from NJ.com."
        )
        + " Players with multiple positions appear in each matching group."
    )
    st.markdown(
        f'<p class="nj-section-title">Player leaderboard</p>'
        f'<p class="nj-section-desc">{pos_desc}</p>',
        unsafe_allow_html=True,
    )

    display_cols = [c for c in _football_display_columns(selected_stat) if c in view.columns]
    if view.empty and selected_conf != ALL_CONFERENCES:
        st.warning(f"No players found for conference: {selected_conf}")
    table = view[display_cols] if not view.empty else view
    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config=_leaderboard_column_config(display_cols),
    )
    _ = last_updated


def _render_players_section(
    sport: SportPageConfig,
    teams_df: pd.DataFrame,
    *,
    season: str,
    player_cache: Path | None,
    annotated_players: list[dict] | None = None,
) -> None:
    if sport.key == "football":
        _render_football_players_section(
            sport,
            teams_df,
            season=season,
            player_cache=player_cache,
            annotated_players=annotated_players,
        )
        return

    if player_cache is None and sport.player_cache_path is None:
        st.info(f"Player stats are not available for {sport.label} yet.")
        return

    players = annotated_players
    last_updated = None
    if players is None:
        raw_players, last_updated = load_raw_players(player_cache)
        players = _annotate_player_games(raw_players) if raw_players else []
    if not players:
        st.info(
            f"No player data found for {season}. Run the player scraper with "
            f"`py -3 player_scraper.py --season {season}` to populate the cache."
        )
        return

    season_suffix = _season_key(season)
    week_labels, week_label_to_key = _week_options_from_players(players)

    if "Conference" in teams_df.columns:
        # Conference list comes from teams; player frame may add it after build.
        conferences = sorted(
            c for c in teams_df["Conference"].dropna().astype(str).unique() if c.strip()
        )
    else:
        conferences = []

    filter_col, week_col, metric_col = st.columns([2, 2, 1])
    with filter_col:
        selected = st.selectbox(
            "Conference",
            options=[ALL_CONFERENCES] + conferences,
            index=0,
            key=f"player_conference_{sport.key}_{season_suffix}",
        )
    with week_col:
        week_key = _week_selectbox(
            labels=week_labels,
            label_to_key=week_label_to_key,
            widget_key=f"player_week_{sport.key}_{season_suffix}",
        )

    pdf = _players_frame_from_records(players, teams_df, week_key=week_key)
    if pdf is None or pdf.empty:
        st.info(
            "No player stats found for the selected week."
            if week_key
            else (
                f"No player data found for {season}. Run the player scraper with "
                f"`py -3 player_scraper.py --season {season}` to populate the cache."
            )
        )
        return

    view = _filter_players(pdf, selected)
    with metric_col:
        st.metric("Players", len(view))

    week_desc = (
        "Weekly totals from game logs in the selected week, sorted by points (PTS)."
        if week_key
        else "Season totals sorted by points (PTS)."
    )
    st.markdown(
        f'<p class="nj-section-title">Player leaderboard</p>'
        f'<p class="nj-section-desc">{week_desc}</p>',
        unsafe_allow_html=True,
    )

    display_cols = [
        c
        for c in [
            "Rank",
            "Player",
            "Team",
            "Conference",
            "Class",
            "Positions",
            "GP",
            "PTS",
            "REB",
            "AST",
            "PPG",
            "RPG",
            "APG",
        ]
        if c in view.columns
    ]
    if view.empty and selected != ALL_CONFERENCES:
        st.warning(f"No players found for conference: {selected}")
    table = view[display_cols] if not view.empty else view
    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config=_leaderboard_column_config(display_cols),
    )
    _ = last_updated


def _filter_and_rank(df: pd.DataFrame, conference: str | None) -> pd.DataFrame:
    if not conference or conference == ALL_CONFERENCES:
        return df
    view = df[df["Conference"] == conference].copy()
    if view.empty:
        return view
    return _rank_by_net(view)


def render_sport_page(sport: SportPageConfig) -> None:
    logo_uri = _logo_png_data_uri()
    logo_html = (
        f'<div class="nj-sport-hero-logo-wrap">'
        f'<img class="nj-sport-hero-logo" src="{logo_uri}" alt="NJ Stat Cast" />'
        f"</div>"
        if logo_uri
        else ""
    )
    st.markdown(
        f"""
        <div class="nj-sport-hero">
            {logo_html}
            <div class="nj-page-header">
                <p class="nj-eyebrow">{sport.label}</p>
                <h1>Statewide Rankings</h1>
                <p class="nj-page-sub">
                    Net rating leaderboard with in-state schedule adjustments and head-to-head tiebreakers.
                </p>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    default_idx = (
        AVAILABLE_SEASONS.index(DEFAULT_UI_SEASON)
        if DEFAULT_UI_SEASON in AVAILABLE_SEASONS
        else len(AVAILABLE_SEASONS) - 2
    )
    season = st.selectbox(
        "Season",
        options=list(AVAILABLE_SEASONS),
        index=default_idx,
        key=f"season_{sport.key}",
        help="Choose a season. Scraped data loads when a cache file exists for that year.",
    )
    season_suffix = _season_key(season)
    teams_cache = team_cache_path_for_season(sport, season)
    players_cache = player_cache_path_for_season(sport, season)

    df, last_updated = load_cached_data(teams_cache)
    st.markdown(
        f'<p class="nj-last-updated">Last updated {_format_timestamp(last_updated)}</p>',
        unsafe_allow_html=True,
    )

    with st.expander("How rankings are calculated"):
        st.markdown(
            "Rankings use **Net = 0.5×norm(SOS) + 0.3×norm(Win%) + 0.2×norm(Avg Margin)**, "
            "with each stat min–max scaled to 0–1 within the current view (statewide or conference). "
            "Win%, margin, and SOS use in-state opponents only; out-of-state games are excluded when "
            "schedule data exists. Adjacent teams may swap when the lower-Net team won the head-to-head series. "
            "Use the **Week** dropdown on Teams or Players to isolate stats to a single week "
            "(calendar week when dates are available; otherwise game-week order)."
        )

    if df is None:
        st.info(
            f"No {sport.label.lower()} data found for {season}. "
            f"Run `py -3 scraper.py --sport {sport.key} --season {season}` "
            "to populate the cache."
        )
        return

    raw_players, _player_updated = load_raw_players(players_cache)
    if sport.key == "basketball":
        annotated_players = _annotate_player_games(raw_players) if raw_players else []
        df = _annotate_teams_dataframe_weeks(df, annotated_players)
    else:
        annotated_players = _annotate_player_games(raw_players) if raw_players else []
        df = _annotate_teams_dataframe_weeks(df, None)
    team_week_labels, team_week_label_to_key = _week_options_from_teams_df(df)

    teams_tab, players_tab = st.tabs(["Teams", "Players"])

    with teams_tab:
        if "Conference" in df.columns:
            conferences = sorted(
                c for c in df["Conference"].dropna().astype(str).unique() if c.strip()
            )
        else:
            conferences = []

        filter_col, week_col, metric_col = st.columns([2, 2, 1])
        with filter_col:
            selected = st.selectbox(
                "Conference",
                options=[ALL_CONFERENCES] + conferences,
                index=0,
                key=f"conference_{sport.key}_{season_suffix}",
            )
        with week_col:
            week_key = _week_selectbox(
                labels=team_week_labels,
                label_to_key=team_week_label_to_key,
                widget_key=f"team_week_{sport.key}_{season_suffix}",
            )

        week_df = _teams_dataframe_for_week(df, week_key)
        with metric_col:
            view = _filter_and_rank(week_df, selected)
            st.metric("Teams", len(view))

        leaderboard_desc = (
            "Sorted by Net within the selected week and conference view."
            if week_key
            else "Sorted by Net within the selected view."
        )
        st.markdown(
            f'<p class="nj-section-title">Leaderboard</p>'
            f'<p class="nj-section-desc">{leaderboard_desc}</p>',
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
        elif view.empty and week_key:
            st.warning("No teams found for the selected week.")
        table = view[display_cols] if not view.empty else view
        st.dataframe(
            table,
            use_container_width=True,
            hide_index=True,
            column_config=_leaderboard_column_config(display_cols),
        )

        if not view.empty:
            week_suffix = week_key or "full"
            _render_scatter_section(view, f"{sport.key}_{season_suffix}_{week_suffix}")

        conf_chart = _conference_strength_chart_df(week_df if week_key else df)
        if conf_chart is not None:
            st.divider()
            st.markdown(
                '<p class="nj-section-title">Conference strength</p>'
                '<p class="nj-section-desc">Average statewide win% by conference.</p>',
                unsafe_allow_html=True,
            )
            _render_conference_strength_chart(conf_chart)

    with players_tab:
        _render_players_section(
            sport,
            df,
            season=season,
            player_cache=players_cache,
            annotated_players=annotated_players,
        )


def render_basketball_page() -> None:
    render_sport_page(BASKETBALL_CONFIG)


def render_football_page() -> None:
    render_sport_page(FOOTBALL_CONFIG)
