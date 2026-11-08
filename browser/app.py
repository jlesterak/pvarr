"""
PVArr browser sidecar - HTTP API

  POST /resolve  {"url": "...", "timeout": 45}  -> playlist URL + headers, see resolver.py
  GET  /health                                  -> {"ok": true, "busy": n}

At most MAX_PAGES pages are open at once (each is ~0.5 GB of RAM); a request
beyond that gets 503 at once rather than queueing behind a slow page. A page
that resolved is remembered for CACHE_SEC, so a failover minutes later costs
nothing. Not published on a host port: only PVArr, on the same Docker
network, talks to it.
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from netguard import page_url_problem
from resolver import Resolver

MAX_PAGES = max(1, int(os.environ.get("PVARR_BROWSER_MAX_PAGES", "2")))
CACHE_SEC = 300.0
TIMEOUT_MIN, TIMEOUT_MAX, TIMEOUT_DEFAULT = 5.0, 60.0, 45.0
CACHE_MAX = 200

resolver = Resolver()
_busy = 0
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await resolver.close()


app = FastAPI(title="PVArr browser sidecar", lifespan=lifespan)


class ResolveRequest(BaseModel):
    url: str
    timeout: Optional[float] = None


def clamp_timeout(value: Optional[float]) -> float:
    if value is None:
        return TIMEOUT_DEFAULT
    return min(TIMEOUT_MAX, max(TIMEOUT_MIN, float(value)))


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "busy": _busy, "max_pages": MAX_PAGES}


@app.post("/resolve")
async def resolve(req: ResolveRequest) -> Dict[str, Any]:
    global _busy
    problem = await asyncio.to_thread(page_url_problem, req.url)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    now = time.monotonic()
    hit = _cache.get(req.url)
    if hit and now - hit[0] < CACHE_SEC:
        return {**hit[1], "cached": True}
    if _busy >= MAX_PAGES:
        raise HTTPException(status_code=503, detail="busy")
    _busy += 1
    try:
        result = await resolver.resolve(req.url, clamp_timeout(req.timeout))
    finally:
        _busy -= 1
    if result["ok"]:
        if len(_cache) >= CACHE_MAX:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[req.url] = (time.monotonic(), result)
    return {**result, "cached": False}
