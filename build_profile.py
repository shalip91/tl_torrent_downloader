#!/usr/bin/env python3
"""
Build genre profile by scanning a media folder on disk.
-------------------------------------------------------
Walks a directory (default: D:\\), finds all video files, extracts titles
from filenames, looks each unique title up on TMDB, collects genre IDs,
and saves the result to state/genre_profile.json.

recommend.py reads that file automatically — run this once (or whenever
your collection changes) instead of deriving the profile from the watchlist.

Usage:
    python build_profile.py                   # scan D:\\
    python build_profile.py --path E:\\Media  # scan a different folder
"""

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import requests
import yaml

# ---------------------------------------------------------------------------
ROOT        = Path(__file__).parent
SECRETS_YML = ROOT / "secrets.yml"
PROFILE_JSON = ROOT / "state" / "genre_profile.json"

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v",
              ".ts", ".mpg", ".mpeg", ".flv", ".webm", ".divx"}

TMDB_BASE = "https://api.themoviedb.org/3"

# Noise tags to strip from filenames (case-insensitive)
_NOISE = re.compile(
    r"""
    \b(
        2160p|1080p|720p|480p|4k|uhd|hd|sd |
        web[-.]?dl|webrip|web|bluray|bdrip|brrip|
        dvdrip|dvdscr|hdtv|pdtv|ts|cam|scr|r5 |
        h264|h265|x264|x265|xvid|divx|hevc|avc |
        aac|ac3|dd5?\.1|dts|flac|mp3|truehd|atmos |
        amzn|nf|hmax|dsnp|atvp|pcok|        # streaming sources
        repack|proper|extended|theatrical|unrated|directors\.cut |
        hebrew|english|heb|eng|multi |
        yify|fgt|sparks|eztv|rarbg|ettv|
        fuzepack|fuzepacks
    )\b
    """,
    re.VERBOSE | re.IGNORECASE,
)

# ---------------------------------------------------------------------------

def load_secrets() -> dict:
    if not SECRETS_YML.exists():
        print(f"ERROR: {SECRETS_YML} not found.")
        sys.exit(1)
    with open(SECRETS_YML, encoding="utf-8") as f:
        return yaml.safe_load(f)


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


# ---------------------------------------------------------------------------
# Filename → title
# ---------------------------------------------------------------------------

def parse_filename(stem: str) -> tuple[str, str]:
    """
    Returns (clean_title, media_type) where media_type is 'tv' or 'movie'.
    """
    name = stem

    # Detect TV: SxxExx pattern
    is_tv = bool(re.search(r'[Ss]\d{1,2}[Ee]\d{1,2}', name))

    # Strip SxxExx and everything after
    name = re.sub(r'[\s._\-]+[Ss]\d{1,2}[Ee]\d{1,2}.*', '', name)

    # Strip year (4 digits, 1900–2099) and everything after
    name = re.sub(r'[\s._\-]+\b(19|20)\d{2}\b.*', '', name)

    # Strip noise tags and everything after the first one
    m = _NOISE.search(name)
    if m:
        name = name[:m.start()]

    # Normalise separators → spaces
    name = re.sub(r'[\s._\-]+', ' ', name).strip()

    # Drop trailing junk like " - " or " ["
    name = re.sub(r'[\s\-\[]+$', '', name).strip()

    return name, "tv" if is_tv else "movie"


def scan_video_files(root: Path) -> list[tuple[str, str]]:
    """
    Walk root recursively, parse each video filename.
    Returns list of (title, media_type), deduplicated.
    """
    seen:   set[str]             = set()
    titles: list[tuple[str, str]] = []

    print(f"Scanning {root} for video files…", flush=True)
    count = 0
    for p in root.rglob("*"):
        if p.suffix.lower() not in VIDEO_EXTS:
            continue
        count += 1
        title, mt = parse_filename(p.stem)
        if not title or len(title) < 2:
            continue
        key = title.lower()
        if key not in seen:
            seen.add(key)
            titles.append((title, mt))

    print(f"  {count} video files → {len(titles)} unique titles")
    return titles


# ---------------------------------------------------------------------------
# TMDB lookup
# ---------------------------------------------------------------------------

def lookup_title(title: str, media_type: str, api_key: str) -> dict | None:
    data = tmdb_get(f"/search/{media_type}", api_key,
                    {"query": title, "language": "en-US"})
    results = data.get("results", [])
    if results:
        return results[0]
    # If movie not found, try as TV (and vice versa)
    alt = "tv" if media_type == "movie" else "movie"
    data = tmdb_get(f"/search/{alt}", api_key,
                    {"query": title, "language": "en-US"})
    results = data.get("results", [])
    if results:
        return {**results[0], "_alt_type": alt}
    return None


def get_genres(tmdb_id: int, media_type: str, api_key: str) -> list[int]:
    data = tmdb_get(f"/{media_type}/{tmdb_id}", api_key, {"language": "en-US"})
    return [g["id"] for g in data.get("genres", [])]


# ---------------------------------------------------------------------------
# Main build logic
# ---------------------------------------------------------------------------

def build_profile(scan_root: Path, api_key: str) -> dict:
    titles = scan_video_files(scan_root)

    tv_genres:    Counter = Counter()
    movie_genres: Counter = Counter()
    wl_ids:       set[int] = set()
    found = 0
    not_found = 0

    print(f"\nLooking up {len(titles)} titles on TMDB…")
    for i, (title, mt) in enumerate(titles, 1):
        print(f"  [{i}/{len(titles)}] {title}…", end=" ", flush=True)

        result = lookup_title(title, mt, api_key)
        time.sleep(0.25)

        if not result:
            print("not found")
            not_found += 1
            continue

        actual_mt = result.get("_alt_type", mt)
        tmdb_id   = result["id"]
        wl_ids.add(tmdb_id)

        genres = get_genres(tmdb_id, actual_mt, api_key)
        time.sleep(0.25)
        print(f"ok  ({actual_mt}, genres: {genres})")

        if actual_mt == "tv":
            tv_genres.update(genres)
        else:
            movie_genres.update(genres)
        found += 1

    print(f"\n✓ {found} titles found, {not_found} not found on TMDB")
    print(f"  TV genres:    {dict(tv_genres.most_common())}")
    print(f"  Movie genres: {dict(movie_genres.most_common())}")

    return {
        "source":       "disk",
        "scan_root":    str(scan_root),
        "tv_genres":    list(tv_genres.keys()),
        "movie_genres": list(movie_genres.keys()),
        "wl_ids":       list(wl_ids),
        "built_at":     __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="D:\\",
                        help="Root folder to scan (default: D:\\)")
    args = parser.parse_args()

    scan_root = Path(args.path)
    if not scan_root.exists():
        print(f"ERROR: path does not exist: {scan_root}")
        sys.exit(1)

    secrets = load_secrets()
    api_key = secrets.get("tmdb_api_key", "")
    if not api_key:
        print("ERROR: tmdb_api_key not set in secrets.yml")
        sys.exit(1)

    profile = build_profile(scan_root, api_key)

    PROFILE_JSON.parent.mkdir(exist_ok=True)
    PROFILE_JSON.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print(f"\nProfile saved → {PROFILE_JSON.resolve()}")
    print("Run  python recommend.py --open  to get recommendations.")


if __name__ == "__main__":
    main()
