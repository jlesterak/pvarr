# PVArr - Implementation TODO

## Phase 1: Core Failover Engine & Studio Foundations
- [x] **Task 1: Environment, Project Structure & Dependency Setup**
- [x] **Task 2: Core Failover Engine (`stream-recorder.py` / `app/recorder.py`)**
- [x] **Task 3: Sports File-Naming & Storage Module (`app/naming.py`)**
- [x] **Task 4: Web Server Backend (`app/server.py`)**
- [x] **Task 5: Web Management Dashboard UI (`app/templates/index.html`)**
- [x] **Task 6: Application Runner Script (`start.sh`) & Graceful Process Cleanup**

## Phase 2: PVArr Branding, Direct FFmpeg Optimization & *Arr Ecosystem Integration
- [x] **Task 1: Recorder Optimization & Proxy Fallback Logic (`app/recorder.py`)**
- [x] **Task 2: *Arr-Style UI & Favicon Integration (`app/templates/index.html` & `app/static/favicon.svg`)**
- [x] **Task 3: Post-Processing Engine (`app/post_processor.py`)**
- [x] **Task 4: Media Server & Notification Webhooks (`app/notifications.py`)**
- [x] **Task 5: Virtual IPTV / M3U Tuner Endpoint (`app/tuner.py` & `app/server.py`)**
- [x] **Task 6: Dockerization & Deployment Assets (`Dockerfile` & `docker-compose.yml`)**

## Phase 3: Documentation, AI Transparency & GitHub Release
- [x] **Task 1: Production README.md & AI Transparency Disclosure**
  - Feature list, architecture overview, API reference, quick start guides (CLI + Docker)
  - "Finding Your Stream URL" guide (DevTools / `detect-headers` / `curl` verification)
  - AI transparency callout banner + dedicated `## AI Genesis & Environmental Footprint` section
  - Env var table, endpoints, and file references verified against the actual codebase

- [x] **Task 2: Git Prep & Publish Automation (`scripts/publish.sh`)**
  - `.gitignore` verified (excludes `venv/`, `__pycache__`, test recordings, logs, `.env`)
  - `scripts/publish.sh` stages, commits, and provides exact GitHub remote attach + push commands

## Phase 4: Release Hardening
- [x] **Task 1: `LICENSE`** — The Unlicense (public domain, no attribution required)

- [x] **Task 2: Automated test suite (`test_pvarr.py`)**
  - 58 tests, stdlib `unittest`, no new dependencies
  - Covers: filename sanitisation + path-traversal rejection, output-path
    collision handling, storage rename/delete guards, M3U + XMLTV generation,
    dependency resolution, failover candidate parsing, FFmpeg argv construction
  - Two end-to-end remux tests encode a real 1s transport stream; they skip
    automatically when FFmpeg is absent
  - CI: `.github/workflows/test.yml` runs the suite on Python 3.9 / 3.11 / 3.12

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

## Phase 5: Failover Correctness
- [x] **Failover state-machine coverage (`test_pvarr.py`)** — 22 tests driving
      the real `_recording_loop` and `_stream_ffmpeg_process` against scripted
      fakes; no FFmpeg spawned, no real timeouts waited out.

- [x] **BUG FIX: force-failover latched forever (`recorder.py`)**
      `_force_failover_flag` was set by `POST /api/recordings/{id}/failover`
      and never cleared. Every subsequent candidate aborted on entry, so one
      press of the dashboard failover button cascaded through all remaining
      candidates and marked the recording `failed`. The button did the
      opposite of its name. Now consumed once, on the candidate being left.

- [x] **BUG FIX: status stuck on `failing_over` (`recorder.py`)**
      After any failover the dashboard reported `failing_over` for the rest of
      the recording, even while candidate 2 was recording normally.

## Phase 8: Stream Outcomes & Plex Live Tuner

### Three-state outcome refactor (closes the freeze + partial-write items)
- [x] `_stream_ffmpeg_process` returned a bool, so "did bytes arrive" stood in
      for "did the stream finish". A mid-recording stall **and** a non-zero
      FFmpeg exit after data both read as a clean finish, and the loop stopped
      instead of failing over — silently truncating the recording at the point
      of failure. Now returns `StreamOutcome`:
      `COMPLETED` (exit 0) / `FAILED` (no bytes) / `INTERRUPTED` (data, then
      stall or crash). Only `COMPLETED` ends the recording.
