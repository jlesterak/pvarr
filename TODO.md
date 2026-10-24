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

## Phase 13: Session Durability & Bounded Recordings  (ACCEPTED, not started)

Agreed with the sponsor 2026-08-30. Build in this order -- each step needs the
one before it.

- [COMPLETED] **1. Disk-space guard (`recorder.py`, `server.py`).** Mandated by the project
      directives and never implemented; the unused `import shutil` in
      `recorder.py` is where it was meant to go. Without it, pointing PVArr at a
      24/7 channel fills the disk until the host breaks -- and the recordings
      volume is usually the same filesystem as everything else. Every active
      recorder checks free space and aborts cleanly below a configured floor.
      Smallest of these items and the only one that prevents damage, so it went
      first.

      Checked on the write path, rate-limited to one `statvfs` every 15s.
      A breach ends the recording rather than failing over -- the problem is
      local, so another candidate cannot help -- and the footage captured so
      far is kept and post-processed exactly as an operator stop would be.
      Status `aborted_no_space` survives the completion block so the reason is
      not hidden behind "completed". `PVARR_MIN_FREE_GB` (default 5) sets the
      floor; `0` disables it. `POST /api/recordings/start` refuses with `507`
      when the volume is already below the floor, rather than starting a
      capture the guard would abort seconds later.

      **Found on the first run:** the dev box was at 100% -- 152 MB free of
      225 GB -- so the guard fired immediately and took several unrelated tests
      down with it. Two real defects came out of that: the test suite depended
      on the host's free space (fixed -- fixtures disable the guard, and the
      guard's own tests stub `free_bytes`), and `server._min_free_gb()` read
      its default off `StreamFailoverRecorder`, which tests routinely replace
      with a `MagicMock` (fixed -- `DEFAULT_MIN_FREE_GB` is now a module-level
      constant imported by name).

      Verified live: real recorder, real subprocess, real writes, with only the
      free-space reading stubbed. Dropped the volume below the floor mid-capture
      -- stopped in 3.5s, kept 3.81 MB, did not fail over, logged the reason.

- [COMPLETED] **2. Session state persisted to `/config`.** All session state lives
      in the in-memory `active_recorders` dict, so a `docker restart` or
      `docker compose up -d` destroys every in-flight recording: the FFmpeg
      child dies, the `.ts` survives on the volume but is orphaned -- no remux,
      no notification, no library entry, and the Plex channel vanishes.
      One small JSON per session (URLs, detected headers, output path, timings,
      active candidate), written **on state transitions only** -- not on a timer
      -- so ongoing disk writes stay near zero. Progress is recovered by
      `stat()`ing the `.ts` at resume, not by persisting counters.

      *Note:* `./config:/config` is already mounted and the Dockerfile creates
      it, but **nothing writes there yet**, and the host `config/` is owned by
      root while the container runs as uid 1000 -- so the first write will fail
      with permission denied until it is chowned. Fix and document with this.

- [COMPLETED] **3. Resume on restart/recreate.** On boot, read the session files
      and reconnect, appending to the same `.ts` (which is how failover already
      works). Bounded by a maximum gap -- past it, finalise instead of
      reconnecting -- and by a resume-attempt counter, so a recording that dies
      immediately cannot loop against `restart: unless-stopped`. Token expiry is
      already handled: the recorder re-probes each candidate at connect time.

- [COMPLETED] **4. Recording windows and duration caps.** A recording may carry an
      end time or a maximum duration; at the deadline it stops cleanly and
      post-processes normally. This is what makes resume *exact* -- `now < end`
      means reconnect, `now >= end` means finalise -- reducing the gap heuristic
      above to a fallback for recordings with no window.
      - **Sponsor decision:** when a window is set and every candidate fails
        before it closes, keep retrying until the window ends. The cycling and
        backoff this needs landed in Phase 14; what remains is lifting the
        `max_cycles` cap while a window is still open.
      - **Sponsor decision:** global backstop `PVARR_MAX_HOURS`, default **6**.
        4 was proposed and revised to 6 on the observation that 4h truncates NFL
        overtime and extra-innings baseball -- the most likely things being
        recorded. Per-recording limits override the backstop.
      - Timezone: the dashboard sends absolute timestamps and renders them back
        in local time, so the container's TZ (UTC, unset in compose) never
        matters. This holds only for one-shot windows.

### Declined
- **Deferred start / scheduled recordings ("start at 1400").** Sponsor declined
  2026-08-30. It needs a pending-job store, a scheduler, and pending-job UI, and
  `cron` + `curl` against the existing API covers it at zero cost. Recurring
  schedules would additionally need real timezone and DST handling.
  **Reversed 2026-10-08:** the sponsor asked for a simple one-shot schedule
  that checks an aggregator page at the start time -- something cron + curl
  cannot do, because the stream links do not exist yet when the job is set
  up. Built as Phase 27 (below). Recurring schedules remain declined.

