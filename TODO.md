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

## Agent-team review of Phase 13 (2026-08-30)

Architect / Security / DevOps reviews of the persistence design, run before any
of it was written. Every claim below was re-verified independently.

### Blocking, and NOT caused by this work -- all fixed, 2026-08-31
These were live bugs found while reviewing the persistence design, not defects
introduced by it. All six are closed and verified; the notes are kept because
each one explains a constraint the next change has to respect.
- [x] **A fresh install cannot record at all.** `config/`, `recordings/` and
      `logs/` are untracked in git, so a clean clone has none of them. Compose
      bind-mounts all three, dockerd creates the missing host directories as
      **root:root**, and the container runs as uid 1000 -- so `naming.py`'s
      `record_dir.mkdir()` raises PermissionError on the first recording. The
      image-time `chown` cannot help: a bind mount grafts the host inode over
      the image's, and permission checks run against the host. Reproduced
      against the published image. This is why `config/` is root-owned here.
      **Fixed** in `docker-entrypoint.sh`: the container now starts as root,
      aligns the `pvarr` user to `PUID`/`PGID`, chowns the three mount roots
      non-recursively (a recursive walk of a multi-TB library on every boot is
      not acceptable), then `exec gosu`s to the unprivileged user. No root
      process survives into the app. If it is already non-root it cannot fix
      anything, so it checks writability and exits 1 with the exact `chown`
      command instead of dying mid-recording.
      **Verified** against a locally built image with all three mounts
      deliberately `root:root`: entrypoint reported "Fixing ownership", the app
      ran as uid 1000, wrote to all three, and the files landed as `1000:1000`
      on the host. Repeated with `PUID=1500` -- `usermod` path taken, files
      landed `1500:1500`. CI now builds the image and asserts both on every
      push.
- [x] **Remux and notification are skipped on every container stop.**
      `cleanup.py` registers a SIGTERM handler at import (`server.py` module
      scope), which overwrites uvicorn's and calls `sys.exit(0)`. The recorder
      thread is a daemon and `stop()` never joins it, so the completion block --
      remux, `final_filepath`, notify -- dies mid-flight. The lifespan shutdown
      hook therefore never runs either. **Demonstrated:** recorded 147 KB, sent
      SIGTERM, `.ts` left un-remuxed with no notification.
      **Fixed** in `app/cleanup.py`, rewritten: the handler no longer calls
      `sys.exit(0)`. It stops every recorder first (so remuxes run
      concurrently), then waits on them against one shared deadline via the new
      `StreamFailoverRecorder.wait_until_finished()`, then chains to whatever
      handler it displaced -- uvicorn's -- so the normal shutdown still happens.
      `PVARR_SHUTDOWN_TIMEOUT` (default 20s) bounds the wait; compose sets
      `stop_grace_period: 30s` so Docker does not SIGKILL first.
      **Verified** by re-running the script that demonstrated the bug: it went
      from `REMUX RAN: no` to `REMUX RAN: yes` with the marker file present.