- [x] Exhausting all candidates *after* capturing footage now yields
      `completed_partial` rather than `failed`, so post-processing still runs
      and the capture is kept. This was the tradeoff that made the original
      decision look like a dilemma; tracking "did we ever get bytes" dissolves it.

### Plex live tuner — the integration had never worked
- [x] **`/api/recordings/{id}/stream` did not exist.** `tuner.py` advertised it
      in every M3U entry, so every channel Plex saw resolved to a 404. Now
      implemented as a tailing MPEG-TS feed: reads the file as it is written,
      drains cleanly when the recorder stops, and survives failover invisibly
      because failover appends to the same file. `?live=true` joins at the
      write head. Idle cap of 300s so a wedged recorder cannot pin a client
      open forever.
- [x] **EPG had no `<programme>` entries.** Plex will not display a channel
      with nothing in the guide. Each channel now gets a programme spanning a
      6-hour window from the recording start.
- [x] **EPG was not XML-escaped.** A filename containing `&` or `<` produced
      malformed XML that Plex rejects outright. Now escaped, and M3U attributes
      use `quoteattr` so a quoted filename cannot break the line.
- [x] **EPG listed stopped sessions the M3U filtered out**, leaving Plex with
      guide entries for channels it could not tune. Both now filter to running.
- [x] Channel titles drop the `.ts` extension.
- [x] `started_at` exposed in the status summary to drive programme times.

### Verified end to end (not just unit-tested)
- Recorded a real 30s MPEG-TS through the full pipeline; post-processor
  remuxed to MP4; `ffprobe` confirmed 30.02s of valid video.
- Against a throttled source simulating a live stream: playlist advertised the
  channel, the advertised URL was pulled for 8s exactly as Plex would, and
  `ffprobe` confirmed the received bytes were decodable h264 + aac.

## Decisions
- **Authentication: accepted risk, will not add.** Deployment is a trusted LAN
  behind a firewall. Documented prominently in the README instead. Revisit only
  if this is ever exposed — note that adding Basic auth would break the Plex and
  Emby tuner fetches unless `/live/*` is exempted or given a token parameter.

## Phase 6: Route Coverage & Path Containment
- [x] **Route integration tests** — 26 tests over every endpoint via FastAPI
      `TestClient`. Dev-only dep (`httpx`) in `requirements-dev.txt`; the group
      skips cleanly when absent so the core suite needs nothing extra.

- [x] **BUG FIX: `/api/status` was never routed (`server.py`)**
      `get_system_status()` was defined without an `@app.get` decorator. The
      dashboard polls `/api/status` on a timer to refresh active sessions, so
      the poll 404'd and the UI never updated during a recording.

- [x] **SECURITY FIX: unauthenticated arbitrary file read/delete (`server.py`)**
      `?dir_path=` was passed straight through to the library endpoints with no
      containment, so `GET /api/library/download/passwd?dir_path=/etc` served
      the file and `DELETE` removed it. No endpoint requires auth, so this was
      reachable by anyone who could reach port 8999. Now constrained to
      `recordings/` plus `PVARR_ALLOWED_DIRS`; filenames carrying a directory
      component are rejected outright.

## Phase 7: Relocation & Stability Audit
- [x] **Relocated to `~/pvarr`** — already an independent git root (own `.git`,
      zero files tracked by the old parent, not a submodule), so the move was a
      plain `mv`. Remote and branch preserved. No tracked file hardcoded the old
      path; only the venv did, and it was rebuilt.

### Bugs found and fixed
- [x] **Recordings never reached the mounted volume.** `docker-compose.yml`
      mounts `./recordings:/recordings`, but the app wrote to `/app/recordings`
      inside the image layer. Every containerised recording was lost on
      recreate. `PVARR_RECORDINGS_DIR` now drives the path and the image sets
      it to `/recordings`. Verified by running the container.
- [x] **detect-headers never worked for shell installs.** `check_deps` accepts
      `detect-headers.sh`, but `detect_candidate_headers` ran whatever it found
      through `sys.executable`. Upstream ships only the `.sh`, so every
      detection failed silently and fell through to the undetected path. Now
      dispatches on extension; the Dockerfile installs the `.sh` too.
      Container `check_deps` now reports all four dependencies OK.
