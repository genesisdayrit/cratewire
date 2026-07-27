# Cratewire

Personal **multi-source music downloader** for building a DJ crate — pull tracks
from SoundCloud / YouTube today, lossless sources (Qobuz/Tidal) later, and land
them somewhere you can sync to a USB and DJ off. Formerly *SpotWire*; v1 was a
macOS Electron/DMG desktop app, archived as the `spotwire-v1` repo.

## Architecture (current slice)

A single **FastAPI** server (Python) that runs download engines in-process and
writes tagged audio to a local folder. Two engines so far:

- **[spotdl](https://github.com/spotDL/spotify-downloader)** → YouTube (great for discovery; lossy)
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** → SoundCloud (free). Preferred over
  streamrip for SoundCloud: it pulls the single *progressive* MP3
  (`http_mp3_1_0`), so there are no HLS segments to reassemble. streamrip 2.1.0
  reorders those segments and produces silently-scrambled audio.
- **[streamrip](https://github.com/nathom/streamrip)** → Qobuz/Tidal/Deezer for
  lossless with credentials (deferred). Not used for SoundCloud — see above.

Both shell out to **ffmpeg** for transcode/tagging.

**Delivery:** downloaded tracks are uploaded to **Dropbox** (its desktop app then
syncs them down to a Mac → USB for DJing). A local JSON **ledger** records what's
been delivered — identity, provenance, timestamps — so Dropbox storage can later
be reconciled against a USB and safely pruned. It also records **failures**: a
track that fails to download or upload lands as a durable retry unit (stage,
error, and the local file if one survived), so the stragglers can be retried later
instead of being lost to a log line. A DB replaces the JSON later.

Client will be an **Expo / React Native** app (iOS first, Android later).

Deliberately deferred until needed: multi-user + auth (better-auth in a Node
service, added in front later), a job queue / scheduler for automated downloads,
running the downloader on a 24/7 cloud host (and yt-dlp proxying for datacenter
IPs), lossless engines, and the USB-reconcile client.

```
cratewire/
  server/            # FastAPI + download engines (this repo, for now)
    app/main.py      # FastAPI app: /health, /version
    app/storage/     # dropbox.py (upload), ledger.py (delivery + failure record)
    scripts/         # smoke_*.py (prove pipelines), retry_failures.py, mint_dropbox_token.py
    tests/           # test_ledger_failures.py (stdlib-only, no network)
    data/            # ledger.json — the delivery record (gitignored)
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

# Prove the SoundCloud (free) pipeline — yt-dlp, progressive stream + duration check:
uv run python scripts/smoke_soundcloud.py
uv run python scripts/smoke_soundcloud.py --url https://soundcloud.com/.../track

# streamrip stays for future lossless (Qobuz/Tidal); its SoundCloud rip is
# unreliable (reorders HLS segments) — kept only as an engine POC:
uv run python scripts/smoke_streamrip.py

# Prove the full Dropbox delivery leg (needs the Dropbox setup below):
uv run python scripts/smoke_dropbox.py

# Deliver a whole playlist; failed tracks are logged to the ledger for retry:
uv run python scripts/smoke_playlist.py

# Retry the deliveries the ledger recorded as failed:
uv run python scripts/retry_failures.py --dry-run   # list what's pending
uv run python scripts/retry_failures.py             # re-attempt them

# Unit-test the ledger's failure API (stdlib only, no network):
uv run python tests/test_ledger_failures.py

# Run the API:
uv run uvicorn app.main:app --reload
# -> http://127.0.0.1:8000/health   http://127.0.0.1:8000/version
```

### Dropbox delivery setup (one-time)

`smoke_dropbox.py` downloads a track, uploads it to `/_smoke_test/` in a
dedicated Dropbox app folder, waits for it to sync back down to this Mac, then
cleans up. To run it you need a Dropbox app + refresh token:

1. Create a **dedicated** app at <https://www.dropbox.com/developers/apps> →
   **Scoped access** → **App folder** (sandboxed to `/Apps/<AppName>/`). On the
   **Permissions** tab enable `files.content.write` + `files.content.read`.
2. `cp .env.example .env`, then paste the app's **App key** / **App secret** into
   `DROPBOX_ACCESS_KEY` / `DROPBOX_ACCESS_SECRET`.
3. Mint a refresh token (opens a browser authorize flow):
   ```bash
   uv run python scripts/mint_dropbox_token.py
   ```
   Paste the printed `DROPBOX_REFRESH_TOKEN` into `.env`.

Real downloads land under `DROPBOX_BASE_PATH` (default `/music`); the smoke test
uses a separate `/_smoke_test/` path and deletes what it uploads.

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
- **Dropbox** needs a dedicated App-folder app + refresh token — see the
  [Dropbox delivery setup](#dropbox-delivery-setup-one-time) above. Credentials
  live in `server/.env` (gitignored); the SDK auto-refreshes the access token.
