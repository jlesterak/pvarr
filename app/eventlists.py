#!/usr/bin/env python3
"""
PVArr saved list pages: pages that link many games (a day's or a league's
fixtures), kept so the dashboard's Events tab can list their games again
without pasting the link.

URL and a name only, never a cookie: a page that needs a browser cookie is
read from the New Recording form instead. Nothing is fetched here; the Events
tab reads a page through /api/event-info, which applies its usual rules.
"""

import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from app.sessions import config_dir

logger = logging.getLogger("PVArrEventLists")

SCHEMA_VERSION = 1
MAX_LIST_PAGES = 20
MAX_LIST_NAME = 80
MAX_LIST_URL = 4096


class ListPageStore:
    """Saved list pages, mirrored to one JSON file like ScheduleStore.

    Never raises at the caller; an unwritable directory keeps pages in memory
    for this run only, and says so once.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else (config_dir() / "event-lists.json")
        self.pages: Dict[str, Dict[str, Any]] = {}
        self.enabled = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._disable(exc)
        self._load()

    def _disable(self, exc: Exception) -> None:
        if self.enabled:
            logger.warning("Saved list pages will not survive a restart (%s). "
                           "Fix ownership of the config mount.", exc)
        self.enabled = False

    def _load(self) -> None:
        if not self.enabled or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable list-page file %s: %s", self.path, exc)
            return
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
            # From a newer PVArr, say: set aside rather than overwritten by the
            # next save, so a downgrade does not lose the list.
            try:
                os.replace(self.path, self.path.with_suffix(".json.bak"))
            except OSError:
                pass
            logger.warning("Set aside list-page file %s: unknown schema (kept as .bak).", self.path)
            return
        for page in (data.get("pages") or [])[:MAX_LIST_PAGES]:
            if isinstance(page, dict) and page.get("id") and page.get("url"):
                self.pages[str(page["id"])] = page

    def save(self) -> bool:
        if not self.enabled:
            return False
        try:
            payload = {"schema": SCHEMA_VERSION, "pages": self.all()}
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tmp-lists-",
                                       suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except (OSError, TypeError, ValueError) as exc:
            self._disable(exc)
            return False

    def add(self, url: str, name: str = "") -> Dict[str, Any]:
        """Save a page, or return the one already saved with that URL.

        Raises ValueError (bad input) or OverflowError (at MAX_LIST_PAGES).
        """
        url, name = (url or "").strip(), " ".join((name or "").split())
        if len(url) > MAX_LIST_URL:
            raise ValueError("Link is too long")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("Link must start with http:// or https://")
        if len(name) > MAX_LIST_NAME:
            raise ValueError(f"Name is longer than {MAX_LIST_NAME} characters")
        for page in self.pages.values():
            if page["url"].rstrip("/") == url.rstrip("/"):
                return page
        if len(self.pages) >= MAX_LIST_PAGES:
            raise OverflowError(f"{MAX_LIST_PAGES} pages are already saved; remove one first")
        page = {"id": uuid.uuid4().hex[:8], "name": name or parts.hostname,
                "url": url, "created_at": time.time()}
        self.pages[page["id"]] = page
        self.save()
        return page

    def remove(self, page_id: str) -> bool:
        if self.pages.pop(page_id, None) is None:
            return False
        self.save()
        return True

    def all(self) -> List[Dict[str, Any]]:
        return sorted(self.pages.values(), key=lambda p: float(p.get("created_at") or 0))
