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

## Open design question — needs a decision, not a fix
- [ ] **Mid-stream freeze ends the recording instead of failing over.**
      `_stream_ffmpeg_process` returns `True` when a stream delivers data and
      then stalls, so `_recording_loop` treats it as "completed naturally" and
      stops. A stream that dies 10 minutes into a 3-hour event therefore yields
      a 10-minute file, which is the exact scenario 3-stage failover exists to
      survive. A clean FFmpeg exit is already distinguishable from a stall
      (`poll()` is not None), so returning `False` on stall is implementable.
      The tradeoff: a long recording that stalls near the end would roll onto
      the next candidate and, if all candidates then exhaust, be marked
      `failed` despite having captured almost everything. Current behaviour is
      pinned by `TestFreezeDetection.test_mid_stream_freeze_after_data_reports_success`.

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

## Still open
- [ ] **No authentication on any endpoint.** Anyone who can reach the port can
      start, stop, and delete recordings. Fine on a trusted LAN, not fine if
      exposed. Wants at minimum a shared-secret header or basic auth before
      anyone puts this behind a public reverse proxy.
- [ ] `_stream_ffmpeg_process` has no test for the partial-write path where
      FFmpeg exits non-zero *after* writing data.
