# Installation and configuration

[Back to the README](../README.md)

## Install

### Docker Compose

Pulls the published image from GHCR:

```bash
docker compose up -d
```

Open <http://localhost:8999>. PVArr serves on port **8999** by default; change it with `PORT` — see [Configuration](installation.md#environment-variables).

To build from source instead of pulling, layer the build override:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

The image is published as [`ghcr.io/jlesterak/pvarr`](https://github.com/jlesterak/pvarr/pkgs/container/pvarr):

```bash
docker pull ghcr.io/jlesterak/pvarr:latest
```

### Pinning a version

By default you track `latest`, so `docker compose pull` picks up new releases.
On a machine doing real recording you probably want upgrades to be deliberate —
set `PVARR_TAG` in `.env` to pin an exact version:

```bash
echo "PVARR_TAG=0.1.1" >> .env
docker compose up -d
```

Rolling back is then a one-line edit. Unset it to follow `latest` again.

### CLI

```bash
./start.sh
```

`start.sh` creates a `venv/`, installs `requirements.txt`, and serves on `${HOST:-0.0.0.0}:${PORT:-8999}`. Set `PVARR_NO_VENV=1` to skip virtualenv creation (this is what the container does).

### Requirements

- **Linux on amd64 (x86-64).** The published image is amd64 only (~900 MB on disk); there is no ARM build for Raspberry Pi or ARM NAS boxes yet.
- **FFmpeg** — required. Does the actual recording.
- **Python 3.10+** and the packages in `requirements.txt` (FastAPI, uvicorn, yt-dlp and others). yt-dlp is what sets the floor: its current releases need 3.10. The container image uses 3.12.
- **yt-dlp** + **curl_cffi** — installed from `requirements.txt`, ~16 MB together. Resolves pages whose player fetches its manifest over XHR, which PVArr's own scraper cannot see, and gives it a browser-compatible HTTPS client. Optional at runtime: if `yt-dlp` is not on `PATH`, that step is skipped and everything else works.
- **[hls-restream-proxy](https://github.com/pcruz1905/hls-restream-proxy)** — *optional*. Two fallbacks live here. `hls-proxy` bridges a stream when a direct FFmpeg connection fails despite correct headers, usually because the token needs continuous refreshing. `detect-headers` tries harder than the built-in probe at following redirect and iframe chains — it is **not** a browser (see [When the probe can't work it out](finding-streams.md#when-the-probe-cant-work-it-out)).

  PVArr resolves these on `PATH` at runtime and degrades gracefully if they're absent: header detection and direct recording are built in and need no external tools. The provided `Dockerfile` installs them into the image automatically.

Verify the app is up by loading the dashboard:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8999/
```

## Environment variables

All configuration is environment-based; recordings are configured per-job from the dashboard or the API.

| Variable | Default | Purpose |
|---|---|---|
| `TZ` | `Etc/UTC` | Your timezone, e.g. `America/Denver`. Recording filenames are dated in local time, so without it an evening recording on a UTC container gets the next day's date. Set in `.env`; `docker-compose.yml` passes it through. |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8999` | HTTP port |
| `PVARR_FLARESOLVERR_URL` | unset | Optional. The address of a [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) container, e.g. `http://flaresolverr:8191`, used only when an event page answers with an anti-bot check instead of the page. Unset means the fallback is off; pasting your browser's cookie still works. FlareSolverr runs a headless browser (up to ~550 MB of RAM and most of a core for 15–70 s per solve) and cannot solve interactive captchas. |
| `PVARR_FLARESOLVERR_TIMEOUT` | `120` | Seconds FlareSolverr may spend solving one page (bounded to 10–300). Some challenges take over a minute. |
| `PVARR_NO_VENV` | unset | Set to `1` to skip virtualenv creation in `start.sh` (used in-container) |
| `PVARR_RECORDINGS_DIR` | `./recordings` | Where recordings are written. The container sets this to `/recordings` so captures land on the mounted volume rather than inside the image. |
| `PVARR_ALLOWED_DIRS` | unset | Extra directories the library API and `output_dir` may write to, `:`-separated. By default only the recordings dir is reachable. |
| `PVARR_LOG_LEVEL` | `INFO` | Root log level. |
| `PVARR_CONFIG_DIR` | `./config` | Where session state is written so a restart can resume an in-flight recording. The container sets this to `/config`. |
| `PVARR_MAX_RESUME_GAP` | `300` | Seconds a recording's file may sit untouched before a restart finalises it instead of reconnecting. Measured from the file's last write, not from when the recording started. |
| `PVARR_MAX_RESUME_ATTEMPTS` | `3` | How many times a session may be resumed before it is finalised instead. Stops a reproducibly broken recording from restart-looping forever. |
| `PUID` / `PGID` | `1000` | User and group the app runs as inside the container. The entrypoint aligns the `pvarr` user to these and takes ownership of the three mount roots, so recordings land owned by you on the host. Set them to your own `id -u` / `id -g`. |
| `PVARR_GRACEFUL_TIMEOUT` | `5` | Seconds PVArr may spend closing open HTTP connections before it stops the recorders. Open dashboard tabs and live stream clients hold connections indefinitely, so without a bound a `docker stop` waits on them and never gets to the recorders at all. |
| `PVARR_SHUTDOWN_TIMEOUT` | `20` | Seconds a stop may spend finishing in-flight recordings — remux, rename, notify — before the process exits anyway. Raise it if you routinely remux very large files. |
| `PVARR_MIN_FREE_GB` | `5` | Free space, in GB, below which an active recording aborts and a new one is refused. `0` disables the guard entirely — only sensible if the recordings volume is separate from the system disk. |
| `PVARR_COMSKIP` | `0` | Set to `1` to run commercial detection on each finished recording. Off by default because it costs roughly 20–40 minutes of CPU on a three-hour capture. Runs *after* the recording is remuxed, in the library and announced, so it never delays anything you were waiting for. |
| `PVARR_COMSKIP_MODE` | `chapters` | `chapters` writes skip points into the file and changes nothing else. `cut` also removes the detected breaks — see the safety notes below. Anything unrecognised means `chapters`. |
| `PVARR_COMSKIP_KEEP_ORIGINAL` | `1` | With `cut`, keep the uncut recording alongside as `<name>.original.mp4`. On by default: cutting is a heuristic acting on footage you cannot re-record, so the safe default costs disk rather than a play. |
| `PVARR_COMSKIP_INI` | unset | Path to your own `comskip.ini`. Tuning is per-source and genuinely matters — logo and blank-frame thresholds that suit one broadcaster are wrong for another. Without it PVArr writes a minimal default. |
| `PVARR_MAX_HOURS` | `6` | Longest any one recording may run without an explicit duration, in hours. A capture pointed at a 24/7 channel never ends on its own — the stream does not stop, so nothing in the failover logic ever fires. 6 rather than 4 so NFL overtime and extra-innings baseball are not truncated. A per-recording duration overrides it; `0` disables it. Rebroadcast channels are never capped by it. |
| `PVARR_BUFFER_MB` | `71` | Size of a rebroadcast channel's buffer, per channel. Roughly 60 seconds at 10 Mbps — deep enough for a client to join late or stall briefly without a gap. It is a file, not memory, so the kernel reclaims it under pressure. |
| `PVARR_BUFFER_DIR` | `<recordings>/.buffers` | Where rebroadcast buffers live. Each takes a steady ~1 MB/s of writes, so point it at an SSD if your library is on a spinning disk. Buffers are deleted when the channel stops. |
| `DISCORD_WEBHOOK_URL` | unset | Discord webhook for notifications. Translated to an Apprise URL automatically — an existing value keeps working. |
| `TELEGRAM_BOT_TOKEN` | unset | Telegram bot token |
| `TELEGRAM_CHAT_ID` | unset | Telegram destination chat |
| `PVARR_APPRISE_URLS` | unset | Any [Apprise](https://github.com/caronc/apprise) URLs, comma- or space-separated — ntfy, Gotify, Pushover, Matrix, Slack, email, plain webhooks. e.g. `ntfy://ntfy.sh/my-topic`. Combines with the Discord/Telegram settings above. |
| `PVARR_DEVICE_ID` | derived from hostname | 8-hex-digit HDHomeRun device id Plex keys the DVR off. Set it to pin the id across hosts. |
| `PVARR_TUNER_COUNT` | `4` | Concurrent tuners advertised to Plex |
| `PLEX_URL` | unset | Plex server URL for library refresh |
| `PLEX_TOKEN` | unset | Plex auth token |
| `EMBY_URL` | unset | Emby server URL for library refresh |
| `EMBY_API_KEY` | unset | Emby API key |

`PVARR_GRACEFUL_TIMEOUT` and `PVARR_SHUTDOWN_TIMEOUT` run **in sequence** and together must fit inside the compose
`stop_grace_period` (30s), or Docker `SIGKILL`s partway through and an
in-flight recording loses the marker that lets it resume. The defaults leave
five seconds of headroom (5 + 20 = 25). Raise one and lower the other, or raise
`stop_grace_period` to match.

For Docker, copy these into a `.env` beside `docker-compose.yml` — it is gitignored.

Recordings are written to `PVARR_RECORDINGS_DIR` (default `recordings/` in the
project directory). The supplied `docker-compose.yml` sets it to `/recordings`
and mounts `./recordings` there.
