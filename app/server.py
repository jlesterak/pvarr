#!/usr/bin/env python3
"""
Web Server Backend Module - Stream Failover Studio
Provides REST API & SSE streaming endpoints for multi-stream failover recording control.
"""

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Dict, Optional, List

from fastapi import FastAPI, Request, Form, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.recorder import StreamFailoverRecorder
from app.naming import StorageManager, generate_sports_filename, probe_video_resolution
from app.cleanup import register_signal_handlers
from app.tuner import generate_m3u_playlist, generate_xmltv_epg
from app.notifications import NotificationManager
from app.post_processor import remux_recording

app = FastAPI(title="PVArr - Personal Video Recorder", version="1.0.0")

notifier = NotificationManager()


BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "app" / "templates"
RECORDINGS_DIR = BASE_DIR / "recordings"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
storage = StorageManager(record_dir=str(RECORDINGS_DIR))

# Active recorder sessions: recording_id -> StreamFailoverRecorder
active_recorders: Dict[str, StreamFailoverRecorder] = {}

# Register SIGINT / SIGTERM handlers for container & process safety
register_signal_handlers(active_recorders)



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

@app.get("/api/status")
async def get_system_status():
    """Return JSON summary of all active recorders."""
    sessions = [r.get_status_summary() for r in active_recorders.values()]
    return {
        "active_count": len([r for r in active_recorders.values() if r.is_running]),
        "total_sessions": len(sessions),
        "sessions": sessions
    }


@app.post("/api/recordings/start")
async def start_recording(
    sport: str = Form("Sports"),
    team_a: str = Form("TeamA"),
    team_b: str = Form("TeamB"),
    resolution: str = Form("1080p"),
    output_dir: Optional[str] = Form(None),
    url_primary: str = Form(...),
    url_backup1: Optional[str] = Form(None),
    url_backup2: Optional[str] = Form(None),
    freeze_timeout: int = Form(15)
):
    """Create and start a new failover recording session."""
    candidates = [u for u in [url_primary, url_backup1, url_backup2] if u and u.strip()]
    if not candidates:
        raise HTTPException(status_code=400, detail="At least one candidate stream URL is required")

    recording_id = str(uuid.uuid4())[:8]
    output_path = storage.get_output_path(
        sport=sport,
        team_a=team_a,
        team_b=team_b,
        resolution=resolution,
        custom_dir=output_dir
    )

    port = 8090 + (len(active_recorders) * 2)

    def _on_complete(filepath):
        sz = round(Path(filepath).stat().st_size / (1024*1024), 2) if Path(filepath).exists() else 0
        notifier.notify_recording_finished(recording_id, Path(filepath).name, sz)
        remux_recording(filepath, target_format="mp4", delete_source=True)

    def _on_failover(rec_id, next_name):
        notifier.notify_failover_triggered(rec_id, next_name)

    recorder = StreamFailoverRecorder(
        recording_id=recording_id,
        candidates=candidates,
        output_filepath=str(output_path),
        base_port=port,
        freeze_timeout_sec=freeze_timeout,
        on_completion_callback=_on_complete,
        on_failover_callback=_on_failover
    )

    notifier.notify_recording_started(recording_id, output_path.name, candidates[0])


    active_recorders[recording_id] = recorder
    recorder.start_recording()

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

    recorder.stop()
    return {"status": "success", "message": f"Stopped session {recording_id}"}


@app.post("/api/recordings/{recording_id}/failover")
async def trigger_failover(recording_id: str):
    """Force switch to the next backup stream candidate."""
    recorder = active_recorders.get(recording_id)
    if not recorder:
        raise HTTPException(status_code=404, detail="Recording session not found")

    if not recorder.is_running:
        raise HTTPException(status_code=400, detail="Recording session is not currently running")

    recorder.force_failover()
    return {"status": "success", "message": f"Forced failover triggered for {recording_id}"}


@app.get("/api/recordings/{recording_id}/logs")
async def stream_logs(recording_id: str):
    """SSE endpoint for streaming real-time log updates."""
    recorder = active_recorders.get(recording_id)
    if not recorder:
        raise HTTPException(status_code=404, detail="Session not found")

    async def log_generator():
        last_sent_idx = 0
        while True:
            history = list(recorder.log_history)
            if len(history) > last_sent_idx:
                for line in history[last_sent_idx:]:
                    yield f"data: {json.dumps({'log': line, 'summary': recorder.get_status_summary()})}\n\n"
                last_sent_idx = len(history)

            if not recorder.is_running and len(history) <= last_sent_idx:
                yield f"data: {json.dumps({'log': '[END] Session completed.', 'summary': recorder.get_status_summary()})}\n\n"
                break

            await asyncio.sleep(0.8)

    return StreamingResponse(log_generator(), media_type="text/event-stream")


@app.get("/api/library")
async def list_library(dir_path: Optional[str] = None):
    """List completed recordings in library."""
    items = storage.list_recordings(str(_resolve_library_dir(dir_path)))
    return {"library": items}


@app.post("/api/library/rename")
async def rename_file(old_name: str = Form(...), new_name: str = Form(...), dir_path: Optional[str] = Form(None)):
    """Rename a recording file in library."""
    success = storage.rename_recording(
        _safe_filename(old_name), _safe_filename(new_name),
        str(_resolve_library_dir(dir_path)),
    )
    if not success:
        raise HTTPException(status_code=400, detail="Rename failed. File might not exist or target name already exists.")
    return {"status": "success", "message": f"Renamed {old_name} to {new_name}"}


@app.delete("/api/library/{filename}")
async def delete_file(filename: str, dir_path: Optional[str] = None):
    """Delete a recording file from library."""
    success = storage.delete_recording(
        _safe_filename(filename), str(_resolve_library_dir(dir_path))
    )
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
    return FileResponse(path=file_path, filename=filename, media_type="video/MP2T")
