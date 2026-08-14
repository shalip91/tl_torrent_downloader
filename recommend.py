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
SECRETS_YML = ROOT / "state" / "secrets.yml"
OUTPUT_HTML = ROOT / "recommend.html"
TMDB_BASE   = "https://api.themoviedb.org/3"
IMG_BASE    = "https://image.tmdb.org/t/p/w300"
PLACEHOLDER = "https://via.placeholder.com/300x450/1a1a2e/ffffff?text=No+Poster"

MAX_RESULTS = 40   # cards per section

# Titles with fewer votes than this are treated as effectively unrated: their
# vote_average is pre-release noise (e.g. The Odyssey had 9 votes @ 4.0 before
# release) so we gate them on popularity instead of the rating thresholds.
# Kept modest (30) so established-but-niche returning shows with a real rating
# but a smallish vote count (e.g. Quarterback: 43 votes @ 7.9) aren't wrongly
# demoted to the popularity floor.
MIN_VOTES_FOR_RATING = 30

# Genre IDs to always exclude from results and from the profile
EXCLUDED_GENRES = {16, 10762, 10751, 10764, 10767, 27}  # 16=Animation, 10762=Kids, 10751=Family, 10764=Reality, 10767=Talk, 27=Horror
DOCUMENTARY_GENRE = 99  # TMDB Documentary genre — routed to its own section
PROFILE_JSON     = ROOT / "state" / "genre_profile.json"
WL_IDS_JSON      = ROOT / "state" / "watchlist_ids.json"

CAT_TYPE = {
    "tv":     "tv",
    "movies": "movie",
    "kids":   "tv",
}

# Friendly streaming-platform name -> TMDB network ID(s). Used to filter TV shows
# by the platform they originate on (config: recommend.tv_networks). IDs verified
# against the TMDB /network endpoint.
PLATFORM_NETWORKS = {
    "netflix":    [213],
    "hbo":        [49, 3186],   # HBO + HBO Max
    "max":        [3186],
    "apple":      [2552],       # Apple TV+
    "amazon":     [1024],       # Prime Video
    "prime":      [1024],
    "disney":     [2739],       # Disney+
    "hulu":       [453],
    "paramount":  [4330],       # Paramount+
    "peacock":    [3353],
    "showtime":   [67],
    "starz":      [318],
    "amc":        [174],
    "fx":         [88],
}


