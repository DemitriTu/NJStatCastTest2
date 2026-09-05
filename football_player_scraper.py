"""
Scrape NJ.com football team stats pages (passing, rushing, receiving, defense).

Reads School_Slug list from the football team standings cache, then scrapes each
team's season stats page and roster for position metadata.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from player_scraper import fetch_html_playwright, fetch_html_with_fallback
from scraper import DEFAULT_SEASON, SCRIPT_DIR, SITE_ORIGIN, _strip_html_tags, load_teams_from_cache

SPORT_KEY = "football"
PATH_SEGMENT = "football"
DEFAULT_TEAMS_CACHE = SCRIPT_DIR / "football_data_cache.json"

STAT_SECTIONS: tuple[tuple[str, str], ...] = (
    ("passing", "Passing"),
    ("rushing", "Rushing"),
    ("receiving", "Receiving"),
    ("defense", "Defense"),
    ("kicking", "Special Teams Scoring"),
    ("punting", "Punting"),
)

HEADER_ALIASES: dict[str, str] = {
    "TDs": "TD",
    "TDS": "TD",
    "INTs": "INT",
    "T/SOLO": "T_SOLO",
    "T/AST": "T_AST",
    "T/TOT": "T_TOT",
    "FUM/TD": "FUM_TD",
    "INT/TD": "INT_TD",
    "FG/LNG": "FG_LNG",
    "IN 20": "IN_20",
    "Punts": "PUNTS",
}

POSITION_GROUPS: tuple[tuple[str, frozenset[str]], ...] = (
    ("QB", frozenset({"QB"})),
    ("RB", frozenset({"RB", "FB"})),
    ("WR / TE", frozenset({"WR", "TE"})),
    ("OL", frozenset({"OL", "OT", "OG", "C"})),
    ("DL", frozenset({"DL", "DE", "DT", "NG"})),
    ("LB", frozenset({"LB"})),
    ("DB", frozenset({"DB", "CB", "S", "FS", "SS"})),
    ("Specialists", frozenset({"K", "P", "LS"})),
)

PLAYER_CELL_RE = re.compile(
    r"^(?P<name>.+?)\s*#\s*(?P<number>\d+)\s*(?:&bull;|•)\s*"
    r"(?P<class>[^•]+?)(?:\s*(?:&bull;|•)\s*(?P<positions>.*))?$",
    re.I,
)
PLAYER_HREF_RE = re.compile(
    rf'href="(/player/([^/"]+)(?:/{re.escape(PATH_SEGMENT)}(?:/season/[^"/]+)?)?)"',
    re.I,
)


def stats_url(season_id: str, school_slug: str) -> str:
    return (
        f"{SITE_ORIGIN}/school/{school_slug}/{PATH_SEGMENT}/"
        f"season/{season_id}/stats"
    )


def roster_url(season_id: str, school_slug: str) -> str:
    return (
        f"{SITE_ORIGIN}/school/{school_slug}/{PATH_SEGMENT}/"
        f"season/{season_id}/roster"
    )


def player_season_url(player_slug: str, season_id: str) -> str:
    return f"{SITE_ORIGIN}/player/{player_slug}/{PATH_SEGMENT}/season/{season_id}"


def _cell_texts(tr_html: str) -> list[str]:
    return [
        _strip_html_tags(td)
        for td in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr_html, re.S | re.I)
    ]


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _empty_to_none(s: str | None) -> str | None:
    if s is None:
        return None
    text = str(s).strip()
    return text or None


def _parse_stat_value(raw: str | None) -> int | float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace("\xa0", " ")
    if not text or text in ("-", "—", "&mdash;"):
        return None
    text = text.replace(",", "")
    if "." in text:
        try:
            return float(text)
        except ValueError:
            return None
    try:
        return int(text)
    except ValueError:
        return None


def _normalize_header(header: str) -> str | None:
    text = header.strip().replace("\xa0", "").strip()
    if not text or text == "&nbsp;":
        return None
    return HEADER_ALIASES.get(text, text)


def _parse_opponent_field(raw: str) -> tuple[str, bool | None]:
    text = (raw or "").strip()
    home: bool | None = None
    if text.lower().startswith("vs."):
        home = True
        text = text[3:].strip()
    elif text.startswith("@"):
        home = False
        text = text[1:].strip()
    return text, home


def _category_from_game_log_headers(stat_keys: list[str]) -> str | None:
    key_set = set(stat_keys)
    if "COMP" in key_set:
        return "passing"
    if "REC" in key_set:
        return "receiving"
    if "FGM" in key_set:
        return "kicking"
    if "PUNTS" in key_set:
        return "punting"
    if "T_TOT" in key_set or "TFL" in key_set:
        return "defense"
    if "ATT" in key_set and "YDS" in key_set:
        return "rushing"
    return None


def parse_player_page_game_logs(html: str) -> dict[str, list[dict]]:
    """Parse per-game stat tables from a player season page."""
    games_by_category: dict[str, list[dict]] = {}
    seen_tables: set[tuple[str, ...]] = set()

    for table_html in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S | re.I):
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S | re.I)
        if not rows:
            continue
        header_cells = _cell_texts(rows[0])
        if not header_cells or header_cells[0].strip().lower() != "date":
            continue

        stat_keys: list[str] = []
        for cell in header_cells[3:]:
            key = _normalize_header(cell)
            if key:
                stat_keys.append(key)
        header_key = tuple(stat_keys)
        if not stat_keys or header_key in seen_tables:
            continue
        seen_tables.add(header_key)

        category = _category_from_game_log_headers(stat_keys)
        if not category:
            continue

        games: list[dict] = []
        for tr_html in rows[1:]:
            cells = _cell_texts(tr_html)
            if len(cells) < 3:
                continue
            date = _empty_to_none(cells[0])
            if not date or date.lower() == "date" or "season total" in date.lower():
                continue
            opponent, home = _parse_opponent_field(cells[1])
            result = _empty_to_none(cells[2])
            stats: dict[str, int | float | None] = {}
            for idx, key in enumerate(stat_keys):
                value_idx = idx + 3
                stats[key] = (
                    _parse_stat_value(cells[value_idx]) if value_idx < len(cells) else None
                )
            if not opponent:
                continue
            games.append(
                {
                    "Date": date,
                    "Opponent": opponent,
                    "Home": home,
                    "Result": result,
                    **stats,
                }
            )
        if games:
            games_by_category.setdefault(category, []).extend(games)
    return games_by_category


def scrape_player_game_logs(
    player_slug: str,
    *,
    season: str,
    timeout_ms: int,
) -> dict[str, list[dict]]:
    url = player_season_url(player_slug, season)
    try:
        html = fetch_html_with_fallback(url, timeout_ms)
    except Exception as e:
        print(
            f"football_player_scraper: game log failed {player_slug}: {e}",
            file=sys.stderr,
        )
        return {}
    return parse_player_page_game_logs(html)


def derive_position_group(positions: str | None) -> str:
    if not positions:
        return "Other"
    codes = {c.strip().upper() for c in re.split(r"[,/]", positions) if c.strip()}
    for group, match_codes in POSITION_GROUPS:
        if codes & match_codes:
            return group
    return "Other"


def parse_player_cell(cell: str) -> dict[str, str | None]:
    text = _strip_html_tags(cell).replace("\xa0", " ").strip()
    match = PLAYER_CELL_RE.match(text)
    if not match:
        return {"Name": text or None, "Number": None, "Class": None, "Positions": None}
    return {
        "Name": _empty_to_none(match.group("name")),
        "Number": _empty_to_none(match.group("number")),
        "Class": _empty_to_none(match.group("class")),
        "Positions": _empty_to_none(match.group("positions")),
    }


def parse_roster_html(html: str, *, school_slug: str, team_name: str | None) -> list[dict]:
    athletes: list[dict] = []
    for tr_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = _cell_texts(tr_html)
        if len(cells) < 2:
            continue
        number = _empty_to_none(cells[0])
        if number and number.lower() in ("#", "no.", "number"):
            continue
        name = _empty_to_none(cells[1])
        if not name or name.lower() == "name":
            continue
        link_m = PLAYER_HREF_RE.search(tr_html)
        player_slug = link_m.group(2) if link_m else None
        positions = _empty_to_none(cells[2]) if len(cells) > 2 else None
        class_year = _empty_to_none(cells[3]) if len(cells) > 3 else None
        athletes.append(
            {
                "Player_Slug": player_slug,
                "Name": name,
                "Number": number,
                "Positions": positions,
                "Class": class_year,
                "Team": team_name,
                "School_Slug": school_slug,
            }
        )
    return athletes


def _roster_lookup(roster: list[dict]) -> dict[tuple[str, str], dict]:
    lookup: dict[tuple[str, str], dict] = {}
    for athlete in roster:
        name = athlete.get("Name")
        if not name:
            continue
        number = str(athlete.get("Number") or "").strip()
        lookup[(_normalize_name(str(name)), number)] = athlete
        if number:
            lookup[(_normalize_name(str(name)), "")] = athlete
    return lookup


def _merge_roster_fields(player: dict, roster_by_key: dict[tuple[str, str], dict]) -> None:
    name = player.get("Name")
    if not name:
        return
    number = str(player.get("Number") or "").strip()
    roster_row = roster_by_key.get((_normalize_name(str(name)), number))
    if roster_row is None and number:
        roster_row = roster_by_key.get((_normalize_name(str(name)), ""))
    if roster_row is None:
        return
    if not player.get("Positions") and roster_row.get("Positions"):
        player["Positions"] = roster_row["Positions"]
    if not player.get("Class") and roster_row.get("Class"):
        player["Class"] = roster_row["Class"]
    if not player.get("Player_Slug") and roster_row.get("Player_Slug"):
        player["Player_Slug"] = roster_row["Player_Slug"]


def _parse_category_table(table_html: str, category: str) -> list[dict]:
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S | re.I)
    if not rows:
        return []

    header_cells = _cell_texts(rows[0])
    stat_keys: list[str] = []
    for cell in header_cells[1:]:
        key = _normalize_header(cell)
        if key:
            stat_keys.append(key)

    players: list[dict] = []
    for tr_html in rows[1:]:
        cells = _cell_texts(tr_html)
        if len(cells) < 2:
            continue
        player_cell = cells[0].strip()
        if not player_cell or player_cell.lower().startswith("total"):
            continue

        parsed = parse_player_cell(player_cell)
        if not parsed.get("Name"):
            continue

        stats: dict[str, int | float | None] = {}
        for idx, key in enumerate(stat_keys):
            value_idx = idx + 1
            stats[key] = _parse_stat_value(cells[value_idx]) if value_idx < len(cells) else None

        if not any(v is not None for v in stats.values()):
            continue

        players.append(
            {
                **parsed,
                "Stat_Category": category,
                "Stats": stats,
            }
        )
    return players


def _find_section_table_html(html: str, label: str) -> str | None:
    """Locate a stats table under a section heading or card title."""
    patterns = (
        rf">{re.escape(label)}</[^>]+>.*?<table[^>]*>(.*?)</table>",
        rf"<strong>\s*{re.escape(label)}\s*</strong>.*?<table[^>]*>(.*?)</table>",
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.S | re.I)
        if match:
            return match.group(1)
    return None


def parse_stats_html(html: str) -> list[dict]:
    players: list[dict] = []
    for category, label in STAT_SECTIONS:
        table_html = _find_section_table_html(html, label)
        if not table_html:
            continue
        players.extend(_parse_category_table(table_html, category))
    return players


def player_record_key(player: dict) -> str:
    return "|".join(
        [
            str(player.get("School_Slug") or ""),
            str(player.get("Stat_Category") or ""),
            str(player.get("Name") or ""),
            str(player.get("Number") or ""),
        ]
    )


def finalize_player(player: dict, *, team_name: str | None, school_slug: str) -> dict:
    out = dict(player)
    out["Team"] = team_name
    out["School_Slug"] = school_slug
    positions = out.get("Positions")
    out["Position_Group"] = derive_position_group(
        str(positions) if positions else None
    )
    return out


def load_player_cache(path: Path) -> dict:
    if not path.is_file():
        return {
            "last_updated": None,
            "season": None,
            "sport": SPORT_KEY,
            "players": [],
        }
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Player cache must be a JSON object.")
    raw.setdefault("players", [])
    raw.setdefault("sport", SPORT_KEY)
    return raw


def save_player_cache(path: Path, *, season: str, players: list[dict]) -> None:
    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "season": season,
        "sport": SPORT_KEY,
        "players": players,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def scrape_team(
    *,
    season: str,
    school_slug: str,
    team_name: str | None,
    timeout_ms: int,
    fetch_games: bool = True,
) -> list[dict]:
    stats_page_url = stats_url(season, school_slug)
    roster_page_url = roster_url(season, school_slug)

    try:
        stats_html = fetch_html_with_fallback(stats_page_url, timeout_ms)
    except Exception as e:
        print(f"football_player_scraper: stats failed {school_slug}: {e}", file=sys.stderr)
        try:
            stats_html = fetch_html_playwright(stats_page_url, timeout_ms)
        except Exception as e2:
            print(f"football_player_scraper: Playwright stats failed {school_slug}: {e2}", file=sys.stderr)
            return []

    try:
        roster_html = fetch_html_with_fallback(roster_page_url, timeout_ms)
    except Exception as e:
        print(f"football_player_scraper: roster failed {school_slug}: {e}", file=sys.stderr)
        roster_html = ""

    roster = parse_roster_html(roster_html, school_slug=school_slug, team_name=team_name)
    roster_by_key = _roster_lookup(roster)

    parsed = parse_stats_html(stats_html)
    out: list[dict] = []
    for row in parsed:
        _merge_roster_fields(row, roster_by_key)
        out.append(
            finalize_player(row, team_name=team_name, school_slug=school_slug)
        )

    if fetch_games and out:
        slugs_to_fetch = {
            str(player["Player_Slug"])
            for player in out
            if player.get("Player_Slug")
        }
        games_by_slug: dict[str, dict[str, list[dict]]] = {}
        for player_slug in sorted(slugs_to_fetch):
            games_by_slug[player_slug] = scrape_player_game_logs(
                player_slug,
                season=season,
                timeout_ms=timeout_ms,
            )
        for player in out:
            player_slug = player.get("Player_Slug")
            category = player.get("Stat_Category")
            if player_slug and category:
                player["Games"] = games_by_slug.get(str(player_slug), {}).get(
                    str(category), []
                )
            else:
                player["Games"] = []

    return out


def resolve_teams(
    *,
    teams_cache: Path,
    school_slug: str | None,
) -> list[dict]:
    if school_slug:
        team_name: str | None = None
        if teams_cache.is_file():
            try:
                for team in load_teams_from_cache(teams_cache):
                    if team.get("School_Slug") == school_slug:
                        team_name = team.get("Team")
                        break
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        return [{"School_Slug": school_slug, "Team": team_name}]

    if not teams_cache.is_file():
        raise FileNotFoundError(
            f"Teams cache not found at {teams_cache}. "
            "Run scraper.py --sport football first, or pass --school-slug."
        )
    teams = load_teams_from_cache(teams_cache)
    out: list[dict] = []
    seen: set[str] = set()
    for team in teams:
        slug = team.get("School_Slug")
        if not slug or slug in seen:
            continue
        seen.add(str(slug))
        out.append({"School_Slug": str(slug), "Team": team.get("Team")})
    if not out:
        raise ValueError("No School_Slug values found in teams cache.")
    return out


def scrape_all_players(
    *,
    season: str,
    teams: list[dict],
    cache_out: Path,
    timeout_ms: int,
    resume: bool,
    fetch_games: bool = True,
) -> list[dict]:
    existing = load_player_cache(cache_out) if resume else {
        "players": [],
        "season": season,
        "sport": SPORT_KEY,
    }
    if resume and existing.get("season") not in (None, season):
        print(
            f"football_player_scraper: cache season {existing.get('season')!r} "
            f"!= {season!r}; starting fresh",
            file=sys.stderr,
        )
        players_by_key: dict[str, dict] = {}
    else:
        players_by_key = {
            player_record_key(player): player
            for player in existing.get("players", [])
            if isinstance(player, dict)
        }

    done_slugs: set[str] = {
        str(player.get("School_Slug"))
        for player in players_by_key.values()
        if player.get("School_Slug")
    }

    for team_i, team in enumerate(teams, start=1):
        school_slug = str(team["School_Slug"])
        if resume and school_slug in done_slugs:
            print(
                f"football_player_scraper: [{team_i}/{len(teams)}] skip {school_slug} (resume)",
                file=sys.stderr,
            )
            continue

        team_name = team.get("Team")
        print(
            f"football_player_scraper: [{team_i}/{len(teams)}] {school_slug}",
            file=sys.stderr,
        )
        try:
            players = scrape_team(
                season=season,
                school_slug=school_slug,
                team_name=team_name,
                timeout_ms=timeout_ms,
                fetch_games=fetch_games,
            )
        except Exception as e:
            print(f"football_player_scraper: error {school_slug}: {e}", file=sys.stderr)
            continue

        for player in players:
            players_by_key[player_record_key(player)] = player

        done_slugs.add(school_slug)
        sorted_players = sorted(
            players_by_key.values(),
            key=lambda p: (
                p.get("Team") or "",
                p.get("Stat_Category") or "",
                p.get("Name") or "",
            ),
        )
        save_player_cache(cache_out, season=season, players=sorted_players)
        print(
            f"football_player_scraper:   {school_slug}: rows={len(players)} "
            f"cache_total={len(sorted_players)}",
            file=sys.stderr,
        )

    final_players = sorted(
        players_by_key.values(),
        key=lambda p: (
            p.get("Team") or "",
            p.get("Stat_Category") or "",
            p.get("Name") or "",
        ),
    )
    save_player_cache(cache_out, season=season, players=final_players)
    print(
        f"football_player_scraper: done. saved_total={len(final_players)}",
        file=sys.stderr,
    )
    return final_players


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scrape NJ.com football team stats pages (backend/CLI only)."
    )
    parser.add_argument("--season", default=DEFAULT_SEASON, help="Season id.")
    parser.add_argument("--school-slug", default=None, help="Scrape a single team.")
    parser.add_argument("--teams-cache", type=Path, default=None, help="Football team cache path.")
    parser.add_argument(
        "--cache-out",
        type=Path,
        default=None,
        help="Output cache path (default: football_player_data_cache_{season}.json).",
    )
    parser.add_argument("--resume", action="store_true", help="Skip teams already in cache.")
    parser.add_argument(
        "--no-games",
        action="store_true",
        help="Skip per-player game logs (season totals only).",
    )
    parser.add_argument("--timeout-ms", type=int, default=45000, help="Per-page timeout in ms.")
    args = parser.parse_args()

    season = args.season.strip()
    timeout_ms = max(5000, args.timeout_ms)
    versioned_teams = SCRIPT_DIR / f"football_data_cache_{season}.json"
    teams_cache = args.teams_cache or (
        versioned_teams
        if versioned_teams.is_file()
        else (DEFAULT_TEAMS_CACHE if season == DEFAULT_SEASON else versioned_teams)
    )
    cache_out = args.cache_out or (SCRIPT_DIR / f"football_player_data_cache_{season}.json")

    try:
        teams = resolve_teams(teams_cache=teams_cache, school_slug=args.school_slug)
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as e:
        print(f"football_player_scraper: {e}", file=sys.stderr)
        return 1

    players = scrape_all_players(
        season=season,
        teams=teams,
        cache_out=cache_out,
        timeout_ms=timeout_ms,
        resume=args.resume,
        fetch_games=not args.no_games,
    )
    print(f"Saved {len(players)} player stat rows to {cache_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
