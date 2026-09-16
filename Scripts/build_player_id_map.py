"""
build_player_id_map.py
======================
Build a static GSIS → Yahoo player-ID crosswalk and save it to
Data/player_id_map.json.

The nflverse play-by-play dataset identifies every player by their GSIS ID
(e.g. "00-0037840").  Yahoo Fantasy rosters identify players by a separate
numeric Yahoo ID (e.g. "34120").  This script bridges the two using the
nflreadpy.load_ff_playerids() table, which includes both IDs for ~5,000
current/recent players.

Output format
-------------
Data/player_id_map.json  — {gsis_id: {"yahoo_id": str|null, "name": str, "position": str}}

Players with no Yahoo ID (unrostered rookies, retired players, DEF specialists)
still appear in the map so we know the lookup was attempted.  When yahoo_id is
null, build_timelines.py falls back to name-based matching.

Manual overrides
----------------
If a player's mapping is ambiguous or wrong, add an entry to the optional
Data/player_id_overrides.json file:

    {"00-0037840": {"yahoo_id": "34120", "name": "Kyren Williams", "position": "RB"}}

Overrides take precedence over the auto-generated map.

Usage
-----
    python Scripts/build_player_id_map.py
    python Scripts/build_player_id_map.py --verbose   # print every GSIS row
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

HERE   = Path(__file__).resolve().parent
DATA   = HERE.parent / "Data"
OUTPUT = DATA / "player_id_map.json"
OVERRIDES = DATA / "player_id_overrides.json"


def _require_deps() -> None:
    try:
        import nflreadpy   # noqa: F401
        import polars      # noqa: F401
    except ImportError as exc:
        sys.exit(f"Missing dependency: {exc}.  Run: pip install nflreadpy polars")


def build_map(verbose: bool = False) -> dict[str, dict]:
    """Return {gsis_id: {yahoo_id, name, position}} from nflreadpy."""
    import nflreadpy
    import polars as pl
    import warnings
    warnings.filterwarnings("ignore")

    log.info("Loading ff_playerids from nflreadpy …")
    ff = nflreadpy.load_ff_playerids()

    # Keep only rows that have a GSIS ID (our primary key)
    ff_with_gsis = ff.filter(pl.col("gsis_id").is_not_null())
    log.info("  %d rows with gsis_id (of %d total)", ff_with_gsis.height, ff.height)

    result: dict[str, dict] = {}
    for row in ff_with_gsis.iter_rows(named=True):
        gsis    = str(row["gsis_id"]).strip()
        yahoo   = str(row["yahoo_id"]).strip() if row["yahoo_id"] is not None else None
        name    = str(row["name"] or "").strip()
        pos     = str(row["position"] or "").strip()
        team    = str(row["team"] or "").strip()

        if not gsis:
            continue

        entry = {"yahoo_id": yahoo, "name": name, "position": pos, "team": team}
        result[gsis] = entry

        if verbose:
            log.debug("  %s  %-30s  %3s  %-4s  yahoo=%s",
                      gsis, name, pos, team, yahoo or "—")

    return result


def build_league_supplement(rosters_root: Path) -> dict[str, str]:
    """
    Build a supplemental GSIS → Yahoo ID map from our own Yahoo roster files.

    nflreadpy.load_ff_playerids() often lacks Yahoo IDs for recent rookies
    because the nflverse community crosswalk lags a few months.  Our Yahoo
    Fantasy roster JSON files already contain the correct Yahoo player IDs for
    every player we've ever rostered.  This function bridges them by:

      1. Collecting (full_name, yahoo_id) from all Yahoo roster files.
      2. Matching each name against nflreadpy.load_rosters(2025) to get the
         player's GSIS ID.

    Returns {gsis_id: yahoo_id_str} for all matched players.
    """
    import nflreadpy
    import polars as pl
    import warnings
    warnings.filterwarnings("ignore")

    # ── nflverse 2025 roster: full_name → gsis_id ─────────────────────────
    log.info("Loading nflreadpy.load_rosters(2025) for league supplement …")
    nfl_r = nflreadpy.load_rosters(seasons=[2025])
    name_to_gsis: dict[str, str] = {}
    for row in nfl_r.iter_rows(named=True):
        name = (row.get("full_name") or "").strip()
        gsis = row.get("gsis_id")
        if name and gsis:
            name_to_gsis[name.lower()] = str(gsis)
    log.info("  %d name→gsis entries from nflverse 2025 roster", len(name_to_gsis))

    # ── Our Yahoo rosters: full_name → yahoo_id ───────────────────────────
    # rosters_root is Data/<season>/rosters/ which contains week_NN/ subdirs.
    yahoo_name_to_id: dict[str, str] = {}
    for week_dir in sorted(rosters_root.iterdir()):
        if not week_dir.is_dir():
            continue
        for f in week_dir.glob("team_*.json"):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            for p in d.get("starters", []) + d.get("bench", []):
                name = (p.get("name") or "").strip()
                pid  = str(p.get("player_id") or "")
                pos  = p.get("position", "")
                # skip team defenses (100xxx IDs) and unnamed entries
                if name and pid and not pid.startswith("100"):
                    yahoo_name_to_id[name.lower()] = pid

    log.info("  %d unique players in Yahoo roster files", len(yahoo_name_to_id))

    # ── Match by full name ─────────────────────────────────────────────────
    supplement: dict[str, str] = {}
    unmatched: list[str] = []
    for name_lower, yahoo_id in sorted(yahoo_name_to_id.items()):
        # Try exact match first, then strip generational suffixes
        gsis = name_to_gsis.get(name_lower)
        if gsis is None:
            # Strip " Jr.", " Sr.", " II", " III", " IV" etc. from end
            import re
            stripped = re.sub(
                r"\s+(jr\.?|sr\.?|ii+|iv|v?i*)\s*$", "", name_lower, flags=re.I
            ).strip()
            gsis = name_to_gsis.get(stripped)
        if gsis:
            supplement[gsis] = yahoo_id
        else:
            unmatched.append(name_lower)

    log.info("  Supplement: %d matched, %d unmatched (teams/retiredplayers)",
             len(supplement), len(unmatched))
    if unmatched:
        log.debug("  Unmatched: %s", unmatched)
    return supplement


def apply_league_supplement(player_map: dict[str, dict],
                            supplement: dict[str, str]) -> dict[str, dict]:
    """
    Merge the league-specific GSIS → Yahoo ID supplement into player_map.

    - For GSIS IDs already in the map with yahoo_id=None, fill in the Yahoo ID.
    - For GSIS IDs already in the map with a Yahoo ID, warn on mismatch.
    - For new GSIS IDs, add them.
    """
    filled  = 0
    added   = 0
    mismatched = 0
    for gsis, yahoo_id in supplement.items():
        if gsis in player_map:
            existing = player_map[gsis].get("yahoo_id")
            if existing is None:
                player_map[gsis]["yahoo_id"] = yahoo_id
                filled += 1
            elif existing != yahoo_id:
                log.warning("  ID mismatch for gsis=%s: ff_playerids=%s vs league=%s",
                            gsis, existing, yahoo_id)
                mismatched += 1
        else:
            player_map[gsis] = {"yahoo_id": yahoo_id, "name": "", "position": "", "team": ""}
            added += 1
    log.info("Supplement applied: %d filled, %d new entries, %d mismatches",
             filled, added, mismatched)
    return player_map


def apply_overrides(player_map: dict[str, dict]) -> dict[str, dict]:
    """Merge manual overrides on top of the auto-generated map."""
    if not OVERRIDES.exists():
        return player_map
    overrides = json.loads(OVERRIDES.read_text())
    log.info("Applying %d manual overrides from %s", len(overrides), OVERRIDES.name)
    for gsis, entry in overrides.items():
        if gsis in player_map:
            player_map[gsis].update(entry)
        else:
            player_map[gsis] = entry
    return player_map


def main() -> None:
    ap = argparse.ArgumentParser(description="Build GSIS → Yahoo player ID crosswalk.")
    ap.add_argument("--verbose", action="store_true", help="Print every row as it's processed.")
    ap.add_argument("--output", default=str(OUTPUT), help="Output JSON file path.")
    args = ap.parse_args()

    _require_deps()

    player_map = build_map(verbose=args.verbose)

    # Add Yahoo IDs for rostered players missing from the ff_playerids crosswalk
    # (primarily 2025 rookies whose entries haven't been updated yet).
    rosters_root = DATA / "2025" / "rosters"
    if rosters_root.exists():
        supplement = build_league_supplement(rosters_root)
        player_map = apply_league_supplement(player_map, supplement)
    else:
        log.warning("No rosters directory found at %s — skipping league supplement", rosters_root)

    player_map = apply_overrides(player_map)

    with_yahoo   = sum(1 for v in player_map.values() if v["yahoo_id"])
    without_yahoo = len(player_map) - with_yahoo

    log.info("Map built: %d entries total, %d with Yahoo ID, %d without",
             len(player_map), with_yahoo, without_yahoo)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(player_map, separators=(",", ":")))
    log.info("Saved → %s", out_path)

    # Quick sanity check: look up known players
    checks = [
        ("00-0037840", "Kyren Williams",  "34120"),
        ("00-0040131", "Kyle Williams",   None),      # unrostered NE WR — should be None
        ("00-0035640", "DK Metcalf",      "31896"),
    ]
    log.info("")
    log.info("Sanity checks:")
    for gsis, expected_name, expected_yahoo in checks:
        entry = player_map.get(gsis, {})
        got_yahoo = entry.get("yahoo_id")
        got_name  = entry.get("name", "NOT FOUND")
        ok  = (got_yahoo == expected_yahoo)
        sym = "✓" if ok else "✗"
        log.info("  %s  %-20s  yahoo=%s  (expected %s)",
                 sym, got_name, got_yahoo or "None", expected_yahoo or "None")


if __name__ == "__main__":
    main()
