#!/usr/bin/env python3
"""
Web Server Backend Module - PVArr
Provides REST API & SSE streaming endpoints for multi-stream failover recording control.
"""

import asyncio
import json
import logging
import os
import time
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, APIRouter, Request, Form, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.recorder import (
    DEFAULT_MIN_FREE_GB,
    PROXY_PORT_STRIDE,
    StreamFailoverRecorder,
    safe_stream_url,
)
from app.probe import probe_stream
from app.naming import (
    StorageManager,
    generate_sports_filename,
    media_type_for,
    probe_video_resolution,
)
from app.cleanup import register_signal_handlers, stop_all
from app.tuner import (
    generate_m3u_playlist,
    generate_xmltv_epg,
    generate_discover,
    generate_lineup,
    generate_lineup_status,
    generate_device_xml,
)
from app.notifications import NotificationManager
from app.post_processor import remux_recording
from app.cleanup import _shutdown_timeout
from app.ringbuffer import RingBuffer
from app.sessions import SessionStore, build_record, resume_decision
from app.logging_config import configure_logging

configure_logging()
logger = logging.getLogger("PVArrServer")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Stop every active recorder on shutdown.

    The SIGINT/SIGTERM handlers registered below only fire when the signal
    reaches this process's handler. Uvicorn installs its own handlers and
    drives shutdown through the ASGI lifespan, so without this hook a
    `docker stop` or Ctrl-C could return before FFmpeg and hls-proxy children
    were terminated, orphaning them.
    """
    # Reattach to anything that was recording when this process last died.
    # Offloaded: it stats files and may run a remux, and this is the event loop.
    try:
        counts = await asyncio.to_thread(resume_sessions)
        if any(counts.values()):
            logger.info(
                "Session recovery: %s resumed, %s finalised, %s discarded.",
                counts["resumed"], counts["finalised"], counts["discarded"],
            )
    except Exception as exc:
        # A broken session file must never stop the server from booting.
        logger.error("Session recovery failed: %s", exc)

    yield
    # Offloaded because stopping reaps FFmpeg (up to 7s each) and then waits for
    # post-processing -- both blocking, and this still runs on the event loop.
    try:
        await asyncio.to_thread(stop_all, active_recorders, _shutdown_timeout())
    except Exception as exc:
        logger.error("Error stopping recorders during shutdown: %s", exc)


app = FastAPI(
    title="PVArr - Personal Video Recorder",
    version=__version__,
    lifespan=lifespan,
)

notifier = NotificationManager()
session_store = SessionStore()


BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "app" / "templates"
# Overridable so the container can write to a mounted volume rather than a
# path inside the image layer.
RECORDINGS_DIR = Path(os.environ.get("PVARR_RECORDINGS_DIR") or (BASE_DIR / "recordings"))


def _min_free_gb() -> float:
    """Free-space floor below which recordings abort. 0 disables the guard."""
    raw = os.environ.get("PVARR_MIN_FREE_GB")
    if raw is None:
        return DEFAULT_MIN_FREE_GB
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("Ignoring invalid PVARR_MIN_FREE_GB=%r", raw)
        return DEFAULT_MIN_FREE_GB

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Exposed to every template as a Jinja global rather than passed through
# each route's context: the dashboard badge was hardcoded to "v1.0.0" and
# silently disagreed with the shipped version for the whole 0.1.x series.
# A global cannot be forgotten by a route added later.
templates.env.globals["pvarr_version"] = __version__
storage = StorageManager(record_dir=str(RECORDINGS_DIR))

# Active recorder sessions: recording_id -> StreamFailoverRecorder
active_recorders: Dict[str, StreamFailoverRecorder] = {}

# Register SIGINT / SIGTERM handlers for container & process safety
register_signal_handlers(active_recorders)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Return a structured error instead of leaking a bare traceback."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "detail": "Internal server error"},
    )



def _allowed_library_dirs():
    """Directories the library API may touch.

    RECORDINGS_DIR always, plus anything listed in PVARR_ALLOWED_DIRS
    (os.pathsep-separated). Without this allowlist a caller could pass any
    absolute path as ?dir_path= and read or delete arbitrary files, since
    none of these endpoints are authenticated.
    """
    dirs = [RECORDINGS_DIR.resolve()]
    for raw in os.environ.get("PVARR_ALLOWED_DIRS", "").split(os.pathsep):
        if raw.strip():
            try:
                dirs.append(Path(raw.strip()).resolve())
            except Exception:
                pass
    return dirs


def _resolve_library_dir(dir_path: Optional[str]) -> Path:
    """Resolve a caller-supplied dir_path, refusing anything outside the allowlist."""
    if not dir_path:
        return RECORDINGS_DIR.resolve()
    try:
        candidate = Path(dir_path).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid dir_path")
    for allowed in _allowed_library_dirs():
        if candidate == allowed or allowed in candidate.parents:
            return candidate
    raise HTTPException(
        status_code=403,
        detail="dir_path is outside the permitted recording directories",
    )


def _active_output_paths() -> Dict[Path, str]:
    """Files a live recorder is currently writing to, mapped to session id.

    Both the .ts being captured and the remuxed final path, because a session
    switches between them at completion and either one being removed underneath
    is the same accident.
    """
    paths: Dict[Path, str] = {}
    for rid, rec in list(active_recorders.items()):
        try:
            if not rec.is_running or rec.is_rebroadcast:
                continue
        except Exception:
            continue
        # Per attribute, so one unreadable path does not hide the others. A
        # recorder in a strange state must never break the library; the worst
        # case is that we fail to protect it, which is what we did before.
        for attr in ("output_filepath", "current_filepath", "final_filepath"):
            try:
                value = getattr(rec, attr, None)
                if value:
                    paths[Path(value).resolve()] = rid
            except Exception:
                continue
    return paths


def _refuse_if_recording(path: Path, verb: str) -> None:
    """Block a library operation on a file that is being recorded to.

    Deleting the file underneath an open append handle does not fail and does
    not stop the recording: the writes keep succeeding into an inode with no
    name, the footage is unrecoverable, and the dashboard shows 0 MB because it
    stats the path. On NFS the orphan is visible as a .nfsXXXX file; on a local
    filesystem it is invisible.

    This is not theoretical -- a live recording was lost to a DELETE that
    returned 200 OK while the session kept running.
    """
    try:
        resolved = Path(path).resolve()
    except Exception:
        return
    owner = _active_output_paths().get(resolved)
    if owner:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot {verb} {resolved.name}: recording {owner} is writing "
                "to it right now. Stop the recording first -- it will be "
                "post-processed and released."
            ),
        )


def _safe_filename(filename: str) -> str:
    """Reject any filename carrying a directory component."""
    name = Path(filename).name
    if not name or name in (".", "..") or name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return name


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    fav_path = BASE_DIR / "app" / "static" / "favicon.svg"
    if fav_path.exists():
        return FileResponse(fav_path, media_type="image/svg+xml")
    return Response(status_code=204)



@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Render single-page management dashboard."""
    recordings_list = storage.list_recordings()
    active_list = [r.get_status_summary() for r in active_recorders.values()]
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "active_recordings": active_list,
            "library": recordings_list,
            "default_dir": str(RECORDINGS_DIR),
        }
    )



