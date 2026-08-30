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

import hashlib
import os
import socket
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
    numbers = assign_channel_numbers(active_sessions)
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
            f" tvg-chno={quoteattr(str(numbers[session_id]))}"
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

    numbers = assign_channel_numbers(active_sessions)
    for idx, session in enumerate(running, start=1):
        session_id = str(session["id"])
        title = _channel_title(session, idx)
        lines.append(f"  <channel id={quoteattr(session_id)}>")
        lines.append(f"    <display-name>{escape(title)}</display-name>")
        # Second display-name carries the HDHomeRun channel number: that is
        # how Plex matches this guide onto a lineup.json entry.
        lines.append(f"    <display-name>{numbers[session_id]}</display-name>")
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


# --------------------------------------------------------------------------
# HDHomeRun tuner emulation
#
# Plex's "Set up Live TV" flow probes a device address for discover.json,
# lineup_status.json and lineup.json before it will offer an M3U/XMLTV path.
# Emulating a HDHomeRun is the more reliable of the two integrations: Plex
# tunes it natively instead of re-parsing a playlist on every scan.
# --------------------------------------------------------------------------

# Model/firmware strings a stock HDHomeRun reports. Plex keys its capability
# checks off these, so they are copied verbatim rather than branded.
HDHR_MODEL = "HDTC-2US"
HDHR_FIRMWARE = "hdhomeruntc_atsc"
HDHR_FIRMWARE_VERSION = "20200101"

# Channel numbers start above the usual broadcast range so a PVArr channel
# never collides with a real one in a mixed Plex lineup.
FIRST_CHANNEL_NUMBER = 1000

# session id -> channel number, stable for as long as the session is running.
# Plex remembers a channel by its number; renumbering live channels on every
# scan would shuffle the guide underneath it.
_channel_numbers: Dict[str, int] = {}


def _running(active_sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [s for s in active_sessions if s.get("is_running")]


def assign_channel_numbers(active_sessions: List[Dict[str, Any]]) -> Dict[str, int]:
    """Map each running session to a stable channel number.

    Numbers of sessions that have stopped are released for reuse, which also
    keeps the registry from growing for the life of the process.
    """
    ids = [str(s["id"]) for s in _running(active_sessions)]
    for gone in set(_channel_numbers) - set(ids):
        del _channel_numbers[gone]
    for sid in ids:
        if sid in _channel_numbers:
            continue
        used = set(_channel_numbers.values())
        number = FIRST_CHANNEL_NUMBER
        while number in used:
            number += 1
        _channel_numbers[sid] = number
    return dict(_channel_numbers)


def device_id() -> str:
    """Stable 8-hex-digit device id, the way a real HDHomeRun reports one.

    Plex keys the DVR it creates off this value, so it must survive a restart
    or Plex adopts the tuner as a second, empty device.
    """
    override = os.environ.get("PVARR_DEVICE_ID")
    if override:
        return override.strip().upper()[:8].rjust(8, "0")
    seed = f"pvarr:{socket.gethostname()}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8].upper()


def tuner_count() -> int:
    """Number of concurrent tuners advertised to Plex."""
    try:
        count = int(os.environ.get("PVARR_TUNER_COUNT", "4"))
    except ValueError:
        return 4
    return max(1, min(count, 32))


def generate_discover(base_url: str) -> Dict[str, Any]:
    """The discover.json payload Plex fetches first."""
    base = base_url.rstrip("/")
    return {
        "FriendlyName": "PVArr",
        "Manufacturer": "Silicondust",
        "ModelNumber": HDHR_MODEL,
        "FirmwareName": HDHR_FIRMWARE,
        "FirmwareVersion": HDHR_FIRMWARE_VERSION,
        "DeviceID": device_id(),
        "DeviceAuth": "pvarr",
        "BaseURL": base,
        "LineupURL": f"{base}/lineup.json",
        "TunerCount": tuner_count(),
    }


def generate_lineup_status() -> Dict[str, Any]:
    """Report a completed channel scan; PVArr's lineup is always current."""
    return {
        "ScanInProgress": 0,
        "ScanPossible": 1,
        "Source": "Cable",
        "SourceList": ["Cable"],
    }


def generate_lineup(active_sessions: List[Dict[str, Any]],
                    host_url: str) -> List[Dict[str, Any]]:
    """The channel lineup: one entry per running recording."""
    numbers = assign_channel_numbers(active_sessions)
    host = host_url.rstrip("/")
    lineup = []
    for idx, session in enumerate(_running(active_sessions), start=1):
        session_id = str(session["id"])
        lineup.append({
            "GuideNumber": str(numbers[session_id]),
            "GuideName": _channel_title(session, idx),
            "URL": f"{host}/api/recordings/{session_id}/stream",
            "HD": 1,
        })
    return lineup


def generate_device_xml(base_url: str) -> str:
    """UPnP device description, which Plex reads to confirm the device type."""
    base = base_url.rstrip("/")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<root xmlns="urn:schemas-upnp-org:device-1-0">\n'
        "  <specVersion><major>1</major><minor>0</minor></specVersion>\n"
        f"  <URLBase>{escape(base)}</URLBase>\n"
        "  <device>\n"
        "    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>\n"
        "    <friendlyName>PVArr</friendlyName>\n"
        "    <manufacturer>Silicondust</manufacturer>\n"
        f"    <modelName>{HDHR_MODEL}</modelName>\n"
        f"    <modelNumber>{HDHR_MODEL}</modelNumber>\n"
        f"    <serialNumber>{device_id()}</serialNumber>\n"
        f"    <UDN>uuid:{device_id()}</UDN>\n"
        "  </device>\n"
        "</root>"
    )
