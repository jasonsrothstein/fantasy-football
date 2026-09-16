"""
lib/yahoo_cookies.py  –  Yahoo Fantasy Sports API client using browser session cookies.

This bypasses Yahoo's OAuth developer-app system entirely.  All calls go to the
same internal API that the Yahoo Fantasy website uses, authenticated with the
user's browser session cookies.

Usage:
    session = login("Scripts/auth/yahoo_cookies.txt")
    game_key = get_nfl_game_key(session, 2025)   # → "461"
    settings  = get_scoring_settings(session, "461", "656728")
    roster    = get_roster(session, "461", "656728", team_id="3", week=5)

Cookie refresh:
    Cookies expire after roughly 24 hours.  When they do, log into
    football.fantasysports.yahoo.com in your browser and replace the contents
    of Scripts/auth/yahoo_cookies.txt with fresh cookies from DevTools →
    Network → any request → Request Headers → Cookie.
"""

import logging
import re
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

_BASE = "https://pub-api-rw.fantasysports.yahoo.com/fantasy/v2"
_WEBSITE = "https://football.fantasysports.yahoo.com"

# Reuse the same stat-ID map as the OAuth client so callers are interchangeable.
STAT_ID_MAP = {
    "4":  "pass_yards",
    "8":  "pass_td",
    "9":  "int_thrown",
    "11": "rush_attempts",
    "12": "rush_yards",
    "13": "rush_td",
    "15": "receptions",
    "16": "rec_yards",
    "17": "rec_td",
    "22": "two_pt_conversion",
    "23": "fumble_lost",
    "25": "fg_0_19",
    "26": "fg_20_29",
    "27": "fg_30_39",
    "28": "fg_40_49",
    "29": "fg_50_plus",
    "30": "fg_miss_0_19",
    "31": "fg_miss_20_29",
    "32": "fg_miss_30_39",
    "33": "fg_miss_40_49",
    "34": "fg_miss_50_plus",
    "37": "pat_made",
    "38": "pat_missed",
    "45": "def_sack",
    "46": "def_int",
    "47": "def_fumble_rec",
    "48": "def_td",
    "49": "def_safety",
    "50": "def_blocked_kick",
    "53": "def_pts_allowed",
    "54": "def_yards_allowed",
}


