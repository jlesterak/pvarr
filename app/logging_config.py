#!/usr/bin/env python3
"""
Central logging configuration for PVArr.

Library modules must not call logging.basicConfig() at import time: whichever
one is imported first wins, the rest are silently ignored, and the root logger
gets reconfigured out from under the application. Configuration belongs to the
entry points (app/server.py and stream-recorder.py), which call configure_logging()
exactly once.
"""

import logging
import re
import os
import sys

_CONFIGURED = False


def configure_logging(level: str = None) -> None:
    """Configure root logging once. Honours PVARR_LOG_LEVEL (default INFO)."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    resolved = (level or os.environ.get("PVARR_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, resolved, logging.INFO),
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # These are chatty at DEBUG and add nothing operationally useful.
    for noisy in ("urllib3", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


# Anything after "?" in a stream URL is where the access token lives. These
# reach three places a person can read: the in-memory log history served by
# /api/status and the log endpoint, the container's stdout, and the text of a
# Discord or Telegram notification -- which leaves the network entirely and
# lands in a third party's message history, where it cannot be expired or
# deleted. The host and path are kept, because that is what identifies which
# candidate is talking and is the whole diagnostic value of the line.
_URL_WITH_SECRETS = re.compile(
    r"""(?ix)
    \b(https?://)                 # scheme
    (?:[^/\s'"<>@]+@)?            # optional user:pass@, dropped entirely
    ([^/\s'"<>?\#]+)              # host[:port]
    ([^\s'"<>?\#]*)               # path
    (?:\?[^\s'"<>]*)?             # query -- the part that carries the token
    (?:\#[^\s'"<>]*)?             # fragment
    """
)


def redact_url_secrets(text: str) -> str:
    """Strip credentials, query strings and fragments from URLs in a string.

    Deliberately applied at the log sink rather than at each call site: a
    redaction you have to remember to call is one that gets forgotten at the
    next call site added, and the URLs here are not all ours -- a token can
    arrive inside FFmpeg's own error text.
    """
    if not text:
        return text
    if not isinstance(text, str):
        # A sink must not be the thing that raises. Coerced rather than
        # returned untouched, so an object whose repr embeds a URL is still
        # scrubbed rather than waved through.
        text = str(text)

    def _clean(match: "re.Match") -> str:
        scheme, host, path = match.group(1), match.group(2), match.group(3)
        had_secret = match.group(0) != f"{scheme}{host}{path}"
        return f"{scheme}{host}{path}" + ("?<redacted>" if had_secret else "")

    return _URL_WITH_SECRETS.sub(_clean, text)
