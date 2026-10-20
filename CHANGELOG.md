# Changelog

All notable changes to PVArr are listed here, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style. Earlier history is summarised in the README and TODO.

## [Unreleased]

## [0.10.0]

### Added
- `GET /api/health`: 503 when a session says "recording" but has written nothing for `PVARR_WEDGE_SEC` (default 300 s). The Docker and compose healthchecks use it instead of `/api/status`, which stayed 200 for a wedged session.
- **Find 3 streams** ranks what it finds: up to six working streams are measured (real resolution, frame rate, bitrate, download speed against real time) and the best three fill the slots, best first; a stream that cannot keep up ranks last. Backups are taken from different real sources where possible: links that relay the same origin feed through different edge servers count as one source, since they fail together. `PVARR_RANK_STREAMS=0` keeps the old page order.
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
