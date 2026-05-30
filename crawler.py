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
    # Keep flat lists (strings/numbers) on one line; nested lists stay block
    if all(isinstance(i, (str, int, float, bool)) for i in data):
        return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=False)


_InlineListDumper.add_representer(list, _inline_representer)


def _save_watchlist(data: dict):
    with open(WATCH_YML, "w", encoding="utf-8") as f:
        yaml.dump(data, f, Dumper=_InlineListDumper, default_flow_style=False,
                  allow_unicode=True, sort_keys=False)

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
    Returns a match dict with keys: category, entry, download_dir, torrent
    or None if no match.
    """
    seeders = torrent.get("seeders", 0)
    if seeders < min_seeders:
        return None

    name = _normalise(torrent.get("name", ""))

    for cat_name, cat in watchlist_data.get("categories", {}).items():
        default_kws  = [str(k).lower() for k in cat.get("default_keywords", [])]
        download_dir = cat.get("download_dir", "")
        for entry in cat.get("watchlist", []):
            entry = list(entry)  # may be stored as list
            title = _normalise(str(entry[0]))
            extra = [_normalise(str(k)) for k in entry[1:]]
            keywords = extra + default_kws

            if title not in name:
                continue
            if all(kw in name for kw in keywords):
                return {
                    "category": cat_name,
                    "entry": entry,
                    "download_dir": download_dir,
                    "torrent": torrent,
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
        entry_str = str(match["entry"])
        dl_log.info("[%s] %s  →  entry removed: %s", match["category"], name, entry_str)

        # Remove from watchlist
        watchlist_data["categories"][match["category"]]["watchlist"].remove(match["entry"])
        _save_watchlist(watchlist_data)
        app_log.info("Removed '%s' from watchlist.yml.", entry_str)

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
