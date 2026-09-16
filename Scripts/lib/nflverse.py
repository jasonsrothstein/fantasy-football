"""
lib/nflverse.py  –  nflverse data client for NFL play-by-play.

Downloads complete NFL play-by-play data via the nflreadpy package (which
pulls from the nflverse project on GitHub) and converts it to the same
fantasy-event format used by lib/espn.py.  This can eventually replace the
ESPN API pipeline with a source that:

  • Has true per-play wall-clock timestamps (time_of_day column, UTC ISO)
  • Covers ~100% of plays (ESPN API drops 5–10%)
  • Uses structured columns instead of regex-parsed play text
  • Ships one ~30 MB parquet per season instead of thousands of HTTP calls
  • Requires no authentication or rate-limiting care

Event format (same as espn.extract_fantasy_events output)
---------------------------------------------------------
Each returned dict has at minimum:
    wallclock   – ISO-8601 string in US Eastern time
    player_name – "F.Lastname" format matching Yahoo/ESPN
    stat        – scoring rule key (e.g. "pass_yards", "def_sack")
    value       – raw stat value (yards, 0/1 for binary events)
    _game_id    – nflverse game_id ("2025_01_KC_BAL")
    _nfl_team   – player's NFL team abbreviation

Defensive events also carry:
    __DEFENSE__ = True
    _def_team   – the fantasy-credited NFL team abbreviation
    _off_player – quarterback / ball-carrier involved (for roster lookup)

Usage
-----
    from lib import nflverse
    games  = nflverse.get_week_games(2025, 1)
    events = nflverse.get_week_events(2025, 1)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

# Import team normalizer lazily to avoid circular imports
def _norm_team(t: str) -> str:
    try:
        from lib.scoring import normalize_nfl_team
        return normalize_nfl_team(t)
    except ImportError:
        _MAP = {"LA": "LAR", "WSH": "WAS", "JAC": "JAX", "LVR": "LV",
                "ARZ": "ARI", "CLV": "CLE", "HST": "HOU"}
        return _MAP.get(t.upper(), t.upper()) if t else t

log = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")


# ─── Optional heavy imports (guard so lib is importable in tests) ─────────────

try:
    import polars as pl  # type: ignore
except ImportError:
    pl = None  # type: ignore

try:
    import nflreadpy  # type: ignore
except ImportError:
    nflreadpy = None  # type: ignore


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _require_deps() -> None:
    if nflreadpy is None or pl is None:
        raise RuntimeError(
            "nflreadpy and polars are required.  Run:\n"
            "    pip install nflreadpy"
        )


def _to_et(utc_str: Optional[str]) -> Optional[str]:
    """Convert a UTC ISO-8601 string (e.g. '2025-09-07T17:05:25.293Z') to ET."""
    if not utc_str:
        return None
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return dt.astimezone(_ET).isoformat()
    except (ValueError, AttributeError):
        return None


def _v(row: dict, key: str, default=None):
    """Safe column read; treats NaN / Polars null sentinel as None."""
    val = row.get(key, default)
    if val is None:
        return default
    # polars returns Python floats for nullable int columns
    try:
        import math
        if isinstance(val, float) and math.isnan(val):
            return default
    except Exception:
        pass
    return val


def _fg_stat(distance: float, made: bool) -> Optional[str]:
    """Return the scoring stat key for a field goal by distance and result."""
    d = int(distance or 0)
    if d < 20:
        tier = "0_19"
    elif d < 30:
        tier = "20_29"
    elif d < 40:
        tier = "30_39"
    elif d < 50:
        tier = "40_49"
    else:
        tier = "50_plus"
    return f"fg_{tier}" if made else f"fg_miss_{tier}"


# ─── Schedule / game list ─────────────────────────────────────────────────────

def get_week_games(season: int, week: int) -> list[dict]:
    """
    Return standard game dicts for all regular-season NFL games in a given week.

    Compatible with espn.get_week_games() — callers can swap the source with
    no changes to build_timelines.py.

    Extra key '_espn_id' carries the ESPN numeric game_id for cross-reference.
    """
    _require_deps()
    sched = nflreadpy.load_schedules([season])
    week_rows = sched.filter(
        (pl.col("week") == week) &
        (pl.col("game_type") == "REG")
    ).to_dicts()

    games = []
    for r in week_rows:
        gametime = _v(r, "gametime") or "00:00"
        games.append({
            "game_id":    _v(r, "game_id", ""),
            "home":       _v(r, "home_team", ""),
            "away":       _v(r, "away_team", ""),
            "home_score": int(_v(r, "home_score") or 0),
            "away_score": int(_v(r, "away_score") or 0),
            "date":       f"{_v(r, 'gameday', '')}T{gametime}:00",
            "status":     "final" if _v(r, "home_score") is not None else "scheduled",
            "_espn_id":   str(_v(r, "espn") or ""),
        })

    log.info("nflverse  season=%d week=%d → %d games", season, week, len(games))
    return games


# ─── Play-by-play loader ──────────────────────────────────────────────────────

def load_pbp_week(season: int, week: int) -> "pl.DataFrame":
    """
    Return a Polars DataFrame of all regular-season plays for one week.

    The full season parquet (~30 MB) is downloaded and cached locally on first
    call; subsequent calls hit nflreadpy's on-disk cache instantly.
    """
    _require_deps()
    pbp = nflreadpy.load_pbp([season])
    return pbp.filter(
        (pl.col("week") == week) &
        (pl.col("season_type") == "REG")
    )


# ─── Per-play event extraction ────────────────────────────────────────────────

def extract_fantasy_events(row: dict) -> list[dict]:
    """
    Convert one nflverse play-by-play row (as a plain dict) into a list of
    fantasy scoring events.

    This is the nflverse equivalent of espn.extract_fantasy_events().  Because
    nflverse provides structured columns for each player role, we never need to
    parse play descriptions with regex.

    Stat accumulation note
    ----------------------
    build_timelines.py accumulates these events over a full game per player,
    then calls compute_pts_from_stats().  That means milestone bonuses (100-yd
    bonus etc.) emerge naturally from the running totals — we just emit the raw
    per-play yard/count values here.
    """
    wall = _to_et(_v(row, "time_of_day"))
    game_id = _v(row, "game_id", "")
    posteam = _norm_team(_v(row, "posteam", "") or "")
    defteam = _norm_team(_v(row, "defteam", "") or "")
    play_type = _v(row, "play_type", "")

    # Strip leading game-clock notation "(7:24) " from play descriptions so
    # TD marker popups show readable text without the redundant clock string.
    raw_desc = _v(row, "desc", "") or ""
    _td_desc = re.sub(r'^\(\d+:\d+\)\s*', '', raw_desc).strip()

    evs: list[dict] = []

    # Pre-fetch player IDs for each role on this play
    _pid_passer        = _v(row, "passer_player_id")           or ""
    _pid_rusher        = _v(row, "rusher_player_id")           or ""
    _pid_receiver      = _v(row, "receiver_player_id")         or ""
    _pid_kicker        = _v(row, "kicker_player_id")           or ""
    _pid_fumbled       = _v(row, "fumbled_1_player_id")        or ""
    _pid_lat_receiver  = _v(row, "lateral_receiver_player_id") or ""
    _pid_lat_rusher    = _v(row, "lateral_rusher_player_id")   or ""
    _pid_returner = (
        _v(row, "kickoff_returner_player_id") or
        _v(row, "punt_returner_player_id") or ""
    )
    _pid_fumrec   = _v(row, "fumble_recovery_1_player_id") or ""

    def _ev(player: str, stat: str, value, *, nfl_team: str = posteam,
            player_id: str = "", extra: Optional[dict] = None) -> dict:
        d: dict = {
            "wallclock":    wall,
            "player_name":  player,
            "stat":         stat,
            "value":        value,
            "_game_id":     game_id,
            "_nfl_team":    nfl_team,
        }
        if player_id:
            d["_player_id"] = player_id
        if extra:
            d.update(extra)
        return d

    def _def(def_team: str, stat: str, *, off_player: str = "",
             extra: Optional[dict] = None) -> dict:
        d: dict = {
            "wallclock":    wall,
            "player_name":  "__DEFENSE__",
            "stat":         stat,
            "value":        1,
            "_game_id":     game_id,
            "_nfl_team":    def_team,
            "__DEFENSE__":  True,
            "_def_team":    def_team,
        }
        if off_player:
            d["_off_player"] = off_player
        if extra:
            d.update(extra)
        return d

    # ── Skip non-scoring play types ───────────────────────────────────────────
    # (kickoffs, punts, timeouts, end-of-quarter markers, etc.)
    if play_type in ("kickoff", "punt", "no_play", "qb_kneel", "qb_spike"):
        # Exception: kickoff/punt return TDs are handled below
        pass  # fall through — return_touchdown check below covers the TD

    two_point_attempt = bool(_v(row, "two_point_attempt", 0))
    is_pass   = bool(_v(row, "pass_attempt", 0)) or bool(_v(row, "complete_pass", 0))
    is_rush   = bool(_v(row, "rush_attempt", 0)) and not bool(_v(row, "sack", 0))
    is_sack   = bool(_v(row, "sack", 0))

    # ── Two-point conversion ──────────────────────────────────────────────────
    # Handle before normal pass/rush to avoid double-counting yards
    if two_point_attempt:
        if _v(row, "two_point_conv_result") == "success":
            passer  = _v(row, "passer_player_name")
            rusher  = _v(row, "rusher_player_name")
            recvr   = _v(row, "receiver_player_name")
            if recvr and passer:           # pass 2PC
                evs.append(_ev(passer, "two_pt_conversion", 1, player_id=_pid_passer))
                evs.append(_ev(recvr,  "two_pt_conversion", 1, player_id=_pid_receiver))
            elif rusher:                   # rush 2PC
                evs.append(_ev(rusher, "two_pt_conversion", 1, player_id=_pid_rusher))
        return evs  # no other stats on 2PC plays

    # ── Field goals ───────────────────────────────────────────────────────────
    fg_attempt = bool(_v(row, "field_goal_attempt", 0))
    if fg_attempt:
        kicker = _v(row, "kicker_player_name")
        result = _v(row, "field_goal_result", "")
        dist   = float(_v(row, "kick_distance") or 0)
        if kicker and result in ("made", "blocked", "missed"):
            made = result == "made"
            stat = _fg_stat(dist, made)
            if stat:
                evs.append(_ev(kicker, stat, 1, player_id=_pid_kicker))
        if result == "blocked":
            evs.append(_def(defteam, "def_blocked_kick"))
        return evs

    # ── Extra point (PAT) ─────────────────────────────────────────────────────
    pat_attempt = bool(_v(row, "extra_point_attempt", 0))
    if pat_attempt:
        kicker = _v(row, "kicker_player_name")
        result = _v(row, "extra_point_result", "")
        if kicker and result:
            stat = "pat_made" if result == "good" else "pat_missed"
            evs.append(_ev(kicker, stat, 1, player_id=_pid_kicker))
        # Blocked PAT credits the defending team's block kick bonus
        if result == "blocked":
            evs.append(_def(defteam, "def_blocked_kick",
                            extra={"_desc": _td_desc} if _td_desc else None))
        return evs

    # ── Passing plays ─────────────────────────────────────────────────────────
    passer   = _v(row, "passer_player_name")
    receiver = _v(row, "receiver_player_name")
    pass_yds = _v(row, "passing_yards")
    rec_yds  = _v(row, "receiving_yards")

    if is_pass and passer:
        # Passing yards (only on completed passes; sacks handled separately)
        if pass_yds is not None and float(pass_yds) != 0:
            evs.append(_ev(passer, "pass_yards", float(pass_yds), player_id=_pid_passer))
        # Completion → receiver stats
        if bool(_v(row, "complete_pass", 0)) and receiver:
            evs.append(_ev(receiver, "receptions",  1, player_id=_pid_receiver))
            if rec_yds is not None:
                evs.append(_ev(receiver, "rec_yards", float(rec_yds), player_id=_pid_receiver))
        # Lateral receiver on passing plays (e.g. pass to A, lateral to B)
        # Yahoo credits the lateral receiver with receiving yards (not a reception).
        lat_receiver = _v(row, "lateral_receiver_player_name")
        lat_rec_yds  = _v(row, "lateral_receiving_yards")
        if lat_receiver and lat_rec_yds is not None:
            evs.append(_ev(lat_receiver, "rec_yards", float(lat_rec_yds),
                           player_id=_pid_lat_receiver))
        # Touchdown
        if bool(_v(row, "pass_touchdown", 0)):
            dist_total = float(abs(rec_yds or pass_yds or 0))
            _td_extra = {"_desc": _td_desc} if _td_desc else None
            evs.append(_ev(passer, "pass_td", 1, player_id=_pid_passer, extra=_td_extra))
            if dist_total >= 40:
                evs.append(_ev(passer, "pass_td_40plus", 1, player_id=_pid_passer))
            if receiver:
                evs.append(_ev(receiver, "rec_td", 1, player_id=_pid_receiver, extra=_td_extra))
                if dist_total >= 40:
                    evs.append(_ev(receiver, "rec_td_40plus", 1, player_id=_pid_receiver))
        # Interception thrown → passer charged, defense credited
        if bool(_v(row, "interception", 0)):
            evs.append(_ev(passer, "int_thrown", 1, player_id=_pid_passer))
            int_player = _v(row, "interception_player_name")
            evs.append(_def(defteam, "def_int", off_player=passer or ""))
            # If the interception was returned for a TD, credit defensive TD
            if bool(_v(row, "return_touchdown", 0)) and _v(row, "td_team") == defteam:
                evs.append(_def(defteam, "def_td", off_player=passer or "",
                                extra={"_desc": _td_desc} if _td_desc else None))

    # ── Sack ─────────────────────────────────────────────────────────────────
    # The defense gets the sack credit; no passer stat change (yards already
    # accounted for in the drive; sack yards don't affect passing_yards total
    # in stat accumulation since passing_yards is NULL on sack plays).
    if is_sack:
        sack_player = _v(row, "sack_player_name")
        h1 = _v(row, "half_sack_1_player_name")
        h2 = _v(row, "half_sack_2_player_name")
        qb = _v(row, "passer_player_name") or ""
        if sack_player:
            evs.append(_def(defteam, "def_sack", off_player=qb))
        elif h1 or h2:
            # Half-sack: one full fantasy sack credit split between the two
            # defenders, but our league awards a sack per-team so just emit one
            evs.append(_def(defteam, "def_sack", off_player=qb))

    # ── Rushing plays ─────────────────────────────────────────────────────────
    rusher   = _v(row, "rusher_player_name")
    rush_yds = _v(row, "rushing_yards")

    if is_rush and rusher:
        if rush_yds is not None:
            evs.append(_ev(rusher, "rush_yards", float(rush_yds), player_id=_pid_rusher))
        # Lateral rusher on rushing plays (e.g. handoff then pitch)
        lat_rusher     = _v(row, "lateral_rusher_player_name")
        lat_rush_yds   = _v(row, "lateral_rushing_yards")
        if lat_rusher and lat_rush_yds is not None:
            evs.append(_ev(lat_rusher, "rush_yards", float(lat_rush_yds),
                           player_id=_pid_lat_rusher))
        if bool(_v(row, "rush_touchdown", 0)):
            dist_total = float(abs(rush_yds or 0))
            _td_extra = {"_desc": _td_desc} if _td_desc else None
            evs.append(_ev(rusher, "rush_td", 1, player_id=_pid_rusher, extra=_td_extra))
            if dist_total >= 40:
                evs.append(_ev(rusher, "rush_td_40plus", 1, player_id=_pid_rusher))

    # ── Fumbles ───────────────────────────────────────────────────────────────
    if bool(_v(row, "fumble_lost", 0)):
        fumbler    = _v(row, "fumbled_1_player_name")
        fumbler_id = _pid_fumbled
        if not fumbler:
            # Fall back: whoever was carrying on this play type
            fumbler    = _v(row, "rusher_player_name") or _v(row, "passer_player_name")
            fumbler_id = _pid_rusher or _pid_passer
        rec_team = _v(row, "fumble_recovery_1_team", "")
        if fumbler:
            evs.append(_ev(fumbler, "fumble_lost", 1, player_id=fumbler_id))
        # Defense recovers → credit
        if rec_team == defteam:
            evs.append(_def(defteam, "def_fumble_rec", off_player=fumbler or ""))
            # Fumble returned for TD by defense
            td_team = _v(row, "td_team", "")
            if td_team == defteam and bool(_v(row, "touchdown", 0)):
                evs.append(_def(defteam, "def_td", off_player=fumbler or "",
                                extra={"_desc": _td_desc} if _td_desc else None))
        # Offensive fumble recovery returned for TD
        elif rec_team == posteam and bool(_v(row, "touchdown", 0)):
            rec_player = _v(row, "fumble_recovery_1_player_name")
            if rec_player:
                _td_extra = {"_desc": _td_desc} if _td_desc else None
                evs.append(_ev(rec_player, "off_fumble_ret_td", 1, player_id=_pid_fumrec,
                               extra=_td_extra))

    # ── Kickoff / punt return TDs (non-interception) ─────────────────────────
    # On punt/kickoff plays, posteam = the kicking/punting team; defteam =
    # the receiving team.  When the receiver returns for a TD, td_team == defteam.
    if bool(_v(row, "return_touchdown", 0)):
        returner = (
            _v(row, "kickoff_returner_player_name")
            or _v(row, "punt_returner_player_name")
        )
        td_team = _v(row, "td_team", "")
        # Return TD by the receiving/defending team (the common case)
        if returner and td_team == defteam:
            _td_extra = {"_desc": _td_desc} if _td_desc else None
            evs.append(_ev(returner, "return_td", 1, nfl_team=defteam, player_id=_pid_returner,
                           extra=_td_extra))
            # Credit the DEF/ST unit — Yahoo awards 6 pts for a
            # kickoff or punt return TD regardless of the individual returner
            evs.append(_def(defteam, "def_return_td",
                            extra={"_desc": _td_desc} if _td_desc else None))

    # ── Safety ────────────────────────────────────────────────────────────────
    if bool(_v(row, "safety", 0)):
        evs.append(_def(defteam, "def_safety"))

    # ── 4th-down stop ────────────────────────────────────────────────────────
    if bool(_v(row, "fourth_down_failed", 0)):
        evs.append(_def(defteam, "def_4th_down_stop"))

    # ── Blocked kicks ─────────────────────────────────────────────────────────
    # field_goal_result == 'blocked' is already handled in the FG branch above.
    # Punt blocks:
    if bool(_v(row, "punt_blocked", 0)):
        evs.append(_def(defteam, "def_blocked_kick"))

    return evs


# ─── Week-level event aggregator ─────────────────────────────────────────────

def get_week_events(season: int, week: int) -> list[dict]:
    """
    Return all fantasy events for every play in a given regular-season week.

    This is the primary entry point for build_timelines.py integration.
    Events are sorted by wallclock time.
    """
    _require_deps()
    df = load_pbp_week(season, week)
    rows = df.to_dicts()

    all_events: list[dict] = []
    for row in rows:
        all_events.extend(extract_fantasy_events(row))

    # Sort by wallclock (None timestamps sort to the end)
    all_events.sort(key=lambda e: e.get("wallclock") or "9999")

    log.info(
        "nflverse  season=%d week=%d → %d plays → %d events",
        season, week, len(rows), len(all_events),
    )
    return all_events
