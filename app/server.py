#!/usr/bin/env python3
"""
Web Server Backend Module - Stream Failover Studio
Provides REST API & SSE streaming endpoints for multi-stream failover recording control.
"""

import asyncio
import json
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

app = FastAPI(title="Stream Failover Studio", version="1.0.0")

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "app" / "templates"
RECORDINGS_DIR = BASE_DIR / "recordings"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
storage = StorageManager(record_dir=str(RECORDINGS_DIR))

# Active recorder sessions: recording_id -> StreamFailoverRecorder
active_recorders: Dict[str, StreamFailoverRecorder] = {}

# Register SIGINT / SIGTERM handlers for container & process safety
register_signal_handlers(active_recorders)



@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
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
    recorder = StreamFailoverRecorder(
        recording_id=recording_id,
        candidates=candidates,
        output_filepath=str(output_path),
        base_port=port,
        freeze_timeout_sec=freeze_timeout
    )

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
    items = storage.list_recordings(dir_path)
    return {"library": items}


@app.post("/api/library/rename")
async def rename_file(old_name: str = Form(...), new_name: str = Form(...), dir_path: Optional[str] = Form(None)):
    """Rename a recording file in library."""
    success = storage.rename_recording(old_name, new_name, dir_path)
    if not success:
        raise HTTPException(status_code=400, detail="Rename failed. File might not exist or target name already exists.")
    return {"status": "success", "message": f"Renamed {old_name} to {new_name}"}


@app.delete("/api/library/{filename}")
async def delete_file(filename: str, dir_path: Optional[str] = None):
    """Delete a recording file from library."""
    success = storage.delete_recording(filename, dir_path)
    if not success:
        raise HTTPException(status_code=404, detail="File not found")
    return {"status": "success", "message": f"Deleted {filename}"}


@app.get("/api/library/download/{filename}")
async def download_file(filename: str, dir_path: Optional[str] = None):
    """Download or stream recording file."""
    target_dir = Path(dir_path).resolve() if dir_path else RECORDINGS_DIR
    file_path = target_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=file_path, filename=filename, media_type="video/MP2T")
