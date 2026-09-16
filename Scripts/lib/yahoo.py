"""
lib/yahoo.py  –  Yahoo Fantasy Sports API client.

Handles OAuth, game-key lookup, league settings, rosters, and scoreboards
for any historical NFL season accessible to the authenticated user.
"""

import json
import logging
import time
import webbrowser
from pathlib import Path
from typing import Optional

from yahoo_oauth import OAuth2 as _OAuth2Base

log = logging.getLogger(__name__)


class _OAuth2WithScope(_OAuth2Base):
    """
    Drop-in replacement for yahoo_oauth.OAuth2 that injects a scope parameter
    into the Yahoo authorization URL.  The base library hard-codes the auth URL
    call without scope, so Fantasy Sports endpoints always return 403 without
    this override.
    """

    def handler(self):
        scope = getattr(self, "scope", None)
        url_kwargs = {
            "redirect_uri": self.callback_uri,
            "response_type": "code",
        }
        if scope:
            url_kwargs["scope"] = scope

        authorize_url = self.oauth.get_authorize_url(**url_kwargs)

        logging.getLogger("yahoo_oauth").debug(
            "AUTHORIZATION URL (scope=%s): %s", scope, authorize_url
        )

        if self.browser_callback:
            webbrowser.open(authorize_url)
            self.verifier = input("Enter verifier : ")
        else:
            self.verifier = input(
                f"AUTHORIZATION URL : {authorize_url}\nEnter verifier : "
            )

        self.token_time = time.time()
        credentials = {"token_time": self.token_time}
        headers = self.generate_oauth2_headers()
        raw_access = self.oauth.get_raw_access_token(
            data={
                "code": self.verifier,
                "redirect_uri": self.callback_uri,
                "grant_type": "authorization_code",
            },
            headers=headers,
        )
        credentials.update(self.oauth2_access_parser(raw_access))
        return credentials


OAuth2 = _OAuth2WithScope

BASE = "https://fantasysports.yahooapis.com/fantasy/v2"

# ─── Standard Yahoo stat IDs → our internal names ────────────────────────────
# Only the stat IDs relevant to fantasy scoring are listed here.
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
    # Kicker
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
    # Defense / Special Teams
    "45": "def_sack",
    "46": "def_int",
    "47": "def_fumble_rec",
    "48": "def_td",
    "49": "def_safety",
    "50": "def_blocked_kick",
    "53": "def_pts_allowed",
    "54": "def_yards_allowed",
}


YAHOO_SCOPE = "fspt-w"  # Fantasy Sports read/write scope (used by Fantasy Tracker 2 app)


def login(oauth_path: str = "auth/oauth2yahoo.json", force_reauth: bool = False) -> OAuth2:
    """
    Load OAuth credentials and refresh the token if necessary.

    Set force_reauth=True to discard the cached token and do a full browser
    re-authorization. Use this when API calls return 403 despite a valid token
    (indicates the token lacks the fantasy-sports scope).
    """
    logging.getLogger("yahoo_oauth").setLevel(logging.WARNING)

    p = Path(oauth_path)

    # Ensure the scope is always present in the credentials file so yahoo_oauth
    # requests fspt-r during the authorization URL step.
    if p.exists():
        creds = json.loads(p.read_text())
        if creds.get("scope") != YAHOO_SCOPE:
            creds["scope"] = YAHOO_SCOPE
            p.write_text(json.dumps(creds, indent=4))

    if force_reauth:
        # Clear token fields — yahoo_oauth will trigger a fresh browser auth flow.
        creds = json.loads(p.read_text())
        for key in ("access_token", "refresh_token", "token_time", "token_type"):
            creds.pop(key, None)
        p.write_text(json.dumps(creds, indent=4))
        log.info("Token cleared — a browser auth flow will start now.")

    oauth = OAuth2(None, None, from_file=oauth_path, scope=YAHOO_SCOPE)
    if not oauth.token_is_valid():
        oauth.refresh_access_token()
    return oauth


def _get(oauth: OAuth2, url: str, params: Optional[dict] = None) -> dict:
    """Make an authenticated GET and return parsed JSON."""
    p = {"format": "json"}
    if params:
        p.update(params)
    resp = oauth.session.get(url, params=p)
    if not resp.ok:
        # Surface Yahoo's error message before raising so we can diagnose
        try:
            body = resp.json()
            msg  = body.get("error", {}).get("description", resp.text[:300])
        except Exception:
            msg = resp.text[:300]
        log.error("Yahoo API %d on %s — %s", resp.status_code, url, msg)
    resp.raise_for_status()
    return resp.json()


# ─── Game key ────────────────────────────────────────────────────────────────

