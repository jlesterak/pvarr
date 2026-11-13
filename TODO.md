# PVArr - Implementation TODO

The project's working log: what was built, why, how it was proven, and what is
still open. Newest phases are at the bottom.

Sites are never named here. Upstreams are "provider A/B/C/D", pages that list
several stream links are "event pages" (`app/aggregator.py` in the code), and
recorded broadcasts are "network A/B". Site-specific field notes live in a
separate private notes repo and must not be copied into this one, its commit
messages, tests or screenshots. See README "Scope".

Everything finished is in the private notes log (phases 1-33, through v0.11.0) and the release notes (`docs/release-notes/`, `CHANGELOG.md`). Only open work and standing decisions stay here.

## Decisions (closed, not open work)
- **Quantified AI footprint figures — will not add.** The build was never
  instrumented, and providers do not publish per-token energy for frontier
  models. Any gram-of-CO₂e or litre-of-water figure produced now would be
  fabricated — exactly the failure mode a transparency section exists to
  avoid. Reopen only if real provider usage data becomes available.
- **Footprint section rewritten in an anarchist/luddite register** with
  carbon-offset donation links (Cool Earth, Clean Air Task Force, Wren —
  all three URLs verified live). The "we don't know and they won't say"
  position is unchanged; only the voice moved.
- **Authentication: accepted risk, will not add.** Deployment is a trusted LAN
  behind a firewall. Documented prominently in the README instead. Revisit only
  if this is ever exposed — note that adding Basic auth would break the Plex and
  Emby tuner fetches unless `/live/*` is exempted or given a token parameter.

## CI runners (2026-11-04)
- [COMPLETED] CI stays on `ubuntu-latest` (sponsor, 2026-11-07): pinning means a bump every runner generation; the jobs only need Python and Docker, so a new Ubuntu is low risk and a failure shows on the next push. Moved `actions/checkout` to v5 and `actions/setup-python` to v6 (Node 24), which clears the Node 20 deprecation warning.

## Wrong-game mark (2026-11-05, released in v0.15.0)
- [COMPLETED] **Wrong Game** button on a running recording (`POST /failover` with `wrong_game=true`). Symptom: a
  live recording captured the wrong game for ~40 min. Both backups and the only link a restart found were the same
  feed (one `quality.source_key`), so a manual switch and the event-page re-check kept landing on it. The mark is per
  feed: the loop skips any candidate resolving to it (like a DNS-blocked one, counted dead so a re-check refills the
  slot), `fresh_link_plays` refuses it, and `wrong_sources` is saved in the session record for resume. Proven by 5
  tests (`TestWrongGame`, plus the `/failover` route saving or refusing the mark) and a live run on a dev instance: marked on candidate 1, candidate 2 (a mirror on
  another edge host) was skipped, candidate 3 recorded, and the mark was in the saved session.

## Roadmap to v1.0 (sponsor-approved 2026-11-09; replaces the 2026-11-05 roadmap)
Re-ranked after five game nights, three daily link checks and three measurement
nights for the browser sidecar. What they showed:
- Within one week six link families that the plain probe could read began to need
  the browser, and one event page had 21 links with none readable without it. The
  sidecar's limit (4 links, 45 s, one page at a time) is now what holds discovery
  back; a miss runs about 33 s, so two misses use the whole budget.
- Freezes cost more than lost segments: 6-20 freeze failovers a game at about 25 s
  each, against 4-25 lost segments.
- A wrong game was recorded on three of five nights.
- The recording volume was close to full before four of five games.

**v1.0 means hands-off on the sponsor's test host** (sponsor, 2026-11-09):
1. Follow a team works end to end: a followed team's game is scheduled, resolved,
   recorded and ended without anyone touching it.
2. Ten scheduled games in a row, each with at least 97% of the programme present
   (time without data plus lost segments at most 3% of the scheduled length), no
   wrong-game minutes and no manual switch, mark, restart or hand-captured link. A
   miss resets the count and gets an entry here.
3. Names frozen: env vars, API routes (`docs/api.md`), session and schedule file
   formats. The README says what v1.0 promises.
4. No open item where data was lost for an unknown reason.

Done from the earlier roadmap:

- [COMPLETED] **Quick wins** (2026-11-05, released in v0.15.1). "N real sources" badge (amber when every backup is
  one feed); standby checks probe each backup every 90 s on a copy (no segment fetch) and
  failover takes the next one that answered, plain order if none did; stream rate over
  ~60 s on the card; 3+ lost segments in 5 min sends one notification per 15 min. Proven by
  6 tests (`TestStandbyAndAlerts`) and a dev-instance run: a backup whose playlist was
  removed read dead within one check, and a forced failover from candidate 2 skipped it.
- [COMPLETED] **Browser resolver sidecar** (optional `pvarr-browser` container,
  Playwright Chromium + ad blocker, `POST /resolve` -> playlist + headers). Used by
  `detect_candidate_headers()` and by Find for links the probe cannot resolve.
  Would supersede `detect-headers` and most FlareSolverr use. Prototype and measure
  first (target: >=70% of JavaScript-player links resolved); Architecture + Security
  review before merge; isolated like FlareSolverr (own network, no LAN egress).
  Detailed plan kept in the private notes; first step is measuring a link corpus.
  Shipped in v0.18.0 (2026-11-08). Go/no-go closed 2026-11-09: over three game nights it
  found 2, 1 and 3 new recordable sources, resolved links took a median of 6-10 s, and no
  page peaked over 1 GB. All three criteria passed on every night.

