"""
Scrape NJ.com boys basketball athlete rosters and per-game stats.

Basketball only. Reads School_Slug list from the team standings cache, then
scrapes each team roster and each linked player's season game log.
Players with zero games played are skipped.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from scraper import (
    BLOCKED_URL_SUBSTRINGS,
    DEFAULT_SEASON,
    SCRIPT_DIR,
    SITE_ORIGIN,
    _strip_html_tags,
    get_sport_settings,
    load_teams_from_cache,
)

SPORT_KEY = "basketball"
PATH_SEGMENT = "boysbasketball"
DEFAULT_TEAMS_CACHE = SCRIPT_DIR / "data_cache.json"
DEFAULT_PLAYER_CACHE = SCRIPT_DIR / "player_data_cache.json"

PLAYER_HREF_RE = re.compile(
    rf'href="(/player/([^/"]+)(?:/{re.escape(PATH_SEGMENT)}(?:/season/[^"/]+)?)?)"',
    re.I,
)
STAT_KEYS = ("2PT", "3PT", "FTM", "FTA", "PTS", "REB", "AST", "BLK", "STL", "GP")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def roster_url(season_id: str, school_slug: str) -> str:
    return (
        f"{SITE_ORIGIN}/school/{school_slug}/{PATH_SEGMENT}/"
        f"season/{season_id}/roster"
    )


def player_season_url(player_slug: str, season_id: str) -> str:
    return f"{SITE_ORIGIN}/player/{player_slug}/{PATH_SEGMENT}/season/{season_id}"


def _parse_int(s: str | None) -> int | None:
    if s is None:
        return None
    cleaned = re.sub(r"[^\d-]", "", str(s).strip())
    if cleaned in ("", "-"):
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _empty_to_none(s: str | None) -> str | None:
    if s is None:
        return None
    text = str(s).strip()
    return text or None


def fetch_html(url: str, timeout_sec: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_html_playwright(url: str, timeout_ms: int) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1400, "height": 900},
            )
            page = context.new_page()

            def _route(route):
                req = route.request
                u = req.url
                if any(s in u for s in BLOCKED_URL_SUBSTRINGS):
                    return route.abort()
                if req.resource_type in ("image", "font", "media"):
                    return route.abort()
                return route.continue_()

            page.route("**/*", _route)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_selector("table", timeout=min(timeout_ms, 15000))
            except Exception:
                pass
            return page.content()
        finally:
            browser.close()


def fetch_html_with_fallback(url: str, timeout_ms: int) -> str:
    try:
        return fetch_html(url, timeout_sec=max(30, timeout_ms // 1000))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"player_scraper: HTTP failed for {url!r}: {e}; trying Playwright", file=sys.stderr)
        return fetch_html_playwright(url, timeout_ms)


def _cell_texts(tr_html: str) -> list[str]:
    return [
        _strip_html_tags(td)
        for td in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr_html, re.S | re.I)
    ]


def parse_roster_html(html: str, *, school_slug: str, team_name: str | None) -> list[dict]:
    """Parse roster table rows into athlete dicts (may lack Player_Slug)."""
    athletes: list[dict] = []
    for tr_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = _cell_texts(tr_html)
        if len(cells) < 2:
            continue
        number = _empty_to_none(cells[0])
        if number and number.lower() in ("#", "no.", "number"):
            continue

        link_m = PLAYER_HREF_RE.search(tr_html)
        player_slug: str | None = None
        if link_m:
            player_slug = link_m.group(2)
            name_m = re.search(
                rf'href="/player/{re.escape(player_slug)}[^"]*"[^>]*>([^<]+)</a>',
                tr_html,
                re.I,
            )
            name = _empty_to_none(name_m.group(1) if name_m else cells[1])
        else:
            name = _empty_to_none(cells[1])

        if not name or name.lower() == "name":
            continue

        positions = _empty_to_none(cells[2]) if len(cells) > 2 else None
        class_year = _empty_to_none(cells[3]) if len(cells) > 3 else None
        ht = _empty_to_none(cells[4]) if len(cells) > 4 else None
        wt = _empty_to_none(cells[5]) if len(cells) > 5 else None

        athletes.append(
            {
                "Player_Slug": player_slug,
                "Name": name,
                "Number": number,
                "Positions": positions,
                "Class": class_year,
                "Ht": ht,
                "Wt": wt,
                "Team": team_name,
                "School_Slug": school_slug,
            }
        )
    return athletes


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


def _stats_from_cells(cells: list[str], start_idx: int) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for i, key in enumerate(STAT_KEYS):
        idx = start_idx + i
        out[key] = _parse_int(cells[idx]) if idx < len(cells) else None
    return out


def parse_player_page_html(html: str, season_id: str, base: dict) -> dict | None:
    """Build a player record with Games + Season_Totals for season_id."""
    player = dict(base)

    h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
    if h1_m:
        h1 = _strip_html_tags(h1_m.group(1))
        num_m = re.search(r"#\s*(\d+)\s*$", h1)
        if num_m and not player.get("Number"):
            player["Number"] = num_m.group(1)
        name = re.sub(r"\s*#\s*\d+\s*$", "", h1).strip()
        if name:
            player["Name"] = name

    # Season totals from Career Stats table row matching season_id
    season_totals: dict[str, int | None] | None = None
    for tr_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = _cell_texts(tr_html)
        if len(cells) >= 11 and cells[0].strip() == season_id:
            season_totals = _stats_from_cells(cells, 1)
            break

    # Game log: section titled "{season} Game Log" through Season Totals footer
    games: list[dict] = []
    section_re = re.compile(
        rf"{re.escape(season_id)}\s+Game Log(.*?)Season Totals",
        re.S | re.I,
    )
    section_m = section_re.search(html)
    if section_m:
        for tr_html in re.findall(r"<tr[^>]*>(.*?)</tr>", section_m.group(1), re.S | re.I):
            cells = _cell_texts(tr_html)
            if len(cells) < 13:
                continue
            if cells[0].strip().lower() == "date":
                continue
            date = _empty_to_none(cells[0])
            opponent_raw = cells[1]
            opponent, home = _parse_opponent_field(opponent_raw)
            result = _empty_to_none(cells[2])
            stats = _stats_from_cells(cells, 3)
            if not date or not opponent:
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

    if season_totals is None and games:
        season_totals = {k: None for k in STAT_KEYS}
        season_totals["GP"] = len(games)
        for key in STAT_KEYS:
            if key == "GP":
                continue
            vals = [g[key] for g in games if isinstance(g.get(key), int)]
            season_totals[key] = sum(vals) if vals else None
    elif season_totals is not None and games and not isinstance(season_totals.get("GP"), int):
        season_totals["GP"] = len(games)

    player["Season_Totals"] = season_totals or {k: None for k in STAT_KEYS}
    player["Games"] = games
    return player


def player_has_games(player: dict) -> bool:
    """Keep only players with a non-empty game log and GP > 0."""
    totals = player.get("Season_Totals") or {}
    gp = totals.get("GP")
    games = player.get("Games") or []
    if not games:
        return False
    if not isinstance(gp, int) or gp <= 0:
        return False
    return True


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


def scrape_roster(
    season_id: str,
    school_slug: str,
    team_name: str | None,
    timeout_ms: int,
) -> list[dict]:
    url = roster_url(season_id, school_slug)
    html = fetch_html_with_fallback(url, timeout_ms)
    athletes = parse_roster_html(html, school_slug=school_slug, team_name=team_name)
    if athletes:
        return athletes
    # Playwright retry if HTTP had no rows
    try:
        html = fetch_html_playwright(url, timeout_ms)
    except Exception as e:
        print(f"player_scraper: Playwright roster failed for {school_slug}: {e}", file=sys.stderr)
        return []
    return parse_roster_html(html, school_slug=school_slug, team_name=team_name)


def scrape_player(
    season_id: str,
    athlete: dict,
    timeout_ms: int,
) -> dict | None:
    slug = athlete.get("Player_Slug")
    if not slug:
        return None
    url = player_season_url(str(slug), season_id)
    try:
        html = fetch_html_with_fallback(url, timeout_ms)
    except Exception as e:
        print(f"player_scraper: failed {slug}: {e}", file=sys.stderr)
        return None
    player = parse_player_page_html(html, season_id, athlete)
    if player is None or not player_has_games(player):
        return None
    return player


def resolve_teams(
    *,
    teams_cache: Path,
    school_slug: str | None,
) -> list[dict]:
    if school_slug:
        team_name: str | None = None
        if teams_cache.is_file():
            try:
                for t in load_teams_from_cache(teams_cache):
                    if t.get("School_Slug") == school_slug:
                        team_name = t.get("Team")
                        break
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        return [{"School_Slug": school_slug, "Team": team_name}]

    if not teams_cache.is_file():
        raise FileNotFoundError(
            f"Teams cache not found at {teams_cache}. "
            "Run scraper.py first, or pass --school-slug for a single team."
        )
    teams = load_teams_from_cache(teams_cache)
    out = []
    seen: set[str] = set()
    for t in teams:
        slug = t.get("School_Slug")
        if not slug or slug in seen:
            continue
        seen.add(str(slug))
        out.append({"School_Slug": str(slug), "Team": t.get("Team")})
    if not out:
        raise ValueError("No School_Slug values found in teams cache.")
    return out


def scrape_all_players(
    *,
    season: str,
    teams: list[dict],
    cache_out: Path,
    workers: int,
    timeout_ms: int,
    resume: bool,
) -> list[dict]:
    existing = load_player_cache(cache_out) if resume else {
        "players": [],
        "season": season,
        "sport": SPORT_KEY,
    }
    # On resume for a different season, start fresh
    if resume and existing.get("season") not in (None, season):
        print(
            f"player_scraper: cache season {existing.get('season')!r} != {season!r}; starting fresh",
            file=sys.stderr,
        )
        players_by_slug: dict[str, dict] = {}
    else:
        players_by_slug = {
            str(p["Player_Slug"]): p
            for p in existing.get("players", [])
            if p.get("Player_Slug")
        }

    done_slugs = set(players_by_slug)
    total_skipped_no_slug = 0
    total_skipped_zero_gp = 0
    total_saved = 0

    for team_i, team in enumerate(teams, start=1):
        school_slug = str(team["School_Slug"])
        team_name = team.get("Team")
        print(
            f"player_scraper: [{team_i}/{len(teams)}] roster {school_slug}",
            file=sys.stderr,
        )
        try:
            athletes = scrape_roster(season, school_slug, team_name, timeout_ms)
        except Exception as e:
            print(f"player_scraper: roster error {school_slug}: {e}", file=sys.stderr)
            continue

        linked = [a for a in athletes if a.get("Player_Slug")]
        skipped_no_slug = len(athletes) - len(linked)
        total_skipped_no_slug += skipped_no_slug

        to_fetch = [
            a for a in linked if str(a["Player_Slug"]) not in done_slugs
        ]
        already = len(linked) - len(to_fetch)
        skipped_zero = 0
        saved_here = 0

        if to_fetch:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = {
                    pool.submit(scrape_player, season, athlete, timeout_ms): athlete
                    for athlete in to_fetch
                }
                for fut in as_completed(futures):
                    athlete = futures[fut]
                    try:
                        player = fut.result()
                    except Exception as e:
                        print(
                            f"player_scraper: worker error {athlete.get('Player_Slug')}: {e}",
                            file=sys.stderr,
                        )
                        continue
                    if player is None:
                        skipped_zero += 1
                        continue
                    slug = str(player["Player_Slug"])
                    players_by_slug[slug] = player
                    done_slugs.add(slug)
                    saved_here += 1

        total_skipped_zero_gp += skipped_zero
        total_saved += saved_here
        print(
            f"player_scraper:   {school_slug}: "
            f"roster={len(athletes)} linked={len(linked)} "
            f"new={saved_here} resumed={already} "
            f"skip_no_slug={skipped_no_slug} skip_zero_gp={skipped_zero}",
            file=sys.stderr,
        )

        # Checkpoint after each team
        save_player_cache(
            cache_out,
            season=season,
            players=sorted(players_by_slug.values(), key=lambda p: (p.get("Team") or "", p.get("Name") or "")),
        )

    print(
        f"player_scraper: done. saved_total={len(players_by_slug)} "
        f"new_this_run={total_saved} "
        f"skip_no_slug={total_skipped_no_slug} skip_zero_gp={total_skipped_zero_gp}",
        file=sys.stderr,
    )
    return list(players_by_slug.values())


def main() -> int:
    # Ensure basketball settings exist (validates sport registry)
    get_sport_settings(SPORT_KEY)

    parser = argparse.ArgumentParser(
        description="Scrape NJ.com boys basketball player game logs (backend/CLI only)."
    )
    parser.add_argument(
        "--season",
        default=DEFAULT_SEASON,
        help=f"Season id (default: {DEFAULT_SEASON}).",
    )
    parser.add_argument(
        "--school-slug",
        default=None,
        help="Scrape a single team roster (e.g. edison-edison).",
    )
    parser.add_argument(
        "--teams-cache",
        type=Path,
        default=None,
        help="Team standings cache with School_Slug values (default: season-named or legacy cache).",
    )
    parser.add_argument(
        "--cache-out",
        type=Path,
        default=None,
        help="Output player cache path (default: player_data_cache_{season}.json).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel HTTP workers for player pages (default: 4).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip players already present in --cache-out.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=45000,
        help="Per-page timeout in ms (default: 45000).",
    )
    args = parser.parse_args()

    season = args.season.strip()
    timeout_ms = max(5000, args.timeout_ms)
    workers = max(1, args.workers)

    versioned_teams = SCRIPT_DIR / f"data_cache_{season}.json"
    teams_cache = args.teams_cache or (
        versioned_teams
        if versioned_teams.is_file()
        else (DEFAULT_TEAMS_CACHE if season == DEFAULT_SEASON else versioned_teams)
    )
    cache_out = args.cache_out or (SCRIPT_DIR / f"player_data_cache_{season}.json")

    try:
        teams = resolve_teams(teams_cache=teams_cache, school_slug=args.school_slug)
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as e:
        print(f"player_scraper: {e}", file=sys.stderr)
        return 1

    players = scrape_all_players(
        season=season,
        teams=teams,
        cache_out=cache_out,
        workers=workers,
        timeout_ms=timeout_ms,
        resume=args.resume,
    )
    print(f"Saved {len(players)} players to {cache_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