def get_nfl_game_key(oauth: OAuth2, season: int) -> str:
    """
    Return the Yahoo game key (e.g. '449') for the given NFL season year.

    Strategy (most reliable first):
    1. Look through the authenticated user's own game history — always accessible.
    2. Fall back to /game/nfl for the current season.
    """
    # 1. User's game history (works for any season the user participated in)
    try:
        url  = f"{BASE}/users;use_login=1/games;game_codes=nfl"
        data = _get(oauth, url)
        user_block = data["fantasy_content"]["users"]["0"]["user"]
        games      = user_block[1]["games"]
        for i in range(games["count"]):
            g = games[str(i)]["game"][0]
            if str(g.get("season")) == str(season):
                key = g["game_key"]
                log.info("NFL game key for %s: %s (from user history)", season, key)
                return key
    except Exception as exc:
        log.warning("users/games lookup failed: %s", exc)

    # 2. Current-season shortcut
    try:
        url  = f"{BASE}/game/nfl"
        data = _get(oauth, url)
        g    = data["fantasy_content"]["game"][0]
        if str(g.get("season")) == str(season):
            key = g["game_key"]
            log.info("NFL game key for %s: %s (from /game/nfl)", season, key)
            return key
    except Exception as exc:
        log.warning("/game/nfl lookup failed: %s", exc)

    raise ValueError(
        f"Could not find NFL game key for season {season}. "
        "Make sure the authenticated account was in an NFL fantasy league that season."
    )


# ─── League settings / scoring rules ─────────────────────────────────────────

def get_scoring_settings(oauth: OAuth2, game_key: str, league_id: str) -> dict:
    """
    Return a dict mapping our internal stat names to points-per-unit.
    e.g. {'pass_yards': 0.04, 'pass_td': 4.0, 'receptions': 1.0, ...}
    """
    url = f"{BASE}/league/{game_key}.l.{league_id}/settings"
    data = _get(oauth, url)
    stat_mods = (
        data["fantasy_content"]["league"][1]["settings"][0]["stat_modifiers"]["stats"]
    )

    rules: dict = {}
    for item in stat_mods:
        s = item.get("stat", {})
        stat_id = str(s.get("stat_id", ""))
        value   = float(s.get("value", 0))
        name    = STAT_ID_MAP.get(stat_id)
        if name and value != 0:
            rules[name] = value

    log.info("Loaded %d scoring rules from league %s", len(rules), league_id)
    return rules


# ─── League matchup schedule ──────────────────────────────────────────────────

def get_weekly_matchups(oauth: OAuth2, game_key: str, league_id: str, week: int) -> list:
    """
    Return the list of matchups for a week:
    [{"team1_id": "1", "team1_name": "...", "team1_score": 134.5,
      "team2_id": "2", "team2_name": "...", "team2_score": 121.2,
      "week_start": "2025-11-06", "week_end": "2025-11-10"}, ...]
    """
    url = f"{BASE}/league/{game_key}.l.{league_id}/scoreboard;week={week}"
    data = _get(oauth, url)
    scoreboard = data["fantasy_content"]["league"][1]["scoreboard"]["0"]["matchups"]
    num_matchups = scoreboard["count"]

    matchups = []
    for i in range(num_matchups):
        m = scoreboard[str(i)]["matchup"]
        week_start = m.get("week_start", "")
        week_end   = m.get("week_end", "")
        teams_raw  = m["0"]["teams"]

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
            "team1_id": t1["team_id"], "team1_name": t1["team_name"], "team1_score": t1["score"],
            "team2_id": t2["team_id"], "team2_name": t2["team_name"], "team2_score": t2["score"],
            "week_start": week_start, "week_end": week_end,
        })

    return matchups


# ─── Team logos ──────────────────────────────────────────────────────────────

def get_team_logos(oauth: OAuth2, game_key: str, league_id: str) -> dict:
    """
    Return a dict mapping team_id (str) -> logo URL (str) for all teams
    in the league, using the /league/teams endpoint which reliably includes
    team_logos metadata.
    """
    url = f"{BASE}/league/{game_key}.l.{league_id}/teams"
    data = _get(oauth, url)
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
        import logging
        logging.getLogger(__name__).warning("Could not parse team logos: %s", exc)
    return logos


# ─── Roster (starters + bench) ───────────────────────────────────────────────

BENCH_POSITIONS = {"BN", "IR"}

def get_roster(oauth: OAuth2, game_key: str, league_id: str,
               team_id: str, week: int) -> dict:
    """
    Return roster for one team in one week.
    {
      "team_id": "3",
      "team_name": "Crab Legolas",
      "starters": [{"name": "Patrick Mahomes", "position": "QB",
                    "nfl_team": "KC", "player_id": "..."},  ...],
      "bench":    [...]
    }
    """
    url = f"{BASE}/team/{game_key}.l.{league_id}.t.{team_id}/roster;week={week}"
    data = _get(oauth, url)
    team_data = data["fantasy_content"]["team"]

    team_name = ""
    team_logo_url = ""
    for item in team_data[0]:
        if "name" in item:
            team_name = item["name"]
        if "team_logos" in item:
            try:
                team_logo_url = item["team_logos"][0]["team_logo"]["url"]
            except (IndexError, KeyError, TypeError):
                pass

    players_raw = team_data[1]["roster"]["0"]["players"]
    starters, bench = [], []

    for i in range(players_raw["count"]):
        p = players_raw[str(i)]["player"]
        meta   = p[0]
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
        "team_id":       team_id,
        "team_name":     team_name,
        "team_logo_url": team_logo_url,
        "starters":      starters,
        "bench":         bench,
    }