def resolve_networks(names: list) -> list:
    """Map a list of friendly platform names to a deduplicated list of TMDB network IDs."""
    ids: list[int] = []
    for name in names or []:
        key = str(name).strip().lower()
        if key in PLATFORM_NETWORKS:
            ids.extend(PLATFORM_NETWORKS[key])
        else:
            print(f"  WARNING: unknown streaming platform '{name}' — skipping. "
                  f"Known: {', '.join(sorted(PLATFORM_NETWORKS))}")
    return sorted(set(ids))

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
    base_movie  = rec.get("minimum_rating_score_movie", 0)
    base_series = rec.get("minimum_rating_score_series", 0)
    base_doc    = rec.get("minimum_rating_score_documentary", base_series)  # defaults to series bar
    base_pop    = rec.get("minimum_popularity_unrated", 0)
    return {
        "next_months":                  rec.get("next_months", 3),
        "minimum_rating_score_movie":   base_movie,
        "minimum_rating_score_series":  base_series,
        # Non-English titles must clear a higher bar. Defaults to base + 0.5
        # (0 stays 0 = no filter) unless explicitly set in config.yml.
        "minimum_rating_score_movie_foreign":  rec.get("minimum_rating_score_movie_foreign",
                                                        base_movie + 0.5 if base_movie else 0),
        "minimum_rating_score_series_foreign": rec.get("minimum_rating_score_series_foreign",
                                                        base_series + 0.5 if base_series else 0),
        # Documentaries get their own rating bar, separate from scripted series.
        "minimum_rating_score_documentary": base_doc,
        "minimum_rating_score_documentary_foreign": rec.get("minimum_rating_score_documentary_foreign",
                                                            base_doc + 0.5 if base_doc else 0),
        # Min votes before a TMDB rating is trusted (else treated as unrated).
        "min_votes_for_rating":         rec.get("min_votes_for_rating", MIN_VOTES_FOR_RATING),
        "minimum_popularity_unrated":   base_pop,
        # Unreleased foreign titles have no real rating, so they're gated on
        # popularity — give them a higher popularity floor than English ones.
        # Defaults to the base floor (no change) unless set in config.yml.
        "minimum_popularity_unrated_foreign": rec.get("minimum_popularity_unrated_foreign", base_pop),
        # How many candidates to pull from TMDB per type before filtering (20/page).
        # Higher = deeper scan but slower. TMDB caps the underlying data at 10000.
        "discover_max_results":         rec.get("discover_max_results", 200),
        # Master on/off switch for the TV streaming-platform filter. When False,
        # all networks are allowed regardless of the tv_networks list below.
        "filter_by_networks":           bool(rec.get("filter_by_networks", True)),
        # Allowed streaming platforms for TV (empty = no restriction). Names are
        # resolved to TMDB network IDs via PLATFORM_NETWORKS.
        "tv_networks":                  rec.get("tv_networks", []) or [],
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
             genre_ids: list[int], upcoming: bool = False, max_pages: int = 3,
             network_ids: list = None) -> list[dict]:
    if media_type == "tv":
        # air_date covers both new shows AND new seasons of existing shows.
        # first_air_date only matches brand-new shows and misses returning seasons.
        if upcoming:
            date_key_gte, date_key_lte = "air_date.gte", "air_date.lte"
        else:
            date_key_gte, date_key_lte = "first_air_date.gte", "first_air_date.lte"
    else:
        # Must be primary_release_date (the film's canonical release). Plain
        # release_date matches ANY regional/re-release entry, so decades-old films
        # with a recent streaming re-issue in the window would flood the list.
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
    base_params["without_genres"] = ",".join(str(g) for g in EXCLUDED_GENRES)
    # Note: no with_original_language filter — non-English titles are allowed, but
    # they must clear a higher rating bar (see the foreign thresholds in build_sections).

    # TV only: restrict to shows originating on the allowed streaming platforms.
    # "|" = OR, so a show on ANY of the listed networks qualifies.
    if media_type == "tv" and network_ids:
        base_params["with_networks"] = "|".join(str(n) for n in network_ids)

    # Fetch up to max_pages (20 results/page). Stop at the last available page
    # (per TMDB's total_pages) or a short page, and respect TMDB's 500-page cap.
    results = []
    total_pages = 1
    for page in range(1, min(max_pages, 500) + 1):
        data = tmdb_get(f"/discover/{media_type}", api_key, {**base_params, "page": page})
        page_results = data.get("results", [])
        results.extend(page_results)
        total_pages = data.get("total_pages", total_pages)
        if page >= total_pages or len(page_results) < 20:
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