- [x] **Notifications blocked the event loop.** `notify_recording_started` ran
      inline in the `async` start handler — up to three HTTP calls at
      `timeout=5`, so a slow webhook stalled the whole server for ~15s. Now a
      `BackgroundTasks` job.
- [x] **`output_dir` was unconstrained** — caller-supplied, `mkdir`'d and
      written to, i.e. arbitrary directory creation and file write. Same
      allowlist as the library endpoints.
- [x] **FFmpeg children were not reaped.** `_recording_loop` called
      `terminate()` with no `wait()`, accumulating zombies across failovers.
      Single `_reap_ffmpeg()` path now used everywhere.
- [x] **No shutdown hook.** Uvicorn drives shutdown through the ASGI lifespan
      and installs its own signal handlers, so `docker stop` could return with
      FFmpeg/hls-proxy children still alive. Added a lifespan shutdown that
      stops every active recorder.
- [x] Container ran as **root**; now a non-root `pvarr` user (verified
      `uid=1000` at runtime).
- [x] **No `.dockerignore`** — `COPY . .` was baking `.git/`, `venv/` and any
      recorded `.ts` into the image.
- [x] Three modules each called `logging.basicConfig()` at import; first import
      won and silently reconfigured the root logger. Centralised in
      `app/logging_config.py`. CLI also double-printed every line.
- [x] `start.sh` passed `--reload-dir` with no `--reload` (no-op); removed.
- [x] Unhandled exceptions returned bare 500s; added a structured handler.
- [x] `freeze_timeout` was unbounded; now validated 1–600.

### Audited and already correct — no change needed
- Webhook timeouts: all four `requests` calls already had `timeout=5`.
- Post-processor already verified `returncode == 0`, destination existence and
  non-zero size before declaring success, and checked the source before delete.
- Direct-FFmpeg-first with proxy fallback already behaved as documented.
- No duplicate output handles: a single `with open(..., "ab")` per attempt.

### Deliberately not done
- **Pydantic request models.** The dashboard posts `application/x-www-form-urlencoded`;
  converting the routes to JSON body models would break the UI for no
  correctness gain. Added field-level validation and structured errors instead.

## Phase 9: Session Lifecycle
- [x] **Recorder now tracks the post-processed file.** `get_filesize_mb()` and
      the status summary followed the original `.ts`, which post-processing
      deletes, so a finished recording showed `0.0 MB` and a `.ts` filename
      next to a perfectly good `.mp4`. `_on_complete` was discarding the remux
      result entirely; it now records `final_filepath`. Verified end to end:
      the dashboard reports `0.67 MB` and the `.mp4` name.

- [x] **`active_recorders` was write-only — an unbounded leak.** Nothing ever
      removed a finished session, so every recording stayed resident for the
      life of the process, each holding a 500-line log buffer and candidate
      state, and `/api/status` returned every session ever started. Finished
      sessions are now pruned to the newest `MAX_FINISHED_SESSIONS` (20);
      running sessions are never pruned.

- [x] **Proxy port climbed forever.** `port = 8090 + len(active_recorders) * 2`
      was derived from the *total* session count, so it rose monotonically and
      never reused a freed slot — eventually running past the valid port range
      on a long-lived server. Now allocates the lowest free port among
      *running* sessions.

### Investigated and closed — not a defect
- **Tuner stream surviving post-processing.** Previously listed as an open bug
  on the theory that deleting the `.ts` mid-stream would cut off an in-flight
  client. It does not: the handler holds an open file descriptor, and POSIX
  keeps it valid after unlink, so the client reads the recording through to
  completion. Verified empirically. The earlier entry overstated the problem.

## Phase 10: Paste-and-Record Header Detection
- [x] **Built-in stream probe (`app/probe.py`).** Finding a stream used to be a
      manual DevTools ritual: copy the m3u8, copy the `Referer`, copy the
      `User-Agent`, verify with `curl`, then type all of it into the form. The
      probe does that work server-side — resolves an m3u8 (or scrapes one out
      of a page), tries the plausible header combinations against the real
      origin, and keeps the first that returns an actual `#EXTM3U`. Covered by
      42 tests driving a scripted fake HTTP layer; no network in the suite.

- [x] **Segment verification.** A playlist that loads is not proof of a
      recordable stream — origins routinely serve the manifest to anyone and
      gate the segments. The probe fetches one segment (ranged, 2KB) with the
      same headers, so a session-gated stream shows as a red field in the
      browser instead of a recording that dies minutes in.

