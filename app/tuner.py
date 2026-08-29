#!/usr/bin/env python3
"""
PVArr Virtual IPTV & Tuner Generator Module
Generates dynamic M3U tuner playlists and XMLTV EPG data for Plex Live TV and Emby DVR integration.
"""

from typing import List, Dict, Any


def generate_m3u_playlist(active_sessions: List[Dict[str, Any]], host_url: str) -> str:
    """
    Generate M3U tuner playlist for active PVArr streams.
    """
    lines = ["#EXTM3U"]
    for idx, session in enumerate(active_sessions, start=1):
        if session.get("is_running"):
            filename = session.get("output_filename", f"Channel {idx}")
            stream_url = f"{host_url.rstrip('/')}/api/recordings/{session['id']}/stream"
            lines.append(f'#EXTINF:-1 tvg-id="{session["id"]}" tvg-name="{filename}" group-title="PVArr DVR",{filename}')
            lines.append(stream_url)
    return "\n".join(lines)


def generate_xmltv_epg(active_sessions: List[Dict[str, Any]]) -> str:
    """
    Generate minimal XMLTV EPG data for IPTV tuners.
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
        '<tv generator-info-name="PVArr">'
    ]

    for session in active_sessions:
        lines.append(f'  <channel id="{session["id"]}">')
        lines.append(f'    <display-name>{session.get("output_filename", "PVArr Live Stream")}</display-name>')
        lines.append('  </channel>')

    lines.append('</tv>')
    return "\n".join(lines)
