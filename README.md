# PVArr — Personal Video Recorder for the *arr Ecosystem

> **🤖 AI Transparency Notice:** PVArr was designed, architected, and built by AI across multiple LLM sessions with a human in the loop. Google Gemini drew the initial architecture; Anthropic Claude has been the primary architect and maintainer since, and does the ongoing design, implementation, testing, and review. See [AI Genesis & Environmental Footprint](#ai-genesis--environmental-footprint).

**Default port:** 8999

---

## What is PVArr?

PVArr records HLS streams to disk. Point it at an `.m3u8` URL, give it up to two backup URLs, and it records continuously — 24/7 sports, news, live events, whatever. It's a self-hosted DVR. No subscriptions, no cloud, no third-party apps.

---

## Features

- **Paste-and-record header detection** — give it an m3u8 (or the page playing one) and PVArr resolves the playlist, works out the `Referer`/`Cookie` the origin demands, and verifies a real segment downloads before you start. See [Finding Your Stream URL](#finding-your-stream-url).
- **3-stage failover** — a primary m3u8 URL plus two backups. If the active stream stalls, dies, or goes quiet without dropping the connection, the recorder advances to the next candidate automatically. Failover can also be forced manually from the dashboard; the button is disabled when a session has no backup left, since there would be nothing to switch to.
- **Direct FFmpeg recording** — writes straight to disk with minimal overhead, and falls back to an `hls-proxy-stream` bridge when the upstream needs injected headers or token refreshing.
- **Sports-friendly auto-naming** — derives readable filenames for broadcasts instead of opaque timestamps.
- **Automatic post-processing** — remuxes the recorded TS into MKV/MP4 on completion. Container change only, no transcode. The Plex/Emby library scan is triggered *after* the remux, so the media server indexes the finished MP4 rather than the TS that is about to be deleted.
- **Virtual tuner** — emulates a HDHomeRun for Plex Live TV and serves an M3U playlist plus XMLTV EPG for Emby/Jellyfin, so active recordings appear as channels.
- **Discord & Telegram webhooks** — notifications on recording start, completion, and failure.
- **Modern *arr dark UI** — a dashboard in the style of Sonarr/Radarr for starting recordings, watching failover state, tailing logs, and managing the library.

---

## Quick Start

### Docker Compose

Pulls the published image from GHCR:

```bash
docker compose up -d
```

Open <http://localhost:8999>.

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

- **FFmpeg** — required. Does the actual recording.
- **Python 3.8+** and the packages in `requirements.txt` (FastAPI, uvicorn, requests, jinja2).
- **[hls-restream-proxy](https://github.com/jlesterak/hls-restream-proxy)** — *optional*. Two fallbacks live here. `hls-proxy` bridges a stream when a direct FFmpeg connection fails despite correct headers, usually because the token needs continuous refreshing. `detect-headers` drives a real browser, and is tried only when PVArr's own probe cannot find the m3u8 — pages that build their URL in JavaScript.

  PVArr resolves both on `PATH` at runtime and degrades gracefully if they're absent: header detection and direct recording are built in and need no external tools. The provided `Dockerfile` installs both into the image automatically.

Verify the app is up by loading the dashboard:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8999/
```

---

## Finding Your Stream URL

Paste the URL into **Add Recording** and press start. PVArr works out the rest.

Most HLS sources reject requests that don't carry the headers a browser would
send — usually `Referer`, sometimes a `Cookie` — and many embed a short-lived
token in the m3u8 URL itself. You do not have to find those headers by hand.
When you paste a URL, PVArr:

1. Resolves it to a playlist. An `.m3u8` is used directly; a page URL is
   fetched and the m3u8 pulled out of the HTML or inline JavaScript.
2. Tries the plausible header combinations against the real origin — no headers
   first, then the embed page as `Referer`, then the site root, then any referer
   the URL carries in its own query string.
3. Keeps the first combination that returns an actual `#EXTM3U` body, and
   fetches one media segment with those same headers to confirm the stream is
   playable and not just the manifest.
4. Reports back in the form: a green line with what it found (master or media
   playlist, variant count, which headers were needed), or a red line saying
   what the origin returned.

The same probe runs again inside the recorder each time it connects to a
candidate — including on failover an hour later — so an expired token is
re-resolved rather than replayed.

### Getting the URL to paste

If the site plays the stream on a normal page, paste the page URL. If that
comes back red, or the player builds its URL in JavaScript, take the m3u8 from
the browser instead:

1. Open the streaming page and press **F12**.
2. Select the **Network** tab, reload, and start playback.
3. Type `m3u8` in the filter box.
4. Right-click the `.m3u8` request → **Copy → Copy link address**.

Paste that into PVArr. Copying the `Referer` and `User-Agent` by hand is no
longer part of the job — the probe derives them. If several m3u8 files appear,
prefer the master playlist (usually the first, often named `master` or `index`);
PVArr shows the variants it found so you can confirm you got the right one.

### When the probe can't work it out

Under each URL field is **Set headers manually**, with `Referer`, `User-Agent`,
and `Cookie`. Anything the probe detected is filled in there, so you can correct
one field rather than supply all three. A value you type wins, and is tried
first on the next probe.

Two cases genuinely need this:

- **Cookie/session gated.** The stream needs a logged-in session. Copy the
  `Cookie` request header from the same DevTools request. A probe that reports
  *segments rejected* is usually this.
- **Referer the probe cannot guess** — a third site's URL, unrelated to either
  the page or the CDN.

For pages that only assemble their m3u8 after running JavaScript, PVArr will
also shell out to `detect-headers` from
[hls-restream-proxy](https://github.com/jlesterak/hls-restream-proxy) when it is
installed (see [Requirements](#requirements)); it drives a real browser and can
see what a plain fetch cannot. It runs only after the built-in probe comes up
empty, and is optional.

### Checking a URL from the shell

`POST /api/probe` is the same code path the dashboard uses:

```bash
curl -s -X POST http://localhost:8999/api/probe \
     --data-urlencode "url=https://cdn.example.com/hls/stream.m3u8?token=xyz" | jq
```

```json
{
  "ok": true,
  "m3u8_url": "https://cdn.example.com/hls/stream.m3u8?token=xyz",
  "referer": "https://streaming-site.com/",
  "kind": "master",
  "headers_required": ["Referer"],
  "segment_ok": true,
  "message": "Master playlist, 5 variants, needs Referer."
}
```

A failed probe returns `ok: false` and the status it saw: `403` means every
header combination was refused, `404` usually means the token has expired —
re-copy it from DevTools.

### Backups

The two backup slots are probed independently and recorded in order. Since
tokens expire, a backup from a *different* source is worth more than a second
URL from the same one.

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
| `PVARR_DEVICE_ID` | derived from hostname | 8-hex-digit HDHomeRun device id Plex keys the DVR off. Set it to pin the id across hosts. |
| `PVARR_TUNER_COUNT` | `4` | Concurrent tuners advertised to Plex |
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
| `POST` | `/api/probe` | Resolve a URL to a playlist and detect the headers it needs |
| `POST` | `/api/recordings/start` | Start a recording (primary + backup URLs) |
| `POST` | `/api/recordings/{id}/stop` | Stop a recording |
| `POST` | `/api/recordings/{id}/failover` | Force failover to the next URL. Returns `400` if the session is not running, or if it is already on its last candidate — advancing past the end would end the recording, not fail it over. |
| `GET` | `/api/recordings/{id}/logs` | Tail recorder logs |
| `GET` | `/api/recordings/{id}/stream` | Live MPEG-TS feed of an in-progress recording (`?live=true` to join at the write head instead of replaying from the start). This is what the tuner playlist points at. |
| `GET` | `/api/library` | List completed recordings |
| `POST` | `/api/library/rename` | Rename a recording |
| `DELETE` | `/api/library/{filename}` | Delete a recording |
| `GET` | `/api/library/download/{filename}` | Download a recording |
| `GET` | `/live/playlist.m3u` · `/live/playlist.m3u8` | M3U tuner playlist |
| `GET` | `/live/epg.xml` | XMLTV EPG |
| `GET` | `/discover.json` · `/lineup.json` · `/lineup_status.json` · `/lineup.post` · `/device.xml` | HDHomeRun tuner emulation, also served under `/live` |

Interactive docs are available at `/docs` (FastAPI).

---

## Plex / Emby Integration

PVArr exposes active recordings as a virtual tuner.

**Plex (HDHomeRun — recommended).** Plex discovers PVArr as a tuner device:

1. **Settings → Live TV & DVR → Set up Plex DVR**, then *Don't see your HDHomeRun? Enter its network address manually*.
2. Device address: `http://<pvarr-host>:8999` — the **base URL, with no path**. Pasting the playlist URL here fails: Plex appends `/discover.json` to whatever you type and gets a 404.
3. When asked for a guide, choose **Have an XMLTV guide on your server?** and enter `http://<pvarr-host>:8999/live/epg.xml`.

**M3U tuner (Emby, Jellyfin, Plex's M3U path).**

1. Add a **Live TV / DVR** source of type **M3U Tuner**.
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
├── probe.py           Stream probe — URL → playlist + the headers it needs
├── post_processor.py  TS → MKV/MP4 remux on completion
├── naming.py          Sports-aware output filenames
├── tuner.py           M3U playlist, XMLTV EPG, HDHomeRun emulation
├── notifications.py   Discord / Telegram / Plex / Emby hooks
├── check_deps.py      Startup dependency validation (FFmpeg, Python packages)
├── cleanup.py         Graceful shutdown of child FFmpeg processes
├── templates/         Dashboard UI
└── static/            Favicon and assets
```

`recorder.py` holds the core loop: it walks the candidate URL list, prefers a direct FFmpeg connection, drops to the proxy bridge when headers are required, and advances to the next candidate on stall, failure, or a forced failover. Before each connection it calls `probe.py` to re-resolve that candidate — playlist URLs carry short-lived tokens, so a failover an hour in needs a fresh answer rather than the one the dashboard found at submit time.

---

## Troubleshooting

**Recording drops repeatedly.** Confirm the primary URL still resolves by pasting it back into Add Recording. Expiring tokens are the usual cause — configure backups from a different source.

**`403` from the upstream.** A required header is missing. Re-run the URL through `POST /api/probe` (or just re-paste it into Add Recording) — the message names the status the origin returned. If the probe reports *segments rejected*, the stream is session gated: copy the `Cookie` request header from DevTools into the manual header fields.

**Tuner doesn't appear in Plex/Emby.** Confirm the media server can reach PVArr (`curl http://<pvarr-host>:8999/live/playlist.m3u`). The playlist is empty when nothing is recording — start a recording first.

**Plex says it can't find a tuner, and the PVArr log shows `404` on `discover.json` / `lineup.json`.** The device address includes a path. Plex appends its own filenames to whatever you enter, so `.../live/playlist.m3u` is probed as `.../live/playlist.m3u/discover.json`. Enter `http://<pvarr-host>:8999` (or `http://<pvarr-host>:8999/live`) instead.

**Plex tunes a channel but the guide is empty.** The XMLTV URL is separate from the device address — add `http://<pvarr-host>:8999/live/epg.xml` as the guide, then run a channel scan.

**Force Failover is greyed out, or returns "No backup stream to fail over to".** That session was started with a single URL. Failover moves to the *next* candidate, so with nothing to move to the request is refused rather than honoured — honouring it would end the recording. Add a backup URL when starting the recording.

**Recordings stopped dead after six to eight minutes (versions before 0.1.2).** FFmpeg's progress output filled its error pipe, which PVArr never drained; FFmpeg then blocked writing to it and stopped producing video, and the stall was not detected — so the recording simply ended early with no error. Measured on a live capture: FFmpeg writes ~184 bytes/sec to that 64KB pipe, and the pre-fix recorder stopped writing video at 7m45s. The exact point varies a little with stream bitrate. Fixed — the pipe is drained continuously and the progress output is switched off at the source. Upgrade if you are seeing this.

**A dead stream was not failed over (versions before 0.1.2).** Freeze detection could not fire while the recorder was waiting on a full read buffer, so a source that went quiet without dropping the connection hung instead of failing over. Fixed.

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

211 tests covering filename sanitisation and collision handling, storage
operations, M3U/XMLTV generation, dependency resolution, the failover state
machine, freeze detection, stream-completion ordering, FFmpeg command
construction, and every HTTP route.

Most spawn no subprocesses — the recorder tests drive the real loop against
scripted fakes. The capture-loop tests run over a real OS pipe, because the
reader selects on a file descriptor and a fake `read()` would not exercise it.
Two tests do a real remux by encoding a one-second transport stream and skip
when FFmpeg is absent; the route tests skip when `httpx` is absent, so the core
suite still runs with only `requirements.txt` installed.

### Syntax checks

```bash
python3 -m py_compile app/*.py stream-recorder.py test_pvarr.py
bash -n start.sh scripts/publish.sh
```

### Releasing

`scripts/publish.sh` commits the tree and publishes the container image in one
step. The image version is `__version__` in `app/__init__.py`:

```bash
scripts/publish.sh --bump patch    # 1.0.0 -> 1.0.1, commit, build, push
scripts/publish.sh --version 2.0.0 # set an explicit version
scripts/publish.sh --skip-docker   # commit only, touch no registry
```

An already-published version tag is never overwritten silently — the script
checks the registry *before* committing and refuses, so a rejected publish
leaves no commit behind. Pass `--force` to overwrite deliberately. Set
`PVARR_IMAGE` to publish a fork somewhere else.

Pushing requires a token with `write:packages`:

```bash
echo $YOUR_TOKEN | docker login ghcr.io -u YOUR_USERNAME --password-stdin
```

**CI builds an image for version tags only.** Ordinary commits to `main` do not
produce an image, so day-to-day work can be committed and pushed freely without
changing what `docker compose pull` gives anyone:

| Trigger | Tags published |
| --- | --- |
| `git push origin main` | *none* — commits never build an image |
| version tag pushed (`v1.0.1`) | `:1.0.1`, `:latest`, `:sha-<short>` |
| manual `workflow_dispatch` | `:sha-<short>` only — deliberately does not move `:latest` |
| `scripts/publish.sh` (builds locally) | `:<version>`, `:latest` |

`:latest` therefore always means *the newest tagged release*, never the newest
commit. Pin `:<version>` in production.

A version tag must match `__version__` in `app/__init__.py` or the workflow
fails rather than publishing a mislabelled image, and the full test suite runs
inside the publish workflow before anything is built.

Cutting a release:

```bash
scripts/publish.sh --bump patch --skip-docker   # set __version__, commit
git push origin main
git tag v1.0.1 && git push origin v1.0.1        # this is what builds the image
```

---

## License

[The Unlicense](LICENSE) — public domain. Do whatever you want with it; no attribution required.

---

## AI Genesis & Environmental Footprint

This program was written by machines. Gemini drew the first architecture; Claude has held the pen since — architecture, code, tests, and the audits that keep finding things wrong with all three — and a human points, decides, and reviews. Stated plainly, because the industry mostly doesn't.

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
