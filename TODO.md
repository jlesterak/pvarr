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

## Longer-term candidates
- A retention/cleanup policy for old recordings on disk.
- Integration coverage for the notification webhooks (currently mocks only).
- A headless-browser probe path so JavaScript-built m3u8 URLs work without the
  external `detect-headers`.

---

## Phase 17: Library organisation  (BUILT 2026-10-24, released in v0.11.1)

`PVARR_LIBRARY_DIR` moves a finished recording to
`<dir>/<Sport>/Season <year>/<Sport> - S<year>E<NN> - <A> vs <B>.mp4`. See
README "Organising finished recordings" and CHANGELOG [0.11.1]. Season is
the recording's calendar year (sponsor decision), so an NFL season splits across
two folders.

Proven by: unit and HTTP tests (traversal, symlink, sibling-prefix, concurrency,
cross-mount refusal, ordering of the finish pipeline; the guard tests were
mutation-checked to fail when the guard is removed), and a real-ffmpeg run in a
scratch folder (two remuxed games organised as E01/E02, a crash-recovery record
finalised as E03, nothing left marked as finishing).

- [COMPLETED 2026-10-31, owner confirmed] Confirm Plex indexes the tree as intended (Personal Media Shows
  agent) on the next real recording, with the library inside the recordings
  volume.
- Deliberately not built: a library on a separate mount (refused with a message,
  never copied), .nfo sidecars, a sports-database lookup, retro-tagging the
  existing library, a category-folder setting.

## Direct-mode extension refusal (fixed and released in v0.11.1, 2026-10-25)
A live game recorded 0 bytes for 10 minutes: every candidate's playlist listed
byte-range segments on `.image` URLs. The segments were real MPEG-TS (sync bytes
at the range offset, plain GET works), but direct mode refused them. Proven by
running the container's ffmpeg against the live playlist: refused with no flags,
clean with `-extension_picky 0 -allowed_extensions ALL`. Fix: relax the
extension check on every path, test updated. Hot-patched into the running
container and a new session recorded cleanly (0 lost segments in the first minute).
- [COMPLETED] The hls-proxy fallback also failed every segment for this source
  class, for two reasons found by running the container's proxy against the live
  playlist: it answered 403 "Unknown upstream host" for segments on a different
  host than the playlist, and it ignored the Range header, returning whole files
  where the playlist asked for byte ranges. Both fixed by a build-time patch of
  the pinned proxy (`scripts/patch-hls-proxy.py`, fails the build if stale).
  Proven: ffmpeg through the patched proxy recorded valid h264/aac at 720p.

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
- [PENDING] Boot finishing runs commercial detection inline and delayed the
  server coming up by ~7 minutes with two long recordings; resumed sessions
  waited behind it. Restarts also spend a session's resume allowance, so three
  restarts in an hour finalised a live recording instead of resuming it.

## Episode titles missing in Plex (fixed 2026-10-26, released in v0.11.2)
- Symptom: with the README's Plex setup (TV Shows, Personal Media Shows agent) every organised recording showed as "Episode 1" or a date. That agent reads episode titles only from the container's title tag; PVArr wrote none, and the comskip chapter write (`-map_metadata 1`) replaced the file's tags with the chapter file's (none).
- Fix: the remux writes `<Team A> vs <Team B>` as the title (`library.episode_title`, same sanitising as the filename; no tag when the form named no teams); the chapter write keeps input 0's tags.
- Proof: `test_title_tag_survives_remux_and_chapters` (real ffmpeg: remux with a title, write a chapter, ffprobe shows both); it fails with the old `-map_metadata 1`.

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

## Protocol-relative base64 stream URLs (fixed 2026-11-02)
- Symptom: three of five links on a college basketball event page led to one player that loads `atob("Ly9...index.m3u8")`, a protocol-relative URL; the probe decoded base64 only when it starts "aHR0c" ("http"), so all three were "no playlist". Fix: "Ly9" ("//") is accepted too. Proof: `test_protocol_relative_base64_stream_url_is_decoded` (fails on the old probe). At the time the decoded playlist answered 404 (channel not yet live), so recording it is still unproven.

## College games got the wrong league (fixed 2026-11-02)
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