@app.get("/live/playlist.m3u8")
@app.get("/live/playlist.m3u")
async def get_tuner_m3u(request: Request):
    """Return dynamic M3U tuner playlist for IPTV clients (Plex / Emby)."""
    active_sessions = [r.get_status_summary() for r in active_recorders.values()]
    base_url = str(request.base_url)
    m3u_content = generate_m3u_playlist(active_sessions, base_url)
    return Response(content=m3u_content, media_type="application/x-mpegurl")


@app.get("/live/epg.xml")
async def get_tuner_epg():
    """Return XMLTV EPG data for active tuner channels."""
    active_sessions = [r.get_status_summary() for r in active_recorders.values()]
    xml_content = generate_xmltv_epg(active_sessions)
    return Response(content=xml_content, media_type="application/xml")

# --------------------------------------------------------------------------
# HDHomeRun tuner emulation
#
# Plex's Live TV setup probes a device address for these three files before it
# will add a tuner; a 404 on any of them fails the whole flow. The router is
# mounted twice so either `http://host:8999` or `http://host:8999/live` works
# as the device address.
# --------------------------------------------------------------------------
hdhr = APIRouter(tags=["hdhomerun"])


def _hdhr_base_url(request: Request) -> str:
    """The address the client used, minus the file it asked for.

    Derived from the request rather than the app root so the URLs PVArr
    advertises come back to the same mount (and the same host header) the
    media server actually reached us on.
    """
    return str(request.url).split("?")[0].rsplit("/", 1)[0]


