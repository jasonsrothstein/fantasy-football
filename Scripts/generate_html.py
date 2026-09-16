#!/usr/bin/env python3
"""
generate_html.py  –  Build a shareable static HTML fantasy matchup report.

Two input modes:
  --timeline  (preferred)  Pre-built timeline JSON from build_timelines.py.
                           Contains exact per-play wallclock timestamps.
  --csv       (legacy)     Polling CSV from matchup_data.py (week_N_scores.csv).

Optional commentary:
  --commentary  Path to a YAML file with text, images, tweets, and GIFs per matchup.
  --init-commentary  Print a blank YAML template for the loaded week and exit.

No server required. Charts are rendered by Plotly.js (loaded from CDN).

Usage:
    # From reconstructed play-by-play data (new pipeline):
    python Scripts/generate_html.py --week 10 --timeline Data/2025/timelines/week_10.json

    # With commentary:
    python Scripts/generate_html.py --week 10 \\
        --timeline Data/2025/timelines/week_10.json \\
        --commentary Commentary/week_10.yaml \\
        --out Reports/week_10_matchups.html

    # Generate a blank commentary template:
    python Scripts/generate_html.py --week 10 --timeline ... --init-commentary > Commentary/week_10.yaml

    # From a polling CSV (legacy):
    python Scripts/generate_html.py --week 5
    python Scripts/generate_html.py --week 5 --csv Scripts/week_5_scores.csv
    python Scripts/generate_html.py --week 5 --out reports/week5.html
"""

import argparse
import base64
import json
import mimetypes
import re
import ssl
import sys
import urllib.request
from pathlib import Path

import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# CSV loading
# ─────────────────────────────────────────────────────────────────────────────

def find_csv(week: int) -> Path:
    """Search common locations for the week scores CSV.

    Prefers Scripts/ (newer format with Date/Time/Vertical columns) over the
    repo root, which may contain older-format CSVs from prior scripts.
    """
    candidates = [
        Path(__file__).parent / f"week_{week}_scores.csv",  # Scripts/ dir
        Path("Scripts") / f"week_{week}_scores.csv",        # from repo root
        Path(f"week_{week}_scores.csv"),                    # cwd fallback
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"Could not find week_{week}_scores.csv.\n"
        f"Searched: {[str(c) for c in candidates]}\n"
        f"Use --csv to specify the path explicitly."
    )


