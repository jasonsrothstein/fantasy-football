#!/usr/bin/env python3
"""
run_week.py  –  One-command pipeline: fetch → build → generate HTML for a week.

Always wipes and rebuilds generated artifacts (timelines, player_scores.json,
HTML files) so that week ordering bugs are impossible — player_scores.json is
always reconstructed from scratch in week order.

Fetch is the only step that can be skipped (it requires Yahoo cookies and
is the slow/authenticated part).  Roster data that is already on disk is
never re-downloaded unless you pass --refetch.

Usage
-----
    python Scripts/run_week.py 3              # full clean rebuild for weeks 1-3
    python Scripts/run_week.py 3 --from 2    # fetch only weeks 2-3; rebuild ALL 1-3
    python Scripts/run_week.py 3 --skip-fetch  # rebuild from cached raw data
    python Scripts/run_week.py 3 --only-html   # re-render HTML only (no fetch/build)
    python Scripts/run_week.py 3 --season 2025 --league 123456
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT    = Path(__file__).resolve().parent.parent   # repo root
SCRIPTS = ROOT / "Scripts"
DATA    = ROOT / "Data"
DOCS    = ROOT / "docs"


# ─── helpers ──────────────────────────────────────────────────────────────────

def detect_season() -> int:
    """Return the latest year found under Data/."""
    candidates = sorted(
        [int(p.name) for p in DATA.iterdir() if p.is_dir() and p.name.isdigit()],
        reverse=True,
    )
    if not candidates:
        raise SystemExit(
            "No Data/<year>/ directory found.\n"
            "Either run fetch_season.py manually first, or pass --season <year>."
        )
    return candidates[0]


def load_settings(season: int) -> dict:
    path = DATA / str(season) / "settings.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _dir_has_files(d: Path) -> bool:
    """True if a directory exists and contains at least one file."""
    return d.is_dir() and any(f.is_file() for f in d.iterdir())


def rosters_fetched(season: int, week: int) -> bool:
    return _dir_has_files(DATA / str(season) / "rosters" / f"week_{week:02d}")


def remove_if_exists(p: Path, label: str) -> None:
    if p.exists():
        p.unlink()
        print(f"    deleted  {p.relative_to(ROOT)}")


def wipe_generated(season: int, target_week: int) -> None:
    """
    Delete all files that will be rebuilt by this run so we never serve
    stale artifacts.  Raw fetched data (rosters/, pbp/) is NOT touched.
    """
    print(f"\n{'─' * 62}")
    print(f"  WIPE  (removing stale artifacts for weeks 1–{target_week})")
    print(f"{'─' * 62}")

    # player_scores.json accumulates across weeks in order — must always be
    # rebuilt from scratch to avoid out-of-order contamination.
    remove_if_exists(DATA / str(season) / "player_scores.json", "player_scores.json")

    # Timeline JSONs and HTML files for every week up to target.
    for week in range(1, target_week + 1):
        remove_if_exists(
            DATA / str(season) / "timelines" / f"week_{week:02d}.json",
            f"timelines/week_{week:02d}.json",
        )
        remove_if_exists(ROOT / f"week_{week}_matchups.html", f"week_{week}_matchups.html")
        # docs/ copy (published via GitHub Pages etc.)
        remove_if_exists(DOCS / f"week_{week}_matchups.html", f"docs/week_{week}_matchups.html")

    print()


def run(cmd: list, label: str) -> None:
    """Run a subprocess, printing the command, and abort on non-zero exit."""
    print(f"\n{'=' * 62}")
    print(f"  {label}")
    print(f"{'=' * 62}")
    print(f"  $ {' '.join(str(c) for c in cmd)}\n")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(
            f"\n[ABORT] '{label}' exited with code {result.returncode}.\n"
            "Fix the error above and re-run.  Already-fetched data is still on\n"
            "disk, so --skip-fetch will skip the download step on the next run.",
            file=sys.stderr,
        )
        sys.exit(result.returncode)


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="run_week.py",
        description="Clean-rebuild fetch → build → HTML pipeline for a fantasy week.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "week", type=int,
        help="Target week number (1–17).  Timelines for ALL weeks 1→N are "
             "always rebuilt in order.",
    )
    parser.add_argument(
        "--from", dest="from_week", type=int, default=1, metavar="N",
        help="Only FETCH weeks N→target (default: 1).  Weeks before N are "
             "assumed already fetched.  Build/HTML still runs for all weeks 1→target.",
    )
    parser.add_argument(
        "--season", type=int,
        help="NFL season year (e.g. 2026).  Auto-detected from Data/ if omitted.",
    )
    parser.add_argument(
        "--league", type=int,
        help="Yahoo league ID.  Read from Data/<season>/settings.json if omitted.",
    )
    parser.add_argument(
        "--skip-fetch", action="store_true",
        help="Skip all fetching; use whatever raw data is already on disk.",
    )
    parser.add_argument(
        "--refetch", action="store_true",
        help="Re-download even weeks whose roster data is already cached "
             "(also picks up player image_url fields added to roster parsing).",
    )
    parser.add_argument(
        "--only-html", action="store_true",
        help="Skip fetch and build; only re-run generate_html.py for each week.",
    )
    args = parser.parse_args()

    # ── resolve season / league ──────────────────────────────────────
    season = args.season or detect_season()
    settings = load_settings(season)
    league   = args.league or settings.get("league_id")

    if not league and not args.skip_fetch and not args.only_html:
        raise SystemExit(
            "League ID not found.  Pass --league <id> or ensure\n"
            f"  Data/{season}/settings.json  has a 'league_id' key.\n"
            "Use --skip-fetch or --only-html to bypass fetching."
        )

    target_week = args.week
    fetch_from  = args.from_week

    if fetch_from > target_week:
        raise SystemExit(f"--from {fetch_from} is greater than target week {target_week}.")

    print(f"\n{'#' * 62}")
    print(f"  Fantasy Pipeline  ·  Season {season}  ·  League {league}")
    print(f"  Target: week {target_week}   |   Fetch from: week {fetch_from}")
    print(f"{'#' * 62}")

    # ── wipe stale artifacts ──────────────────────────────────────────
    if not args.only_html:
        wipe_generated(season, target_week)

    # ── process each week ─────────────────────────────────────────────
    for week in range(1, target_week + 1):

        print(f"\n{'─' * 62}")
        print(f"  WEEK {week}{' ← target' if week == target_week else ''}")
        print(f"{'─' * 62}")

        # ── 1. FETCH ─────────────────────────────────────────────────
        if not args.only_html and not args.skip_fetch:
            if week < fetch_from:
                print(f"  [fetch] Week {week} is before --from {fetch_from} – skipping fetch.")
            elif not args.refetch and rosters_fetched(season, week):
                print(f"  [fetch] Roster data for week {week} already on disk – skipping.")
            else:
                run(
                    [
                        sys.executable, SCRIPTS / "fetch_season.py",
                        "--season", str(season),
                        "--league", str(league),
                        "--weeks",  str(week),
                        "--skip-existing",
                    ],
                    f"Fetch  week {week}  (season {season}, league {league})",
                )

        # ── 2. BUILD TIMELINE ─────────────────────────────────────────
        # Always runs for every week (no skipping) so that player_scores.json
        # is written in chronological order without any gaps.
        if not args.only_html:
            run(
                [
                    sys.executable, SCRIPTS / "build_timelines.py",
                    "--season", str(season),
                    "--week",   str(week),
                ],
                f"Build timeline  week {week}",
            )

        # ── 3. GENERATE HTML ──────────────────────────────────────────
        timeline_path = DATA / str(season) / "timelines" / f"week_{week:02d}.json"
        run(
            [
                sys.executable, SCRIPTS / "generate_html.py",
                "--week",     str(week),
                "--timeline", str(timeline_path),
            ],
            f"Generate HTML  week {week}",
        )

    # ── done ──────────────────────────────────────────────────────────
    print(f"\n{'#' * 62}")
    print(f"  ✓  Pipeline complete  (weeks 1–{target_week} rebuilt)")
    print(f"  Open:  week_{target_week}_matchups.html")
    print(f"{'#' * 62}\n")


if __name__ == "__main__":
    main()