def _active_sessions() -> List[dict]:
    return [r.get_status_summary() for r in active_recorders.values()]


@hdhr.get("/discover.json")
async def hdhr_discover(request: Request):
    return JSONResponse(content=generate_discover(_hdhr_base_url(request)))


@hdhr.get("/lineup_status.json")
async def hdhr_lineup_status():
    return JSONResponse(content=generate_lineup_status())


@hdhr.get("/lineup.json")
async def hdhr_lineup(request: Request):
    return JSONResponse(
        content=generate_lineup(_active_sessions(), str(request.base_url))
    )


@hdhr.get("/lineup.post")
@hdhr.post("/lineup.post")
async def hdhr_lineup_post():
    """Channel-scan trigger. PVArr's lineup is always live, so this is a no-op
    that must still answer 200 or Plex reports the scan as failed."""
    return JSONResponse(content={})


@hdhr.get("/device.xml")
async def hdhr_device_xml(request: Request):
    return Response(
        content=generate_device_xml(_hdhr_base_url(request)),
        media_type="application/xml",
    )


app.include_router(hdhr)
app.include_router(hdhr, prefix="/live")


@app.get("/api/status")
async def get_system_status():
    """Return JSON summary of all active recorders."""
    sessions = [r.get_status_summary() for r in active_recorders.values()]
    return {
        # So "what is icebox actually running?" is answerable with curl,
        # without trusting a number rendered in the UI.
        "version": __version__,
        "active_count": len([r for r in active_recorders.values() if r.is_running]),
        "total_sessions": len(sessions),
        "sessions": sessions
    }


# Finished sessions are kept so the dashboard can show recent history, but not
# forever: each holds a 500-line log buffer and candidate state.
MAX_FINISHED_SESSIONS = 20


def _prune_finished_sessions() -> None:
    """Drop the oldest finished sessions once we exceed the retention cap.

    Nothing else removes entries, so without this active_recorders grows for
    the life of the process.
    """
    finished = [(rid, r) for rid, r in active_recorders.items() if not r.is_running]
    excess = len(finished) - MAX_FINISHED_SESSIONS
    if excess <= 0:
        return
    finished.sort(key=lambda kv: kv[1].stop_time or 0.0)
    for rid, _ in finished[:excess]:
        active_recorders.pop(rid, None)


def _allocate_proxy_port(base: int = 8090) -> int:
    """Lowest free proxy port among *running* sessions.

    Deriving this from the total session count made the port climb for the
    life of the process and never reuse a freed one.
    """
    used = {r.base_port for r in active_recorders.values() if r.is_running}
    port = base
    while port in used:
        # Step by the whole reserved block, not by 2. A session binds
        # base_port + candidate_index, so with three candidates it occupies
        # base .. base+2 -- stepping by 2 handed the next session a base that
        # the previous one's last candidate would bind, and the second proxy
        # then failed to start.
        port += PROXY_PORT_STRIDE
    return port


@app.post("/api/probe")
async def probe_url(url: str = Form(...), referer: Optional[str] = Form(None)):
    """Resolve a pasted URL to a playlist + headers, for the Add Recording form.

    Runs off the event loop: a probe is several sequential HTTP requests to a
    third-party origin and can sit on a timeout for seconds, which would stall
    every other dashboard request if awaited inline.
    """
    if len(url) > 4096:
        raise HTTPException(status_code=400, detail="URL is too long")
    result = await asyncio.to_thread(probe_stream, url.strip(), referer=(referer or None))
    return JSONResponse(content=result)


def buffer_dir() -> Path:
    """Where rebroadcast ring buffers live.

    Under the recordings volume by default, in a dot-directory so it does not
    show up as content. Overridable because a ring takes a steady ~1 MB/s of
    writes per channel and an operator may want that off a spinning library
    disk. Nothing here is kept: the file is deleted when the channel stops.
    """
    override = os.environ.get("PVARR_BUFFER_DIR")
    return Path(override) if override else (RECORDINGS_DIR / ".buffers")