Open, in order (each version still needs the sponsor's approval to ship):

- [COMPLETED] **v0.20: discovery that keeps up.** Released in v0.20.0 (2026-11-12).
  - [COMPLETED 2026-11-09] Why a miss takes about 33 s, measured. It is the sidecar's
    own schedule (an 8 s first wait, then three nudge rounds of about 8 s), the same
    for every page. All 19 hits logged over three nights came within 13.4 s, after
    no nudge or one. So there is no cheap early exit worth building; the gain is in
    what gets asked and in which order (the budget and ordering items below).
  - [COMPLETED 2026-11-09] Found by that measurement: on the test host 4 of 7 misses
    were not misses at all. The sidecar's holder container resolved names through
    the network's filtering DNS server, which answers `0.0.0.0` for some stream
    hosts, and the sidecar refused those requests as private addresses (PVArr itself
    had been given another DNS server weeks earlier; the holder, added later, had
    not). With a `dns:` entry on the holder the same 12 links went from 5 resolved
    to 9, the four former misses in 6-8 s. Fix in the software: such a request is
    still refused but counted apart, a miss says "N request(s) to M host(s) blocked
    by DNS: ... give it another one", and a page the sidecar refuses reports the
    reason on the card instead of "HTTP 400". Docs: `docs/browser-sidecar.md` "DNS",
    `docs/troubleshooting.md`. Proof: `test_a_host_the_dns_server_answers_zero_for_is_reported_as_dns_blocked`,
    `test_a_miss_caused_by_a_dns_blocklist_says_so` (real Chromium), `test_a_refused_page_says_why`.
    The three sidecar measurement nights ran with this handicap, so its real yield
    is higher than the go/no-go figures.
  - [COMPLETED 2026-11-10] Sidecar budget by situation. Before a game (a scheduled check, or Find with
    nothing recording): every unresolved link up to 12, two pages at a time, 3 min
    in all. During a recording: as today (4 links, 45 s, one page). Two pages
    measured up to 1.8 GB, inside the documented 2 GB limit. Proof: `test_idle_find_looks_at_more_pages_two_at_a_time`,
    `test_the_wide_browser_look_is_off_while_recording_or_for_a_scan_override`; independent validator ran edge cases (abort, raising resolver, concurrency cap 2, deadline).
  - [COMPLETED 2026-11-10] Likeliest link first: remember per link host whether the browser resolved it
    (count, last time) in a small file in the config folder and try those first.
    Learned from the operator's own results; nothing is shipped with PVArr.
    Proof: `test_the_browser_is_asked_about_the_likeliest_host_first`, `test_host_memory_survives_a_restart_and_never_raises` (hostile files ignored, validator found a deeply nested file escaped the loader; fixed).
  - [COMPLETED 2026-11-10, design review 2026-11-10 and 2026-11-11] Team-name
    check (warning only): the page title of a link (from the probe, or the browser
    sidecar when it resolved the link) is compared with the recording's two teams
    at connect and on the backup check. "ok" when it names either team (a word of
    its name, city or nickname, or its abbreviation in capitals); "other" when it
    names a game (vs/@, or a full team name from the bundled table) and neither;
    otherwise no opinion. "other" marks the source (amber on the card), logs one
    line and, at connect, notifies once per recording. It changes nothing else.
    Known limits: a title sharing a city with a scheduled team passes; nicknames
    joined by a dash alone give no opinion; a generic title with " @ " or " v "
    can be flagged; a resume after a restart can notify once more; links resolved
    by yt-dlp carry no title. Shown titles have every numeral removed.
    Proof: `TestTeamNameCheck`, `TestProbeReportsThePageTitle`, and a dev-instance
    run against a loopback stream with synthetic pages. Not yet seen on a live game.
  - [COMPLETED 2026-11-10] Disk warning before the game: a waiting job's length at an
    assumed 6 Mbit/s (no stream is known yet), plus the jobs that start before it
    and the time left on running recordings on the same volume, plus
    `PVARR_MIN_FREE_GB`, against free space. Read on every list refresh (amber
    line on the job, `disk_warning` in the API, so it shows at creation and clears
    when space is freed); one notification per job from 20 min before the start.
    Deletes and refuses nothing. Proof: `TestScheduleDiskWarning` (the sum,
    earlier jobs and a running recording counted, an unreadable volume, one
    notification that survives a restart, a volume that does not answer).
  - [COMPLETED 2026-11-11] Design review of the whole version (Architect, Security,
    DevOps), two passes. What it changed, each with a test:
    - A page title could still show a score through characters that only look
      like numbers (superscripts, circled and Roman numerals, then date and unit
      signs): the shown title now drops every character that is a numeral or
      normalises to one. Checked against every Unicode code point.
      `test_a_shown_title_carries_no_numeral_of_any_kind_and_no_format_characters`.
      `POST /api/probe` no longer returns the title.
    - The disk read ran on the request path: a network mount that had gone away
      would have frozen the dashboard and the scheduler, and after a first fix
      could still have held a container stop. It now runs on a daemon thread, one
      at a time, 2 s at most for the caller that started it, none for the rest.
      `test_a_volume_that_does_not_answer_costs_two_seconds_not_the_server`.
    - The wide browser look could hold a scheduled start back by minutes when
      working links were already in hand (all one feed): then it keeps the 45 s
      limit. `test_a_waiting_start_with_a_working_link_gets_the_short_look`.
    - The wide look used both sidecar pages even after a recording had started,
      and a one-page sidecar answered "busy" to half its links, which were then
      dropped: one page once something records, a busy link is asked again, a
      page gets no more time than the look has left, and a connect asks a busy
      sidecar once more. `test_the_wide_look_drops_to_one_page_once_a_recording_starts`,
      `test_a_busy_sidecar_costs_no_link_its_look`,
      `test_a_page_gets_no_more_time_than_the_look_has_left`,
      `test_a_busy_sidecar_is_asked_once_more_at_connect`.
    - A miss caused by a DNS blocklist counted against the link's host and kept
      it at the back of the queue after the DNS was fixed: no longer counted
      (in `test_host_memory_survives_a_restart_and_never_raises`).
    - A backup already marked by the 90 s check sent no notification when the
      recording moved to it. `test_a_backup_marked_by_the_check_notifies_when_the_recording_moves_to_it`.
    - Found by the independent validator: a marked backup whose connect got
      no title at all (the browser gave none in time) was recorded without the
      notification. A standing mark now notifies (same test).
    - Notifications were sent on the recording thread, between probing a link
      and starting FFmpeg: now on their own thread.
    - The sidecar's refusal reason is cleaned of control and format characters
      before it reaches the card. `test_a_refused_page_says_why`.
    - Accepted, not changed: the low-disk text says GB for what is GiB (as the
      `PVARR_MIN_FREE_GB` guard always has); the host memory is rewritten on
      every browser result (small file); the sidecar does not cancel a page
      PVArr has stopped waiting for (bounded by the trimmed page time).
  - [PENDING] Brittle test found while checking each commit: `TestFreezeFailover.test_a_freeze_moves_straight_to_the_backup`
    errors when run on its own (its fake FFmpeg launcher meets a command without `-i`), at v0.19.0 too, and
    failed once in a full run on a host under heavy disk load. It passes in a normal full run. Make it stand
    alone or drop it.
- [PENDING] **v0.21: follow a team.**
  - A follow list; every 30 min the saved event-list pages are read (as the Events
    tab does) and a followed team's game gets a schedule, once, with 30 min kept
    after the end by default.
  - Resolve 20 min before, record from 5 min before.
  - End on final (opt-in, after a design review): ask a public scoreboard for the
    game's status near the scheduled end and stop a few minutes after it reads
    final, so overtime is never cut. Status only: never score or clock, nothing
    stored, logged or shown. If the lookup fails, the scheduled end plus the kept
    time applies. This would be PVArr's first outside call at run time, so it stays
    off unless switched on and the README says so.
- [PENDING] **v0.22: fewer and visible gaps.**
  - Decide the freeze timer from at least three games of stall logs.
  - Measure where the ~25 s of a failover goes; if connecting dominates, reuse the
    playlist and headers the 90 s backup check already resolved.
  - **Gap map** per recording (where, how long, which source) on the Library card.
  - Refetch a failed segment from a mirror of the same feed (same `source_key`),
    only where PVArr fetches the segments itself (the relay); build it only if
    segment loss is still above about 1% of a programme after the items above. The
    other half of the old "fewer gaps" item, stepping down to a lighter level,
    shipped in v0.16.0 and still waits for its first field proof.
- [PENDING] **v0.23: release candidate.** The review follow-ups under "DNS-blocked
  sources" (`init: true`, healthcheck `start_period`, a rebroadcast channel that does
  not come back after a clean restart, a partial `.mp4` after a killed remux, comskip
  under `nice`); remove `detect-headers`, which the sidecar supersedes; prove that a
  restart mid-recording resolves its links again through the sidecar; freeze and
  document the names; README "How it differs"; close or archive the stale open
  items further down.
