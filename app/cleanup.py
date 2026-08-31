#!/usr/bin/env python3
"""
Graceful Process Cleanup Module - PVArr
Stops active recorders on SIGINT/SIGTERM, gives their post-processing a chance
to finish, and then hands the signal back to whoever had it first.
"""

import logging
import os
import signal
import sys
import time
from typing import Dict, Any

logger = logging.getLogger("ProcessCleanup")

# How long the whole shutdown may spend waiting for post-processing. Must sit
# inside the container's stop_grace_period or Docker SIGKILLs us mid-remux.
DEFAULT_SHUTDOWN_TIMEOUT_SEC = 20.0


def _shutdown_timeout() -> float:
    raw = os.environ.get("PVARR_SHUTDOWN_TIMEOUT")
    if raw is None:
        return DEFAULT_SHUTDOWN_TIMEOUT_SEC
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("Ignoring invalid PVARR_SHUTDOWN_TIMEOUT=%r", raw)
        return DEFAULT_SHUTDOWN_TIMEOUT_SEC


def stop_all(active_recorders_ref: dict, timeout: float) -> bool:
    """Stop every recorder, then wait for post-processing within one budget.

    Stopping and waiting are separate passes on purpose: stop() returns as soon
    as FFmpeg is reaped, so stopping everything first lets all the remuxes run
    concurrently against a single shared deadline instead of serially.

    Returns True if everything finished in time.
    """
    recorders = list(active_recorders_ref.items())
    for rec_id, recorder in recorders:
        try:
            logger.info("Stopping active recorder session: %s", rec_id)
            recorder.stop()
        except Exception as exc:
            logger.error("Error stopping recorder %s: %s", rec_id, exc)

    deadline = time.monotonic() + timeout
    all_done = True
    for rec_id, recorder in recorders:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            if recorder.wait_until_finished(remaining):
                continue
        except Exception as exc:
            logger.error("Error waiting on recorder %s: %s", rec_id, exc)
        all_done = False
        logger.warning(
            "Recorder %s did not finish post-processing within the shutdown "
            "budget; its .ts is left on disk un-remuxed.", rec_id,
        )
    return all_done


def register_signal_handlers(active_recorders_ref: dict):
    """Stop recorders on SIGINT/SIGTERM, then chain to the previous handler.

    This used to call sys.exit(0) directly, which had two consequences. It
    killed the daemon recorder threads before their completion block could run,
    so remux and notification were skipped on every container stop. And because
    it is registered at import -- after uvicorn installs its own handlers --
    it *replaced* uvicorn's, so the graceful shutdown that drives the ASGI
    lifespan never ran either.

    Chaining fixes both, and incidentally avoids a deadlock: uvicorn waits for
    in-flight responses before running lifespan shutdown, while the tuner and
    log streams tail until `is_running` goes false. Stopping the recorders
    before handing over is what lets those responses drain.
    """
    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }

    def _handle_signal(sig, frame):
        sig_name = signal.Signals(sig).name
        logger.info("Received %s. Stopping active recordings...", sig_name)

        finished = stop_all(active_recorders_ref, _shutdown_timeout())
        logger.info(
            "Active recordings stopped%s.",
            "" if finished else " (post-processing incomplete)",
        )

        prior = previous.get(sig)
        if callable(prior) and prior not in (signal.SIG_DFL, signal.SIG_IGN):
            # Usually uvicorn's handler: let it run its own graceful shutdown.
            prior(sig, frame)
            return
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
