#!/usr/bin/env python3
"""
debug_scoring.py  –  Trace every play-level scoring event for one team/week.

Usage:
    python Scripts/debug_scoring.py --season 2025 --week 1 --team "The Chop Block"
    python Scripts/debug_scoring.py --season 2025 --week 1 --team "The Chop Block" --out /tmp/debug.html
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from lib import espn as espn_lib
from lib.scoring import (
    LEAGUE_RULES, normalize_name, espn_abbrev_key, yahoo_abbrev_key,
    normalize_nfl_team, compute_pts_from_stats, pts_allowed_bonus,
)

_ET = ZoneInfo("America/New_York")

STAT_LABELS = {
    "pass_yards":      "Pass Yds",   "pass_td":        "Pass TD",
    "int_thrown":      "INT",        "pass_td_40plus": "Pass TD 40+ bonus",
    "pass_yards_300":  "300 Pass Yd bonus", "pass_yards_400": "400 Pass Yd bonus",
    "pass_yards_500":  "500 Pass Yd bonus",
    "rush_yards":      "Rush Yds",   "rush_td":        "Rush TD",
    "rush_td_40plus":  "Rush TD 40+ bonus",
    "rush_yards_100":  "100 Rush Yd bonus", "rush_yards_150": "150 Rush Yd bonus",
    "rush_yards_200":  "200 Rush Yd bonus",
    "receptions":      "Reception",  "rec_yards":      "Rec Yds",
    "rec_td":          "Rec TD",     "rec_td_40plus":  "Rec TD 40+ bonus",
    "rec_yards_100":   "100 Rec Yd bonus",  "rec_yards_150": "150 Rec Yd bonus",
    "rec_yards_200":   "200 Rec Yd bonus",
    "two_pt_conversion":"2PT Conv",  "fumble_lost":    "Fumble Lost",
    "return_td":       "Return TD",  "off_fumble_ret_td": "Off Fum Ret TD",
    "fg_0_19":         "FG 0-19",    "fg_20_29":       "FG 20-29",
    "fg_30_39":        "FG 30-39",   "fg_40_49":       "FG 40-49",
    "fg_50_plus":      "FG 50+",
    "fg_miss_0_19":    "FG Miss 0-19","fg_miss_20_29":  "FG Miss 20-29",
    "fg_miss_30_39":   "FG Miss 30-39","fg_miss_40_49": "FG Miss 40-49",
    "pat_made":        "PAT Made",   "pat_missed":     "PAT Missed",
    "def_sack":        "DEF Sack",   "def_int":        "DEF INT",
    "def_fumble_rec":  "DEF Fum Rec","def_td":         "DEF TD",
    "def_safety":      "DEF Safety", "def_blocked_kick":"DEF Blk Kick",
    "def_return_td":   "DEF Ret TD", "def_4th_down_stop":"DEF 4th Stop",
    "pts_allowed":     "Pts Allowed",
}


def fmt_time(wc_str: str) -> str:
    try:
        dt = datetime.fromisoformat(wc_str.replace("Z", "+00:00"))
        return dt.astimezone(_ET).strftime("%a %-I:%M%p")
    except Exception:
        return wc_str or ""


def fmt_val(stat: str, val) -> str:
    if ("yards" in stat
            and not any(x in stat for x in ("100","150","200","300","400","500","bonus"))):
        return f"{val} yds"
    if stat.startswith("def_") or stat in (
            "pass_td","rush_td","rec_td","receptions","fumble_lost",
            "int_thrown","two_pt_conversion","return_td","off_fumble_ret_td"):
        return "1"
    if stat.startswith("fg_") or stat.startswith("pat_"):
        return f"{val} yds"
    return str(val)


def run(season: int, week: int, team_name: str) -> dict:
    root = _HERE.parent / "Data" / str(season)

    # ── Roster ────────────────────────────────────────────────────────────────
    roster_path = root / "rosters" / f"week_{week:02d}"
    target_roster = None
    target_tid = None
    all_rosters = {}
    for fn in sorted(roster_path.glob("*.json")):
        r = json.loads(fn.read_text())
        all_rosters[r["team_id"]] = r
        if r.get("team_name", "").lower() == team_name.lower():
            target_roster = r
            target_tid = r["team_id"]
    if not target_roster:
        raise ValueError(f"Team '{team_name}' not found in week {week} rosters")

    starters = target_roster["starters"]

    # ── Build name indices ─────────────────────────────────────────────────────
    abbrev_to_nfl = defaultdict(dict)   # abbr -> {nfl: canonical_name or None}
    full_to_name  = {}                  # normalized full -> canonical name
    nfl_team_set  = set()               # NFL teams our skill players are on
    def_nfl       = None
    def_name      = None
    kick_nfl      = None                # NFL team for our kicker slot
    kick_name     = None                # Yahoo name of our kicker

    for player in starters:
        pname = player.get("name", "")
        nfl   = player.get("nfl_team", "").upper()
        pos   = player.get("position", "")
        full_to_name[normalize_name(pname)] = pname
        abbr = yahoo_abbrev_key(pname)
        if abbr and nfl:
            abbrev_to_nfl[abbr][nfl] = pname
            # Also index by short initial so ESPN's "A.Brown" matches Yahoo's "A.J. Brown"
            parts = abbr.split()
            if parts and len(parts[0]) > 1:
                short = parts[0][0] + " " + " ".join(parts[1:])
                abbrev_to_nfl[short].setdefault(nfl, pname)
        if pos == "DEF":
            def_nfl  = nfl
            def_name = pname
        elif pos == "K":
            kick_nfl  = nfl
            kick_name = pname
            nfl_team_set.add(nfl)
        elif nfl:
            nfl_team_set.add(nfl)

    # Index ALL rostered players (all teams, starters + bench) for NFL-team
    # lookups.  This lets us resolve e.g. "A.Rodgers" → PIT even when he is on
    # another team's bench, so sack attribution is correct.
    for tid, roster in all_rosters.items():
        for player in roster.get("starters", []) + roster.get("bench", []):
            pname = player.get("name", "")
            nfl   = player.get("nfl_team", "").upper()
            if not (pname and nfl):
                continue
            abbr = yahoo_abbrev_key(pname)
            if abbr:
                abbrev_to_nfl[abbr].setdefault(nfl, None)
                parts = abbr.split()
                if parts and len(parts[0]) > 1:
                    short = parts[0][0] + " " + " ".join(parts[1:])
                    abbrev_to_nfl[short].setdefault(nfl, None)
            nn = normalize_name(pname)
            if nn and nn not in full_to_name:
                full_to_name[nn] = None

    # ── Schedule ──────────────────────────────────────────────────────────────
    schedule = json.loads((root / "schedule.json").read_text()).get(str(week), [])
    # Normalise ESPN abbreviations (WSH→WAS etc.) so they match Yahoo roster data
    game_nfl_teams = {
        g["game_id"]: {
            "home": normalize_nfl_team(g.get("home", "")),
            "away": normalize_nfl_team(g.get("away", "")),
        }
        for g in schedule
    }

    # ── Yahoo official total ───────────────────────────────────────────────────
    yahoo_total = None
    matchups_all = json.loads((root / "matchups.json").read_text())
    for m in matchups_all.get(str(week), []):
        if m.get("team1_id") == target_tid:
            yahoo_total = m.get("team1_score"); break
        if m.get("team2_id") == target_tid:
            yahoo_total = m.get("team2_score"); break

    # ── Helper: find which NFL team a (possibly unknown) player is on ──────────
    def nfl_of(espn_name: str, game_nfl: set):
        abbr = espn_abbrev_key(espn_name)
        if abbr:
            nfl_opts = abbrev_to_nfl.get(abbr, {})
            matches = {nfl: v for nfl, v in nfl_opts.items() if nfl in game_nfl} \
                      if game_nfl else nfl_opts
            if len(matches) == 1:
                return next(iter(matches.keys()))
        return None

    # ── Process all plays using stat-accumulation ─────────────────────────────
    # Mirrors build_timelines.py exactly: compute_pts_from_stats handles milestones
    # inline so no separate milestone pass is needed.
    pbp_dir = root / "pbp" / f"week_{week:02d}"
    rows: list = []
    player_stats:     dict = defaultdict(lambda: defaultdict(float))
    player_pts_cache: dict = defaultdict(float)
    player_totals:    dict = defaultdict(float)
    team_total = 0.0
    play_seq   = 0

    def accum(pname: str, stat: str, value: float, wc: str, row_evs: list):
        nonlocal team_total
        old_pts = player_pts_cache[pname]
        player_stats[pname][stat] = player_stats[pname].get(stat, 0.0) + value
        new_pts = compute_pts_from_stats(player_stats[pname], LEAGUE_RULES)
        delta   = round(new_pts - old_pts, 4)
        player_pts_cache[pname] = new_pts
        if delta == 0:
            return
        player_totals[pname] = round(player_totals[pname] + delta, 2)
        team_total = round(team_total + delta, 2)
        row_evs.append({
            "player":       pname,
            "stat":         stat,
            "stat_label":   STAT_LABELS.get(stat, stat),
            "value":        value,
            "value_fmt":    fmt_val(stat, value),
            "pts":          round(delta, 2),
            "player_total": player_totals[pname],
        })

    for gfile in sorted(pbp_dir.glob("*.json")):
        gid      = gfile.stem
        plays    = json.loads(gfile.read_text())
        gteams   = game_nfl_teams.get(gid, {})
        game_nfl = {gteams.get("home",""), gteams.get("away","")} - {""}
        has_def  = def_nfl is not None and def_nfl in game_nfl

        # No early-skip: traded players' roster entries have the wrong NFL team,
        # so we must process every game and rely on the trade fallback in the
        # matching logic below.

        for play_idx, play in enumerate(plays):
            wc = play.get("wallclock")
            if not wc:
                continue

            events = espn_lib.extract_fantasy_events(play)
            row_evs: list = []

            for ev in events:
                pname_espn = ev["player_name"]
                stat       = ev["stat"]
                value      = ev.get("value", 0)

                # ── Team-defense events ──────────────────────────────────
                if pname_espn == "__DEFENSE__":
                    if not has_def:
                        continue
                    off_name          = ev.get("_off_player") or ""
                    ret_name          = ev.get("_ret_player") or ""
                    def_player        = ev.get("_def_player") or ""
                    explicit_def_team = normalize_nfl_team(ev.get("_explicit_def_team") or "")
                    target = None

                    # Explicit team name from play text (e.g. "RECOVERED by PIT-").
                    if explicit_def_team == def_nfl:
                        target = def_name

                    if target is None and off_name:
                        off_nfl = nfl_of(off_name, game_nfl)
                        if off_nfl:
                            # We know the offensive player's team → DEF = other team.
                            others = game_nfl - {off_nfl}
                            if len(others) == 1 and next(iter(others)) == def_nfl:
                                target = def_name
                            # off_player resolved → no fallback; skip if not our DEF
                            if target is None:
                                continue
                        else:
                            # off_player present but NFL team unresolvable.
                            # Try the defensive player hint (e.g. sacker's name).
                            if def_player:
                                dp_nfl = nfl_of(def_player, game_nfl)
                                if dp_nfl == def_nfl:
                                    target = def_name
                            if target is None:
                                continue
                    elif ret_name:
                        ret_nfl = nfl_of(ret_name, game_nfl)
                        if ret_nfl == def_nfl:
                            target = def_name
                        elif def_nfl in game_nfl:
                            target = def_name
                    elif def_nfl in game_nfl:
                        # No hint at all (e.g. safety): credit our DEF
                        target = def_name
                    if target:
                        accum(target, stat, value, wc, row_evs)
                    continue

                # ── Kicker events ────────────────────────────────────────
                if stat in ("pat_made", "pat_missed"):
                    abbr = espn_abbrev_key(pname_espn)
                    matched = None
                    if abbr:
                        nfl_opts = abbrev_to_nfl.get(abbr, {})
                        ms = {nfl: v for nfl, v in nfl_opts.items() if nfl in game_nfl} \
                             if game_nfl else nfl_opts
                        if len(ms) == 1:
                            matched = next(iter(ms.values()))
                    if matched is None:
                        matched = full_to_name.get(normalize_name(pname_espn))
                    if matched is None and kick_nfl and kick_name:
                        # Fall back to team-based kicker matching
                        scorer = ev.get("_td_scorer", "")
                        scorer_nfl = nfl_of(scorer, game_nfl) if scorer else None
                        if scorer_nfl == kick_nfl:
                            matched = kick_name
                        elif scorer_nfl is None and kick_nfl in game_nfl:
                            matched = kick_name
                    if matched:
                        accum(matched, stat, value, wc, row_evs)
                    continue

                # ── Skill-position events ────────────────────────────────
                abbr = espn_abbrev_key(pname_espn)
                matched = None
                if abbr:
                    nfl_opts = abbrev_to_nfl.get(abbr, {})
                    ms = {nfl: full for nfl, full in nfl_opts.items() if nfl in game_nfl} \
                         if game_nfl else nfl_opts
                    if len(ms) == 1:
                        matched = next(iter(ms.values()))
                    # Trade fallback: roster may have wrong NFL team (mid-season trade).
                    # Use player's fantasy team if unambiguous even if NFL team changed.
                    if matched is None:
                        real_vals = {v for v in nfl_opts.values() if v is not None}
                        if len(real_vals) == 1:
                            matched = next(iter(real_vals))
                if matched is None:
                    matched = full_to_name.get(normalize_name(pname_espn))
                if matched is None:
                    continue

                accum(matched, stat, value, wc, row_evs)

            if row_evs:
                play_seq += 1
                rows.append({
                    "seq":         play_seq,
                    "play_id":     f"{gid}:{play_idx}",
                    "time":        fmt_time(wc),
                    "description": play.get("play_text", ""),
                    "events":      row_evs,
                    "team_total":  team_total,
                })

    # ── Defense pts-allowed (game-level, added at game end) ───────────────────
    defense_row = None
    if def_nfl:
        for game_meta in schedule:
            home = normalize_nfl_team(game_meta.get("home", ""))
            away = normalize_nfl_team(game_meta.get("away", ""))
            if def_nfl not in (home, away):
                continue
            gid      = game_meta["game_id"]
            pbp_file = pbp_dir / f"{gid}.json"
            if not pbp_file.exists():
                continue
            plays_def = json.loads(pbp_file.read_text())
            last_wc   = next((p["wallclock"] for p in reversed(plays_def)
                              if p.get("wallclock")), None)

            # Use actual final scores stored in schedule.json when available
            if def_nfl == home:
                opp_pts = game_meta.get("away_score")
            else:
                opp_pts = game_meta.get("home_score")
            # Fall back to PBP counting for older cached schedules without scores
            if opp_pts is None:
                opp_pts = sum(
                    6 if p.get("scoring_type") == "TD" else
                    3 if p.get("scoring_type") == "FG" else
                    1 if p.get("scoring_type") == "PAT" else
                    2 if p.get("scoring_type") == "SF" else 0
                    for p in plays_def if p.get("is_scoring")
                ) // 2

            bonus = pts_allowed_bonus(int(opp_pts), LEAGUE_RULES)
            dn    = def_name or "Defense"
            player_totals[dn] = round(player_totals[dn] + bonus, 2)
            team_total = round(team_total + bonus, 2)
            defense_row = {
                "player":       dn,
                "opp_pts":      int(opp_pts),
                "pts":          round(bonus, 2),
                "player_total": player_totals[dn],
                "team_total":   team_total,
                "time":         fmt_time(last_wc) if last_wc else "?",
            }
            break

    return {
        "team_name":           team_name,
        "starters":            starters,
        "rows":                rows,
        "defense_row":         defense_row,
        "reconstructed_total": round(team_total, 2),
        "yahoo_total":         yahoo_total,
        "player_totals":       dict(player_totals),
    }


# ─── HTML output ──────────────────────────────────────────────────────────────

CSS = """
body { font-family:'Segoe UI',system-ui,sans-serif; background:#0f1419; color:#e6edf3;
       padding:1.5rem; font-size:13px; }