- [PENDING] **v1.0.0** when the four criteria hold. The ten-game count starts with
  the first game recorded on v0.21.

Not pursuing: stitching different broadcasts together, scorebug OCR, site-specific
code, DRM, a built-in player, transcoding, a bookmarklet (the sidecar covers it),
an arm64 image (until someone asks), sending every recording through the relay
(an architecture change for a small gain).

## Longer-term candidates
- A retention/cleanup policy for old recordings on disk. Not planned: the full
  volumes seen so far were other data on the host, which the disk warning in v0.20
  covers without deleting anything.
- Integration coverage for the notification webhooks (currently mocks only).

---

## Completed before 2026-10-28 (detail: CHANGELOG and `docs/release-notes/`)
- [COMPLETED] Library organisation, `PVARR_LIBRARY_DIR` (2026-10-24, v0.11.1). Season = calendar year (sponsor decision), so an NFL season splits across two folders. Not built on purpose: a library on a separate mount, .nfo sidecars, a sports-database lookup, retro-tagging, a category-folder setting. Plex indexing confirmed by the owner 2026-10-31.
- [COMPLETED] Direct-mode extension refusal and hls-proxy cross-host/Range fixes for byte-range `.image` segments (2026-10-25, v0.11.1; `scripts/patch-hls-proxy.py`).
- [COMPLETED] Episode titles missing in Plex: remux writes `<Team A> vs <Team B>` as the title tag and the chapter write keeps it (2026-10-26, v0.11.2; `test_title_tag_survives_remux_and_chapters`).

## DNS-blocked sources (fixed 2026-10-25, released in v0.11.2)
After a container recreate, a live source's segment CDN began resolving to
`0.0.0.0` (a DNS blocklist), so a playlist that had recorded fine for ~40 minutes
could no longer be followed; the probe called it a "private or loopback address"
and the recorder spent a full lap on yt-dlp, header detection and the proxy for
each candidate. Now classified as blocked by DNS, failed over at once, and the
event page is re-checked for another source. Proven live: one event-page Find
reported 14 of 36 links as blocked by DNS and recorded from the one that worked.
- [COMPLETED] Why it recorded for ~40 minutes first: neither local resolver
  logged a single query for the CDN before the container was recreated, so the
  old container resolved through a different (public) server. Not provable
  after the fact; the recreated one used the blocking resolvers. The sponsor's
  deployment now gives the container public DNS (documented in
  docs/troubleshooting.md).
- [COMPLETED] Boot finishing runs commercial detection inline and delayed the
  server coming up by ~7 minutes with two long recordings; resumed sessions
  waited behind it. Restarts also spend a session's resume allowance, so three
  restarts in an hour finalised a live recording instead of resuming it.
  - Fix (2026-11-04, released in v0.14.2): `resume_sessions` now resumes and discards first and returns the records to finalise; `_finalise_in_background` runs them one at a time on a daemon thread after the server is up (the same `_finalise_orphan` path: remux, organise, notify, commercial detection). Their files are marked as finishing at once, so delete and rename refuse them while they wait; the path check is repeated before each; a shutdown stops it between recordings, and an unfinished record stays on disk for the next boot. On shutdown `stop_all` first marks each still-capturing session `interrupted_by: "shutdown"` (saved before anything is killed); a resume of a marked session costs no attempt, up to 10 per recording, and the mark is cleared when it resumes. A crash or a SIGKILL before the mark still counts. Design review (Architect, Security, DevOps): go with changes; the marking-before-kill, the early file protection, the repeated path check and the cap came from it.
  - Proof: `test_boot_resumes_first_and_finalises_in_the_background`, `test_shutdown_marks_only_sessions_still_capturing`, `test_a_clean_restart_does_not_spend_a_resume_attempt`, `test_a_crash_still_counts_and_free_resumes_are_capped` (all fail on the old code).
- [PENDING] Follow-ups from that review, not done (each is a Docker/Compose or subprocess change for the sponsor to decide): `init: true` (or tini) so PID 1 reaps children; remux and comskip children are not in the process registry, so a shutdown leaves them to die with the container (outside a container they can outlive PVArr); run comskip under `nice`/`ionice` and cap concurrent remux/comskip runs; the compose file's healthcheck sets no `start_period` (the image's is 10 s), which matters less now the server comes up before finalising. Also seen: a rebroadcast channel's session is removed before the shutdown check in `_on_complete`, so a channel does not come back after a clean restart although `resume_decision` says it should; and a remux killed part-way leaves a partial `.mp4` the next attempt may name around.