- [x] **`/api/status` served live session cookies** in plaintext to anything on
      the LAN. `CandidateStream.to_dict()` included `cookie`. Verified with a
      real request. Consistent with "unauthenticated by design", but it meant a
      cookie was not a secret PVArr kept.
      **Decided: redact.** "Unauthenticated by design" is a statement about
      *PVArr's* data -- your recordings, your session list. It is not a licence
      to hand out a credential for the sponsor's paid subscription to anything
      that can open a socket. The two are not the same risk and should not
      share a policy.
      **Fixed:** `to_dict()` now reports `has_cookie: bool` and takes
      `include_secrets=False`; the value is returned only to callers that opt
      in -- the FFmpeg command builder, and session persistence when it lands.
      The dashboard never read the field (it fills its cookie box from the
      caller's own `/api/probe` response), so nothing in the UI changed.
      Four regression tests assert the token cannot appear anywhere in a
      serialised status payload.
- [x] **`config/` was not gitignored.** Once state lands there, `git add -A`
      would commit live cookies to a public repo. Fixed immediately.
- [x] **CRLF injection into FFmpeg `-headers` and into hls-proxy's
      channels.conf.** Values were concatenated unchecked; `probe.py` accepts a
      `referer=` from a third-party m3u8 query string and percent-decodes it, so
      a hostile page can supply a real CRLF. Persistence would have made a
      poisoned header permanent and replayed it every boot. Fixed: rejected
      (not stripped) at both sinks.
- [x] **No URL length cap on `/api/recordings/start`**, though `/api/probe` has
      one. Fixed.

### Design conclusions that changed the plan
- **Persistence belongs in a new `app/sessions.py`**, not in the recorder and
  not in `server.py`. The recorder stays a pure engine and gains one more
  injected callback alongside the existing log/completion/failover ones.
- **The gap must be measured from the `.ts` mtime, not the last transition.**
  Under transitions-only writing, a healthy three-hour recording's last
  transition is at t=0, so a naive gap check would finalise exactly the long
  recordings the feature exists to save.
- **`stop()` conflates "operator stopped" with "process going away"** and
  unconditionally sets `completed`. Persisting that means nothing ever resumes.
  It has to be split before resume can work at all.
- **Nothing may be written at shutdown**, because shutdown does not reliably
  run (see above). Whatever is on disk at an arbitrary instant must suffice.
- **Do not persist probe-derived headers or the resolved m3u8** except as
  diagnostics -- tokens expire, and storing them invites a future "skip the
  probe on resume" optimisation that reconnects with a dead token.
- **Re-validate `output_filepath` against the allowlist on read-back.** The
  likeliest trigger is not an attacker but allowlist drift: a stale file naming
  a directory the sponsor has since removed from `PVARR_ALLOWED_DIRS`.
- Rejected: one combined `sessions.json`; reusing `get_status_summary()` as the
  on-disk format; any "last seen alive" heartbeat field.

### Operational
- **Watchtower on this host recreates PVArr unattended at 04:00 daily** --
  `MONITOR_ONLY=false`, `CLEANUP=true`, no label filter, 10s grace. That is the
  exact scenario resume is for, happening on a schedule, mid-recording. Pin
  `PVARR_TAG` or add `com.centurylinklabs.watchtower.enable=false`.
- Container logs are unrotated; a resume crash-loop would fill the disk.
- `./logs:/app/logs` is a dead mount -- logging goes to stdout only.
- Cost of the write pattern: ~1-2 KB per transition, under 30 MB even for a
  pathological six-hour flapping session. Three to four orders of magnitude
  below the video it describes. Negligible, conditional on no timer writes.

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

## Still open

### Longer-term candidates
- A retention/cleanup policy for old recordings on disk.
- Integration coverage for the notification webhooks (currently mocks only).
- A headless-browser probe path so JavaScript-built m3u8 URLs work without the
  external `detect-headers`.

---

## Phase 16 — The guide says what is happening (2026-08-31)

humantodo line 2: "update the media guide for Plex so that it shows the name of
the file/stream as well."

- [x] **Programme descriptions were useless.** Every entry read `PVArr live
      recording <uuid>`. The uuid is not something the sponsor can act on, and
      the one question you actually have mid-event -- *which feed am I watching
      right now?* -- had no answer anywhere in Plex.
      Now: `<title>` is the recording name, `<sub-title>` is the live source
      (`Primary`, `Backup 1`), `<desc>` carries the filename being written, the
      failover position (`2 of 3, failover armed`) and the start time.
- [x] **Channel titles only stripped `.ts`.** `current_filepath` follows the
      remux, so a session whose post-processing had finished was advertised as
      `Bears vs Packers.mp4`. Now strips any container in
      `RECORDING_EXTENSIONS`.

### Decision: no live counters in the guide
Considered and rejected: putting elapsed time and megabytes-written into the
programme description. Plex caches XMLTV and refetches on its own schedule, so
a counter baked in there is wrong within seconds of being fetched. A number
that is visibly stale reads as a bug. The description carries only facts that
hold for the life of the recording; live figures stay on the dashboard, which
polls. A test asserts no counter leaks back in.

### Notes for the rebroadcast work (line 3)
`_channel_title`, `_source_name` and `_programme_description` all take a plain
session dict and never touch a recorder object. A rebroadcast-only channel that
presents the same keys will get a correct guide entry for free -- but
`output_filename` will be meaningless for one, since nothing is being written.
That is the first thing the rebroadcast design has to answer.

## Agent-team review of rebroadcast mode (2026-08-31)

Architect / Security / DevOps, briefed on humantodo line 3 ("an option to not
record and just rebroadcast"). The reviews surfaced six live bugs that have
nothing to do with rebroadcast; those were fixed first and are recorded here.
The feature itself is NOT built -- the design decision is still with the
sponsor.

### Live bugs found and fixed
- [x] **Proxy port blocks overlapped.** `_allocate_proxy_port` stepped by 2,
      but `start_proxy` binds `base_port + candidate_index` and a session holds
      up to three candidates -- so session A's third candidate bound the port
      already handed to session B, and B's proxy failed to start. Found
      independently by Architect and Security. Fixed: a shared
      `PROXY_PORT_STRIDE = 4`, and the index is taken modulo the stride so a
      session can never escape its own block.
- [x] **The live log view froze after 500 lines.** `log_history` is trimmed,
      but the SSE endpoint tracked a plain index into it, so once trimming
      began `len(history) > last_sent_idx` was never true again. Silent: no
      error, the pane just stopped. Fixed with a monotonic sequence number and
      `logs_since()`; a reader further behind than the buffer is deep gets what
      is still held rather than nothing.
- [x] **hls-proxy's pipes were never drained.** Spawned with `stdout=PIPE,
      stderr=PIPE` and nothing reading either. Exactly the defect that stopped
      FFmpeg dead at ~7 minutes, in the module next door. stdout is now
      discarded, stderr drained to a bounded tail, and a proxy that exits
      immediately now says why instead of failing silently.
- [x] **`stop_proxy` left zombies.** `kill()` with no `wait()`, one per
      failover. `_reap_ffmpeg` documents this exact defect and does it right;
      the proxy path was simply missed.
- [x] **Failover backoff was not interruptible.** `time.sleep(delay)` with
      delay up to 60s, against a 20s shutdown budget and a 30s
      `stop_grace_period` -- a stop landing in a backoff was SIGKILLed, losing
      the remux that the shutdown fix exists to protect. Now
      `_stop_event.wait(delay)`.
      Note: the test fixture had patched `time.sleep` to stay fast. That patch
      silently stopped working, and the suite went from 1s to 120s. The fixture
      now zeroes `_failover_delay` instead, and a test asserts a stop during a
      30s backoff returns in under 5s.
- [x] **No URL scheme validation, no FFmpeg protocol whitelist.** Verified by
      Security against ffmpeg 6.1.1: `file://`, `concat:` and `tcp://` were all
      reachable from `/api/recordings/start`, and captured bytes are readable
      back through the stream and download endpoints. Bounded in practice --
      ffmpeg's mpegts demuxer drops non-media content, and `file:` segments
      under an http parent are already blocked by ffmpeg's own default
      whitelist -- but it is a cheap fix. Now rejected at the API boundary and
      pinned with `-protocol_whitelist http,https,tcp,tls,crypto,data`.
- [x] **Proxy `channels.conf` held a tokenised URL and was never deleted.**
      Written under `recordings/.proxy_conf/`, which is on the mounted volume.
      Now removed in `stop_proxy`.
      **Action for the sponsor:** one pre-existing file is still on disk at
      `recordings/.proxy_conf/channels_893cc63a.conf`. It is gitignored and was
      never committed, but it holds a real stream URL. Delete it when
      convenient -- not doing so myself, per the escalation rule on config
      files.

### Still open, deliberately not fixed
- [ ] **No cap on concurrent sessions or on readers per session.** Every tail
      reader goes through `asyncio.to_thread` onto the default executor
      (`min(32, cpu+4)` workers), so ~32 active readers starve `/api/probe` and
      the shutdown hook. Not urgent for a single-sponsor LAN install, and the
      right fix depends on the rebroadcast decision below.
- [x] **URL tokens still reach the logs.** Fixed 2026-08-31 -- see the
      redaction pass below.
- [x] **Notifications ship the full primary URL** to Discord/Telegram. Fixed
      2026-08-31; it was a parameter mismatch, not a formatting choice.
- [ ] **`_failover_delay` and `max_cycles=3` are recording semantics.** A
      permanent channel should retry forever, not give up after three laps.
- [ ] **The healthcheck cannot see a wedged session.** Both the Dockerfile and
      compose healthchecks hit `/api/status`, which returns 200 for a session
      whose `bytes_written` has been frozen for an hour.
- [ ] **hls-proxy's bind address is unverified.** It is cloned at build time,
      not vendored. If it binds 0.0.0.0 it is a second unauthenticated relay on
      8090+. Check before rebroadcast ships.

### The rebroadcast design decision -- SPONSOR INPUT NEEDED
Architect and DevOps agree on the shape: keep `StreamFailoverRecorder` and swap
its *sink*. The failover machinery never touches the file -- only three lines
inside `_stream_ffmpeg_process` do -- so a sink abstraction reuses 100% of the
cycling, backoff and freeze detection and duplicates none of it. Rejected: a
separate recorder class (duplicates ~200 lines of the loop this project has
spent its whole history debugging).

They disagree on the buffer, and this is the real decision:
- **Architect** wants an in-memory hub: a bounded per-subscriber queue, evict a
  slow client rather than drop chunks from the middle of its stream (which
  hands Plex a corrupt transport stream).
- **DevOps** says in-memory is the wrong call on this host class and gives the
  number: at 10 Mbps a stalled client accumulates 75 MB/min, and with no
  `mem_limit` in `docker-compose.yml` the OOM killer takes uvicorn -- PID 1 --
  killing every concurrent *recording* too. On a 4 GB NAS that is ~27 minutes
  from one wedged Plex client to losing the game you were recording.

Lead Engineer's call, for the sponsor to confirm: **DevOps wins on the buffer.**
The same 75 MB as a capped ring file on disk is page cache, which the kernel
reclaims under pressure instead of OOM-killing, and is served from RAM anyway.
It also keeps the existing tail-the-file fan-out, which already works. Architect
was right that a rotating file breaks a reader holding an fd across truncation
-- so it needs a fixed-size ring written in place, not log-style rotation.

Two more constraints, from Architect, that any implementation must respect:
- Chunks are 65536 bytes, which is not a multiple of 188, so chunk boundaries
  are not TS-packet-aligned. A late joiner must start on a 188-byte boundary or
  it gets a partial packet before its first PAT/PMT.
- Each failover spawns a fresh FFmpeg with its own timeline, so there is a PTS
  discontinuity at every switch. Invisible in a DVR file; it is exactly where a
  live client drops. Untested -- must be tried on icebox before promising 24/7.

### Sponsor decisions on rebroadcast (2026-08-31)
- **On-disk ring buffer confirmed.** In-memory fan-out rejected; the OOM risk
  to concurrent recordings decided it.
- **Persistence lands first.** Rebroadcast is blocked on Phase 13 items 2 and
  3, because a permanent channel that vanishes on restart is not permanent.
- **PVArr does not promise 24/7 recording.** So the PTS discontinuity at each
  failover is low priority *as a live-streaming concern*. It stays open only
  to the extent that it affects the recorded file -- see below.
- [x] **Checked: the failover discontinuity does NOT affect the finished file.**
      Each failover spawns a fresh FFmpeg with its own timeline and appends to
      the same .ts. The live-client drop is now explicitly out of scope, but
      the same discontinuity sits in the middle of every multi-candidate
      recording, where it could plausibly affect the remux to .mp4, seeking and
      scrubbing in Plex, or the reported duration. That is an existing-recording
      concern, not a rebroadcast one, so it is worth an hour to establish
      empirically. Test locally, do not speculate.

#### Measured, 2026-08-31 (two 10s clips, separate FFmpeg runs, concatenated
#### as .ts then remuxed exactly as post_processor does)
- The raw `.ts` reports **10.02s for 20s of content**. ffprobe reads the
  container timeline, and the second FFmpeg restarts at zero, so everything
  after the failover is invisible to a duration probe.
- The remuxed `.mp4` reports **20.03s**, and seeking to 15s -- inside the
  second half -- works. FFmpeg re-times the discontinuity on the way through.
- `_on_complete` always remuxes to mp4 and deletes the source, so **the file a
  user actually keeps is correct**. The sponsor's call to deprioritise stands.
- Residual, low: if the remux ever *fails*, the kept `.ts` underreports its
  duration and Plex will show the wrong length. Worth a guard eventually --
  not worth work now.

## Phase 13 items 2 & 3 -- session persistence and resume (2026-08-31)

Built after the sponsor confirmed persistence lands before rebroadcast.

New module `app/sessions.py`. The recorder was NOT touched beyond a stop
reason: it stays a pure capture engine, and `server.py` owns the store and
calls it at transitions. Persistence that reaches into the capture loop is
persistence that stalls the capture loop.

- One JSON per session under `<config>/sessions/`, written **on state
  transitions only** -- start, failover, finish. A clean three-hour recording
  writes twice. Ongoing disk writes are zero.
- **No progress counters are persisted.** Bytes and elapsed time are recovered
  by `stat()`ing the `.ts` at resume. A counter in a file disagrees with reality
  the moment the process dies, which is exactly when it is read.
- Written 0600 in a 0700 directory. The files hold stream URLs and the session
  `Cookie` -- a resume against a gated stream cannot work without them.
- Atomic write via `mkstemp` + `os.replace`. The likeliest moment to be
  interrupted is a shutdown, which is exactly when this file is being written.
- Store failure never propagates. On an unwritable directory it disables
  itself, warns once, and every call becomes a no-op -- a running recording
  must not die because its state file cannot be written.

### `stop()` now takes a reason, and this was the crux
It previously set `status = "completed"` unconditionally. Persisting that meant
a restart read "completed" and nothing ever resumed. An **operator** stop
finishes the recording: remux, notify, forget the session. A **shutdown** stop
means the process is going away with the recording still wanted: keep the
`.ts`, keep the record, decide at boot.

That reverses part of the v0.1.4 fix on purpose. v0.1.4 made a container stop
remux before exiting; remuxing now would delete the file the resume needs. The
remux still runs on shutdown **if persistence is unavailable**, since then there
is nothing to resume from -- better a finished file than an orphaned one.

### Three fates at boot, decided by `resume_decision()`
Kept a pure function so the policy is testable without a filesystem, a recorder
or a server.
- **resume** -- file exists, has content, was written recently, attempt budget
  intact. Reattach and keep appending.
- **finalise** -- footage worth keeping but too cold to reconnect (past
  `PVARR_MAX_RESUME_GAP`, default 300s) or `PVARR_MAX_RESUME_ATTEMPTS`
  exhausted. Remux and notify. A session that dies, resumes and dies again is
  reproducibly broken, not unlucky.
- **discard** -- nothing on disk to keep.

**The gap is measured from the `.ts` mtime, not the last transition.** Under
transitions-only writing a healthy three-hour recording's last transition is at
t=0, so a gap measured from that would finalise exactly the long recordings the
feature exists to save. There is a test for this specific trap.

### Verified end to end
Real recorder, real subprocess, real bytes. Captured 327,680 bytes; ran
`stop_all()` exactly as `docker stop` does; confirmed **no remux ran** and the
state file survived; cleared all in-process state; called `resume_sessions()`.
The **same file** grew to 655,360 bytes. One file on disk, not two.

290 tests (was 271).

### Found while building
`config/` on the dev box is root-owned, so the store disabled itself on first
run -- the exact failure the Phase 13 note predicted. It degraded correctly
rather than taking the app down. In the container the entrypoint already chowns
`/config`, so this only bites outside Docker; both cases are now in the README
troubleshooting section. `PVARR_CONFIG_DIR=/config` is now set in the Dockerfile
and compose file.

### Not done, deliberately
Item 4 (recording windows, `PVARR_MAX_HOURS=6`) is still pending. Rebroadcast is
now unblocked.

## Phase 16b -- Rebroadcast without recording (2026-08-31)

humantodo line 3, built after the sponsor confirmed the on-disk buffer and
after persistence landed.

### What was built
`app/ringbuffer.py` -- a fixed-size file written in a circle, with many
independent readers. `StreamFailoverRecorder` gained a *sink*: bytes go either
to a growing file (recording) or to a ring (rebroadcast). The capture loop, the
cycling failover, the backoff and the freeze detection are untouched and shared
between both modes, which was the whole point of the sink approach -- there is
no second copy of the loop this project has spent its history debugging.

### Why a file and not memory
Settled by DevOps' number and confirmed by the sponsor. At 10 Mbps a client
that connects and stops reading accumulates ~75 MB/min; there is no `mem_limit`
in `docker-compose.yml`, so the OOM killer takes uvicorn (PID 1) and every
concurrent *recording* dies with it -- about 27 minutes from one wedged Plex
client to losing the game. The same 75 MB as a file is page cache: reclaimed
under pressure, and served from RAM anyway.

### Why written in place, not rotated
Architect's objection, and it holds. Log-style rotation truncates or renames
the file out from under a reader holding an open fd, which hands it zero-fill
in the middle of a transport stream. A fixed file written in a circle never
changes size, so a reader's descriptor stays valid for the life of the channel.

### Packet alignment
MPEG-TS is 188-byte packets and a decoder starting mid-packet produces garbage
until it resynchronises. Capacity is forced to a whole number of packets and
positions derive from a monotonic absolute offset, so `offset % 188` survives
every wrap. A lapped reader is skipped forward to the oldest data still held,
rounded UP to a packet boundary -- rounding down would point at bytes already
overwritten. There is a test asserting exactly this.

### Decisions worth keeping
- **Viewers join at the live edge, never at the start of the buffer.** Plex is
  tuning a live channel; replaying a minute of history would put every viewer a
  minute behind and further behind on every reconnect.
- **The writer never blocks on a reader.** A stalled client is lapped and
  resynchronises. Backpressure from a viewer to the capture thread would let
  one bad client stall the upstream pull for everyone.
- **A channel always resumes after a restart, ignoring the file check and the
  attempt limit.** Its buffer is deleted at shutdown by design, so the
  recording rules would discard every channel on every restart. There is no
  restart-loop risk: a channel whose upstream is genuinely dead ends itself
  through `max_cycles` and is removed that way.
- **The guide says "Live rebroadcast -- not being recorded".** Saying
  "Recording to ..." on a channel that keeps nothing would be a promise PVArr
  is not making.

### Verified end to end
Real recorder, real subprocess, real ring, real uvicorn on a real socket, three
concurrent HTTP viewers:
- **3/3 viewers served, 1 upstream pull.** This is the claim that matters --
  re-fetching a session-gated stream per viewer is how an account gets
  throttled.
- All three landed at the live edge (counters 167-178) with ordered, valid data.
- `recordings/` empty, library empty, `output_filename` empty.
- Buffer deleted on stop; session record forgotten.

Note: the FastAPI `TestClient` cannot serve three simultaneous streaming reads
from threads -- it drives ASGI through a single portal -- so the fan-out half of
that test runs against a real uvicorn. Worth remembering before concluding a
streaming endpoint is broken.

323 tests (was 305).

### Still open
- [ ] The failover PTS discontinuity is where a live client drops. Out of scope
      for recordings (measured: the finished .mp4 is fine) but it DOES apply to
      a rebroadcast viewer. Untested against a real player. PVArr does not
      promise 24/7, so this stays low -- but it is the first thing to look at
      if the sponsor reports Plex dropping a channel at a failover.
- [ ] No cap on concurrent channels or on viewers per channel. Each viewer read
      goes through `asyncio.to_thread` onto the default executor
      (`min(32, cpu+4)` workers).
- [ ] The healthcheck still cannot see a wedged channel.


## Release v0.2.0 (2026-08-31)  [COMPLETED]

First minor bump of the series. Sponsor-approved. The version level is
deliberate: 0.1.x had been a recorder that could only record, and rebroadcast
changes what PVArr *is* rather than adding to what it already did. Backward
compatible in every respect -- existing recordings behave identically, and the
two new settings (`PVARR_BUFFER_MB`, `PVARR_BUFFER_DIR`) have working defaults,
so an upgrade needs no action from a user.

Carries two capabilities the sponsor did not have at v0.1.3:
- **Recordings survive a restart** (Phase 13 items 2 & 3) -- a `docker restart`,
  a Watchtower update or a host reboot no longer orphans an in-flight `.ts`.
- **Rebroadcast without recording** (Phase 16b) -- a live channel for
  Plex/Emby/Jellyfin that keeps nothing on disk.

Plus the guide naming work (Phase 16) and the six bugs from the agent-team
review of Phase 13.

323 tests green at the tag.

### Upgrade note
`PVARR_CONFIG_DIR=/config` must be a mount the container user can write, or
session persistence disables itself and says so once in the log. It degrades
cleanly -- recordings still run, they just do not survive a restart. This bit
the dev box: `config/` was root-owned and the store correctly disabled itself.
See PUID/PGID in the README.

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

## Version badge lied about the running build (2026-08-31)  [COMPLETED]

**Symptom.** The sponsor pulled v0.2.0 onto icebox and the dashboard header
still read `v1.0.0`. Indistinguishable from "the pull did not take" — the worst
possible ambiguity at the exact moment you are trying to confirm which build
you are testing.

**Cause.** `app/templates/index.html:89` carried the literal string `v1.0.0`,
hardcoded from the first commit and never wired to `__version__`. It has
therefore been wrong for every release in the 0.1.x series; nobody noticed
because it was wrong in a stable way. `scripts/publish.sh` bumps
`app/__init__.py` and CI checks the tag against it, but neither can reach a
number baked into markup.

**Fix.** `__version__` is registered as a Jinja global
(`templates.env.globals["pvarr_version"]`) rather than threaded through the one
route's context dict — a global cannot be forgotten by a route added later,
which is how this class of bug returns. The template renders
`v{{ pvarr_version }}`.

`/api/status` now also reports `version`, so "what is icebox actually running?"
is answerable with `curl` without trusting a number rendered in a page. That is
the check the sponsor actually needed and did not have.

**Proven.** Reintroduced the hardcoded literal and confirmed the new guard
fails with the offending file and line named, then restored it. Three tests
fail with the bug present, all pass without it.

Seven tests added (330 total, was 323):
- the badge renders the real `__version__`
- the page does not contain a stale `v1.0.0`
- **no template anywhere hardcodes a `vX.Y.Z` literal** — the regression guard;
  this is the one that would have caught the original bug
- `/api/status` and `/openapi.json` both report `__version__`
- `__version__` is semver, and the assignment line still matches the regex
  that `scripts/publish.sh` and the CI tag guard both sed. If that line is ever
  reformatted the release script silently fails to bump and CI's tag-vs-code
  check reads an empty string.

## Deleting a live recording silently destroyed it (2026-08-31)  [COMPLETED]

Found by the sponsor on icebox during v0.2.0 testing. The most damaging bug
found in this project so far: it destroys footage and reports success.

**Symptom.** Candidate 1 failed, candidate 2 connected and ran for four
minutes, elapsed time climbed, but recorded size stayed at 0 MB and no file
appeared in the library. `ls` of the recordings directory showed only
`.nfs000000000e3b01ab00000001`. A manual force-failover to candidate 3 made a
proper `.ts` appear and data start filling.

**Root cause, confirmed in the container log:**

    "DELETE /api/library/2026-08-31_MLB_Yankees_vs_RedSox_1080p.ts" 200 OK

issued mid-recording against the running session's own output file. The library
delete endpoint unlinked it without ever asking whether a recorder was writing
to it, and answered 200.

**Why it was silent.** An append handle keeps working perfectly after its file
is deleted — writes succeed, the offset advances, nothing raises. The bytes go
to an inode with no name and are freed when the handle closes. The directory is
NFS-exported from a QNAP, so it showed as a silly-rename (`.nfsXXXX`); on a
local filesystem there would have been nothing to see at all.

Three separate mechanisms all failed to notice, each for a defensible reason:
- **Freeze detection** watches `last_write_time`, updated on every *successful*
  write. The writes were succeeding. The stream looked perfectly healthy.
- **`get_filesize_mb()`** stats the path, not the handle. Path gone -> 0.0.
- **The dashboard** renders only `filesize_mb`, never `bytes_written`, so the
  two never visibly disagreed.

**Why candidate 3 "fixed" it.** Every attempt reopens with `open(path, "ab")`,
which recreates a missing file. The manual failover made a fresh, correctly
named file. That behaviour is diagnostic of nothing else.

### Fix, in two halves

**1. PVArr refuses (`server.py`).** `_active_output_paths()` maps every live
recorder's `output_filepath`, `current_filepath` and `final_filepath` to its
session id; `_refuse_if_recording()` raises **409** from both the delete and
the rename endpoint. Rename is included because renaming out from under a
handle strands the recording writing to a path nothing will look at. A
rebroadcast channel blocks nothing — it keeps no file.

**2. PVArr notices anyway (`recorder.py`).** Half 1 cannot help when something
*outside* PVArr removes the file, which on a QNAP export is a real scenario —
File Station, SMB, a cleanup cron, another *arr tool. The new `_FileSink`
carries `is_intact()`, comparing the inode of the open handle against the inode
at the path, rate-limited to every 15s alongside the disk guard.

**`st_nlink == 0` is the wrong test and there is a test asserting so.** A
silly-rename is a *rename*, so the link count stays 1 and a link-count check
passes happily on exactly the case this exists to catch. Only the inode
comparison works.

On detection: log loudly, recreate the file, continue. After
`MAX_OUTPUT_REOPENS` (3) it stops with status `aborted_output_lost` rather than
looping forever against something that keeps deleting the file. `_RingSink`
answers `is_intact() -> True` so the capture loop never branches on sink type.

### Proven
Unit tests plus a real-recorder end-to-end run (`e2e_delete_live.py`): deleted
the `.ts` from under a live capture, and the recording recovered —

    file recreated      : True
    bytes on disk after : 1212416      (not a phantom)
    reopen count        : 1
    still recording     : True
    logged loudly       : True

The same script demonstrates the old behaviour for contrast: write-after-unlink
raises nothing and the path does not exist.

17 tests added (347 total, was 330), including the silly-rename case and a
guard that a broken sink can never take a recording down.

### Still open from the same logs
- [x] **hls-proxy 404s on `cand_0`.** `http://127.0.0.1:8090/channel/cand_0:
      Server returned 404 Not Found` at 12:14:42, 12:21:00 and 12:30:17.
      Fallback mode has never once worked for candidate 1 in these logs.
      Resolved in "Candidate 1's 404s" below (v0.2.3): the channel mode was
      keyed off the referer, so the proxy scraped a playlist as an HTML page.
- [x] **Anti-leech decoy segments.** Candidate 1's playlist lists segments
      disguised as TikTok CDN image URLs
      (`...tplv-tiktokx-origin.image ... is not in allowed_segment_extensions`).
      FFmpeg refuses them by extension. Resolved in v0.2.3 -- and note the
      guess recorded here was **wrong**: `-allowed_extensions ALL` does nothing
      on the shipped build. Measurement inside the image found
      `-extension_picky 0`, and the flags are now probed per binary.
- [x] The dashboard still never shows `bytes_written`. Had it been beside
      `filesize_mb`, the disagreement would have been visible immediately.
      Shipped in v0.2.2 as *Captured*, with *On Disk* going amber on a
      disagreement.

## Release v0.2.1 (2026-08-31)  [COMPLETED]

Patch. Sponsor-approved. One bug, but the most damaging one found so far:
deleting a recording from the library while it was still running destroyed the
footage and returned 200 OK.

Anyone on v0.2.0 has a delete button that eats live recordings, which is why
this did not wait for other changes to accumulate.

- Delete and rename now return `409` for a file a live recorder owns.
- The capture loop detects its output file vanishing (inode comparison against
  the path, every 15s), recreates it, and continues; three strikes stops with
  `aborted_output_lost`.
- The dashboard version badge shows the real `__version__` instead of the
  hardcoded `v1.0.0` it had carried since the first commit, and `/api/status`
  reports the running version so it can be confirmed with `curl`.

347 tests green at the tag. Nothing to do on upgrade.

## Dashboard honesty pass (2026-08-31)  [COMPLETED]

Two things the sponsor hit while testing v0.2.1 on icebox, plus the
`bytes_written` item that had been sitting in "Still open" since the
delete-while-recording bug.

### 1. "Completed" was shown while the remux was still running
**Symptom.** Stopped a recording; status read `completed` but the dot kept
pulsing green.

**Cause.** `stop()` sets `status = "completed"` immediately, but
`on_completion_callback` -- the remux -- then runs on the recorder thread and
`is_running` is not cleared until it returns. The log shows the window: stop at
12:39:18, `Remux successful` at 12:41:47. Two and a half minutes of
`completed` + green pulse, with no `.mp4` in the library the whole time.

Both halves were half right, which is why it looked merely cosmetic: the
capture *had* finished, and the thread *was* still working. Greying the dot
would have been the wrong fix -- it would have claimed done while the remux ran.

**Fix.** A real `post_processing` status for the duration of the callback, with
the resolved final status restored in a `finally`. An `aborted_no_space` or
`aborted_output_lost` session still comes out the far side with its own status
intact, and a callback that raises no longer strands the session.

### 2. Finished sessions cluttered the dashboard
Collapsed, not removed. A finished session still holds its log history, and
that history is the most useful thing in the app right after a recording ends
-- it is what diagnosed the delete bug. Removing finished sessions to tidy the
page would have thrown away the evidence.

Live sessions (including post-processing) render in full; finished ones become
a one-line row under *Recently Finished* that expands to its event log. The
header dot now keys off live sessions rather than "any session exists", which
is the specific reason a stopped recording kept blinking.

### 3. `bytes_written` is on screen
It was in `/api/status` all along and the page rendered only `filesize_mb`, so
when the two disagreed there was nothing visible to show it. Four minutes of
footage were lost to a discrepancy the dashboard already had the data to
display.

*On Disk* and *Captured* now sit side by side, and On Disk turns amber when
Captured is climbing while the file is not -- the signature of writing into a
deleted file. Scoped to `status === 'recording'` only: during post-processing
the remux has already removed the `.ts`, so on-disk is legitimately 0 and
warning there would cry wolf on every successful recording.

### Verified
357 tests (was 347). The Alpine helpers cannot be exercised by the Python
suite, so the state machine was run directly in node against five sessions
(recording, post-processing, completed, aborted, and one writing into a hole)
and each produced the right dot, colour and warning. That run is what caught
the post-processing false positive. `node --check` on the extracted inline
script guards against a syntax error taking the whole page blank -- something
no Python test would notice.

## README intro pass (2026-08-31)  [COMPLETED]

humantodo lines 5-8, folded in ahead of the next release. Doc-only.

- **Dropped "Default port: 8999" from the intro.** It was duplication: the port
  is already in Quick Start and in the `PORT` row of the configuration table.
  The Quick Start line now says outright that 8999 is the *default* and points
  at `PORT`, so nothing is lost by removing it from the top.
- **Strengthened the pronunciation note.** The sponsor's actual worry was not
  people spelling out *pee-vee-ay-arr-arr* — it was the flat *pee-vee-ar*, said
  like the letter. The Arr must be growled, pirate-style. The note now names
  that as the real offence and closes in the sponsor's own words: it is always
  **peevee ARRRR, matey**. Thank y'arrrrr.

Not shipped on its own; rides with the next release.

## v0.2.1 validated on icebox (2026-08-31)

Sponsor ran the full plan against real streams on the QNAP-backed NFS volume.
All four passed. Recording it here because two of these were only ever proven
on the dev box, and one was never proven at all.

1. **Delete guard.** Deleting or renaming a file a live recording is writing to
   is refused; the file survives; unrelated library files still delete. The
   guard did not turn the library read-only.

2. **External delete, on real NFS.** ***The important one.*** Deleting the `.ts`
   from the QNAP side — outside PVArr, where the 409 cannot help — is detected,
   the file is recreated, and the recording continues. This was verified locally
   against ext4 only; NFS silly-rename semantics on a real export were exactly
   where the inode check could have been wrong, and they are not. The reasoning
   behind rejecting `st_nlink == 0` in favour of the inode comparison now has
   field evidence, not just a unit test.

3. **Rebroadcast.** First clean end-to-end run against real streams — both
   previous attempts were eaten by the delete bug before they got anywhere.
   Channel serves, guide says it is not being recorded, nothing kept on disk.

4. **Guide naming.** Plex Info shows the recording name and the feeding
   candidate.

### Still not proven
- [ ] **PTS discontinuity across a failover, seen by a live rebroadcast
      viewer.** The channel itself works; what has still never been observed is
      a failover *while a client is watching it*. That remains the first thing
      to look at if Plex ever drops a channel mid-game. Recordings are
      unaffected and measured — only the live viewer path is open.

## Release v0.2.2 (2026-08-31)  [COMPLETED]

Patch. Sponsor-approved. No change to how recording works — this is the
dashboard telling the truth, plus a README pass.

Everything in it came out of the sponsor's v0.2.1 test session:
- **`post_processing` status.** A stopped recording no longer reports
  "completed" beside a pulsing green dot while the remux is still running and
  no `.mp4` exists in the library yet.
- **Finished sessions collapse** to a one-line row that expands to its event
  log, instead of sitting at the top of the page looking active. Collapsed
  rather than removed: the log history is the evidence, and it is what
  diagnosed the delete-while-recording bug.
- **`bytes_written` is on screen** as *Captured*, beside *On Disk*. On Disk
  turns amber when the two diverge — the signature of writing into a deleted
  file, which was invisible before despite the API carrying both numbers all
  along.
- **README intro pass** (humantodo lines 5-8): the duplicated port line is
  gone, and the pronunciation note now defends against the mispronunciation
  that actually happens.

357 tests green at the tag. Nothing to do on upgrade.

## Candidate 1's 404s: two bugs, neither of them the stream (2026-08-31)  [COMPLETED]

Sponsor asked whether the stream was down or the headers were wrong. Neither.
The stream was healthy, both tokens were valid, and header detection was
correct. Both failures were ours.

### Was the stream down? No.
Decoded from the failing URLs in the log: the segment token `x-expires`
expires 2026-09-01T00:00:00Z and the playlist path token 2026-08-31T20:43:06Z
-- 11.5 and 8.2 hours *after* the failure. The probe also succeeded
("Probe resolved Candidate 1: media, headers none"), so the origin was serving
a playlist. Nothing was expired and nothing was down.

### Bug 1 — the proxy fallback has never worked for a stream needing no Referer
`start_proxy` chose the hls-proxy channel mode with
`mode = "literal" if candidate.referer else "direct"`.

The referer decides nothing of the sort. In hls-proxy, **literal** means "this
URL *is* the playlist"; every other mode makes it fetch the URL as an HTML
page, look for an `<iframe>`, then look for an m3u8 inside that. So a stream
needing no `Referer` -- the common case, and exactly what the probe reported
for candidate 1 -- got `direct`, and the proxy tried to scrape MPEG-TS playlist
text as a web page. No iframe, no m3u8, and it answered
`404 Channel not found or scrape failed: cand_0`.

That is the 404 in the log, three times over. **The entire fallback path was
dead for any stream that does not need a Referer**, which was never noticed
because the streams that reach fallback usually do need one.

Fixed: the mode is now chosen by whether we hold a playlist (`.m3u8` in the
path) or a page to scrape. The referer is written to its own field either way.

### Bug 2 — the fallback could not have rescued this stream anyway
Candidate 1 is anti-leech: its segments are MPEG-TS served from a TikTok
*image* CDN on URLs ending `.image`. FFmpeg's HLS demuxer refuses them by
extension, which is what killed Direct Mode. hls-proxy mirrors the upstream
extension onto its own `/proxy.<ext>` path, so the rewritten segments get
refused for the same reason.

Which FFmpeg option unlocks this is **not** guessable, and they are not
interchangeable. Measured inside the shipped image (Debian ffmpeg 5.1.9)
against a real `.image` segment:

| flags | result |
|---|---|
| none | refused: `not in allowed_segment_extensions` |
| `-allowed_extensions ALL` | refused: same |
| `-allowed_segment_extensions ALL` | refused one step later: `extension none mismatches` |
| **`-extension_picky 0`** | **PASS, 42676 bytes** |

`extension_picky` does not exist on upstream ffmpeg 6.1 (this dev box), which
has only `allowed_extensions` -- and passing an option a build does not know is
fatal. So `hls_extension_flags()` asks the binary what it supports and sends
only that, cached per path.

**Scoped to the fallback only.** There the playlist comes from our own proxy on
127.0.0.1, so "any extension" means "any file this process already fetched and
rewrote", not "anything a remote playlist cares to name". Direct Mode keeps
FFmpeg's strict default, and `-protocol_whitelist` forbids `file://` on both
paths regardless -- the protocol list, not the extension list, is what actually
stops a hostile playlist reading local files. There is a test asserting that.

### Verified
The dev box could not reproduce any of this: ffmpeg 6.1 here happily accepts a
`.image` segment and does not even have the option that matters. Everything
above was measured **inside `ghcr.io/jlesterak/pvarr:0.2.2`** against a local
origin serving real MPEG-TS bytes at a `.image` URL, through the real
hls-proxy, driven by PVArr's own `start_proxy` and `_build_ffmpeg_cmd`:

    ffmpeg flags probed : ['-allowed_extensions','ALL',
                           '-allowed_segment_extensions','ALL',
                           '-extension_picky','0']
    channels.conf mode  : 'literal'   <-- was 'direct', the 404
    bytes captured      : 42676       <-- was 0

That is the whole reason to keep a copy of the shipped image around: reasoning
from the dev box's FFmpeg would have produced a confident, wrong fix.

370 tests (was 357).

### Still open
- [ ] Whether candidate 1 *records* for the sponsor now. This proves the
      mechanism against a synthetic origin of the same shape; it does not prove
      that particular provider stays up.

## Release v0.2.3 (2026-08-31)  [COMPLETED]

Patch. Sponsor-approved ("shipit"). One fix, but it reopens a whole path:

- **The proxy fallback works again for streams that need no `Referer`.** The
  channel mode was chosen from the referer, which decides nothing about whether
  a URL is a playlist or a page to scrape. Any candidate without a Referer got
  sent down the scrape path and came back `404 Channel not found or scrape
  failed`. That is not a tuning issue — the fallback was non-functional for
  that entire class of stream.
- **Anti-leech segments (`.image`, `.png`, and friends) now record through the
  fallback.** FFmpeg's option for this differs between builds and passing an
  unknown one is fatal, so PVArr probes the binary and sends only what it
  supports. Strict default kept on Direct Mode; `-protocol_whitelist` still
  forbids `file://` on both paths.

370 tests green at the tag. Nothing to do on upgrade beyond pulling the image.

### Note for next time: the image is published twice
`scripts/publish.sh` builds and pushes `:X.Y.Z` and `:latest` from whatever
machine runs it, and *then* the `v*` tag makes CI build and push the same tags
again. Same Dockerfile, so the same image — but only the CI build runs the test
suite first, and only CI is reproducible. Worth switching the script to
`--skip-docker` by default and letting the tag be the single publisher.

## The proxy's channels.conf could outlive its session (2026-08-31)  [COMPLETED]

Found while clearing the sponsor's stale `channels_893cc63a.conf`. That file
was already gone -- `_remove_proxy_conf()` had cleaned it up -- but the sweep
showed the cleanup was reachable only on the straight-line path.

`channels.conf` holds the **fully tokenised stream URL** and is written to the
mounted recordings volume, where anything with read access to that share can
see it. Two ways it survived:

1. **The fallback block was not in a `try/finally`.** Anything raising between
   `start_proxy()` and the teardown -- the capture call, the ffmpeg command
   build -- skipped `stop_proxy()`, leaving both the credential on disk *and*
   an orphaned hls-proxy still holding its port.
2. **`self._proxy_conf_file = conf_file` was set several lines after the
   write.** `_remove_proxy_conf()` can only delete what it has been told
   about, so a failure in between orphaned the file with no reference left to
   it -- unreachable by any later cleanup.

Fixed both: the bookkeeping now happens immediately before the write, and the
fallback block tears down in a `finally`.

### Proven, not assumed
Each guard was checked by reverting its fix and confirming the matching test
goes red:
- revert the `finally` -> `tokenised channels.conf outlived the session`
- revert the ordering -> `orphaned conf: nothing held a reference to it`

373 tests (was 370).

### Also cleaned up
A real tokenised TikTok CDN URL had been hardcoded at line 16 of a scratchpad
test script (`e2e_proxy_mode.py`) while reproducing the candidate 1 404s.
Scrubbed. Session-local temp dir, token expiring the same night, but it should
not have been written down. Repo and scratchpad both verified clean for that
host.


## Phase 13 item 4: Recording windows & duration caps (2026-08-31)  [COMPLETED]

A recording may now carry a length. At the deadline it stops cleanly and
post-processes exactly as an operator stop does. Both sponsor decisions from
the Phase 13 spec are implemented as agreed.

### What was built
- `duration_minutes` (what a curl or cron caller wants) or `end_time` (an
  absolute epoch, what the dashboard sends) on `POST /api/recordings/start`.
  **Stop After (min)** on the new-recording form; blank means "until the
  stream ends".
- `PVARR_MAX_HOURS`, default **6**, for recordings given no length. A capture
  pointed at a 24/7 channel never ends by itself -- the stream does not stop,
  so no failover ever fires and it runs until the disk guard trips. That is a
  safety net doing a scheduler's job.
- Checked on the write path *and* at the top of each failover lap. Write path
  alone would never fire on a healthy stream's deadline... in fact the reverse:
  a healthy stream never leaves the write loop, and a lap-only check would sit
  through up to 60s of backoff, or miss the deadline entirely if every
  candidate happened to be down as the window closed.
- `ends_at` and `seconds_remaining` in the status summary; the card shows
  "42m left" under Elapsed, titled with the absolute local time.

### Stored absolute, never as a duration
A duration restarts its clock on every resume, so a recording that crashed
twice would run well past the end that was asked for -- and each restart would
be handed a fresh six hours by the backstop, which is the exact thing the
backstop exists to prevent. `end_time` is persisted as an absolute epoch and
the backstop is measured from the *original* `start_time`. `resume_decision()`
now finalises rather than resuming a session whose window closed while the
container was down: an exact answer where the mtime gap was a guess.

### Sponsor decision: retry until the window closes
`max_cycles` is a guess at "these sources are dead"; an explicit end time is a
statement that the event runs until then, and a stream down at kick-off is
often back minutes later. So the give-up cap is lifted while a window is open.

Deliberately keyed off an **explicit** window and not the backstop: with no
duration given, the 6h figure is a default the operator may not know about, and
retrying for six hours against three dead URLs is not what anyone means by a
safety net. There is a test for that distinction.

### The status has to stay honest
First cut called every deadline stop `completed_window`. Wrong, and the test
caught it: if the stream died twenty minutes into a two-hour window and never
came back, "finished on schedule" hides a truncated file behind a reassuring
word -- the same failure as reporting "completed" while a remux was still
running. Now:
- capturing when the deadline arrived -> `completed_window` ("finished on
  schedule")
- window closed after several dead laps, bytes on disk -> `completed_partial`
- window closed having captured nothing -> `failed`

### Rebroadcast is exempt from the backstop
Found while writing this, not after. A live channel is meant to sit there --
the sponsor starts one and expects it in Plex tomorrow -- and it writes into a
fixed-size ring, so none of the reasoning behind the backstop applies. Without
the exemption this change would have silently killed every channel at the six
hour mark. An explicit `end_time` is still honoured, for a deliberately finite
channel.

398 tests (was 373). The dashboard helper was additionally exercised in node
across nine cases (null, undefined, sub-minute, minutes, hours, zero, negative,
and the two status gates).


## Credential redaction in logs and notifications (2026-08-31)  [COMPLETED]

Follow-on from clearing the stale `channels.conf`: same class of leak, larger
blast radius. `redact_url_secrets()` in `logging_config.py` strips userinfo,
query string and fragment from any URL in a string, keeping scheme, host and
path -- which is what identifies the candidate and is the whole diagnostic
value of the line.

### Applied at the sinks, not the call sites
A redaction you have to remember to call is one that gets forgotten at the next
call site added -- and these URLs are not all ours to sanitise at source, since
a token can arrive inside FFmpeg's own error text. So it goes in
`StreamFailoverRecorder._log()`, which every recorder log line passes through.

### The notification leak was a parameter mismatch, not a formatting choice
`notify_recording_started(session_id, filename, candidate_name)` renders its
third argument as "Stream: {value}". `server.py` was passing `candidates[0]` --
the raw tokenised primary URL. So **every** "recording started" message shipped
a live stream token to Discord and Telegram, where it lands in a third party's
message history that cannot be expired or deleted. That is a worse exposure
than the same token in a local log, and worse than the conf file, which never
left the box.

Fixed at the call site (pass `recorder.candidates[0].name`) *and* at the sink,
because a notification cannot be recalled.

### Also found while doing it
`trigger_media_server_refresh()` and `send_telegram()` logged raw exception
text on failure. `requests` puts the failing URL in its exception message, and
those URLs carry `X-Plex-Token`, `api_key` and the Telegram bot token in their
query strings -- so a Plex refresh failing would print the Plex token to
stdout. All four handlers now redact.

### Proven, not assumed
- revert the `_log` redaction -> `'SECRET' unexpectedly found in [...log_history]`
- revert the call site -> `'SECRET' unexpectedly found in '<id> <file> https://cdn.example/live.m3u8?token=SECRET'`

408 tests (was 398).

### Deliberately NOT redacted -- sponsor call if this should change
`candidates[].url` in `/api/status` still carries the full tokenised URL. The
operator typed it, the dashboard shows it back to them, and the advanced header
override fields are keyed by it, so redacting it would break the UI and hide
the operator's own input from them. It is an API contract decision rather than
a bug fix, so it is recorded here rather than made quietly. Note that PVArr has
no authentication at all, so port 8999 is trusted-LAN-only either way -- which
is the real reason this one is not urgent.

## Release v0.3.0 (2026-08-31)  [COMPLETED]

Minor, not patch: recording windows are new capability, and the API grew two
optional fields. Backward compatible -- every existing call, compose file and
mount works unchanged. Sponsor-approved ("ship it").

**What a user gets that they did not have before:**
- **Recording windows.** *Stop After (min)* on the new-recording form, or
  `duration_minutes` / `end_time` on `POST /api/recordings/start`. The
  recording stops cleanly at the deadline and post-processes normally, and the
  card shows the time remaining.
- **A 6-hour backstop** (`PVARR_MAX_HOURS`) for recordings given no length,
  because a capture pointed at a 24/7 channel never ends by itself. Live
  rebroadcast channels are exempt.
- **Stream tokens no longer leave the box.** Every "recording started"
  notification had been shipping the fully tokenised primary URL to Discord and
  Telegram; a failed Plex or Emby refresh had been printing its token to
  stdout. Both fixed, plus redaction of every recorder log line.
- **The proxy's `channels.conf` can no longer outlive its session**, so a
  tokenised URL is not left on the recordings share by a failed fallback.

**On upgrade:** nothing to do. Pull and restart.

  - One behaviour change worth knowing: a recording started with no duration
    now stops after 6 hours where previously it ran until the disk guard or the
    stream ended. Set a duration per recording, raise `PVARR_MAX_HOURS`, or set
    it to `0` to restore the old behaviour.

408 tests green at the tag.

### Shipped without the icebox pass
The sponsor chose to ship before field-testing the three checks recommended
above (candidate 1 recording, a short duration stopping on time, a rebroadcast
channel unaffected by the cap). Recorded because if any of them misbehaves,
this is the release to look at -- and the 6-hour default is the change most
likely to surprise, since it alters what an existing untouched workflow does.

## humantodo line 2, step 1: the probe says what it tried (2026-08-31)  [COMPLETED]

Sponsor asked whether their testing would help line 2 ("resolve the instances
where the app can't detect headers automatically") or whether to build first.
Answer: their testing is the input -- I cannot invent failing providers, and
every fix that stuck this session came from their data -- but it would have
come back as "it didn't work", because the dashboard was throwing the evidence
away.

### What was already there
`probe_stream()` has always recorded an `attempts` list -- every URL, the
referer sent, the status returned -- and `/api/probe` has always returned it.
The dashboard rendered only `message`, and on failure did
`this.probes[key] = { ..., data: null }`, discarding the trace at exactly the
moment it was worth having.

### What was missing
- **The segment check recorded nothing.** "Segments rejected -- stream may be
  session gated" with no status code tells an operator something is wrong and
  nothing about what. It now records status, and the variant-playlist descent
  for a master playlist too.
- **The segment's extension was never surfaced.** This is the candidate 1 case:
  playlist 200, segment 200, everything "fine", and it still will not record,
  because FFmpeg refuses `.image` by extension. That cost an hour to diagnose
  from logs. The trace now names it at probe time.
- **A 2xx that is not a playlist looked like an unexplained failure.** Usually
  an anti-bot interstitial answering 200 with HTML. Now called out -- and
  scoped to a *successful* status, because on a 403 the body is obviously not a
  playlist and saying so reads like a second, unrelated problem.
- **The page fetch was not a recorded step**, so a scrape that found no m3u8
  showed nothing before the failure.

### Dashboard
Collapsed "Show what PVArr tried (n)" under the probe verdict, colour-coded by
status, with a **Copy trace** button. Query strings are stripped from every URL
in the trace, so it carries no access token and is safe to paste into a bug
report. Falls back to a prompt() when the clipboard API is unavailable --
plain-http LAN access is not a secure context, which is exactly how this
dashboard is normally reached.

### Proven
- Three failure shapes driven end to end against a local origin: referer-gated
  playlist with `.image` segments, the same with no referer hint, and a public
  playlist with 403 segments. Each produced the right trace.
- The UI guard was checked by restoring `data: null` and re-running the helper
  in node: 1 trace row with the fix, **0 without it**.

419 tests (was 408).

### Next, and it needs the sponsor
Throw the streams that fail header detection at the probe and send the traces.
The fix depends entirely on what they show: a referer heuristic, a
cookie-capture step, or -- if the m3u8 is built in JavaScript -- no amount of
probing helps and the right answer is a better message pointing at DevTools and
the optional `detect-headers` browser path.

## humantodo line 2, step 2: "needs a header" vs "not talking to us" (2026-08-31)  [COMPLETED]

The sponsor supplied a failing URL (`lb7.strmd.st`, a tokenised HLS link) and
asked whether the JavaScript limitation explained it. It did not, and chasing
it produced a better answer than the feature we set out to build.

### The evidence
Tried from the dev box, every referer (none, the CDN origin, strmd.st,
streamed.su/.st, embedme.top, embedstreams.top, google.com) and several
user-agents (Chrome, curl, VLC), in both requests and curl. **Identical
139-byte nginx 403 every time.** Then the decisive test:

    https://lb7.strmd.st/            -> 403   (the site's own front page)
    https://lb7.strmd.st/hello-there -> 403
    bogus token + real path          -> 403
    real token + bogus path          -> 403

The host refuses the request before it ever looks at the path, so the token was
never evaluated and no header was ever going to matter. All five sibling hosts
(lb1/2/5/7/9, one /24 at 92.63.196.x) behave the same; the apex does not
respond at all.

Sponsor then confirmed 403 from icebox **and** 403 in a real browser on their
own network. Three independent clients, three different TLS stacks, one of them
an actual browser. The link is simply dead.

### What PVArr got wrong
It answered all of that with "The stream likely needs a cookie or a referer
PVArr cannot guess -- copy them from DevTools." That sent the sponsor hunting
in DevTools for a header that does not exist, on a link that was not going to
work for anybody.

### The fix
On **total** failure only, the probe now asks the origin for its own front
page and records it in the trace as stage `origin`. If the root is refused with
the same status as the playlist (401/403/429), the message says the host is
refusing us outright, that this is not a missing header, and that the link has
probably expired -- instead of pointing at DevTools. When the root *does*
answer, the old advice stands and is now stated with more confidence, because
we have evidence the host is gating this stream specifically.

Costs one extra request, and only on a probe that already failed. A successful
probe is unchanged; there is a test asserting that.

### Also corrected: the README was wrong about detect-headers
Checked inside the shipped image: `detect-headers` is a symlink to upstream's
**shell** version -- 11 curl calls, zero browser references, no Playwright
module and no Chromium in the image. The README claimed it "drives a real
browser and can see what a plain fetch cannot". True of upstream's Python
variant, false of what we ship, and it is exactly the claim that made the
JavaScript theory sound plausible. README now says what is actually in the
container and what it can and cannot do.

425 tests (was 419). Verified against two local origins (root answers / root
refuses) and against the sponsor's real URL, which now produces the correct
message.

### Still open for line 2
This URL turned out to be a dead link, so it taught us nothing about header
detection itself. Still need traces from a stream that *is* reachable and still
fails to detect -- that is the case the original humantodo line is about.

### Housekeeping
The dev box hit 100% disk (6.7 MB free) mid-session and took 302 tests down
with it. Cause was 16.66 GB of dangling images from this session's own docker
builds; `docker image prune` recovered it. Worth remembering that building the
shipped image to test against is not free.

## humantodo line 2, step 3: it was the wrong URL, not the wrong headers (2026-08-31)  [COMPLETED]

Sponsor tested **five streams from five different providers**. All five gave
the same "Every header combination was rejected (403)... copy them from
DevTools", and all five 403'd under curl and in a browser too.

Five independent providers do not all break the same way by coincidence. The
common factor is not the providers -- it is what was being pasted.

### The cause
`_probe_candidate()` probes `candidate.url` -- **the URL the operator typed** --
fresh at connect time and again on every failover. The resolved playlist is
kept separately in `candidate.m3u8_url` and is never fed back in as input.

That makes the choice of pasted URL decisive:

- Paste a **page**: PVArr scrapes it and mints a token itself, from the machine
  doing the recording, every single time it connects. Token expiry mid-capture
  fixes itself.
- Paste a **tokenised m3u8**: there is nothing to re-resolve. Every retry
  replays the same token. And that token was minted for the operator's *browser
  session*, often expiring in minutes -- so a URL copied out of DevTools is
  frequently dead before it is pasted, and dead for good.

Reproduced end to end against a local origin that mints per-session tokens with
a short TTL: the copied m3u8 probes fine immediately, 403s seconds later, and
the same origin's page URL keeps working indefinitely with a fresh token each
probe.

### The fix
`looks_tokenised()` spots an access token in a URL -- either in the query
string (token/sig/hash/expires/hdnts/...) or baked into the path as a long
opaque segment, which is what nginx `secure_link` does and what the sponsor's
provider used. On a 401/403/404 from a host that *is* otherwise answering, the
message now names the real problem and says to paste the page URL instead of
sending the operator after headers that do not exist.

Precedence matters: the "host refuses its own front page" check still wins,
because telling someone to paste the page URL of a host that refuses everything
would be wrong advice. There is a test for that ordering.

The path heuristic needs randomness, not just length -- `2024-nfl-week-1-
highlights` is 26 characters of ordinary slug. Long hex, or a mix of upper and
lower case, neither of which occurs in a human-written path segment.

### README was overselling this too
It said "an expired token is re-resolved rather than replayed", full stop. Only
true when a page URL was pasted. Corrected, and "Getting the URL to paste" now
leads with paste-the-page and explains why, rather than presenting the DevTools
m3u8 as an equal option.

434 tests (was 425).

### Still open
Whether this actually resolves the sponsor's five. It explains all the observed
evidence, but the confirming test is theirs to run: paste the **page** URL for
those same five streams. If a page URL still fails on a host whose root answers,
that is a genuine header-detection gap and the trace will finally show it.

## humantodo line 2: the root cause, and a decision for the sponsor (2026-08-31)

Sponsor pasted the page URL for the failing streams and got "No .m3u8 found on
that page (HTTP 200)". So both exits are closed:

- **page URL** -> the player builds the m3u8 in JavaScript; nothing to scrape
- **DevTools m3u8** -> token is session-bound and short-lived; 403

Their original instinct ("could it be the javascript limitation you mentioned")
was right. It was wrongly ruled out on the strmd.st URL, where a dead host
made it look like something else, and not revisited when the pattern turned out
to hold across five providers.

### The capability PVArr documents for this has never shipped
1. The Dockerfile clones `pcruz1905/hls-restream-proxy` and copies
   `detect-headers-py.py` *if present*.
2. That repo ships only `detect-headers.sh` -- 11 curl calls, no browser.
3. `/home/jake/hls-restream-proxy/detect-headers-py.py` is **untracked in
   git**. It exists on this dev box and nowhere else.

So the Dockerfile's condition for the Playwright script has never once been
true, in any build. `_detect_via_script` was documented "(browser-backed)" and
the README claimed it "drives a real browser". Both described a file that was
never published. Docstring and README both corrected.

The five failures are therefore not five awkward providers. They are one
missing feature.

### Measured cost of the real fix
Inside `ghcr.io/jlesterak/pvarr:0.3.0`, `pip install playwright` plus
`playwright install --with-deps chromium`:

    baseline image   710 MB
    after            1645 MB
    DELTA            935 MB      (2.3x the image)

RAM is transient rather than resident -- a few hundred MB while a page renders,
released after. CPU is a few seconds per candidate at connect and at each
failover, not continuous. Nothing touches the capture path: if the browser is
absent or fails, PVArr behaves exactly as it does today.

### Two architectures -- SPONSOR DECISION NEEDED
- **A: bake it into the image.** +935 MB for every user, including the majority
  who never hit a JS-built URL. Simplest to operate, worst to distribute.
- **B: optional sidecar container.** Main image unchanged; a second container
  runs the browser and PVArr asks it over HTTP for a page's m3u8. Costs nothing
  for anyone who does not need it, and the sponsor pulls it only on icebox.
  This is exactly the FlareSolverr pattern the *arr ecosystem already uses for
  Cloudflare challenges, so it will be familiar to users.
- **C: do nothing.** These providers stay unusable in PVArr.

Recommended: **B**. An optional heavyweight dependency should be opt-in, and
the ecosystem precedent is strong.

### Open question for the sponsor
Whether the DevTools m3u8 failed *immediately* or worked briefly first. Pure
TTL means a manual stopgap exists for short recordings; immediate failure means
it is bound to the browser session or IP and there is no stopgap. The fix is
the same either way.

## humantodo line 1: integrate rather than build (2026-08-31)

Sponsor confirmed line 1 was pointing at exactly the FlareSolverr discussion:
when a problem is already solved by a tool in this ecosystem, wire it up rather
than growing our own. Sizes, measured rather than guessed:

    comskip     333 KB installed (deps already present)
    yt-dlp      3.1 MB
    curl_cffi   13 MB   (prebuilt wheel, no compiler)
    Chromium    935 MB

### Decided and built: yt-dlp  [COMPLETED]
`app/ytdlp.py`, wired into `detect_candidate_headers` between the built-in
probe and the detect-headers script. Cheapest first.

It exists for the case the probe *structurally* cannot handle. The sponsor ran
`document.documentElement.outerHTML.includes('m3u8')` on their pages and got
**false**: the player fetches its manifest over XHR and hands it to hls.js, so
the URL is never in the document. No scraper will ever find it. That result
also downgrades FlareSolverr for this purpose -- it returns rendered HTML, not
intercepted requests, so it would not find the URL either. Its remaining value
is Cloudflare cookies.

Design notes:
- **Subprocess, not import.** yt-dlp can hang on a slow origin; a subprocess
  takes a timeout and an in-process call does not. It is also then replaceable
  without a PVArr release, which matters for a tool that ships fortnightly.
- **`-J`, not `-g`.** `-g` prints only the URL; the JSON carries
  `http_headers`, which is where the Referer and User-Agent live. The URL
  without them just moves the 403 one step later.
- **Skipped for pasted playlist URLs.** The probe has already tried that exact
  URL with every header combination it has. Calling yt-dlp would add up to 20s
  to a *failover* to learn nothing. Caught because the suite jumped from 1.3s
  to 15.1s the moment it was wired in -- the dev box has yt-dlp installed, so
  tests were really shelling out. Same class of defect as the disk guard
  reading the host's free space.
- **Timeout 20s, not 45.** This runs while a live recording is off the air.

### `--impersonate` is another extension_picky
`--help` on yt-dlp 2024.04.09 mentions impersonate three times, and
`--impersonate chrome` exits with a Python traceback: the option is recognised,
but every target needs `curl_cffi`. Passing it on such a build turns every
resolution into a hard failure -- and the first version of this module passed
it unconditionally, with a comment claiming it was "silently ignored". It is
not.

`impersonation_available()` runs `--list-impersonate-targets` and looks for a
row not marked "(not available)". Cached per binary. Exactly the lesson from
`hls_extension_flags`: ask the binary what it can do, never what its help text
mentions.

`curl_cffi` is now in requirements, so the container can impersonate.
**Measured against the sponsor's wall: it does not help there.**
`curl_cffi.get('https://lb7.strmd.st/', impersonate='chrome')` returns the same
403, same 139 bytes, as plain requests. That host blocks by network, not by TLS
fingerprint -- more evidence the link was simply dead. Kept anyway: it is 13 MB
and the capability is general.

Verified against real yt-dlp output, not only mocks: resolves a public Mux HLS
test stream, correctly reports impersonation unavailable on the system binary
and available in the venv.

452 tests (was 434).

### Decided, not yet built: comskip  [PENDING]
Sponsor decision: **comchap (chapter marks) as the default, comcut (actual
removal) as an option.** Non-destructive by default is the right call next to
everything else this project does to avoid losing footage -- a false positive
in a cut eats a play that cannot be re-recorded.

Sponsor corrected my pessimism: many of these streams rebroadcast **OTA
channels**, which is comskip's home turf -- real station logos, real black
frames, real ad breaks. Others show a static card ("MLB Commercial Break In
Progress"), which comskip will likely miss because it is neither black nor
logo-free.

The shipped FFmpeg already has `freezedetect`, `blackdetect`, `blackframe` and
`silencedetect` -- verified inside `ghcr.io/jlesterak/pvarr:0.3.0`. A static
break card is a frozen frame, so `freezedetect` catches precisely the case
comskip misses, at zero added dependency. Plan: comskip for the OTA
rebroadcasts, a freezedetect pass for the static-card streams, both writing
chapters.

Runs after the remux, off the capture path entirely. ~20-40 min of CPU for a
3-hour recording, single-threaded.

### Considered and declined
- **Tdarr / Unmanic** -- do not build transcoding. Point them at the recordings
  folder; that is what they are for.
- **Sonarr / Radarr APIs** -- PVArr is not indexer-driven, there is no release
  to grab. The Plex/Emby tuner integration already covers the ecosystem need.
- **Bazarr** -- live streams do not carry subtitles worth fetching.

### Still open
- **Apprise** would replace the hand-rolled Discord/Telegram code with 100+
  targets and let us delete code rather than add it. Not urgent.
- **FlareSolverr**, narrowed: Cloudflare cookies only, since the m3u8 is not in
  the DOM. Worth it only if a provider turns out to be Cloudflare-gated *and*
  yt-dlp cannot resolve it.

## Release v0.4.0 (2026-08-31)  [COMPLETED]

Minor: new capability and two new Python dependencies, backward compatible.
Sponsor-approved ("ship it").

**What a user gets that they did not have before:**
- **yt-dlp resolution.** PVArr can now resolve a page whose player fetches its
  manifest over XHR -- the case its own scraper structurally cannot see,
  because the m3u8 never enters the HTML. Extractors for thousands of sites,
  plus a real browser TLS fingerprint via `curl_cffi`.
- **The probe says what it tried.** "Show what PVArr tried" under a failed
  probe lists every header combination, its status, the segment fetch and its
  extension, with a Copy trace button. Query strings stripped, so it is safe
  to paste into a bug report.
- **It stops blaming headers for things that are not headers.** A host that
  refuses its own front page is named as such; a rejected access token is named
  as such, with the advice to paste the page URL instead of hunting DevTools
  for a header that does not exist.
- **Recording windows and a 6-hour backstop** (from v0.3.x work carried here in
  full).
- **Stream tokens no longer reach logs or notifications.**
- **Buccaneers vs Raiders** as the example fixture.

**On upgrade:** nothing to do. Pull and restart.

  - The image grows ~16 MB (yt-dlp + curl_cffi).
  - Unchanged from v0.3.0: a recording started with no duration stops after
    6 hours. Set a duration, raise `PVARR_MAX_HOURS`, or set it to `0`.

452 tests green at the tag.

### Known limitation shipped knowingly
The sponsor's five failing providers are not fixed by this. yt-dlp may resolve
some of them -- that is the test to run -- but for a provider it has no
extractor for, whose manifest is XHR-only, the remaining answer is real
request interception in a browser, which is not built. What v0.4.0 guarantees
is an accurate diagnosis instead of a misleading one.
