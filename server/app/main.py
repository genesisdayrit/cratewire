"""Cratewire — FastAPI entrypoint.

Minimal for now: a health check and a version endpoint that proves spotdl is
importable inside the running server process. Download/queue/auth all come later.
The APScheduler lifespan is wiring only — no real jobs yet.
"""

from __future__ import annotations

import importlib.metadata as md
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.scheduler import shutdown_scheduler, start_scheduler


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start the in-process scheduler on boot; shut it down cleanly on exit."""
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(title="Cratewire", version="0.1.0", lifespan=lifespan)


def _pkg_version(name: str) -> str:
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return "not-installed"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/version")
def version() -> dict[str, str]:
    """Report the versions the download pipeline depends on."""
    return {
        "cratewire": app.version,
        "spotdl": _pkg_version("spotdl"),
        "yt_dlp": _pkg_version("yt-dlp"),
    }
