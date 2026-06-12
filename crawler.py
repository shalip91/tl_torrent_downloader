#!/usr/bin/env python3
"""
TorrentLeech Watchlist Downloader
----------------------------------
Polls the TorrentLeech JSON browse API every 1-2 hours (randomised).
Matches torrents against watchlist.yml. On match: downloads the .torrent
file, removes the entry from watchlist.yml, and appends a line to
logs/downloaded.log.

First run (or expired session): prompts for PHPSESSID, tlpass, tluid
from your browser's DevTools → Application → Cookies.

Never logs cookies or passkey.
"""

import logging
import pickle
import random
import re
import sys
import time
from pathlib import Path

import requests
import yaml

# ---------------------------------------------------------------------------
# Paths (all relative to this file so the folder is portable)
# ---------------------------------------------------------------------------
ROOT       = Path(__file__).parent
STATE_DIR  = ROOT / "state"
LOGS_DIR   = ROOT / "logs"
CONFIG_YML = ROOT / "config.yml"
WATCH_YML  = ROOT / "watchlist.yml"
COOKIES_PKL = STATE_DIR / "session_cookies.pkl"

STATE_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_YML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_watchlist() -> dict:
    with open(WATCH_YML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

# ---------------------------------------------------------------------------
# Logging — two separate loggers
# ---------------------------------------------------------------------------

def _setup_logging(cfg: dict):
    level = getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO)
    fmt   = "%(asctime)s [%(levelname)s] %(message)s"

    # App logger
    app_log = logging.getLogger("app")
    app_log.setLevel(level)
    app_log.propagate = False
    fh = logging.FileHandler(LOGS_DIR / "app.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(fmt))
    app_log.addHandler(fh)
    app_log.addHandler(ch)

    # Downloaded logger (append-only, no console output)
    dl_log = logging.getLogger("downloaded")
    dl_log.setLevel(logging.INFO)
    dl_log.propagate = False
    dfh = logging.FileHandler(LOGS_DIR / "downloaded.log", encoding="utf-8")
    dfh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    dl_log.addHandler(dfh)

    return app_log, dl_log

# ---------------------------------------------------------------------------
# Anti-detection: rotating User-Agents + realistic headers
# ---------------------------------------------------------------------------

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

_ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.8,he;q=0.6",
    "en-US,en;q=0.9,fr;q=0.7",
]


def _browser_headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def _api_headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.torrentleech.org/torrents/browse/list/",
        "Connection": "keep-alive",
    }

# ---------------------------------------------------------------------------
# Session / cookie management
# ---------------------------------------------------------------------------

def _make_session(cookies: dict) -> requests.Session:
    sess = requests.Session()
    for name, value in cookies.items():
        sess.cookies.set(name, value, domain="www.torrentleech.org", path="/")
    return sess


def _is_logged_in(sess: requests.Session, cfg: dict, log) -> bool:
    try:
        r = sess.get(
            cfg["base_url"] + "/",
            headers=_browser_headers(),
            timeout=cfg["http"]["timeout_rss"],
        )
        return "logout" in r.text.lower()
    except Exception as e:
        log.warning("Session check failed: %s", e)
        return False


def _prompt_cookies(log) -> dict:
    """Ask the user to paste their three TorrentLeech auth cookies."""
    print()
    print("=" * 60)
    print("TorrentLeech session cookies required.")
    print("Open Chrome → torrentleech.org")
    print("F12 → Application → Cookies → https://www.torrentleech.org")
    print("Copy the values for: PHPSESSID, tlpass, tluid")
    print("=" * 60)
    cookies = {}
    for name in ("PHPSESSID", "tlpass", "tluid"):
        while True:
            val = input(f"  {name}: ").strip()
            if val:
                cookies[name] = val
                break
            print("  (value cannot be empty)")
    return cookies