## Still open

### Longer-term candidates
- A retention/cleanup policy for old recordings on disk.
- Integration coverage for the notification webhooks (currently mocks only).
- A headless-browser probe path so JavaScript-built m3u8 URLs work without the
  external `detect-headers`.

---

## Phase 17: Tagging & Library Organisation  (ACCEPTED, not started — future feature)

humantodo line 3. Scoped with the sponsor 2026-08-31. **Do not start this
without a fresh go-ahead** — it is a deliberate later feature, parked here so
the analysis is not re-derived from scratch.

### The asymmetry that makes this hard
The *arr tools make naming look easy because the file is a known entity
*before* it exists: Sonarr requests episode 7 of TVDB:81189 and derives the
name from an id it already holds. Renaming is a lookup.

PVArr is the inverse. An operator pastes an HLS URL for a game happening now.
There is no id — only what was typed into three text boxes, possibly "Pack"
and "Bears" at 4:58 because kickoff was at 5:00. Matching that to a sports
database is *fuzzy matching*, not lookup, and that is where these features
usually die.

The one redeeming signal: PVArr knows the wall clock. A live game is pinned to
a moment, so "something like Bears vs something like Packers, starting
2026-08-31T19:05Z" is close to a unique key against a schedule API. That is
what would make L4 tractable *if* it is ever revisited.

### The Plex constraint (drove the whole design)
Plex has **no sports metadata agent**. Library types are Movies, TV Shows,
Music, Photos, Other Videos; there is no TVDB for last night's game. Anything
fetched from an external DB is invisible unless written in a form Plex reads,
which for personal media means the naming convention itself.

The pattern that works is sport-as-series under Plex's *Personal Media Shows*
agent:

    Sports/NFL/Season 2026/NFL - S2026E07 - Bears vs Packers.mp4

Critically, **this needs no external database at all** — sport, teams, date and
a counter are already in hand. That is why L3/L4 were cut: they buy canonical
team names and logos, and carry essentially all of the fragility.

### Sponsor decisions (2026-08-31)
1. **Record flat, then move into a separate configurable library root.** Not
   organise `recordings/` in place. Keeps the capture path dumb and the path
   guard simple. Same-filesystem by default.
2. **`NFL - S2026E07 - Bears vs Packers` is acceptable.** Ugly, but it is what
   Plex actually understands.
3. **L1 and L2 only. L3 (.nfo sidecars) and L4 (sports DB) are cut** from the
   first pass. Ship the offline half, live with it, then judge.
4. **Retro-tagging the existing library is out of scope.** A sweep that
   reorganises everything already recorded is a file manager, it is the
   highest-risk code in the feature, and it is a one-time job better done with
   `mv` and a shell loop a human can watch.

Also declined: becoming a metadata server (artwork cache, browse UI). Plex
already is one.

### Scope, as accepted
- **L1 — Path templates + category routing.** Optional Sports/News/Sitcoms
  folders, nested by sport and season. Offline, deterministic, testable.
- **L2 — Plex-friendly `SxxExx` numbering.** The episode counter must be
  derived by scanning the season folder, **not** kept in a state file. A
  counter file drifts the moment someone moves a file by hand, and it drifts
  silently.

### Four blockers already in the code (costed before any work starts)
1. **The library is flat.** `naming.py:146` uses `iterdir()`, one level deep.
   The moment a recording lives in `Sports/NFL/` the library UI goes blind —
   the same class of bug as the `.ts`-only one commented at `naming.py:140`.
2. **The path guard will refuse.** `_safe_filename` (`server.py:172`) rejects
   any filename carrying a directory component. That is correct and deliberate:
   it is the path-traversal defence on endpoints that are unauthenticated by
   design. Subfolders make every delete/rename/download return 400. It must be
   **replaced with a resolve-and-contain check, never simply relaxed.** This is
   the single item most likely to introduce a vulnerability if done casually.
3. **Cross-filesystem moves are not moves.** `os.replace` will not cross a
   mount. If the library root is a different volume, a 6 GB game is a real
   copy: minutes of I/O, both copies on disk simultaneously, and the
   disk-space guard needs to account for it.
4. **Remux writes beside the source** (`post_processor.py`, `with_suffix`).
   Organising is a separate step afterwards with its own failure path, and the
   notification plus the library entry must reference the *final* location,
   not where the file was born.