def load_csv(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    # CSV format: Timestamp,Date,Time,Vertical,{Team} PTS,{Team} PROJ,{Team} WPCT,...
    if {"Date", "Time"}.issubset(df.columns):
        df["_dt"] = pd.to_datetime(
            df["Date"].astype(str) + " " + df["Time"].astype(str), errors="coerce"
        )
    elif "Timestamp" in df.columns:
        df["_dt"] = pd.to_datetime(df["Timestamp"], unit="s", errors="coerce")
    else:
        raise ValueError("CSV must have 'Date'+'Time' or 'Timestamp' columns.")
    df = df.dropna(subset=["_dt"]).reset_index(drop=True)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Matchup detection
# ─────────────────────────────────────────────────────────────────────────────

def extract_team_names(columns: list) -> list:
    names: set = set()
    for col in columns:
        m = re.match(r"^(.+)\s+(PTS|PROJ|WPCT)$", col)
        if m:
            names.add(m.group(1))
    return sorted(names)


def detect_matchups(df: pd.DataFrame) -> list:
    """
    Find head-to-head pairs by identifying teams whose win probabilities
    sum to approximately 1.0 on the same row (reciprocal matchup pairs).
    """
    team_names = extract_team_names(df.columns.tolist())
    cols = set(df.columns)
    pair_counts: dict = {}

    for idx in range(len(df)):
        row = df.iloc[idx]
        used: set = set()
        pairs: list = []

        for t1 in team_names:
            if t1 in used:
                continue
            c1 = f"{t1} WPCT"
            if c1 not in cols:
                continue
            try:
                w1 = float(row[c1])
            except (TypeError, ValueError):
                continue

            for t2 in team_names:
                if t2 == t1 or t2 in used:
                    continue
                c2 = f"{t2} WPCT"
                if c2 not in cols:
                    continue
                try:
                    w2 = float(row[c2])
                except (TypeError, ValueError):
                    continue
                if abs((w1 + w2) - 1.0) <= 0.02:
                    a, b = (t1, t2) if t1 < t2 else (t2, t1)
                    pairs.append((a, b))
                    used.add(t1)
                    used.add(t2)
                    break

        if len(pairs) != 6:
            continue
        for p in pairs:
            pair_counts[p] = pair_counts.get(p, 0) + 1

    if not pair_counts:
        return []

    seen: set = set()
    matchups: list = []
    for (a, b), _ in sorted(pair_counts.items(), key=lambda x: (-x[1], x[0])):
        if a in seen or b in seen:
            continue
        matchups.append({"team1": a, "team2": b})
        seen |= {a, b}
        if len(matchups) == 6:
            break
    return matchups


# ─────────────────────────────────────────────────────────────────────────────
# Data payload builder
# ─────────────────────────────────────────────────────────────────────────────

def safe_floats(series: pd.Series) -> list:
    out = []
    for v in series:
        try:
            out.append(round(float(v), 2))
        except (TypeError, ValueError):
            out.append(None)
    return out


def build_payload(df: pd.DataFrame, matchups: list, week: int) -> dict:
    times = df["_dt"].dt.strftime("%Y-%m-%dT%H:%M:%S").tolist()

    vertical_idx = None
    if "Vertical" in df.columns:
        for i, v in enumerate(df["Vertical"]):
            try:
                if float(v) == 1.0:
                    vertical_idx = i
                    break
            except (TypeError, ValueError):
                pass

    dt = df["_dt"].dropna()
    if len(dt) >= 2:
        lo = f"{dt.iloc[0].strftime('%b')} {dt.iloc[0].day}"
        hi = f"{dt.iloc[-1].strftime('%b')} {dt.iloc[-1].day}, {dt.iloc[-1].year}"
        date_range = f"{lo} \u2013 {hi}"
    else:
        date_range = f"Week {week}"

    matchup_list = []
    for m in matchups:
        t1, t2 = m["team1"], m["team2"]

        def col(team, field):
            key = f"{team} {field}"
            return safe_floats(df[key]) if key in df.columns else [None] * len(df)

        pts1  = col(t1, "PTS")
        pts2  = col(t2, "PTS")
        proj1 = col(t1, "PROJ")
        proj2 = col(t2, "PROJ")
        wpct1 = col(t1, "WPCT")

        final1 = next((v for v in reversed(pts1) if v is not None), 0.0)
        final2 = next((v for v in reversed(pts2) if v is not None), 0.0)
        winner = t1 if final1 >= final2 else t2

        matchup_list.append({
            "team1": t1,  "team2": t2,
            "winner": winner,
            "final1": final1, "final2": final2,
            "pts1": pts1,   "pts2": pts2,
            "proj1": proj1, "proj2": proj2,
            "wpct1": wpct1,
        })

    return {
        "week": week,
        "date_range": date_range,
        "times": times,
        "vertical_idx": vertical_idx,
        "matchups": matchup_list,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Commentary
# ─────────────────────────────────────────────────────────────────────────────

def render_commentary_html(text: str) -> str:
    """Convert Markdown-ish text to HTML.

    Double blank lines separate blocks. Blocks that start with '<' are treated
    as raw HTML and passed through unchanged. All other blocks are wrapped in
    <p> tags with single newlines converted to <br>.
    """
    if not text or not text.strip():
        return ""
    blocks = re.split(r"\n{2,}", text.strip())
    parts = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if block.startswith("<"):
            parts.append(block)
        else:
            # Basic inline escaping for plain-text blocks only
            safe = block.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            safe = safe.replace("\n", "<br>")
            parts.append(f"<p>{safe}</p>")
    return "\n".join(parts)


def fetch_logo_as_data_uri(url: str) -> str:
    """Download a logo URL and return a base64 data URI, or '' on failure."""
    if not url:
        return ""
    try:
        import urllib.request, ssl
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(url, context=ctx, timeout=8) as resp:
                data = resp.read()
                ct = resp.headers.get_content_type() or "image/jpeg"
        except Exception:
            # Fallback: skip SSL verification
            ctx2 = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx2.check_hostname = False
            ctx2.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(url, context=ctx2, timeout=8) as resp:
                data = resp.read()
                ct = resp.headers.get_content_type() or "image/jpeg"
        return f"data:{ct};base64,{base64.b64encode(data).decode()}"
    except Exception as exc:
        print(f"  [warn] Could not fetch logo {url}: {exc}", file=sys.stderr)
        return url   # fall back to the original URL


def embed_local_images(html: str, base_dir: Path) -> str:
    """Replace local file paths in src="..." with base64 data URIs.

    URLs (http/https/data//) are left untouched. If a local file cannot be
    found the original src is left unchanged.
    """
    def replacer(m: re.Match) -> str:
        src = m.group(1)
        if src.startswith(("http://", "https://", "data:", "//")):
            return m.group(0)
        # Try relative to commentary file directory, then cwd
        for candidate in (base_dir / src, Path(src)):
            if candidate.exists():
                mime, _ = mimetypes.guess_type(str(candidate))
                mime = mime or "image/jpeg"
                data = base64.b64encode(candidate.read_bytes()).decode()
                return f'src="data:{mime};base64,{data}"'
        return m.group(0)

    return re.sub(r'src="([^"]*)"', replacer, html)


def load_commentary(path: Path) -> dict:
    """Parse a commentary YAML file.

    Returns a dict with:
      intro_html   – rendered HTML string for the week intro section
      matchup_map  – dict keyed by frozenset of lowercased team names → HTML string
    """
    try:
        import yaml
    except ImportError:
        print("ERROR: pyyaml is required for --commentary. Run: pip install pyyaml",
              file=sys.stderr)
        sys.exit(1)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {"intro_html": "", "matchup_map": {}}

    base_dir = path.parent

    intro_text = raw.get("intro", "") or ""
    intro_html = render_commentary_html(intro_text)
    intro_html = embed_local_images(intro_html, base_dir)

    matchup_map: dict = {}
    for entry in raw.get("matchups", []) or []:
        teams = entry.get("teams", []) or []
        if len(teams) < 2:
            continue
        text = entry.get("text", "") or ""
        html = render_commentary_html(text)
        html = embed_local_images(html, base_dir)
        key = frozenset(t.strip().lower() for t in teams)
        matchup_map[key] = html

    return {"intro_html": intro_html, "matchup_map": matchup_map}


def match_commentary(matchup: dict, matchup_map: dict) -> str:
    """Return the commentary HTML for a matchup, or '' if none."""
    key = frozenset([matchup["team1"].lower(), matchup["team2"].lower()])
    return matchup_map.get(key, "")


def build_commentary_template(payload: dict) -> str:
    """Return a blank YAML commentary template pre-populated with team names."""
    lines = [
        f"week: {payload['week']}",
        "",
        "# Week intro — appears above all matchup cards.",
        "# Write normal text, or paste HTML embed codes (tweets, images, gifs).",
        "intro: |",
        "  Week recap goes here...",
        "",
        "matchups:",
    ]
    for m in payload["matchups"]:
        t1, t2 = m["team1"], m["team2"]
        lines += [
            f'  - teams: ["{t1}", "{t2}"]',
            "    text: |",
            "      Write your commentary here...",
            "",
            "      # Paste tweet embed code directly:",
            '      # <blockquote class="twitter-tweet"><p>...</p>',
            '      #   <a href="https://twitter.com/.../status/ID"></a>',
            "      # </blockquote>",
            "",
            "      # Embed a GIF or image by URL:",
            '      # <img src="https://media.giphy.com/media/ID/giphy.gif">',
            "",
            "      # Embed a local image (base64-embedded into the HTML):",
            '      # <img src="Commentary/assets/photo.jpg" alt="caption">',
            "",
        ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# HTML template
# All literal { } in CSS/JS must be {{ }} so Python .format() passes them through.
# JavaScript template literal placeholders ${...} become ${{...}} here.
# Commentary and intro content travel inside DATA JSON to avoid .format() escaping.
# ─────────────────────────────────────────────────────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Pickle Fingers Week {week}</title>
  __PLOTLY_SCRIPT__
  <style>
    :root {{
      --bg:      #0f1419;
      --surface: #1a2332;
      --border:  #2d3a4f;
      --text:    #e6edf3;
      --muted:   #8b949e;
      --accent:  #58a6ff;
      --green:   #3fb950;
    }}
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
      padding: 2rem 1.5rem 3rem;
      min-height: 100vh;
    }}
    header {{
      max-width: 1100px;
      margin: 0 auto 2rem;
      padding-bottom: 1.25rem;
      border-bottom: 1px solid var(--border);
    }}
    header h1 {{
      font-size: 1.8rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }}
    header .subtitle {{
      color: var(--muted);
      font-size: 0.95rem;
      margin-top: 0.4rem;
    }}
    /* ── Week intro card ─────────────────────────────────────────────── */
    .week-intro {{
      max-width: 1100px;
      margin: 0 auto 2rem;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 1.25rem 1.5rem 1.4rem;
    }}
    .week-intro p {{
      color: var(--text);
      line-height: 1.75;
      font-size: 0.95rem;
      margin-bottom: 0.65rem;
    }}
    .week-intro p:last-child {{ margin-bottom: 0; }}
    .week-intro img {{
      max-width: 100%;
      max-height: 400px;
      border-radius: 8px;
      margin: 0.6rem 0;
    }}
    .week-intro blockquote.twitter-tweet {{ margin: 0.75rem 0; }}
    /* ── Commentary section ──────────────────────────────────────────── */
    .commentary-wrap {{
      max-width: 1100px;
      margin: 0 auto 2.5rem;
    }}
    .commentary-wrap h2 {{
      font-size: 1.1rem;
      font-weight: 700;
      letter-spacing: -0.01em;
      margin-bottom: 0.85rem;
      color: var(--text);
    }}
    .commentary-body {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 1.25rem 1.5rem 1.4rem;
      max-width: 750px;
    }}
    .commentary-body p {{
      color: var(--text);
      line-height: 1.75;
      font-size: 0.95rem;
      margin-bottom: 0.65rem;
    }}
    .commentary-body p:last-child {{ margin-bottom: 0; }}
    .commentary-body strong {{ color: #fff; }}
    /* ── Tweets section ─────────────────────────────────────────────── */
    .tweets-wrap {{
      max-width: 1100px;
      margin: 0 auto 2.5rem;
    }}
    .tweets-wrap h2 {{
      font-size: 1.1rem;
      font-weight: 700;
      letter-spacing: -0.01em;
      margin-bottom: 1rem;
      color: var(--text);
    }}
    .tweets-grid {{
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
      max-width: 550px;
    }}
    .tweet-item {{
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }}
    .tweet-comment {{
      font-size: 0.92rem;
      color: var(--text);
      line-height: 1.55;
      margin: 0;
    }}
    .tweet-item .twitter-tweet {{
      margin: 0 !important;
    }}
    /* ── Standings table ────────────────────────────────────────────── */
    .standings-wrap {{
      max-width: 1100px;
      margin: 0 auto 2.5rem;
      display: flex;
      flex-direction: column;
      align-items: center;
    }}
    .standings-wrap h2 {{
      align-self: flex-start;
      font-size: 1.1rem;
      font-weight: 700;
      letter-spacing: -0.01em;
      margin-bottom: 0.85rem;
      color: var(--text);
    }}
    .standings-wrap .standings-table {{
      margin: 0 auto;
    }}
    .standings-table {{
      width: auto;
      table-layout: auto;
      border-collapse: collapse;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      overflow: hidden;
      font-size: 0.88rem;
    }}
    .standings-table col.col-rank {{ }}
    .standings-table col.col-team {{ }}
    .standings-table col.col-data {{ }}
    .standings-table thead tr {{
      background: #161b22;
    }}
    .standings-table th {{
      padding: 0.6rem 0.85rem;
      text-align: center;
      color: var(--muted);
      font-weight: 600;
      font-size: 0.78rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      white-space: nowrap;
      cursor: pointer;
      user-select: none;
    }}
    .standings-table th.col-team-hdr {{ text-align: left; }}
    .standings-table th:hover {{ color: var(--text); }}
    .standings-table th.sort-asc::after  {{ content: ' ▲'; font-size: 0.65rem; }}
    .standings-table th.sort-desc::after {{ content: ' ▼'; font-size: 0.65rem; }}
    .standings-table td {{
      padding: 0.55rem 0.85rem;
      border-top: 1px solid var(--border);
      vertical-align: middle;
      white-space: nowrap;
      text-align: center;
      color: var(--muted);
    }}
    .standings-table td:nth-child(2) {{ text-align: left; }}
    .standings-table tbody tr:hover {{ background: rgba(255,255,255,0.03); }}
    .standings-table .rank {{
      color: var(--muted);
      font-size: 0.8rem;
      width: 2rem;
      text-align: center;
      padding-right: 0.4rem;
    }}
    .standings-table .col-team {{
      display: flex;
      align-items: center;
      gap: 0.55rem;
    }}
    .standings-table .col-team img {{
      width: 28px; height: 28px;
      border-radius: 50%;
      object-fit: cover;
      flex-shrink: 0;
    }}
    .standings-table .team-name {{ font-weight: 500; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .standings-table .sort-active {{ font-weight: 700; color: #fff; }}
    /* ── Matchup grid — single column ───────────────────────────────── */
    .grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 1.5rem;
      max-width: 1100px;
      margin: 0 auto;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      overflow: hidden;
    }}
    .card-header {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 0.5rem;
      padding: 1rem 1.25rem 0.85rem;
      border-bottom: 1px solid var(--border);
    }}
    .teams {{
      display: flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.95rem;
      font-weight: 600;
      flex-wrap: wrap;
    }}
    .teams .sep    {{ color: var(--muted); font-weight: 400; font-size: 0.85rem; }}
    .teams .winner {{ color: var(--accent); }}
    .teams .loser  {{ color: var(--muted); }}
    .team-logo {{
      width: 26px; height: 26px; border-radius: 50%;
      object-fit: cover; flex-shrink: 0;
      background: var(--surface);
    }}
    .score-box     {{ font-size: 0.9rem; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .score-box .sw {{ color: var(--green); font-weight: 600; }}
    .score-box .ss {{ color: var(--muted); margin: 0 0.2em; }}
    .score-box .sl {{ color: var(--muted); }}
    .chart-wrap    {{ height: 340px; padding: 6px 0 0; position: relative; }}
    .chart-div     {{ width: 100%; height: 100%; }}
    /* ── Play / Replay overlay ───────────────────────────────────────── */
    .chart-overlay {{
      position: absolute; inset: 0;
      display: flex; align-items: center; justify-content: center;
      z-index: 10; pointer-events: auto;
      background: transparent;
    }}
    .chart-overlay.hidden {{ display: none; }}
    .play-btn {{
      width: 68px; height: 68px; border-radius: 50%;
      background: rgba(88,166,255,0.12);
      border: 2px solid rgba(88,166,255,0.7);
      color: rgba(88,166,255,0.9);
      font-size: 1.6rem; line-height: 1;
      cursor: pointer; display: flex; align-items: center; justify-content: center;
      padding-left: 5px;       /* optical centre for ▶ glyph */
      transition: background 0.18s, transform 0.12s, border-color 0.18s, color 0.18s;
    }}
    .play-btn:hover {{
      background: rgba(88,166,255,0.25);
      border-color: #58a6ff; color: #58a6ff;
      transform: scale(1.1);
    }}
    .replay-btn {{
      position: absolute; top: 14px; right: 14px;
      background: rgba(15,20,25,0.82);
      border: 1px solid var(--border);
      color: var(--muted); font-size: 0.78rem;
      padding: 4px 11px; border-radius: 6px;
      cursor: pointer; display: none; z-index: 10;
      transition: color 0.15s, border-color 0.15s;
    }}
    .replay-btn:hover {{ color: var(--text); border-color: #58a6ff; }}
    /* ── Per-matchup commentary ──────────────────────────────────────── */
    .commentary {{
      padding: 1rem 1.25rem 1.4rem;
      border-top: 1px solid var(--border);
    }}
    .commentary p {{
      color: var(--text);
      line-height: 1.75;
      font-size: 0.93rem;
      margin-bottom: 0.75rem;
    }}
    .commentary p:last-child {{ margin-bottom: 0; }}
    .commentary img {{
      max-width: 100%;
      max-height: 400px;
      border-radius: 8px;
      margin: 0.6rem 0;
      display: block;
    }}
    .commentary blockquote.twitter-tweet {{ margin: 0.75rem 0; }}
    /* ── Responsive ──────────────────────────────────────────────────── */
    @media (max-width: 640px) {{
      .chart-wrap {{ height: 280px; }}
      body        {{ padding: 1rem 0.75rem 2rem; }}
      .play-btn   {{ width: 56px; height: 56px; font-size: 1.3rem; }}
    }}
    /* Proximity-based TD marker tooltip (fixed, pointer-events: none) */
    #td-tip {{
      position: fixed;
      display: none;
      background: rgba(13,17,23,0.97);
      border: 1px solid #2d3a4f;
      border-radius: 6px;
      color: #e6edf3;
      font-size: 0.8rem;
      line-height: 1.5;
      max-width: 520px;
      padding: 7px 11px;
      pointer-events: none;
      z-index: 9999;
      box-shadow: 0 4px 18px rgba(0,0,0,0.55);
    }}
  </style>
</head>
<body>
<!-- Floating tooltip for TD marker hover (proximity-based, outside Plotly unified hover) -->
<div id="td-tip"></div>
  <header>
    <h1>Pickle Fingers Week {week}</h1>
    <p class="subtitle">{date_range}</p>
  </header>
  <div class="commentary-wrap" id="commentary-wrap"></div>
  <div class="tweets-wrap" id="tweets-wrap"></div>
  <div class="standings-wrap" id="standings-wrap"></div>
  <div style="max-width:1100px;margin:0 auto 0.85rem;"><h2 style="font-size:1.1rem;font-weight:700;letter-spacing:-0.01em;color:var(--text);">Matchups</h2></div>
  <div class="grid" id="grid"></div>

  <script>
    const DATA = {data_json};


    // ── chart colours ──────────────────────────────────────────────────────
    const C_WIN  = '#3fb950';   // green  – winner
    const C_LOSE = '#f85149';   // red    – loser

    // ── Week intro block ───────────────────────────────────────────────────
    if (DATA.intro_html) {{
      const introDiv = document.createElement('div');
      introDiv.className = 'week-intro';
      introDiv.innerHTML = DATA.intro_html;
      document.getElementById('grid').insertAdjacentElement('beforebegin', introDiv);
    }}

    // ── Commentary section ─────────────────────────────────────────────────
    (function() {{
      const html = DATA.week_commentary_html || '';
      if (!html) return;
      const wrap = document.getElementById('commentary-wrap');
      const h2 = document.createElement('h2');
      h2.textContent = 'Commentary';
      wrap.appendChild(h2);
      const body = document.createElement('div');
      body.className = 'commentary-body';
      body.innerHTML = html;
      wrap.appendChild(body);
    }})();

    // ── Tweets section ─────────────────────────────────────────────────────
    (function() {{
      const urls = DATA.tweet_urls || [];
      if (!urls.length) return;

      const wrap = document.getElementById('tweets-wrap');
      const h2 = document.createElement('h2');
      h2.textContent = 'Tweets';
      wrap.appendChild(h2);

      const grid = document.createElement('div');
      grid.className = 'tweets-grid';
      urls.forEach(entry => {{
        // Support both plain URL strings and {{url, comment}} objects
        const url     = (typeof entry === 'string') ? entry : entry.url;
        const comment = (typeof entry === 'string') ? '' : (entry.comment || '');

        const item = document.createElement('div');
        item.className = 'tweet-item';

        if (comment) {{
          const p = document.createElement('p');
          p.className = 'tweet-comment';
          p.textContent = comment;
          item.appendChild(p);
        }}

        const bq = document.createElement('blockquote');
        bq.className = 'twitter-tweet';
        bq.setAttribute('data-theme', 'dark');
        const a = document.createElement('a');
        a.href = url;
        bq.appendChild(a);
        item.appendChild(bq);
        grid.appendChild(item);
      }});
      wrap.appendChild(grid);
    }})();

    // ── Standings table ────────────────────────────────────────────────────
    (function() {{
      const rows = DATA.standings || [];
      if (!rows.length) return;

      // Sort state: null = default (record+PF), otherwise the clicked column key.
      // clickCount tracks how many times the active column has been clicked:
      //   1 → desc, 2 → asc, 3 → reset to default
      const DEFAULT_KEY = 'record';
      let sortKey  = DEFAULT_KEY;
      let clickCnt = 1;   // default view is already "first click" (desc) on record

      // Columns — rank is display-only (no sort); team sorts by name
      const cols = [
        {{ key: 'rank',   label: '#',                    sortable: false, numeric: true  }},
        {{ key: 'team',   label: 'Team Name',            sortable: true,  numeric: false }},
        {{ key: 'record', label: 'Record',               sortable: true,  numeric: true  }},
        {{ key: 'pf',     label: 'Points For',           sortable: true,  numeric: true  }},
        {{ key: 'pa',     label: 'Points Against',       sortable: true,  numeric: true  }},
        {{ key: 'pr',     label: 'Power Ranking',        sortable: true,  numeric: true  }},
        {{ key: 'etew',   label: 'Every Team Every Week',sortable: true,  numeric: true  }},
        {{ key: 'bs',     label: 'Pickles',              sortable: true,  numeric: true  }},
      ];

      // Returns sort value for a row given a key, higher = "better"
      function sortVal(row, key) {{
        if (key === 'record' || key === 'rank') return row.wins * 1e6 + row.pf;
        if (key === 'pf')   return row.pf;
        if (key === 'pa')   return row.pa;
        if (key === 'etew') return row.etew_wins * 1e6 + row.pf;
        if (key === 'pr')   return row.pr != null ? row.pr : -Infinity;
        if (key === 'team') return 0;   // handled separately via localeCompare
        return row[key] != null ? row[key] : -Infinity;
      }}

      // direction: 1 = desc (high→low first), -1 = asc (low→high first)
      function direction() {{ return clickCnt === 2 ? -1 : 1; }}

      function sortedRows() {{
        const copy = rows.map(r => ({{...r}}));
        copy.sort((a, b) => {{
          const dir = direction();
          if (sortKey === 'team') {{
            return dir * (a.team_name || '').localeCompare(b.team_name || '');
          }}
          const av = sortVal(a, sortKey), bv = sortVal(b, sortKey);
          // dir=-1 (desc): bv-av puts larger values first
          // dir=1  (asc):  av-bv puts smaller values first
          return dir * (bv - av);
        }});
        return copy;
      }}

      function renderTable() {{
        const sorted = sortedRows();
        const isDefault = (sortKey === DEFAULT_KEY && clickCnt === 1);
        const dir = direction();

        const wrap = document.getElementById('standings-wrap');
        wrap.innerHTML = '';

        const h2 = document.createElement('h2');
        h2.textContent = 'Standings';
        wrap.appendChild(h2);

        const tbl = document.createElement('table');
        tbl.className = 'standings-table';

        // Colgroup for widths
        const cg = document.createElement('colgroup');
        cols.forEach(col => {{
          const c = document.createElement('col');
          c.className = col.key === 'rank' ? 'col-rank' : col.key === 'team' ? 'col-team' : 'col-data';
          cg.appendChild(c);
        }});
        tbl.appendChild(cg);

        // Header
        const thead = tbl.createTHead();
        const hr = thead.insertRow();
        cols.forEach(col => {{
          const th = document.createElement('th');
          th.textContent = col.label;
          const isActive = col.sortable && sortKey === col.key;

          if (col.key === 'team') th.classList.add('col-team-hdr');

          if (isActive) {{
            th.style.color = '#fff';
            th.style.fontWeight = '700';
            th.classList.add(dir === 1 ? 'sort-desc' : 'sort-asc');
          }}

          if (col.sortable) {{
            th.style.cursor = 'pointer';
            th.addEventListener('click', () => {{
              if (sortKey === col.key) {{
                clickCnt++;
                if (clickCnt > 3) clickCnt = 1;
                if (clickCnt === 3) {{
                  // Reset to default
                  sortKey  = DEFAULT_KEY;
                  clickCnt = 1;
                }}
              }} else {{
                sortKey  = col.key;
                clickCnt = 1;
              }}
              renderTable();
            }});
          }} else {{
            th.style.cursor = 'default';
          }}
          hr.appendChild(th);
        }});

        // Body
        const tbody = tbl.createTBody();
        sorted.forEach((row, rank) => {{
          const tr = tbody.insertRow();

          // Rank (always reflects current sort position)
          const tdRank = tr.insertCell();
          tdRank.className = 'rank';
          tdRank.textContent = rank + 1;

          // Team (logo + name)
          const tdTeam = tr.insertCell();
          const teamDiv = document.createElement('div');
          teamDiv.className = 'col-team';
          if (row.logo) {{
            const img = document.createElement('img');
            img.src = row.logo;
            img.alt = row.team_name;
            teamDiv.appendChild(img);
          }}
          const nameSpan = document.createElement('span');
          nameSpan.className = sortKey === 'team' ? 'team-name sort-active' : 'team-name';
          nameSpan.textContent = row.team_name;
          teamDiv.appendChild(nameSpan);
          tdTeam.appendChild(teamDiv);

          // Data cells: [key, formatted value]
          const etewVal = (row.etew_wins != null && row.etew_losses != null)
            ? row.etew_wins + '-' + row.etew_losses : '—';
          const dataCells = [
            ['record', row.wins + '-' + row.losses],
            ['pf',     row.pf.toFixed(2)],
            ['pa',     row.pa.toFixed(2)],
            ['pr',     row.pr != null ? row.pr.toFixed(2) : '—'],
            ['etew',   etewVal],
            ['bs',     row.bs != null ? row.bs : '—'],
          ];
          dataCells.forEach(([key, val]) => {{
            const td = tr.insertCell();
            td.textContent = val;
            if (sortKey === key) {{
              // Active sort column: bold white
              td.style.color      = '#fff';
              td.style.fontWeight = '700';
            }} else if (val === '—') {{
              // Truly empty/unfilled column — dim it
              td.style.color = '#3a4454';
            }}
          }});
        }});

        wrap.appendChild(tbl);
      }}

      renderTable();
    }})();

    // ── X-axis: sequential play indices (dead time compressed out) ─────────
    // Each element of DATA.times is an ISO timestamp for one play/event.
    // We plot on 0-based integer indices so there is no wasted horizontal
    // space between game windows (Thursday → Sunday gap, etc.).
    // Tick labels are placed wherever the gap between consecutive timestamps
    // exceeds GAP_MS, which marks the start of a new game window.

    // All times are displayed in US Eastern Time so day labels and hover
    // tooltips are consistent for every viewer regardless of their timezone.
    function toET(d) {{
      // Returns a plain Date whose .getHours()/.getDay() etc. reflect ET.
      // Leverages the Intl API (supported in all modern browsers).
      const etStr = d.toLocaleString('en-US', {{ timeZone: 'America/New_York' }});
      return new Date(etStr);
    }}

    function fmtTime(d) {{
      const et = toET(d);
      const DAYS = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
      let h = et.getHours(), mi = et.getMinutes();
      const ap = h >= 12 ? 'pm' : 'am';
      h = ((h + 11) % 12) + 1;
      return DAYS[et.getDay()] + ' ' + h +
             (mi ? ':' + String(mi).padStart(2, '0') : '') + ap;
    }}

    function buildXAxis(times) {{
      const DAY_NAMES = ['Sunday','Monday','Tuesday','Wednesday',
                         'Thursday','Friday','Saturday'];
      const xVals      = times.map((_, i) => i);
      const hoverText  = times.map(t => fmtTime(new Date(t)));  // "Thu 8:20pm" per minute

      // Identify ET day boundaries, with a special split for Sunday Night
      // (8:10 pm ET = the SNF kickoff window).
      const SUN_NIGHT_MINS = 20 * 60 + 10;   // 8:10 pm ET in minutes-since-midnight

      const dayBoundaries = [];
      const dayMidpoints  = [];
      const dayNames      = [];

      let dayStart   = 0;
      let currentDay = toET(new Date(times[0])).toDateString();

      for (let i = 1; i <= times.length; i++) {{
        const dayKey = i < times.length
          ? toET(new Date(times[i])).toDateString()
          : null;

        if (dayKey !== currentDay) {{
          const dayEnd = i - 1;
          const dayNum = toET(new Date(times[dayStart])).getDay();

          if (dayNum === 0) {{
            // Sunday: look for the first play at or after 8:10 pm ET to mark SNF.
            let snbIdx = -1;
            for (let j = dayStart; j <= dayEnd; j++) {{
              const et = toET(new Date(times[j]));
              if (et.getHours() * 60 + et.getMinutes() >= SUN_NIGHT_MINS) {{
                snbIdx = j;
                break;
              }}
            }}

            if (snbIdx > dayStart) {{
              // Sunday daytime section
              dayMidpoints.push((dayStart + snbIdx - 1) / 2);
              dayNames.push('Sunday');
              // Separator before Sunday Night
              dayBoundaries.push(snbIdx - 0.5);
              // Sunday Night section
              dayMidpoints.push((snbIdx + dayEnd) / 2);
              dayNames.push('Sunday Night');
            }} else {{
              // All plays are either before 8:10 pm (no SNF) or all after (pure SNF week).
              dayMidpoints.push((dayStart + dayEnd) / 2);
              dayNames.push(snbIdx === dayStart ? 'Sunday Night' : 'Sunday');
            }}
          }} else {{
            dayMidpoints.push((dayStart + dayEnd) / 2);
            dayNames.push(DAY_NAMES[dayNum]);
          }}

          if (i < times.length) {{
            dayBoundaries.push(i - 0.5);
          }}
          dayStart   = i;
          currentDay = dayKey;
        }}
      }}

      // Annotations draw the day name labels below the plot area.
      // xaxis.ticktext (all minutes) drives the unified-hover header instead.
      const dayAnnotations = dayMidpoints.map((x, k) => ({{
        x, y: -0.07,
        xref: 'x', yref: 'paper',
        text: dayNames[k],
        showarrow: false,
        font: {{ color: '#8b949e', size: 11 }},
        xanchor: 'center', yanchor: 'top',
      }}));

      return {{ xVals, hoverText, dayBoundaries, dayAnnotations }};
    }}

    const {{ xVals, hoverText, dayBoundaries, dayAnnotations }} = buildXAxis(DATA.times);

    // ── layout factory (per-chart Y range) ────────────────────────────
    function baseLayout(yMax) {{
      const shapes = dayBoundaries.map(x => ({{
        type: 'line',
        x0: x, x1: x,
        y0: 0, y1: 1, yref: 'paper',
        line: {{ color: '#2d3a4f', width: 1.5 }}
      }}));

      return {{
        paper_bgcolor: '#1a2332',
        plot_bgcolor:  '#0f1419',
        font: {{ color: '#e6edf3', family: "'Segoe UI', system-ui, sans-serif", size: 12 }},
        margin: {{ t: 10, r: 20, b: 56, l: 54, pad: 2 }},
        hovermode: 'x unified',
        shapes,
        // Day name labels are annotations rather than axis ticktext,
        // freeing ticktext to carry per-minute time strings for the hover header.
        annotations: dayAnnotations,
        xaxis: {{
          type: 'linear',
          gridcolor: '#2d3a4f', linecolor: '#2d3a4f',
          zeroline: false,        // suppress the default line drawn at x=0
          // Map every minute index → "Day HH:MMam/pm" so the unified-hover
          // header shows the actual time instead of a raw integer.
          tickvals: xVals,
          ticktext: hoverText,
          showticklabels: false,  // axis labels come from annotations
          showgrid: false,
        }},
        yaxis: {{
          gridcolor: '#2d3a4f', linecolor: '#2d3a4f',
          zerolinecolor: '#2d3a4f',
          tickfont: {{ color: '#8b949e', size: 11 }},
          title: {{ text: 'Points', font: {{ color: '#8b949e', size: 11 }}, standoff: 6 }},
          range: [0, yMax],
          fixedrange: false,
        }},
        legend: {{
          bgcolor: 'rgba(15,20,25,0.8)', bordercolor: '#2d3a4f', borderwidth: 1,
          font: {{ size: 10, color: '#8b949e' }},
          x: 0.01, xanchor: 'left', y: 0.99, yanchor: 'top',
        }},
      }};
    }}

    const plotConfig = {{
      responsive: true,
      displayModeBar: false,   // hide the zoom/pan/reset toolbar entirely
    }};

    // ── build one card DOM element ─────────────────────────────────────────
    function buildCard(m, idx) {{
      const isT1Win  = m.winner === m.team1;
      const winTeam  = isT1Win ? m.team1  : m.team2;
      const loseTeam = isT1Win ? m.team2  : m.team1;
      const winPts   = isT1Win ? m.final1 : m.final2;
      const losePts  = isT1Win ? m.final2 : m.final1;
      const winLogo  = isT1Win ? (m.logo1 || '') : (m.logo2 || '');
      const loseLogo = isT1Win ? (m.logo2 || '') : (m.logo1 || '');

      function logoImg(url, alt) {{
        if (!url) return '';
        return '<img class="team-logo" src="' + url + '" alt="' + esc(alt) + '">';
      }}

      const card = document.createElement('article');
      card.className = 'card';
      card.innerHTML =
        '<div class="card-header">' +
          '<div class="teams">' +
            logoImg(winLogo, winTeam) +
            '<span class="team winner">' + esc(winTeam)  + '</span>' +
            '<span class="sep">vs</span>' +
            logoImg(loseLogo, loseTeam) +
            '<span class="team loser">'  + esc(loseTeam) + '</span>' +
          '</div>' +
          '<div class="score-box">' +
            '<span class="sw">' + winPts.toFixed(2)  + '</span>' +
            '<span class="ss">&ndash;</span>' +
            '<span class="sl">' + losePts.toFixed(2) + '</span>' +
          '</div>' +
        '</div>' +
        '<div class="chart-wrap">' +
          '<div class="chart-div" id="c' + idx + '"></div>' +
          '<div class="chart-overlay" id="overlay-' + idx + '">' +
            '<button class="play-btn" onclick="startAnim(' + idx + ')">&#9654;</button>' +
          '</div>' +
          '<button class="replay-btn" id="replay-' + idx + '" onclick="startAnim(' + idx + ')">&#8635; Replay</button>' +
        '</div>';

      // Inject commentary below the chart if present
      const commentaryHtml = DATA.commentary && DATA.commentary[idx];
      if (commentaryHtml) {{
        const cDiv = document.createElement('div');
        cDiv.className = 'commentary';
        cDiv.innerHTML = commentaryHtml;
        card.appendChild(cDiv);
      }}

      return card;
    }}

    // ── render one Plotly chart ────────────────────────────────────────────
    // Charts are initially blank (empty traces) so the Play button triggers
    // the animation.  Axes are locked to their final ranges from the start so
    // the grid / day labels / separator lines are all visible immediately.
    function renderChart(m, idx) {{
      const isT1Win  = m.winner === m.team1;
      const winName  = isT1Win ? m.team1  : m.team2;
      const loseName = isT1Win ? m.team2  : m.team1;
      const winTDs   = isT1Win ? (m.td_markers1 || []) : (m.td_markers2 || []);
      const loseTDs  = isT1Win ? (m.td_markers2 || []) : (m.td_markers1 || []);

      const trajMax = Math.max(
        ...m.pts1.filter(v => v !== null),
        ...m.pts2.filter(v => v !== null),
        0
      );
      const yMax = Math.max(Math.max(m.final1, m.final2), trajMax) + 20;

      // Winner trace first → appears on top in the unified hover box.
      const traces = [
        {{ x: [], y: [],
           name: winName, mode: 'lines', yaxis: 'y',
           line: {{ color: C_WIN, width: 2.5 }},
           hovertemplate: '%{{y:.2f}}<extra>' + esc(winName) + '</extra>' }},
        {{ x: [], y: [],
           name: loseName, mode: 'lines', yaxis: 'y',
           line: {{ color: C_LOSE, width: 2.5 }},
           hovertemplate: '%{{y:.2f}}<extra>' + esc(loseName) + '</extra>' }},
        // TD marker dots — winner colour, revealed during animation.
        // hoverinfo:'skip' keeps them out of the unified hover box;
        // a native mousemove listener handles proximity-based tooltips.
        {{ x: [], y: [], text: [], mode: 'markers', showlegend: false,
           hoverinfo: 'skip',
           marker: {{ color: C_WIN, size: 10, symbol: 'circle',
                      line: {{ color: '#0d1117', width: 1.5 }} }} }},
        // TD marker dots — loser colour
        {{ x: [], y: [], text: [], mode: 'markers', showlegend: false,
           hoverinfo: 'skip',
           marker: {{ color: C_LOSE, size: 10, symbol: 'circle',
                      line: {{ color: '#0d1117', width: 1.5 }} }} }},
      ];

      // Lock x-axis to the full data range so the grid doesn't collapse when
      // traces are empty, and auto-size is suppressed during animation.
      const layout = baseLayout(yMax);
      layout.xaxis.range = [0, xVals.length - 1];
      layout.xaxis.autorange = false;
      layout.yaxis.autorange = false;

      Plotly.newPlot('c' + idx, traces, layout, plotConfig);

      // Store per-chart metadata for the animation loop.
      chartMeta[idx] = {{ yMax, winName, loseName, winTDs, loseTDs }};

      // Attach proximity-based hover for TD marker dots.
      setupTdHover(idx);
    }}

    function esc(s) {{
      const d = document.createElement('div');
      d.textContent = s;
      return d.innerHTML;
    }}

    // Wrap a string at word boundaries, inserting <br> every ~maxLen chars.
    function wrapAt(str, maxLen) {{
      if (!str || str.length <= maxLen) return str;
      const words = str.split(' ');
      const lines = [];
      let line = '';
      for (const word of words) {{
        const candidate = line ? line + ' ' + word : word;
        if (candidate.length > maxLen && line) {{
          lines.push(line);
          line = word;
        }} else {{
          line = candidate;
        }}
      }}
      if (line) lines.push(line);
      return lines.join('<br>');
    }}

    // Format a TD marker's hover text: bold player name + wrapped play description.
    function tdText(td) {{
      const p = esc(td.player || '');
      // Strip jersey-number prefixes like "16-J.Goff" → "J.Goff".
      // Match digits+dash only when followed by a capital letter (player name).
      const raw = (td.desc || '').replace(/\\d+-(?=[A-Z])/g, '');
      const d = wrapAt(esc(raw), 120);
      return d ? '<b>' + p + '</b><br>' + d : '<b>' + p + '</b>';
    }}

    // Attach a native mousemove listener that shows #td-tip only when the
    // cursor is within RADIUS pixels of a visible TD marker dot.  This
    // replaces Plotly's unified-hover for markers (which fires for the entire
    // x-column) with a tight proximity check.
    const _tdTip = document.getElementById('td-tip');
    const TD_RADIUS = 15;   // pixels — how close the cursor must be

    function setupTdHover(idx) {{
      const div = document.getElementById('c' + idx);

      div.addEventListener('mousemove', function(e) {{
        const layout = div._fullLayout;
        if (!layout) {{ _tdTip.style.display = 'none'; return; }}

        const xa   = layout.xaxis;
        const ya   = layout.yaxis;
        const rect = div.getBoundingClientRect();
        const mx   = e.clientX - rect.left;
        const my   = e.clientY - rect.top;

        // Convert a data-space point to pixel coords within the div.
        // l2p() returns px relative to the axis plot-area origin;
        // _offset adds the margin between the div edge and the plot area.
        function toPx(xi, y) {{
          return [xa.l2p(xi) + xa._offset,
                  ya.l2p(y)  + ya._offset];
        }}

        const meta = chartMeta[idx];
        if (!meta) {{ _tdTip.style.display = 'none'; return; }}

        const allTDs = [...(meta.winTDs || []), ...(meta.loseTDs || [])];
        let closest = null, minD = TD_RADIUS;

        for (const td of allTDs) {{
          try {{
            const [px, py] = toPx(td.xi, td.y);
            const d = Math.hypot(mx - px, my - py);
            if (d < minD) {{ minD = d; closest = td; }}
          }} catch (_) {{}}
        }}

        if (closest) {{
          _tdTip.innerHTML = tdText(closest);
          _tdTip.style.display = 'block';
          // Nudge tooltip so it stays inside the viewport
          const tw = Math.min(520, window.innerWidth - e.clientX - 20);
          _tdTip.style.maxWidth = tw + 'px';
          _tdTip.style.left = (e.clientX + 14) + 'px';
          _tdTip.style.top  = (e.clientY - 10) + 'px';
        }} else {{
          _tdTip.style.display = 'none';
        }}
      }});

      div.addEventListener('mouseleave', function() {{
        _tdTip.style.display = 'none';
      }});
    }}

    // ── Animation state ────────────────────────────────────────────────────
    // chartMeta[idx]  – {{ yMax, winName, loseName }} set by renderChart
    // animState[idx]  – {{ rafId }} for cancellation on replay
    const chartMeta  = {{}};
    const animState  = {{}};
    const ANIM_DURATION = 10000;  // ms for a full left-to-right reveal

    function startAnim(idx) {{
      const m = DATA.matchups[idx];
      const {{ winTDs, loseTDs }} = chartMeta[idx];

      // Cancel any running animation for this chart
      if (animState[idx] && animState[idx].rafId) {{
        cancelAnimationFrame(animState[idx].rafId);
      }}
      animState[idx] = {{ rafId: null }};

      // Hide overlay + replay button, reset all four traces to empty
      document.getElementById('overlay-' + idx).classList.add('hidden');
      document.getElementById('replay-' + idx).style.display = 'none';
      Plotly.restyle('c' + idx, {{ x: [[], []], y: [[], []] }}, [0, 1]);
      Plotly.restyle('c' + idx, {{ x: [[], []], y: [[], []], text: [[], []] }}, [2, 3]);

      const isT1Win = m.winner === m.team1;
      const winY    = isT1Win ? m.pts1 : m.pts2;
      const loseY   = isT1Win ? m.pts2 : m.pts1;
      const N       = xVals.length;
      const state   = animState[idx];
      const start   = performance.now();

      function frame(now) {{
        const t = Math.min((now - start) / ANIM_DURATION, 1);
        const n = Math.max(1, Math.ceil(t * N));   // linear — constant speed

        // Update line traces
        Plotly.restyle('c' + idx, {{
          x: [xVals.slice(0, n), xVals.slice(0, n)],
          y: [winY.slice(0, n),  loseY.slice(0, n)],
        }}, [0, 1]);

        // Update TD marker traces: show only markers whose minute index < n
        const visWin  = winTDs.filter(td => td.xi < n);
        const visLose = loseTDs.filter(td => td.xi < n);
        Plotly.restyle('c' + idx, {{
          x:    [visWin.map(td => td.xi),   visLose.map(td => td.xi)],
          y:    [visWin.map(td => td.y),    visLose.map(td => td.y)],
          text: [visWin.map(tdText),         visLose.map(tdText)],
        }}, [2, 3]);

        if (t < 1) {{
          state.rafId = requestAnimationFrame(frame);
        }} else {{
          // Final frame: ensure complete data for all traces
          Plotly.restyle('c' + idx, {{
            x: [xVals, xVals],
            y: [winY,  loseY],
          }}, [0, 1]);
          Plotly.restyle('c' + idx, {{
            x:    [winTDs.map(td => td.xi),  loseTDs.map(td => td.xi)],
            y:    [winTDs.map(td => td.y),   loseTDs.map(td => td.y)],
            text: [winTDs.map(tdText),        loseTDs.map(tdText)],
          }}, [2, 3]);
          document.getElementById('replay-' + idx).style.display = 'block';
        }}
      }}

      state.rafId = requestAnimationFrame(frame);
    }}

    // ── main render loop ───────────────────────────────────────────────────
    const grid = document.getElementById('grid');
    DATA.matchups.forEach((m, i) => {{
      grid.appendChild(buildCard(m, i));
      renderChart(m, i);
    }});

    // ── Twitter widget script (loaded only when needed) ────────────────────
    if (DATA.needs_twitter) {{
      const s = document.createElement('script');
      s.async = true;
      s.src = 'https://platform.twitter.com/widgets.js';
      document.body.appendChild(s);
    }}
  </script>
</body>
</html>
"""


_PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.35.0.min.js"
_PLOTLY_CACHE = Path(__file__).parent / "vendor" / "plotly-2.35.0.min.js"


def _plotly_script_tag(inline: bool = False) -> str:
    """Return the Plotly.js <script> element.

    Default (inline=False): a lightweight CDN <script src="..."> tag (~1 KB).
    The file is 140 KB and opens instantly on any device with internet — the
    right choice for sharing via text or email.

    With inline=True (--offline flag): Plotly.js is embedded directly in the
    HTML (~4.5 MB).  Use this only when recipients genuinely have no internet.
    Note that large inline scripts can crash mobile browsers.
    """
    if not inline:
        return f'<script src="{_PLOTLY_CDN}" charset="utf-8"></script>'

    # ── Offline / inlined mode ────────────────────────────────────────────────
    if not _PLOTLY_CACHE.exists():
        _PLOTLY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading Plotly.js → {_PLOTLY_CACHE} …", end=" ", flush=True)
        try:
            # macOS Python ships without system CA certs; try verified SSL
            # first then fall back to unverified — the CDN URL is known-safe.
            downloaded = False
            for verify in (True, False):
                try:
                    if verify:
                        ctx = ssl.create_default_context()
                    else:
                        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                        ctx.check_hostname = False
                        ctx.verify_mode = ssl.CERT_NONE
                    with urllib.request.urlopen(_PLOTLY_CDN, context=ctx) as r:
                        _PLOTLY_CACHE.write_bytes(r.read())
                    downloaded = True
                    break
                except Exception:
                    if not verify:
                        raise
            if downloaded:
                print(f"done ({_PLOTLY_CACHE.stat().st_size // 1024} KB)")
        except Exception as exc:
            print(f"FAILED ({exc})\n  Falling back to CDN tag.")
            if _PLOTLY_CACHE.exists():
                _PLOTLY_CACHE.unlink()
            return f'<script src="{_PLOTLY_CDN}" charset="utf-8"></script>'

    js = _PLOTLY_CACHE.read_text(encoding="utf-8")
    return f"<script>{js}</script>"


def render_html(payload: dict, commentary: dict | None = None,
                inline_plotly: bool = False) -> str:
    # Merge commentary into payload so it travels inside DATA JSON,
    # which avoids any .format() escaping issues with curly braces.
    intro_html = ""
    commentary_list: list[str] = [""] * len(payload["matchups"])

    if commentary:
        intro_html = commentary.get("intro_html", "") or ""
        matchup_map = commentary.get("matchup_map", {})
        commentary_list = [
            match_commentary(m, matchup_map) for m in payload["matchups"]
        ]

    payload["intro_html"] = intro_html
    payload["commentary"] = commentary_list

    # Load week commentary markdown
    season = payload.get("season") or ""
    commentary_md_path = Path(f"Data/{season}/commentary/week_{payload['week']:02d}.md") if season else None
    week_commentary_html = ""
    if commentary_md_path and commentary_md_path.exists():
        md_text = commentary_md_path.read_text(encoding="utf-8").strip()
        if md_text:
            try:
                import markdown as _md
                week_commentary_html = _md.markdown(md_text, extensions=["nl2br"])
            except ImportError:
                # Fallback: split on blank lines → <p> tags
                paras = [p.strip() for p in md_text.split("\n\n") if p.strip()]
                week_commentary_html = "".join(f"<p>{p}</p>" for p in paras)
    payload["week_commentary_html"] = week_commentary_html

    # Load tweet URLs for this week
    tweets_path = Path(f"Data/{season}/tweets/week_{payload['week']:02d}.json") if season else None
    tweet_urls: list = []
    if tweets_path and tweets_path.exists():
        try:
            tweet_urls = json.load(tweets_path.open())
        except Exception:
            pass
    payload["tweet_urls"] = tweet_urls

    all_text = intro_html + "".join(commentary_list) + ("tweet" if tweet_urls else "")
    payload["needs_twitter"] = "twitter-tweet" in all_text or bool(tweet_urls)

    # Sort matchups highest winning score → lowest for display order.
    payload["matchups"].sort(
        key=lambda m: max(m.get("final1", 0) or 0, m.get("final2", 0) or 0),
        reverse=True,
    )

    # Embed team logo URLs as base64 data URIs so they display from any context
    # (local file, email attachment, mobile) without CORS issues.
    _logo_cache: dict = {}
    def _embed_logo(url: str) -> str:
        if not url:
            return ""
        if url not in _logo_cache:
            print(f"  Fetching logo …", file=sys.stderr)
            _logo_cache[url] = fetch_logo_as_data_uri(url)
        return _logo_cache[url]

    for m in payload["matchups"]:
        m["logo1"] = _embed_logo(m.get("logo1", ""))
        m["logo2"] = _embed_logo(m.get("logo2", ""))
    for row in payload.get("standings", []):
        row["logo"] = _embed_logo(row.get("logo", ""))

    data_json = json.dumps(payload, separators=(",", ":"))
    html = _HTML.format(
        week=payload["week"],
        date_range=payload["date_range"],
        data_json=data_json,
    )
    # Inject Plotly.js after .format() so its many { } chars don't interfere.
    return html.replace("__PLOTLY_SCRIPT__", _plotly_script_tag(inline_plotly), 1)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def load_timeline_json(path: Path) -> dict:
    """Load a pre-built timeline JSON produced by build_timelines.py."""
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a shareable static HTML fantasy matchup report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--week", type=int, required=True,
                        help="NFL week number (e.g. 10)")
    parser.add_argument("--timeline", type=str, default=None,
                        help="Path to a timeline JSON from build_timelines.py (preferred)")
    parser.add_argument("--csv",  type=str, default=None,
                        help="Path to a polling CSV (week_N_scores.csv) — legacy mode")
    parser.add_argument("--out",  type=str, default=None,
                        help="Output HTML file path (default: week_N_matchups.html)")
    parser.add_argument("--commentary", type=str, default=None,
                        help="Path to a YAML commentary file (optional)")
    parser.add_argument("--init-commentary", action="store_true",
                        help="Print a blank YAML commentary template and exit")
    parser.add_argument("--offline", action="store_true",
                        help="Inline Plotly.js into the HTML for viewing without "
                             "internet (makes file ~4.5 MB; may not open on mobile)")
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else Path(f"week_{args.week}_matchups.html")

    # ── New pipeline: timeline JSON ───────────────────────────────────────────
    if args.timeline:
        tl_path = Path(args.timeline)
        if not tl_path.exists():
            print(f"ERROR: timeline file not found: {tl_path}", file=sys.stderr)
            sys.exit(1)
        print(f"Loading  : {tl_path}  (timeline JSON)")
        payload = load_timeline_json(tl_path)
        # Inject season derived from timeline path (e.g. Data/2026/timelines/...)
        import re as _re
        _m = _re.search(r'Data[/\\](\d{4})[/\\]', str(tl_path.resolve()))
        if _m:
            payload["season"] = int(_m.group(1))
        print(f"           Week {payload['week']}  |  "
              f"{len(payload['matchups'])} matchups  |  "
              f"{len(payload['times'])} time points")

    # ── Legacy pipeline: polling CSV ──────────────────────────────────────────
    else:
        csv_path = Path(args.csv) if args.csv else find_csv(args.week)
        print(f"Loading  : {csv_path}  (polling CSV)")
        df = load_csv(csv_path)
        print(f"           {len(df)} rows  |  {len(df.columns)} columns")

        print("Matchups :", end=" ")
        matchups = detect_matchups(df)
        if not matchups:
            if len(df) == 0:
                print("\nERROR: The CSV has headers but no data rows.\n"
                      "The polling loop was never run for this week.",
                      file=sys.stderr)
            else:
                print("\nERROR: Could not detect any matchups from this CSV.\n"
                      "Check that win-probability columns ('{Team} WPCT') are present "
                      "and that at least a few rows have fractional win probabilities.",
                      file=sys.stderr)
            sys.exit(1)

        for m in matchups:
            print(f"\n           {m['team1']}  vs  {m['team2']}", end="")
        print()
        payload = build_payload(df, matchups, args.week)

    # ── Init-commentary: print template and exit ──────────────────────────────
    if args.init_commentary:
        print(build_commentary_template(payload))
        return

    # ── Load optional commentary ──────────────────────────────────────────────
    commentary = None
    if args.commentary:
        cpath = Path(args.commentary)
        if not cpath.exists():
            print(f"ERROR: commentary file not found: {cpath}", file=sys.stderr)
            sys.exit(1)
        print(f"Commentary: {cpath}")
        commentary = load_commentary(cpath)
        n_matched = sum(
            1 for m in payload["matchups"]
            if match_commentary(m, commentary["matchup_map"])
        )
        has_intro = bool(commentary.get("intro_html"))
        print(f"           {n_matched}/{len(payload['matchups'])} matchups have commentary"
              + ("  |  intro present" if has_intro else ""))

    html = render_html(payload, commentary, inline_plotly=args.offline)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    size_kb = out_path.stat().st_size // 1024
    print(f"\nSaved    : {out_path}  ({size_kb} KB)")
    if args.offline:
        print("Mode     : offline — Plotly.js inlined (no internet needed, but large file)")
    else:
        print("Mode     : CDN — send to anyone; opens on any device with internet")

    # ── Publish to docs/ for GitHub Pages ─────────────────────────────────────
    repo_root = Path(__file__).parent.parent
    docs_dir  = repo_root / "docs"
    if docs_dir.is_dir():
        import shutil
        dest = docs_dir / out_path.name
        shutil.copy2(out_path, dest)
        gh_user = "jasonsrothstein"
        gh_repo = "fantasy-football"
        url = f"https://{gh_user}.github.io/{gh_repo}/{out_path.name}"
        print(f"Published: {dest}")
        print(f"Share URL: {url}")
        print(f"           (push to GitHub + enable Pages to activate)")
    else:
        print("Tip      : create a docs/ folder in the repo root to enable GitHub Pages publishing")


if __name__ == "__main__":
    main()