def get_session(cfg: dict, log) -> requests.Session:
    """Return an authenticated session, prompting for cookies if needed."""
    while True:
        if COOKIES_PKL.exists():
            with open(COOKIES_PKL, "rb") as f:
                cookies = pickle.load(f)
            sess = _make_session(cookies)
            if _is_logged_in(sess, cfg, log):
                log.info("Loaded saved session.")
                return sess
            log.warning("Saved session expired or invalid.")

        # Prompt
        cookies = _prompt_cookies(log)
        sess = _make_session(cookies)
        if _is_logged_in(sess, cfg, log):
            with open(COOKIES_PKL, "wb") as f:
                pickle.dump(cookies, f)
            log.info("Session verified and saved.")
            return sess
        print("  Could not verify login — please check the cookie values and try again.\n")

# ---------------------------------------------------------------------------
# YAML inline-list dumper (preserves `- [item1, item2]` format)
# ---------------------------------------------------------------------------

class _InlineListDumper(yaml.Dumper):
    pass


def _inline_representer(dumper, data):
    # An entry list always starts with a string (title or keyword).
    # A container list (e.g. the watchlist itself) starts with a list.
    # Only render as flow-style when it looks like an entry.
    if data and isinstance(data[0], (str, int, float, bool)):
        return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=False)


_InlineListDumper.add_representer(list, _inline_representer)


def _save_watchlist(data: dict):
    """Full rewrite — used only when an entire entry is removed."""
    with open(WATCH_YML, "w", encoding="utf-8") as f:
        yaml.dump(data, f, Dumper=_InlineListDumper, default_flow_style=False,
                  allow_unicode=True, sort_keys=False, width=9999)


def _surgical_remove_episode(title: str, ep_val) -> bool:
    """
    Remove a single episode number from a compact-format line in watchlist.yml,
    preserving all whitespace and alignment.
    Returns True if the entry line itself was also removed (list became empty).
    """
    text  = WATCH_YML.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    result = []
    removed_entry = False

    for line in lines:
        # Only touch lines that contain this title inside a flow sequence
        if not (line.lstrip().startswith("- [") and title in line):
            result.append(line)
            continue

        ep_str = str(ep_val)
        # Remove ", N" (not the first element)
        new_line = re.sub(r",\s*" + re.escape(ep_str) + r"(?=\s*[,\]])", "", line)
        if new_line == line:
            # Remove "N, " or "N," (first element)
            new_line = re.sub(r"\b" + re.escape(ep_str) + r"\s*,\s*", "", line)
        if new_line == line:
            # Only element — "N"
            new_line = re.sub(r"\b" + re.escape(ep_str) + r"\b", "", line)

        # If the inner list is now empty, drop the whole line
        if re.search(r"\[\s*\]", new_line):
            removed_entry = True
            continue

        result.append(new_line)

    WATCH_YML.write_text("".join(result), encoding="utf-8")
    return removed_entry

# ---------------------------------------------------------------------------
# Torrent API
# ---------------------------------------------------------------------------

def fetch_torrents(sess: requests.Session, cfg: dict, log) -> list[dict]:
    """Fetch recent torrents from the TorrentLeech JSON browse API."""
    base = cfg["base_url"]
    tw   = cfg.get("time_window", "-1 day")
    by   = cfg.get("orderby", "added")
    ord_ = cfg.get("order", "desc")

    tw_encoded = tw.replace(" ", "%20")
    url = f"{base}/torrents/browse/list/added/{tw_encoded}/orderby/{by}/order/{ord_}"
    log.debug("Fetching: %s", url)

    delay_min = cfg["request_delay_min_seconds"]
    delay_max = cfg["request_delay_max_seconds"]
    time.sleep(random.uniform(delay_min, delay_max))

    try:
        r = sess.get(url, headers=_api_headers(), timeout=cfg["http"]["timeout_rss"])
        r.raise_for_status()
        torrents = r.json().get("torrentList", [])
        log.info("Fetched %d torrents from API.", len(torrents))
        for t in torrents:
            log.debug("  [torrent] %s (seeders=%s)", t.get("name", "?"), t.get("seeders", "?"))
        return torrents
    except Exception as e:
        log.error("Failed to fetch torrents: %s", e)
        return []


