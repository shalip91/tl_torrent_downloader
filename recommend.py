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
import gzip
import json
import re
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

# IMDb publishes its ratings as a free public dataset (no API key, no rate limit).
# One gzipped TSV of every rated title: tconst \t averageRating \t numVotes.
# We cache it in state/ and refresh it weekly; lookups are a single streaming pass.
IMDB_RATINGS_URL  = "https://datasets.imdbws.com/title.ratings.tsv.gz"
IMDB_RATINGS_GZ   = Path(__file__).parent / "state" / "title.ratings.tsv.gz"
IMDB_MAX_AGE_DAYS = 7

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
# Every title ever auto-added to watchlist.yml. Checked before adding so a
# documentary you deliberately delete from the watchlist doesn't reappear on
# the next run, and so a downloaded (and therefore removed) title stays gone.
AUTO_DOCS_JSON   = ROOT / "state" / "auto_added_docs.json"

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

# Movies have no "network" on TMDB, so platform-owned FILMS are found two other
# ways. Production company works pre-release (an unreleased Netflix film is
# already tagged with the company); watch provider only populates at/after
# release. Used together they cover the whole window. IDs verified against
# TMDB's /search/company and /watch/providers/movie endpoints.
PLATFORM_COMPANIES = {
    "netflix":   [178464, 198834, 185004],
    "apple":     [194232],      # Apple Studios
    "amazon":    [210099],      # Amazon MGM Studios
    "prime":     [210099],
    "hbo":       [14914],       # HBO Documentary Films
    "max":       [14914],
    "hulu":      [308758],
}