class YahooCookieSession:
    """Thin wrapper around requests.Session that injects the crumb automatically."""

    def __init__(self, cookie_str: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Cookie": cookie_str,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        })
        self._crumb: Optional[str] = None

    def _get_crumb(self, hint_league_id: Optional[str] = None) -> str:
        """Fetch (and cache) the crumb token from a Yahoo Fantasy page.

        The crumb only appears on pages that load the full JS app, so we try
        a few fallback URLs — a league-specific page is most reliable.
        """
        if self._crumb:
            return self._crumb

        candidates = []
        if hint_league_id:
            candidates.append(f"{_WEBSITE}/f1/{hint_league_id}")
        candidates += [
            f"{_WEBSITE}/f1/",
            _WEBSITE + "/",
        ]

        for url in candidates:
            log.debug("Fetching crumb from %s …", url)
            r = self.session.get(url)
            if not r.ok:
                continue
            m = re.search(r"crumb=fantasy_apis\|([A-Za-z0-9_\-]+)", r.text)
            if m:
                self._crumb = m.group(1)
                log.debug("Crumb obtained from %s", url)
                return self._crumb

        raise RuntimeError(
            "Could not extract crumb from Yahoo Fantasy page.  "
            "Your cookies may have expired — log into football.fantasysports.yahoo.com "
            "and refresh Scripts/auth/yahoo_cookies.txt."
        )

    def get(self, url: str, **extra_params) -> dict:
        """GET a Yahoo Fantasy v2 JSON endpoint; returns parsed JSON."""
        params = {
            "format": "json",
            "crumb": f"fantasy_apis|{self._get_crumb()}",
        }
        params.update(extra_params)
        resp = self.session.get(url, params=params)
        if not resp.ok:
            try:
                body = resp.json()
                msg = body.get("error", {}).get("description", resp.text[:300])
            except Exception:
                msg = resp.text[:300]
            log.error("Yahoo %d on %s — %s", resp.status_code, url, msg)
            if resp.status_code in (401, 403):
                raise PermissionError(
                    f"Yahoo returned {resp.status_code} on {url}.\n"
                    "If you see 'cookies expired', refresh yahoo_cookies.txt from your browser."
                )
        resp.raise_for_status()
        return resp.json()


def login(
    cookie_path: str = "Scripts/auth/yahoo_cookies.txt",
    league_id: Optional[str] = None,
) -> "YahooCookieSession":
    """Load cookies from file and return a ready session.

    Passing league_id speeds up crumb acquisition by fetching a league-specific
    page, which reliably contains the crumb token.
    """
    p = Path(cookie_path)
    if not p.exists():
        raise FileNotFoundError(
            f"Cookie file not found: {cookie_path}\n"
            "Log into football.fantasysports.yahoo.com, open DevTools → Network → "
            "any request → Request Headers → Cookie, and paste the value into that file."
        )
    cookie_str = p.read_text().strip()
    session = YahooCookieSession(cookie_str)
    # Warm up the crumb so we fail fast on expired cookies.
    session._get_crumb(hint_league_id=league_id)
    log.info("Yahoo cookie session ready.")
    return session


def get_nfl_game_key(session: YahooCookieSession, season: int) -> str:
    """Return the game key (e.g. '461') for the given NFL season year."""
    data = session.get(f"{_BASE}/users;use_login=1/games;game_codes=nfl")
    games = data["fantasy_content"]["users"]["0"]["user"][1]["games"]
    for i in range(games["count"]):
        g = games[str(i)]["game"][0]
        if str(g.get("season")) == str(season):
            key = g["game_key"]
            log.info("NFL game key for %s: %s", season, key)
            return key
    raise ValueError(
        f"No NFL game found for season {season} in your Yahoo account history."
    )


def get_scoring_settings(
    session: YahooCookieSession, game_key: str, league_id: str
) -> dict:
    """Return a dict mapping internal stat names → points-per-unit."""
    url = f"{_BASE}/league/{game_key}.l.{league_id}/settings"
    data = session.get(url)
    stat_mods = (
        data["fantasy_content"]["league"][1]["settings"][0]
        ["stat_modifiers"]["stats"]
    )
    rules: dict = {}
    for item in stat_mods:
        s = item.get("stat", {})
        stat_id = str(s.get("stat_id", ""))
        value = float(s.get("value", 0))
        name = STAT_ID_MAP.get(stat_id)
        if name and value != 0:
            rules[name] = value
    log.info("Loaded %d scoring rules from league %s", len(rules), league_id)
    return rules


def get_weekly_matchups(
    session: YahooCookieSession, game_key: str, league_id: str, week: int
) -> list:
    """Return list of matchup dicts for the given week."""
    url = f"{_BASE}/league/{game_key}.l.{league_id}/scoreboard;week={week}"
    data = session.get(url)
    scoreboard = data["fantasy_content"]["league"][1]["scoreboard"]["0"]["matchups"]
    num_matchups = scoreboard["count"]

    matchups = []
    for i in range(num_matchups):
        m = scoreboard[str(i)]["matchup"]
        week_start = m.get("week_start", "")
        week_end = m.get("week_end", "")
        teams_raw = m["0"]["teams"]

        def extract_team(t_dict):
            td = t_dict["team"]
            meta = td[0]
            stats = td[1] if len(td) > 1 else {}
            name, tid = "", ""
            for item in meta:
                if "name" in item:
                    name = item["name"]
                if "team_id" in item:
                    tid = str(item["team_id"])
            score = 0.0
            if "team_points" in stats:
                score = float(stats["team_points"].get("total", 0))
            return {"team_id": tid, "team_name": name, "score": score}

        t1 = extract_team(teams_raw["0"])
        t2 = extract_team(teams_raw["1"])
        matchups.append({
            "team1_id": t1["team_id"], "team1_name": t1["team_name"],
            "team1_score": t1["score"],
            "team2_id": t2["team_id"], "team2_name": t2["team_name"],
            "team2_score": t2["score"],
            "week_start": week_start, "week_end": week_end,
        })
    return matchups


BENCH_POSITIONS = {"BN", "IR"}


def get_roster(
    session: YahooCookieSession,
    game_key: str,
    league_id: str,
    team_id: str,
    week: int,
) -> dict:
    """Return starter/bench roster for one team in one week."""
    url = f"{_BASE}/team/{game_key}.l.{league_id}.t.{team_id}/roster;week={week}"
    data = session.get(url)
    team_data = data["fantasy_content"]["team"]

    team_name = ""
    for item in team_data[0]:
        if "name" in item:
            team_name = item["name"]

    players_raw = team_data[1]["roster"]["0"]["players"]
    starters, bench = [], []

    for i in range(players_raw["count"]):
        p = players_raw[str(i)]["player"]
        meta = p[0]
        status = p[1]

        name, player_id, nfl_team = "", "", ""
        for item in meta:
            if "player_id" in item:
                player_id = str(item["player_id"])
            if "name" in item:
                name = item["name"].get("full", "")
            if "editorial_team_abbr" in item:
                nfl_team = item["editorial_team_abbr"].upper()

        roster_pos = status.get("selected_position", [{}])[1].get("position", "BN")

        player = {
            "name": name,
            "position": roster_pos,
            "nfl_team": nfl_team,
            "player_id": player_id,
        }
        if roster_pos in BENCH_POSITIONS:
            bench.append(player)
        else:
            starters.append(player)

    return {
        "team_id": team_id,
        "team_name": team_name,
        "starters": starters,
        "bench": bench,
    }


def get_team_logos(session: YahooCookieSession, game_key: str, league_id: str) -> dict:
    """
    Return a dict mapping team_id (str) -> logo URL (str) for all teams
    in the league, using the /league/teams endpoint which includes team_logos.
    """
    url = f"{_BASE}/league/{game_key}.l.{league_id}/teams"
    data = session.get(url)
    logos: dict = {}
    try:
        teams = data["fantasy_content"]["league"][1]["teams"]
        for k, v in teams.items():
            if k == "count":
                continue
            team_meta = v["team"][0]
            tid, logo = "", ""
            for item in team_meta:
                if isinstance(item, dict):
                    if "team_id" in item:
                        tid = str(item["team_id"])
                    if "team_logos" in item:
                        try:
                            logo = item["team_logos"][0]["team_logo"]["url"]
                        except (IndexError, KeyError, TypeError):
                            pass
            if tid:
                logos[tid] = logo
    except (KeyError, IndexError, TypeError) as exc:
        log.warning("Could not parse team logos: %s", exc)
    return logos