def download_torrent(sess: requests.Session, torrent: dict, dest_dir: str,
                     cfg: dict, log) -> bool:
    """Download a .torrent file. Returns True on success."""
    fid      = str(torrent["fid"])
    filename = torrent["filename"]
    base     = cfg["base_url"]
    url      = f"{base}/download/{fid}/{filename}"

    delay_min = cfg["download_delay_min_seconds"]
    delay_max = cfg["download_delay_max_seconds"]
    time.sleep(random.uniform(delay_min, delay_max))

    try:
        r = sess.get(url, headers=_browser_headers(), timeout=cfg["http"]["timeout_download"])
        r.raise_for_status()
        if b"<!DOCTYPE" in r.content[:100].lower() or len(r.content) < 200:
            log.error("Download returned HTML instead of .torrent (likely session expired).")
            return False
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / filename
        out_path.write_bytes(r.content)
        log.info("Downloaded → %s", out_path)
        return True
    except Exception as e:
        log.error("Download failed for '%s': %s", filename, e)
        return False

# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    return re.sub(r"[\s._\-]+", " ", text).strip().lower()


def match_torrent(torrent: dict, watchlist_data: dict, min_seeders: int) -> dict | None:
    """
    Supports three entry formats:
      - [title, episode]                    e.g. [Show, S02E10]
      - [title, [ep1, ep2, ...]]            e.g. [Show, [S03E01, S03E02]]
      - [title, season, [E01, E02, ...]]    e.g. [Show, S03, [E01, E02, E03]]  (compact)

    Returns a match dict with keys: category, entry, matched_episode,
    matched_ep_short (for compact format), download_dir, torrent.
    """
    seeders = torrent.get("seeders", 0)
    if seeders < min_seeders:
        return None

    name = _normalise(torrent.get("name", ""))

    for cat_name, cat in watchlist_data.get("categories", {}).items():
        default_kws  = [str(k).lower() for k in cat.get("default_keywords", [])]
        download_dir = cat.get("download_dir", "")

        for entry in cat.get("watchlist", []):
            entry = list(entry)
            title = _normalise(str(entry[0]))

            if title not in name:
                continue

            # --- Compact format: [title, S03, [E01, E02, ...]] ---
            if (len(entry) >= 3
                    and isinstance(entry[1], str)
                    and re.match(r'^[Ss]\d+[Ee]$', entry[1])
                    and isinstance(entry[2], list)):
                season    = str(entry[1])           # e.g. "S03"
                extra_kws = [_normalise(str(k)) for k in entry[3:]]
                for ep_val in entry[2]:
                    ep_short = str(ep_val).zfill(2)  # zero-pad for matching only
                    full_ep  = season + ep_short      # "S03E01"
                    ep_kws   = [_normalise(full_ep)] + extra_kws + default_kws
                    if all(kw in name for kw in ep_kws):
                        return {
                            "category":         cat_name,
                            "entry":            entry,
                            "matched_episode":  full_ep,
                            "matched_ep_short": ep_val,  # original value (int) for removal
                            "download_dir":     download_dir,
                            "torrent":          torrent,
                        }

            # --- Full episode list: [title, [S03E01, S03E02, ...]] ---
            elif len(entry) > 1 and isinstance(entry[1], list):
                episodes  = [str(e) for e in entry[1]]
                extra_kws = [_normalise(str(k)) for k in entry[2:]]
                for episode in episodes:
                    ep_kws = [_normalise(episode)] + extra_kws + default_kws
                    if all(kw in name for kw in ep_kws):
                        return {
                            "category":        cat_name,
                            "entry":           entry,
                            "matched_episode": episode,
                            "matched_ep_short": None,
                            "download_dir":    download_dir,
                            "torrent":         torrent,
                        }

            # --- Single episode or title-only: [title, episode] or [title] ---
            else:
                episodes  = [str(entry[1])] if len(entry) > 1 else [None]
                extra_kws = [_normalise(str(k)) for k in entry[2:]]
                for episode in episodes:
                    ep_kws = ([_normalise(episode)] if episode else []) + extra_kws + default_kws
                    if all(kw in name for kw in ep_kws):
                        return {
                            "category":        cat_name,
                            "entry":           entry,
                            "matched_episode": episode,
                            "matched_ep_short": None,
                            "download_dir":    download_dir,
                            "torrent":         torrent,
                        }
    return None