- [x] **Detection moved to connect time (`app/recorder.py`).** The recorder
      probes each candidate as it connects, not once at submit. Playlist tokens
      expire, so a failover an hour into a recording needs a fresh resolution.
      `detect-headers` is now the *second* choice, tried only when the built-in
      probe finds nothing — it still earns its place on pages that assemble
      their m3u8 in JavaScript, which needs a real browser.

- [x] **Cookie support end to end.** Cookies picked up during a probe are
      carried into FFmpeg's `-headers`, and are settable by hand. Session-gated
      streams were previously unrecordable without the proxy.

- [x] **Dashboard feedback (`app/templates/index.html`).** Each URL field probes
      on paste (debounced, newest-answer-wins) and reports what was found:
      playlist kind, variant count, headers required. Manual `Referer` /
      `User-Agent` / `Cookie` fields sit under each field, prefilled from the
      probe, and are sent as per-URL overrides keyed by URL rather than slot
      position.

### Deliberately not done
- **No SSRF allowlist on `/api/probe`.** The endpoint fetches a caller-supplied
  URL, which is the whole point of the feature, and PVArr is an unauthenticated
  LAN service where blocking private addresses would break legitimate local
  IPTV sources. Response bodies are capped at 512KB and non-http(s) schemes are
  refused, so it cannot be turned into a local file reader. Do not expose PVArr
  to the internet.

## Phase 11: Force-Failover & Completion Ordering

- [x] **BUG FIX: force-failover killed single-URL recordings (`recorder.py`,
      `server.py`).** The state machine was correct; the guard was missing.
      With no backup configured, the request advanced past the last candidate,
      which ends the recording -- so the button stopped a live capture and the
      API still answered `200 success`. `has_next_candidate` now gates it:
      `force_failover()` refuses and returns `False`, and the endpoint answers
      `400` naming the reason. Reproduced live against the real subprocess
      path before the fix (recording died at t=5s), and after (recording
      continued past t=11s).

- [x] **BUG FIX: dashboard showed "Stream 2 of 1" (`recorder.py`).**
      `current_candidate` was `index + 1`, and the index legitimately runs one
      past the end once candidates are exhausted. Clamped in the status summary.

- [x] **BUG FIX: the failover button looked dead even when it worked
      (`recorder.py`, `index.html`).** Three causes, all fixed:
      the loop only set `failing_over` after the current attempt unwound and
      held it ~1s against a 3s poll, so the state was never observed --
      `force_failover()` now sets it on the spot; the dashboard's single
      post-POST refresh fired before the recorder thread had reacted, so it
      repainted the *old* candidate -- it now re-polls at 0.5/1.5/3s; and a
      non-2xx reply was discarded silently, so the new refusal would have been
      invisible -- it is now surfaced. The button also greys out with a tooltip
      when no backup is configured.

- [x] **BUG FIX: Plex/Emby were told to scan before the remux existed
      (`server.py`).** `_on_complete` fired `notify_recording_finished` --
      which triggers the library refresh -- and remuxed afterwards. The media
      server therefore scanned while only the `.ts` was on disk, indexed a file
      the remux was about to delete, and did not see the finished `.mp4` until
      its next scheduled scan. The webhook also quoted the `.ts` name and its
      pre-remux size. Order reversed; the notification now reports the final
      file. Costs nothing: this already ran on the recorder thread, not the
      event loop. Verified end to end with a real 3s TS -- at scan time the
      `.ts` is gone, the `.mp4` is present and `ffprobe`-valid, and the
      announcement names the `.mp4`.

- [x] **Regression coverage.** 10 tests (195 -> 205). Nine fail against the
      pre-fix tree and pass after; the tenth guards behaviour that was already
      correct. The existing failover tests replaced `_stream_ffmpeg_process`
      with a scripted fake, so none of this was reachable by them.

## Phase 12: Capture-Loop Reliability  (sponsor-approved)

- [x] **BUG FIX: freeze detection could not fire (`recorder.py`).**
      `_stream_ffmpeg_process` read the FFmpeg pipe with `stdout.read(32768)`,
      which parks the loop in the kernel until a full 32KB has arrived. A
      source that stalled mid-buffer was therefore never noticed: the freeze
      timeout below the read was unreachable. Measured before the fix --
      `freeze_timeout_sec=5`, a child that wrote 1KB then hung, and the
      recorder sat on the dead source for the full 20s of the test without
      failing over. Now `select()` bounds the wait and `os.read` takes whatever
      has actually arrived. After the fix the same scenario failed over at
      ~12s (direct attempt, proxy retry, then the next candidate).

