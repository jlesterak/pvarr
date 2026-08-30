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

## Still open
- Nothing tracked. Next candidates if the project continues: a retention/cleanup
  policy for old recordings on disk, and integration coverage for the
  notification webhooks (currently only exercised via mocks).
