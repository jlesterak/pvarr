"""
PVArr browser sidecar - HTTP API

  POST /resolve  {"url": "...", "timeout": 45}  -> playlist URL + headers, see resolver.py
  GET  /health                                  -> {"ok": true, "busy": n}

At most MAX_PAGES pages are open at once (each is ~0.5 GB of RAM); a request
beyond that gets 503 at once rather than queueing behind a slow page. Every
request has a hard limit of its timeout plus GRACE_SEC, whatever the page
does. No cache: a resolve costs 5-15 s, and a cached playlist URL would be
the dead one exactly when a failover asks again. Not published on a host
port: only PVArr, on its own Docker network, talks to it.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from netguard import page_url_problem
from resolver import Resolver

MAX_PAGES = max(1, int(os.environ.get("PVARR_BROWSER_MAX_PAGES", "2")))
TIMEOUT_MIN, TIMEOUT_MAX, TIMEOUT_DEFAULT = 5.0, 60.0, 45.0
GRACE_SEC = 15.0

resolver = Resolver()
_busy = 0


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
    if _busy >= MAX_PAGES:
        raise HTTPException(status_code=503, detail="busy")
    timeout = clamp_timeout(req.timeout)
    _busy += 1
    try:
        result = await asyncio.wait_for(resolver.resolve(req.url, timeout), timeout + GRACE_SEC)
    except asyncio.TimeoutError:
        return {"ok": False, "m3u8_url": "", "kind": "", "note": "page did not finish in time"}
    finally:
        _busy -= 1
    # The playlist can be on a private address (a redirect, or a name that
    # resolves to one): never hand that back. PVArr checks again itself.
    if result.get("ok"):
        problem = await asyncio.to_thread(page_url_problem, result["m3u8_url"])
        if problem:
            result.update(ok=False, m3u8_url="", cookie="", note=f"playlist refused: {problem}")
    return result
