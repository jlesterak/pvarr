#!/usr/bin/env python3
"""
Graceful Process Cleanup Module - PVArr
Registers SIGINT and SIGTERM handlers to stop all active background ffmpeg and proxy processes cleanly.
"""

import logging
import signal
import sys
from typing import Dict, Any

logger = logging.getLogger("ProcessCleanup")


def register_signal_handlers(active_recorders_ref: dict):
    """
    Register OS signal handlers (SIGINT, SIGTERM) to stop all active recorders gracefully.
    """
    def _handle_signal(sig, frame):
        sig_name = "SIGINT" if sig == signal.SIGINT else "SIGTERM"
        logger.info(f"Received {sig_name} signal! Initiating graceful shutdown of active recordings...")
        
        for rec_id, recorder in list(active_recorders_ref.items()):
            try:
                logger.info(f"Stopping active recorder session: {rec_id}")
                recorder.stop()
            except Exception as e:
                logger.error(f"Error stopping recorder {rec_id}: {e}")
                
        logger.info("All active recordings stopped cleanly. Exiting.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
