"""
lib/scoring.py  –  Fantasy points calculation engine.

Takes a list of fantasy events (from espn.extract_fantasy_events) and a
scoring rules dict (from yahoo.get_scoring_settings) and returns the total
points contributed by each event.

Defense scoring note
--------------------
Individual defensive plays (sacks, INTs, fumble recoveries, def TDs, safeties)
are scored event-by-event with real wallclock times just like skill positions.

Points-allowed and yards-allowed are game-level stats that are only known after
the game ends. build_timelines.py handles those separately by computing final
opponent score, adding those defense points at the game's final wallclock time.
"""

import logging

log = logging.getLogger(__name__)


# ─── Hardcoded league scoring rules ──────────────────────────────────────────
# These are the exact rules for league 656728 (2025 season) and will not change.
# The Yahoo API stat-ID mapping returns corrupt values, so we bypass it entirely.

LEAGUE_RULES: dict = {
    # ── Passing ──────────────────────────────────────────────────────────────
    "pass_yards":         0.04,   # 1 pt / 25 yds
    "pass_td":            4.0,
    "int_thrown":        -2.0,
    # 40+ yard passing TD: +1 bonus (separate event emitted by espn.py)
    "pass_td_40plus":     1.0,
    # Per-game yardage milestones (awarded once each if threshold crossed)
    "pass_yards_300":     1.0,
    "pass_yards_400":     1.0,
    "pass_yards_500":     1.0,

    # ── Rushing ──────────────────────────────────────────────────────────────
    "rush_yards":         0.1,    # 1 pt / 10 yds
    "rush_td":            6.0,
    "rush_td_40plus":     2.0,    # +2 bonus for 40+ yd rushing TD
    "rush_yards_100":     1.0,
    "rush_yards_150":     1.0,
    "rush_yards_200":     1.0,

    # ── Receiving (0.5 PPR) ───────────────────────────────────────────────────
    "receptions":         0.5,
    "rec_yards":          0.1,    # 1 pt / 10 yds
    "rec_td":             6.0,
    "rec_td_40plus":      2.0,    # +2 bonus for 40+ yd receiving TD
    "rec_yards_100":      1.0,
    "rec_yards_150":      1.0,
    "rec_yards_200":      1.0,

    # ── Misc offense ─────────────────────────────────────────────────────────
    "return_td":          6.0,
    "two_pt_conversion":  2.0,
    "fumble_lost":       -2.0,
    "off_fumble_ret_td":  6.0,

    # ── Kicker ───────────────────────────────────────────────────────────────
    "fg_0_19":            3.0,
    "fg_20_29":           3.0,
    "fg_30_39":           3.0,
    "fg_40_49":           4.0,
    "fg_50_plus":         5.0,
    "fg_miss_0_19":      -1.0,
    "fg_miss_20_29":     -1.0,
    "fg_miss_30_39":     -1.0,
    "fg_miss_40_49":     -1.0,
    "fg_miss_50_plus":    0.0,   # not listed in league rules → 0
    "pat_made":           1.0,
    "pat_missed":        -1.0,

    # ── Defense / Special Teams ───────────────────────────────────────────────
    "def_sack":           2.0,
    "def_int":            3.0,
    "def_fumble_rec":     3.0,
    "def_td":             6.0,
    "def_safety":         3.0,
    "def_blocked_kick":   2.0,
    "def_return_td":      6.0,   # kickoff / punt return TD
    "def_4th_down_stop":  1.0,   # hard to extract from PBP; tracked where possible
    "def_xp_returned":    2.0,   # extra point returned; very rare
}

# Keep DEFAULT_RULES as an alias so existing code that imports it still works.
DEFAULT_RULES = LEAGUE_RULES

# Points-allowed scoring thresholds (defense game-level stat)
# Maps (pts_allowed_min, pts_allowed_max_exclusive) → bonus_points
PTS_ALLOWED_THRESHOLDS = [
    (0,   1,  10.0),
    (1,   7,   7.0),
    (7,  14,   4.0),
    (14, 21,   1.0),
    (21, 28,   0.0),
    (28, 35,  -1.0),
    (35, 999, -4.0),
]

# Yards-allowed scoring — not used in this league (no tier defined)
YARDS_ALLOWED_THRESHOLDS: list = []


# ─── Per-game yardage milestones ─────────────────────────────────────────────
# Maps a cumulative-yardage stat to the threshold bonuses earned once crossed.
# Used by compute_pts_from_stats() so milestones fire at the correct moment.

YARD_MILESTONES: dict = {
    "pass_yards": [
        (300, "pass_yards_300"),
        (400, "pass_yards_400"),
        (500, "pass_yards_500"),
    ],
    "rush_yards": [
        (100, "rush_yards_100"),
        (150, "rush_yards_150"),
        (200, "rush_yards_200"),
    ],
    "rec_yards": [
        (100, "rec_yards_100"),
        (150, "rec_yards_150"),
        (200, "rec_yards_200"),
    ],
}


def compute_pts_from_stats(stats: dict, rules: dict) -> float:
    """
    Compute total fantasy points from a player's *accumulated* game stat sheet.

    Unlike score_event (which scores a single play), this function treats
    `stats` as the complete in-game totals at a given moment and applies
    all milestone bonuses automatically — e.g. the 100-yard rush bonus fires
    the instant cumulative rush yards cross 100.

    Parameters
    ----------
    stats : dict
        Accumulated stats for one player in one game.
        Keys match LEAGUE_RULES (e.g. "pass_yards", "rush_td", …).
    rules : dict
        Scoring rules dict (e.g. DEFAULT_RULES).

    Returns
    -------
    float
        Total fantasy points earned so far this game.
    """
    pts = 0.0
    for stat, value in stats.items():
        pts += value * rules.get(stat, 0.0)

    # One-time milestone bonuses (stacks: 150 yds earns both 100 AND 150 bonuses)
    for yard_stat, milestones in YARD_MILESTONES.items():
        total = stats.get(yard_stat, 0.0)
        for threshold, bonus_stat in milestones:
            if total >= threshold:
                pts += rules.get(bonus_stat, 0.0)

    return round(pts, 4)


