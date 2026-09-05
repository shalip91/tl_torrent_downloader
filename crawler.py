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
from datetime import datetime, timedelta
from pathlib import Path

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

# Pick ONE User-Agent for the whole process run and reuse it on every request.
# The old code rolled a fresh random UA per request, which means a single
# logged-in session presented a different browser identity on each call. That
# looks inconsistent to Cloudflare/bot-management and is the most likely reason
# the browse/list XHR endpoint started returning an empty body (a 200 with no
# JSON -> "Expecting value: line 1 column 1") while the cookies still passed the
# homepage login check. Rotating per *process* still varies across restarts
# without breaking a live session.
_SESSION_UA = random.choice(_USER_AGENTS)


def _browser_headers() -> dict:
    return {
        "User-Agent": _SESSION_UA,
        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def _api_headers() -> dict:
    return {
        "User-Agent": _SESSION_UA,
        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Connection": "keep-alive",
    }

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

class SessionCheckError(Exception):
    """Raised when login status can't be verified due to a transient/network
    problem (DNS, timeout, connection refused, etc). This is NOT the same as
    being logged out -- callers should retry later, not treat it as an expired
    session or prompt for new cookies."""


class NotLoggedInError(Exception):
    """Raised by a fetch when the site answered, but not with the authenticated
    payload -- i.e. the session really does look logged out. Only raised after
    the transient possibilities (network errors, Cloudflare challenge pages)
    have been retried and ruled out."""


def _stdin_interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


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
# "Newest torrent already seen" watermark (per site)
# ---------------------------------------------------------------------------
# Lets a cycle fetch only what appeared since the previous cycle instead of
# re-scanning a whole time window. TorrentLeech adds ~14 torrents/hour and a
# page holds 35, so a normal 60-120 minute cycle is satisfied by ONE request.

TS_FMT = "%Y-%m-%d %H:%M:%S"

# Re-examine a few minutes either side of the watermark in case the site
# inserts slightly out of order. Re-seeing a torrent is harmless: a downloaded
# movie is already off the watchlist and a downloaded episode has advanced
# next_episode, so neither matches a second time.
WATERMARK_OVERLAP = timedelta(minutes=15)


def _watermark_path(site_name: str) -> Path:
    return STATE_DIR / f"last_seen_{site_name}.txt"


def _load_watermark(site_name: str) -> datetime | None:
    p = _watermark_path(site_name)
    if not p.exists():
        return None
    try:
        return datetime.strptime(p.read_text(encoding="utf-8").strip(), TS_FMT)
    except (ValueError, OSError):
        return None


def _save_watermark(site_name: str, ts: datetime):
    _watermark_path(site_name).write_text(ts.strftime(TS_FMT), encoding="utf-8")


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.strptime(str(value).strip(), TS_FMT)
    except (ValueError, TypeError):
        return None

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


def _remove_watchlist_entry(show_name: str) -> bool:
    """
    Surgically delete the watchlist line for `show_name` from watchlist.yml.
    Used for entries with no next_episode (i.e. movies) once they've been
    downloaded: there's no "next episode" to advance to, so the entry is
    simply done and must be removed -- otherwise it keeps matching (and
    re-downloading) on every future crawl cycle.
    Returns True if a line was found and removed.
    """
    lines  = WATCH_YML.read_text(encoding="utf-8").splitlines(keepends=True)
    needle = f'name: "{show_name}"'
    result = [line for line in lines if needle not in line]
    removed = len(result) != len(lines)
    if removed:
        WATCH_YML.write_text("".join(result), encoding="utf-8")
    return removed

# ---------------------------------------------------------------------------
# Matching (shared across all sites)
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    return re.sub(r"[\s._\-]+", " ", text).strip().lower()



def _parse_episode(ep_str: str) -> tuple[int, int] | None:
    """'S03E01' → (3, 1). Returns None if not parseable."""
    m = re.search(r'[Ss](\d+)[Ee](\d+)', ep_str)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _coerce_size(val) -> int | None:
    """Best-effort convert a torrent size to bytes. Accepts an int/float byte
    count, a plain digit string, or a human string like '3.0 GB' / '769.8 MiB'.
    Returns None if it can't be parsed."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+", s):
        return int(s)
    m = re.match(r"([\d.]+)\s*([KMGTP]?)i?B\b", s, re.IGNORECASE)
    if m:
        mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
                "T": 1024 ** 4, "P": 1024 ** 5}[m.group(2).upper()]
        try:
            return int(float(m.group(1)) * mult)
        except ValueError:
            return None
    return None


def _torrent_size(torrent: dict) -> int | None:
    """Size in bytes for a torrent dict, trying the field names different sites
    use. Returns None if unknown (e.g. Fuzer, which doesn't expose size)."""
    for key in ("size", "filesize", "fileSize", "sizeBytes", "size_bytes"):
        if key in torrent and torrent[key] not in (None, ""):
            v = _coerce_size(torrent[key])
            if v is not None:
                return v
    return None


def _human_size(n: int | None) -> str:
    if n is None:
        return "size?"
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024:
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} PiB"


def _select_preferred(matches: list[dict], prefer: str, site_name: str, log) -> list[dict]:
    """When several torrents satisfy the SAME target (a show's episode, or a
    movie), keep only one:
        prefer='smallest' -> smallest file (default)
        prefer='largest'  -> largest file
    A torrent whose size is unknown is only kept if nothing else in its group
    has a known size. Original match order is otherwise preserved."""
    groups: dict = {}
    order:  list = []
    for m in matches:
        key = (m["category"],
               _normalise(str(m["entry"].get("name", ""))),
               m.get("matched_episode", ""))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(m)

    largest = str(prefer).lower() == "largest"
    chosen  = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            chosen.append(group[0])
            continue

        def _rank(m):
            sz = _torrent_size(m["torrent"])
            if sz is None:
                return (1, 0)                        # unknown size -> always last
            return (0, -sz if largest else sz)

        ranked = sorted(group, key=_rank)
        pick   = ranked[0]
        log.info("[%s] %d releases match '%s' %s — keeping %s [%s] (prefer=%s); "
                 "skipping: %s",
                 site_name, len(group), pick["entry"].get("name", "?"),
                 pick.get("matched_episode", "") or "(movie)",
                 pick["torrent"].get("name", "?"),
                 _human_size(_torrent_size(pick["torrent"])), prefer,
                 ", ".join(f'{g["torrent"].get("name", "?")} '
                           f'[{_human_size(_torrent_size(g["torrent"]))}]'
                           for g in ranked[1:]))
        chosen.append(pick)
    return chosen


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
                ns, ne = next_parsed

                # Only accept a sensible continuation of THIS show — not an
                # arbitrary higher SxxExx, which can belong to a DIFFERENT show
                # that happens to share the name (e.g. the US "Married at First
                # Sight" at S20 vs the one tracked here at S08). Allowed:
                #   • same season, at or after the awaited episode (S08E35 → S08E36)
                #   • the next season's premiere                   (S01E10 → S02E01)
                # Rejected: landing mid next-season (S01E10 → S02E03) or jumping
                # multiple seasons (S01E10 → S11E01).
                same_season   = (t_season == ns and t_ep >= ne)
                next_premiere = (t_season == ns + 1 and t_ep == 1)
                if not (same_season or next_premiere):
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
    elif "next_episode" not in entry:
        # Movie (or any entry with no episode tracking) — fully satisfied now.
        # Remove it so it isn't matched (and re-downloaded) again next cycle.
        if _remove_watchlist_entry(show_name):
            app_log.info("'%s' → downloaded, removed from watchlist.", show_name)
        else:
            app_log.warning("'%s' → downloaded, but couldn't find its line in "
                             "watchlist.yml to remove.", show_name)

    return True

# ===========================================================================
# TorrentLeech Adapter
# ===========================================================================

TL_COOKIE_NAMES = ["PHPSESSID", "tlpass", "tluid"]
TL_DOMAIN       = "www.torrentleech.org"


def tl_is_logged_in(sess: requests.Session, site_cfg: dict, cfg: dict, log) -> bool:
    # Validate against the actual authenticated browse/list endpoint instead of
    # grepping the homepage for the word "logout". The old homepage check was
    # unreliable and flapped between True/False: TorrentLeech serves the HTML
    # login page (HTTP 200) to a dead session on the API endpoint, yet the
    # homepage could still contain "logout" somewhere — so the check reported
    # "logged in" for a session that couldn't actually fetch anything. That both
    # masked the outage and stopped the crawler from ever prompting for fresh
    # cookies. Here: a live session returns JSON; a dead one returns the login
    # page (text/html), which fails json() → False → triggers re-auth.
    base      = site_cfg["base_url"]
    check_url = f"{base}/torrents/browse/list/added/-1%20day/orderby/added/order/desc"
    # TorrentLeech's endpoint has been observed answering inconsistently for the
    # same session (JSON one moment, the HTML login page the next — Cloudflare /
    # load-balancer behaviour). Retry a few times: a genuinely live session will
    # return JSON on at least one attempt; only conclude "logged out" if EVERY
    # attempt returns non-JSON. A healthy session returns JSON on the first try
    # and incurs no extra requests.
    for attempt in range(3):
        try:
            r = sess.get(check_url, headers=_api_headers(), timeout=cfg["http"]["timeout_rss"])
        except requests.exceptions.RequestException as e:
            # Network/DNS/timeout issue -- NOT the same as being logged out.
            log.warning("[TorrentLeech] Session check failed (network issue): %s", e)
            raise SessionCheckError(str(e)) from e
        try:
            r.json()
            return True
        except ValueError:
            if attempt < 2:
                time.sleep(2)
    return False


# Backoff between retries of a single request, in seconds. Deliberately long:
# a fast retry loop is exactly what looks like abuse to rate limiting, and a
# Cloudflare challenge needs real time to clear. Only ever paid on failure —
# a healthy cycle makes one request and never sleeps here.
TL_RETRY_BACKOFF = (20, 60)


def _tl_get_json(sess: requests.Session, url: str, cfg: dict, log) -> dict:
    """GET a TorrentLeech browse/list URL and return the decoded JSON payload.

    Raises SessionCheckError if it can't get an answer at all (network error,
    reset, timeout) and NotLoggedInError if the site answered with something
    other than JSON on every attempt.

    Both possibilities are retried with long backoff first. A single non-JSON
    response is NOT proof of a dead session: TorrentLeech serves a Cloudflare
    challenge page (HTTP 200, text/html) under load, which previously tripped a
    false "session expired" and cost a whole cycle.
    """
    attempts  = len(TL_RETRY_BACKOFF) + 1
    last_err  = None
    for attempt in range(attempts):
        time.sleep(random.uniform(cfg["request_delay_min_seconds"],
                                  cfg["request_delay_max_seconds"]))
        try:
            r = sess.get(url, headers=_api_headers(), timeout=cfg["http"]["timeout_rss"])
            r.raise_for_status()
        except Exception as e:
            last_err = SessionCheckError(str(e))
            log.warning("[TorrentLeech] Request failed (attempt %d/%d): %s",
                        attempt + 1, attempts, e)
        else:
            try:
                return r.json()
            except ValueError:
                body = (r.text or "").strip()
                last_err = NotLoggedInError(
                    f"non-JSON body (HTTP {r.status_code}, "
                    f"{r.headers.get('Content-Type', '?')})")
                log.warning(
                    "[TorrentLeech] Non-JSON response (attempt %d/%d): HTTP %s, "
                    "%d bytes, content-type=%s | First 200 chars: %r",
                    attempt + 1, attempts, r.status_code, len(r.content),
                    r.headers.get("Content-Type", "?"), body[:200])

        if attempt < len(TL_RETRY_BACKOFF):
            wait = TL_RETRY_BACKOFF[attempt]
            log.info("[TorrentLeech] Backing off %ds before retry.", wait)
            time.sleep(wait)

    log.error("[TorrentLeech] Giving up after %d attempts. URL: %s", attempts, url)
    raise last_err


def tl_fetch_torrents(site_name: str, sess: requests.Session, site_cfg: dict,
                      cfg: dict, log) -> list[dict]:
    """Fetch only what TorrentLeech has added since the previous cycle.

    Pages the newest-first browse list and stops at the first torrent already
    seen last run (the watermark in state/last_seen_*.txt). TorrentLeech adds
    ~14 torrents/hour and a page holds 35, so a normal 60-120 minute cycle is
    satisfied by ONE request.

    This replaces two earlier approaches, both of which lost coverage:

      * Scraping `added/-N day` ordered by "completed" and keeping 5 pages.
        A 3-day window holds ~1700 torrents, so that saw ~10% of it — all hot
        TV. A modestly-seeded movie (Night Nurse 2160p, 134 completions) was
        inside the window but far below the cut, so match_torrent never saw it.

      * One search request per watchlist title. Complete, but ~35 requests a
        cycle; TorrentLeech started answering with resets, timeouts and
        Cloudflare challenge pages, and a failed search silently dropped that
        title for the whole cycle.

    Incremental paging is lighter than both (1-2 requests vs 5 or 35) AND has
    complete coverage: every new torrent is checked against every watchlist
    entry, with no sort order to hide behind and no per-title call to fail.

    time_window is now only a backstop bounding how far back to look after
    downtime; the watermark ends normal cycles long before it.
    """
    base = site_cfg["base_url"]
    tw   = site_cfg.get("time_window", "-3 day").replace(" ", "%20")
    ord_ = site_cfg.get("order", "desc")
    # Safety cap on paging, only reached after long downtime (or on first run).
    max_pages = int(site_cfg.get("max_pages", 5))

    watermark = _load_watermark(site_name)
    cutoff    = (watermark - WATERMARK_OVERLAP) if watermark else None
    if cutoff:
        log.info("[TorrentLeech] Fetching torrents added since %s.",
                 cutoff.strftime(TS_FMT))
    else:
        log.info("[TorrentLeech] No watermark yet — scanning window %s (first run).",
                 site_cfg.get("time_window", "-3 day"))

    new_torrents: list[dict] = []
    seen_fids   = set()
    newest_ts   = None
    reached_old = False
    exhausted   = False
    pages_done  = 0

    for page in range(1, max_pages + 1):
        url = (f"{base}/torrents/browse/list/added/{tw}"
               f"/orderby/added/order/{ord_}/page/{page}")
        log.debug("[TorrentLeech] Fetching: %s", url)
        payload = _tl_get_json(sess, url, cfg, log)   # raises on real failure

        page_list  = payload.get("torrentList", []) or []
        pages_done = page
        if not page_list:
            exhausted = True
            break

        for t in page_list:
            ts = _parse_ts(t.get("addedTimestamp"))
            if ts and (newest_ts is None or ts > newest_ts):
                newest_ts = ts
            # List is newest-first, so the first already-seen torrent means
            # everything below it was handled on an earlier cycle.
            if cutoff and ts and ts <= cutoff:
                reached_old = True
                break
            fid = t.get("fid")
            if fid not in seen_fids:
                seen_fids.add(fid)
                new_torrents.append(t)

        if reached_old:
            break

        num_found = payload.get("numFound") or 0
        if num_found and page * len(page_list) >= num_found:
            exhausted = True
            break

    if not (reached_old or exhausted):
        log.warning(
            "[TorrentLeech] Hit the %d-page cap without catching up to the last "
            "cycle — torrents older than what was fetched may have been missed. "
            "Raise max_pages, or shorten the interval between checks.", max_pages)

    # Only advance the watermark on a successful fetch (failures raise above),
    # so a bad cycle can't skip torrents by moving the marker forward.
    if newest_ts:
        _save_watermark(site_name, newest_ts)

    log.info("[TorrentLeech] Fetched %d new torrent(s) in %d request(s).",
             len(new_torrents), pages_done)
    for t in new_torrents:
        log.debug("  [TL] %s | %s", t.get("addedTimestamp"), t.get("name", "?"))
    return new_torrents


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
    except requests.exceptions.RequestException as e:
        # Network/DNS/timeout issue -- NOT the same as being logged out.
        log.warning("[Fuzer] Session check failed (network issue): %s", e)
        raise SessionCheckError(str(e)) from e


def fuzer_fetch_torrents(site_name: str, sess: requests.Session, site_cfg: dict,
                         cfg: dict, log) -> list[dict]:
    # site_name is unused here — Fuzer's browse page has no date filter to page
    # against, so it's scraped whole (one page, ~50 rows) every cycle. Accepted
    # so every adapter shares one signature.
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
        # The fetch itself proves the session: a logged-in request returns JSON,
        # so a separate pre-flight login probe is a wasted request against a
        # site we're deliberately keeping load off. The fetch raises
        # NotLoggedInError instead, and only then do we re-authenticate.
        "login_via_fetch": True,
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
    if not _stdin_interactive():
        # Running headless (e.g. pythonw.exe via the scheduled task) -- input()
        # would block forever with no console attached, hanging the crawler
        # indefinitely with nothing left in the logs. Fail loudly instead.
        raise SessionCheckError(
            f"[{site_name}] Cookies are missing or expired and no console is "
            "attached to prompt for new ones (running as a silent scheduled "
            "task). Run `python crawler.py` manually once to re-enter cookies."
        )
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

    cookies = _load_cookies(site_name)
    if cookies:
        sess = _make_session(cookies, domain)
        # Retry a network-flaky check a few times before concluding anything --
        # a DNS blip or timeout is not the same as an actual logout and must
        # never be treated as a reason to prompt for new cookies.
        for attempt in range(3):
            try:
                if adapter["is_logged_in"](sess, site_cfg, cfg, log):
                    log.info("[%s] Loaded saved session.", site_name)
                    return sess
                break  # genuinely not logged in -- fall through to reauth below
            except SessionCheckError:
                if attempt < 2:
                    wait = 30 * (attempt + 1)
                    log.warning("[%s] Retrying session check in %ds...", site_name, wait)
                    time.sleep(wait)
                else:
                    log.warning("[%s] Could not verify session after retries "
                                "(network issue) — will try again next cycle.",
                                site_name)
                    raise
        log.warning("[%s] Saved session expired or invalid.", site_name)

    # Only reached on a genuine logout, never on a network hiccup.
    while True:
        cookies = _prompt_cookies(site_name, adapter["cookie_names"])
        sess    = _make_session(cookies, domain)
        try:
            if adapter["is_logged_in"](sess, site_cfg, cfg, log):
                _save_cookies(site_name, cookies)
                log.info("[%s] Session verified and saved.", site_name)
                return sess
        except SessionCheckError as e:
            log.warning("[%s] Could not verify new cookies (network issue): %s", site_name, e)
        print(f"  Could not verify login for {site_name} — please check the cookie values.\n")

# ===========================================================================
# Per-site crawl cycle
# ===========================================================================

def run_once_site(site_name: str, site_cfg: dict, sess: requests.Session,
                  cfg: dict, watchlist_data: dict, app_log, dl_log) -> requests.Session:
    adapter = SITE_ADAPTERS[site_cfg["type"]]

    if adapter.get("login_via_fetch"):
        # No pre-flight probe: go straight to the fetch, which proves the
        # session as a side effect. Only a genuine NotLoggedInError (raised
        # after retries have ruled out network errors and challenge pages)
        # triggers re-authentication.
        try:
            torrents = adapter["fetch_torrents"](site_name, sess, site_cfg, cfg, app_log)
        except SessionCheckError as e:
            app_log.warning("[%s] Skipping this cycle — site unreachable (%s).", site_name, e)
            return sess
        except NotLoggedInError as e:
            app_log.warning("[%s] Session appears expired (%s) — re-authenticating.", site_name, e)
            try:
                sess = get_session(site_name, site_cfg, cfg, app_log)
                torrents = adapter["fetch_torrents"](site_name, sess, site_cfg, cfg, app_log)
            except (SessionCheckError, NotLoggedInError) as e2:
                app_log.warning("[%s] %s — skipping this cycle.", site_name, e2)
                return sess
    else:
        # Refresh session if needed
        try:
            logged_in = adapter["is_logged_in"](sess, site_cfg, cfg, app_log)
        except SessionCheckError:
            app_log.warning("[%s] Skipping this cycle — couldn't verify session (network issue).", site_name)
            return sess

        if not logged_in:
            app_log.warning("[%s] Session expired — re-authenticating.", site_name)
            try:
                sess = get_session(site_name, site_cfg, cfg, app_log)
            except SessionCheckError as e:
                app_log.warning("[%s] %s — skipping this cycle.", site_name, e)
                return sess

        torrents = adapter["fetch_torrents"](site_name, sess, site_cfg, cfg, app_log)

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

    # When several releases satisfy the same episode/movie, keep just one per
    # the configured preference (default: smallest file). This is also what
    # stops two releases of the same episode (e.g. Silo S03E07) being queued.
    matches = _select_preferred(matches, cfg.get("prefer_size", "smallest"),
                                site_name, app_log)

    def _ep_sort_key(m):
        parsed = _parse_episode(m.get("matched_episode", ""))
        return parsed if parsed else (0, 0)

    matches.sort(key=_ep_sort_key)

    for match in matches:
        torrent = match["torrent"]
        # Re-validate against the CURRENT watchlist — it may have just advanced
        # after downloading an earlier torrent this same cycle. Without this,
        # two different releases of the same episode both matched the original
        # snapshot and BOTH downloaded (e.g. Silo S03E07 grabbed twice). Once
        # S03E07 advances the show to S03E08, any other S03E07 release — or a
        # movie already downloaded and removed this cycle — no longer matches
        # and is skipped.
        current = match_torrent(torrent, watchlist_data, min_seeders)
        if not current:
            app_log.info("[%s] Skipping '%s' — episode already satisfied this cycle.",
                         site_name, torrent.get("name", "?"))
            continue
        handle_match(current, sess, lambda s, t, d, c, l: download_fn(s, t, d, c, l),
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
        try:
            sessions[site_name] = get_session(site_name, site_cfg, cfg, app_log)
        except SessionCheckError as e:
            app_log.warning("[%s] Could not establish session at startup (%s). "
                            "Will keep retrying each cycle.", site_name, e)
            sessions[site_name] = requests.Session()  # placeholder; retried in run_once_site

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