def _default_channel_name(sport: str, team_a: str, team_b: str) -> str:
    """A readable name for a rebroadcast channel in the Plex guide.

    A rebroadcast writes no file, so there is no filename for the guide to
    fall back on -- without this every channel would show as "Channel 1".
    """
    teams = " vs ".join(part for part in (team_a, team_b) if part and part.strip())
    name = teams or (sport or "").strip()
    return name or "PVArr Live"


def _launch_session(record: Dict[str, Any], port: int) -> StreamFailoverRecorder:
    """Build a recorder from a session record, persist it, and start it.

    Shared by a fresh start and by a resume at boot, so the two cannot drift
    apart -- a resumed recording must behave identically to a new one, and the
    surest way to guarantee that is for there to be one code path.
    """
    recording_id = record["id"]
    output_filepath = record["output_filepath"]

    ring = None
    if record.get("rebroadcast"):
        ring = RingBuffer(output_filepath)

    def _on_complete(filepath):
        """Post-process first, announce second.

        The notification triggers a Plex/Emby library scan. Firing it before
        the remux meant the scan ran while only the .ts existed and the .mp4
        did not, so Plex indexed a file the remux was about to delete and never
        saw the finished recording until its next scheduled scan. The webhook
        text also quoted the .ts name and its pre-remux size. Remuxing first
        costs nothing extra -- this already runs on the recorder thread, not
        the event loop.

        A shutdown is the exception. The recording is not finished -- the
        container is going away and we intend to resume -- so remuxing here
        would delete the .ts that the resume needs to append to. The session
        record is left in place and the decision is made at boot instead. If
        persistence is unavailable there is nothing to resume from, so the
        remux still runs: better a finished file than an orphaned one.
        """
        if ring is not None:
            # A rebroadcast keeps nothing: there is no file to remux, no
            # library entry to announce, and the ring is deleted outright.
            # Leaving it would put 75 MB of footage nobody asked for on the
            # volume, per channel, forever.
            ring.close()
            session_store.remove(recording_id)
            logger.info("Rebroadcast channel %s stopped; buffer discarded.", recording_id)
            return

        if recorder.stop_reason == "shutdown" and session_store.enabled:
            logger.info(
                "Session %s interrupted by shutdown; keeping %s for resume.",
                recording_id, Path(filepath).name,
            )
            return

        result = remux_recording(filepath, target_format="mp4", delete_source=True)
        # Point the session at the remuxed file: the .ts it was recording to
        # has just been deleted, so size and filename lookups would read 0.
        if result.get("status") == "success" and result.get("output_filepath"):
            recorder.final_filepath = Path(result["output_filepath"])

        final_path = recorder.final_filepath or Path(filepath)
        sz = round(final_path.stat().st_size / (1024*1024), 2) if final_path.exists() else 0
        notifier.notify_recording_finished(recording_id, final_path.name, sz)
        # Genuinely finished: stop tracking it, so a later restart does not
        # try to resume a recording that has already been remuxed and shipped.
        session_store.remove(recording_id)

    def _on_failover(rec_id, next_name):
        notifier.notify_failover_triggered(rec_id, next_name)
        # A state transition, and one of the few things worth re-persisting:
        # a resume should reattach to the candidate that was actually working,
        # not start again from the primary that had already failed.
        record["current_candidate_index"] = recorder.current_candidate_index
        session_store.save(record)

    recorder = StreamFailoverRecorder(
        recording_id=recording_id,
        candidates=record["candidates"],
        output_filepath=output_filepath,
        base_port=port,
        freeze_timeout_sec=record.get("freeze_timeout_sec", 60),
        on_completion_callback=_on_complete,
        on_failover_callback=_on_failover,
        header_overrides=record.get("header_overrides") or {},
        min_free_gb=record.get("min_free_gb", DEFAULT_MIN_FREE_GB),
        ring=ring,
        channel_name=record.get("channel_name") or None,
    )
    if record.get("started_at"):
        recorder.start_time = record["started_at"]

    session_store.save(record)
    active_recorders[recording_id] = recorder
    recorder.start_recording()
    return recorder


