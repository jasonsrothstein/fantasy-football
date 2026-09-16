"""
lib/espn.py  –  ESPN public API client for NFL play-by-play.

No authentication required. Fetches game schedules and per-game plays with
real-world wallclock timestamps. All functions return plain dicts/lists so
callers can cache them directly as JSON.
"""

import logging
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/",
    "Origin": "https://www.espn.com",
})

# site.api.espn.com has been returning 403 since mid-2026; the functionally
# identical site.web.api.espn.com endpoint still works.
SCOREBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
SUMMARY_URL    = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/summary"


# ─── Game schedule ────────────────────────────────────────────────────────────

def get_week_games(season: int, week: int) -> list:
    """
    Return all NFL games for a given season and regular-season week.
    Each entry: {"game_id": "401671803", "home": "KC", "away": "BAL",
                 "home_score": 27, "away_score": 20,
                 "date": "2025-11-06T20:20:00Z", "status": "final"}
    """
    params = {"seasontype": 2, "week": week, "year": season, "limit": 20}
    resp = SESSION.get(SCOREBOARD_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    games = []
    for event in data.get("events", []):
        comp   = event.get("competitions", [{}])[0]
        status = comp.get("status", {}).get("type", {})
        teams  = {}
        scores = {}
        for t in comp.get("competitors", []):
            side = t["homeAway"]
            teams[side]  = t.get("team", {}).get("abbreviation", "")
            try:
                scores[side] = int(t.get("score", 0) or 0)
            except (ValueError, TypeError):
                scores[side] = 0
        games.append({
            "game_id":    event["id"],
            "home":       teams.get("home", ""),
            "away":       teams.get("away", ""),
            "home_score": scores.get("home", 0),
            "away_score": scores.get("away", 0),
            "date":       event.get("date", ""),
            "status":     status.get("name", ""),
        })

    log.info("Week %d/%d: %d games found", season, week, len(games))
    return games


# ─── Play-by-play ─────────────────────────────────────────────────────────────

def get_game_plays(game_id: str) -> list:
    """
    Return all plays for one game, each with a wallclock timestamp.
    Each entry:
    {
      "wallclock":    "2025-11-06T20:28:15Z",  # UTC; None if missing
      "period":       1,
      "clock":        "14:52",
      "play_type":    "Rush",                  # ESPN type text
      "yards":        8,
      "athletes":     [{"id": "...", "name": "Christian McCaffrey", "role": null}],
      "is_scoring":   False,
      "scoring_type": None,                    # "TD", "FG", "PAT", "Safety" etc.
      "play_text":    "C.McCaffrey rush for 8 yards",
    }
    """
    resp = SESSION.get(SUMMARY_URL, params={"event": game_id}, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    plays = []

    # Primary source: drives.previous[] → plays[]
    drives = data.get("drives", {})
    drive_list = drives.get("previous", [])
    if not drive_list:
        # Some games use a flat 'plays' key at the top level
        drive_list = [{"plays": data.get("plays", [])}]

    for drive in drive_list:
        for raw in drive.get("plays", []):
            p = _parse_play(raw)
            if p:
                plays.append(p)

    log.debug("Game %s: %d raw plays parsed", game_id, len(plays))
    return plays


def _parse_play(raw: dict) -> Optional[dict]:
    """Normalise one ESPN play dict into our flat schema."""
    wc = raw.get("wallclock")  # may be None for some plays

    play_type_obj = raw.get("type") or {}
    play_type = play_type_obj.get("text", "") or ""
    yards     = raw.get("statYardage") or 0

    # Athletes involved — older API provided `athletesInvolved` with full names;
    # newer web API omits it, so fall back to extracting abbreviated names from
    # the play description text (e.g. "J.Williams", "P.Mahomes").
    athletes = []
    for a in raw.get("athletesInvolved", []):
        athletes.append({
            "id":   a.get("id", ""),
            "name": a.get("fullName") or a.get("displayName") or "",
            "role": a.get("type", {}).get("text") if isinstance(a.get("type"), dict) else None,
        })

    if not athletes:
        play_text = raw.get("text", "")
        for name in _extract_players_from_text(play_text):
            athletes.append({"id": "", "name": name, "role": None})

    # Scoring metadata
    sc = raw.get("scoringType") or {}
    is_scoring   = bool(sc)
    scoring_abbr = sc.get("abbreviation", "") if sc else ""  # "TD", "FG", "PAT", "SF"

    return {
        "wallclock":    wc,
        "period":       raw.get("period", {}).get("number", 0),
        "clock":        raw.get("clock", {}).get("displayValue", ""),
        "play_type":    play_type,
        "yards":        int(yards),
        "athletes":     athletes,
        "is_scoring":   is_scoring,
        "scoring_type": scoring_abbr or None,
        "play_text":    raw.get("text", ""),
    }


# ─── Play → fantasy events ────────────────────────────────────────────────────
# Each "event" is one atomic scoring contribution:
# {"wallclock": "...", "player_name": "...", "stat": "rush_yards", "value": 8}

def extract_fantasy_events(play: dict) -> list:
    """
    Convert one normalised play into zero or more fantasy-scoring events.
    Events with unknown players (empty name) are discarded.
    """
    wc       = play["wallclock"]
    ptype    = play["play_type"].lower()
    yards    = play["yards"]
    athletes = play["athletes"]
    text     = play["play_text"].lower()
    scoring  = play["scoring_type"]  # "TD", "FG", "PAT", "SF", None

    events = []

    def evt(name, stat, value):
        if name:
            events.append({"wallclock": wc, "player_name": name,
                            "stat": stat, "value": value})

    # ── Rush plays ────────────────────────────────────────────────────────────
    if "rush" in ptype or "scramble" in ptype:
        rusher = _get_player(athletes, 0)
        # When a lineman/FB is "reported in as eligible", ESPN puts that player
        # at athletes[0] and shifts the actual ball carrier to athletes[1].
        # Detect this and extract the real rusher from the play text instead.
        if "reported in as eligible" in play["play_text"].lower():
            m = re.search(r'eligible\.\s+([A-Z]\.[\w\'-]+)', play["play_text"])
            if m:
                rusher = m.group(1)
            else:
                rusher = _get_player(athletes, 1) or rusher
        # statYardage can be negative when an offensive penalty nullifies or
        # reverses a gain (e.g. holding).  In that case official NFL stats
        # credit the rusher with 0 yards (the play is wiped out).
        rush_yards = 0 if yards < 0 and "penalty" in play["play_text"].lower() else yards
        if rush_yards != 0:
            evt(rusher, "rush_yards", rush_yards)
        if scoring == "TD" or "touchdown" in ptype:
            evt(rusher, "rush_td", 1)
            if rush_yards >= 40:
                evt(rusher, "rush_td_40plus", 1)   # +2 bonus for 40+ yd rushing TD
            # PAT / extra-point that follows a rushing TD is embedded in this play
            _emit_pat(athletes, 1, play["play_text"], events, wc, rusher)
        if "fumble" in text and ("lost" in text or "recovered by" in text):
            evt(rusher, "fumble_lost", 1)

    # ── Pass plays ────────────────────────────────────────────────────────────
    elif "pass" in ptype:
        if "reported in as eligible" in play["play_text"].lower():
            # athletes[0] = eligible reporter (lineman); extract passer/receiver
            # from the play text for reliability regardless of whether the
            # eligible player is a decoy or the actual receiver.
            m = re.search(
                r'eligible\.\s+([A-Z]\.[\w\'-]+)\s+pass\s+\S+\s+\S+\s+to\s+([A-Z]\.[\w\'-]+)',
                play["play_text"])
            if m:
                passer, receiver = m.group(1), m.group(2)
            else:
                passer   = _get_player(athletes, 1) or _get_player(athletes, 0)
                receiver = _get_player(athletes, 2) or _get_player(athletes, 1)
        else:
            passer   = _get_player(athletes, 0)
            receiver = _get_player(athletes, 1)

        if "incomplete" in ptype or "incomplete" in text:
            pass  # no stats
        elif "interception" in ptype or "interception" in text:
            evt(passer, "int_thrown", 1)
        else:
            # Complete pass — when statYardage is negative due to a penalty,
            # the play is officially wiped out so credit 0 yards.
            pass_yards = 0 if yards < 0 and "penalty" in play["play_text"].lower() else yards
            if pass_yards != 0:
                evt(passer,   "pass_yards", pass_yards)
                evt(receiver, "rec_yards",  pass_yards)
            evt(receiver, "receptions", 1)
            if scoring == "TD" or "touchdown" in ptype:
                evt(passer,   "pass_td", 1)
                evt(receiver, "rec_td",  1)
                if pass_yards >= 40:
                    evt(passer,   "pass_td_40plus", 1)  # +1 bonus for passer
                    evt(receiver, "rec_td_40plus",  1)  # +2 bonus for receiver
                # PAT embedded in passing TD play (kicker at athletes[2])
                _emit_pat(athletes, 2, play["play_text"], events, wc, passer)
            if "fumble" in text and "lost" in text:
                evt(receiver, "fumble_lost", 1)

    # ── 2-point conversion ────────────────────────────────────────────────────
    elif "two-point" in ptype or "2pt" in ptype or "two point" in ptype:
        if "conversion" in text and ("pass" in text or "run" in text):
            for a in athletes:
                if a["name"]:
                    evt(a["name"], "two_pt_conversion", 1)

    # ── Field goals ───────────────────────────────────────────────────────────
    elif "field goal" in ptype:
        kicker = _get_player(athletes, 0)
        if scoring == "FG":
            # Bucket the distance
            bucket = _fg_bucket(yards, made=True)
            evt(kicker, bucket, 1)
        else:
            bucket = _fg_bucket(yards, made=False)
            evt(kicker, bucket, 1)

    # ── Extra point ───────────────────────────────────────────────────────────
    elif "extra point" in ptype or "pat" in ptype:
        kicker = _get_player(athletes, 0)
        if scoring == "PAT":
            evt(kicker, "pat_made", 1)
        else:
            evt(kicker, "pat_missed", 1)

    # ── Defensive events ──────────────────────────────────────────────────────
    # For all team-defense events we emit player_name="__DEFENSE__" with a
    # _off_player hint (the offensive player involved).  build_timelines.py
    # looks up which NFL team that player is on, then assigns the stat to the
    # OTHER team's DEF fantasy starter.

    elif "sack" in ptype or ("sack" in text
                              and "rush" not in ptype
                              and "pass" not in ptype):
        # athletes[0] is the QB being sacked (OFFENSE), not the sacker.
        # Give sack credit to the opposing team's defense, NOT the QB.
        qb_name = _get_player(athletes, 0)
        # Also extract the sacker's name from the parenthetical in the play text
        # e.g. "...sacked for -4 yards (A.Highsmith)" → sacker = "A.Highsmith"
        sacker_m = re.search(r'\(([A-Z](?:\.[A-Z])?\.[\w\'-]+)', play["play_text"])
        sacker = sacker_m.group(1) if sacker_m else ""
        # Extract yard-line team & value: "at NYJ 12" → ("NYJ", 12)
        # When yards are small (≤15) the sack happened near that team's end zone;
        # the DEFENSE is the team whose end zone is threatened, so the DEF team =
        # the team in the yard-line string.  For larger values the QB is being
        # sacked in their own deeper territory (team = OFFENSE).
        events.append({
            "wallclock":   wc, "player_name": "__DEFENSE__",
            "stat":        "def_sack", "value": 1,
            "_off_player": qb_name,  # QB being sacked (offensive team's player)
            "_def_player": sacker,   # player who made the sack (defensive team)
        })
        # Fumble on the sack (QB loses ball while being sacked)
        if "fumble" in text and qb_name:
            # "fumble recovery (own)" ptype → own team recovered, no fumble_lost
            if "own" not in ptype:
                evt(qb_name, "fumble_lost", 1)

    elif "interception" in ptype and athletes:
        # Defensive INT.  The intercetor is a defender; we tag this as a team
        # DEF stat and let build_timelines.py resolve which team intercepted
        # from the _off_player (the QB who threw it, usually athletes[1]).
        qb_name = _get_player(athletes, 1) or _get_player(athletes, 0)
        events.append({
            "wallclock": wc, "player_name": "__DEFENSE__",
            "stat": "def_int", "value": 1,
            "_off_player": qb_name,
        })

    elif "fumble" in ptype and "recovery" in ptype:
        # "fumble recovery (own)" → the offense recovered their own fumble; no score.
        # "fumble recovery" (without "own") → the defense recovered; DEF gets credit.
        if "own" not in ptype:
            # Identify the player who fumbled (offense) as hint
            fumbler = _get_player(athletes, 0)
            events.append({
                "wallclock": wc, "player_name": "__DEFENSE__",
                "stat": "def_fumble_rec", "value": 1,
                "_off_player": fumbler,
            })
            # The fumbling player loses the ball
            if fumbler:
                evt(fumbler, "fumble_lost", 1)

    elif "safety" in ptype:
        events.append({
            "wallclock": wc, "player_name": "__DEFENSE__",
            "stat": "def_safety", "value": 1,
            "_off_player": _get_player(athletes, 0),
        })

    elif scoring == "TD" and any("return" in (a.get("role") or "").lower() for a in athletes):
        # Kick/punt return TD → team DEF/ST scores
        returner = _get_player(athletes, 0)
        events.append({
            "wallclock": wc, "player_name": "__DEFENSE__",
            "stat": "def_return_td", "value": 1,
            "_off_player": "",           # no opposing player hint needed
            "_ret_player": returner,     # the player who returned it (for team lookup)
        })

    # ── Kickoff / punt fumble recovery by the kicking team ────────────────────
    # ESPN records these as a single "Kickoff" or "Punt" play rather than a
    # separate "Fumble Recovery" event.  Detect them from play text patterns like:
    #   "C.Boswell kicks N yds from PIT 35 ... X.Gipson FUMBLES, RECOVERED by PIT-..."
    # Only credit the DEF when the KICKING team recovers (not when the return
    # team recovers their own fumble).
    if "kickoff" in ptype or "punt" in ptype:
        if "fumbl" in text and "recovered" in text.lower():
            rec_m = re.search(r'RECOVERED? by ([A-Z]+)-', play["play_text"],
                               re.IGNORECASE)
            # Determine the kicking team from "kicks/punts from {TEAM} {N}"
            kick_from_m = re.search(r'from ([A-Z]+) \d+', play["play_text"])
            if rec_m and kick_from_m:
                rec_team  = rec_m.group(1).upper()
                kick_team = kick_from_m.group(1).upper()
                # Only emit DEF credit when the kicking team recovers the fumble
                # (i.e. the receiving/return team fumbled).
                if rec_team == kick_team:
                    fum_m = re.search(
                        r'FUMBLES?\s+\(([A-Z]\.[\w\'-]+)\)', play["play_text"])
                    fumbler = fum_m.group(1) if fum_m else ""
                    events.append({
                        "wallclock":         wc, "player_name": "__DEFENSE__",
                        "stat":              "def_fumble_rec", "value": 1,
                        "_off_player":       fumbler,   # player who fumbled (return team)
                        "_explicit_def_team": rec_team, # directly named in play text
                    })

    return events


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _extract_players_from_text(text: str) -> list:
    """
    Extract abbreviated player name tokens from ESPN play description text.
    Handles standard NFL formats: 'J.Williams', 'C.McCaffrey', 'T.Y.Hilton',
    'A.J.Brown', 'D.K.Metcalf'.
    Returns names in order of appearance; first is typically the ball carrier
    or passer, second is the receiver.
    """
    # Match: optional second initial + dot (T.Y., A.J., D.K.) then Capitalized word
    # that may include embedded caps (McCaffrey) or hyphens (St.Brown handled separately)
    return re.findall(r'\b[A-Z](?:\.[A-Z])?\.(?:[A-Z][A-Za-z\'-]+)\b', text)


def _get_player(athletes: list, idx: int) -> str:
    if idx < len(athletes):
        return athletes[idx].get("name", "")
    return ""


def _fg_bucket(yards: int, made: bool) -> str:
    prefix = "fg" if made else "fg_miss"
    if yards < 20:  return f"{prefix}_0_19"
    if yards < 30:  return f"{prefix}_20_29"
    if yards < 40:  return f"{prefix}_30_39"
    if yards < 50:  return f"{prefix}_40_49"
    return f"{prefix}_50_plus"


def _yards_from_text(text: str) -> int:
    """
    Parse the first 'for N yards' value from a play description.
    Returns 0 if not found.  Handles negatives ("for -3 yards").
    """
    m = re.search(r'for (-?\d+) yards?', text, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _emit_pat(athletes: list, kicker_idx: int, text: str,
              events: list, wc, scorer_name: str) -> None:
    """
    Emit a pat_made or pat_missed event for the kicker embedded in a TD play.

    The kicker name is taken from athletes[kicker_idx] when present, or parsed
    from the play text ("X.Lastname extra point is GOOD").

    A ``_td_scorer`` hint is attached so build_timelines.py can fall back to
    team-based kicker matching when the kicker isn't on any fantasy roster.
    """
    if "extra point" not in text.lower():
        return  # no PAT on this play (e.g. 2-point conversion)

    # Always prefer text extraction — athletes[kicker_idx] can be wrong when a
    # "reported in as eligible" play shifts all athlete indices.
    m = re.search(r'([A-Z]\.[A-Za-z\'-]+)\s+extra point', text)
    kicker = m.group(1) if m else _get_player(athletes, kicker_idx)

    if not kicker:
        return

    made = "extra point is good" in text.lower() or "extra point is no good" not in text.lower()
    # Confirm explicitly
    if "extra point is no good" in text.lower() or "extra point failed" in text.lower():
        made = False
    elif "extra point is good" in text.lower():
        made = True

    stat = "pat_made" if made else "pat_missed"
    events.append({
        "wallclock":   wc,
        "player_name": kicker,
        "stat":        stat,
        "value":       1,
        "_td_scorer":  scorer_name,   # hint for team-based matching fallback
    })


