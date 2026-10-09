# Development

[Back to the README](../README.md)

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

`recorder.py` holds the core loop: it walks the candidate URL list, prefers a direct FFmpeg connection, drops to the proxy bridge when a candidate will not connect directly (or freezes with no backup to move to), and advances to the next candidate on stall, failure, or a forced failover. Before each connection it calls `probe.py` to re-resolve that candidate — playlist URLs carry short-lived tokens, so a failover an hour in needs a fresh answer rather than the one the dashboard found at submit time.

## Failover and the file's timeline

Every failover starts a **fresh FFmpeg process**, and FFmpeg normalises whatever
it reads to its own zero — a source whose own clock says one hour still comes
out starting at ~1.4 seconds. Those bytes are appended to the same `.ts`, so
without correction the file's clock walks *backwards* at every switch.

That is what a player trips over. mpv logs `Invalid audio PTS ... Reset playback
due to audio timestamp reset` and restarts its clock mid-stream, `--start=` and
seeking land in the wrong place, and `ffprobe` reports only the length of the
first segment — a three-hour game with two failovers claiming to be forty
minutes long.

PVArr now reads the last timestamp back out of the bytes it just wrote and hands
the next FFmpeg an `-output_ts_offset`, so each segment continues the timeline
instead of restarting it. It applies to every splice: a candidate switch, the
proxy-bridge retry, and a **resume after a restart**, where the offset is
recovered from the tail of the existing file rather than starting at zero.

Two things to expect, both harmless:

- **A gap of a second or two at each switch.** Real footage that did not arrive
  while PVArr reconnected. The timeline steps forward over it rather than
  pretending it was continuous.
- **mpv may still print `Invalid audio PTS: 10.03 -> 11.38` at a splice.** That
  is mpv noting the forward step and carrying on. The line to worry about is
  `Reset playback due to audio timestamp reset`, which should no longer appear.

Finished recordings were never affected: `_on_complete` remuxes to `.mp4`, and
that pass re-times the splice regardless. The bug only ever showed up in the raw
`.ts` — which is exactly what the live tuner feed serves, and what you get if a
remux fails.

### Refreshing the team list

The teams behind autocomplete live in `app/data/teams.json`, committed to the
repo and shipped in the image; PVArr never fetches them at runtime. To pick up
new teams or a renamed club, run on a machine with internet access:

```bash
python3 -m app.tags --refresh
```

It asks ESPN's public JSON API (free, no key) for each league in
`app/tags.py`'s `LEAGUES` and rewrites the file. ESPN's API is unofficial and
could change; a league that fails to refresh keeps its previous teams rather
than being emptied, and the command prints which. Commit the result. Fan
nicknames (`avs`, `habs`, `niners`) are a short hand-kept list in
`TEAM_ALIASES`, not in the snapshot, so a refresh never erases them.

### Tests

```bash
pip install -r requirements-dev.txt   # adds httpx, needed for route tests
python3 test_pvarr.py                 # full suite, verbose
python3 -m unittest discover          # quiet
```

About 700 tests covering filename sanitisation and collision handling, storage
operations, M3U/XMLTV generation, dependency resolution, the failover state
machine, cycling failover and manual candidate switching, freeze detection,
stream-completion ordering, the disk-space guard, library listing across
containers, FFmpeg command construction, proxy credential cleanup, recording
windows and the duration backstop, log and notification redaction, the
probe attempt trace, yt-dlp resolution, commercial detection, and every
HTTP route.

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