def _finalise_orphan(record: Dict[str, Any]) -> None:
    """Post-process a recording we are not going to reconnect to.

    The footage is real and worth keeping; only the live capture is over. This
    is the same work _on_complete does, minus the recorder, because there is
    no longer a process to attach one to.
    """
    recording_id = record["id"]
    filepath = record["output_filepath"]
    try:
        result = remux_recording(filepath, target_format="mp4", delete_source=True)
        final = Path(result.get("output_filepath") or filepath)
        size = round(final.stat().st_size / (1024 * 1024), 2) if final.exists() else 0
        notifier.notify_recording_finished(recording_id, final.name, size)
        logger.info("Finalised orphaned recording %s as %s", recording_id, final.name)
    except Exception as exc:
        logger.error("Could not finalise orphaned recording %s: %s", recording_id, exc)
    finally:
        session_store.remove(recording_id)


def resume_sessions() -> Dict[str, int]:
    """Reattach to recordings that were in flight when the process died.

    Runs once at startup. Every record gets one of three fates, decided by
    resume_decision(): reconnect and keep appending to the same .ts, finalise
    it as it stands, or discard it because there is nothing on disk. Failures
    are contained per-session -- one bad record must not stop the others, and
    must never stop the server booting.
    """
    counts = {"resumed": 0, "finalised": 0, "discarded": 0}
    for record in session_store.load_all():
        recording_id = record.get("id", "?")
        try:
            decision = resume_decision(record)
            if decision == "discard":
                logger.info("Discarding session %s: no footage on disk.", recording_id)
                session_store.remove(recording_id)
                counts["discarded"] += 1
                continue
            if decision == "finalise":
                logger.info("Finalising session %s rather than resuming.", recording_id)
                _finalise_orphan(record)
                counts["finalised"] += 1
                continue

            record["resume_attempts"] = int(record.get("resume_attempts", 0)) + 1
            _launch_session(record, _allocate_proxy_port())
            logger.info(
                "Resumed session %s, appending to %s (attempt %s).",
                recording_id, Path(record["output_filepath"]).name,
                record["resume_attempts"],
            )
            counts["resumed"] += 1
        except Exception as exc:
            logger.error("Could not resume session %s: %s", recording_id, exc)
    return counts


