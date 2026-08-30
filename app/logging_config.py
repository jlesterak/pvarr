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
