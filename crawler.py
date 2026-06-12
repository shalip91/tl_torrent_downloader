#!/usr/bin/env python3
"""
Multi-Site Watchlist Downloader
---------------------------------
Polls configured torrent sites every 1-2 hours (randomised).
Matches torrents against watchlist.yml. On match: downloads the .torrent
file, removes the episode from watchlist.yml, appends to logs/downloaded.log.

Supported sites: torrentleech, fuzer
Each site has its own session cookies stored in state/cookies_{site}.pkl.

On first run (or expired session): prompts for cookies per site.
Never logs cookies.
"""

import logging
import pickle
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT        = Path(__file__).parent
STATE_DIR   = ROOT / "state"
LOGS_DIR    = ROOT / "logs"
CONFIG_YML  = ROOT / "config.yml"
WATCH_YML   = ROOT / "watchlist.yml"

STATE_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_YML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_watchlist() -> dict:
    with open(WATCH_YML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(cfg: dict):
    level = getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO)
    fmt   = "%(asctime)s [%(levelname)s] %(message)s"

    app_log = logging.getLogger("app")
    app_log.setLevel(level)
    app_log.propagate = False
    fh = logging.FileHandler(LOGS_DIR / "app.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(fmt))
    app_log.addHandler(fh)
    app_log.addHandler(ch)

    dl_log = logging.getLogger("downloaded")
    dl_log.setLevel(logging.INFO)
    dl_log.propagate = False
    dfh = logging.FileHandler(LOGS_DIR / "downloaded.log", encoding="utf-8")
    dfh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    dl_log.addHandler(dfh)

    return app_log, dl_log

# ---------------------------------------------------------------------------
# Anti-detection
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
        "Connection": "keep-alive",
    }

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _cookies_pkl(site_name: str) -> Path:
    return STATE_DIR / f"cookies_{site_name}.pkl"


def _make_session(cookies: dict, domain: str) -> requests.Session:
    sess = requests.Session()
    for name, value in cookies.items():
        sess.cookies.set(name, value, domain=domain, path="/")
    return sess


def _save_cookies(site_name: str, cookies: dict):
    with open(_cookies_pkl(site_name), "wb") as f:
        pickle.dump(cookies, f)


def _load_cookies(site_name: str) -> dict | None:
    p = _cookies_pkl(site_name)
    if not p.exists():
        return None
    with open(p, "rb") as f:
        return pickle.load(f)

# ---------------------------------------------------------------------------
# YAML inline-list dumper
# ---------------------------------------------------------------------------

class _InlineListDumper(yaml.Dumper):
    pass


def _inline_representer(dumper, data):
    if data and isinstance(data[0], (str, int, float, bool)):
        return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=False)


_InlineListDumper.add_representer(list, _inline_representer)


def _save_watchlist(data: dict):
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
        if not (line.lstrip().startswith("- [") and title in line):
            result.append(line)
            continue

        ep_str = str(ep_val)
        new_line = re.sub(r",\s*" + re.escape(ep_str) + r"(?=\s*[,\]])", "", line)
        if new_line == line:
            new_line = re.sub(r"\b" + re.escape(ep_str) + r"\s*,\s*", "", line)
        if new_line == line:
            new_line = re.sub(r"\b" + re.escape(ep_str) + r"\b", "", line)

        # Check specifically that the episode sublist (after SxxE,) became empty.
        # Using any \[\s*\] would falsely match a keyword-override [] at the end of the line.
        if re.search(r'[Ss]\d+[Ee],\s*\[\s*\]', new_line):
            removed_entry = True
            continue

        result.append(new_line)

    WATCH_YML.write_text("".join(result), encoding="utf-8")
    return removed_entry

# ---------------------------------------------------------------------------
# Matching (shared across all sites)
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    return re.sub(r"[\s._\-]+", " ", text).strip().lower()


