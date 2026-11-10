# Changelog

All notable changes to PVArr are listed here, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) style. Earlier history is summarised in the README and TODO.

## [Unreleased]

### Added
- Browser sidecar, nothing recording: a scan looks at up to 12 unresolved links, two pages at a time, within 3 min (during a recording it stays at 4 links, one page, 45 s).
- Low-disk warning for scheduled recordings: a waiting job shows an amber **low disk** line (and `disk_warning` in `GET`/`POST /api/schedules`) when its length at an assumed 6 Mbit/s, plus the scheduled recordings before it and the time left on running ones, plus `PVARR_MIN_FREE_GB`, exceeds the free space; one notification per job from 20 minutes before the start. Warning only: nothing is deleted or refused.
- The browser's results are remembered per link host in `browser-hosts.json` (config folder), and the likeliest hosts are asked first; hosts that keep missing go last.

### Changed
- Browser sidecar: a request to a host its DNS server answers `0.0.0.0` for (a network-wide blocklist) is still refused, but a miss now says "blocked by DNS" and how many hosts, instead of "private addresses"; a page the sidecar refuses reports the reason on the card instead of "HTTP 400". docs/browser-sidecar.md explains giving the sidecar another DNS server.

## [0.19.0] - 2026-11-09

### Added
- Time without data on the recording card, in the finish log line and in the finish notification (from one minute): every gap of more than 12 s in which the file got nothing new, across failovers, so a recording that keeps freezing no longer reads "0 lost".
- Stall measurement: an attempt that saw stalls of 8 s or more logs their lengths when PVArr leaves the source, and a recording ends with one line per source (count, median, longest). Measurement only; the freeze timer is unchanged.
- A backup whose playlist sits on the same provider host and path as the stream being recorded but carries a different event number is skipped and marked wrong game on its own. The backup check and the event-page re-check refuse it too. Dates and timestamps in a path never count as event numbers.
- The Referer a feed last delivered data with is remembered for the rest of the recording and reused when a later connect to the same feed finds none. A Referer the probe chose is never replaced.

## [0.18.0] - 2026-11-08

### Added
- Optional browser sidecar (`ghcr.io/jlesterak/pvarr-browser`, docs/browser-sidecar.md): a separate container that opens a page in headless Chromium and reports the playlist its player requested. With `PVARR_BROWSER_URL` set, PVArr sends it links whose page loads but holds no playlist: in Find, scheduled scans and the event-page re-check when the probe came up short (up to 4 links, one at a time, 45 s at most), and on connect. Every playlist it returns is checked by PVArr's own probe. Unset, nothing changes.
- `PVARR_BROWSER_TIMEOUT` (default 30 s, 10-60): what the sidecar may spend on one page.

## [0.17.0] - 2026-11-07

### Added
- Segments packed inside WebP images (a short WebP header followed by MPEG-TS) are recognised by the probe and unwrapped by the relay; no decode needed.
- The mid-recording event-page re-check logs the links that gave no playlist (up to 20) on the recording's card.

## [0.16.0] - 2026-11-07

### Added
- Step down on a slow connection: four segment losses within five minutes that are timeouts (not HTTP errors from the source) switch to a lower quality of the same playlist, or to an alive backup that is a different feed at no more than 75% of the bitrate. Back up after 20 quiet minutes; stays down after a relapse within 10 minutes; at most three step-downs per recording.

### Changed
- The break-card pass finds cards by repeated pictures (a keyframe matching one at least five minutes away, held for 45 s) instead of quiet audio plus a missing score overlay. One FFmpeg pass instead of two; adds numpy to the image (~70 MB).

## [0.15.1] - 2026-11-05

### Added
- The recording card counts the real sources behind its links (mirrors of one feed count once) and turns the count amber when every backup is the same feed.
- Backups are checked every 90 s while another source records (a page and playlist fetch, no video), with a green or red dot on each badge. Failover goes to the next backup that answered, and falls back to plain order if none did; a backup that did not answer is re-checked every 3 minutes.
- The card shows the stream rate (bandwidth the recording uses, over the last minute). Three or more lost segments within 5 minutes send one notification (at most every 15 minutes) naming that rate.

## [0.15.0] - 2026-11-05

### Added
- **Wrong Game** button on a running recording (`POST /api/recordings/{id}/failover` with `wrong_game=true`). It marks the stream being recorded as the wrong game: PVArr leaves it at once and never connects to it again in that recording, under any link that plays the same feed (mirrors on other edge hosts included). Marked sources count as failed, so the event-page re-check refills their slots but never swaps the same feed back in; the mark survives a restart. Allowed on a single-URL session, which then keeps retrying until its window closes.

## [0.14.2] - 2026-11-04