@app.post("/api/recordings/start")
async def start_recording(
    background_tasks: BackgroundTasks,
    sport: str = Form("Sports"),
    team_a: str = Form("TeamA"),
    team_b: str = Form("TeamB"),
    resolution: str = Form("1080p"),
    output_dir: Optional[str] = Form(None),
    url_primary: str = Form(...),
    url_backup1: Optional[str] = Form(None),
    url_backup2: Optional[str] = Form(None),
    freeze_timeout: int = Form(15),
    stream_headers: Optional[str] = Form(None),
    rebroadcast: bool = Form(False),
    channel_name: Optional[str] = Form(None),
):
    """Create and start a new failover recording session.

    With `rebroadcast=true` nothing is kept: the stream is captured into a
    bounded ring and served straight to Plex/Emby/Jellyfin as a live channel.
    Failover, freeze detection and the guide entry all work exactly as they
    do for a recording -- only the destination differs.
    """
    candidates = [u for u in [url_primary, url_backup1, url_backup2] if u and u.strip()]
    if not candidates:
        raise HTTPException(status_code=400, detail="At least one candidate stream URL is required")

    # /api/probe already caps this; start did not, so an arbitrarily large URL
    # could be held in memory and (once sessions are persisted) written to disk.
    for candidate_url in candidates:
        if len(candidate_url) > 4096:
            raise HTTPException(status_code=400, detail="URL is too long")
        # FFmpeg will open file://, concat: and tcp:// just as readily as
        # https://, and the captured bytes are readable back through the
        # stream and download endpoints. Refuse anything that is not a real
        # stream URL here, at the boundary, rather than at the argv list.
        try:
            safe_stream_url(candidate_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if not 1 <= freeze_timeout <= 600:
        raise HTTPException(
            status_code=400, detail="freeze_timeout must be between 1 and 600 seconds"
        )

    # output_dir is caller-supplied and gets mkdir'd, so it needs the same
    # containment as the library endpoints -- otherwise it is arbitrary
    # directory creation and file write anywhere the process can reach.
    resolved_output_dir = str(_resolve_library_dir(output_dir)) if output_dir else None

    recording_id = str(uuid.uuid4())[:8]
    if rebroadcast:
        # No recording path: the ring's backing file is the only thing that
        # touches disk, and it is deleted when the channel stops. It still
        # needs a real parent directory, because start_proxy() writes its
        # channels.conf alongside the output path.
        output_path = buffer_dir() / f"{recording_id}.buf"
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=400, detail=f"Could not prepare buffer directory: {exc}"
            )
    else:
        try:
            output_path = storage.get_output_path(
                sport=sport,
                team_a=team_a,
                team_b=team_b,
                resolution=resolution,
                custom_dir=resolved_output_dir,
            )
        except OSError as exc:
            raise HTTPException(
                status_code=400, detail=f"Could not prepare output directory: {exc}"
            )

    # Optional per-URL header overrides from the dashboard, keyed by URL:
    # {"https://.../x.m3u8": {"referer": "...", "user_agent": "...", "cookie": "..."}}
    # Malformed input is ignored rather than fatal -- the recorder probes each
    # candidate itself, so these are a hint, not a requirement.
    header_overrides: Dict[str, Dict[str, str]] = {}
    if stream_headers:
        try:
            parsed = json.loads(stream_headers)
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    if isinstance(value, dict):
                        header_overrides[str(key).strip()] = {
                            field: str(value[field])
                            for field in ("referer", "user_agent", "cookie")
                            if value.get(field)
                        }
        except (ValueError, TypeError):
            logger.warning("Ignoring malformed stream_headers payload")

    # Fail fast rather than starting a capture that the disk guard will abort
    # moments later -- and rather than being the thing that fills the volume.
    min_free_gb = _min_free_gb()
    if min_free_gb > 0:
        try:
            free_gb = shutil.disk_usage(output_path.parent).free / 1024 ** 3
        except OSError:
            free_gb = None
        if free_gb is not None and free_gb < min_free_gb:
            raise HTTPException(
                status_code=507,
                detail=(
                    f"Only {free_gb:.2f} GB free on {output_path.parent}, below "
                    f"the {min_free_gb:.2f} GB floor (PVARR_MIN_FREE_GB). "
                    "Free some space or lower the floor."
                ),
            )

    _prune_finished_sessions()
    port = _allocate_proxy_port()

    record = build_record(
        recording_id=recording_id,
        candidates=candidates,
        output_filepath=str(output_path),
        started_at=time.time(),
        header_overrides=header_overrides,
        freeze_timeout_sec=freeze_timeout,
        min_free_gb=min_free_gb,
        rebroadcast=rebroadcast,
        channel_name=(channel_name or "").strip()
        or _default_channel_name(sport, team_a, team_b),
    )
    recorder = _launch_session(record, port)

    # Runs in the threadpool after the response is sent. Called inline this
    # would block the event loop for up to 15s (three HTTP calls, timeout=5).
    background_tasks.add_task(
        notifier.notify_recording_started,
        recording_id, output_path.name, candidates[0],
    )

    return JSONResponse({
        "status": "success",
        "message": f"Started recording session {recording_id}",
        "session": recorder.get_status_summary()
    })


@app.post("/api/recordings/{recording_id}/stop")
async def stop_recording(recording_id: str):
    """Stop an active recording session."""
    recorder = active_recorders.get(recording_id)
    if not recorder:
        raise HTTPException(status_code=404, detail="Recording session not found")

    # An operator stop genuinely finishes the recording, so _on_complete
    # remuxes and the session record is removed there. Contrast stop_all(),
    # which passes reason="shutdown" precisely so this does not happen.
    recorder.stop(reason="operator")
    return {"status": "success", "message": f"Stopped session {recording_id}"}


