#!/usr/bin/env python3
"""
fetch_season.py  –  Download and cache all raw data needed to reconstruct
                    points-over-time for a full fantasy season.

This script is intentionally a pure downloader — it stores everything as JSON
so that the reconstruction step (build_timelines.py) can be re-run without
hitting any API again.

What it fetches
---------------
  Yahoo (browser session cookies):
    • NFL game key for the season
    • League scoring settings
    • Weekly matchup schedule (who plays who)
    • Every team's roster for every week (starters vs bench)

  ESPN (no auth):
    • Game IDs for every NFL week
    • Full play-by-play (with wallclock timestamps) for every game

Usage
-----
    python Scripts/fetch_season.py --season 2025 --league 656728
    python Scripts/fetch_season.py --season 2025 --league 656728 --weeks 10 11 12
    python Scripts/fetch_season.py --season 2025 --league 656728 --skip-existing

Auth
----
    Reads Scripts/auth/yahoo_cookies.txt (or override with --cookies).
    When cookies expire (~24 h), log into football.fantasysports.yahoo.com,
    open DevTools → Network → any request → Request Headers → Cookie,
    and replace the file contents with the fresh value.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# Make lib importable when run from repo root or Scripts/
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from lib import yahoo_cookies as yahoo, espn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# How long to pause between Yahoo API calls to avoid rate-limiting
YAHOO_SLEEP = 0.4   # seconds
ESPN_SLEEP  = 0.25  # seconds


# ─── Path helpers ─────────────────────────────────────────────────────────────

def data_root(season: int) -> Path:
    return _HERE.parent / "Data" / str(season)

def settings_path(season: int) -> Path:
    return data_root(season) / "settings.json"

def matchups_path(season: int) -> Path:
    return data_root(season) / "matchups.json"

def roster_path(season: int, week: int, team_id: str) -> Path:
    return data_root(season) / "rosters" / f"week_{week:02d}" / f"team_{int(team_id):02d}.json"

def pbp_path(season: int, week: int, game_id: str) -> Path:
    return data_root(season) / "pbp" / f"week_{week:02d}" / f"{game_id}.json"

def schedule_path(season: int) -> Path:
    return data_root(season) / "schedule.json"


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ─── Fetch helpers ────────────────────────────────────────────────────────────

def fetch_settings(oauth, game_key: str, league_id: str,
                   season: int, skip: bool) -> dict:
    p = settings_path(season)
    if skip and p.exists():
        log.info("Settings: cached (%s)", p)
        return _load(p)

    log.info("Fetching scoring settings …")
    rules = yahoo.get_scoring_settings(oauth, game_key, league_id)
    meta  = {
        "season":    season,
        "game_key":  game_key,
        "league_id": league_id,
        "rules":     rules,
    }
    _save(p, meta)
    log.info("Saved settings → %s", p)
    return meta


def fetch_matchups(oauth, game_key: str, league_id: str,
                   season: int, weeks: list, skip: bool) -> dict:
    p = matchups_path(season)
    if skip and p.exists():
        log.info("Matchups: cached (%s)", p)
        return _load(p)

    log.info("Fetching matchup schedule for %d weeks …", len(weeks))
    all_matchups = {}
    for week in weeks:
        matchups = yahoo.get_weekly_matchups(oauth, game_key, league_id, week)
        all_matchups[str(week)] = matchups
        log.info("  Week %2d: %d matchups", week, len(matchups))
        time.sleep(YAHOO_SLEEP)

    _save(p, all_matchups)
    log.info("Saved matchups → %s", p)
    return all_matchups


def fetch_rosters(oauth, game_key: str, league_id: str,
                  season: int, weeks: list, num_teams: int, skip: bool) -> None:
    total = len(weeks) * num_teams
    done  = 0
    for week in weeks:
        for team_id in range(1, num_teams + 1):
            p = roster_path(season, week, str(team_id))
            if skip and p.exists():
                done += 1
                continue
            try:
                roster = yahoo.get_roster(oauth, game_key, league_id,
                                          str(team_id), week)
                _save(p, roster)
                log.info("  Roster wk%02d t%02d %-22s  [%d/%d]",
                         week, team_id, roster["team_name"], done + 1, total)
            except Exception as exc:
                log.warning("  FAILED wk%02d t%02d: %s", week, team_id, exc)
            done += 1
            time.sleep(YAHOO_SLEEP)


def fetch_schedule_and_pbp(season: int, weeks: list, skip: bool) -> dict:
    """Fetch ESPN game IDs for each week, then play-by-play for each game."""
    sched_p = schedule_path(season)

    # Load or build the schedule (game_id lists per week)
    if skip and sched_p.exists():
        schedule = _load(sched_p)
        log.info("Schedule: cached (%s)", sched_p)
    else:
        log.info("Fetching ESPN schedule …")
        schedule = {}
        for week in weeks:
            games = espn.get_week_games(season, week)
            schedule[str(week)] = games
            log.info("  Week %2d: %d games", week, len(games))
            time.sleep(ESPN_SLEEP)
        _save(sched_p, schedule)
        log.info("Saved schedule → %s", sched_p)

    # Fetch play-by-play for each game
    for week in weeks:
        games = schedule.get(str(week), [])
        for g in games:
            gid = g["game_id"]
            p   = pbp_path(season, week, gid)
            if skip and p.exists():
                continue
            try:
                plays = espn.get_game_plays(gid)
                _save(p, plays)
                log.info("  PbP wk%02d game %-12s  %s vs %s  (%d plays)",
                         week, gid, g["away"], g["home"], len(plays))
            except Exception as exc:
                log.warning("  FAILED pbp wk%02d game %s: %s", week, gid, exc)
            time.sleep(ESPN_SLEEP)

    return schedule


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fetch and cache all raw data for a fantasy season.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--season",  type=int, required=True,
                    help="NFL season year (e.g. 2025)")
    ap.add_argument("--league",  type=str, required=True,
                    help="Yahoo league ID (e.g. 656728)")
    ap.add_argument("--teams",   type=int, default=12,
                    help="Number of teams in the league (default: 12)")
    ap.add_argument("--start-week", type=int, default=1,
                    help="First week to fetch (default: 1)")
    ap.add_argument("--end-week",   type=int, default=17,
                    help="Last week to fetch (default: 17)")
    ap.add_argument("--weeks", type=int, nargs="+", default=None,
                    help="Fetch only specific weeks (overrides --start/end-week)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip files that are already cached (safe to re-run)")
    ap.add_argument("--cookies", type=str,
                    default=str(_HERE / "auth" / "yahoo_cookies.txt"),
                    help="Path to yahoo_cookies.txt (browser session cookies)")
    ap.add_argument("--no-yahoo", action="store_true",
                    help="Skip Yahoo fetching (only fetch ESPN data)")
    ap.add_argument("--no-espn",  action="store_true",
                    help="Skip ESPN fetching (only fetch Yahoo data)")
    ap.add_argument("--refresh-scores", action="store_true",
                    help="Re-fetch ESPN schedule to update home_score/away_score "
                         "(does NOT re-fetch play-by-play)")
    args = ap.parse_args()

    weeks = args.weeks or list(range(args.start_week, args.end_week + 1))
    skip  = args.skip_existing

    log.info("=" * 60)
    log.info("Season %d  |  League %s  |  Weeks %s",
             args.season, args.league, weeks)
    log.info("=" * 60)

    # ── Yahoo data ────────────────────────────────────────────────────────────
    game_key = None
    if not args.no_yahoo:
        log.info("Authenticating with Yahoo (cookie session) …")
        oauth = yahoo.login(args.cookies, league_id=args.league)
        log.info("Authenticated.")

        game_key = yahoo.get_nfl_game_key(oauth, args.season)
        log.info("NFL game key for %d: %s", args.season, game_key)

        fetch_settings(oauth, game_key, args.league, args.season, skip)
        fetch_matchups(oauth, game_key, args.league, args.season, weeks, skip)

        # Fetch team logo URLs (once per season, not per-week)
        logos_p = Path(f"Data/{args.season}/team_logos.json")
        if not (skip and logos_p.exists()):
            log.info("Fetching team logos …")
            logos = yahoo.get_team_logos(oauth, game_key, args.league)
            _save(logos_p, logos)
            log.info("Saved team logos → %s  (%d teams)", logos_p, len(logos))
        else:
            log.info("Team logos: cached (%s)", logos_p)

        log.info("Fetching rosters  (%d weeks × %d teams = %d calls) …",
                 len(weeks), args.teams, len(weeks) * args.teams)
        fetch_rosters(oauth, game_key, args.league, args.season,
                      weeks, args.teams, skip)
        log.info("Yahoo fetch complete.")

    # ── ESPN data ─────────────────────────────────────────────────────────────
    if args.refresh_scores and not args.no_espn:
        log.info("Re-fetching ESPN schedule (scores only) …")
        sched_p = schedule_path(args.season)
        old_sched = _load(sched_p) if sched_p.exists() else {}
        for week in weeks:
            games = espn.get_week_games(args.season, week)
            old_sched[str(week)] = games
            log.info("  Week %2d: %d games refreshed", week, len(games))
            time.sleep(ESPN_SLEEP)
        _save(sched_p, old_sched)
        log.info("Saved updated schedule → %s", sched_p)
    elif not args.no_espn:
        log.info("Fetching ESPN play-by-play …")
        fetch_schedule_and_pbp(args.season, weeks, skip)
        log.info("ESPN fetch complete.")

    log.info("=" * 60)
    log.info("All done. Data stored in:  %s", data_root(args.season))
    log.info("Next step:  python Scripts/build_timelines.py --season %d", args.season)


if __name__ == "__main__":
    main()
