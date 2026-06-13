#!/usr/bin/env python3
"""
TMDB Recommendations — Upcoming Releases
-----------------------------------------
Reads watchlist.yml, extracts your taste via genre IDs, then queries TMDB's
/discover endpoint to find upcoming releases (future content only).

Usage:
    python recommend.py
    python recommend.py --open               # auto-opens recommend.html after generating
    python recommend.py --next_months 6      # next 6 months (default 3)
    python recommend.py --refresh            # rebuild genre profile from watchlist
"""

import argparse
import json
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml

# ---------------------------------------------------------------------------
ROOT        = Path(__file__).parent
WATCH_YML   = ROOT / "watchlist.yml"
SECRETS_YML = ROOT / "secrets.yml"
OUTPUT_HTML = ROOT / "recommend.html"
TMDB_BASE   = "https://api.themoviedb.org/3"
IMG_BASE    = "https://image.tmdb.org/t/p/w300"
PLACEHOLDER = "https://via.placeholder.com/300x450/1a1a2e/ffffff?text=No+Poster"

MAX_RESULTS = 40   # cards per section

# Genre IDs to always exclude from results and from the profile
EXCLUDED_GENRES = {16, 10762, 10751, 10764, 10767}  # 16=Animation, 10762=Kids, 10751=Family, 10764=Reality, 10767=Talk
PROFILE_JSON     = ROOT / "state" / "genre_profile.json"

CAT_TYPE = {
    "tv":     "tv",
    "movies": "movie",
    "kids":   "tv",
}

# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_secrets() -> dict:
    if not SECRETS_YML.exists():
        print(f"ERROR: {SECRETS_YML} not found.")
        sys.exit(1)
    with open(SECRETS_YML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_watchlist() -> dict:
    with open(WATCH_YML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_recommend_cfg() -> dict:
    """Read the recommend: section from config.yml, with safe defaults."""
    with open(ROOT / "config.yml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    rec = cfg.get("recommend", {})
    return {
        "next_months":                  rec.get("next_months", 3),
        "minimum_rating_score_movie":   rec.get("minimum_rating_score_movie", 0),
        "minimum_rating_score_series":  rec.get("minimum_rating_score_series", 0),
    }

# ---------------------------------------------------------------------------
# TMDB helpers
# ---------------------------------------------------------------------------

def tmdb_get(endpoint: str, api_key: str, params: dict = None) -> dict:
    p = dict(params or {})
    p["api_key"] = api_key
    try:
        r = requests.get(f"{TMDB_BASE}{endpoint}", params=p, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"  TMDB error ({endpoint}): {e}")
        return {}


def search_tmdb(title: str, media_type: str, api_key: str) -> dict | None:
    data = tmdb_get(f"/search/{media_type}", api_key, {"query": title, "language": "en-US"})
    results = data.get("results", [])
    return results[0] if results else None


def get_genres(tmdb_id: int, media_type: str, api_key: str) -> list[int]:
    data = tmdb_get(f"/{media_type}/{tmdb_id}", api_key, {"language": "en-US"})
    return [g["id"] for g in data.get("genres", [])]


def discover(media_type: str, api_key: str, date_gte: str, date_lte: str,
             genre_ids: list[int], upcoming: bool = False) -> list[dict]:
    if media_type == "tv":
        # air_date covers both new shows AND new seasons of existing shows.
        # first_air_date only matches brand-new shows and misses returning seasons.
        if upcoming:
            date_key_gte, date_key_lte = "air_date.gte", "air_date.lte"
        else:
            date_key_gte, date_key_lte = "first_air_date.gte", "first_air_date.lte"
    else:
        date_key_gte, date_key_lte = "primary_release_date.gte", "primary_release_date.lte"

    base_params = {
        "sort_by":   "popularity.desc",
        "language":  "en-US",
        date_key_gte: date_gte,
        date_key_lte: date_lte,
    }
    # Upcoming content has 0 votes — don't filter by vote count
    if not upcoming:
        base_params["vote_count.gte"] = 3

    if genre_ids:
        base_params["with_genres"] = "|".join(str(g) for g in genre_ids)   # OR logic
    base_params["without_genres"]        = ",".join(str(g) for g in EXCLUDED_GENRES)
    base_params["with_original_language"] = "en"

    # Fetch up to 3 pages (60 results); stop early if a page is empty
    results = []
    for page in range(1, 4):
        data = tmdb_get(f"/discover/{media_type}", api_key, {**base_params, "page": page})
        page_results = data.get("results", [])
        results.extend(page_results)
        if len(page_results) < 20:
            break

    # If genre filter produced too few results, fetch again without genre constraint
    if len(results) < 5 and genre_ids:
        fallback_params = {k: v for k, v in base_params.items() if k != "with_genres"}
        for page in range(1, 3):
            data = tmdb_get(f"/discover/{media_type}", api_key, {**fallback_params, "page": page})
            page_results = data.get("results", [])
            results.extend(page_results)
            if len(page_results) < 20:
                break

    for r in results:
        r.setdefault("media_type", media_type)
    return results

# ---------------------------------------------------------------------------
# Normalise
# ---------------------------------------------------------------------------

def fmt_date(raw: str) -> str:
    """'2026-07-15' → 'Jul 15, 2026'"""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%b %d, %Y")
    except Exception:
        return raw


def normalise(item: dict) -> dict:
    mt         = item.get("media_type", "movie")
    title      = item.get("title") or item.get("name") or "?"
    raw_date   = item.get("release_date") or item.get("first_air_date") or ""
    return {
        "id":         item.get("id"),
        "type":       mt,
        "title":      title,
        "raw_date":   raw_date,
        "date_label": fmt_date(raw_date) if raw_date else "TBA",
        "rating":     round(item.get("vote_average", 0), 1),
        "votes":      item.get("vote_count", 0),
        "overview":   item.get("overview", ""),
        "poster":     IMG_BASE + item["poster_path"] if item.get("poster_path") else PLACEHOLDER,
        "url":        f"https://www.themoviedb.org/{mt}/{item.get('id')}",
    }

# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _build_genre_profile(watchlist_data: dict, api_key: str) -> dict:
    """Fetch TMDB genre IDs for every title in the watchlist. Slow — cached after first run."""
    tv_genres:    set[int] = set()
    movie_genres: set[int] = set()
    seen_titles:  set[str] = set()
    wl_ids:       set[int] = set()

    total = sum(
        1 for cat in watchlist_data.get("categories", {}).values()
        for e in cat.get("watchlist", [])
        if not str(list(e)[0]).lower().startswith("dummy")
    )
    print(f"\nBuilding genre profile for {total} watchlist titles…")

    for cat_name, cat in watchlist_data.get("categories", {}).items():
        mt = CAT_TYPE.get(cat_name, "tv")
        for entry in cat.get("watchlist", []):
            title = str(list(entry)[0])
            if title.lower().startswith("dummy"):
                continue
            if title.lower() in seen_titles:
                continue
            seen_titles.add(title.lower())

            print(f"  [{mt}] {title}…", end=" ", flush=True)
            result = search_tmdb(title, mt, api_key)
            time.sleep(0.25)
            if not result:
                print("not found")
                continue

            tmdb_id = result["id"]
            wl_ids.add(tmdb_id)
            genres = get_genres(tmdb_id, mt, api_key)
            time.sleep(0.25)
            print(f"genres: {genres}")

            if mt == "tv":
                tv_genres.update(genres)
            else:
                movie_genres.update(genres)

    return {
        "tv_genres":       list(tv_genres),
        "movie_genres":    list(movie_genres),
        "wl_ids":          list(wl_ids),
        "watchlist_mtime": WATCH_YML.stat().st_mtime,
        "built_at":        datetime.now().isoformat(timespec="seconds"),
    }


def collect_genres(watchlist_data: dict, api_key: str,
                   force_refresh: bool = False) -> tuple[set[int], set[int], set[int]]:
    """
    Load genre profile from cache, rebuilding only when watchlist.yml has changed
    (or --refresh is passed).
    Returns (tv_genres, movie_genres, watchlist_tmdb_ids).
    """
    wl_mtime = WATCH_YML.stat().st_mtime

    if not force_refresh and PROFILE_JSON.exists():
        profile = json.loads(PROFILE_JSON.read_text(encoding="utf-8"))
        source  = profile.get("source", "watchlist")

        if source == "disk":
            # Disk-built profile — never auto-rebuild from watchlist; use --refresh to redo disk scan
            print(f"\nUsing disk-built genre profile (built {profile.get('built_at', '?')}, scanned {profile.get('scan_root', '?')})")
            print("  Run  python build_profile.py  to rescan your disk.")
            return (set(profile["tv_genres"])    - EXCLUDED_GENRES,
                    set(profile["movie_genres"]) - EXCLUDED_GENRES,
                    set(profile["wl_ids"]))

        if profile.get("watchlist_mtime") == wl_mtime:
            print(f"\nUsing cached genre profile (built {profile.get('built_at', '?')})")
            print("  Run with --refresh to rebuild.")
            return (set(profile["tv_genres"])    - EXCLUDED_GENRES,
                    set(profile["movie_genres"]) - EXCLUDED_GENRES,
                    set(profile["wl_ids"]))
        print("\nwatchlist.yml has changed — rebuilding genre profile…")
    else:
        if force_refresh:
            print("\n--refresh: rebuilding genre profile…")

    profile = _build_genre_profile(watchlist_data, api_key)
    PROFILE_JSON.parent.mkdir(exist_ok=True)
    PROFILE_JSON.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print(f"  Profile saved → {PROFILE_JSON}")

    return (set(profile["tv_genres"])    - EXCLUDED_GENRES,
            set(profile["movie_genres"]) - EXCLUDED_GENRES,
            set(profile["wl_ids"]))


def build_sections(api_key: str, watchlist_data: dict,
                   upcoming_months: int, min_rating_movie: float,
                   min_rating_series: float, force_refresh: bool = False) -> list:
    """Returns a deduplicated, rating-filtered list of normalised upcoming release dicts."""
    tv_genres, movie_genres, wl_ids = collect_genres(watchlist_data, api_key, force_refresh)

    today         = datetime.now()
    cutoff_today  = today.strftime("%Y-%m-%d")
    cutoff_future = (today + timedelta(days=upcoming_months * 30)).strftime("%Y-%m-%d")

    print(f"\nDiscover upcoming ({cutoff_today} → {cutoff_future}, {upcoming_months} month(s))…")
    upcoming_raw: list[dict] = []
    for mt, genres in [("tv", list(tv_genres)), ("movie", list(movie_genres))]:
        results = discover(mt, api_key, cutoff_today, cutoff_future, genres, upcoming=True)
        print(f"  {mt}: {len(results)} results")
        upcoming_raw.extend(results)
        time.sleep(0.3)

    seen: set[int] = set(wl_ids)
    out  = []
    for item in upcoming_raw:
        nid = item.get("id")
        if not nid or nid in seen:
            continue
        seen.add(nid)
        n = normalise(item)
        # Apply rating filter only when votes exist (unrated upcoming content always passes)
        if n["votes"] > 0:
            threshold = min_rating_movie if n["type"] == "movie" else min_rating_series
            if threshold > 0 and n["rating"] < threshold:
                continue
        out.append(n)

    out.sort(key=lambda x: (-x["rating"], -x["votes"]))
    return out[:MAX_RESULTS]

# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CARD_TMPL = """
<div class="card" onclick="window.open('{url}','_blank')">
  <div class="poster-wrap">
    <img class="poster" src="{poster}" alt="{title}" loading="lazy"
         onerror="this.src='{placeholder}'">
    <span class="badge badge-{type}">{type_label}</span>
    {rating_badge}
  </div>
  <div class="info">
    <div class="card-title" title="{title}">{title}</div>
    <div class="meta {date_class}">{date_icon} {date_label}</div>
    <div class="overview">{overview}</div>
  </div>
</div>
"""

HTML_TMPL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>What to Watch – {date}</title>
<style>
  :root {{
    --bg: #0d0d1a; --surface: #161627; --border: #2a2a45;
    --accent: #e50914; --text: #e8e8f0; --muted: #888;
    --tv: #1a7fc1; --movie: #c17a1a;
    --upcoming: #27ae60; --recent: #8e44ad;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; padding: 24px; }}
  h1 {{ font-size: 1.6rem; margin-bottom: 4px; }}
  .subtitle {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 32px; }}
  h2 {{ font-size: 1.15rem; color: var(--muted); text-transform: uppercase;
        letter-spacing: .08em; margin: 36px 0 16px;
        border-bottom: 1px solid var(--border); padding-bottom: 8px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(175px, 1fr)); gap: 18px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
           overflow: hidden; cursor: pointer; transition: transform .15s, border-color .15s; }}
  .card:hover {{ transform: translateY(-4px); border-color: var(--accent); }}
  .poster-wrap {{ position: relative; }}
  .poster {{ width: 100%; aspect-ratio: 2/3; object-fit: cover; display: block; }}
  .badge {{ position: absolute; top: 8px; left: 8px; font-size: .65rem; font-weight: 700;
            padding: 2px 7px; border-radius: 4px; text-transform: uppercase; }}
  .badge-tv {{ background: var(--tv); }}
  .badge-movie {{ background: var(--movie); }}
  .rating-badge {{ position: absolute; top: 8px; right: 8px; background: rgba(0,0,0,.75);
                   font-size: .75rem; font-weight: 700; padding: 3px 7px; border-radius: 4px;
                   color: #f5c518; }}
  .info {{ padding: 10px 12px 12px; }}
  .card-title {{ font-weight: 600; font-size: .9rem; white-space: nowrap; overflow: hidden;
                 text-overflow: ellipsis; margin-bottom: 4px; }}
  .meta {{ font-size: .75rem; margin-bottom: 6px; font-weight: 600; }}
  .meta.upcoming {{ color: var(--upcoming); }}
  .meta.recent {{ color: #aaa; font-weight: 400; }}
  .overview {{ font-size: .75rem; color: #aaa; line-height: 1.45;
               display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }}
  .empty {{ color: var(--muted); font-style: italic; padding: 12px 0; }}
</style>
</head>
<body>
<h1>🎬 What to Watch</h1>
<p class="subtitle">Generated {date} &nbsp;·&nbsp; Filtered to your genres · next {upcoming_months} months</p>

<h2>🚀 Coming soon</h2>
<div class="grid">
{upcoming_cards}
</div>

</body>
</html>
"""


def make_card(item: dict) -> str:
    rating       = item["rating"]
    rating_badge = (f'<span class="rating-badge">⭐ {rating}</span>' if rating > 0 else "")
    type_label   = "TV" if item["type"] == "tv" else "Movie"
    return CARD_TMPL.format(
        url=item["url"],
        poster=item["poster"],
        placeholder=PLACEHOLDER,
        title=item["title"].replace('"', "&quot;"),
        type=item["type"],
        type_label=type_label,
        rating_badge=rating_badge,
        date_label=item["date_label"],
        date_class="upcoming",
        date_icon="📅",
        overview=(item["overview"] or "No description available.").replace("<", "&lt;"),
    )


def generate_html(upcoming: list, upcoming_months: int, gen_date: str) -> str:
    u_cards = "\n".join(make_card(r) for r in upcoming)
    if not u_cards:
        u_cards = '<p class="empty">No upcoming titles found for your genres.</p>'
    return HTML_TMPL.format(
        date=gen_date,
        upcoming_months=upcoming_months,
        upcoming_cards=u_cards,
    )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open", action="store_true", help="Open recommend.html when done")
    parser.add_argument("--next_months", type=int, default=3,
                        help="How many months ahead to include upcoming releases (default 3)")
    parser.add_argument("--refresh", action="store_true",
                        help="Force rebuild of genre profile from watchlist")
    args = parser.parse_args()

    secrets = load_secrets()
    api_key = secrets.get("tmdb_api_key", "")
    if not api_key:
        print("ERROR: tmdb_api_key not set in secrets.yml")
        sys.exit(1)

    rec_cfg        = load_recommend_cfg()
    next_months    = args.next_months if args.next_months != 3 else rec_cfg["next_months"]
    min_movie      = rec_cfg["minimum_rating_score_movie"]
    min_series     = rec_cfg["minimum_rating_score_series"]

    watchlist_data = load_watchlist()
    upcoming = build_sections(api_key, watchlist_data, next_months, min_movie, min_series, args.refresh)

    gen_date = datetime.now().strftime("%d %b %Y, %H:%M")
    html = generate_html(upcoming, next_months, gen_date)
    OUTPUT_HTML.write_text(html, encoding="utf-8")

    print(f"\n✓ {len(upcoming)} upcoming titles")
    print(f"  → {OUTPUT_HTML.resolve()}")

    if args.open:
        webbrowser.open(OUTPUT_HTML.as_uri())


if __name__ == "__main__":
    main()
