# Changelog

All notable changes to PVArr are listed here, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style. Earlier history is summarised in the README and TODO.

## [Unreleased]

## [0.11.1] - 2026-10-25

### Added
- **Organise finished recordings** (`PVARR_LIBRARY_DIR`, off by default): after the remux a recording moves to `<dir>/<Sport>/Season <year>/<Sport> - S<year>E<NN> - <A> vs <B>.mp4`, a layout Plex reads as a show per sport. The episode number is found by scanning the season folder, so hand edits cannot desync it. A library on a separate mount is refused rather than copied; a failed move leaves the file in `recordings/` with the reason in the recording's log. The library listing now covers both folders and each row carries its folder.

### Fixed
- A live stream whose playlist serves MPEG-TS segments under a non-video extension (`.image` URLs with byte ranges) was refused in direct mode ("not in allowed_segment_extensions"), so the recording stayed at 0 bytes while it cycled through every candidate. The extension relaxation now applies to direct mode as well as the local proxy path.
- The fallback proxy now works for streams whose segments live on a different host than the playlist, and for playlists that pack segments into one file with byte ranges. Previously it refused those segments (403 "Unknown upstream host") and ignored the byte range, so every segment failed. The pinned proxy is patched at image build (`scripts/patch-hls-proxy.py`); the build fails if the patch stops matching.

### Changed
- Finishing a recording is one code path for a normal stop and for a recording finalised at boot. The boot path now also warns about lost segments and runs commercial detection, and a crash between the remux and the announcement no longer leaves an `.mp4` that is never announced or organised.
- Library delete, rename and download now refuse symbolic links, and a file is protected while it is being remuxed, organised or scanned for ads, not only while it records.
- A session file whose output path is outside the permitted folders is dropped at boot instead of finalised.
- Team and sport names in filenames are capped at 60 characters.
- The commercial-detection cut backup is now hidden (`.<name>.original.mp4`), so Plex and the library list do not mistake it for a second copy.

## [0.11.0] - 2026-10-20

### Added
- **Events** tab (and header button): save the listing pages you use (on the server, so every device sees them), list a page's games grouped live / by day, tick several and schedule them all at once, each starting 5, 10 or 15 min before its listed time and ending after the league's usual length. Already-scheduled, untimed and finished games are marked and cannot be ticked. API: `GET/POST /api/event-lists`, `DELETE /api/event-lists/{id}`.
- A recording started from an event page re-checks it when two of its sources die, and swaps fresh working links into the dead slots (never the one recording), skipping the links it already holds. Automatically at most once per 10 minutes, never delaying a scheduled start; also on demand with **Re-check event page** on the card (`POST /api/recordings/{id}/rescan`).
- **Find 3 streams** ranks what it finds: up to six working streams are measured (real resolution, frame rate, bitrate, download speed against real time) and the best three fill the slots, best first; a stream that cannot keep up ranks last. Backups are taken from different real sources where possible: links that relay the same origin feed through different edge servers count as one source, since they fail together. `PVARR_RANK_STREAMS=0` keeps the old page order.
- **Find 3 streams**, a scheduled start, and a mid-recording re-check log one line naming each picked stream's host and what it measured (`Find picks: #1 … 720p · 1.8 Mbps · 14.5x real-time; …`), so an unattended recording shows why it chose its streams. A backup that relays the same feed as an earlier pick is marked `[same source as #N]`, with the number of distinct sources at the end.

### Changed
- **Find 3 streams** probes up to 40 links on an event page, up from 20. It stops once it has enough working ones: three, or up to six to rank when ranking is on. If those six all play one feed (several sites relaying one origin), it keeps probing, up to ten working links, for one that plays a different feed, so a backup can come from it; only that extra link is measured.

### Fixed
- A listing page on the internet could name a game link into the local network, and scheduling it made PVArr fetch it and trust private streams there. Such links are now left out (a listing page on your own network keeps them).
- At phone width the header pushed the page sideways; the buttons now show just their icons there.
- A recording was finalised mid-event after its third restart in total, however long it had run fine since the last one: `PVARR_MAX_RESUME_ATTEMPTS` counted for the recording's whole life. Now a resume that keeps recording for 10 minutes starts the count over, so only a real restart loop is stopped.
- A re-check that found working links but had no failed source to replace said "nothing new to swap in". It now says how many it found and that no source has failed.
- **Force Failover** stayed greyed out once a recording reached its last source, although the server wraps back to the first. It is now disabled only for a single-source recording.

## [0.10.0]

### Added
- `GET /api/health`: 503 when a session says "recording" but has written nothing for `PVARR_WEDGE_SEC` (default 300 s). The Docker and compose healthchecks use it instead of `/api/status`, which stayed 200 for a wedged session.
- **Read game** on the event page box: fills in Sport, both teams, Start at and End at from what the page says about the game (schema.org event data, "A vs B" text, a machine-readable start time, a LIVE label), or lists the games on a page that links to many so one can be picked and scheduled (`POST /api/event-info`). **Find 3 streams** also fills in Sport and the teams.

### Changed
- A rebroadcast channel keeps retrying a dead upstream (with the 60 s backoff cap) until it is stopped, instead of ending itself after `max_cycles` laps.
- hls-proxy is started with `HLS_PROXY_BIND=127.0.0.1` explicitly, so an inherited environment variable cannot expose it.

Full notes: [docs/release-notes/v0.10.0.md](docs/release-notes/v0.10.0.md).

## [0.9.0]

### Added
- Each comskip run appends the recording's length, ad time and ad share to `/config/comskip-stats.csv`.
- **Ad Stats** dashboard tab: share of recorded time that is ads, overall, by league and per recording (`GET /api/stats/ads`).

Full notes: [docs/release-notes/v0.9.0.md](docs/release-notes/v0.9.0.md).

## [0.8.3]

### Fixed
- A recording with an end time no longer stops early when a stream ends itself mid-event (FFmpeg exits cleanly on a flaky upstream). Before the end time that now counts as a drop and fails over.

Full notes: [docs/release-notes/v0.8.3.md](docs/release-notes/v0.8.3.md).

## [0.8.1]

First public release.

### Added
- `TZ` passed through in `docker-compose.yml`, so recordings are dated in local time.
- Issue and pull request templates, `SECURITY.md`.

### Changed
- The optional hls-restream-proxy dependency is pinned, and a failed download now fails the image build.
- README rewritten for newcomers; reference material moved to `docs/`.

Full notes: [docs/release-notes/v0.8.1.md](docs/release-notes/v0.8.1.md).

## [0.8.0]

### Added
- Scheduling a recording from an event page.
- `PVARR_FLARESOLVERR_TIMEOUT` environment variable and clearer FlareSolverr messages.

### Changed
- Failover to the next source happens in about 20 seconds.
- A scheduled job leaves the list as soon as its recording starts.

Full notes: [docs/release-notes/v0.8.0.md](docs/release-notes/v0.8.0.md).