### Process note
This touches `naming.py`, `server.py`, `post_processor.py`, `tuner.py`,
`sessions.py` (the persisted output path) and the dashboard template, changes
the on-disk layout, and rewrites a security control. Per the project
directives that is a **convene-the-team change** — Architect, Security and
DevOps get a scoped design before any code is written. Blocker 2 is the
Security brief; blocker 3 is the DevOps brief.

### If L4 is ever revisited
Provider survey, done 2026-08-31:
- **TheSportsDB** — the only realistic fit. Free tier, community-run,
  teams/leagues/logos/events. Good coverage of major leagues, patchy below.
  Requires a key. Would go behind a small provider interface as the single
  implementation, entirely optional, strictly post-remux, and structurally
  incapable of failing a recording.
- **ESPN's undocumented JSON endpoints** — widely used, entirely unofficial,
  can vanish overnight, ToS grey. Do not build on it.
- **API-Sports and similar** — commercial, per-request quotas.
- **Sportradar / Stats Perform** — enterprise pricing. Not for a self-hosted
  tool.

## Phase 16: Host Instrumentation

- [x] **`scripts/watch-host.sh` — measure a recording host instead of guessing.**
      The sponsor's question before a live capture was whether the target host
      could take the disk I/O and general load. There was no way to answer it
      from the workspace: PVArr reports what *it* thinks it captured, and
      nothing recorded what the machine underneath was doing. The script
      samples load, CPU idle, available memory, free space on the recordings
      volume, per-device read/write KB/s and I/O busy time from
      `/proc/diskstats`, FFmpeg process count / CPU / RSS, and captured MB from
      `/api/status`, into a CSV plus a summary on exit.

      Runs on the host being measured, unattended if wanted
      (`--duration 5h`). Dependencies are coreutils and `/proc`; the
      `/api/status` columns need `curl` + `python3` and degrade to `NA`
      without them. System metrics only — it never reads the video and never
      takes a frame grab.

      Device resolution handles the awkward cases: a partition with no
      `/proc/diskstats` row of its own falls back to the parent whole disk,
      and `/dev/mapper` LVM/crypt paths resolve through `readlink -f` to their
      `dm-N` name. Verified against a live recording on the workstation
      (`dm-1`, an LVM volume): four samples, plausible throughput, FFmpeg
      correctly counted at one process.

      **Expected shape for a 1080p sports HLS capture**, for comparison when
      reading a real run: FFmpeg is `-c copy`, so there is no transcode — CPU
      should sit near idle, and a few percent is already suspicious. Writes are
      a sequential append flushed per 64KB chunk to the page cache, never
      `fsync`ed, so the kernel batches them; sustained write rate is simply the
      stream bitrate (~5 Mbps ≈ 625 KB/s ≈ 2.2 GB/hour). Disk busy time should
      be low single-digit percent on anything that is not a heavily contended
      spindle. The floor that stops a capture is `PVARR_MIN_FREE_GB`, default
      5.0 GB, re-checked every 15s during recording
      (`Recorder.DISK_CHECK_INTERVAL_SEC`), and it aborts rather than failing
      over — a local problem is not fixed by another stream.

### Not done, and why
- **Remote monitoring of the test host.** Out of bounds by Directive 6 and
  architecturally wrong anyway: the measurement has to run on the host being
  measured, not be pulled across a network by a process that is not awake when
  the capture is.

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
- Decisive test, next live game: poll the playlist every 1s for 2 min, log
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

*From "Commercial detection (comskip)  [COMPLETED — needs field validation":*
- [ ] **Detecting an animated break card.** freezedetect is out. Candidate
      signals: an abnormally long gap with no scene cuts (live sport cuts
      constantly, a card does not), and the very low encoded bitrate of a
      simple loop. **Needs a real sample to measure against** -- building a
      detector from my guess about what these cards look like would repeat the
      `-allowed_extensions ALL` mistake exactly. A 60-second clip spanning one
      break would settle it.

*From "Commercial detection (comskip)  [COMPLETED — needs field validation":*
- [ ] Investigate the network B tail miss: which logo comskip learned (verbose
      log / `.logo.txt`), and whether pinning the logo area fixes the ends.

*From "Phase 25: Event page -> first three working streams (2026-10-05) [C":*
- [PENDING] If the cookie paste fails from the container but works in the
  browser, the `cf_clearance` cookie is likely bound to something the
  container does not share (IP or TLS client); FlareSolverr is then the only
  route.

*From "Phase 31-rescan: Re-check the event page when sources die (2026-10-":*
- [PENDING] Live re-check with `exclude` on the next event-page recording
  (container restarted onto 9c96d27 the same evening).