PLATFORM_PROVIDERS = {
    "netflix":   [8, 1796],     # Netflix + Netflix Standard with Ads
    "apple":     [350],
    "amazon":    [9],
    "prime":     [9],
    "disney":    [337],
    "hulu":      [15],
    "paramount": [2303, 2616],
    "peacock":   [386],
    "hbo":       [1899],
    "max":       [1899],
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


# Display names for the platform badge on documentary FILMS, which have no
# TMDB "network" of their own to show.
PLATFORM_LABELS = {
    "netflix": "Netflix",   "hbo":       "HBO Max",     "max":     "HBO Max",
    "apple":   "Apple TV+", "amazon":    "Prime Video", "prime":   "Prime Video",
    "disney":  "Disney+",   "hulu":      "Hulu",        "paramount": "Paramount+",
    "peacock": "Peacock",   "showtime":  "Showtime",    "starz":   "STARZ",
    "amc":     "AMC",       "fx":        "FX",
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
        # Platforms whose upcoming documentaries are ALWAYS included, bypassing
        # every rating/popularity floor (docs score far too low on TMDB
        # popularity to survive the normal gate).
        "always_include_doc_platforms": rec.get("always_include_doc_platforms", []) or [],
        # Auto-add those documentaries to watchlist.yml so the crawler hunts them.
        "add_docs_to_watchlist":        bool(rec.get("add_docs_to_watchlist", False)),
        "docs_watchlist_category":      rec.get("docs_watchlist_category", "documentry"),
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


def discover_platform_documentaries(api_key: str, date_gte: str, date_lte: str,
                                    platform_names: list, max_pages: int = 3) -> list[dict]:
    """
    Dedicated pass for documentaries on specific streaming platforms.

    Why this exists: documentaries score an order of magnitude lower on TMDB
    popularity than scripted content — upcoming Netflix docs typically sit
    below 1.0, against a minimum_popularity_unrated floor of 8. Since unreleased
    titles have no votes, they're gated on popularity, so a platform's entire
    documentary slate is filtered out before it ever reaches the page. This pass
    queries for them explicitly; the caller exempts the results from that gate.

    Items are tagged with _always_include so build_sections knows to skip the
    rating/popularity filters for them, and with _platform_label so a
    documentary FILM (which has no TMDB "network") can still show its platform.
    """
    found: list[dict] = []

    def pages(endpoint: str, params: dict) -> list[dict]:
        out = []
        for page in range(1, max_pages + 1):
            data = tmdb_get(endpoint, api_key, {**params, "page": page})
            batch = data.get("results", [])
            out.extend(batch)
            if page >= data.get("total_pages", 1) or len(batch) < 20:
                break
        return out

    # Queried one platform at a time rather than OR-ing every ID together, so
    # each hit can be attributed back to the platform that produced it.
    for name in platform_names or []:
        key = str(name).strip().lower()
        if key not in PLATFORM_NETWORKS:
            print(f"  WARNING: unknown platform '{name}' in always_include_doc_platforms "
                  f"— skipping. Known: {', '.join(sorted(PLATFORM_NETWORKS))}")
            continue
        label = PLATFORM_LABELS.get(key, str(name).title())

        def collect(endpoint: str, params: dict, media_type: str) -> None:
            for x in pages(endpoint, params):
                x.setdefault("media_type", media_type)
                x["_always_include"] = True
                x["_platform_label"] = label
                found.append(x)

        # --- Documentary SERIES, by originating network ---
        collect("/discover/tv", {
            "sort_by": "popularity.desc", "language": "en-US",
            "air_date.gte": date_gte, "air_date.lte": date_lte,
            "with_genres": str(DOCUMENTARY_GENRE),
            "with_networks": "|".join(str(n) for n in PLATFORM_NETWORKS[key])}, "tv")

        # --- Documentary FILMS. Two separate queries, because TMDB can't OR
        #     across with_companies and with_watch_providers in one request. ---
        movie_base = {
            "sort_by": "popularity.desc", "language": "en-US",
            "primary_release_date.gte": date_gte, "primary_release_date.lte": date_lte,
            "with_genres": str(DOCUMENTARY_GENRE),
        }
        if PLATFORM_COMPANIES.get(key):
            collect("/discover/movie", {**movie_base,
                    "with_companies": "|".join(str(c) for c in PLATFORM_COMPANIES[key])}, "movie")
        if PLATFORM_PROVIDERS.get(key):
            collect("/discover/movie", {**movie_base,
                    "with_watch_providers": "|".join(str(p) for p in PLATFORM_PROVIDERS[key]),
                    "watch_region": "US"}, "movie")

    return found


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
    # append_to_response pulls the IMDb ID in the same request — free, and it
    # saves an /external_ids call per show later when attaching IMDb ratings.
    data = tmdb_get(f"/tv/{tmdb_id}", api_key,
                    {"language": "en-US", "append_to_response": "external_ids"})
    if not data:
        return None

    first_air = data.get("first_air_date") or ""
    networks  = data.get("networks") or []
    network   = networks[0]["name"] if networks else ""   # originating platform
    imdb_id   = (data.get("external_ids") or {}).get("imdb_id") or ""

    # Brand-new series: the show itself premieres inside the window.
    if first_air and today_str <= first_air <= cutoff_str:
        return {"classification": "new", "date": first_air, "season_number": 1,
                "network": network, "imdb_id": imdb_id}

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
        return {"classification": "returning", "date": ad, "season_number": sn,
                "network": network, "imdb_id": imdb_id}

    return None

# ---------------------------------------------------------------------------
# IMDb ratings (public dataset — no API key needed)
# ---------------------------------------------------------------------------

def ensure_imdb_dataset() -> Path | None:
    """
    Make sure state/title.ratings.tsv.gz exists and is younger than
    IMDB_MAX_AGE_DAYS, downloading it if not (~8 MB). Returns the path, or a
    stale/None fallback if the download fails so a network hiccup only costs us
    the IMDb column rather than the whole run.
    """
    if IMDB_RATINGS_GZ.exists():
        age_days = (time.time() - IMDB_RATINGS_GZ.stat().st_mtime) / 86400
        if age_days < IMDB_MAX_AGE_DAYS:
            return IMDB_RATINGS_GZ
        print(f"  IMDb ratings dataset is {age_days:.1f} days old — refreshing…")
    else:
        print("  Downloading IMDb ratings dataset (~8 MB, first run only)…")

    tmp = IMDB_RATINGS_GZ.parent / (IMDB_RATINGS_GZ.name + ".tmp")
    try:
        IMDB_RATINGS_GZ.parent.mkdir(exist_ok=True)
        with requests.get(IMDB_RATINGS_URL, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
        tmp.replace(IMDB_RATINGS_GZ)
        return IMDB_RATINGS_GZ
    except (requests.RequestException, OSError) as e:
        print(f"  WARNING: IMDb dataset download failed ({e})")
        try:
            tmp.unlink()
        except OSError:
            pass
        if IMDB_RATINGS_GZ.exists():
            print("  Falling back to the previously cached (stale) copy.")
            return IMDB_RATINGS_GZ
        print("  IMDb scores will be omitted for this run.")
        return None


def lookup_imdb_ratings(imdb_ids: set) -> dict:
    """
    Stream the ratings TSV once, keeping only the tconsts we asked for.
    The file has ~1.6M rows, so we never build a full in-memory index.
    Returns {tconst: (average_rating, num_votes)}.
    """
    if not imdb_ids:
        return {}
    path = ensure_imdb_dataset()
    if not path:
        return {}

    found: dict = {}
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            next(f, None)                      # header row
            for line in f:
                tconst, _, rest = line.partition("\t")
                if tconst not in imdb_ids:
                    continue
                avg, _, votes = rest.partition("\t")
                try:
                    found[tconst] = (round(float(avg), 1), int(votes.strip()))
                except ValueError:
                    continue
                if len(found) == len(imdb_ids):
                    break                      # everything we needed
    except (OSError, EOFError) as e:
        print(f"  WARNING: could not read IMDb dataset ({e}) — IMDb scores omitted.")
    return found


def attach_imdb_ratings(items: list, api_key: str) -> None:
    """
    Fill in imdb_rating / imdb_votes on each item, in place.

    TV items already carry their imdb_id (picked up during the /tv/{id} lookup),
    so only movies cost an extra TMDB call here. Called once on the final,
    already-truncated lists so we never resolve titles that aren't displayed.
    """
    if not items:
        return
    need = [i for i in items if not i.get("imdb_id")]
    if need:
        print(f"  Resolving IMDb IDs for {len(need)} title(s)…")
    for it in need:
        data = tmdb_get(f"/{it['type']}/{it['id']}/external_ids", api_key)
        it["imdb_id"] = data.get("imdb_id") or ""
        time.sleep(0.15)

    ratings = lookup_imdb_ratings({i["imdb_id"] for i in items if i.get("imdb_id")})
    for it in items:
        hit = ratings.get(it.get("imdb_id") or "")
        if hit:
            it["imdb_rating"], it["imdb_votes"] = hit
    print(f"  IMDb ratings found for {len(ratings)}/{len(items)} title(s) "
          f"(unreleased titles usually have none yet).")

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
        # Filled in later by attach_imdb_ratings(); 0 means "no IMDb rating yet",
        # which is the normal case for unreleased titles.
        "imdb_id":      "",
        "imdb_rating":  0.0,
        "imdb_votes":   0,
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
        for e in (cat.get("watchlist") or [])
        if not str(list(e)[0]).lower().startswith("dummy")
    )
    print(f"\nBuilding genre profile for {total} watchlist titles…")

    for cat_name, cat in watchlist_data.get("categories", {}).items():
        mt = CAT_TYPE.get(cat_name, "tv")
        for entry in (cat.get("watchlist") or []):
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
        for entry in (cat.get("watchlist") or []):
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
                   always_include_doc_platforms: list = None,
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

    # Guaranteed platform-documentary lane. Runs FIRST so these titles claim
    # their IDs in the `seen` set before the popularity-gated main scan can
    # reach them (and so a doc film lands in the documentaries section rather
    # than being swept into "coming soon · movies").
    if always_include_doc_platforms:
        print(f"\nAlways-include documentary pass "
              f"({', '.join(always_include_doc_platforms)}) — bypasses rating/popularity floors…")
        plat_docs = discover_platform_documentaries(
            api_key, cutoff_today, cutoff_future, always_include_doc_platforms)
        print(f"  {len(plat_docs)} documentary candidate(s) found")
        upcoming_raw.extend(plat_docs)
        time.sleep(0.3)

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
        # Titles from the always-include documentary lane skip every quality
        # gate below — they were requested explicitly by platform.
        always = bool(item.get("_always_include"))
        # Recorded on the item so the watchlist auto-add can tell a platform
        # documentary apart from one that merely passed the normal filters.
        n["always_include"] = always
        if always:
            # Films have no TMDB network; fall back to the platform this title
            # was matched on so the badge still identifies where it lands.
            n["network"] = item.get("_platform_label", "")

        # Rating / popularity gate — applied BEFORE the per-show detail lookup
        # so we don't waste an API call on titles that can't make the cut.
        if always:
            pass
        elif n["votes"] >= min_votes_for_rating:
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
            # Documentary films from the always-include lane belong in the
            # documentaries section, not buried among the scripted movies.
            if always and DOCUMENTARY_GENRE in n["genres"]:
                documentaries.append(n)
            else:
                movie_out.append(n)
            continue

        # TV: only keep shows whose FIRST episode airs inside the window — either
        # a brand-new series, or an existing show's new season premiere. Shows
        # that are already mid-air (e.g. Silo, S03 already premiered) are dropped.
        info = get_tv_release_info(n["id"], api_key, cutoff_today, cutoff_future)
        time.sleep(0.2)
        if info:
            n["raw_date"]     = info["date"]
            # Keep the platform fallback if TMDB lists no network for the show.
            n["network"]      = info.get("network", "") or n["network"]
            n["imdb_id"]      = info.get("imdb_id", "")
            n["is_returning"] = info["classification"] == "returning"
            if info["classification"] == "returning":
                n["date_label"] = f"Season {info['season_number']} · {fmt_date(info['date'])}"
            else:
                n["date_label"] = f"Premiere · {fmt_date(info['date'])}"
        elif always:
            # TMDB has no confirmed premiere/season data for this title yet, but
            # it matched the platform documentary query — keep it (with whatever
            # date discover matched on) instead of silently dropping it.
            n["date_label"] = fmt_date(n["raw_date"]) if n["raw_date"] else "TBA"
        else:
            continue

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
    documentaries.sort(key=lambda x: (x["raw_date"] or "9999-99-99", -x["rating"]))
    movie_out.sort(key=lambda x: (x["raw_date"] or "9999-99-99", -x["popularity"]))

    sections = (new_series[:MAX_RESULTS], returning[:MAX_RESULTS],
                documentaries[:MAX_RESULTS], movie_out[:MAX_RESULTS])

    # IMDb scores are attached last, to the already-truncated lists, so we only
    # pay the per-movie /external_ids lookup for titles that actually get shown.
    print("\nAttaching IMDb ratings…")
    attach_imdb_ratings([item for section in sections for item in section], api_key)

    return sections

# ---------------------------------------------------------------------------
# Watchlist auto-add
# ---------------------------------------------------------------------------

# TMDB titles use typographic punctuation (’ – …) that never survives scene
# release naming, and crawler.py matches a watchlist name as a plain SUBSTRING
# of the torrent name after collapsing only [\s._-]. So "Schumacher ’94 – The
# Birth of a Legend" can never match "Schumacher.94.The.Birth.of.a.Legend...".
# These are stripped/flattened so the stored name is one the crawler can hit.
_TITLE_SUBS = {
    "’": "", "‘": "", "'": "",        # apostrophes: dropped by scene naming
    "“": "", "”": "", '"': "",        # quotes
    "–": " ", "—": " ", "‒": " ",  # en/em dashes -> space
    "…": " ", " ": " ",                # ellipsis, nbsp
}


def sanitise_title(title: str) -> str:
    """Turn a TMDB display title into something crawler.py can actually match."""
    t = title
    for src, dst in _TITLE_SUBS.items():
        t = t.replace(src, dst)
    # Punctuation that release names drop entirely. '&' is kept — it survives in
    # scene names and existing watchlist entries rely on it.
    t = re.sub(r"[:;,/\\!?*()\[\]]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def match_key(name: str) -> str:
    """Mirror of crawler.py's _normalise, for duplicate detection."""
    return re.sub(r"[\s._\-]+", " ", sanitise_title(name)).strip().lower()


def load_auto_added() -> set:
    if not AUTO_DOCS_JSON.exists():
        return set()
    try:
        data = json.loads(AUTO_DOCS_JSON.read_text(encoding="utf-8"))
        return {match_key(t) for t in data.get("added", [])}
    except (OSError, ValueError):
        return set()


def save_auto_added(names: list) -> None:
    prev = []
    if AUTO_DOCS_JSON.exists():
        try:
            prev = json.loads(AUTO_DOCS_JSON.read_text(encoding="utf-8")).get("added", [])
        except (OSError, ValueError):
            prev = []
    AUTO_DOCS_JSON.parent.mkdir(exist_ok=True)
    AUTO_DOCS_JSON.write_text(json.dumps({
        "added":      sorted(set(prev) | set(names)),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2, ensure_ascii=False), encoding="utf-8")


def add_entries_to_watchlist_category(category: str, entries: list) -> int:
    """
    Append entries to `category`'s watchlist in watchlist.yml.

    Edits the file as TEXT rather than round-tripping through yaml.safe_dump,
    which would strip every comment and reflow the hand-aligned entries in all
    the other categories. Returns the number of lines added, or -1 if the
    category (or its watchlist: key) couldn't be found.

    entries: [{"name": str, "next_episode": str | None}, ...]
    """
    lines  = WATCH_YML.read_text(encoding="utf-8").splitlines()
    cat_re = re.compile(rf"^(\s*){re.escape(category)}:\s*$")

    start, indent = None, ""
    for i, ln in enumerate(lines):
        m = cat_re.match(ln)
        if m:
            start, indent = i, m.group(1)
            break
    if start is None:
        return -1

    # Find this category's "watchlist:" key, stopping if we dedent into the next one.
    wl_idx = None
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln.startswith(indent + " "):
            break
        if re.match(r"^\s*watchlist:\s*(\[\s*\])?\s*$", ln):
            wl_idx = i
            break
    if wl_idx is None:
        return -1

    # Insert after the last existing "- {...}" entry, or right after the key.
    insert_at = wl_idx + 1
    for i in range(wl_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("- "):
            insert_at = i + 1
        elif stripped == "":
            continue
        else:
            break

    # "watchlist: []" must lose the [] before block entries can follow it.
    lines[wl_idx] = re.sub(r"watchlist:\s*\[\s*\]\s*$", "watchlist:", lines[wl_idx])

    entry_indent = indent + "  "
    width = max(len(e["name"]) for e in entries)
    new_lines = []
    for e in entries:
        if e.get("next_episode"):
            pad = " " * (width - len(e["name"]) + 1)
            new_lines.append(f'{entry_indent}- {{name: "{e["name"]}",{pad}'
                             f'next_episode: "{e["next_episode"]}"}}')
        else:
            new_lines.append(f'{entry_indent}- {{name: "{e["name"]}"}}')

    lines[insert_at:insert_at] = new_lines
    WATCH_YML.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(new_lines)


def sync_docs_to_watchlist(documentaries: list, category: str) -> list:
    """
    Add newly-discovered platform documentaries to watchlist.yml so the crawler
    starts hunting for them. Only titles from the always-include platform lane
    are considered — not every documentary that happened to pass the filters.

    Skips anything already in ANY category of the watchlist, and anything
    previously auto-added (so deletions and completed downloads stay gone).
    Returns the list of display titles added.
    """
    docs = [d for d in documentaries if d.get("always_include")]
    if not docs:
        print("  No platform documentaries to consider.")
        return []

    wl = load_watchlist()
    existing = set()
    for cat in (wl.get("categories") or {}).values():
        for e in (cat.get("watchlist") or []):
            if isinstance(e, dict) and e.get("name"):
                existing.add(match_key(str(e["name"])))

    if category not in (wl.get("categories") or {}):
        print(f"  WARNING: category '{category}' not found in watchlist.yml — skipping auto-add.")
        return []

    already  = load_auto_added()
    pending, added_titles = [], []
    for d in docs:
        clean = sanitise_title(d["title"])
        key   = match_key(clean)
        if not clean:
            continue
        if key in existing:
            print(f"    · {d['title']} — already in watchlist")
            continue
        if key in already:
            print(f"    · {d['title']} — previously added (removed by you or downloaded); skipping")
            continue
        if key in {match_key(p['name']) for p in pending}:
            continue
        # A documentary SERIES needs an episode pointer; a documentary FILM has
        # none, which is exactly how crawler.py tells the two apart.
        entry = {"name": clean, "next_episode": "S01E01" if d["type"] == "tv" else None}
        pending.append(entry)
        added_titles.append(clean)
        kind = "series" if d["type"] == "tv" else "film"
        note = f' (cleaned from "{d["title"]}")' if clean != d["title"] else ""
        print(f"    + {clean}  [{kind}]{note}")

    if not pending:
        print("  Nothing new to add.")
        return []

    written = add_entries_to_watchlist_category(category, pending)
    if written < 0:
        print(f"  WARNING: could not locate '{category}:' watchlist in {WATCH_YML.name} — nothing added.")
        return []

    save_auto_added(added_titles)

    # Make sure the category's download_dir exists, or the crawler has nowhere
    # to put the .torrent files it fetches.
    dl_dir = (wl.get("categories", {}).get(category) or {}).get("download_dir", "")
    if dl_dir:
        try:
            Path(dl_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"  WARNING: could not create download dir {dl_dir}: {e}")

    print(f"  Added {written} documentary entr{'y' if written == 1 else 'ies'} "
          f"to '{category}' in {WATCH_YML.name}")
    return added_titles

# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CARD_TMPL = """
<div class="card" onclick="window.open('{url}','_blank')">
  <div class="poster-wrap">
    <img class="poster" src="{poster}" alt="{title}" loading="lazy"
         onerror="this.src='{placeholder}'">
    <span class="badge badge-{type}">{type_label}</span>
    {badges}
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
  /* Top-right stack: IMDb score, TMDB score, then the originating network.
     A flex column means chips stack automatically however many are present. */
  .badges {{ position: absolute; top: 8px; right: 8px; display: flex; flex-direction: column;
             align-items: flex-end; gap: 4px; max-width: 80%; }}
  .chip {{ background: rgba(0,0,0,.78); font-size: .7rem; font-weight: 700; line-height: 1.35;
           padding: 2px 7px; border-radius: 4px; white-space: nowrap;
           text-decoration: none; display: block; }}
  .chip-imdb {{ color: #f5c518; }}            /* IMDb yellow */
  .chip-imdb:hover {{ background: #f5c518; color: #000; }}
  .chip-tmdb {{ color: #5ad9a5; }}            /* TMDB green  */
  .chip-pop  {{ color: #ff8a5c; }}            /* popularity fallback */
  .chip-net  {{ font-size: .62rem; font-weight: 600; color: #d8d8e8;
                overflow: hidden; text-overflow: ellipsis; max-width: 100%; }}
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
    chips: list[str] = []

    # IMDb first — it's the score most people anchor on. Absent for most
    # unreleased titles, which simply have no IMDb rating yet.
    imdb_rating = item.get("imdb_rating", 0)
    if imdb_rating > 0:
        imdb_votes = item.get("imdb_votes", 0)
        href = f'https://www.imdb.com/title/{item["imdb_id"]}/' if item.get("imdb_id") else ""
        label = f'IMDb {imdb_rating}'
        title_attr = f'IMDb — {imdb_votes:,} votes'
        if href:
            # stopPropagation so clicking the chip opens IMDb instead of the
            # card's own TMDB link.
            chips.append(f'<a class="chip chip-imdb" href="{href}" target="_blank" '
                         f'title="{title_attr}" onclick="event.stopPropagation()">{label}</a>')
        else:
            chips.append(f'<span class="chip chip-imdb" title="{title_attr}">{label}</span>')

    if rating > 0:
        chips.append(f'<span class="chip chip-tmdb" title="TMDB — {item["votes"]:,} votes">'
                     f'TMDB {rating}</span>')
    elif popularity > 0:
        # No votes yet -- show popularity instead so it's obvious why an
        # unrated title made the cut (useful for tuning minimum_popularity_unrated).
        chips.append(f'<span class="chip chip-pop" title="TMDB popularity (no rating yet)">'
                     f'🔥 {popularity}</span>')

    network = item.get("network", "")
    if network:
        net_esc = network.replace("<", "&lt;").replace('"', "&quot;")
        chips.append(f'<span class="chip chip-net">{net_esc}</span>')

    badges       = f'<div class="badges">{"".join(chips)}</div>' if chips else ""
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
        badges=badges,
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
    parser.add_argument("--no-watchlist", action="store_true",
                        help="Don't auto-add documentaries to watchlist.yml this run")
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
        always_include_doc_platforms=rec_cfg["always_include_doc_platforms"],
        force_refresh=args.refresh)

    # Feed newly-found platform documentaries into watchlist.yml so the crawler
    # picks them up on its next cycle.
    if rec_cfg["add_docs_to_watchlist"] and not args.no_watchlist:
        print(f"\nAuto-adding platform documentaries to "
              f"{WATCH_YML.name} → '{rec_cfg['docs_watchlist_category']}'…")
        sync_docs_to_watchlist(documentaries, rec_cfg["docs_watchlist_category"])

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
