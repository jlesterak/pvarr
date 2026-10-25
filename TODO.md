# PVArr - Implementation TODO

The project's working log: what was built, why, how it was proven, and what is
still open. Newest phases are at the bottom.

Sites are never named here. Upstreams are "provider A/B/C/D", pages that list
several stream links are "event pages" (`app/aggregator.py` in the code), and
recorded broadcasts are "network A/B". Site-specific field notes live in a
separate private notes repo and must not be copied into this one, its commit
messages, tests or screenshots. See README "Scope".

## Open work

Everything finished is in the private notes log (phases 1-33, through v0.11.0) and the release notes (`docs/release-notes/`, `CHANGELOG.md`). Only open work and standing decisions stay here.

## Closed by decision (not open work)
- **Quantified AI footprint figures — will not add.** The build was never
  instrumented, and providers do not publish per-token energy for frontier
  models. Any gram-of-CO₂e or litre-of-water figure produced now would be
  fabricated — exactly the failure mode a transparency section exists to
  avoid. Reopen only if real provider usage data becomes available.
- **Footprint section rewritten in an anarchist/luddite register** with
  carbon-offset donation links (Cool Earth, Clean Air Task Force, Wren —
  all three URLs verified live). The "we don't know and they won't say"
  position is unchanged; only the voice moved.

## Decisions
- **Authentication: accepted risk, will not add.** Deployment is a trusted LAN
  behind a firewall. Documented prominently in the README instead. Revisit only
  if this is ever exposed — note that adding Basic auth would break the Plex and
  Emby tuner fetches unless `/live/*` is exempted or given a token parameter.

## Still open

### Longer-term candidates
- A retention/cleanup policy for old recordings on disk.
- Integration coverage for the notification webhooks (currently mocks only).
- A headless-browser probe path so JavaScript-built m3u8 URLs work without the
  external `detect-headers`.

---

## Phase 17: Library organisation  (BUILT 2026-10-24, released in v0.11.1)

`PVARR_LIBRARY_DIR` moves a finished recording to
`<dir>/<Sport>/Season <year>/<Sport> - S<year>E<NN> - <A> vs <B>.mp4`. See
README "Organising finished recordings" and CHANGELOG [Unreleased]. Season is
the recording's calendar year (sponsor decision), so an NFL season splits across
two folders.

Proven by: unit and HTTP tests (traversal, symlink, sibling-prefix, concurrency,
cross-mount refusal, ordering of the finish pipeline; the guard tests were
mutation-checked to fail when the guard is removed), and a real-ffmpeg run in a
scratch folder (two remuxed games organised as E01/E02, a crash-recovery record
finalised as E03, nothing left marked as finishing).

- [PENDING] Confirm Plex indexes the tree as intended (Personal Media Shows
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

## DNS-blocked sources (fixed 2026-10-25, unreleased)
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

## Quality ranking ignores byte ranges  [PENDING]
Find / re-check log "segment is not video" for every source whose playlist
packs segments with EXT-X-BYTERANGE inside a non-video file: `quality.measure`
fetches the whole file and sees the wrapper's header (an image) instead of the
MPEG-TS at the offset. Those sources still get picked, but unmeasured, so the
ranking cannot tell a good one from a bad one. Fix: honour the byte range when
fetching the sample segment. Seen live 2026-10-25 on every pick of a re-check.

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
- Decisive test, next live game (scheduled 2026-10-25 afternoon, sponsor-approved): poll the playlist every 1s for 2 min, log
  media-sequence, window length, `Cache-Control`/`Age`, and which edge
  answered. Two minutes settles it.
- Likely fix if confirmed: a small playlist relay (design already sketched in
  Phase 18) that polls the source every ~1s with a cache-buster and serves
  FFmpeg a longer rolling window -- video still goes straight from the CDN.
  Architecture change -> sponsor decision.

## Open items carried from completed phases

*From "v0.2.1 validated on the test server (2026-08-31)":*
- [ ] **PTS discontinuity across a failover, seen by a live rebroadcast
      viewer.** The channel itself works; what has still never been observed is
      a failover *while a client is watching it*. That remains the first thing
      to look at if Plex ever drops a channel mid-game. Recordings are
      unaffected and measured — only the live viewer path is open.

*From "Candidate 1's 404s: two bugs, neither of them the stream (2026-08-3":*
- [ ] Whether candidate 1 *records* for the sponsor now. This proves the
      mechanism against a synthetic origin of the same shape; it does not prove
      that particular provider stays up.

*From "Phase 25: Event page -> first three working streams (2026-10-05) [C":*
- [PENDING] If the cookie paste fails from the container but works in the
  browser, the `cf_clearance` cookie is likely bound to something the
  container does not share (IP or TLS client); FlareSolverr is then the only
  route.

*From "Phase 31-rescan: Re-check the event page when sources die (2026-10-":*
- [PENDING] Live re-check with `exclude` on the next event-page recording
  (container restarted onto 9c96d27 the same evening).