@app.post("/api/recordings/{recording_id}/failover")
async def trigger_failover(recording_id: str):
    """Force switch to the next backup stream candidate."""
    recorder = active_recorders.get(recording_id)
    if not recorder:
        raise HTTPException(status_code=404, detail="Recording session not found")

    if not recorder.is_running:
        raise HTTPException(status_code=400, detail="Recording session is not currently running")

    # A single-URL session has nowhere to go: honouring the request would end
    # the recording rather than switch it, and the caller still got a "success".
    # Sessions with several candidates always have a next one now, because the
    # list cycles round to the first.
    if not recorder.has_next_candidate:
        raise HTTPException(
            status_code=400,
            detail=(
                "No backup stream to fail over to: this session was started "
                "with a single URL. Add a backup URL when starting the "
                "recording."
            ),
        )

    recorder.force_failover()
    return {"status": "success", "message": f"Forced failover triggered for {recording_id}"}


@app.post("/api/recordings/{recording_id}/switch")
async def switch_candidate(recording_id: str, candidate: int = Form(...)):
    """Move a running recording to a specific candidate, 1-based.

    Failover only ever moves to the *next* stream, so there was no way back to
    the primary once it recovered -- which is the common case, since the usual
    failure is a token that expires and is reissued minutes later.
    """
    recorder = active_recorders.get(recording_id)
    if not recorder:
        raise HTTPException(status_code=404, detail="Recording session not found")

    if not recorder.is_running:
        raise HTTPException(status_code=400, detail="Recording session is not currently running")

    total = len(recorder.candidates)
    if not 1 <= candidate <= total:
        raise HTTPException(
            status_code=400,
            detail=f"Candidate must be between 1 and {total} for this session",
        )

    if not recorder.switch_to_candidate(candidate - 1):
        raise HTTPException(
            status_code=400,
            detail=f"Already recording candidate {candidate}",
        )

    return {
        "status": "success",
        "message": f"Switching {recording_id} to candidate {candidate}",
    }


@app.get("/api/recordings/{recording_id}/logs")
async def stream_logs(recording_id: str):
    """SSE endpoint for streaming real-time log updates."""
    recorder = active_recorders.get(recording_id)
    if not recorder:
        raise HTTPException(status_code=404, detail="Session not found")

    async def log_generator():
        # A sequence number, not an index. log_history is trimmed to its last
        # LOG_HISTORY_LIMIT lines, so an index into it stops advancing once
        # trimming starts -- the "anything new?" test could then never be true
        # again and the live log view sat frozen for the rest of the session.
        last_seq = 0
        while True:
            lines, last_seq = recorder.logs_since(last_seq)
            for line in lines:
                yield f"data: {json.dumps({'log': line, 'summary': recorder.get_status_summary()})}\n\n"

            if not recorder.is_running and not lines:
                yield f"data: {json.dumps({'log': '[END] Session completed.', 'summary': recorder.get_status_summary()})}\n\n"
                break

            await asyncio.sleep(0.8)

    return StreamingResponse(log_generator(), media_type="text/event-stream")


# How long to keep a tuner client connected while the recorder is running but
# producing nothing. Generous: a failover cycle (freeze timeout, proxy retry,
# candidate switch) can legitimately leave a gap of a minute or so.
STREAM_IDLE_TIMEOUT_SEC = 300


async def _tail_ring(recorder, recording_id: str):
    """Serve a rebroadcast channel from its bounded ring.

    Every viewer reads the same ring at their own absolute offset, so N clients
    still cost exactly one upstream pull -- which is the whole point, since
    re-fetching a session-gated stream per viewer is how an account gets
    throttled.

    A viewer always joins at the live edge. Replaying the buffer would put
    every client a minute behind the event and further behind on every
    reconnect, and this is a live channel, not a recording.

    Nothing here can apply backpressure to the capture thread. A client that
    stops reading is lapped and resynchronises; it never slows the stream down
    for anyone else.
    """
    ring = recorder.ring
    offset = ring.live_offset()
    idle = 0.0
    try:
        while True:
            chunk, offset = await asyncio.to_thread(ring.read, offset, 65536)
            if chunk:
                idle = 0.0
                yield chunk
                continue
            if not recorder.is_running or ring.closed:
                break
            await asyncio.sleep(0.25)
            idle += 0.25
            if idle >= STREAM_IDLE_TIMEOUT_SEC:
                logger.warning(
                    "Rebroadcast stream %s idle for %ss; closing", recording_id, idle
                )
                break
    except asyncio.CancelledError:
        # Client hung up (Plex switching channels). Normal, not an error.
        raise


