#!/usr/bin/env python3
"""
build_timelines.py  –  Reconstruct fantasy points-over-time from cached data.

Reads Yahoo roster/matchup data and NFL play-by-play (nflverse by default,
ESPN cache as fallback) and writes one timeline JSON per week for generate_html.

Algorithm
---------
1. Load the week's matchup pairings (which teams play each other).
2. Load each team's active starters from the cached Yahoo roster.
3. Fetch all fantasy events for every NFL game that week (nflverse or ESPN).
4. For each event, identify which starter was involved and score it.
5. Sort all scoring events by wallclock time, accumulate per-team points.
6. Add defense points-allowed bonus at each game's final wallclock timestamp.
7. Write a timeline JSON with the exact same structure that generate_html.py
   already understands.

Usage
-----
    python Scripts/build_timelines.py --season 2025
    python Scripts/build_timelines.py --season 2025 --week 10
    python Scripts/build_timelines.py --season 2025 --week 10 --source espn
    python Scripts/build_timelines.py --season 2025 --week 10 --verbose
"""

import argparse
import bisect
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_ET     = ZoneInfo("America/New_York")
_MINUTE = timedelta(minutes=1)
_GAP    = timedelta(minutes=25)   # dead-time threshold between game windows


def _build_minute_grid(sorted_event_times: list) -> list:
    """Return a 1-minute-resolution datetime grid over all active game windows.

    Consecutive events separated by more than _GAP are treated as different
    windows (Thursday night vs Sunday, Sunday vs Monday). Dead time between
    windows is excluded so the x-axis stays compressed.
    """
    if not sorted_event_times:
        return []

    def floor_min(dt):
        return dt.replace(second=0, microsecond=0)

    # Identify contiguous game windows
    windows: list = []
    ws = we = sorted_event_times[0]
    for t in sorted_event_times[1:]:
        if t - we <= _GAP:
            we = t
        else:
            windows.append((ws, we))
            ws = we = t
    windows.append((ws, we))

    # Expand each window into 1-minute ticks
    grid: list = []
    for ws, we in windows:
        cur = floor_min(ws)
        end = floor_min(we) + _MINUTE
        while cur <= end:
            grid.append(cur)
            cur += _MINUTE

    return grid


def _count_windows(sorted_event_times: list) -> int:
    if not sorted_event_times:
        return 0
    count = 1
    for i in range(1, len(sorted_event_times)):
        if sorted_event_times[i] - sorted_event_times[i - 1] > _GAP:
            count += 1
    return count
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from lib import espn as espn_lib
from lib import nflverse as nv_lib
from lib.scoring import (
    DEFAULT_RULES, merge_with_defaults, normalize_name,
    espn_abbrev_key, yahoo_abbrev_key, normalize_nfl_team,
    score_event, compute_pts_from_stats, pts_allowed_bonus, yards_allowed_bonus,
)

log = logging.getLogger(__name__)


# ─── Path helpers (mirror fetch_season.py) ────────────────────────────────────

def data_root(season: int) -> Path:
    return _HERE.parent / "Data" / str(season)

def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))

