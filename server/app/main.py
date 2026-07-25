"""SpotWire v2 — FastAPI entrypoint.

Minimal for now: a health check and a version endpoint that proves spotdl is
importable inside the running server process. Download/queue/auth all come later.
"""

from __future__ import annotations

import importlib.metadata as md

from fastapi import FastAPI

app = FastAPI(title="SpotWire", version="0.1.0")


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
        "spotwire": app.version,
        "spotdl": _pkg_version("spotdl"),
        "yt_dlp": _pkg_version("yt-dlp"),
    }