### Fixed
- A lap in which any candidate recorded data no longer counts as fruitless, so "No data from any candidate" and the giving-up path only follow laps that really got nothing.
- The "FFmpeg said" line on a freeze (and a candidate's `last_error`) shows only FFmpeg output from the last 60 s; with none, the newest line is shown with its age.
- The event-page re-check probes each fresh link before it replaces a failed source, so a link with nothing playable no longer pushes out one that was working.
- The chapter remux keeps the recording's file permissions and is skipped (with a warning) unless the volume has the file's size plus 5 GB free.
- At boot, stale sessions are finished in the background after the server is up, and a clean restart no longer spends a recording's resume allowance.

## [0.14.1] - 2026-11-02

### Fixed
- A player page passing a protocol-relative URL through base64 (`atob("Ly9...")`) was not resolved: only base64 starting with "http" was decoded.
- A college game read from an event page could get the wrong league when both schools play several sports: an unrecognised label ("NCAAB") let the first shared league (football) win. "NCAAB", "CBB" and "NCAA basketball" now name men's basketball, a league word in the page's URL counts as a hint, and a sport word ("Basketball") narrows the choice to that sport.

## [0.14.0] - 2026-11-02

### Added
- Segments packed inside PNG images (video carried as a gzip of MPEG-TS in the pixels) are recognised and recorded: the probe and quality check unwrap them, and the loopback relay hands FFmpeg the MPEG-TS, fetching new segments as soon as the playlist lists them. The image is decoded by the bundled FFmpeg.
- Playlists delivered as a JSON segment list (`segments` with `url` and `duration`) are found next to an `.m3u8` in a player page and served to FFmpeg as m3u8 by the relay.
- A page that answers 401/403 to the probe's ordinary client is fetched again with the browser TLS profile, and its embeds are walked with it.
- When a player lists several playlists, the probe keeps the first whose segment check passes rather than the first that loads.
- `/api/probe` and `/api/status` add `wrapped` / `png_wrapped` and `json_playlist`.

### Fixed
- The single-URL probe could call a stream whose segments are plain images "resolved"; it now fails with "segments are images, not video".
- Mirrors whose playlist URLs differ only by a long token in the path now share a source letter.
- The relay refuses non-http(s) URIs and keeps serving the last good playlist through a brief 5xx/timeout instead of ending FFmpeg's run.

## [0.13.0] - 2026-11-01

### Added
- Extend or edit a running recording's end: **+15m / +30m / +1h / Edit** on the card, `POST /api/recordings/{id}/end` (`add_minutes` from the current end, or an exact `end_time`). Saved, so a resume keeps it.
- Events tab: **Keep 0/15/30/60 min after**, added to each scheduled game's end.
- A schedule that found no stream shows **not listed yet**, **blocked**, **page error** or **no stream** instead of *waiting*, and lists up to five links a manual step could rescue, with a hint and **Record manually**. `/api/schedules` adds `miss_kind` and `fixable`; `/api/aggregate` adds `fixable` per link.

## [0.12.0] - 2026-10-28

### Added
- Event-page checks count every link by status (working, not video, failed, unresolved, not checked) and group working links into real sources: mirrors of one feed share a letter. Shown in the result line, the link list, the re-check log and `/api/aggregate` (`summary`; `status` and `source` per link).
- The recording card shows how many real sources stand behind its candidates and a source letter on each; `/api/status` adds `source` per candidate and `distinct_sources`/`unknown_sources` per session.

### Fixed
- A link whose segments are not video (images, HTML) counted as working and could be picked as the primary; it is now excluded and listed as *not video*.
- Quality measurement ignored EXT-X-BYTERANGE and fetched whole files, so sources packing segments inside a non-video file were picked unmeasured; it now requests the range.

## [0.11.2] - 2026-10-26

### Changed
- New theme: warm wood, parchment and brass instead of blue (status colours keep their meanings).

### Fixed
- Plex showed every organised recording as "Episode 1" (or a date) when the Sports library uses the **Personal Media Shows** agent, which takes episode titles only from the file's title tag, never the filename. The remux now writes `<Team A> vs <Team B>` as the title tag, and writing comskip chapters keeps the file's tags instead of replacing them with an empty set.
- A source whose host this network's DNS blocklist answers with `0.0.0.0` / `::` (Pi-hole, AdGuard and similar) is now recognised as **blocked by DNS** rather than reported as a private address. The recorder fails over immediately, without the yt-dlp, header-detection and proxy retries that would ask the same blocked host, counts it as dead so the event page is re-checked for another source right away (one such source is enough; ordinary failures still wait for two), and a Find or re-check skips such links with the reason shown. Connecting to `0.0.0.0` reaches this very machine, so a blocked host is never tried.
- The fallback proxy's new segment-host learning (0.11.1) refused only private IP *literals*; `localhost`, alternate spellings such as `127.1` or `2130706433`, and LAN hostnames were still learned. It now resolves each host and learns it only when every address is public.

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
