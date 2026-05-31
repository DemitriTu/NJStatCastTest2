"""
Scrape NJ.com high school basketball standings with Playwright.
Handles JS-rendered content and tables inside iframes.
Optional schedule scrape for strength of schedule (SOS).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import Playwright, Page, sync_playwright

WL_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
SCHEDULE_RESULT_RE = re.compile(r"^([WL])\s+(\d+)\s*-\s*(\d+)$", re.I)
SCHOOL_PATH_RE = re.compile(r"/school/([^/]+)/boysbasketball", re.I)
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_CACHE_PATH = SCRIPT_DIR / "data_cache.json"

DEFAULT_SEASON = "2025-2026"
STANDINGS_BASE = "https://highschoolsports.nj.com/boysbasketball/standings/season"
SITE_ORIGIN = "https://highschoolsports.nj.com"

# Query-string values as NJ.com expects them (spaces / hyphens preserved).
NJ_CONFERENCES: tuple[str, ...] = (
    "BCSL",
    "Big North",
    "Cape-Atlantic",
    "Colonial",
    "CVC",
    "GMC",
    "HCIAL",
    "NJAC",
    "NJIC",
    "Olympic",
    "SEC",
    "Shore",
    "Skyland",
    "Tri-County",
    "UCC",
)

DEFAULT_URL = f"{STANDINGS_BASE}/{DEFAULT_SEASON}?" + urlencode({"conference": "GMC"})

# Blocking third-party ad/analytics requests prevents sponsored `hs-offer` rows
# from replacing real standings rows in headless Chromium.
BLOCKED_URL_SUBSTRINGS = (
    "googlesyndication",
    "doubleclick.net",
    "adsafeprotected",
    "adsrvr.org",
    "criteo.com",
    "scorecardresearch",
    "linkedin.com/px",
    "tinypass.com",
    "postrelease.com",
    "rubiconproject",
    "openx.net",
    "liadm.com",
    "crwdcntrl.net",
    "amazon-adsystem",
    "analytics.yahoo",
    "google-analytics.com",
    "googletagmanager.com",
    "matheranalytics",
    "parsely.com",
    "i.matheranalytics",
)


def conference_standings_url(season_id: str, conference: str) -> str:
    return f"{STANDINGS_BASE}/{season_id}?" + urlencode({"conference": conference})


def team_season_url(season_id: str, school_slug: str) -> str:
    return f"{SITE_ORIGIN}/school/{school_slug}/boysbasketball/season/{season_id}"


def compute_avg_margin(pf: int, pa: int, gp: int) -> float:
    """Average scoring margin per game: (PF - PA) / GP."""
    if gp <= 0:
        raise ValueError("GP must be positive")
    return round((pf - pa) / gp, 3)


def _norm_team_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _cell_text(cell) -> str:
    return (cell.inner_text() or "").strip()


def _parse_int(s: str) -> int | None:
    try:
        return int(re.sub(r"[^\d-]", "", s))
    except ValueError:
        return None


def _slug_from_href(href: str | None) -> str:
    if not href:
        return ""
    m = SCHOOL_PATH_RE.search(href)
    return m.group(1) if m else ""


def _row_is_data_row(cells: list[str]) -> bool:
    if len(cells) < 3:
        return False
    if WL_RE.match(cells[1] or ""):
        return True
    return False


def _extract_rows_from_table_html(frame) -> list[dict[str, str | int | float]]:
    """Parse standings-like tables: Team, W-L, ..., PF, PA (last two numeric)."""
    rows_out: list[dict[str, str | int | float]] = []
    tables = frame.locator("table")
    count = tables.count()
    for i in range(count):
        table = tables.nth(i)
        trs = table.locator("tr")
        n = trs.count()
        for r in range(n):
            row = trs.nth(r)
            cls = (row.get_attribute("class") or "").lower()
            if "hs-offer" in cls or "group-header" in cls:
                continue
            tds = row.locator("td, th")
            m = tds.count()
            if m < 9:
                continue
            texts = [_cell_text(tds.nth(j)) for j in range(m)]
            if not _row_is_data_row(texts):
                continue
            w_m = WL_RE.match(texts[1])
            if not w_m:
                continue
            wins, losses = int(w_m.group(1)), int(w_m.group(2))
            gp = wins + losses
            pf = _parse_int(texts[-2])
            pa = _parse_int(texts[-1])
            if pf is None or pa is None:
                continue
            team = texts[0].replace("\n", " ").strip()
            if not team or team.upper() in ("TEAM", "SCHOOL"):
                continue
            margin = compute_avg_margin(pf, pa, gp)
            first_cell = row.locator("td").first
            href = None
            if first_cell.count() > 0:
                al = first_cell.locator("a")
                if al.count() > 0:
                    href = al.first.get_attribute("href")
            school_slug = _slug_from_href(href)
            win_pct = round(wins / gp, 4) if gp else 0.0
            rows_out.append(
                {
                    "Team": team,
                    "Wins": wins,
                    "Losses": losses,
                    "GP": gp,
                    "Win_Pct": win_pct,
                    "PF": pf,
                    "PA": pa,
                    "Avg_Margin": margin,
                    "School_Slug": school_slug,
                }
            )
    return rows_out


def _dedupe_within_page(rows: list[dict]) -> list[dict]:
    by_team: dict[str, dict] = {}
    for row in rows:
        by_team[row["Team"]] = row
    return list(by_team.values())


def _dedupe_across_conferences(rows: list[dict]) -> list[dict]:
    """Same team name in multiple conferences: larger GP wins; tie-breaker prefers row with School_Slug."""
    by_team: dict[str, dict] = {}
    for row in rows:
        name = row["Team"]
        if name not in by_team:
            by_team[name] = row
            continue
        cur = by_team[name]
        rgp, cgp = int(row["GP"]), int(cur["GP"])
        if rgp > cgp:
            by_team[name] = row
        elif rgp < cgp:
            continue
        else:
            rs = (row.get("School_Slug") or "").strip()
            cs = (cur.get("School_Slug") or "").strip()
            if rs and not cs:
                by_team[name] = row
    return list(by_team.values())


def _conference_from_standings_url(url: str) -> str | None:
    vals = parse_qs(urlparse(url).query).get("conference")
    return vals[0] if vals else None


def _route_handler(route):
    u = route.request.url
    if any(s in u for s in BLOCKED_URL_SUBSTRINGS):
        return route.abort()
    return route.continue_()


def _schedule_route_handler(route):
    """Block ads/analytics plus heavy assets on schedule pages (faster loads; DOM text unchanged)."""
    req = route.request
    u = req.url
    if any(s in u for s in BLOCKED_URL_SUBSTRINGS):
        return route.abort()
    if req.resource_type in ("image", "font", "media"):
        return route.abort()
    return route.continue_()


def _launch_context_page(p: Playwright):
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1400, "height": 900},
    )
    page = context.new_page()
    page.route("**/*", _route_handler)
    return browser, context, page


def _scrape_standings_page(page: Page, url: str, timeout_ms: int) -> list[dict]:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 45000))
    except Exception:
        pass
    page.wait_for_timeout(1500)
    try:
        page.wait_for_selector("table", timeout=timeout_ms)
    except Exception:
        pass
    all_rows: list[dict] = []
    for frame in page.frames:
        try:
            all_rows.extend(_extract_rows_from_table_html(frame))
        except Exception:
            continue
    return _dedupe_within_page(all_rows)


def _opponent_name_from_schedule_row(row) -> str:
    link = row.locator("a[href^='/game/']").first
    if link.count() == 0:
        return ""
    name_el = link.locator("span.ml-1")
    if name_el.count() > 0:
        return name_el.inner_text().strip()
    img = link.locator("img")
    if img.count() > 0:
        return (img.get_attribute("alt") or "").strip()
    return ""


def _parse_schedule_game_row(row) -> dict | None:
    """Parse one schedule row; returns game dict or None if opponent missing."""
    opponent = _opponent_name_from_schedule_row(row)
    if not opponent:
        return None
    game: dict = {"Opponent": opponent}
    tds = row.locator("td")
    if tds.count() >= 3:
        result_text = tds.nth(2).inner_text().strip()
        m = SCHEDULE_RESULT_RE.match(result_text)
        if m:
            pf, pa = int(m.group(2)), int(m.group(3))
            game["Won"] = m.group(1).upper() == "W"
            game["PF"] = pf
            game["PA"] = pa
            game["Margin"] = pf - pa
    return game


def scrape_schedule_games(page: Page, season_id: str, school_slug: str, timeout_ms: int) -> list[dict]:
    """Completed and scheduled games from the team season schedule table."""
    url = team_season_url(season_id, school_slug)
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 28000))
    except Exception:
        pass
    page.wait_for_timeout(800)
    try:
        page.wait_for_selector("tr:has(a[href*='/game/'])", timeout=min(timeout_ms, 18000))
    except Exception:
        pass
    rows = page.locator("tr:has(a[href*='/game/'])")
    k = rows.count()
    games: list[dict] = []
    seen: set[tuple[str, int, int]] = set()
    for i in range(k):
        row = rows.nth(i)
        game = _parse_schedule_game_row(row)
        if not game:
            continue
        if "PF" in game and "PA" in game:
            key = (_norm_team_name(game["Opponent"]), int(game["PF"]), int(game["PA"]))
            if key in seen:
                continue
            seen.add(key)
        games.append(game)
    return games


def scrape_schedule_opponent_names(page: Page, season_id: str, school_slug: str, timeout_ms: int) -> list[str]:
    """Opponent display names from the team season schedule table."""
    names: list[str] = []
    seen: set[str] = set()
    for game in scrape_schedule_games(page, season_id, school_slug, timeout_ms):
        key = _norm_team_name(game["Opponent"])
        if key in seen:
            continue
        seen.add(key)
        names.append(game["Opponent"])
    return names


def _resolve_opponent_to_slug(opponent_name: str, slug_by_norm: dict[str, str], norm_list: list[str]) -> str | None:
    """Map schedule opponent label to a School_Slug from our standings set."""
    o = _norm_team_name(opponent_name)
    if not o:
        return None
    if o in slug_by_norm:
        return slug_by_norm[o]
    for tn, slug in slug_by_norm.items():
        if len(o) >= 8 and len(tn) >= 8 and (o in tn or tn in o):
            if abs(len(tn) - len(o)) <= 24:
                return slug
    close = get_close_matches(o, norm_list, n=1, cutoff=0.82)
    if close:
        return slug_by_norm.get(close[0])
    return None


def _resolve_games_to_slugs(
    games: list[dict],
    self_slug: str,
    slug_by_norm: dict[str, str],
    norm_list: list[str],
) -> list[dict]:
    resolved: list[dict] = []
    for game in games:
        copy = dict(game)
        opp_slug = _resolve_opponent_to_slug(str(game.get("Opponent", "")), slug_by_norm, norm_list)
        copy["Opponent_Slug"] = opp_slug
        resolved.append(copy)
    return resolved


def _opponent_slugs_from_games(games: list[dict], self_slug: str) -> list[str]:
    slugs: list[str] = []
    seen: set[str] = set()
    for game in games:
        opp = (game.get("Opponent_Slug") or "").strip()
        if not opp or opp == self_slug or opp in seen:
            continue
        seen.add(opp)
        slugs.append(opp)
    return slugs


def _resolve_names_to_slugs(
    names: list[str],
    self_slug: str,
    slug_by_norm: dict[str, str],
    norm_list: list[str],
) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for nm in names:
        r = _resolve_opponent_to_slug(nm, slug_by_norm, norm_list)
        if r and r != self_slug and r not in seen:
            seen.add(r)
            resolved.append(r)
    return resolved


def _build_slug_lookup(teams: list[dict]) -> tuple[dict[str, str], list[str]]:
    slug_by_norm: dict[str, str] = {}
    for t in teams:
        slug = (t.get("School_Slug") or "").strip()
        if not slug:
            continue
        n = _norm_team_name(str(t["Team"]))
        slug_by_norm.setdefault(n, slug)
    return slug_by_norm, sorted(slug_by_norm.keys())


def _avg_win_pct(slugs: list[str], win_pct: dict[str, float]) -> float | None:
    vals = [win_pct[s] for s in slugs if s in win_pct]
    if not vals:
        return None
    return sum(vals) / len(vals)


def compute_sos_for_teams(
    teams: list[dict],
    opponents_by_slug: dict[str, list[str]],
) -> None:
    """
    SOS = (2 * Opp_Win_Pct + Opp_Opp_Win_Pct) / 3.
    Opp_Win_Pct: mean win% of opponents (known slugs only).
    Opp_Opp_Win_Pct: mean over opponents of (mean win% of that opponent's opponents).
    Mutates teams in place; sets SOS, Opp_Win_Pct, Opp_Opp_Win_Pct or None.
    """
    win_pct: dict[str, float] = {}
    for t in teams:
        slug = (t.get("School_Slug") or "").strip()
        if not slug:
            continue
        gp = int(t["GP"])
        w = int(t["Wins"])
        win_pct[slug] = (w / gp) if gp else 0.0

    for t in teams:
        slug = (t.get("School_Slug") or "").strip()
        if not slug:
            t["Opp_Win_Pct"] = None
            t["Opp_Opp_Win_Pct"] = None
            t["SOS"] = None
            continue
        opps = opponents_by_slug.get(slug, [])
        ow = _avg_win_pct(opps, win_pct)
        oow_parts: list[float] = []
        for o in opps:
            o_opps = opponents_by_slug.get(o, [])
            sub = _avg_win_pct(o_opps, win_pct)
            if sub is not None:
                oow_parts.append(sub)
        oow = sum(oow_parts) / len(oow_parts) if oow_parts else None
        t["Opp_Win_Pct"] = round(ow, 4) if ow is not None else None
        t["Opp_Opp_Win_Pct"] = round(oow, 4) if oow is not None else None
        if ow is not None and oow is not None:
            t["SOS"] = round((2 * ow + oow) / 3, 4)
        else:
            t["SOS"] = None


def _schedule_worker(
    worker_id: int,
    slugs: list[str],
    season_id: str,
    timeout_ms: int,
    slug_by_norm: dict[str, str],
    norm_list: list[str],
) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if not slugs:
        return out
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()
        page.route("**/*", _schedule_route_handler)
        try:
            total = len(slugs)
            for n, slug in enumerate(slugs, start=1):
                if n == 1 or n % 25 == 0 or n == total:
                    print(
                        f"scraper: worker{worker_id} schedule {n}/{total} …",
                        file=sys.stderr,
                    )
                try:
                    games = scrape_schedule_games(page, season_id, slug, timeout_ms)
                    out[slug] = _resolve_games_to_slugs(games, slug, slug_by_norm, norm_list)
                except Exception as e:
                    print(f"scraper: schedule failed for {slug!r}: {e}", file=sys.stderr)
                    out[slug] = []
        finally:
            context.close()
            browser.close()
    return out


def scrape_games_map(
    season_id: str,
    teams: list[dict],
    timeout_ms: int,
    parallel_workers: int = 4,
) -> dict[str, list[dict]]:
    slug_by_norm, norm_list = _build_slug_lookup(teams)
    slugs = [(t.get("School_Slug") or "").strip() for t in teams if (t.get("School_Slug") or "").strip()]
    if not slugs:
        return {}

    workers = max(1, min(max(1, parallel_workers), len(slugs)))
    if workers == 1:
        return _schedule_worker(0, slugs, season_id, timeout_ms, slug_by_norm, norm_list)

    chunks: list[list[str]] = [[] for _ in range(workers)]
    for i, s in enumerate(slugs):
        chunks[i % workers].append(s)

    merged: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = []
        for wid, chunk in enumerate(chunks):
            if not chunk:
                continue
            futs.append(
                ex.submit(
                    _schedule_worker,
                    wid,
                    chunk,
                    season_id,
                    timeout_ms,
                    slug_by_norm,
                    norm_list,
                )
            )
        for fut in as_completed(futs):
            merged.update(fut.result())
    return merged


def scrape_opponents_map(
    season_id: str,
    teams: list[dict],
    timeout_ms: int,
    parallel_workers: int = 4,
) -> dict[str, list[str]]:
    games_by_slug = scrape_games_map(season_id, teams, timeout_ms, parallel_workers=parallel_workers)
    return {
        slug: _opponent_slugs_from_games(games, slug)
        for slug, games in games_by_slug.items()
    }


def attach_schedule_and_sos(
    season_id: str,
    teams: list[dict],
    timeout_ms: int = 60000,
    parallel_workers: int = 4,
) -> None:
    games_by_slug = scrape_games_map(
        season_id,
        teams,
        timeout_ms,
        parallel_workers=parallel_workers,
    )
    opponents_by_slug = {
        slug: _opponent_slugs_from_games(games, slug) for slug, games in games_by_slug.items()
    }
    slug_to_team = {
        (t.get("School_Slug") or "").strip(): t for t in teams if (t.get("School_Slug") or "").strip()
    }
    for slug, games in games_by_slug.items():
        team = slug_to_team.get(slug)
        if team is not None:
            team["Games"] = games
    for t in teams:
        t.setdefault("Games", [])
    compute_sos_for_teams(teams, opponents_by_slug)


def scrape_standings(url: str, timeout_ms: int = 60000) -> list[dict[str, str | int | float]]:
    with sync_playwright() as p:
        browser, context, page = _launch_context_page(p)
        try:
            rows = _scrape_standings_page(page, url, timeout_ms)
        finally:
            context.close()
            browser.close()
    conf = _conference_from_standings_url(url)
    if conf:
        for r in rows:
            r["Conference"] = conf
    return rows


def scrape_all_conferences(season_id: str = DEFAULT_SEASON, timeout_ms: int = 60000) -> list[dict]:
    merged: list[dict] = []
    with sync_playwright() as p:
        browser, context, page = _launch_context_page(p)
        try:
            for conf in NJ_CONFERENCES:
                url = conference_standings_url(season_id, conf)
                try:
                    rows = _scrape_standings_page(page, url, timeout_ms)
                    for r in rows:
                        copy = dict(r)
                        copy["Conference"] = conf
                        merged.append(copy)
                except Exception as e:
                    print(f"scraper: skipping conference {conf!r} ({url}): {e}", file=sys.stderr)
        finally:
            context.close()
            browser.close()
    return _dedupe_across_conferences(merged)


def load_teams_from_cache(path: Path) -> list[dict]:
    """Load team rows from data_cache.json ({teams: [...]}) or a plain JSON list."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "teams" in raw:
        teams = raw["teams"]
    elif isinstance(raw, list):
        teams = raw
    else:
        raise ValueError("Cache file must be a list of teams or { \"teams\": [...] }.")
    if not teams:
        raise ValueError("Cache file contains no teams.")
    return teams


def save_teams(
    teams: list[dict],
    json_path: Path | None = None,
    csv_path: Path | None = None,
) -> None:
    json_path = json_path or (SCRIPT_DIR / "teams.json")
    csv_path = csv_path or (SCRIPT_DIR / "data.csv")
    ranked = sorted(teams, key=lambda x: float(x["Avg_Margin"]), reverse=True)
    ranked = [{k: v for k, v in t.items() if k != "Category"} for t in ranked]
    json_path.write_text(json.dumps(ranked, indent=2), encoding="utf-8")
    cache_payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "teams": ranked,
    }
    DATA_CACHE_PATH.write_text(json.dumps(cache_payload, indent=2), encoding="utf-8")
    if ranked:
        csv_rows = [{k: v for k, v in t.items() if k != "Games"} for t in ranked]
        fieldnames = list(csv_rows[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(csv_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape NJ.com basketball standings.")
    parser.add_argument(
        "--url",
        default=None,
        help="If set, scrape only this standings URL (single conference).",
    )
    parser.add_argument(
        "--season",
        default=os.environ.get("NJ_STANDINGS_SEASON", DEFAULT_SEASON),
        help="Season id for multi-conference scrape (e.g. 2025-2026).",
    )
    parser.add_argument(
        "--skip-schedule",
        action="store_true",
        help="Skip schedule scrape and SOS (standings only, faster).",
    )
    parser.add_argument(
        "--sos-only",
        action="store_true",
        help="Skip standings scrape; load teams from --cache-in and compute SOS only.",
    )
    parser.add_argument(
        "--cache-in",
        type=Path,
        default=DATA_CACHE_PATH,
        help="JSON file for --sos-only (default: data_cache.json).",
    )
    parser.add_argument(
        "--schedule-timeout-ms",
        type=int,
        default=int(os.environ.get("NJ_SCHEDULE_TIMEOUT_MS", "45000")),
        help="Timeout (ms) for each team schedule page during SOS.",
    )
    parser.add_argument(
        "--schedule-workers",
        type=int,
        default=int(os.environ.get("NJ_SCHEDULE_WORKERS", "4")),
        help="Parallel Playwright workers for schedule pages (default: 4).",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=SCRIPT_DIR / "teams.json",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=SCRIPT_DIR / "data.csv",
    )
    args = parser.parse_args()

    schedule_ms = max(5000, args.schedule_timeout_ms)
    schedule_workers = max(1, args.schedule_workers)

    if args.sos_only:
        if args.skip_schedule:
            print("scraper: --sos-only ignores --skip-schedule", file=sys.stderr)
        if not args.cache_in.is_file():
            print(f"scraper: no cache file at {args.cache_in}", file=sys.stderr)
            return 1
        try:
            teams = load_teams_from_cache(args.cache_in)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"scraper: failed to load cache: {e}", file=sys.stderr)
            return 1
        attach_schedule_and_sos(
            args.season,
            teams,
            timeout_ms=schedule_ms,
            parallel_workers=schedule_workers,
        )
        save_teams(teams, json_path=args.json_out, csv_path=args.csv_out)
        with_sos = sum(1 for t in teams if t.get("SOS") is not None)
        print(
            f"SOS computed for {with_sos}/{len(teams)} teams.",
            file=sys.stderr,
        )
        print(f"Saved {len(teams)} teams to {args.json_out} and {args.csv_out}")
        return 0

    url = args.url or os.environ.get("NJ_STANDINGS_URL")
    if url:
        teams = scrape_standings(url)
    else:
        teams = scrape_all_conferences(args.season)

    if not teams:
        print("No teams extracted. Check URL or page structure.", file=sys.stderr)
        return 1

    if args.skip_schedule:
        for t in teams:
            t.setdefault("Opp_Win_Pct", None)
            t.setdefault("Opp_Opp_Win_Pct", None)
            t.setdefault("SOS", None)
            t.setdefault("Games", [])
        save_teams(teams, json_path=args.json_out, csv_path=args.csv_out)
        print(f"Saved {len(teams)} teams to {args.json_out} and {args.csv_out}")
        return 0

    for t in teams:
        t.setdefault("Opp_Win_Pct", None)
        t.setdefault("Opp_Opp_Win_Pct", None)
        t.setdefault("SOS", None)
        t.setdefault("Games", [])
    save_teams(teams, json_path=args.json_out, csv_path=args.csv_out)
    print(
        "Checkpoint: standings saved (SOS not computed yet). "
        "If this run is interrupted, use --sos-only to finish SOS.",
        file=sys.stderr,
    )

    attach_schedule_and_sos(
        args.season,
        teams,
        timeout_ms=schedule_ms,
        parallel_workers=schedule_workers,
    )
    save_teams(teams, json_path=args.json_out, csv_path=args.csv_out)
    with_sos = sum(1 for t in teams if t.get("SOS") is not None)
    print(f"SOS computed for {with_sos}/{len(teams)} teams.", file=sys.stderr)
    print(f"Saved {len(teams)} teams to {args.json_out} and {args.csv_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
