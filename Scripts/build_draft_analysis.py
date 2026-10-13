#!/usr/bin/env python3
"""
build_draft_analysis.py  –  Compute Fantasy Points over Expected (FPOE).

Pipeline
--------
1.  Load draft.json (Yahoo draft pick order).
2.  Load player_scores.json (weekly fantasy points for all NFL players).
3.  Identify all rostered players across the season (from rosters/).
4.  Compute FPPG in two modes per player:
      injuries_off – denominator = weeks player appeared in nflverse player_stats
                     (only games where they were active; injuries not penalised)
      injuries_on  – denominator = total NFL weeks elapsed − team's bye weeks
                     (injuries count as zero-point games)
5.  Rank all rostered players by position FPPG; find replacement-level FPPG:
      QB23 · RB25 · WR25 · TE12 · K12 · DEF12
6.  FPOR = player FPPG − replacement FPPG  (can be negative)
7.  Fit an exponential-decay trendline to (pick#, FPOR) for drafted players:
      FPOR_trend(x) = a · exp(−b · x) + c
      (Grid search over b; solve for a and c analytically via least-squares.)
8.  FPOE = FPOR − FPOR_trend(pick#)
9.  Save Data/{season}/draft_analysis.json.

Usage
-----
    python Scripts/build_draft_analysis.py --season 2026 --week 4
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "Data"

# ── Replacement-level thresholds (1-indexed rank by FPPG among rostered players)
REPLACEMENT_RANK: dict[str, int] = {
    "QB":  23,
    "RB":  25,
    "WR":  25,
    "TE":  12,
    "K":   12,
    "DEF": 12,
}

POSITIONS = set(REPLACEMENT_RANK.keys())

# Yahoo slot indicators that are NOT real positions
_SLOT_INDICATORS = {"BN", "IR", "NA", "Q/W/R/T", "W/R/T", "W/R", "Q/W"}
_POS_REMAP = {"PK": "K", "LA": "LAR"}  # Yahoo sometimes uses PK for kicker

# nflverse → Yahoo team normalization (nflverse uses "LA" for Rams, etc.)
_NFL_TEAM_NORM: dict[str, str] = {
    "LA":  "LAR",
    "WAS": "WSH",
    "JAX": "JAX",
    "LV":  "LV",
    "WSH": "WSH",
}

# Reverse map: Yahoo/ESPN team codes → nflverse codes (for bye-week lookup)
_TO_NFLVERSE: dict[str, str] = {
    "LAR": "LA",
    "WSH": "WAS",
    "JAC": "JAX",
    "GNB": "GB",
    "KAN": "KC",
    "NOR": "NO",
    "NWE": "NE",
    "SFO": "SF",
    "TAM": "TB",
    "SDG": "LAC",
    "OAK": "LV",
    "LVR": "LV",
    "ARZ": "ARI",
    "CLV": "CLE",
    "HST": "HOU",
}


def _to_nflverse_team(team: str) -> str:
    t = team.upper().strip()
    return _TO_NFLVERSE.get(t, t)


def _norm_pos(pos: str) -> str:
    p = pos.upper().strip()
    return _POS_REMAP.get(p, p)


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── Data loaders ──────────────────────────────────────────────────────────────

def load_team_names(season: int) -> dict[str, str]:
    """Return {team_id: team_name} scraped from all roster files."""
    root = DATA / str(season) / "rosters"
    names: dict[str, str] = {}
    for week_dir in sorted(root.iterdir()):
        if not week_dir.is_dir():
            continue
        for tf in week_dir.glob("team_*.json"):
            d = _load(tf)
            tid = str(d.get("team_id", ""))
            tn  = d.get("team_name", "")
            if tid and tn:
                names[tid] = tn
    return names


def load_rostered_players(season: int, yahoo_all: dict) -> dict[str, dict]:
    """
    Return {yahoo_id: {name, pos, nfl_team}} for every player who appeared on
    any roster (starters or bench) across all weeks.  Uses the most-recently-
    seen values so name/team updates are captured.
    """
    root = DATA / str(season) / "rosters"
    players: dict[str, dict] = {}
    for week_dir in sorted(root.iterdir()):
        if not week_dir.is_dir():
            continue
        for tf in week_dir.glob("team_*.json"):
            d = _load(tf)
            for p in d.get("starters", []) + d.get("bench", []):
                pid = str(p.get("player_id", "")).strip()
                if not pid:
                    continue
                raw_pos = _norm_pos(p.get("position", ""))
                if raw_pos in _SLOT_INDICATORS or raw_pos not in POSITIONS:
                    # Slot indicator — look up real position from yahoo_all_players
                    raw_pos = _norm_pos(yahoo_all.get(pid, {}).get("position", ""))
                players[pid] = {
                    "name":     p.get("name", ""),
                    "pos":      raw_pos if raw_pos in POSITIONS else "",
                    "nfl_team": p.get("nfl_team", "").upper(),
                }
    log.info("Rostered players: %d unique", len(players))
    return players


def load_weekly_pts(season: int) -> tuple[dict[str, dict[int, float]], dict[str, str]]:
    """
    Return ({yahoo_id: {week: pts}}, {yahoo_id: image_url}) from player_scores.json.
    The image_url is the most-recently-seen NFL CDN headshot URL per player.
    """
    path = DATA / str(season) / "player_scores.json"
    raw  = _load(path)
    pts:   dict[str, dict[int, float]] = {}
    photos: dict[str, str] = {}
    for wk_str, pos_data in raw.items():
        wk = int(wk_str)
        for _pos, players in pos_data.items():
            for p in players:
                pid = str(p.get("player_id", "")).strip()
                if pid:
                    pts.setdefault(pid, {})[wk] = float(p.get("pts", 0))
                    url = p.get("image_url", "")
                    if url:
                        photos[pid] = url
    return pts, photos


def load_bye_weeks(season: int) -> dict[str, set[int]]:
    """
    Return {nflverse_team_code: {bye_week, ...}} using nflreadpy schedules.
    Falls back to empty dict (no byes assumed) on error.
    """
    try:
        import nflreadpy  # type: ignore
        import polars as pl  # type: ignore
        df  = nflreadpy.load_schedules(seasons=[season])
        reg = df.filter(pl.col("game_type") == "REG")
        all_teams = set(reg["home_team"].to_list()) | set(reg["away_team"].to_list())
        all_weeks = sorted(reg["week"].unique().to_list())
        bye_weeks: dict[str, set[int]] = {t: set() for t in all_teams}
        for wk in all_weeks:
            wk_teams = (
                set(reg.filter(pl.col("week") == wk)["home_team"].to_list()) |
                set(reg.filter(pl.col("week") == wk)["away_team"].to_list())
            )
            for team in all_teams - wk_teams:
                bye_weeks[team].add(wk)
        log.info("Bye weeks loaded for %d teams", len(bye_weeks))
        return bye_weeks
    except Exception as exc:
        log.warning("Could not load bye weeks via nflreadpy: %s", exc)
        return {}


def load_nflverse_played_weeks(
    season: int, player_id_map: dict
) -> dict[str, set[int]]:
    """
    Return {yahoo_id: {week, ...}} for weeks where the player appeared in
    nflverse player_stats (i.e. they were active and had at least one stat).
    """
    try:
        import nflreadpy  # type: ignore
        import polars as pl  # type: ignore
        df  = nflreadpy.load_player_stats(seasons=[season])
        reg = df.filter(pl.col("season_type") == "REG")

        # Build gsis_id → yahoo_id lookup
        gsis_to_yahoo: dict[str, str] = {
            gsis: entry["yahoo_id"]
            for gsis, entry in player_id_map.items()
            if entry.get("yahoo_id")
        }

        played: dict[str, set[int]] = {}
        for row in reg.select(["player_id", "week"]).iter_rows(named=True):
            gsis     = str(row["player_id"]).strip()
            yahoo_id = gsis_to_yahoo.get(gsis)
            if yahoo_id:
                played.setdefault(yahoo_id, set()).add(int(row["week"]))

        log.info("nflverse played-weeks: %d players mapped", len(played))
        return played
    except Exception as exc:
        log.warning("Could not load nflverse player_stats: %s", exc)
        return {}


# ── FPPG computation ──────────────────────────────────────────────────────────

def compute_fppg(
    pid: str,
    info: dict,
    pts_by_week: dict[str, dict[int, float]],
    current_week: int,
    bye_weeks: dict[str, set[int]],
    nfl_played: dict[str, set[int]],
    mode: str,
) -> tuple[float, int]:
    """
    Return (fppg, games_played).

    mode='injuries_on'  – denominator = total NFL weeks elapsed − team byes
    mode='injuries_off' – denominator = weeks appeared in nflverse player_stats
                          (DEF uses team schedule; fallback = weeks pts > 0)
    """
    wk_pts   = pts_by_week.get(pid, {})
    total_pts = sum(v for wk, v in wk_pts.items() if wk <= current_week)
    pos = info.get("pos", "")

    # Bye weeks for this player's team (in nflverse codes)
    nflv_team = _to_nflverse_team(info.get("nfl_team", ""))
    team_byes = {w for w in bye_weeks.get(nflv_team, set()) if w <= current_week}

    if mode == "injuries_on":
        games = max(1, current_week - len(team_byes))
    else:
        # injuries_off: snap-based
        if pos == "DEF":
            games = max(1, current_week - len(team_byes))
        else:
            active_weeks = {w for w in nfl_played.get(pid, set()) if w <= current_week}
            if active_weeks:
                games = len(active_weeks)
            else:
                # Fallback: weeks with pts > 0 (omits true 0-pt active weeks)
                games = max(1, len({w for w in wk_pts if w <= current_week and wk_pts[w] > 0}))

    fppg = round(total_pts / games, 4) if games > 0 else 0.0
    return fppg, games


# ── Exponential trendline fitting (numpy only, no scipy) ──────────────────────

def fit_exponential(xs: list[float], ys: list[float]) -> tuple[float, float, float] | None:
    """
    Fit  f(x) = a · exp(−b · x) + c  using a 1-D grid search over b.

    For each candidate b we solve the linear system [exp(−b·x), 1] · [a, c]' = y
    via numpy least-squares, keeping only solutions with a > 0 (decreasing curve).
    Returns (a, b, c) for the b with the smallest residual sum-of-squares, or
    None if no valid fit is found.
    """
    x = np.array(xs, dtype=float)
    y = np.array(ys, dtype=float)

    best: tuple[float, float, float] | None = None
    best_rss = float("inf")

    # Search b in [0.001, 0.15] — covers "half-life" from ~5 picks to 693 picks
    for b in np.linspace(0.001, 0.15, 500):
        e = np.exp(-b * x)
        A = np.column_stack([e, np.ones_like(x)])
        result = np.linalg.lstsq(A, y, rcond=None)
        a_est, c_est = float(result[0][0]), float(result[0][1])
        if a_est <= 0:
            continue  # reject — we want a decreasing curve
        rss = float(np.sum((a_est * e + c_est - y) ** 2))
        if rss < best_rss:
            best_rss = rss
            best = (a_est, float(b), c_est)

    return best


def trendline_val(pick: int, params: tuple[float, float, float]) -> float:
    a, b, c = params
    return a * math.exp(-b * pick) + c


# ── Main build ────────────────────────────────────────────────────────────────

def build(season: int, current_week: int) -> dict:
    draft_path = DATA / str(season) / "draft.json"
    if not draft_path.exists():
        log.error(
            "draft.json not found at %s.\n"
            "Run:  python Scripts/fetch_season.py "
            "--season %d --league <ID> --fetch-draft --skip-existing",
            draft_path, season,
        )
        sys.exit(1)

    draft_picks: list[dict] = _load(draft_path)

    yahoo_all_path = DATA / str(season) / "yahoo_all_players.json"
    yahoo_all: dict = _load(yahoo_all_path) if yahoo_all_path.exists() else {}

    id_map_path = DATA / "player_id_map.json"
    player_id_map: dict = _load(id_map_path) if id_map_path.exists() else {}

    team_names = load_team_names(season)
    rostered   = load_rostered_players(season, yahoo_all)
    pts_by_week, player_photos = load_weekly_pts(season)
    bye_weeks   = load_bye_weeks(season)
    nfl_played  = load_nflverse_played_weeks(season, player_id_map)

    log.info(
        "Inputs: %d draft picks · %d rostered · %d in player_id_map · "
        "%d with nflverse snap data",
        len(draft_picks), len(rostered), len(player_id_map), len(nfl_played),
    )

    # Dict keyed by pick#
    draft_by_player: dict[str, dict] = {str(p["player_id"]): p for p in draft_picks}

    # ── Step 1: FPPG for all rostered players ─────────────────────────────
    player_out: dict[str, dict] = {}
    for pid, info in rostered.items():
        if info["pos"] not in POSITIONS:
            log.debug("Skipping %s (%s) — unknown position", info["name"], pid)
            continue
        total_pts = sum(
            v for wk, v in pts_by_week.get(pid, {}).items() if wk <= current_week
        )
        entry: dict = {
            "name":      info["name"],
            "pos":       info["pos"],
            "nfl_team":  info["nfl_team"],
            "total_pts": round(total_pts, 2),
        }
        for mode in ("injuries_on", "injuries_off"):
            fppg, games = compute_fppg(
                pid, info, pts_by_week, current_week, bye_weeks, nfl_played, mode
            )
            entry[mode] = {"fppg": fppg, "games_played": games}
        player_out[pid] = entry

    # ── Step 2: Replacement-level FPPG per position ───────────────────────
    replacement: dict[str, dict[str, float]] = {}
    for mode in ("injuries_on", "injuries_off"):
        replacement[mode] = {}
        for pos, rank in REPLACEMENT_RANK.items():
            ranked = sorted(
                [v[mode]["fppg"] for v in player_out.values() if v["pos"] == pos],
                reverse=True,
            )
            idx = rank - 1
            repl_fppg = ranked[idx] if idx < len(ranked) else (ranked[-1] if ranked else 0.0)
            replacement[mode][pos] = round(repl_fppg, 4)
            log.info(
                "  Replacement %-12s %-3s  rank=%2d / %-2d  FPPG=%.2f",
                mode, pos, rank, len(ranked), repl_fppg,
            )

    # ── Step 3: FPOR for all rostered players ─────────────────────────────
    for pid, entry in player_out.items():
        pos = entry["pos"]
        for mode in ("injuries_on", "injuries_off"):
            repl  = replacement[mode].get(pos, 0.0)
            fpor  = round(entry[mode]["fppg"] - repl, 4)
            entry[mode]["fpor"] = fpor

    # ── Step 4: Exponential trendline (drafted players only) ──────────────
    trendline_params: dict[str, dict | None] = {}
    trendline_std: dict[str, float | None]   = {}

    for mode in ("injuries_on", "injuries_off"):
        xs, ys = [], []
        for pid, entry in player_out.items():
            if pid in draft_by_player:
                xs.append(float(draft_by_player[pid]["pick"]))
                ys.append(entry[mode]["fpor"])

        params = fit_exponential(xs, ys) if len(xs) >= 4 else None
        if params:
            trendline_params[mode] = {"a": round(params[0], 4),
                                       "b": round(params[1], 6),
                                       "c": round(params[2], 4)}
            residuals = [ys[i] - trendline_val(int(xs[i]), params) for i in range(len(xs))]
            std = math.sqrt(sum(r * r for r in residuals) / len(residuals))
            trendline_std[mode] = round(std, 4)
            log.info(
                "  Trendline %-12s  a=%.2f  b=%.5f  c=%.2f  residual_std=%.2f",
                mode, *params, std,
            )
        else:
            trendline_params[mode] = None
            trendline_std[mode]    = None
            log.warning("  Trendline %s: fit failed (need ≥4 drafted players with data)", mode)

    # ── Step 5: FPOE for all drafted players ──────────────────────────────
    picks_out: list[dict] = []
    for pick_info in draft_picks:
        pid   = str(pick_info["player_id"])
        tid   = str(pick_info["team_id"])
        pick  = int(pick_info["pick"])
        round_num = int(pick_info["round"])

        entry = player_out.get(pid, {})
        name  = entry.get("name") or yahoo_all.get(pid, {}).get("name", f"Unknown ({pid})")
        pos   = entry.get("pos") or _norm_pos(yahoo_all.get(pid, {}).get("position", ""))

        nfl_team = entry.get("nfl_team", "")
        # Player photo: use player_scores image_url; for DEF fall back to ESPN CDN logo
        photo_url = player_photos.get(pid, "")
        if not photo_url and _norm_pos(pos) == "DEF" and nfl_team:
            photo_url = (
                f"https://a.espncdn.com/i/teamlogos/nfl/500/{nfl_team.lower()}.png"
            )

        pick_out: dict = {
            "pick":        pick,
            "round":       round_num,
            "team_id":     tid,
            "team_name":   team_names.get(tid, f"Team {tid}"),
            "player_id":   pid,
            "player_name": name,
            "position":    pos,
            "nfl_team":    nfl_team,
            "total_pts":   entry.get("total_pts", 0.0),
            "photo_url":   photo_url,
        }

        for mode in ("injuries_on", "injuries_off"):
            mode_data = entry.get(mode, {})
            fpor      = mode_data.get("fpor")
            params    = trendline_params.get(mode)

            if fpor is not None and params:
                p = (params["a"], params["b"], params["c"])
                tv   = round(trendline_val(pick, p), 4)
                fpoe = round(fpor - tv, 4)
            else:
                tv   = None
                fpoe = None

            pick_out[mode] = {
                "games_played": mode_data.get("games_played", 0),
                "fppg":         mode_data.get("fppg", 0.0),
                "fpor":         fpor if fpor is not None else 0.0,
                "trend_val":    tv,
                "fpoe":         fpoe,
            }

        picks_out.append(pick_out)

    return {
        "season":        season,
        "through_week":  current_week,
        "replacement":   replacement,
        "trendline":     trendline_params,
        "trendline_std": trendline_std,
        "picks":         picks_out,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Compute FPOE draft analysis and save draft_analysis.json.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--season", type=int, required=True, help="NFL season year, e.g. 2026")
    ap.add_argument("--week",   type=int, required=True,
                    help="Most recent fully-completed NFL week to include")
    args = ap.parse_args()

    result = build(args.season, args.week)

    out = DATA / str(args.season) / "draft_analysis.json"
    _save(out, result)

    n_picks = len(result["picks"])
    log.info("Saved → %s  (%d picks)", out, n_picks)
    for mode in ("injuries_on", "injuries_off"):
        p = result["trendline"].get(mode)
        s = result["trendline_std"].get(mode)
        if p:
            log.info("  %-12s  a=%.2f  b=%.5f  c=%.2f  std=%.2f", mode, p["a"], p["b"], p["c"], s)


if __name__ == "__main__":
    main()
