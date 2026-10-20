# Changelog

All notable changes to PVArr are listed here, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style. Earlier history is summarised in the README and TODO.

## [Unreleased]

### Added
- A recording started from an event page re-checks it when two of its sources die, and swaps fresh working links into the dead slots (never the one recording), skipping the links it already holds. Automatically at most once per 10 minutes, never delaying a scheduled start; also on demand with **Re-check event page** on the card (`POST /api/recordings/{id}/rescan`).
- **Find 3 streams** ranks what it finds: up to six working streams are measured (real resolution, frame rate, bitrate, download speed against real time) and the best three fill the slots, best first; a stream that cannot keep up ranks last. Backups are taken from different real sources where possible: links that relay the same origin feed through different edge servers count as one source, since they fail together. `PVARR_RANK_STREAMS=0` keeps the old page order.
- **Find 3 streams**, a scheduled start, and a mid-recording re-check log one line naming each picked stream's host and what it measured (`Find picks: #1 … 720p · 1.8 Mbps · 14.5x real-time; …`), so an unattended recording shows why it chose its streams.

### Changed
- **Find 3 streams** probes up to 40 links on an event page, up from 20. It stops once it has enough working ones: three, or up to six to rank when ranking is on. If those six all play one feed (several sites relaying one origin), it keeps probing, up to ten working links, for one that plays a different feed, so a backup can come from it; only that extra link is measured.

### Fixed
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
