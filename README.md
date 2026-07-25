# SpotWire

Personal Spotify → local audio downloader. A clean v2 rewrite (v1 was a macOS
Electron/DMG desktop app; see the archived `spotwire-v1` repo).

## Architecture (current slice)

A single **FastAPI** server (Python) that runs [spotdl](https://github.com/spotDL/spotify-downloader)
in-process to download tracks/playlists. spotdl shells out to **yt-dlp** (audio
source) and **ffmpeg** (transcode/tag).

Client will be an **Expo / React Native** app (iOS first, Android later).

Deliberately deferred until needed: multi-user + auth (better-auth in a Node
service, added in front later), a job queue / scheduler for automated downloads,
object storage, and yt-dlp proxying for datacenter IPs.

```
spotwire/
  server/            # FastAPI + spotdl (this repo, for now)
    app/main.py      # FastAPI app: /health, /version
    scripts/         # smoke_spotdl.py — prove the pipeline
    Dockerfile       # python:3.12-slim + ffmpeg + uv
  client/            # Expo app (not built yet)
```

## Server — quickstart

Requires [uv](https://docs.astral.sh/uv/) and ffmpeg (`brew install ffmpeg`).

```bash
cd server
uv sync

# Prove the spotdl pipeline (fast, no audio downloaded):
uv run python scripts/smoke_spotdl.py

# Full end-to-end (downloads one track into server/downloads/):
uv run python scripts/smoke_spotdl.py --download

# Run the API:
uv run uvicorn app.main:app --reload
# -> http://127.0.0.1:8000/health   http://127.0.0.1:8000/version
```

### Docker

```bash
cd server
docker build -t spotwire-server .
docker run --rm -p 8000:8000 spotwire-server
```

## Spotify credentials

Optional — spotdl has built-in shared credentials. For heavier use, copy
`server/.env.example` to `server/.env` and add your own from the
[Spotify dashboard](https://developer.spotify.com/dashboard).
