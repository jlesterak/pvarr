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


# Anything after "?" in a stream URL is where the access token usually lives.
# These reach three places a person can read: the in-memory log history served
# by /api/status and the log endpoint, the container's stdout, and the text of a
# Discord or Telegram notification -- which leaves the network entirely and
# lands in a third party's message history, where it cannot be expired or
# deleted. The host and path are kept, because that is what identifies which
# candidate is talking and is the whole diagnostic value of the line -- except
# for path segments that are themselves credentials; see _looks_like_token().
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


# Not every provider puts the token after the "?". strmd.st carries it as path
# segments -- /secure/<token>/rtmp/stream/<token>/playlist.m3u8 -- and a
# query-only scrub passed that URL to Discord intact. No provider layout is
# matched here, because those rotate; a segment is treated as a credential when
# it *looks* generated, which is the one property every token shares.
_MIN_TOKEN_CHARS = 16
_TOKEN_SEGMENT = re.compile(r"^[A-Za-z0-9_\-.~+=%]+$")
_HEX = re.compile(r"^[0-9a-fA-F]+$")
_TRAILING_EXT = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def _char_class(c: str) -> int:
    return 0 if c.isdigit() else (1 if c.islower() else 2)


def _looks_like_token(segment: str) -> bool:
    """Is this URL path segment a credential rather than a name?

    Measured by how often the characters change class (digit, lowercase,
    uppercase). A generated token switches every one or two characters; a
    name -- `chunklist`, `index_1080p`, `media_w1234567_b2596000_12345` -- stays
    in one class for long stretches. Hex that mixes digits and letters is
    caught outright, because two classes make long runs likely enough that the
    run test alone missed ~7% of 20-character hex tokens.

    Length is measured on the whole segment, separators included. Counting
    only letters and digits let '-' and '_' push 40% of random 16-character
    base64url tokens under the threshold.

    Errs towards redacting: a false positive costs a log line some diagnostic
    detail, while a false negative puts a live credential in a chat history.
    A CamelCase name of 16+ letters can be caught, and that is accepted.
    """
    if not _TOKEN_SEGMENT.match(segment):
        return False
    stem = _TRAILING_EXT.sub("", segment)
    if len(stem) < _MIN_TOKEN_CHARS:
        return False
    alnum = [c for c in stem if c.isalnum()]
    if len(alnum) < _MIN_TOKEN_CHARS // 2:
        return False  # mostly punctuation; nothing generated about it
    joined = "".join(alnum)
    if (_HEX.match(joined) and any(c.isdigit() for c in joined)
            and any(c.isalpha() for c in joined)):
        return True  # md5/sha digests: nginx secure_link and similar
    runs = 1 + sum(1 for a, b in zip(alnum, alnum[1:])
                   if _char_class(a) != _char_class(b))
    return len(alnum) / runs <= 3.0


def redact_url_secrets(text: str) -> str:
    """Strip credentials, query strings, fragments and token-shaped path
    segments from URLs in a string.

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
        path = "/".join(
            "<redacted>" if _looks_like_token(part) else part
            for part in path.split("/")
        )
        return f"{scheme}{host}{path}" + ("?<redacted>" if had_secret else "")

    return _URL_WITH_SECRETS.sub(_clean, text)