# ---------------------------------------------------------------------------
# One crawl cycle
# ---------------------------------------------------------------------------

def run_once(sess: requests.Session, cfg: dict, app_log, dl_log) -> requests.Session:
    # Refresh session if needed
    if not _is_logged_in(sess, cfg, app_log):
        app_log.warning("Session expired mid-run — re-authenticating.")
        sess = get_session(cfg, app_log)

    torrents = fetch_torrents(sess, cfg, app_log)
    if not torrents:
        return sess

    min_seeders = cfg.get("min_seeders", 1)

    # Reload watchlist every cycle so live edits take effect
    watchlist_data = load_watchlist()
    total_entries = sum(
        len(cat.get("watchlist", []))
        for cat in watchlist_data.get("categories", {}).values()
    )
    app_log.info("Checking %d torrents against %d watchlist entries.", len(torrents), total_entries)

    for torrent in torrents:
        match = match_torrent(torrent, watchlist_data, min_seeders)
        if not match:
            continue

        name = torrent["name"]
        app_log.info("MATCH [%s] '%s'", match["category"], name)

        ok = download_torrent(sess, torrent, match["download_dir"], cfg, app_log)
        if not ok:
            continue

        # Log to downloaded.log
        matched_ep = match["matched_episode"]
        dl_log.info("[%s] %s", match["category"], name)

        # Remove from watchlist
        cat_watchlist = watchlist_data["categories"][match["category"]]["watchlist"]
        # Find the original entry object (match["entry"] is a copy)
        original  = next(e for e in cat_watchlist if list(e)[0] == match["entry"][0])
        matched_ep       = match["matched_episode"]
        matched_ep_short = match["matched_ep_short"]
        title            = original[0]

        if matched_ep_short and isinstance(original[2], list):
            # Compact format [title, SxxE, [1, 2, ...]]: surgical text edit
            entry_removed = _surgical_remove_episode(title, matched_ep_short)
            if entry_removed:
                app_log.info("All episodes done — removed '%s' from watchlist.yml.", title)
            else:
                app_log.info("Removed %s from '%s' in watchlist.yml.", matched_ep, title)

        elif matched_ep and isinstance(original[1], list):
            # Full list format [title, [S03E01, ...]]: remove full episode code
            original[1].remove(matched_ep)
            app_log.info("Removed %s from '%s' in watchlist.yml.", matched_ep, title)
            if not original[1]:
                cat_watchlist.remove(original)
                app_log.info("All episodes done — removed '%s' from watchlist.yml.", title)
            _save_watchlist(watchlist_data)

        else:
            # Single episode or title-only: remove whole entry
            cat_watchlist.remove(original)
            app_log.info("Removed '%s' from watchlist.yml.", match["entry"])
            _save_watchlist(watchlist_data)

    return sess

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    cfg     = load_config()
    app_log, dl_log = _setup_logging(cfg)

    app_log.info("TL Watchlist Downloader starting.")
    sess = get_session(cfg, app_log)

    interval_min = cfg.get("interval_min_minutes", 60)
    interval_max = cfg.get("interval_max_minutes", 120)

    while True:
        try:
            cfg = load_config()  # reload each cycle so edits take effect
            interval_min = cfg.get("interval_min_minutes", 60)
            interval_max = cfg.get("interval_max_minutes", 120)
            sess = run_once(sess, cfg, app_log, dl_log)
        except KeyboardInterrupt:
            app_log.info("Stopped by user.")
            break
        except Exception as e:
            app_log.exception("Unexpected error in run_once: %s", e)

        wait = random.uniform(interval_min * 60, interval_max * 60)
        app_log.info("Next check in %.0f minutes.", wait / 60)
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            app_log.info("Stopped by user.")
            break


if __name__ == "__main__":
    main()