def _save(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")

def timeline_path(season: int, week: int) -> Path:
    return data_root(season) / "timelines" / f"week_{week:02d}.json"


# ─── Load cached data ─────────────────────────────────────────────────────────

def load_scoring_rules(season: int) -> dict:  # noqa: ARG001
    """
    Return the hardcoded league scoring rules.

    The Yahoo scoring API stat-ID mapping produces corrupt values (e.g.
    receptions=6.0, rec_yards=2.0) so we bypass it entirely and use the
    exact rules provided by the league commissioner, which are constant
    across all seasons of this league.
    """
    log.info("Using hardcoded league scoring rules (Yahoo API mapping bypassed)")
    return dict(DEFAULT_RULES)


def load_rosters_for_week(season: int, week: int, num_teams: int) -> dict:
    """Return {team_id_str: roster_dict} for all teams in the week."""
    rosters = {}
    for tid in range(1, num_teams + 1):
        p = data_root(season) / "rosters" / f"week_{week:02d}" / f"team_{tid:02d}.json"
        if p.exists():
            rosters[str(tid)] = _load(p)
        else:
            log.warning("Missing roster: %s", p)
    return rosters


def load_week_plays_espn(season: int, week: int) -> list:
    """Load all cached ESPN plays for a week (ESPN source only)."""
    pbp_dir = data_root(season) / "pbp" / f"week_{week:02d}"
    all_plays = []
    if not pbp_dir.exists():
        log.warning("No PbP directory for week %d", week)
        return []
    for f in pbp_dir.glob("*.json"):
        plays = _load(f)
        for p in plays:
            p["_game_id"] = f.stem
        all_plays.extend(plays)
    return all_plays


def load_schedule_week(season: int, week: int) -> list:
    """Load game schedule from cached ESPN schedule.json (ESPN source only)."""
    p = data_root(season) / "schedule.json"
    if not p.exists():
        return []
    return _load(p).get(str(week), [])


def load_week_data(season: int, week: int, source: str = "nflverse") -> tuple:
    """
    Return (events, schedule) for a given week and data source.

    events   – list of pre-extracted fantasy-event dicts (wallclock, player, stat, value …)
    schedule – list of game dicts (game_id, home, away, home_score, away_score, date)

    For source='nflverse': fetches fresh data via nflreadpy (cached after first download).
    For source='espn':     reads from the JSON files written by fetch_season.py.
    """
    if source == "nflverse":
        schedule = nv_lib.get_week_games(season, week)
        events   = nv_lib.get_week_events(season, week)
        log.info("nflverse  week %d: %d games, %d events", week, len(schedule), len(events))
        return events, schedule

    # ESPN fallback: extract events from raw PBP plays
    schedule  = load_schedule_week(season, week)
    all_plays = load_week_plays_espn(season, week)
    events: list = []
    for play in all_plays:
        wc = play.get("wallclock")
        if not wc:
            continue
        gid = play.get("_game_id", "")
        for ev in espn_lib.extract_fantasy_events(play):
            ev["wallclock"] = wc
            ev["_game_id"]  = gid
            events.append(ev)
    log.info("espn  week %d: %d plays → %d events", week, len(all_plays), len(events))
    return events, schedule


# ─── Core reconstruction ──────────────────────────────────────────────────────

def _parse_wc(wc: Optional[str]) -> Optional[datetime]:
    if not wc:
        return None
    try:
        return datetime.fromisoformat(wc.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def build_week_timeline(season: int, week: int, scoring_rules: dict,
                        num_teams: int = 12,
                        source: str = "nflverse") -> Optional[dict]:
    """
    Return a timeline dict ready for generate_html.py, or None if data is missing.
    """
    # ── Load inputs ───────────────────────────────────────────────────────────
    matchups_file = data_root(season) / "matchups.json"
    if not matchups_file.exists():
        log.error("matchups.json not found — run fetch_season.py first")
        return None

    all_matchups = _load(matchups_file)
    week_matchups = all_matchups.get(str(week), [])
    if not week_matchups:
        log.warning("No matchup data for week %d", week)
        return None

    rosters = load_rosters_for_week(season, week, num_teams)

    # Load team logos (fetched once per season by fetch_season.py)
    logos_path = data_root(season) / "team_logos.json"
    team_logos: dict = {}
    if logos_path.exists():
        team_logos = _load(logos_path)

    all_events, schedule = load_week_data(season, week, source)

    if not all_events:
        log.error("No play-by-play events for week %d. "
                  "For espn source run fetch_season.py; nflverse requires network.", week)
        return None

    # ── Build name → team_id indices ──────────────────────────────────────────
    # ESPN plays use abbreviated names ("J.Burrow") not full names ("Joe Burrow").
    # We build two indices:
    #   full_to_team  : normalized full name → team_id  (fallback for exact hits)
    #   abbrev_to_nfl : abbrev key → {nfl_team_upper → team_id}  (primary path)
    # Disambiguation: for a given abbreviated name we filter to the NFL teams
    # actually playing in the game, resolving most "J.Williams" collisions.
    #
    # A second index for team defenses:
    #   nfl_def_to_tid : NFL team abbr → fantasy tid whose DEF starter is that team

    full_to_team:    dict = {}                      # "joe burrow" → "1"
    abbrev_to_nfl:   dict = defaultdict(dict)       # "j burrow" → {"CIN": "1"}
    nfl_def_to_tid:  dict = {}                      # "PIT" → "3"  (team with PIT DEF)
    nfl_kick_to_tid: dict = {}                      # "WAS" → "5"  (team with WAS kicker)
    kick_tid_to_abbr: dict = {}                     # "5"   → "t bass"  (rostered kicker abbrev)

    # ── Load GSIS → Yahoo ID crosswalk ────────────────────────────────────────
    # Built by Scripts/build_player_id_map.py from nflreadpy.load_ff_playerids().
    # Maps GSIS player IDs (from nflverse PBP) → Yahoo player IDs.
    # Used as the primary resolution path to replace name-based matching.
    _id_map_path = Path(__file__).parent.parent / "Data" / "player_id_map.json"
    _gsis_to_yahoo: dict[str, str | None] = {}          # gsis_id → yahoo_id (str or None)
    _yahoo_to_tid: dict[str, str] = {}                  # yahoo_id → fantasy team_id (per week)
    if _id_map_path.exists():
        _raw_map = json.loads(_id_map_path.read_text())
        _gsis_to_yahoo = {k: v.get("yahoo_id") for k, v in _raw_map.items()}
    else:
        log.warning("player_id_map.json not found — run Scripts/build_player_id_map.py for best accuracy")

    for tid, roster in rosters.items():
        for player in roster.get("starters", []):
            pname    = player.get("name", "")
            nfl      = player.get("nfl_team", "").upper()
            pos      = player.get("position", "")
            yahoo_id = str(player.get("player_id", "") or "")

            # Populate Yahoo ID → fantasy team map (starters only earn pts)
            if yahoo_id:
                _yahoo_to_tid[yahoo_id] = tid

            full_nn = normalize_name(pname)
            if full_nn:
                full_to_team[full_nn] = tid

            abbr = yahoo_abbrev_key(pname)
            if abbr and nfl:
                abbrev_to_nfl[abbr][nfl] = tid
                # Also index by short initial (e.g. "a brown" for "aj brown")
                # ESPN drops compound initials: "A.J. Brown" → "A.Brown"
                parts = abbr.split()
                if parts and len(parts[0]) > 1:
                    short = parts[0][0] + " " + " ".join(parts[1:])
                    abbrev_to_nfl[short].setdefault(nfl, tid)

            if pos == "DEF" and nfl:
                nfl_def_to_tid[nfl] = tid
            elif pos == "K" and nfl:
                nfl_kick_to_tid[nfl] = tid
                kick_tid_to_abbr[tid] = yahoo_abbrev_key(pname)  # for name verification

        # Index bench players for NFL-team lookups (they don't earn fantasy pts
        # but their team affiliation is needed to correctly attribute defensive
        # events — e.g. knowing A.Rodgers is on PIT means a sack of him goes to
        # the opposing team's DEF, not PIT's).
        for player in roster.get("bench", []):
            pname = player.get("name", "")
            nfl   = player.get("nfl_team", "").upper()
            if not (pname and nfl):
                continue
            abbr = yahoo_abbrev_key(pname)
            if abbr:
                # Use tid=None sentinel so _nfl_team_of can find the NFL team
                # without treating the player as a scorable starter.
                abbrev_to_nfl[abbr].setdefault(nfl, None)
                parts = abbr.split()
                if parts and len(parts[0]) > 1:
                    short = parts[0][0] + " " + " ".join(parts[1:])
                    abbrev_to_nfl[short].setdefault(nfl, None)
            full_nn = normalize_name(pname)
            if full_nn and full_nn not in full_to_team:
                # Store a special sentinel so NFL team can be found without
                # accidentally routing stats to a fantasy team.
                full_to_team[full_nn] = None

    # Pre-build game → NFL teams mapping for fast per-play lookup.
    # Normalise ESPN abbreviations (WSH→WAS, JAC→JAX etc.) so they match the
    # Yahoo team abbreviations stored in roster entries.
    game_nfl_teams: dict = {}   # game_id → {"home": "PHI", "away": "DAL"}
    for g in schedule:
        game_nfl_teams[g["game_id"]] = {
            "home": normalize_nfl_team(g.get("home", "")),
            "away": normalize_nfl_team(g.get("away", "")),
        }

    # ── Stat-accumulation approach ─────────────────────────────────────────────
    # For each (tid, game_id, player_name) triple we maintain a running stat
    # sheet.  After every play we recompute points-from-stats (which naturally
    # handles yardage milestone bonuses) and emit only the delta to team_events.
    # This means:
    #  • milestones fire at the exact play they are crossed
    #  • no separate milestone-bonus pass needed
    #  • defensive team-play credits (sacks, INTs, etc.) handled here too
    #
    # IMPORTANT: the key includes player_name so that two starters from the
    # same NFL team on the same fantasy roster (e.g. QB + RB both from NYG)
    # have separate stat buckets.  Without this, their stats are combined and
    # milestone bonuses can fire incorrectly (e.g. QB 66 rush yds + RB 71 rush
    # yds = 137 combined → spurious rush_yards_100 bonus).

    player_stats: dict = defaultdict(lambda: defaultdict(float))
    # player_stats[(tid, gid, player_name)][stat_name] = cumulative value

    player_pts: dict = defaultdict(float)
    # player_pts[(tid, gid, player_name)] = current fantasy pts from accumulated stats

    team_events: dict = defaultdict(list)
    unmatched_names: set = set()

    # TD stats that should produce a dot marker on the timeline graph.
    _TD_STATS = frozenset({
        "rush_td", "rec_td", "pass_td",
        "def_td", "def_return_td", "return_td", "off_fumble_ret_td",
    })
    # td_events[tid] = [(wc_dt, display_name, stat, desc), ...]
    td_events: dict = defaultdict(list)

    def _resolve_tid(player_name: str, game_nfl: set,
                     nfl_team_hint: str = "",
                     player_id: str = "") -> Optional[str]:
        """Return the fantasy team_id for a player, or None.

        Resolution priority
        -------------------
        1. GSIS player ID (player_id) → Yahoo ID → fantasy team.
           This is unambiguous: if the GSIS ID maps to a Yahoo ID that is on
           some fantasy team's starter list this week, return that team.
           If the GSIS ID is known but has no Yahoo ID (player not rostered in
           any fantasy league), return None immediately — no trade fallback.
           This eliminates false positives from same-name players (e.g. Kyle
           Williams NE vs Kyren Williams LAR).

        2. nfl_team_hint exact match (nflverse events without a GSIS hit).
           Direct lookup of abbrev_key in the specific NFL team's roster entry.

        3. game_nfl filter + trade fallback (ESPN or GSIS-less events).
           Classic disambiguation by which teams are playing this game, with a
           fallback for mid-season trades where the roster team differs from
           the playing team.
        """
        # ── Path 1: GSIS player ID ────────────────────────────────────────────
        _gsis_known = False   # True when this GSIS ID is in the crosswalk
        if player_id and _gsis_to_yahoo:
            if player_id in _gsis_to_yahoo:
                _gsis_known = True
                yahoo_id = _gsis_to_yahoo[player_id]
                if yahoo_id is not None:
                    tid = _yahoo_to_tid.get(yahoo_id)
                    if tid is not None:
                        return tid
                    # The Yahoo ID is valid but this player is not a starter
                    # in any roster this week (benched or small ID mismatch
                    # between ff_playerids and our Yahoo API data).
                    # Fall through to name matching.
                # else: yahoo_id is None — player is in crosswalk but has no
                # Yahoo ID (e.g. 2025 rookie not yet in the ff_playerids
                # dataset, or an unrostered player like an NFL-only WR).
                # Fall through to name matching below, but use strict mode:
                # the GSIS ID confirms which player this is, so we apply the
                # team hint exactly and skip the trade fallback.
            # GSIS ID not in our crosswalk — fall through to name matching.

        # ── Path 2: name-based resolution ────────────────────────────────────
        abbr = espn_abbrev_key(player_name)
        if abbr:
            nfl_opts = abbrev_to_nfl.get(abbr, {})

            # Fast path: exact team hint (nflverse _nfl_team).
            if nfl_team_hint:
                t = nfl_opts.get(nfl_team_hint)
                if t is not None:
                    return t
                # Hint didn't match — could be trade or different player.
                # If the GSIS ID is known (player confirmed in crosswalk),
                # skip the trade fallback to avoid false positives: we know
                # exactly which player this is, so a hint miss means they are
                # NOT a fantasy starter at their current NFL team.
                if _gsis_known:
                    return None
                # GSIS not known — fall through to game_nfl + trade fallback.

            if _gsis_known:
                # No team hint but GSIS confirmed: require a unique game match
                # only; skip trade fallback to prevent name-collision errors.
                matches = {nfl: t for nfl, t in nfl_opts.items() if nfl in game_nfl} \
                          if game_nfl else nfl_opts
                if len(matches) == 1:
                    return next(iter(matches.values()))
                return None

            # Standard path (no GSIS info): filter by game participants, then
            # trade fallback for mid-season trades where the roster team may
            # differ from the playing team.
            matches = {nfl: t for nfl, t in nfl_opts.items() if nfl in game_nfl} \
                      if game_nfl else nfl_opts
            if len(matches) == 1:
                return next(iter(matches.values()))
            if len(matches) > 1:
                log.debug("  Ambiguous %r game %s → %s", player_name, game_nfl, matches)
            # Trade fallback: roster may have wrong NFL team (mid-season trade).
            real_tids = {t for t in nfl_opts.values() if t is not None}
            if len(real_tids) == 1:
                return next(iter(real_tids))
        return full_to_team.get(normalize_name(player_name))

    def _nfl_team_of(player_name: str, game_nfl: set) -> Optional[str]:
        """Return the NFL team abbr for a player name (needed for DEF routing)."""
        abbr = espn_abbrev_key(player_name)
        if abbr:
            nfl_opts = abbrev_to_nfl.get(abbr, {})
            matches = {nfl: t for nfl, t in nfl_opts.items() if nfl in game_nfl} \
                      if game_nfl else nfl_opts
            if len(matches) == 1:
                return next(iter(matches.keys()))
        # Fallback: check full name in full_to_team to get tid, then scan roster
        nn = normalize_name(player_name)
        tid = full_to_team.get(nn)
        if tid and rosters.get(tid):
            for p in rosters[tid].get("starters", []):
                if normalize_name(p.get("name", "")) == nn:
                    return p.get("nfl_team", "").upper()
        return None

    _DEBUG_TID = None  # set to a team_id string to trace events for that team

    def _accum(tid: str, gid: str, stat: str, value: float, wc_dt,
               player_name: str = "") -> None:
        """Update one stat for a player; emit the fantasy-point delta to team_events."""
        key = (tid, gid, player_name)
        old_pts = player_pts[key]
        player_stats[key][stat] = player_stats[key].get(stat, 0.0) + value
        new_pts = compute_pts_from_stats(player_stats[key], scoring_rules)
        delta = round(new_pts - old_pts, 4)
        if delta != 0:
            team_events[tid].append((wc_dt, delta))
            if _DEBUG_TID is not None and tid == _DEBUG_TID and abs(delta) >= 0.01:
                import sys as _sys
                print(f"  [TDS] {player_name:<30} {stat:<22} val={value:+.2f}  delta={delta:+.4f}  running={round(new_pts,4)}  gid={gid}", file=_sys.stderr)
        player_pts[key] = new_pts

    # Track last wallclock per game_id for defense pts-allowed attribution
    last_wc_per_game: dict = {}

    for ev in all_events:
        wc_raw = ev.get("wallclock")
        if not wc_raw:
            continue
        wc_dt = _parse_wc(wc_raw)
        if not wc_dt:
            continue

        gid      = ev.get("_game_id", "")
        gteams   = game_nfl_teams.get(gid, {})
        game_nfl = {gteams.get("home", ""), gteams.get("away", "")} - {""}

        # Track the latest wallclock seen for each game (used for defense bonus)
        if gid and (gid not in last_wc_per_game or wc_dt > last_wc_per_game[gid]):
            last_wc_per_game[gid] = wc_dt

        player_name = ev["player_name"]
        stat        = ev.get("stat", "")
        value       = ev.get("value", 0)

        # ── Team-defense events ────────────────────────────────────────────
        if player_name == "__DEFENSE__":
            # nflverse events carry _def_team directly; ESPN events use
            # _explicit_def_team or require inference from _off_player.
            def_nfl: Optional[str] = normalize_nfl_team(
                ev.get("_def_team") or ev.get("_explicit_def_team") or ""
            )
            if def_nfl and def_nfl not in game_nfl:
                def_nfl = None   # sanity: must be a team actually in this game

            # ESPN fallback inference (off_player → opposite team is defense)
            if def_nfl is None:
                off_name   = ev.get("_off_player") or ""
                ret_name   = ev.get("_ret_player") or ""
                def_player = ev.get("_def_player") or ""

                if off_name and game_nfl:
                    off_nfl = _nfl_team_of(off_name, game_nfl)
                    if off_nfl and off_nfl in game_nfl:
                        others = game_nfl - {off_nfl}
                        if len(others) == 1:
                            def_nfl = next(iter(others))
                    if def_nfl is None and def_player:
                        dp_nfl = _nfl_team_of(def_player, game_nfl)
                        if dp_nfl and dp_nfl in game_nfl:
                            def_nfl = dp_nfl
                    if def_nfl is None:
                        log.debug("  DEF %s: off_player %r unresolvable, skipping",
                                  stat, off_name)
                        continue
                elif ret_name and game_nfl:
                    ret_nfl = _nfl_team_of(ret_name, game_nfl)
                    if ret_nfl and ret_nfl in game_nfl:
                        def_nfl = ret_nfl
                    if def_nfl is None:
                        for nfl in game_nfl:
                            if nfl_def_to_tid.get(nfl):
                                def_nfl = nfl
                                break
                elif game_nfl:
                    for nfl in game_nfl:
                        if nfl_def_to_tid.get(nfl):
                            def_nfl = nfl
                            break

            if def_nfl is None:
                log.debug("  DEF event %s in game %s — could not resolve team",
                          stat, gid)
                continue

            def_tid = nfl_def_to_tid.get(def_nfl)
            if def_tid is None:
                continue   # no fantasy team owns this defense

            _accum(def_tid, gid, stat, value, wc_dt,
                   player_name=f"DEF {def_nfl}")
            if stat in _TD_STATS:
                td_events[def_tid].append(
                    (wc_dt, f"DEF {def_nfl}", stat, ev.get("_desc", ""))
                )
            continue

        # ── Kicker events ─────────────────────────────────────────────────
        # nflverse kicker names come from structured columns so they match
        # directly.  For ESPN, we fall back to team-based matching.
        if stat in ("pat_made", "pat_missed", "fg_0_19", "fg_20_29",
                    "fg_30_39", "fg_40_49", "fg_50_plus",
                    "fg_miss_0_19", "fg_miss_20_29", "fg_miss_30_39",
                    "fg_miss_40_49", "fg_miss_50_plus"):
            nfl_hint  = normalize_nfl_team(ev.get("_nfl_team", ""))
            player_id = ev.get("_player_id", "")
            tid = _resolve_tid(player_name, game_nfl,
                               nfl_team_hint=nfl_hint, player_id=player_id)
            if tid is None:
                # ESPN fallback: use _td_scorer hint or team-based lookup
                scorer_name = ev.get("_td_scorer", "")
                if scorer_name:
                    scorer_nfl = _nfl_team_of(scorer_name, game_nfl)
                    if scorer_nfl:
                        tid = nfl_kick_to_tid.get(scorer_nfl)
                if tid is None:
                    # Try the event's _nfl_team hint (nflverse sets this)
                    nfl_hint = normalize_nfl_team(ev.get("_nfl_team", ""))
                    if nfl_hint:
                        candidate = nfl_kick_to_tid.get(nfl_hint)
                        if candidate is not None:
                            # Verify: Yahoo sometimes lists a kicker on the
                            # wrong team (e.g. "Nick Folk (ATL)" when Folk
                            # actually plays for NYJ).  If the event carries a
                            # player name, only accept the team-based lookup
                            # when the name matches the rostered kicker.
                            event_abbr    = yahoo_abbrev_key(player_name)
                            rostered_abbr = kick_tid_to_abbr.get(candidate, "")
                            if (not event_abbr or not rostered_abbr
                                    or event_abbr == rostered_abbr):
                                tid = candidate
                # Game-level loop: ONLY for ESPN events that lack an _nfl_team
                # hint.  nflverse always sets _nfl_team so using the loop there
                # would incorrectly assign the opponent's kicker stats to any
                # team that owns the other team's kicker in that game.
                if tid is None and not nfl_hint:
                    for nfl in game_nfl:
                        t = nfl_kick_to_tid.get(nfl)
                        if t:
                            tid = t
                            break
            if tid is None:
                continue
            _accum(tid, gid, stat, value, wc_dt, player_name=player_name)
            continue

        # ── Skill-position events ──────────────────────────────────────────
        nfl_hint  = normalize_nfl_team(ev.get("_nfl_team", ""))
        player_id = ev.get("_player_id", "")
        tid = _resolve_tid(player_name, game_nfl,
                           nfl_team_hint=nfl_hint, player_id=player_id)
        if tid is None:
            if player_name not in unmatched_names:
                log.debug("  Unmatched: %r game=%s", player_name, game_nfl)
                unmatched_names.add(player_name)
            continue

        _accum(tid, gid, stat, value, wc_dt, player_name=player_name)
        if stat in _TD_STATS:
            td_events[tid].append(
                (wc_dt, player_name, stat, ev.get("_desc", ""))
            )

    if unmatched_names:
        log.info("  %d unique player names unmatched (mostly bench/ST players)",
                 len(unmatched_names))

    # ── Defense points-allowed (game-level) ───────────────────────────────────
    _add_defense_pts_allowed(
        team_events, rosters, schedule,
        season, week, scoring_rules,
        last_wc_per_game=last_wc_per_game,
    )

    # ── Debug: verify team_events sum for _DEBUG_TID ──────────────────────────
    if _DEBUG_TID is not None:
        import sys as _bsys
        _tds_total = sum(v for _, v in team_events.get(_DEBUG_TID, []))
        print(f"  [DEBUG] team_events['{_DEBUG_TID}'] raw sum = {round(_tds_total,4)}", file=_bsys.stderr)
        _running = 0.0
        for _dt, _v in sorted(team_events.get(_DEBUG_TID, [])):
            _running = round(_running + _v, 4)
            if abs(_v) >= 0.01:
                print(f"    [EV] {_dt.isoformat()[:19]}  delta={_v:+.4f}  running={_running}", file=_bsys.stderr)

        # ── Per-player breakdown ───────────────────────────────────────────────
        print(f"\n  {'Player':<30} {'Stats':<55} {'Pts':>6}", file=_bsys.stderr)
        print(f"  {'-'*30} {'-'*55} {'-'*6}", file=_bsys.stderr)
        _player_totals: dict = {}
        for (_tid, _gid, _pname), _stats in player_stats.items():
            if _tid != _DEBUG_TID:
                continue
            _pts = compute_pts_from_stats(dict(_stats), scoring_rules)
            _player_totals[_pname] = _player_totals.get(_pname, 0.0) + _pts
        _stat_summaries: dict = {}
        for (_tid, _gid, _pname), _stats in player_stats.items():
            if _tid != _DEBUG_TID:
                continue
            _d = _stat_summaries.setdefault(_pname, {})
            for _k, _v2 in _stats.items():
                _d[_k] = round(_d.get(_k, 0.0) + _v2, 4)
        for _pname, _pts in sorted(_player_totals.items(), key=lambda x: -x[1]):
            _s = _stat_summaries.get(_pname, {})
            _parts = []
            if _s.get("pass_yards"):    _parts.append(f"{_s['pass_yards']:.0f}PyD")
            if _s.get("pass_td"):       _parts.append(f"{_s['pass_td']:.0f}PTD")
            if _s.get("int_thrown"):    _parts.append(f"-{_s['int_thrown']:.0f}INT")
            if _s.get("rush_yards"):    _parts.append(f"{_s['rush_yards']:.0f}RuYD")
            if _s.get("rush_td"):       _parts.append(f"{_s['rush_td']:.0f}RuTD")
            if _s.get("receptions"):    _parts.append(f"{_s['receptions']:.0f}Rec")
            if _s.get("rec_yards"):     _parts.append(f"{_s['rec_yards']:.0f}ReYD")
            if _s.get("rec_td"):        _parts.append(f"{_s['rec_td']:.0f}ReTD")
            if _s.get("fumble_lost"):   _parts.append(f"-{_s['fumble_lost']:.0f}Fum")
            _fg = sum(_s.get(k,0) for k in ("fg_0_19","fg_20_29","fg_30_39","fg_40_49","fg_50_plus"))
            if _fg:                     _parts.append(f"{_fg:.0f}FG")
            if _s.get("pat_made"):      _parts.append(f"{_s['pat_made']:.0f}PAT")
            if _s.get("def_sack"):      _parts.append(f"{_s['def_sack']:.0f}Sck")
            if _s.get("def_int"):       _parts.append(f"{_s['def_int']:.0f}DInt")
            if _s.get("def_fumble_rec"):_parts.append(f"{_s['def_fumble_rec']:.0f}FR")
            if _s.get("def_td"):        _parts.append(f"{_s['def_td']:.0f}DTD")
            if _s.get("def_return_td"): _parts.append(f"{_s['def_return_td']:.0f}RetTD")
            if _s.get("def_safety"):    _parts.append(f"{_s['def_safety']:.0f}Saf")
            if _s.get("def_blocked_kick"):_parts.append(f"{_s['def_blocked_kick']:.0f}Blk")
            if _s.get("def_4th_down_stop"):_parts.append(f"{_s['def_4th_down_stop']:.0f}4thSt")
            print(f"  {_pname:<30} {', '.join(_parts) or '—':<55} {_pts:>6.2f}", file=_bsys.stderr)
        print(f"  {'TOTAL':<87} {sum(_player_totals.values()):>6.2f}", file=_bsys.stderr)

    # ── Build unified timeline ─────────────────────────────────────────────────
    # Collect all event times, sort, then build a 1-minute-resolution grid.
    # Each minute in the grid represents one x-axis tick so that Sundays
    # (many parallel games) are proportionally wider than Thursday/Monday
    # single-game slots — while gaps > 25 min between game windows are still
    # excluded, preserving the Thursday→Sunday dead-time compression.
    all_times_set: set = set()
    for events in team_events.values():
        for dt, _ in events:
            all_times_set.add(dt)

    # Include game start times so each team begins at 0.
    # Only add timezone-aware datetimes to avoid mixing with event timestamps.
    for g in schedule:
        dt = _parse_wc(g.get("date"))
        if dt and dt.tzinfo is not None:
            all_times_set.add(dt)

    if not all_times_set:
        log.warning("No timestamped events found for week %d", week)
        return None

    minute_grid = _build_minute_grid(sorted(all_times_set))

    # Convert each minute tick to ET ISO string with UTC offset
    times_iso = [
        dt.astimezone(_ET).isoformat(timespec="seconds")
        for dt in minute_grid
    ]

    # ── Build per-matchup arrays ───────────────────────────────────────────────
    def cumulative_pts(tid: str) -> list:
        """Running cumulative points at each minute tick."""
        events = sorted(team_events.get(tid, []))   # [(datetime, pts), ...]
        result = []
        cumulative = 0.0
        ev_idx = 0
        for minute in minute_grid:
            next_min = minute + _MINUTE
            while ev_idx < len(events) and events[ev_idx][0] < next_min:
                cumulative = round(cumulative + events[ev_idx][1], 2)
                ev_idx += 1
            result.append(cumulative)
        return result

    matchup_list = []
    week_start, week_end = "", ""

    # Pre-sort minute_grid for bisect lookups below
    _mg_sorted = minute_grid  # already sorted

    def _make_td_markers(tid: str, pts_array: list) -> list:
        """Convert raw td_events entries into x-index + y-value marker dicts.

        When the same fantasy team owns both the passer and the receiver of a
        TD pass, two events land at the exact same wallclock timestamp.  Those
        are merged into one dot whose player label reads "QB & WR" so the
        hover tooltip names both contributors.
        """
        # ── Step 1: build intermediate list, keeping wallclock for merging ────
        raw = []
        for wc_dt, display_name, stat, desc in td_events.get(tid, []):
            search_dt = wc_dt + timedelta(seconds=59)
            idx = bisect.bisect_right(_mg_sorted, search_dt) - 1
            idx = max(0, min(idx, len(_mg_sorted) - 1))
            y = pts_array[idx] if pts_array else 0.0
            raw.append({"xi": idx, "y": y, "player": display_name,
                        "stat": stat, "desc": desc, "_wc": wc_dt})

        # ── Step 2: match each pass_td to a rec_td at the same wallclock ──────
        # (same wallclock = same play in nflverse data)
        pass_idx_by_wc: dict = {}   # wc_dt -> index of pass_td in raw
        for i, m in enumerate(raw):
            if m["stat"] == "pass_td":
                pass_idx_by_wc[m["_wc"]] = i

        paired_rec:  set = set()   # indices of rec_tds absorbed into a pass_td
        paired_pass: dict = {}     # pass_td index -> rec_td index

        for i, m in enumerate(raw):
            if m["stat"] == "rec_td" and m["_wc"] in pass_idx_by_wc:
                pi = pass_idx_by_wc[m["_wc"]]
                if pi not in paired_pass:   # take only the first receiver match
                    paired_pass[pi] = i
                    paired_rec.add(i)

        # ── Step 3: build final output, merging paired entries ────────────────
        merged = []
        for i, m in enumerate(raw):
            if i in paired_rec:
                continue    # already folded into its pass_td entry
            entry = {k: v for k, v in m.items() if k != "_wc"}
            if i in paired_pass:
                rec = raw[paired_pass[i]]
                entry["player"] = f"{m['player']} & {rec['player']}"
                entry["desc"]   = m["desc"] or rec["desc"]
            merged.append(entry)

        return merged

    for m in week_matchups:
        t1_id  = m["team1_id"]
        t2_id  = m["team2_id"]
        t1_name = m["team1_name"]
        t2_name = m["team2_name"]
        # Pull logo URLs from team_logos.json (fetched once per season)
        t1_logo = team_logos.get(t1_id, "")
        t2_logo = team_logos.get(t2_id, "")

        if not week_start:
            week_start = m.get("week_start", "")
            week_end   = m.get("week_end", "")

        pts1 = cumulative_pts(t1_id)
        pts2 = cumulative_pts(t2_id)

        # Use Yahoo's official final scores when available (much more accurate
        # than our ESPN reconstruction, which is an approximation).
        yahoo_final1 = m.get("team1_score")  # None if not fetched
        yahoo_final2 = m.get("team2_score")

        # Reconstructed trajectory final (for winner determination fallback)
        recon_final1 = pts1[-1] if pts1 else 0.0
        recon_final2 = pts2[-1] if pts2 else 0.0

        # Prefer Yahoo official scores for who actually won
        if yahoo_final1 is not None and yahoo_final2 is not None:
            final1, final2 = yahoo_final1, yahoo_final2
        else:
            final1, final2 = recon_final1, recon_final2

        winner = t1_name if final1 >= final2 else t2_name

        matchup_list.append({
            "team1":  t1_name,  "team2":  t2_name,
            "logo1":  t1_logo,  "logo2":  t2_logo,
            "winner": winner,
            "final1": final1,   "final2": final2,
            "pts1":   pts1,     "pts2":   pts2,
            "proj1":  None,     "proj2":  None,
            "wpct1":  None,
            "td_markers1": _make_td_markers(t1_id, pts1),
            "td_markers2": _make_td_markers(t2_id, pts2),
        })
        log.info("  %-26s  %5.1f  vs  %-26s  %5.1f  →  %s",
                 t1_name, final1, t2_name, final2, winner)

    # Date range string
    try:
        lo = datetime.strptime(week_start, "%Y-%m-%d")
        hi = datetime.strptime(week_end,   "%Y-%m-%d")
        date_range = (f"{lo.strftime('%b')} {lo.day} "
                      f"\u2013 {hi.strftime('%b')} {hi.day}, {hi.year}")
    except (ValueError, TypeError):
        date_range = f"Week {week}"

    log.info("  Minute grid: %d ticks across %d game windows",
             len(minute_grid), _count_windows(sorted(all_times_set)))

    standings = compute_standings(all_matchups, week, team_logos, season)

    return {
        "week":         week,
        "date_range":   date_range,
        "times":        times_iso,
        "vertical_idx": None,   # removed green-line feature; kept for schema compat
        "matchups":     matchup_list,
        "standings":    standings,
    }


def compute_standings(all_matchups: dict, through_week: int,
                      team_logos: dict, season: int) -> list:
    """
    Compute cumulative standings through `through_week` from matchups data.
    Returns a list of dicts sorted by wins desc, then PF desc:
      {team_id, team_name, logo, wins, losses, pf, pa, pr, etew_wins,
       etew_losses, bs}

    ETEW (Every Team Every Week): each week every team is ranked by score
    against all other teams.  The top scorer earns (N-1) ETEW wins and 0
    losses; the bottom scorer earns 0 wins and (N-1) losses; rank r
    (1=best) earns (N-r) wins and (r-1) losses.  These accumulate across
    weeks.  Sanity check: etew_wins + etew_losses == (N-1) * weeks_played.
    """
    teams: dict = {}   # team_id -> accumulator dict

    for wk in range(1, through_week + 1):
        week_matchups = all_matchups.get(str(wk), [])
        if not week_matchups:
            continue

        # Collect all teams' scores this week
        week_scores: list = []   # [(tid, name, score), ...]
        for m in week_matchups:
            for side, opp in [("team1", "team2"), ("team2", "team1")]:
                tid       = str(m[f"{side}_id"])
                name      = m[f"{side}_name"]
                score     = m.get(f"{side}_score") or 0.0
                opp_score = m.get(f"{opp}_score") or 0.0

                if tid not in teams:
                    teams[tid] = {
                        "team_id":      tid,
                        "team_name":    name,
                        "logo":         team_logos.get(tid, ""),
                        "wins":         0,
                        "losses":       0,
                        "pf":           0.0,
                        "pa":           0.0,
                        "etew_wins":    0,
                        "etew_losses":  0,
                        "median_wins":  0,
                        "median_losses": 0,
                        "bs":           None,
                    }
                teams[tid]["pf"] += score
                teams[tid]["pa"] += opp_score
                if score > opp_score:
                    teams[tid]["wins"] += 1
                else:
                    teams[tid]["losses"] += 1

                week_scores.append((tid, score))

        # ETEW + median wins: rank all teams by score this week (highest = rank 1)
        n = len(week_scores)
        half = n // 2   # top half = median wins, bottom half = median losses
        week_scores.sort(key=lambda x: x[1], reverse=True)
        for rank, (tid, _score) in enumerate(week_scores, start=1):
            teams[tid]["etew_wins"]   += (n - rank)
            teams[tid]["etew_losses"] += (rank - 1)
            if rank <= half:
                teams[tid]["median_wins"]   += 1
            else:
                teams[tid]["median_losses"] += 1

    # Load manually-assigned Pickles points
    # Format: { "Team Name": { "1": N, "3": N, ... } }
    # We sum only weeks 1 through through_week so each build is historically accurate.
    pickles_path = data_root(season) / "pickles.json"
    pickles_raw: dict = {}
    if pickles_path.exists():
        pickles_raw = _load(pickles_path)
        log.info("Loaded Pickles from %s", pickles_path)

    for t in teams.values():
        weekly = pickles_raw.get(t["team_name"], {})
        total = sum(
            v for wk_str, v in weekly.items()
            if int(wk_str) <= through_week
        )
        t["bs"] = total if total > 0 else None

    # Compute Power Ranking and round PF/PA
    # PR = (2 * PF) + (PF * win_pct) + (PF * median_win_pct)
    for t in teams.values():
        total_games  = t["wins"] + t["losses"]
        win_pct      = t["wins"] / total_games if total_games else 0.0
        median_games = t["median_wins"] + t["median_losses"]
        median_pct   = t["median_wins"] / median_games if median_games else 0.0
        pf = t["pf"]
        t["pr"] = round((2 * pf) + (pf * win_pct) + (pf * median_pct), 2)

    # Sort: wins desc, PF desc as tiebreaker
    result = sorted(
        teams.values(),
        key=lambda t: (-t["wins"], -t["pf"]),
    )
    for row in result:
        row["pf"] = round(row["pf"], 2)
        row["pa"] = round(row["pa"], 2)
    return result


def _turnover_td_pts_excluded(pbp_df, nfl_abbr: str, game_id: str) -> int:
    """
    Return the number of points scored AGAINST nfl_abbr via turnover-return TDs
    (fumble-recovery TDs or pick-6s) in game_id while nfl_abbr's OFFENSE was on
    the field.

    Yahoo fantasy scoring does NOT penalise the fantasy DEF for these points
    because the DEF unit was on the sideline when the opponent scored.

    Each qualifying TD is worth 6 points, so the return value is 6 * count.
    """
    try:
        import polars as pl
        # Plays where nfl_abbr's offense was on field AND opponent scored a TD
        # via a fumble recovery or interception return.
        mask = (
            (pl.col("game_id") == game_id) &
            (pl.col("posteam") == nfl_abbr) &
            (pl.col("touchdown") == 1) &
            (pl.col("td_team") != nfl_abbr) &
            (
                (pl.col("fumble_lost") == 1) |
                (
                    (pl.col("interception") == 1) &
                    (pl.col("return_touchdown") == 1)
                )
            )
        )
        count = len(pbp_df.filter(mask))
        return count * 6
    except Exception:
        return 0


def _add_defense_pts_allowed(
    team_events: dict,
    rosters: dict,
    schedule: list,
    season: int,
    week: int,
    scoring_rules: dict,
    last_wc_per_game: Optional[dict] = None,
) -> None:
    """
    For each team with a DEF starter, add the pts-allowed bonus at the game's
    last play wallclock.

    last_wc_per_game  – {game_id: datetime} built from event timestamps.
                        When present (nflverse path) no PBP files are read.
                        When absent (old ESPN path) falls back to PBP files.

    Fantasy scoring rule: fumble-return TDs and pick-6s scored by the opponent
    while the fantasy DEF's team's OFFENSE was on the field do NOT count toward
    points-allowed for the fantasy DEF (the DEF wasn't on the field).  We load
    the raw PBP to detect and subtract these plays.
    """
    # Build: nfl_team_abbr → fantasy team_id
    nfl_to_fantasy: dict = {}
    for tid, roster in rosters.items():
        for player in roster.get("starters", []):
            if player["position"] == "DEF":
                nfl = player.get("nfl_team", "").upper()
                if nfl:
                    nfl_to_fantasy[nfl] = tid

    if not nfl_to_fantasy:
        return

    pbp_dir = data_root(season) / "pbp" / f"week_{week:02d}"

    # Load raw nflverse PBP for turnover-TD exclusion (cached after first download)
    _pbp_df = None
    try:
        _pbp_df = nv_lib.load_pbp_week(season, week)
    except Exception as exc:
        log.debug("  Could not load raw PBP for turnover-TD exclusion: %s", exc)

    for game_meta in schedule:
        home       = normalize_nfl_team(game_meta.get("home", ""))
        away       = normalize_nfl_team(game_meta.get("away", ""))
        home_score = game_meta.get("home_score")
        away_score = game_meta.get("away_score")
        gid        = game_meta["game_id"]

        for nfl_abbr, ftid in nfl_to_fantasy.items():
            if nfl_abbr not in (home, away):
                continue

            opponent_pts = away_score if nfl_abbr == home else home_score

            # ── Get last wallclock for this game ──────────────────────────
            last_wc_dt: Optional[datetime] = None
            if last_wc_per_game:
                last_wc_dt = last_wc_per_game.get(gid)

            if last_wc_dt is None:
                # ESPN fallback: read the PBP JSON file
                pbp_file = pbp_dir / f"{gid}.json"
                if not pbp_file.exists():
                    continue
                plays    = json.loads(pbp_file.read_text())
                last_raw = _last_wallclock(plays)
                if last_raw is None:
                    continue
                last_wc_dt = _parse_wc(last_raw)
                if last_wc_dt is None:
                    continue
                if opponent_pts is None:
                    opponent_pts = _count_team_score_from_pbp(plays)
                    log.debug("  DEF %s: using PBP fallback score (%d pts)",
                              nfl_abbr, opponent_pts)

            if opponent_pts is None:
                log.debug("  DEF %s: opponent score unknown, skipping bonus", nfl_abbr)
                continue

            # ── Adjust for turnover-return TDs (Yahoo rule) ───────────────
            # Fumble-recovery TDs and pick-6s scored against this team while
            # their offense was on the field don't count as pts-allowed for
            # the fantasy DEF (the DEF unit wasn't on the field).
            excluded = 0
            if _pbp_df is not None:
                excluded = _turnover_td_pts_excluded(_pbp_df, nfl_abbr, gid)
                if excluded:
                    log.debug("  DEF %s: excluding %d pts (turnover-return TDs) "
                              "from pts-allowed  raw=%d  adj=%d",
                              nfl_abbr, excluded, int(opponent_pts),
                              int(opponent_pts) - excluded)

            adj_opp_pts = max(0, int(opponent_pts) - excluded)
            bonus = pts_allowed_bonus(adj_opp_pts, scoring_rules)
            team_events[ftid].append((last_wc_dt, bonus))
            log.debug("  DEF %s: opp=%d adj=%d pts → %+.1f bonus (10 base + tier)",
                      nfl_abbr, int(opponent_pts), adj_opp_pts, bonus)


def _count_team_score_from_pbp(plays: list) -> int:
    """
    Rough fallback: estimate ONE team's score from all scoring plays divided
    by 2.  Only used when schedule.json lacks actual final scores.
    """
    pts = 0
    for play in plays:
        if not play.get("is_scoring"):
            continue
        st = play.get("scoring_type", "")
        if st == "TD":    pts += 6
        elif st == "FG":  pts += 3
        elif st == "PAT": pts += 1
        elif st == "SF":  pts += 2
    return pts // 2


def _last_wallclock(plays: list) -> Optional[str]:
    last = None
    for p in plays:
        wc = p.get("wallclock")
        if wc:
            last = wc
    return last


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Reconstruct points-over-time from cached season data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--season",  type=int, required=True,
                    help="NFL season year (e.g. 2025)")
    ap.add_argument("--week",    type=int, default=None,
                    help="Build only this week (default: all available weeks)")
    ap.add_argument("--teams",   type=int, default=12,
                    help="Number of teams (default: 12)")
    ap.add_argument("--start-week", type=int, default=1)
    ap.add_argument("--end-week",   type=int, default=17)
    ap.add_argument("--source", choices=["nflverse", "espn"], default="nflverse",
                    help="Play-by-play data source (default: nflverse)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    weeks = [args.week] if args.week else list(range(args.start_week, args.end_week + 1))
    rules = load_scoring_rules(args.season)
    log.info("Loaded %d scoring rules  source=%s", len(rules), args.source)

    built = 0
    for week in weeks:
        log.info("─" * 50)
        log.info("Building week %d …", week)
        timeline = build_week_timeline(args.season, week, rules, args.teams, args.source)
        if timeline is None:
            log.warning("  Skipped week %d (missing data)", week)
            continue
        out = timeline_path(args.season, week)
        _save(out, timeline)
        log.info("  Saved → %s", out)
        built += 1

    log.info("=" * 50)
    log.info("Built %d/%d week timelines.", built, len(weeks))
    log.info("Next step:")
    for week in weeks:
        p = timeline_path(args.season, week)
        if p.exists():
            log.info("  python Scripts/generate_html.py --week %d --timeline %s",
                     week, p)


if __name__ == "__main__":
    main()
