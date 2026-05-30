# TL Watchlist Downloader

Polls TorrentLeech every 1–2 hours and auto-downloads `.torrent` files for anything on your watchlist.

## Install

```bash
pip install -r requirements.txt
```

## First Run

```bash
python crawler.py
```

On first run it will ask for your TorrentLeech session cookies:
1. Open Chrome → torrentleech.org (logged in)
2. F12 → Application → Cookies → `https://www.torrentleech.org`
3. Copy and paste: `PHPSESSID`, `tlpass`, `tluid`

Cookies are saved to `state/session_cookies.pkl` and reused automatically.

## Run at Windows Startup (recommended)

Open PowerShell as Administrator, then:

```powershell
.\Install-Scheduler.ps1
schtasks /run /tn TL-WatchlistDownloader
```

To uninstall: `Unregister-ScheduledTask -TaskName TL-WatchlistDownloader -Confirm:$false`

## Adding to the Watchlist

Edit `watchlist.yml` — changes take effect on the next check without restarting.

```yaml
categories:
  tv:
    default_keywords: [1080p]
    watchlist:
    - [Show Name, S01E01]      # title + episode
    - [Another Show, S02E05]
  movies:
    default_keywords: [2160p]
    watchlist:
    - [Movie Title]            # title only, keyword handles the rest
```

Each entry is `[title, episode]` for TV or `[title]` for movies. The `default_keywords` (e.g. `1080p`, `2160p`) are matched automatically — add extra keywords inline: `[Movie Title, HDR]`.

Once downloaded, the entry is removed from the watchlist automatically.

## Logs

| File | Contents |
|------|----------|
| `logs/app.log` | Crawler activity, matches, errors |
| `logs/downloaded.log` | One line per downloaded torrent |