def merge_with_defaults(fetched_rules: dict) -> dict:
    """Fill in any gaps in fetched rules with sensible defaults."""
    merged = dict(DEFAULT_RULES)
    merged.update(fetched_rules)
    return merged


def score_event(event: dict, rules: dict) -> float:
    """Return fantasy points for a single event dict."""
    stat  = event.get("stat", "")
    value = event.get("value", 0)
    pts_per_unit = rules.get(stat, 0.0)
    return round(pts_per_unit * value, 4)


def pts_allowed_bonus(pts_allowed: int, rules: dict) -> float:
    """
    Return the defense points-allowed bonus.
    Uses league rules if 'def_pts_allowed_0' etc. are present,
    otherwise falls back to the standard thresholds.
    """
    # Yahoo passes this as a single scaled value; use thresholds instead
    for lo, hi, pts in PTS_ALLOWED_THRESHOLDS:
        if lo <= pts_allowed < hi:
            return pts
    return -4.0


def yards_allowed_bonus(yards_allowed: int, rules: dict) -> float:
    if "def_yards_allowed" not in rules:
        return 0.0
    for lo, hi, pts in YARDS_ALLOWED_THRESHOLDS:
        if lo <= yards_allowed < hi:
            return pts
    return -5.0


# ─── Name normalisation (for Yahoo ↔ ESPN matching) ──────────────────────────

import re as _re
_SUFFIX = _re.compile(r"\b(jr\.?|sr\.?|ii|iii|iv|v)\b", _re.I)
_PUNCT  = _re.compile(r"[^a-z0-9 ]")

# ESPN and Yahoo sometimes use different abbreviations for the same NFL team.
# Keys are ESPN abbreviations; values are the Yahoo canonical form.
_ABBR_NORMALIZE: dict = {
    "WSH": "WAS",   # Washington Commanders
    "JAC": "JAX",   # Jacksonville Jaguars
    "LVR": "LV",    # Las Vegas Raiders
    "ARZ": "ARI",   # Arizona Cardinals
    "CLV": "CLE",   # Cleveland Browns
    "HST": "HOU",   # Houston Texans
    "LA":  "LAR",   # nflverse uses "LA" for Los Angeles Rams; Yahoo uses "LAR"
    "WSH": "WAS",   # some sources use WSH for Washington Commanders
}


def normalize_nfl_team(abbr: str) -> str:
    """Normalise an NFL team abbreviation to the Yahoo canonical form."""
    a = abbr.upper()
    return _ABBR_NORMALIZE.get(a, a)


def normalize_name(name: str) -> str:
    """
    Lower-case, strip punctuation and name suffixes so
    'Patrick Mahomes II' matches 'Patrick Mahomes'.
    """
    n = name.lower()
    n = _SUFFIX.sub("", n)
    n = _PUNCT.sub("", n)
    return " ".join(n.split())


def espn_abbrev_key(name: str) -> str:
    """
    Convert an ESPN play-text abbreviated name to a matchable key.

    ESPN encodes player names as 'Initial.Lastname' or 'I1.I2.Lastname':
      'J.Burrow'    → 'j burrow'
      'D.K.Metcalf' → 'dk metcalf'
      'T.Y.Hilton'  → 'ty hilton'
      'C.McCaffrey' → 'c mccaffrey'
    """
    parts = name.split(".")
    if len(parts) < 2:
        return normalize_name(name)
    initials = "".join(p.lower() for p in parts[:-1])
    surname  = _PUNCT.sub("", parts[-1].lower())
    return f"{initials} {surname}".strip()


def yahoo_abbrev_key(name: str) -> str:
    """
    Generate the abbreviated key for a Yahoo full player name so it matches
    ESPN play-text abbreviations.

      'Joe Burrow'    → 'j burrow'       (full first name → first letter)
      'DK Metcalf'    → 'dk metcalf'     (2-char initials kept whole)
      'A.J. Brown'    → 'aj brown'       (dotted initials stripped)
      'T.Y. Hilton'   → 'ty hilton'
      'Justin Jefferson' → 'j jefferson'
    """
    parts = name.split()
    if not parts:
        return ""
    # Strip punctuation from the first word to get clean initials/name
    first_clean = _PUNCT.sub("", parts[0].lower())  # "joe", "dk", "aj", "ty"
    last_clean  = _PUNCT.sub("", " ".join(parts[1:]).lower())
    last_clean  = _SUFFIX.sub("", last_clean)        # drop Jr./II/III suffixes
    last_clean  = " ".join(last_clean.split())
    # ≤ 2 chars after cleaning → treat as initials (DK, TY, AJ); else use first char
    initials = first_clean if len(first_clean) <= 2 else first_clean[0]
    return f"{initials} {last_clean}".strip()


def build_name_index(roster_players: list) -> dict:
    """
    Build a dict of normalised_name → player dict for fast lookup.
    If two players share a normalised name the later one wins (rare edge case).
    """
    return {normalize_name(p["name"]): p for p in roster_players if p.get("name")}
