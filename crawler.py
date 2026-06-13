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

def _advance_next_episode(show_name: str, matched_ep: str) -> str:
    """
    Surgically update next_episode in watchlist.yml to matched_ep + 1.
    Finds the line by show name and replaces whatever next_episode value is stored,
    so it works correctly even when next_episode was advanced by a prior download
    in the same run.
    Preserves all formatting and alignment.
    Returns the new episode string.
    """
    m = re.match(r'^(S\d+E)(\d+)$', matched_ep, re.IGNORECASE)
    if not m:
        return matched_ep

    prefix = m.group(1).upper()
    width  = len(m.group(2))
    new_ep = f"{prefix}{str(int(m.group(2)) + 1).zfill(width)}"

    lines  = WATCH_YML.read_text(encoding="utf-8").splitlines(keepends=True)
    result = []
    for line in lines:
        if f'name: "{show_name}"' in line and 'next_episode:' in line:
            line = re.sub(r'next_episode:\s*"[^"]*"', f'next_episode: "{new_ep}"', line)
        result.append(line)
    WATCH_YML.write_text("".join(result), encoding="utf-8")
    return new_ep

# ---------------------------------------------------------------------------
# Matching (shared across all sites)
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    return re.sub(r"[\s._\-]+", " ", text).strip().lower()


def _parse_episode(ep_str: str) -> tuple[int, int] | None:
    """'S03E01' → (3, 1). Returns None if not parseable."""
    m = re.search(r'[Ss](\d+)[Ee](\d+)', ep_str)
    return (int(m.group(1)), int(m.group(2))) if m else None


def match_torrent(torrent: dict, watchlist_data: dict, min_seeders: int = 0) -> dict | None:
    """
    Watchlist entry format:
      TV:    {name: "Show Title", next_episode: "S03E01"}
             {name: "Show Title", next_episode: "S03E01", res: [720, 1080]}
             res: []  → no resolution filter
             res omitted → use category default_res
      Movie: {name: "Movie Title"}
             {name: "Movie Title", res: [2160]}

    Episode matching: torrent episode >= next_episode (season-aware).
    e.g. next_episode=S02E11, torrent=S03E01 → match (season 3 > season 2).
    The matched episode (S03E01) is returned so next_episode advances to S03E02.
    """
    seeders = torrent.get("seeders", 0)
    if seeders < min_seeders:
        return None

    torrent_name     = _normalise(torrent.get("name", ""))
    torrent_name_raw = torrent.get("name", "")

    for cat_name, cat in watchlist_data.get("categories", {}).items():
        default_res  = cat.get("default_res", [])
        download_dir = cat.get("download_dir", "")

        for entry in cat.get("watchlist", []):
            if not isinstance(entry, dict):
                continue

            show    = _normalise(str(entry.get("name", "")))
            next_ep = str(entry.get("next_episode", ""))

            if not show or show not in torrent_name:
                continue

            # Episode check (TV only — entries without next_episode are movies)
            actual_ep = ""
            if next_ep:
                next_parsed = _parse_episode(next_ep)
                if not next_parsed:
                    continue

                # Find SxxExx in the torrent filename
                em = re.search(r'[Ss](\d+)[Ee](\d+)', torrent_name_raw)
                if not em:
                    continue

                t_season, t_ep = int(em.group(1)), int(em.group(2))

                # Match only if torrent episode >= next_episode
                if (t_season, t_ep) < next_parsed:
                    continue

                # Build matched episode string using same zero-padding as next_ep
                s_width  = len(re.search(r'[Ss](\d+)', next_ep).group(1))
                e_width  = len(re.search(r'[Ee](\d+)', next_ep).group(1))
                actual_ep = f"S{str(t_season).zfill(s_width)}E{str(t_ep).zfill(e_width)}"

            # Resolution check
            res_list = entry.get("res", None)   # None → use default_res
            if res_list is None:
                res_list = default_res
            res_kws = [_normalise(f"{r}p") for r in res_list]
            if res_kws and not any(kw in torrent_name for kw in res_kws):
                continue

            return {
                "category":        cat_name,
                "entry":           entry,
                "matched_episode": actual_ep,   # the episode actually found, e.g. S03E01
                "download_dir":    download_dir,
                "torrent":         torrent,
            }

    return None


def handle_match(match: dict, sess: requests.Session, download_fn, cfg: dict,
                 watchlist_data: dict, app_log, dl_log) -> bool:
    """Download torrent and advance next_episode in watchlist.yml. Returns True on success."""
    torrent = match["torrent"]
    name    = torrent["name"]
    cat     = match["category"]
    entry   = match["entry"]

    app_log.info("MATCH [%s] '%s'", cat, name)

    ok = download_fn(sess, torrent, match["download_dir"], cfg, app_log)
    if not ok:
        return False

    dl_log.info("[%s] %s", cat, name)

    matched_ep = match.get("matched_episode", "")
    show_name  = entry.get("name", "")

    if matched_ep:
        new_ep = _advance_next_episode(show_name, matched_ep)
        app_log.info("'%s' → next episode advanced to %s", show_name, new_ep)

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

    # Collect all matches first, then sort by episode (low → high) before downloading
    download_fn = adapter["download"]
    matches = []
    for torrent in torrents:
        match = match_torrent(torrent, watchlist_data, min_seeders)
        if match:
            matches.append(match)

    def _ep_sort_key(m):
        parsed = _parse_episode(m.get("matched_episode", ""))
        return parsed if parsed else (0, 0)

    matches.sort(key=_ep_sort_key)

    for match in matches:
        handle_match(match, sess, lambda s, t, d, c, l: download_fn(s, t, d, c, l),
                     cfg, watchlist_data, app_log, dl_log)
        # Reload watchlist after each download so next_episode is fresh for the next iteration
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