- [x] **BUG FIX: every recording longer than ~8 minutes wedged
      (`recorder.py`).** FFmpeg was spawned with `stderr=subprocess.PIPE` and
      that pipe was never read. FFmpeg writes a progress line to stderr, the
      pipe holds 64KB, and once it filled FFmpeg blocked writing to stderr and
      stopped producing video entirely. Not a stream fault, and no failover
      logic could have recovered from it.

      **Measurement, corrected.** The first estimate here (~124 B/s, "under ten
      minutes") came from the wrong configuration -- `-c:v libx264` with stderr
      redirected to a file, rather than the recorder's `-c copy` with stderr on
      a pipe. Re-measured properly over 60s each:

      | configuration | stderr rate | 64KB pipe fills in |
      | --- | --- | --- |
      | libx264, to file (the flawed original) | 68.6 B/s | 15.9 min |
      | libx264, to pipe | 68.6 B/s | 15.9 min |
      | `-c copy`, unthrottled | 51.5 B/s | 21.2 min |
      | **`-c copy`, realtime (`-re`) -- the real case** | **184.1 B/s** | **5.9 min** |

      File versus pipe makes no difference; realtime versus unthrottled makes a
      large one, because the progress line is emitted on a wall-clock timer.

      **Confirmed end to end, not extrapolated.** The pre-fix tree (`be14933`)
      was checked out into a worktree and run against a realtime `-c copy`
      source: video stopped at **t=465s (7m45s)** and never resumed. The same
      test against the fixed tree ran past that point without a stall.

      Fixed twice over, deliberately: `-nostats -loglevel error -hide_banner`
      cuts the source of the spam (measured 3717 bytes -> **0 bytes** over 30s),
      and a small daemon thread drains stderr continuously so the pipe cannot
      fill even if a stream does produce real errors.

      This one was masked by the freeze bug. Fixing freeze detection alone
      would have turned a silent hang into a failover cascade -- the same
      deadlock recurring on every candidate in turn.

- [x] **FFmpeg's own errors are now surfaced.** The stderr drain keeps the last
      15 lines in a bounded buffer. On a failed or interrupted attempt the tail
      is logged and stored in `candidate.last_error`, so a `403`, a `404` or a
      codec complaint reaches the dashboard instead of vanishing into an
      unread pipe. Nothing is attached to a clean completion.

- [x] **Byte counter now moves smoothly.** `bytes_written` advanced only in
      32KB steps, so the dashboard showed `0.00 MB` for the first several
      seconds of a low-bitrate stream.

### Cost of the change
One `select()` wakeup per 0.5s while a stream is idle, and one extra syscall
per read while it is flowing -- roughly 20/sec on a 5 Mbps stream, which is
noise. One daemon thread per recording attempt, blocked on a pipe read. No
additional disk writes. `select()` on pipes is POSIX; PVArr is Linux/Docker.

### Verified end to end
- Real FFmpeg through the real recorder for 60s: continuous monotonic growth to
  19.34 MB, no stalls, stderr thread exits cleanly on stop.
- 211 tests pass (was 195 at the start of Phase 11).

## Phase 15: Library Knew Only About `.ts`

Reported by the sponsor as "delete media errors -- seems it's looking for .ts
not .mp4". The delete failure was the visible edge of a larger problem.

- [x] **BUG FIX: finished recordings were invisible in the library
      (`naming.py`).** `list_recordings()` was `glob("*.ts")`. Post-processing
      remuxes to `.mp4` and deletes the `.ts`, so a recording disappeared from
      the library at the exact moment it succeeded -- the library could only
      ever show captures that were still running or had failed to remux.
      Now lists `.ts`, `.mp4` and `.mkv`, and ignores directories.

- [x] **BUG FIX: the reported delete error.** Stopping a recording refreshed the
      library after 1.5s -- before a real remux finishes -- so the list showed
      the `.ts` that was about to be deleted. Clicking delete on that stale
      entry `404`'d. The dashboard now re-checks at 1.5s, 5s, 15s and 30s while
      post-processing runs, and the listing shows the remuxed file once it
      lands.

- [x] **BUG FIX: rename forced `.ts` onto everything (`naming.py`).**
      `if not new_filename.endswith(".ts"): new_filename += ".ts"` turned
      `highlights.mp4` into `highlights.mp4.ts` -- a name that lies about the
      contents, and one Plex would mis-handle. A new name now inherits the
      file's existing container when it has no recognised extension of its own.

- [x] **BUG FIX: downloads always claimed MPEG-TS (`server.py`).** The
      `media_type` was hardcoded `video/MP2T`, so a remuxed `.mp4` downloaded
      with a Content-Type contradicting its contents. Now derived from the
      extension via `naming.media_type_for()`.

### Verified end to end
Real 2s TS, real FFmpeg remux: library went from `[]` after remux (pre-fix) to
`['NFL_Bears_vs_Packers.mp4']`; download returned `video/mp4`; rename to
`Highlights` produced `Highlights.mp4` rather than `Highlights.mp4.ts`; delete
returned 200 where it previously 404'd. 234 tests, up from 225.

## Phase 14: Cycling Failover & Manual Stream Selection

- [x] **BUG FIX: the candidate list was a one-way walk (`recorder.py`).**
      `current_candidate_index` was only ever `+= 1`; nothing reset or
      decremented it, and the loop exited once it passed the end. So there was
      no route back to candidate 1 after it recovered, and a blip that touched
      all three sources ended the recording outright -- even twenty minutes
      into a three-hour capture with every source healthy again a minute later.
      An expiring token, which is the most common failure here and resolves
      itself in minutes, was enough to trigger it. The index now wraps.
      Automatic and forced failover always shared this path, so both were
      affected identically.

- [x] **Bounded cycling.** Gives up after `max_cycles` (default 3) complete laps
      that produced no data. Any bytes at all reset the counter, so a long
      capture that fails over occasionally can never exhaust its budget. Backoff
      between fruitless laps escalates 5s / 10s / 20s, capped at 60s, so a set
      of genuinely dead origins is not hammered in a tight loop; within a lap
      the original 1s pause is unchanged.

- [x] **Manual switch to a specific candidate.** `POST /api/recordings/{id}/switch`
      (`candidate=1..3`, 1-based) and clickable candidate badges in the
      dashboard. Automatic failover deliberately only moves forwards -- see the
      decision below -- so this is the only way back to the primary.

- [x] **`has_next_candidate` re-derived.** It meant "not yet at the end of the
      list", which was only right while the walk was one-way. It now means
      "more than one candidate", since the last one cycles round to the first.
      The refusal shipped in 0.1.2 therefore narrows to genuinely single-URL
      sessions -- where forcing a failover would still end the recording -- and
      that protection is unchanged.

### Decision: no automatic return to the primary
Considered and rejected: periodically health-checking candidate 1 and switching
back to it while a backup is working fine. It means abandoning a *working*
stream for one that might work, and every switch puts a discontinuity in the
file. For a DVR that trades real footage for possible quality. The
primary/backup order is about what to try first, not a ranking to keep
restoring. Returning to an earlier candidate is a manual action instead.

### Verified
- Real subprocesses, three candidates, candidate 1 dead for the whole of lap 1:
  the recorder went 1 -> 2 -> 3 -> "Cycling back to Candidate 1 (lap 1 of 3)"
  and recorded. Pre-fix this path ended the recording.
- 225 tests (was 211). Three existing tests were rewritten rather than patched,
  because this change deliberately inverts their premise.

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

- [PENDING] **2. Session state persisted to `/config`.** All session state lives
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

- [PENDING] **3. Resume on restart/recreate.** On boot, read the session files
      and reconnect, appending to the same `.ts` (which is how failover already
      works). Bounded by a maximum gap -- past it, finalise instead of
      reconnecting -- and by a resume-attempt counter, so a recording that dies
      immediately cannot loop against `restart: unless-stopped`. Token expiry is
      already handled: the recorder re-probes each candidate at connect time.

- [PENDING] **4. Recording windows and duration caps.** A recording may carry an
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

## Still open

### Longer-term candidates
- A retention/cleanup policy for old recordings on disk.
- Integration coverage for the notification webhooks (currently mocks only).
- A headless-browser probe path so JavaScript-built m3u8 URLs work without the
  external `detect-headers`.