def match_torrent(torrent: dict, watchlist_data: dict, min_seeders: int = 0) -> dict | None:
    """
    Supports three entry formats:
      - [title, episode]                    e.g. [Show, S02E10]
      - [title, [ep1, ep2, ...]]            e.g. [Show, [S03E01, S03E02]]
      - [title, season_prefix, [N, N, ...]] e.g. [Show, S03E, [1, 2, 3]]  (compact)
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

            # Compact format: [title, SxxE, [1, 2, ...]]
            # Optional 4th element: if a list  → overrides default_keywords for this entry
            #                       if scalars → extra keywords added to default_keywords
            # Examples:
            #   [Show, S01E, [1,2,3]]          -- uses default_keywords (e.g. 1080p)
            #   [Show, S01E, [1,2,3], [720p]]  -- requires 720p instead
            #   [Show, S01E, [1,2,3], []]      -- no resolution filter
            if (len(entry) >= 3
                    and isinstance(entry[1], str)
                    and re.match(r'^[Ss]\d+[Ee]$', entry[1])
                    and isinstance(entry[2], list)):
                season = str(entry[1])
                if len(entry) > 3 and isinstance(entry[3], list):
                    kws = [_normalise(str(k)) for k in entry[3]]  # override
                else:
                    kws = [_normalise(str(k)) for k in entry[3:]] + default_kws
                for ep_val in entry[2]:
                    ep_short = str(ep_val).zfill(2)
                    full_ep  = season + ep_short
                    ep_kws   = [_normalise(full_ep)] + kws
                    if all(kw in name for kw in ep_kws):
                        return {
                            "category":         cat_name,
                            "entry":            entry,
                            "matched_episode":  full_ep,
                            "matched_ep_short": ep_val,
                            "download_dir":     download_dir,
                            "torrent":          torrent,
                        }

            # Full list format: [title, [S03E01, S03E02, ...]]
            # Optional 3rd element: if a list → overrides default_keywords
            elif len(entry) > 1 and isinstance(entry[1], list):
                if len(entry) > 2 and isinstance(entry[2], list):
                    kws = [_normalise(str(k)) for k in entry[2]]  # override
                else:
                    kws = [_normalise(str(k)) for k in entry[2:]] + default_kws
                for episode in entry[1]:
                    ep_kws = [_normalise(str(episode))] + kws
                    if all(kw in name for kw in ep_kws):
                        return {
                            "category":         cat_name,
                            "entry":            entry,
                            "matched_episode":  str(episode),
                            "matched_ep_short": None,
                            "download_dir":     download_dir,
                            "torrent":          torrent,
                        }

            # Single episode or title-only: [title, episode] or [title]
            # Optional 3rd element: if a list → overrides default_keywords
            else:
                episodes = [str(entry[1])] if len(entry) > 1 else [None]
                if len(entry) > 2 and isinstance(entry[2], list):
                    kws = [_normalise(str(k)) for k in entry[2]]  # override
                else:
                    kws = [_normalise(str(k)) for k in entry[2:]] + default_kws
                for episode in episodes:
                    ep_kws = ([_normalise(episode)] if episode else []) + kws
                    if all(kw in name for kw in ep_kws):
                        return {
                            "category":         cat_name,
                            "entry":            entry,
                            "matched_episode":  episode,
                            "matched_ep_short": None,
                            "download_dir":     download_dir,
                            "torrent":          torrent,
                        }
    return None


def handle_match(match: dict, sess: requests.Session, download_fn, cfg: dict,
                 watchlist_data: dict, app_log, dl_log) -> bool:
    """Download and remove from watchlist. Returns True on success."""
    torrent  = match["torrent"]
    name     = torrent["name"]
    cat      = match["category"]

    app_log.info("MATCH [%s] '%s'", cat, name)

    ok = download_fn(sess, torrent, match["download_dir"], cfg, app_log)
    if not ok:
        return False

    dl_log.info("[%s] %s", cat, name)

    # Remove from watchlist
    cat_watchlist    = watchlist_data["categories"][cat]["watchlist"]
    original         = next(e for e in cat_watchlist if list(e)[0] == match["entry"][0])
    matched_ep       = match["matched_episode"]
    matched_ep_short = match["matched_ep_short"]
    title            = original[0]

    if matched_ep_short and isinstance(original[2], list):
        entry_removed = _surgical_remove_episode(title, matched_ep_short)
        if entry_removed:
            app_log.info("All episodes done - removed '%s' from watchlist.yml.", title)
        else:
            app_log.info("Removed %s from '%s' in watchlist.yml.", matched_ep, title)

    elif matched_ep and isinstance(original[1], list):
        original[1].remove(matched_ep)
        app_log.info("Removed %s from '%s' in watchlist.yml.", matched_ep, title)
        if not original[1]:
            cat_watchlist.remove(original)
            app_log.info("All episodes done - removed '%s' from watchlist.yml.", title)
        _save_watchlist(watchlist_data)

    else:
        cat_watchlist.remove(original)
        app_log.info("Removed '%s' from watchlist.yml.", match["entry"])
        _save_watchlist(watchlist_data)

    return True

# ===========================================================================
# TorrentLeech Adapter
# ===========================================================================

TL_COOKIE_NAMES = ["PHPSESSID", "tlpass", "tluid"]
TL_DOMAIN       = "www.torrentleech.org"


def tl_is_logged_in(sess: requests.Session, site_cfg: dict, cfg: dict, log) -> bool:
    try:
        r = sess.get(site_cfg["base_url"] + "/", headers=_browser_headers(),
                     timeout=cfg["http"]["timeout_rss"])
        return "logout" in r.text.lower()
    except Exception as e:
        log.warning("[TorrentLeech] Session check failed: %s", e)
        return False


def tl_fetch_torrents(sess: requests.Session, site_cfg: dict, cfg: dict, log) -> list[dict]:
    base = site_cfg["base_url"]
    tw   = site_cfg.get("time_window", "-3 day").replace(" ", "%20")
    by   = site_cfg.get("orderby", "completed")
    ord_ = site_cfg.get("order", "desc")
    url  = f"{base}/torrents/browse/list/added/{tw}/orderby/{by}/order/{ord_}"

    log.debug("[TorrentLeech] Fetching: %s", url)
    time.sleep(random.uniform(cfg["request_delay_min_seconds"], cfg["request_delay_max_seconds"]))

    try:
        r = sess.get(url, headers=_api_headers(), timeout=cfg["http"]["timeout_rss"])
        r.raise_for_status()
        torrents = r.json().get("torrentList", [])
        log.info("[TorrentLeech] Fetched %d torrents.", len(torrents))
        for t in torrents:
            log.debug("  [TL] %s", t.get("name", "?"))
        return torrents
    except Exception as e:
        log.error("[TorrentLeech] Fetch failed: %s", e)
        return []


def tl_download_torrent(sess: requests.Session, torrent: dict, dest_dir: str,
                        cfg: dict, log) -> bool:
    url = f"https://{TL_DOMAIN}/download/{torrent['fid']}/{torrent['filename']}"
    time.sleep(random.uniform(cfg["download_delay_min_seconds"], cfg["download_delay_max_seconds"]))
    try:
        r = sess.get(url, headers=_browser_headers(), timeout=cfg["http"]["timeout_download"])
        r.raise_for_status()
        if b"<!doctype" in r.content[:200].lower() or len(r.content) < 200:
            log.error("[TorrentLeech] Download returned HTML (session expired?).")
            return False
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / torrent["filename"]).write_bytes(r.content)
        log.info("[TorrentLeech] Downloaded → %s/%s", dest_dir, torrent["filename"])
        return True
    except Exception as e:
        log.error("[TorrentLeech] Download failed: %s", e)
        return False

# ===========================================================================
# Fuzer Adapter
# ===========================================================================

FUZER_COOKIE_NAMES = ["fzr2sessionhash", "fzr2userid", "fzr2password"]
FUZER_DOMAIN       = "www.fuzer.xyz"
_FUZER_TORRENT_RE  = re.compile(
    r'attachmentid=(\d+)&(?:amp;)?d=[^"]*"\s+title="[^\n"]*\n([^"]+)\.torrent"'
)


def fuzer_is_logged_in(sess: requests.Session, site_cfg: dict, cfg: dict, log) -> bool:
    try:
        r = sess.get(site_cfg["base_url"] + "/browse.php", headers=_browser_headers(),
                     timeout=cfg["http"]["timeout_rss"])
        # "התנתק" = logout link in Hebrew, present only when logged in
        return "התנתק" in r.content.decode("windows-1255", errors="replace")
    except Exception as e:
        log.warning("[Fuzer] Session check failed: %s", e)
        return False


def fuzer_fetch_torrents(sess: requests.Session, site_cfg: dict, cfg: dict, log) -> list[dict]:
    base = site_cfg["base_url"]
    url  = f"{base}/browse.php?order=uploaded&sort=desc"

    log.debug("[Fuzer] Fetching: %s", url)
    time.sleep(random.uniform(cfg["request_delay_min_seconds"], cfg["request_delay_max_seconds"]))

    try:
        r = sess.get(url, headers=_browser_headers(), timeout=cfg["http"]["timeout_rss"])
        r.raise_for_status()
        html = r.content.decode("windows-1255", errors="replace")
        matches = _FUZER_TORRENT_RE.findall(html)
        torrents = [{"fid": aid, "name": name, "filename": name + ".torrent", "seeders": 0}
                    for aid, name in matches]
        log.info("[Fuzer] Fetched %d torrents.", len(torrents))
        for t in torrents:
            log.debug("  [Fuzer] %s", t["name"])
        return torrents
    except Exception as e:
        log.error("[Fuzer] Fetch failed: %s", e)
        return []


def fuzer_download_torrent(sess: requests.Session, torrent: dict, dest_dir: str,
                           cfg: dict, log) -> bool:
    url = f"https://{FUZER_DOMAIN}/attachment.php?attachmentid={torrent['fid']}&d="
    time.sleep(random.uniform(cfg["download_delay_min_seconds"], cfg["download_delay_max_seconds"]))
    try:
        r = sess.get(url, headers=_browser_headers(), timeout=cfg["http"]["timeout_download"])
        r.raise_for_status()
        if b"<!doctype" in r.content[:200].lower() or len(r.content) < 200:
            log.error("[Fuzer] Download returned HTML (session expired?).")
            return False
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        filename = torrent["filename"]
        (dest / filename).write_bytes(r.content)
        log.info("[Fuzer] Downloaded → %s/%s", dest_dir, filename)
        return True
    except Exception as e:
        log.error("[Fuzer] Download failed: %s", e)
        return False

# ===========================================================================
# Site registry
# ===========================================================================

SITE_ADAPTERS = {
    "torrentleech": {
        "cookie_names":  TL_COOKIE_NAMES,
        "domain":        TL_DOMAIN,
        "is_logged_in":  tl_is_logged_in,
        "fetch_torrents": tl_fetch_torrents,
        "download":      tl_download_torrent,
        "min_seeders":   True,   # respect min_seeders from config
    },
    "fuzer": {
        "cookie_names":  FUZER_COOKIE_NAMES,
        "domain":        FUZER_DOMAIN,
        "is_logged_in":  fuzer_is_logged_in,
        "fetch_torrents": fuzer_fetch_torrents,
        "download":      fuzer_download_torrent,
        "min_seeders":   False,  # Fuzer doesn't expose seeders in HTML
    },
}

# ===========================================================================
# Session management (per site)
# ===========================================================================

def _prompt_cookies(site_name: str, cookie_names: list[str]) -> dict:
    print()
    print("=" * 60)
    print(f"Session cookies required for: {site_name}")
    print("Open Chrome → login to the site")
    print("F12 → Application → Cookies → site URL")
    print(f"Copy values for: {', '.join(cookie_names)}")
    print("=" * 60)
    cookies = {}
    for name in cookie_names:
        while True:
            val = input(f"  {name}: ").strip()
            if val:
                cookies[name] = val
                break
            print("  (value cannot be empty)")
    return cookies


def get_session(site_name: str, site_cfg: dict, cfg: dict, log) -> requests.Session:
    adapter = SITE_ADAPTERS[site_cfg["type"]]
    domain  = adapter["domain"]

    while True:
        cookies = _load_cookies(site_name)
        if cookies:
            sess = _make_session(cookies, domain)
            if adapter["is_logged_in"](sess, site_cfg, cfg, log):
                log.info("[%s] Loaded saved session.", site_name)
                return sess
            log.warning("[%s] Saved session expired or invalid.", site_name)

        cookies = _prompt_cookies(site_name, adapter["cookie_names"])
        sess    = _make_session(cookies, domain)
        if adapter["is_logged_in"](sess, site_cfg, cfg, log):
            _save_cookies(site_name, cookies)
            log.info("[%s] Session verified and saved.", site_name)
            return sess
        print(f"  Could not verify login for {site_name} — please check the cookie values.\n")

# ===========================================================================
# Per-site crawl cycle
# ===========================================================================

def run_once_site(site_name: str, site_cfg: dict, sess: requests.Session,
                  cfg: dict, watchlist_data: dict, app_log, dl_log) -> requests.Session:
    adapter = SITE_ADAPTERS[site_cfg["type"]]

    # Refresh session if needed
    if not adapter["is_logged_in"](sess, site_cfg, cfg, app_log):
        app_log.warning("[%s] Session expired — re-authenticating.", site_name)
        sess = get_session(site_name, site_cfg, cfg, app_log)

    torrents = adapter["fetch_torrents"](sess, site_cfg, cfg, app_log)
    if not torrents:
        return sess

    min_seeders = cfg.get("min_seeders", 1) if adapter["min_seeders"] else 0

    total_entries = sum(
        len(cat.get("watchlist", []))
        for cat in watchlist_data.get("categories", {}).values()
    )
    app_log.info("[%s] Checking %d torrents against %d watchlist entries.",
                 site_name, len(torrents), total_entries)

    download_fn = adapter["download"]
    for torrent in torrents:
        match = match_torrent(torrent, watchlist_data, min_seeders)
        if not match:
            continue
        handle_match(match, sess, lambda s, t, d, c, l: download_fn(s, t, d, c, l),
                     cfg, watchlist_data, app_log, dl_log)
        # Reload watchlist after each removal so subsequent matches see the updated state
        watchlist_data = load_watchlist()

    return sess


def run_once(sessions: dict, cfg: dict, app_log, dl_log) -> dict:
    watchlist_data = load_watchlist()

    for site_name, site_cfg in cfg.get("sites", {}).items():
        if not site_cfg.get("enabled", True):
            continue
        try:
            sessions[site_name] = run_once_site(
                site_name, site_cfg, sessions[site_name],
                cfg, watchlist_data, app_log, dl_log
            )
            # Reload watchlist between sites so second site sees updated state
            watchlist_data = load_watchlist()
        except Exception as e:
            app_log.exception("[%s] Unexpected error: %s", site_name, e)

    return sessions

# ===========================================================================
# Main
# ===========================================================================

def main():
    cfg = load_config()
    app_log, dl_log = _setup_logging(cfg)
    app_log.info("Multi-Site Watchlist Downloader starting.")

    sites = cfg.get("sites", {})
    if not sites:
        app_log.error("No sites configured in config.yml. Add a 'sites' section.")
        sys.exit(1)

    # Initialise one session per enabled site
    sessions = {}
    for site_name, site_cfg in sites.items():
        if not site_cfg.get("enabled", True):
            app_log.info("[%s] Disabled — skipping.", site_name)
            continue
        site_type = site_cfg.get("type", "")
        if site_type not in SITE_ADAPTERS:
            app_log.error("Unknown site type '%s' for site '%s'. Skipping.", site_type, site_name)
            continue
        sessions[site_name] = get_session(site_name, site_cfg, cfg, app_log)

    interval_min = cfg.get("interval_min_minutes", 60)
    interval_max = cfg.get("interval_max_minutes", 120)

    while True:
        try:
            cfg          = load_config()
            interval_min = cfg.get("interval_min_minutes", 60)
            interval_max = cfg.get("interval_max_minutes", 120)
            sessions     = run_once(sessions, cfg, app_log, dl_log)
        except KeyboardInterrupt:
            app_log.info("Stopped by user.")
            break
        except Exception as e:
            app_log.exception("Unexpected error: %s", e)

        wait = random.uniform(interval_min * 60, interval_max * 60)
        app_log.info("Next check in %.0f minutes.", wait / 60)
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            app_log.info("Stopped by user.")
            break


if __name__ == "__main__":
    main()