@app.get("/api/recordings/{recording_id}/stream")
async def stream_recording(recording_id: str, live: bool = False):
    """Serve an in-progress recording as a continuous MPEG-TS feed.

    This is the URL the /live tuner playlist advertises: Plex and Emby open it
    as a live channel and read until the far end stops sending. Because
    failover appends to the same output file, a mid-event switch to a backup
    URL is invisible to the client -- the bytes simply keep arriving.

    By default the feed starts at the beginning of the recording, so a client
    joining late still gets the whole event. Pass ?live=true to join at the
    current write position instead.
    """
    recorder = active_recorders.get(recording_id)
    if not recorder:
        raise HTTPException(status_code=404, detail="Recording session not found")

    if recorder.is_rebroadcast:
        return StreamingResponse(
            _tail_ring(recorder, recording_id), media_type="video/mp2t"
        )

    path = Path(recorder.output_filepath)

    # The file only appears once the first chunk lands.
    for _ in range(100):
        if path.exists():
            break
        if not recorder.is_running:
            raise HTTPException(status_code=404, detail="Recording produced no data")
        await asyncio.sleep(0.1)
    else:
        raise HTTPException(
            status_code=503, detail="Recording has not started producing data yet"
        )

    async def tail():
        handle = await asyncio.to_thread(open, path, "rb")
        try:
            if live:
                await asyncio.to_thread(handle.seek, 0, os.SEEK_END)
            idle = 0.0
            while True:
                chunk = await asyncio.to_thread(handle.read, 65536)
                if chunk:
                    idle = 0.0
                    yield chunk
                    continue
                if not recorder.is_running:
                    # Drain whatever was written between the last read and the
                    # recorder stopping, then end the response cleanly.
                    final = await asyncio.to_thread(handle.read, 65536)
                    if final:
                        yield final
                        continue
                    break
                await asyncio.sleep(0.25)
                idle += 0.25
                if idle >= STREAM_IDLE_TIMEOUT_SEC:
                    logger.warning(
                        "Tuner stream %s idle for %ss; closing", recording_id, idle
                    )
                    break
        except asyncio.CancelledError:
            # Client hung up (Plex switching channels). Normal, not an error.
            raise
        finally:
            await asyncio.to_thread(handle.close)

    return StreamingResponse(
        tail(),
        media_type="video/mp2t",
        headers={"Cache-Control": "no-cache, no-store", "Accept-Ranges": "none"},
    )


@app.get("/api/library")
async def list_library(dir_path: Optional[str] = None):
    """List completed recordings in library."""
    items = storage.list_recordings(str(_resolve_library_dir(dir_path)))
    return {"library": items}


@app.post("/api/library/rename")
async def rename_file(old_name: str = Form(...), new_name: str = Form(...), dir_path: Optional[str] = Form(None)):
    """Rename a recording file in library."""
    src = _safe_filename(old_name)
    target_dir = _resolve_library_dir(dir_path)
    _refuse_if_recording(target_dir / src, "rename")
    success = storage.rename_recording(
        src, _safe_filename(new_name), str(target_dir),
    )
    if not success:
        raise HTTPException(status_code=400, detail="Rename failed. File might not exist or target name already exists.")
    return {"status": "success", "message": f"Renamed {old_name} to {new_name}"}


@app.delete("/api/library/{filename}")
async def delete_file(filename: str, dir_path: Optional[str] = None):
    """Delete a recording file from library."""
    name = _safe_filename(filename)
    target_dir = _resolve_library_dir(dir_path)
    _refuse_if_recording(target_dir / name, "delete")
    success = storage.delete_recording(name, str(target_dir))
    if not success:
        raise HTTPException(status_code=404, detail="File not found")
    return {"status": "success", "message": f"Deleted {filename}"}


@app.get("/api/library/download/{filename}")
async def download_file(filename: str, dir_path: Optional[str] = None):
    """Download or stream recording file."""
    target_dir = _resolve_library_dir(dir_path)
    filename = _safe_filename(filename)
    file_path = target_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    # Was hardcoded to MPEG-TS, so a remuxed .mp4 downloaded with a Content-Type
    # that contradicted its contents.
    return FileResponse(
        path=file_path, filename=filename, media_type=media_type_for(filename)
    )