h1 { color:#58a6ff; margin-bottom:.25rem; }
h2 { color:#79c0ff; margin-top:2rem; font-size:1rem; }
.summary { color:#8b949e; margin-bottom:1.5rem; }
.totals { display:flex; gap:2rem; flex-wrap:wrap; margin:1rem 0 2rem; }
.totals .box { background:#1a2332; border:1px solid #2d3a4f; border-radius:8px;
               padding:.6rem 1.2rem; }
.totals .box .label { color:#8b949e; font-size:.8rem; }
.totals .box .val { font-size:1.5rem; font-weight:700; color:#e6edf3; }
.totals .box .val.green { color:#3fb950; }
.totals .box .val.red   { color:#f85149; }
table { border-collapse:collapse; width:100%; margin-bottom:2rem; }
th { background:#1a2332; color:#8b949e; font-weight:600; font-size:.78rem;
     text-transform:uppercase; letter-spacing:.03em; padding:.5rem .7rem;
     border-bottom:2px solid #2d3a4f; text-align:left; white-space:nowrap; }
td { padding:.35rem .7rem; border-bottom:1px solid #161b22; vertical-align:top; }
tr:hover td { background:#1a2332; }
.pts-pos { color:#3fb950; font-weight:600; }
.pts-neg { color:#f85149; font-weight:600; }
.pts-zero { color:#8b949e; }
.play-id { color:#8b949e; font-size:.75rem; font-family:monospace; }
.desc { max-width:340px; color:#c9d1d9; }
.player-name { color:#79c0ff; font-weight:600; white-space:nowrap; }
.stat { color:#d2a8ff; font-size:.82rem; white-space:nowrap; }
.val  { color:#c9d1d9; }
.run-total { color:#e6edf3; font-weight:700; }
.team-total { color:#58a6ff; font-weight:700; }
.section-header { background:#161b22 !important; }
.section-header td { color:#79c0ff; font-weight:700; padding:.6rem .7rem;
                     border-top:2px solid #2d3a4f; border-bottom:1px solid #2d3a4f; }
.milestone { background:#0d1117; }
.milestone td { color:#e6edf3; }
"""


def pts_class(pts):
    if pts > 0: return "pts-pos"
    if pts < 0: return "pts-neg"
    return "pts-zero"


def pts_str(pts):
    return f"{pts:+.2f}" if pts != 0 else "0.00"


def render_html(data: dict) -> str:
    rows         = data["rows"]
    defense_row  = data["defense_row"]
    recon        = data["reconstructed_total"]
    yahoo        = data["yahoo_total"]
    diff         = round(recon - (yahoo or 0), 2)
    ptots        = data["player_totals"]
    starters     = data["starters"]
    team_name    = data["team_name"]

    starter_rows = ""
    for p in starters:
        pname = p["name"]
        pts   = ptots.get(pname, 0.0)
        starter_rows += (
            f"<tr><td class='player-name'>{pname}</td>"
            f"<td>{p['position']}</td><td>{p['nfl_team']}</td>"
            f"<td class='{pts_class(pts)}'>{pts:+.2f}</td></tr>"
        )

    play_rows_html = ""
    for row in rows:
        first   = True
        nevents = len(row["events"])
        for ev in row["events"]:
            pstr = pts_str(ev["pts"])
            pcls = pts_class(ev["pts"])
            if first:
                play_rows_html += (
                    f"<tr>"
                    f"<td rowspan='{nevents}' class='play-id'>{row['seq']}</td>"
                    f"<td rowspan='{nevents}' style='white-space:nowrap'>{row['time']}</td>"
                    f"<td rowspan='{nevents}' class='desc'>{row['description']}</td>"
                    f"<td class='player-name'>{ev['player']}</td>"
                    f"<td class='stat'>{ev['stat_label']}</td>"
                    f"<td class='val'>{ev['value_fmt']}</td>"
                    f"<td class='{pcls}'>{pstr}</td>"
                    f"<td class='run-total'>{ev['player_total']:.2f}</td>"
                    f"<td rowspan='{nevents}' class='team-total'>{row['team_total']:.2f}</td>"
                    f"</tr>"
                )
                first = False
            else:
                play_rows_html += (
                    f"<tr>"
                    f"<td class='player-name'>{ev['player']}</td>"
                    f"<td class='stat'>{ev['stat_label']}</td>"
                    f"<td class='val'>{ev['value_fmt']}</td>"
                    f"<td class='{pcls}'>{pstr}</td>"
                    f"<td class='run-total'>{ev['player_total']:.2f}</td>"
                    f"</tr>"
                )

    def_html = ""
    if defense_row:
        dr   = defense_row
        pstr = pts_str(dr["pts"])
        pcls = pts_class(dr["pts"])
        def_html = (
            "<tr class='section-header'>"
            "<td colspan='9'>🛡 Defense — Points Allowed (game-level)</td></tr>"
            f"<tr class='milestone'>"
            f"<td class='play-id'>—</td>"
            f"<td style='white-space:nowrap'>{dr['time']}</td>"
            f"<td class='desc'>Opponent scored {dr['opp_pts']} pts (actual final score)</td>"
            f"<td class='player-name'>{dr['player']}</td>"
            f"<td class='stat'>Pts Allowed</td>"
            f"<td class='val'>{dr['opp_pts']} pts</td>"
            f"<td class='{pcls}'>{pstr}</td>"
            f"<td class='run-total'>{dr['player_total']:.2f}</td>"
            f"<td class='team-total'>{dr['team_total']:.2f}</td>"
            f"</tr>"
        )

    diff_cls  = "green" if diff >= 0 else "red"
    yahoo_str = f"{yahoo:.2f}" if yahoo is not None else "N/A"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Scoring Debug — {team_name}</title>
<style>{CSS}</style>
</head>
<body>
<h1>Scoring Debug: {team_name}</h1>
<p class="summary">Every play-level event that contributed to the reconstructed score.
Milestone bonuses (100/150/200 yd) fire inline when the threshold is crossed.</p>

<div class="totals">
  <div class="box"><div class="label">Reconstructed</div>
    <div class="val">{recon:.2f}</div></div>
  <div class="box"><div class="label">Yahoo Official</div>
    <div class="val">{yahoo_str}</div></div>
  <div class="box"><div class="label">Difference</div>
    <div class="val {diff_cls}">{diff:+.2f}</div></div>
</div>

<h2>Starter Totals</h2>
<table>
  <thead><tr><th>Player</th><th>Pos</th><th>NFL Team</th><th>Recon Pts</th></tr></thead>
  <tbody>{starter_rows}</tbody>
</table>

<h2>Play-by-Play Events</h2>
<table>
  <thead>
    <tr>
      <th>#</th><th>Time (ET)</th><th>Description</th>
      <th>Player</th><th>Stat</th><th>Value</th>
      <th>Pts Δ</th><th>Player Total</th><th>Team Total</th>
    </tr>
  </thead>
  <tbody>
    {play_rows_html}
    {def_html}
  </tbody>
</table>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description="Trace scoring events for one fantasy team.")
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--week",   type=int, default=1)
    ap.add_argument("--team",   required=True)
    ap.add_argument("--out",    default=None)
    args = ap.parse_args()

    data = run(args.season, args.week, args.team)

    print(f"\nTeam            : {data['team_name']}")
    print(f"Reconstructed   : {data['reconstructed_total']:.2f}")
    print(f"Yahoo official  : {data['yahoo_total']}")
    diff = data["reconstructed_total"] - (data["yahoo_total"] or 0)
    print(f"Difference      : {diff:+.2f}")
    print(f"\nPer-player breakdown:")
    for p in data["starters"]:
        pts = data["player_totals"].get(p["name"], 0.0)
        print(f"  {p['name']:<28} {pts:>7.2f} pts")
    print(f"\nPlays with scoring events: {len(data['rows'])}")

    out = args.out or f"/tmp/debug_{args.season}_w{args.week}_{args.team.replace(' ','_')}.html"
    Path(out).write_text(render_html(data), encoding="utf-8")
    print(f"\nDetailed table  : {out}")


if __name__ == "__main__":
    main()
