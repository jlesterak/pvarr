#!/usr/bin/env python3
"""
PVArr Notification & Media Server Refresh Integration Module
Handles Webhook alerts (Discord, Telegram) and Media Server Library Refresh API calls (Plex, Emby, Jellyfin).
"""

import json
import logging
import os
import requests
from typing import Dict, Any, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PVArrNotifications")


class NotificationManager:
    def __init__(self):
        self.discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.plex_url = os.getenv("PLEX_URL", "")  # e.g. http://192.168.1.50:32400
        self.plex_token = os.getenv("PLEX_TOKEN", "")
        self.emby_url = os.getenv("EMBY_URL", "")  # e.g. http://192.168.1.50:8096
        self.emby_api_key = os.getenv("EMBY_API_KEY", "")

    def send_discord(self, title: str, description: str, color: int = 3447003):
        """Send Discord webhook embed notification."""
        if not self.discord_webhook_url:
            return

        payload = {
            "embeds": [{
                "title": f"PVArr — {title}",
                "description": description,
                "color": color,
                "footer": {"text": "PVArr Personal Video Recorder"}
            }]
        }
        try:
            requests.post(self.discord_webhook_url, json=payload, timeout=5)
        except Exception as e:
            logger.error(f"Failed to send Discord webhook: {e}")

    def send_telegram(self, message: str):
        """Send Telegram message notification."""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return

        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.telegram_chat_id,
            "text": f"<b>PVArr</b>\n{message}",
            "parse_mode": "HTML"
        }
        try:
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")

    def notify_recording_started(self, session_id: str, filename: str, candidate_name: str):
        msg = f"🎥 <b>Recording Started</b>\nSession: <code>{session_id}</code>\nFile: <code>{filename}</code>\nActive Stream: {candidate_name}"
        self.send_discord("Recording Started 🎥", f"**Session:** `{session_id}`\n**File:** `{filename}`\n**Stream:** {candidate_name}", color=3066993)
        self.send_telegram(msg)

    def notify_failover_triggered(self, session_id: str, next_candidate_name: str):
        msg = f"⚠️ <b>Failover Triggered!</b>\nSession: <code>{session_id}</code>\nSwitched to: {next_candidate_name}"
        self.send_discord("Stream Failover Triggered ⚠️", f"**Session:** `{session_id}`\n**Switched to:** {next_candidate_name}", color=15105570)
        self.send_telegram(msg)

    def notify_recording_finished(self, session_id: str, filename: str, size_mb: float):
        msg = f"✅ <b>Recording Finished</b>\nSession: <code>{session_id}</code>\nFile: <code>{filename}</code>\nSize: {size_mb} MB"
        self.send_discord("Recording Finished ✅", f"**Session:** `{session_id}`\n**File:** `{filename}`\n**Size:** {size_mb} MB", color=3066993)
        self.send_telegram(msg)
        self.trigger_media_server_refresh()

    def trigger_media_server_refresh(self):
        """Trigger Plex & Emby library refresh endpoints."""
        # Plex refresh
        if self.plex_url and self.plex_token:
            url = f"{self.plex_url.rstrip('/')}/library/sections/all/refresh?X-Plex-Token={self.plex_token}"
            try:
                requests.get(url, timeout=5)
                logger.info("Plex library refresh triggered successfully.")
            except Exception as e:
                logger.error(f"Plex refresh failed: {e}")

        # Emby / Jellyfin refresh
        if self.emby_url and self.emby_api_key:
            url = f"{self.emby_url.rstrip('/')}/Library/Refresh?api_key={self.emby_api_key}"
            try:
                requests.post(url, timeout=5)
                logger.info("Emby/Jellyfin library refresh triggered successfully.")
            except Exception as e:
                logger.error(f"Emby refresh failed: {e}")
