#!/usr/bin/env python3
"""
PVArr Virtual IPTV & Tuner Generator Module
Generates dynamic M3U tuner playlists and XMLTV EPG data for Plex Live TV and
Emby DVR integration.

Each active recording is advertised as a live channel whose stream URL is
/api/recordings/{id}/stream -- a continuous MPEG-TS feed tailing the file as it
is written. Failover appends to that same file, so a mid-event switch to a
backup URL is invisible to the client.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
from xml.sax.saxutils import escape, quoteattr

# Plex will not display a channel that has no programme in the guide, and a
# live recording has no known end time. Advertise a generous window.
PROGRAMME_WINDOW_HOURS = 6


def _xmltv_time(dt: datetime) -> str:
    """Format a datetime as XMLTV expects: YYYYMMDDHHMMSS +0000."""
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def _channel_title(session: Dict[str, Any], index: int) -> str:
    """Human-readable channel name, without the .ts extension."""
    name = session.get("output_filename") or f"Channel {index}"
    return name[:-3] if name.endswith(".ts") else name


def generate_m3u_playlist(active_sessions: List[Dict[str, Any]], host_url: str) -> str:
    """Generate an M3U tuner playlist for the active PVArr streams."""
    lines = ["#EXTM3U"]
    for idx, session in enumerate(active_sessions, start=1):
        if not session.get("is_running"):
            continue
        session_id = str(session["id"])
        title = _channel_title(session, idx)
        stream_url = f"{host_url.rstrip('/')}/api/recordings/{session_id}/stream"
        # quoteattr keeps a title containing quotes from breaking the attributes.
        lines.append(
            f"#EXTINF:-1"
            f" tvg-id={quoteattr(session_id)}"
            f" tvg-name={quoteattr(title)}"
            f' group-title="PVArr DVR",{title}'
        )
        lines.append(stream_url)
    return "\n".join(lines)


def generate_xmltv_epg(active_sessions: List[Dict[str, Any]]) -> str:
    """Generate XMLTV guide data for the active tuner channels.

    Filtered to running sessions so the guide matches the playlist exactly.
    Advertising a channel here that the M3U omits leaves Plex holding guide
    entries for channels it cannot tune.
    """
    running = [s for s in active_sessions if s.get("is_running")]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
        '<tv generator-info-name="PVArr">',
    ]

    for idx, session in enumerate(running, start=1):
        session_id = str(session["id"])
        title = _channel_title(session, idx)
        lines.append(f"  <channel id={quoteattr(session_id)}>")
        lines.append(f"    <display-name>{escape(title)}</display-name>")
        lines.append("  </channel>")

    for idx, session in enumerate(running, start=1):
        session_id = str(session["id"])
        title = _channel_title(session, idx)
        started = session.get("started_at") or time.time()
        start_dt = datetime.fromtimestamp(started, tz=timezone.utc)
        stop_dt = start_dt + timedelta(hours=PROGRAMME_WINDOW_HOURS)
        lines.append(
            f"  <programme start=\"{_xmltv_time(start_dt)}\""
            f" stop=\"{_xmltv_time(stop_dt)}\""
            f" channel={quoteattr(session_id)}>"
        )
        lines.append(f"    <title lang=\"en\">{escape(title)}</title>")
        lines.append(
            f"    <desc lang=\"en\">{escape('PVArr live recording ' + session_id)}</desc>"
        )
        lines.append("  </programme>")

    lines.append("</tv>")
    return "\n".join(lines)
