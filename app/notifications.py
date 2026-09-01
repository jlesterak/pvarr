#!/usr/bin/env python3
"""
PVArr Notifications & Media Server Refresh

Notification delivery goes through Apprise, which speaks 100+ services behind
one interface. This replaced hand-rolled Discord and Telegram senders: two
bespoke payload formats, two error paths, two places to forget to redact a
token, and no way to reach anything else without writing a third.

Existing configuration keeps working. `DISCORD_WEBHOOK_URL`,
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are translated into Apprise URLs at
startup, so nobody has to rewrite a working `.env` to upgrade. Anything Apprise
supports can be added directly through `PVARR_APPRISE_URLS` -- ntfy, Gotify,
Pushover, Matrix, Slack, email, a plain webhook.

The Plex and Emby library refreshes are deliberately NOT Apprise. They are not
notifications; they are API calls to a specific endpoint with a specific token,
and pretending otherwise would obscure what they do.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import requests

from app.logging_config import redact_url_secrets

logger = logging.getLogger("PVArrNotifications")

try:
    import apprise
    APPRISE_AVAILABLE = True
except ImportError:      # optional at runtime; PVArr records fine without it
    APPRISE_AVAILABLE = False

# https://discord.com/api/webhooks/<id>/<token>  ->  discord://<id>/<token>
_DISCORD_WEBHOOK = re.compile(
    r"^https?://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/(\d+)/([\w-]+)", re.I)


def _split_urls(raw: str) -> List[str]:
    """Apprise URLs from a config string, comma- or whitespace-separated."""
    return [part for part in re.split(r"[,\s]+", raw or "") if part.strip()]


def discord_to_apprise(webhook_url: str) -> Optional[str]:
    """Translate a Discord webhook URL into Apprise's scheme.

    Kept as a translation rather than asking operators to re-enter their
    webhook in a new format: the value in their `.env` already works, and an
    upgrade that silently stops notifying is worse than one that never started.
    """
    match = _DISCORD_WEBHOOK.match((webhook_url or "").strip())
    if not match:
        return None
    return f"discord://{match.group(1)}/{match.group(2)}"


class NotificationManager:
    def __init__(self):
        self.plex_url = os.getenv("PLEX_URL", "")     # e.g. http://192.168.1.50:32400
        self.plex_token = os.getenv("PLEX_TOKEN", "")
        self.emby_url = os.getenv("EMBY_URL", "")     # e.g. http://192.168.1.50:8096
        self.emby_api_key = os.getenv("EMBY_API_KEY", "")
        self.targets: List[str] = self._build_targets()
        if self.targets and not APPRISE_AVAILABLE:
            logger.warning(
                "Notification targets are configured but apprise is not installed; "
                "no notifications will be sent."
            )

    # -- configuration -----------------------------------------------------

    def _build_targets(self) -> List[str]:
        """Every configured destination, as Apprise URLs."""
        targets: List[str] = []

        webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        if webhook:
            translated = discord_to_apprise(webhook)
            if translated:
                targets.append(translated)
            else:
                logger.warning(
                    "DISCORD_WEBHOOK_URL does not look like a Discord webhook; "
                    "ignoring it. Expected https://discord.com/api/webhooks/<id>/<token>"
                )

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if token and chat:
            targets.append(f"tgram://{token}/{chat}")

        targets.extend(_split_urls(os.getenv("PVARR_APPRISE_URLS", "")))
        return targets

    # -- delivery ----------------------------------------------------------

    def send(self, title: str, body: str) -> bool:
        """Deliver one message to every configured target.

        The single choke point for outbound text, which is why redaction lives
        here. A notification leaves the network for good -- there is no taking
        it back out of a Discord channel and no expiring it -- so a stream
        token must never reach this call, whatever a caller passes in.
        """
        if not self.targets or not APPRISE_AVAILABLE:
            return False
        payload = apprise.Apprise()
        for url in self.targets:
            if not payload.add(url):
                logger.warning("Ignoring unusable notification target: %s",
                               redact_url_secrets(url))
        if not len(payload):
            return False
        try:
            return bool(payload.notify(
                title=redact_url_secrets(title),
                body=redact_url_secrets(body),
            ))
        except Exception as exc:
            logger.error("Notification failed: %s", redact_url_secrets(str(exc)))
            return False

    # -- events ------------------------------------------------------------

    def notify_recording_started(self, session_id: str, filename: str, candidate_name: str):
        self.send(
            "PVArr — Recording Started 🎥",
            f"Session: {session_id}\nFile: {filename}\nStream: {candidate_name}",
        )

    def notify_failover_triggered(self, session_id: str, next_candidate_name: str):
        self.send(
            "PVArr — Stream Failover Triggered ⚠️",
            f"Session: {session_id}\nSwitched to: {next_candidate_name}",
        )

    def notify_recording_finished(self, session_id: str, filename: str, size_mb: float):
        self.send(
            "PVArr — Recording Finished ✅",
            f"Session: {session_id}\nFile: {filename}\nSize: {size_mb} MB",
        )
        self.trigger_media_server_refresh()

    # -- media servers -----------------------------------------------------

    def trigger_media_server_refresh(self):
        """Ask Plex and Emby to rescan. Not a notification -- an API call."""
        if self.plex_url and self.plex_token:
            url = f"{self.plex_url.rstrip('/')}/library/sections/all/refresh?X-Plex-Token={self.plex_token}"
            try:
                requests.get(url, timeout=5)
                logger.info("Plex library refresh triggered successfully.")
            except Exception as e:
                # requests puts the failing URL in its exception text, and that
                # URL carries the Plex token.
                logger.error("Plex refresh failed: %s", redact_url_secrets(str(e)))

        if self.emby_url and self.emby_api_key:
            url = f"{self.emby_url.rstrip('/')}/Library/Refresh?api_key={self.emby_api_key}"
            try:
                requests.post(url, timeout=5)
                logger.info("Emby/Jellyfin library refresh triggered successfully.")
            except Exception as e:
                logger.error("Emby refresh failed: %s", redact_url_secrets(str(e)))
