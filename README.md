# PVArr — Personal Video Recorder for the *arr Ecosystem

> **🤖 AI Transparency Notice:** PVArr was designed, architected, and built by AI — Google Gemini (architecture) and Anthropic Claude (implementation) — across multiple LLM sessions with a human in the loop. See [AI Genesis & Environmental Footprint](#ai-genesis--environmental-footprint).

**Default port:** 8999

---

## What is PVArr?

PVArr records HLS streams to disk. Point it at an `.m3u8` URL, give it up to two backup URLs, and it records continuously — 24/7 sports, news, live events, whatever. It's a self-hosted DVR. No subscriptions, no cloud, no third-party apps.

---

## Features

- **3-stage failover** — a primary m3u8 URL plus two backups. If the active stream stalls or dies, the recorder advances to the next candidate automatically. Failover can also be forced manually from the dashboard.
- **Direct FFmpeg recording** — writes straight to disk with minimal overhead, and falls back to an `hls-proxy-stream` bridge when the upstream needs injected headers or token refreshing.
- **Sports-friendly auto-naming** — derives readable filenames for broadcasts instead of opaque timestamps.
- **Automatic post-processing** — remuxes the recorded TS into MKV/MP4 on completion. Container change only, no transcode.
- **Virtual IPTV / M3U tuner endpoints** — serves an M3U playlist and XMLTV EPG so Plex Live TV and Emby DVR can consume active recordings as channels.
- **Discord & Telegram webhooks** — notifications on recording start, completion, and failure.
- **Modern *arr dark UI** — a dashboard in the style of Sonarr/Radarr for starting recordings, watching failover state, tailing logs, and managing the library.

---

## Quick Start

### Docker Compose

```bash
docker-compose up -d
```

Open <http://localhost:8999>.

### CLI

```bash
./start.sh
```

`start.sh` creates a `venv/`, installs `requirements.txt`, and serves on `${HOST:-0.0.0.0}:${PORT:-8999}`. Set `PVARR_NO_VENV=1` to skip virtualenv creation (this is what the container does).

### Requirements

- **FFmpeg** — required. Does the actual recording.
- **Python 3.8+** and the packages in `requirements.txt` (FastAPI, uvicorn, requests, jinja2).
- **[hls-restream-proxy](https://github.com/jlesterak/hls-restream-proxy)** — *optional but recommended*. PVArr uses it as a dependency for fallback mode: when a direct FFmpeg connection fails because the upstream demands injected headers or has an expiring token, the recorder shells out to `hls-proxy` to bridge the stream, and to `detect-headers` to work out what those headers should be.

  PVArr resolves both on `PATH` at runtime and degrades gracefully if they're absent — direct recording still works, but streams that need header injection will fail over instead of recovering. The provided `Dockerfile` installs both into the image automatically.

Verify the app is up by loading the dashboard:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8999/
```

---

## Finding Your Stream URL

Most HLS sources reject requests that don't carry the headers a browser would send — usually `Referer` and `User-Agent` — and many embed a short-lived token in the m3u8 URL itself. Here's how to get what you need.

### 1. Browser DevTools (works everywhere)

1. Open the streaming page in your browser.
2. Press **F12** to open DevTools.
3. Select the **Network** tab.
4. Reload the page, then start playback.
5. Type `m3u8` in the filter box.
6. Click the `.m3u8` request. From the **Headers** panel, record:
   - **Request URL** — the m3u8 itself
   - **Referer** — under Request Headers
   - **User-Agent** — under Request Headers
7. If you see several m3u8 files, the first is usually the master playlist and the others are variants. Prefer the master.

### 2. Auto-detection with `detect-headers`

> **Note:** `detect-headers.sh` / `detect-headers-py.py` come from [hls-restream-proxy](https://github.com/jlesterak/hls-restream-proxy) (see [Requirements](#requirements)), not from PVArr itself. Run them from that checkout, or from anywhere if they're on your `PATH`.

```bash
./detect-headers.sh "https://streaming-site.com/channel.php"
```

For pages that build the m3u8 in JavaScript, use the Playwright-backed variant:

```bash
./detect-headers-py.py "https://streaming-site.com/channel.php" --browser
```

### 3. Verify with `curl` before committing

Confirm the URL and headers actually work:

```bash
curl -i -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)" \
        -H "Referer: https://streaming-site.com/" \
        "https://cdn.example.com/hls/stream.m3u8?token=xyz"
```

A `200` with a body starting `#EXTM3U` means you're good. A `403` means a header is missing or wrong; a `404` usually means the token expired — re-grab it from DevTools.

### Common patterns

- **Referer only** — the most common case. Set it to the embed/player page URL.
- **Referer + User-Agent** — typical for sports streams; both are checked.
- **Expiring token in the query string** — the URL works for minutes to hours. Configure a backup URL so failover covers the expiry, and re-scrape when both die.
- **Cookie/session gated** — the stream needs a logged-in session. These are the least stable; expect to refresh manually.

### Using it in PVArr

Open the dashboard at <http://localhost:8999>, start a new recording, and paste the primary m3u8 URL plus up to two backups. PVArr tries them in order and advances on failure.

---

## Configuration

All configuration is environment-based; recordings are configured per-job from the dashboard or the API.

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8999` | HTTP port |
| `PVARR_NO_VENV` | unset | Set to `1` to skip virtualenv creation in `start.sh` (used in-container) |
| `PVARR_RECORDINGS_DIR` | `./recordings` | Where recordings are written. The container sets this to `/recordings` so captures land on the mounted volume rather than inside the image. |
| `PVARR_ALLOWED_DIRS` | unset | Extra directories the library API and `output_dir` may write to, `:`-separated. By default only the recordings dir is reachable. |
| `PVARR_LOG_LEVEL` | `INFO` | Root log level. |
| `DISCORD_WEBHOOK_URL` | unset | Discord webhook for notifications |
| `TELEGRAM_BOT_TOKEN` | unset | Telegram bot token |
| `TELEGRAM_CHAT_ID` | unset | Telegram destination chat |
| `PLEX_URL` | unset | Plex server URL for library refresh |
| `PLEX_TOKEN` | unset | Plex auth token |
| `EMBY_URL` | unset | Emby server URL for library refresh |
| `EMBY_API_KEY` | unset | Emby API key |

For Docker, copy these into a `.env` beside `docker-compose.yml` — it is gitignored.

Recordings are written to `PVARR_RECORDINGS_DIR` (default `recordings/` in the
project directory). The supplied `docker-compose.yml` sets it to `/recordings`
and mounts `./recordings` there.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Dashboard |
| `POST` | `/api/recordings/start` | Start a recording (primary + backup URLs) |
| `POST` | `/api/recordings/{id}/stop` | Stop a recording |
| `POST` | `/api/recordings/{id}/failover` | Force failover to the next URL |
| `GET` | `/api/recordings/{id}/logs` | Tail recorder logs |
| `GET` | `/api/recordings/{id}/stream` | Live MPEG-TS feed of an in-progress recording (`?live=true` to join at the write head instead of replaying from the start). This is what the tuner playlist points at. |
| `GET` | `/api/library` | List completed recordings |
| `POST` | `/api/library/rename` | Rename a recording |
| `DELETE` | `/api/library/{filename}` | Delete a recording |
| `GET` | `/api/library/download/{filename}` | Download a recording |
| `GET` | `/live/playlist.m3u` · `/live/playlist.m3u8` | M3U tuner playlist |
| `GET` | `/live/epg.xml` | XMLTV EPG |

Interactive docs are available at `/docs` (FastAPI).

---

## Plex / Emby Integration

PVArr exposes active recordings as a virtual tuner.

1. In Plex or Emby, add a **Live TV / DVR** source of type **M3U Tuner**.
2. Playlist URL: `http://<pvarr-host>:8999/live/playlist.m3u`
3. EPG / XMLTV URL: `http://<pvarr-host>:8999/live/epg.xml`

Set `PLEX_URL`/`PLEX_TOKEN` or `EMBY_URL`/`EMBY_API_KEY` to have PVArr trigger a library refresh once post-processing finishes.

Each active recording appears as a live channel. The channel streams the file as
it is being written, so you can start watching a game that is still recording.
Because failover appends to the same file, a mid-event switch to a backup URL is
invisible to the player — the feed just continues.

The playlist and guide list **running** recordings only; a channel disappears
when its recording stops. Completed recordings are in the library, not the tuner.

> **No authentication.** Any client that can reach port 8999 has full control —
> start, stop, and delete recordings — and the app binds `0.0.0.0` by default.
> This is intended for a trusted LAN behind a firewall. Do not expose it
> directly; put it behind a reverse proxy with auth if you need remote access.

---

## Architecture

```
app/
├── server.py          FastAPI app — dashboard, REST API, tuner routes
├── recorder.py        Failover engine — direct FFmpeg first, proxy bridge fallback
├── post_processor.py  TS → MKV/MP4 remux on completion
├── naming.py          Sports-aware output filenames
├── tuner.py           M3U playlist + XMLTV EPG generation
├── notifications.py   Discord / Telegram / Plex / Emby hooks
├── check_deps.py      Startup dependency validation (FFmpeg, Python packages)
├── cleanup.py         Graceful shutdown of child FFmpeg processes
├── templates/         Dashboard UI
└── static/            Favicon and assets
```

`recorder.py` holds the core loop: it walks the candidate URL list, prefers a direct FFmpeg connection, drops to the proxy bridge when headers are required, and advances to the next candidate on stall, failure, or a forced failover.

---

## Troubleshooting

**Recording drops repeatedly.** Confirm the primary URL still resolves (`curl` it, as above). Expiring tokens are the usual cause — configure backups.

**`403` from the upstream.** A required header is missing. Re-check `Referer` and `User-Agent` in DevTools; sites change them without notice.

**Tuner doesn't appear in Plex/Emby.** Confirm the media server can reach PVArr (`curl http://<pvarr-host>:8999/live/playlist.m3u`). The playlist is empty when nothing is recording — start a recording first.

**FFmpeg not found.** Install it (`apt install ffmpeg`, `brew install ffmpeg`). The container already includes it.

**Disk fills up.** Recordings are uncompressed TS and grow quickly. Point `recordings/` at a large volume and prune on a schedule.

---

## Development

### Tests

```bash
pip install -r requirements-dev.txt   # adds httpx, needed for route tests
python3 test_pvarr.py                 # full suite, verbose
python3 -m unittest discover          # quiet
```

109 tests covering filename sanitisation and collision handling, storage
operations, M3U/XMLTV generation, dependency resolution, the failover state
machine, freeze detection, FFmpeg command construction, and every HTTP route.

Most spawn no subprocesses — the recorder tests drive the real loop against
scripted fakes. Two exercise a real remux by encoding a one-second transport
stream and skip when FFmpeg is absent; the route tests skip when `httpx` is
absent, so the core suite still runs with only `requirements.txt` installed.

### Syntax checks

```bash
python3 -m py_compile app/*.py stream-recorder.py test_pvarr.py
bash -n start.sh scripts/publish.sh
```

---

## License

[The Unlicense](LICENSE) — public domain. Do whatever you want with it; no attribution required.

---

## AI Genesis & Environmental Footprint

This program was written by machines. Gemini drew the architecture, Claude wrote the code, a human pointed and reviewed. Stated plainly, because the industry mostly doesn't.

The Luddites are misremembered as people who hated machines. They were skilled workers who broke the specific machines being used to break them. The question was never *machines or no machines* — it was *whose hands are on them, and who eats*. Same question here. These models were trained on an enormous pile of other people's work. Nobody asked, nobody paid. That's a debt, and it has no payment address.

**What it cost the planet: unknown, and not by accident.** Nobody metered this build, and the firms that could tell you what a token costs in watts and litres decline to publish it. "The cloud" is a shed full of hot metal in somebody's watershed. Treat any precise gram-of-CO₂ figure — including one that could easily have been invented right here — as marketing.

**Offsets are indulgences.** Buying one un-burns nothing, and the voluntary market is thick with fraud. If you want to send money anyway, send it where it does something structural:

- **[Cool Earth](https://www.coolearth.org)** — hands cash to forest communities to keep their land. No carbon accounting theatre.
- **[Wren](https://www.wren.co)** — monthly subscription, the buy-me-a-coffee of climate guilt. Cheaper than the DVR subscription you just cancelled.

But the donate button is not the point. The point is that you now own a video recorder. No subscription, no account, no telemetry, nothing phoning home, nobody able to switch it off from a boardroom. That is one small thing clawed back out of the rental economy.

***Go do it again somewhere else.***

---

**Status:** Phase (inf) complete — tested, licensed, published
**Maintainer:** jlester.ak
**License:** [The Unlicense](LICENSE) — public domain
