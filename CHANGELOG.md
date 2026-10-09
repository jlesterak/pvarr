# Changelog

All notable changes to PVArr are listed here, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style. Earlier history is summarised in the README and TODO.

## [0.8.0]

### Added
- Scheduling a recording from an event page.
- `PVARR_FLARESOLVERR_TIMEOUT` environment variable and clearer FlareSolverr messages.
- `TZ` passed through in `docker-compose.yml`.

### Changed
- Failover to the next source happens in about 20 seconds.
- A scheduled job leaves the list as soon as its recording starts.
- The optional hls-restream-proxy dependency is pinned.

Full notes: [docs/release-notes/v0.8.0.md](docs/release-notes/v0.8.0.md).
