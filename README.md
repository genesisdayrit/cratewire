# Cratewire

Personal **multi-source music downloader** for building a DJ crate — pull tracks
from SoundCloud / YouTube today, lossless sources (Qobuz/Tidal) later, and land
them somewhere you can sync to a USB and DJ off. Formerly *SpotWire*; v1 was a
macOS Electron/DMG desktop app, archived as the `spotwire-v1` repo.

## Architecture (current slice)

A single **FastAPI** server (Python) that runs download engines in-process and
writes tagged audio to a local folder. Two engines so far:

- **[spotdl](https://github.com/spotDL/spotify-downloader)** → YouTube (great for discovery; lossy)
- **[streamrip](https://github.com/nathom/streamrip)** → SoundCloud (free), and Qobuz/Tidal/Deezer for lossless with credentials

Both shell out to **ffmpeg** for transcode/tagging.

Client will be an **Expo / React Native** app (iOS first, Android later).

Deliberately deferred until needed: multi-user + auth (better-auth in a Node
service, added in front later), a job queue / scheduler for automated downloads,
cloud/Dropbox delivery, and yt-dlp proxying for datacenter IPs.

```
cratewire/
  server/            # FastAPI + download engines (this repo, for now)
    app/main.py      # FastAPI app: /health, /version
    scripts/         # smoke_spotdl.py, smoke_streamrip.py — prove the pipelines
    Dockerfile       # python:3.12-slim + ffmpeg + uv
  client/            # Expo app (not built yet)
```

## Server — quickstart

Requires [uv](https://docs.astral.sh/uv/) and ffmpeg (`brew install ffmpeg`).

```bash
cd server
uv sync

# Prove the spotdl (YouTube) pipeline — fast, no audio:
uv run python scripts/smoke_spotdl.py
uv run python scripts/smoke_spotdl.py --download   # + real download

# Prove the streamrip (SoundCloud, free) pipeline:
uv run python scripts/smoke_streamrip.py

# Run the API:
uv run uvicorn app.main:app --reload
# -> http://127.0.0.1:8000/health   http://127.0.0.1:8000/version
```

### Docker

```bash
cd server
docker build -t cratewire-server .
docker run --rm -p 8000:8000 cratewire-server
```

## Credentials

- **spotdl** works with built-in shared Spotify credentials — none needed for
  dev. For heavier use, copy `server/.env.example` to `server/.env` and add your
  own from the [Spotify dashboard](https://developer.spotify.com/dashboard).
- **streamrip** works free on SoundCloud (no login). Lossless from Qobuz/Tidal
  requires a paid subscription configured in streamrip's config (`rip config path`).