def get_tv_release_info(tmdb_id: int, api_key: str,
                        today_str: str, cutoff_str: str) -> dict | None:
    """
    Decide whether a TV show is worth showing, based on when its FIRST episode
    (of the series, or of an upcoming new season) airs.

    Returns:
      {"classification": "new",       "date": <first_air_date>, "season_number": 1}
          -> brand-new series premiering within [today, cutoff]
      {"classification": "returning", "date": <season_premiere>, "season_number": N}
          -> an existing show whose NEXT season's first episode airs within the window
      None
          -> the show is already mid-air (its current season premiered before today)
             or has no confirmed premiere in the window -> skip it entirely.
    """
    data = tmdb_get(f"/tv/{tmdb_id}", api_key, {"language": "en-US"})
    if not data:
        return None

    first_air = data.get("first_air_date") or ""
    networks  = data.get("networks") or []
    network   = networks[0]["name"] if networks else ""   # originating platform

    # Brand-new series: the show itself premieres inside the window.
    if first_air and today_str <= first_air <= cutoff_str:
        return {"classification": "new", "date": first_air, "season_number": 1, "network": network}

    # Returning series: look for a season whose premiere (its first episode's
    # air_date) falls within the window and is still in the future. A season
    # that already started (air_date < today) means the show is mid-air -> skip.
    upcoming_seasons = []
    for s in data.get("seasons", []):
        sn = s.get("season_number", 0)
        ad = s.get("air_date") or ""
        if sn and sn >= 1 and ad and today_str <= ad <= cutoff_str:
            upcoming_seasons.append((ad, sn))

    if upcoming_seasons:
        upcoming_seasons.sort()          # soonest premiere first
        ad, sn = upcoming_seasons[0]
        return {"classification": "returning", "date": ad, "season_number": sn, "network": network}

    return None

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
    today_str  = datetime.now().strftime("%Y-%m-%d")

    # For TV, TMDB discover always returns the show's ORIGINAL first_air_date, not the
    # upcoming season date. If that date is in the past, the show is a returning season —
    # label it as such rather than showing a misleading old premiere date.
    if mt == "tv" and raw_date and raw_date < today_str:
        date_label  = "Returning season"
        is_returning = True
    else:
        date_label  = fmt_date(raw_date) if raw_date else "TBA"
        is_returning = False

    return {
        "id":           item.get("id"),
        "type":         mt,
        "title":        title,
        "lang":         item.get("original_language") or "en",
        "genres":       item.get("genre_ids") or [],
        "network":      "",   # filled in for TV from the /tv/{id} lookup
        "raw_date":     raw_date,
        "date_label":   date_label,
        "is_returning": is_returning,
        "rating":       round(item.get("vote_average", 0), 1),
        "votes":        item.get("vote_count", 0),
        "popularity":   round(item.get("popularity", 0), 1),
        "overview":     item.get("overview", ""),
        "poster":       IMG_BASE + item["poster_path"] if item.get("poster_path") else PLACEHOLDER,
        "url":          f"https://www.themoviedb.org/{mt}/{item.get('id')}",
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


def get_watchlist_ids(watchlist_data: dict, api_key: str) -> set[int]:
    """
    Look up TMDB IDs for every entry in the current watchlist and cache by mtime.
    Used to ensure watchlist shows never appear as recommendations.
    """
    wl_mtime = WATCH_YML.stat().st_mtime

    if WL_IDS_JSON.exists():
        cached = json.loads(WL_IDS_JSON.read_text(encoding="utf-8"))
        if cached.get("watchlist_mtime") == wl_mtime:
            return set(cached.get("ids", []))

    print("  Resolving watchlist TMDB IDs for exclusion…")
    ids: set[int] = set()
    for cat_name, cat in watchlist_data.get("categories", {}).items():
        mt = CAT_TYPE.get(cat_name, "tv")
        for entry in cat.get("watchlist", []):
            if not isinstance(entry, dict):
                continue
            title = entry.get("name", "")
            if not title:
                continue
            result = search_tmdb(title, mt, api_key)
            if result:
                ids.add(result["id"])
            time.sleep(0.25)

    WL_IDS_JSON.parent.mkdir(exist_ok=True)
    WL_IDS_JSON.write_text(json.dumps({
        "watchlist_mtime": wl_mtime,
        "ids": list(ids),
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")
    return ids


def build_sections(api_key: str, watchlist_data: dict,
                   upcoming_months: int, min_rating_movie: float,
                   min_rating_series: float, min_popularity_unrated: float = 0,
                   min_rating_movie_foreign: float = 0, min_rating_series_foreign: float = 0,
                   min_rating_doc: float = 0, min_rating_doc_foreign: float = 0,
                   min_popularity_unrated_foreign: float = 0,
                   min_votes_for_rating: int = MIN_VOTES_FOR_RATING,
                   tv_network_ids: list = None,
                   discover_max_results: int = 200,
                   force_refresh: bool = False) -> list:
    """Returns (new_series, returning_seasons, movies): deduplicated, rating-filtered lists."""
    tv_genres, movie_genres, wl_ids = collect_genres(watchlist_data, api_key, force_refresh)

    today         = datetime.now()
    cutoff_today  = today.strftime("%Y-%m-%d")
    cutoff_future = (today + timedelta(days=upcoming_months * 30)).strftime("%Y-%m-%d")

    max_pages = max(1, -(-discover_max_results // 20))   # ceil division; 20 results/page
    print(f"\nDiscover upcoming ({cutoff_today} → {cutoff_future}, {upcoming_months} month(s), "
          f"up to {max_pages} pages / ~{max_pages * 20} candidates per type)…")
    if tv_network_ids:
        print(f"  TV restricted to networks: {tv_network_ids}")
    upcoming_raw: list[dict] = []
    for mt, genres in [("tv", list(tv_genres)), ("movie", list(movie_genres))]:
        results = discover(mt, api_key, cutoff_today, cutoff_future, genres,
                           upcoming=True, max_pages=max_pages,
                           network_ids=(tv_network_ids if mt == "tv" else None))
        print(f"  {mt}: {len(results)} results")
        upcoming_raw.extend(results)
        time.sleep(0.3)

    seen: set[int] = set()  # no exclusions — deduplicate only
    new_series, returning, documentaries, movie_out = [], [], [], []
    print("\nClassifying TV candidates (new premiere / returning season / already airing)…")
    for item in upcoming_raw:
        nid = item.get("id")
        if not nid or nid in seen:
            continue
        seen.add(nid)
        n = normalise(item)

        # Rating / popularity gate — applied BEFORE the per-show detail lookup
        # so we don't waste an API call on titles that can't make the cut.
        if n["votes"] >= min_votes_for_rating:
            # Enough votes for a meaningful rating (established/returning shows).
            # Non-English titles use a stricter (higher) threshold. Documentaries
            # use their own bar, separate from scripted series.
            is_foreign = n["lang"] != "en"
            if n["type"] == "movie":
                threshold = min_rating_movie_foreign if is_foreign else min_rating_movie
            elif DOCUMENTARY_GENRE in n["genres"]:
                threshold = min_rating_doc_foreign if is_foreign else min_rating_doc
            else:
                threshold = min_rating_series_foreign if is_foreign else min_rating_series
            if threshold > 0 and n["rating"] < threshold:
                continue
        else:
            # Few/no votes (typical for unreleased titles) -- the rating is just
            # pre-release noise, so fall back to TMDB's "popularity" (buzz/search
            # interest) as a floor instead of applying a meaningless rating gate.
            # Non-English titles must clear a higher popularity floor.
            floor = min_popularity_unrated_foreign if n["lang"] != "en" else min_popularity_unrated
            if floor > 0 and n["popularity"] < floor:
                continue

        if n["type"] == "movie":
            movie_out.append(n)
            continue

        # TV: only keep shows whose FIRST episode airs inside the window — either
        # a brand-new series, or an existing show's new season premiere. Shows
        # that are already mid-air (e.g. Silo, S03 already premiered) are dropped.
        info = get_tv_release_info(n["id"], api_key, cutoff_today, cutoff_future)
        time.sleep(0.2)
        if not info:
            continue

        n["raw_date"]     = info["date"]
        n["network"]      = info.get("network", "")
        n["is_returning"] = info["classification"] == "returning"
        if info["classification"] == "returning":
            n["date_label"] = f"Season {info['season_number']} · {fmt_date(info['date'])}"
        else:
            n["date_label"] = f"Premiere · {fmt_date(info['date'])}"

        # Documentaries get their own section (new or returning alike).
        if DOCUMENTARY_GENRE in n["genres"]:
            documentaries.append(n)
        elif n["is_returning"]:
            returning.append(n)
        else:
            new_series.append(n)

    # New series are mostly unrated -> order by soonest premiere, then buzz.
    # Returning seasons have history -> order by rating, then vote count.
    # Movies -> soonest release first.
    new_series.sort(key=lambda x: (x["raw_date"], -x["popularity"]))
    returning.sort(key=lambda x: (-x["rating"], -x["votes"]))
    documentaries.sort(key=lambda x: (x["raw_date"], -x["rating"]))
    movie_out.sort(key=lambda x: (x["raw_date"] or "9999-99-99", -x["popularity"]))

    return (new_series[:MAX_RESULTS], returning[:MAX_RESULTS],
            documentaries[:MAX_RESULTS], movie_out[:MAX_RESULTS])

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
    {network_badge}
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
  .network-badge {{ position: absolute; right: 8px; background: rgba(0,0,0,.78);
                    font-size: .62rem; font-weight: 600; padding: 2px 7px; border-radius: 4px;
                    color: #d8d8e8; max-width: 78%; white-space: nowrap; overflow: hidden;
                    text-overflow: ellipsis; }}
  .info {{ padding: 10px 12px 12px; }}
  .card-title {{ font-weight: 600; font-size: .9rem; white-space: nowrap; overflow: hidden;
                 text-overflow: ellipsis; margin-bottom: 4px; }}
  .meta {{ font-size: .75rem; margin-bottom: 6px; font-weight: 600; }}
  .meta.upcoming {{ color: var(--upcoming); }}
  .meta.returning {{ color: #e67e22; font-weight: 600; }}
  .meta.recent {{ color: #aaa; font-weight: 400; }}
  .overview {{ font-size: .75rem; color: #aaa; line-height: 1.45;
               display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }}
  .empty {{ color: var(--muted); font-style: italic; padding: 12px 0; }}
</style>
</head>
<body>
<h1>🎬 What to Watch</h1>
<p class="subtitle">Generated {date} &nbsp;·&nbsp; Filtered to your genres · premieres in the next {upcoming_months} month(s)</p>

<h2>🆕 New series</h2>
<div class="grid">
{new_cards}
</div>

<h2>🔄 Returning seasons</h2>
<div class="grid">
{returning_cards}
</div>

<h2>🎥 Documentaries</h2>
<div class="grid">
{doc_cards}
</div>

<h2>🎬 Coming soon · movies</h2>
<div class="grid">
{movie_cards}
</div>

</body>
</html>
"""


def make_card(item: dict) -> str:
    rating     = item["rating"]
    popularity = item.get("popularity", 0)
    if rating > 0:
        rating_badge = f'<span class="rating-badge">⭐ {rating}</span>'
    elif popularity > 0:
        # No votes yet -- show popularity instead so it's obvious why an
        # unrated title made the cut (useful for tuning minimum_popularity_unrated).
        rating_badge = f'<span class="rating-badge">🔥 {popularity}</span>'
    else:
        rating_badge = ""
    # Network badge sits just below the rating badge (or at the top if there's no rating).
    network = item.get("network", "")
    if network:
        top = "34px" if rating_badge else "8px"
        net_esc = network.replace("<", "&lt;").replace('"', "&quot;")
        network_badge = f'<span class="network-badge" style="top:{top}">{net_esc}</span>'
    else:
        network_badge = ""
    type_label   = "TV" if item["type"] == "tv" else "Movie"
    date_class   = "returning" if item.get("is_returning") else "upcoming"
    date_icon    = "🔄" if item.get("is_returning") else "📅"
    return CARD_TMPL.format(
        url=item["url"],
        poster=item["poster"],
        placeholder=PLACEHOLDER,
        title=item["title"].replace('"', "&quot;"),
        type=item["type"],
        type_label=type_label,
        rating_badge=rating_badge,
        network_badge=network_badge,
        date_label=item["date_label"],
        date_class=date_class,
        date_icon=date_icon,
        overview=(item["overview"] or "No description available.").replace("<", "&lt;"),
    )


def generate_html(new_series: list, returning: list, documentaries: list, movies: list,
                  upcoming_months: int, gen_date: str) -> str:
    def grid(items: list, empty_msg: str) -> str:
        cards = "\n".join(make_card(r) for r in items)
        return cards if cards else f'<p class="empty">{empty_msg}</p>'
    return HTML_TMPL.format(
        date=gen_date,
        upcoming_months=upcoming_months,
        new_cards=grid(new_series, "No brand-new series premiering in this window."),
        returning_cards=grid(returning, "No returning seasons premiering in this window."),
        doc_cards=grid(documentaries, "No documentaries premiering in this window."),
        movie_cards=grid(movies, "No upcoming movies found for your genres."),
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

    rec_cfg            = load_recommend_cfg()
    next_months        = args.next_months if args.next_months != 3 else rec_cfg["next_months"]
    min_movie          = rec_cfg["minimum_rating_score_movie"]
    min_series         = rec_cfg["minimum_rating_score_series"]
    min_movie_foreign  = rec_cfg["minimum_rating_score_movie_foreign"]
    min_series_foreign = rec_cfg["minimum_rating_score_series_foreign"]
    min_doc            = rec_cfg["minimum_rating_score_documentary"]
    min_doc_foreign    = rec_cfg["minimum_rating_score_documentary_foreign"]
    min_votes          = rec_cfg["min_votes_for_rating"]
    min_popularity     = rec_cfg["minimum_popularity_unrated"]
    min_pop_foreign    = rec_cfg["minimum_popularity_unrated_foreign"]
    discover_max       = rec_cfg["discover_max_results"]
    tv_network_ids     = resolve_networks(rec_cfg["tv_networks"]) if rec_cfg["filter_by_networks"] else []
    if not rec_cfg["filter_by_networks"]:
        print("\nNetwork filter disabled (filter_by_networks: false) — all TV networks allowed.")

    watchlist_data = load_watchlist()
    new_series, returning, documentaries, movies = build_sections(
        api_key, watchlist_data, next_months,
        min_rating_movie=min_movie,
        min_rating_series=min_series,
        min_popularity_unrated=min_popularity,
        min_rating_movie_foreign=min_movie_foreign,
        min_rating_series_foreign=min_series_foreign,
        min_rating_doc=min_doc,
        min_rating_doc_foreign=min_doc_foreign,
        min_popularity_unrated_foreign=min_pop_foreign,
        min_votes_for_rating=min_votes,
        tv_network_ids=tv_network_ids,
        discover_max_results=discover_max,
        force_refresh=args.refresh)

    gen_date = datetime.now().strftime("%d %b %Y, %H:%M")
    html = generate_html(new_series, returning, documentaries, movies, next_months, gen_date)
    OUTPUT_HTML.write_text(html, encoding="utf-8")

    print(f"\n✓ {len(new_series)} new series · {len(returning)} returning seasons · "
          f"{len(documentaries)} documentaries · {len(movies)} movies")
    print(f"  → {OUTPUT_HTML.resolve()}")

    if args.open:
        webbrowser.open(OUTPUT_HTML.as_uri())


if __name__ == "__main__":
    main()