## Real-source counts; non-video and byte-range segments (fixed 2026-10-28, released in v0.12.0)
- Symptom: a page check reported "3 working streams from 2 independent sources" while every recordable link on the page was one feed; the counts covered only the three picks. Its pick 1 was a playlist whose segments are real images (video drawn into the pixels, decoded by the site's player): FFmpeg found no stream in it, and the recording spent each failover cycling through it and its mirror. Separately, the byte-range item below.
- Byte ranges: `quality.measure` fetched the whole file for EXT-X-BYTERANGE segments packed inside a non-video file and saw the wrapper's header, so those sources were picked unmeasured. Now it requests the range (and slices it out when a server ignores Range).
- Fix: a link whose every sampled segment is not video is never picked (`status: not_video`). The page check reports every link's status and a source letter shared by mirrors of one feed, with counts in `summary` and the result line; a re-check logs the same for the rest of the page. `/api/status` gives each candidate its letter and the session `distinct_sources`/`unknown_sources`; the card shows them.
- Proof: `test_not_video_is_never_picked_and_the_page_is_summarised`, `test_measure_reads_the_video_at_the_byte_range` (Range honoured and ignored), `test_segments_that_are_all_images_are_not_video`, `test_byterange_segments_parse_with_and_without_offset`, `test_status_counts_real_sources_behind_the_candidates`. Mutation-checked: removing the exclusion, the Range header or the whole-file slice each fails a test.
- [COMPLETED 2026-11-02] The single-URL probe sniffed only bytes 0-2047 and could call an image-only stream "resolved". A PNG first segment is now fetched whole: video packed inside is unwrapped (next section), an image without video fails the check with "segments are images, not video". Byte ranges in the single-URL probe are still not sliced.

## PNG-wrapped segments and JSON segment-list playlists (built 2026-11-02, released in v0.14.0)
- Symptom: a live game recorded 0 bytes for 21 minutes ("Output file #0 does not contain any stream"). Its event page had 24 links and none recordable: the five that resolved were one feed whose segments are PNG images (the page check called two "working" and three "not video", by sampling luck); seven more, provider A's mirrors, answered 403 to every header combination; twelve build their player in JavaScript. Sponsor: "we used to easily get three, now one at best."
- Cause, measured on live segments: the images' pixels hold a short marker, a 4-byte length and a gzip of plain MPEG-TS; three decoded segments gave 15 s of clean 720p H.264/AAC. Provider A packs its segments the same way and now serves its playlist as a JSON segment list behind a tokenised URL in the player page; the `.m3u8` the page names refuses everyone.
- Built: `app/pngwrap.py` (unwrap by shape; the PNG decode is the bundled FFmpeg, argv list, stdin/stdout, 10 s timeout; size and ratio caps). `quality.measure` and `probe._check_segment` unwrap a PNG segment before judging it (`wrapped`). `probe.as_playlist` turns a JSON segment list into m3u8 (`json_playlist`); the probe tries up to two `.json` URLs found next to an `.m3u8`. `PlaylistRelay` serves converted playlists and, with `unwrap_segments`, serves each segment unwrapped, prefetching the newest four (two workers, eight cached, about 5 MB each). The recorder starts the relay for either and skips the hls-proxy retry, which can read neither. `quality.source_key` drops long token path segments, so a provider's mirrors share a source letter.
- Cost: wrapped streams pass all video through PVArr: ~4-8 Mbps on loopback, ~0.1 s per 5 s segment for the FFmpeg decode (desktop), ~40-70 MB of cached segments per recording. Nothing else changes.
- Proof: `TestPngWrappedSegments` (unwrap byte-for-byte through all five PNG filters; plain, damaged and non-TS images refused; measure and probe flag wrapped and refuse plain images; prefetch fetches the newest four before FFmpeg asks and serves from them; private segment refused; real FFmpeg records PNG-wrapped HLS through the relay) and `TestJsonSegmentList`. Mutation-checked: serving the image raw, or skipping the unwrap in measure, fails a test. Live on the same evening: the page check went from 0 recordable to 4 (one feed, 720p60); a provider A link probed as JSON + wrapped, 720p60 at 5x real-time, and 45 s recorded through the relay ffprobed clean.
- Design review (Architect, Security, DevOps), same day, acted on before release:
  - A ~76 KB image could inflate to 64 MB and cost ~40 s of pure-Python CPU per segment; a gzip of repeated sync bytes passed as video. Now: 24 MB caps, inflated image at most 4x its compressed data, payload at most 10x its gzip and refused if cut off at the cap, at most 10,000 image chunks.
  - Pure-Python PNG unfiltering cost 0.4-1.8 s per segment depending on the encoder's filters, all under the GIL (about two wrapped recordings on a Pi 4). Replaced by the bundled FFmpeg: 0.10-0.12 s, byte-identical on live segments, no new dependency.
  - The relay would read `file://` segment URIs (its fetch client opens them): only http(s) is fetched now.
  - A segment requested while FFmpeg ran more than the cache behind evicted itself and returned 502; eviction now skips it and prefers finished entries. The relay's segment wait is 12 s, under FFmpeg's 15 s read timeout.
  - One failed playlist reload ended the FFmpeg run; a 5xx or timeout now gets the last good playlist for up to 15 s (a 4xx, such as an expired token, is passed on so the recorder re-resolves).
  - JSON lists without sequence numbers, and URLs with whitespace or control characters, are refused. The browser-TLS fetch built its body with `bytes +=` (~1 s per 6 MB); now a bytearray.
  - Proof: `test_a_segment_older_than_the_cache_is_still_served`, `test_a_playlist_blip_serves_the_last_good_copy_but_a_refusal_does_not`, `test_relay_never_reads_a_non_web_url`, bomb and no-sequence cases added to the refusal tests; the eviction and stale-playlist tests fail when their fix is reverted.
- Same evening, the remaining links: one more independent source was readable without a browser. Its link's page answered 403 to the plain client and 200 to a browser TLS profile (no challenge), and its player listed six playlists, the first answering 504 on every segment and the fifth serving video at 3.8x real-time. The probe stopped at the 403, and from the player kept the first playlist that loaded. Now: a page refused with 401/403 is fetched once more with the browser TLS profile (embeds walked with it), and a playlist whose segment check did not pass is kept only as a fallback while the rest of the player's playlists are tried. Proof: `TestProbeFindsTheWorkingPlaylist` (two tests fail on the old probe; the fallback guard passes on both). Live, same night: the link resolved past the 403, skipped three playlists whose segments timed out and picked the fourth: 720p30, 3.1 Mbps, 8x real-time, a third independent source. The probe took 35 s, most of it those timeouts.
- [PENDING] If an upstream re-signs segment URLs on every reload, each reload looks like new segments: up to four extra image fetches and cache churn. Not seen yet; keying on the path alone could merge distinct segments, so it waits for a real case.
- [PENDING] Live page check showing both providers picked as independent sources, and a full game recorded on them.

## Open after the 2026-11-02 evening
- [PENDING] Provider A (JSON segment list, PNG-wrapped) froze for 15 s twice in about 40 minutes of one game, with no relay error logged; each cost ~25 s and a failover to a slower source, then a manual switch back. Cause unknown: find out whether the JSON list stopped advancing, a segment fetch hung, or the relay's prefetch fell behind.
- [PENDING] On a loaded 3-core host, FlareSolverr took 95-120 s to solve an event page against PVArr's 120 s limit, so Events-tab loads sometimes time out. Measure before raising the limit.
- [PENDING] Nothing stops a working link that plays a different game from being recorded under the scheduled game's name (seen on a college game where every playable link was another game). Needs a design idea before code.

## Open after the 2026-11-03 evening
- [PENDING] Every candidate froze ("no data for 15 s") every 3-25 min over a 3 h hockey recording, across three providers. The relay host's container resolver logged 60+ timeouts to its first upstream DNS server in that time, several within a minute of a freeze, but two freezes had none nearby. Host DNS order changed (local override); re-measure freezes on the next game before calling DNS the cause.
- [COMPLETED] Refuse a failover to a backup that plays a different game. On a 2026-11-08 game, two backups pulled from the same provider host with an event number in the playlist path; one backup's number differed from the stream being recorded and it was another game (about 50 s recorded before a manual Wrong Game mark). When the current and next source carry a comparable event number in their playlist URLs and they differ, skip that backup (or mark it wrong game) on its own. Generic URL comparison only, no site list. Done 2026-11-09, released in v0.19.0: `quality.event_number` takes the one standalone run of 5+ digits in a playlist path (digits inside a token, two numbers, a YYYYMMDD date, or a number that reads as a Unix time in s/ms/us within 30 days give none) and a template of the digit-stripped host plus the rest of the path. The first stream to deliver data sets the event per template; a later connect on the same template with another number is skipped and marked wrong game, a backup check reports it wrong game, and a fresh event-page link is refused. Wrong Game on the anchoring stream clears it. Proven by `TestEventNumberMismatch` (5 tests, including a signed-expiry path that must never compare).
- [COMPLETED] A relay reply cut off by FFmpeg (stopped for a failover) logged a full `ConnectionResetError` traceback each time; seen on every failover of the 2026-11-08 game. The relay now ignores a client hang-up mid-reply.
- [COMPLETED] The "FFmpeg said" line on a freeze is the stderr tail of the whole FFmpeg run, so an "SPS ... buffering period" message from when it joined the stream is reported as if it caused the freeze.
  - Fix (2026-11-04, released in v0.14.2): each tail line keeps the time it arrived (the deque stays 15 lines). A failure reports only lines from the last 60 s; with none, the newest line labelled with its age ("(from 14 min earlier) ..."). Per-line redaction before the 500-char cut is unchanged, and `last_error` holds the same text as the log line. Documented in `docs/troubleshooting.md`.
  - Proof: `test_failure_reports_only_stderr_from_near_the_failure` (recent 403 reported, start-up line dropped; age label; redaction and cap); the existing end-to-end stderr tests still find the 403 in `last_error`.
- [COMPLETED] "No data from any candidate in 1 full attempts" is logged when the list wraps even if every candidate just recorded for 10+ minutes (the counter resets on data, then the wrap adds one).
  - Fix (2026-11-04, released in v0.14.2): a lap in which any candidate delivered data no longer counts toward the fruitless-lap budget, so the warning and the "giving up" path only follow laps with no data, as `docs/features.md` already said. The "Cycling back" line shows "(fruitless lap N of M)" only when N > 0. Worse than the wording: with a one-lap budget the miscount ended a working recording as `completed_partial`.
  - Proof: `test_lap_that_delivered_data_is_not_counted_as_fruitless` (fails on the old code with `completed_partial`).
- [COMPLETED] The event-page re-check swaps a failed candidate's link for a fresh one without probing it. Twice in one evening a link that had played (freezing now and then) was replaced by a page with nothing playable, costing ~50 s and ~2 min of gap. Probe a fresh link before replacing, or keep the old one as a fallback.
  - Fix (2026-11-04, released in v0.14.2): after the page check, each fresh link is probed again with the recorder's own connect probe (`_probe_candidate`, via `fresh_link_plays`), one at a time, in a worker thread, outside the page lock, and only as many as there are slots to fill. A link that does not play is dropped and the next one tried; if none plays, the failed source keeps its slot and the card log says "none played when probed again; kept the current links". With auto-probe off nothing is probed, as before. Also: the "FFmpeg said" age label shows seconds under a minute instead of "0 min earlier" (only reachable if the 60 s window is shortened).
  - Proof: `test_rescan_keeps_a_failed_source_when_the_fresh_link_does_not_play` (dud skipped for the next link, probes off the main thread, all-dud keeps the old link; fails on the old code), `test_rescan_probes_nothing_when_no_slot_needs_a_link`, and `test_rescan_swaps_fresh_picks_into_dead_slots` now asserts the probe; age label in `test_failure_reports_only_stderr_from_near_the_failure`.
- [COMPLETED] Post-processing a 2.7 h 720p recording: comskip ran 73 min (19 breaks found; 3 h timeout). Measure whether that is normal for this host or a sign it is starved by the other containers.
  - Measured 2026-11-04 on the same host and image (8 threads, shipped ini, nothing else running): the same file took **29 min** (3,160 CPU-s, ~1.8 cores, 19 breaks, the same result), i.e. ~6x real time. comskip decodes every frame, so cost follows the source: this one is 59.94 fps at ~4.4 Mb/s; a 2 h 25 min 30 fps, ~1.5 Mb/s recording took 7 min (~21x real time). On the evening itself it got the same ~1.8 cores but needed at least 5,680 CPU-s (still running 51 min in, done by 71 min), so the 73 min is roughly 2x normal and includes the break-card pass and chapter write that follow comskip.
  - Not starvation by other containers: the transcoding container on the host was idle the whole window (only its hourly plugin sync in its log), and nothing else on the host is heavy. The likely extra cost was a manual analysis run (FFmpeg reading the same file) during the window, competing for the same cores and cache; the host's sysstat history has no data for that day, so this is not provable. No code change: a long, high-frame-rate recording takes ~30 min of comskip here, well inside the 3 h timeout. Lowering its priority (`nice`) is in the review follow-ups above, not needed for this.

## Protocol-relative base64 stream URLs (fixed 2026-11-02, released in v0.14.1)
- Symptom: three of five links on a college basketball event page led to one player that loads `atob("Ly9...index.m3u8")`, a protocol-relative URL; the probe decoded base64 only when it starts "aHR0c" ("http"), so all three were "no playlist". Fix: "Ly9" ("//") is accepted too. Proof: `test_protocol_relative_base64_stream_url_is_decoded` (fails on the old probe). At the time the decoded playlist answered 404 (channel not yet live), so recording it is still unproven.

## College games got the wrong league (fixed 2026-11-02, released in v0.14.1)
- Symptom: a men's basketball game added from the Events tab was scheduled as NCAAF. Both schools play football and basketball; the page's league label ("NCAAB") was not a known alias, so `eventinfo._event` fell back to the first league the two teams share, which is football in catalogue order.
- Fix: "ncaab", "cbb" and "ncaa basketball" are aliases of NCAAM; when the label names no league, a word in the URL path may (`_league_in_url`); a sport word in the label or URL ("basketball") narrows the shared leagues to that sport. A hint still wins only if both teams play in that league.
- Proof: `test_college_league_follows_the_label_or_url_not_the_first_sport_both_schools_play` (three cases fail on the old code; no-hint and wrong-league guards pass on both). The live schedule was re-created as NCAAM by hand.

## Extend a running recording (built 2026-11-01, released in v0.13.0)
- Ask: games run long and the only fix was a second recording; the Events tab had "start early" but nothing for running late.
- Built: `POST /api/recordings/{id}/end` (`add_minutes` from the current end, or exact `end_time`), saved to the session record so a resume keeps it. Card buttons **+15m / +30m / +1h / Edit**; Events tab **Keep 0/15/30/60 min after** (default 0, today's behaviour).
- Proof: `test_extend_adds_to_the_current_end_and_persists_it`, `test_extend_without_a_deadline_counts_from_now`, `test_end_edit_refuses_past_far_and_stopped`.
- Not built: editing a *waiting* schedule's end (cancel and reschedule).

## Schedules that find no stream look like errors (built 2026-11-01, released in v0.13.0)
- Symptom: a schedule whose page check found nothing kept the blue **WAITING** badge, reading as "not time yet"; the reason was only in the grey message.
- Built: on a miss the job stores `miss_kind` and `fixable`; the badge shows **not listed yet** (amber), **blocked**, **page error** or **no stream** (red). The probe tags failures a person can rescue (`probe.fixable_kind`: JS-built player, 403 wanting a cookie/referer, rejected token); the row lists up to five with a hint and **Record manually**. Find's link list marks them "(fixable by hand)". Notifications unchanged: no links.
- Proof: `test_miss_kind_matches_the_reason`, `test_only_refusals_a_person_can_fix_are_marked_fixable`, `test_fixable_links_keep_only_tagged_links_with_a_hint`, `test_a_miss_records_its_kind_and_the_fixable_links_but_never_notifies_them`, plus asserts added to the aggregator summary and DevTools-message tests.

## Segment loss investigation

### Live session 66954d39, 2026-10-04 (local container, v:latest) -- cause 2 confirmed  [PENDING]
- Source: provider B, direct mode, 720p, ran 20:22-00:01 UTC.
- **584 segments lost, all 584 "expired", 0 failed to download.** Steady
  2-8/min from minute 3 to the end. So it is candidate cause 2 (falling behind
  the live window), not cause 1 (fetch errors) -- `-seg_max_retry` would not
  have helped this session.
- Final MP4 2125 MB over ~3h39m = ~1.3 Mbit/s against ~1.8 Mbit/s for this
  source class: roughly a quarter of the programme missing.
- Container CPU 5%, host idle afterwards; nothing pointed at local CPU.
- Candidates 1 and 2 were the **same URL**, so failover could not help.
- End: stream stopped at ~23:59, both candidates froze, lap cycling, remux OK.
- Next: measure segment duration / window length / per-segment fetch time on
  that provider (needs sponsor approval for an outside connection), then
  reproduce locally with a short window + slow segment server.
- **Outside probe, 2026-10-05 (sponsor-approved, one use):** playlist already
  404 -- stream over, token dead. Nothing measured. Per-request approval is
  needed again for the next live game.
- **Timestamp gaps in the finished MP4** (packet PTS only, no frames decoded):
  3.56h span, **423 gaps, 34.8 min missing (16%)**. Gap sizes: 316 x 4s,
  102 x 8s -- so **segments are 4s** and one or two vanish at a time. Median
  13-16s between gaps (p10 12s, p90 44s): a steady **get-three-lose-one**
  rhythm the whole game, not bursts. Network hiccups would come in bursts.
- FFmpeg argv has no `-re`/`-readrate`, so FFmpeg is not throttling itself.
- Leading hypothesis (unproven): the source playlist window is ~3 segments
  (12s) but FFmpeg only sees a *new* playlist every ~16s (CDN caching of the
  playlist, or reloads landing on out-of-sync edge servers). Then a segment
  rotates in and out between two fetches and **never appears in any playlist
  FFmpeg reads** -- "expired", zero failures, exactly this pattern.
- **Playlist probe, 2026-10-24 (sponsor-approved, on a different source from
  the 10-04 session, whose provider was not confirmed to match):** polled the
  playlist every 1s for 2 min through the running container's own headers.
  117 of 117 requests OK, media-sequence advanced 31 in 120s (one per 4s, no
  jumps or reversals), **window = 15 segments (60s)**, `s-maxage=1`, no `Age`
  header, ~0.5s median fetch. The same recording had 0 lost segments after an
  hour. So the "3-segment window plus stale playlist" hypothesis does **not**
  hold for this source; it is still untested for the source that lost 16%.
  Next: repeat the probe whenever a game uses that source class.
- Decisive test (ran, owner confirmed 2026-10-31; results not written down here), next live game (scheduled 2026-10-25 afternoon, sponsor-approved): poll the playlist every 1s for 2 min, log
  media-sequence, window length, `Cache-Control`/`Age`, and which edge
  answered. Two minutes settles it.
- Likely fix if confirmed: a small playlist relay (design already sketched in
  Phase 18) that polls the source every ~1s with a cache-buster and serves
  FFmpeg a longer rolling window -- video still goes straight from the CDN.
  Architecture change -> sponsor decision.

## Commercial marking lost to a shutdown (fixed 2026-11-13, unreleased)
- [COMPLETED] A host shutdown 43 minutes into the commercial detection of a three-and-a-half-hour recording left it without chapter marks, and the next start did not redo them. Cause: the session record was removed before detection began, so nothing remembered the marking was owed. The same shutdown waited its whole 20 s budget on that recorder and logged that a `.ts` was left un-remuxed, although the finished file was already in the library.
  - Fix: with detection on, the record stays at `stage="commercials"` until the marking has run to its end; boot runs only that step for such a record (no remux, move or notification), counts each start and gives up after three unfinished runs. A shutdown does not wait for a recorder that is only marking. A stale hidden chapter copy is removed before a redo.
  - Proof: 11 tests; the 7 written first all failed on the old code. End to end in a throwaway offline container on the released image with this code mounted in: stopped during comskip on a two-hour synthetic file, the stop took 0.5 s and the record stayed (`stage: commercials`, one attempt); the next start logged the redo, wrote the chapters and cleared the record.
  - Design review (architect/security/devops) folded in: a marking that would begin while the process is already stopping is not started and not counted; the shutdown wait re-checks every half second, so a remux that hands over to the marking mid-wait releases it; the shutdown note is not written for a recorder that is only marking (it could bring back a record just removed); a damaged attempt count in a record is read as zero; a repeated run does not add a second ad-statistics row.
  - [PENDING] From that review, not done: a clean stop counts as an unfinished run like a crash does (nothing on disk tells them apart; the limit is three for that reason); scratch directories of a killed run stay in the container's temp folder until it is recreated (small in chapters mode, full-size parts in cut mode); comskip is not run under `nice` and is still outside the process registry (see the review follow-ups under "DNS-blocked sources"); a boot redo and a recording that finishes at the same time can run two detections at once.

## Open items carried from completed phases

*From "v0.2.1 validated on the test server (2026-08-31)":*
- [PENDING] **PTS discontinuity across a failover, seen by a live rebroadcast
      viewer.** The channel itself works; what has still never been observed is
      a failover *while a client is watching it*. That remains the first thing
      to look at if Plex ever drops a channel mid-game. Recordings are
      unaffected and measured — only the live viewer path is open.

*From "Candidate 1's 404s: two bugs, neither of them the stream" (2026-08):*
- [PENDING] Whether candidate 1 *records* for the sponsor now. This proves the
      mechanism against a synthetic origin of the same shape; it does not prove
      that particular provider stays up.

*From "Phase 25: Event page -> first three working streams" (2026-10-05):*
- [PENDING] If the cookie paste fails from the container but works in the
  browser, the `cf_clearance` cookie is likely bound to something the
  container does not share (IP or TLS client); FlareSolverr is then the only
  route.

*From "Phase 31-rescan: Re-check the event page when sources die" (2026-10):*
- [PENDING] Live re-check with `exclude` on the next event-page recording
  (container restarted onto 9c96d27 the same evening).

*Launch prep (2026-10-29):*
- [PENDING] README: add a short "How it differs" note. If you have an IPTV
  provider playlist, Dispatcharr (or DVarr for scheduled sports) is the better
  tool; PVArr is for web pages, with multi-source failover into one file.
- [COMPLETED] Commercial detection missed breaks where a source replaces ads with cards. On one baseball recording (1280x720, re-encoded at a flat ~1.5 Mb/s) comskip with the shipped ini caught 5% of break time.
  - Built: `app/breakcards.py`, run by `commercials.process()` whenever `PVARR_COMSKIP=1` (FFmpeg only, so also without comskip). Break = audio 10 dB under the recording's median **and** the score overlay gone, each holding 30 s on its own before they are combined. The overlay is learned per file: pixels steady through the game that change in at least 30% of quiet frames (background that never moves is not overlay). Gone = 60% of those pixels changed, so a score change does not count. No steady overlay: no card breaks. Breaks from both detectors are merged into one chapter write; if PVArr's chapter file fails, comskip's still goes in. Cut mode and stats use the merged list.
  - Found while building: combining the two signals second by second, then merging, or averaging the overlay distance, marked quiet stretches where the score digits changed. The share rule fixed it. A "breaks over half the file" guard was dropped: quiet is relative to the median, so it can never exceed half.
  - Proof: on the sample, 90% of break time found, 91% of what it marked agreed with the stand-in key (two independent signals: exact repeated footage, which live play never produces, and overlay absence); 65 s, 86 MB peak. Tests: `TestBreakCards` (card found; replay or quiet play alone is not a break; score change is not; no overlay gives up; FFmpeg output parsing; end-to-end on a generated clip through real FFmpeg) and `TestCommercialsWithCards` (runs without comskip; one chapter write carries both; a failed card pass or chapter file leaves comskip's chapters).
- [COMPLETED] Step down on a slow connection (2026-11-07, released in v0.16.0, sponsor request: keep the best quality, drop only when the connection loses segments). A lost segment counts against the connection when FFmpeg reports it expired, or failed with no source reason (checked against real FFmpeg: a segment that times out on a fresh connection prints no reason; a 404 prints "HTTP error 404"); expiries within 10 s of a source error do not count. 4 such events in 5 min step to a lower level of the same playlist (same host only, through the relay so its contents are checked for private addresses), else to an alive backup that is a different feed at <= 75% of the bitrate. Back up after 20 quiet min; a relapse within 10 min stays down; max 3 step-downs. Design review (architect/security/devops) folded in: own request slot so it never overrides a manual switch or failover; losses before a step do not re-trigger; no failover alert for a step. Mixed-resolution remux checked with real FFmpeg (1080p then 720p .ts remuxes, the 720p part decodes at 1280x720). Proof: `TestStepDownOnSlowConnection`, `TestStepDownInTheLoop` (real FFmpeg wording test on a local source that 404s one segment and stalls another). Independent validation 2026-11-07: all criteria pass, 888 tests; two of its findings fixed: a step the loop discards (a failover got there first) no longer uses up one of the 3 step-downs, and a failover off the lighter stream clears the step-back state (`test_dropped_step_does_not_use_up_a_step_down`, `test_failover_off_the_lighter_stream_clears_step_state`). Left as is: the same-host check ignores the port. Open: first field proof needs a game with a multi-level playlist or a lighter separate feed (rare so far: every master seen on 2026-11-07 had one level); a stall partway through a segment logs no lost segment at all, so it is invisible to this.
- [COMPLETED] Card pass found nothing on a second recording (2026-11-03: hockey, 720p, ~2.7 h, stitched from three sources by failover; the sponsor saw at least one card on it). Both signals failed: audio never dropped more than 7.9 dB under the median over a 31 s window (needs 10; zero quiet seconds), and no steady overlay was learned for the whole file (one 30-min window of six had one). It also took 15 min of one core (13 min of it the keyframe decode) against 65 s on the 720p sample, at the same resolution. Not releasable as is: try 2-3 more recordings, then decide whether the thresholds can be made to generalise or the module should be dropped. Third recording (2026-11-07 check, hockey, 720p, stitched from two sources): same result, no steady overlay learned, no breaks; 4 min of one core, 79 MB peak. Two of two real recordings now fail on the overlay step, which points at stitched multi-source files. Resolved by the repeat-frame rebuild below.
- [COMPLETED] Hand-check the card pass's three disputed spans: moot. The stand-in key that disputed them (repeated footage) is now the detector.
- [COMPLETED] Break-card pass rebuilt on repeated pictures (2026-11-07, released in v0.16.0). The quiet-audio + overlay rule found nothing on a stitched hockey recording. A card repeats from break to break and live play never repeats a picture, so a second is break when its keyframe (64x36 gray, halved to 32x18) is within RMS 3 of one at least 300 s away, and such seconds hold for 45 s (gaps up to 10 s bridged). The audio pass is gone; numpy added for the all-pairs comparison (~70 MB in the image; `OPENBLAS_NUM_THREADS=1`). Proof: on the 2026-11-03 hockey recording (60 fps, the one the old rule missed) it found all four spans the sponsor confirmed as cards, each within 3 s, 10 breaks / 16 min in all; on the 2026-11-07 one (30 fps) 12 breaks / 23 min, matching the study. Cost 3.3 and 9.7 min of one niced core (mostly the keyframe decode), 190 MB peak. Tests: `TestBreakCards` (a card in two breaks is found in both; live play, a 4-min still shot and a card shown once are not; sparse keyframes; parsing; end-to-end through real FFmpeg). Limits: a card shown once is missed; anything shown twice (a long slate) is marked.
- [COMPLETED] From the design review, not caused by this change: comskip and the new FFmpeg passes run as plain `subprocess.run`, outside the process registry, so shutdown does not kill them (stopping the container does); the chapter remux replaces the file without copying its mode bits and without a free-space check.
  - Fix (2026-11-04, released in v0.14.2): `apply_chapters` copies the original's mode bits onto the new file before the swap, and is skipped with a WARN unless the volume has the file's size plus 5 GB free (`CHAPTER_FREE_MARGIN_BYTES`, the default recording floor, so the remux cannot push a running recording into its low-disk abort). Documented in `docs/comskip.md`. Limitation: an operator who raised `PVARR_MIN_FREE_GB` above 5 is only protected up to 5 GB.
  - Proof: `test_chapter_remux_keeps_mode_bits_and_checks_free_space` (fails on the old code: mode 0600 instead of 0664; with too little free space FFmpeg is never started and the file is untouched).
  - Won't fix: the shutdown/registry half (sponsor decision, 2026-11-04). PVArr runs as a container and `docker stop` kills these children; a registry for them would add moving parts for no real gain.
- [COMPLETED] Mid-recording event-page re-check now logs the links that gave no playlist (2026-11-07, released in v0.17.0). On one game every working link on the page came down to a single slow upstream (freeze and failover every 1-2 min for the whole first half hour), and the re-check said "9 unresolved" without naming them, so nothing could try them by hand or with a browser-based resolver. The card log now lists them (capped at 20), `aggregator.unresolved_links()`. Find already showed them. Proof: `test_not_video_is_never_picked_and_the_page_is_summarised` asserts only the unresolved link is listed, not the failed one; 889 tests.
- [COMPLETED] A 15 s freeze followed by a failover is not counted as lost segments, so a recording that froze 20 times in 25 min (about a third of the expected bytes missing) still shows "0 lost". Count freeze time, or show bytes per minute against the stream's bitrate, so a starved recording is visible on the card. Done 2026-11-09, released in v0.19.0: every gap of more than 12 s between writes, across failovers, is counted whole as time without data; the card, the finish log line and the finish notification (from 1 min) show it. Proven by `TestNoDataAndStalls` (6 tests: long gaps count whole, 6 s segment pacing counts nothing, an open gap shows while running).
- [COMPLETED] Tell a briefly stalling source from a dead one (measurement part). On a 2026-11-07 game the best source's playlist server stalled for 1-8 s at a time from mid-game on, and each stall that ran past the 15 s freeze timer failed over to a worse backup. Waiting a little longer on a source that has been healthy (or failing back sooner) would have kept the better picture. Measure the stall lengths before changing the timer. Done 2026-11-09, released in v0.19.0 (sponsor: measure only): each attempt that saw stalls of 8 s or more logs their lengths (a freeze adds its timer as the last one), and a recording ends with one line per source: count, median, longest. Proven by `TestNoDataAndStalls`.
- [PENDING] Decide the freeze timer from the measured stalls once a few games have logged them (wait longer on a source that has been healthy, or fail back sooner).
- [COMPLETED] Remember the Referer that worked for a source across failovers. On the same game one segment host started requiring a Referer mid-game; a failover back to that source must reuse the header that last worked instead of starting without it. Done 2026-11-09, released in v0.19.0: the Referer an attempt delivered data with is kept per feed (`source_key`); a connect whose detection found none gets it (FFmpeg headers, relay and hls-proxy all read it), a Referer the probe chose is never replaced. Proven by `TestRefererMemory` (3 tests, including a full connect through the recording loop).
- [COMPLETED] Segments packed inside WebP images (2026-11-07, released in v0.17.0). Found on a live game: a separate, healthy 1080p feed (~4.8 Mb/s) whose segments are a 42-byte WebP header followed by plain MPEG-TS, while every link PVArr could read froze about once a minute. `pngwrap.is_wrapped` / `webp_ts`: a RIFF/WEBP file with whole 188-byte TS packets from a sync byte within the first 64 bytes to the end; the header is cut, nothing decoded. Probe, measure and relay use `is_wrapped`. Proof: `test_webp_wrapped_ts_is_unwrapped_by_cutting_the_header` (synthetic header; a picture or a cut-short packet is refused), WebP case added to `test_probe_flags_wrapped_segments_and_refuses_plain_images`; a real segment unwrapped to 1080p H.264 + AAC; hot-patched into the live container, the recording ran at full rate (36 MB/min) with no freezes. 890 tests.
- [COMPLETED] Browser sidecar (2026-11-07, used by PVArr when `PVARR_BROWSER_URL` is set, released in v0.18.0 on 2026-11-08 with its own image `ghcr.io/jlesterak/pvarr-browser`). Find marks links whose player builds the playlist URL in JavaScript "unresolved"; on one live game those included the only healthy feed. Measurement on that game's 7 unresolved links: Chromium in new headless mode resolved 4 (2 recordable new sources), median 7.3 s, ~0.5 GB RAM per page; the old headless shell resolved 1. Built: `browser/` (separate optional container, `POST /resolve`, max 2 pages, hard per-request limit, no cache). Docs: `docs/browser-sidecar.md`.
  - Design review (architect/security/devops) folded in: every call into the page has its own limit plus an overall one (a page that freezes its main thread used to hold a slot forever); a dead Chromium is relaunched once; a cancelled request still closes its page; the 5-min result cache was dropped (it would hand back the dead playlist exactly when a failover asks again); service workers blocked, WebSockets to private addresses refused, `localhost.` caught, a playlist on a private address never returned; only small or playlist-typed bodies are read; the user agent names the real Chromium version. Chromium's own sandbox is off (Playwright's default in a container), so the docs make network isolation required and give a complete holder-container Compose example (`init: true`, user-owned tmpfs, 1.5 GB limit). CI launches Chromium inside the built image.
  - Proof: 19 tests with real Chromium against local fixture pages (script-fetched playlist, cross-origin nested player, click-to-play, popup ad, JSON segment list, no stream, LAN playlist and LAN WebSocket blocked, a frozen page returns in time and the browser still works after) and API checks (bad scheme, private host, busy, clamp, private playlist refused, hard limit frees the slot); 4/7 again on the live links after the hardening. Container check (2026-11-08) using the documented Compose example: image 1.83 GB, runs as uid 10001, LAN unreachable from inside, 6 live links from a second game resolved the same as Chrome on the host (1/6, a recordable new source, 6.6 s; misses stop at ~33 s), container peak 652 MiB of the 1.5 GB limit, 264 MiB idle after, no zombie processes.
  - PVArr integration (2026-11-08, `app/browser.py`): only links the probe marks `devtools_m3u8` (page fetched, no playlist in it) go to the sidecar; a dead playlist, a 404 or a DNS-blocked host never do, so failover is not slowed. Find/scheduled scan/re-check: only when the probe phase came up short, up to 4 links in page order, one page at a time (the second sidecar slot stays free for failover connects), 20 s each, 45 s in all, inside the existing abort; a resolved link replaces its probe result, so ranking, source letters and summary are unchanged. Connect (and the fresh-link check before a re-check swap): the probe (which runs yt-dlp itself when installed), then the browser. Every returned playlist is re-probed with the frame's Referer/UA/Cookie; a private playlist from a public page is refused before any fetch; header values with control characters or over a size cap are dropped; only the URL goes to the sidecar, never the operator's cookie. The call runs on an abandoned daemon thread so Stop and shutdown never wait for it; a page that gave nothing is not asked again for 5 min; 3 unreachable calls in a row pause it for 5 min.
  - Design review (architect/security/devops) folded in, including one real hole: the sidecar blocked only literal private IPs, so a page could reach PVArr's unauthenticated API by its Docker name on the network they share. The sidecar now resolves host names (once per page) and refuses private ones; DNS rebinding stays a documented residual. Compose example: `mem_limit` 2g (two pages measured up to 1.8 GB), `PVARR_BROWSER_URL` on PVArr.
  - Proof: 11 PVArr tests with a fake sidecar (off unless configured, only for JS pages, timeout bounds, frame headers used and only URL+timeout sent, private playlist refused with no fetch, CRLF values dropped, miss remembered but busy not, unreachable pause, Stop abandons a slow call in under 2 s, Find asks only when short and in page order, at most 4 pages one at a time, connect falls back and a 403 playlist does not) + 1 sidecar test (a Docker-name host resolving private is blocked); 901 + 20 green. End to end: test PVArr + sidecar container on a live JavaScript-player page: connect said "Browser sidecar resolved" in 8 s, 1-minute recording 7 MB, 0 segments lost.
  - Released by sponsor decision after 2 of 3 planned measurement nights; the third (2026-11-09) passed as well, see the roadmap. Open: an arm64 sidecar image (needs a native arm64 runner; PVArr's own image is amd64 too).
