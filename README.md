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

**Delivery:** downloaded tracks upload to **Dropbox** (desktop app syncs them to a
Mac → USB for DJing) and/or **Cloudflare R2** (website/radio). `DELIVERY_DESTINATIONS`
selects `dropbox`, `r2`, or both. A local JSON **ledger** records what's been
delivered — identity, provenance, destination, timestamps — so storage can later
be reconciled and safely pruned. It also records **failures**: a track that fails
to download or upload lands as a durable retry unit (stage, error, destination,
and the local file if one survived), so the stragglers can be retried later
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
    app/storage/     # dropbox.py + r2.py (upload), ledger.py (delivery + failure record)
    scripts/         # smoke_*.py (prove pipelines), retry_failures.py, mint_dropbox_token.py
    tests/           # ledger + R2 unit tests (R2 uses moto; no live network)
    data/            # ledger.json — the delivery record (gitignored)
    Dockerfile       # python:3.12-slim + ffmpeg + uv
  client/            # Expo app (not built yet)
```

## Server — quickstart

Requires [uv](https://docs.astral.sh/uv/) and ffmpeg (`brew install ffmpeg`).
[Deno](https://deno.com/) (`brew install deno`) is optional locally but recommended: yt-dlp uses it as a JavaScript runtime for YouTube.

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

# Prove the Cloudflare R2 delivery leg (needs the R2 setup below):
uv run python scripts/smoke_r2.py
uv run python scripts/smoke_r2.py --cleanup   # delete the object afterward

# Deliver a whole playlist; failed tracks are logged to the ledger for retry:
uv run python scripts/smoke_playlist.py

# Retry the deliveries the ledger recorded as failed:
uv run python scripts/retry_failures.py --dry-run   # list what's pending
uv run python scripts/retry_failures.py             # re-attempt them

# Unit tests (ledger is stdlib-only; R2 uses moto, no live network):
uv run pytest
uv run python tests/test_ledger_failures.py   # ledger subset, no extra deps

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

### Cloudflare R2 delivery

Same bucket/creds as the personal site (`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, optional `R2_PUBLIC_URL`,
`R2_KEY_PREFIX` default `prod`). Endpoint is
`https://<account_id>.r2.cloudflarestorage.com` (region `auto`, signature v4).

Uncomment those vars in `server/.env` (see `.env.example`), then:

```bash
cd server
uv run python scripts/smoke_r2.py
# or: SMOKE_SPOTIFY_URL=https://open.spotify.com/track/... uv run python scripts/smoke_r2.py
uv run python scripts/smoke_r2.py --cleanup   # delete the smoke object afterward
```

Default object key: `{prefix}/music/cratewire/{crate}/{artists} - {title}.{ext}`
(same filename convention as Dropbox). Override with `R2_KEY_LAYOUT`.

To send real (non-smoke) downloads to R2 as well as — or instead of — Dropbox:

```
DELIVERY_DESTINATIONS=dropbox,r2   # or just r2
```

R2 buckets are **private** unless you attach a custom domain or enable an
`r2.dev` public development URL. Leave `R2_PUBLIC_URL` unset to keep objects
private. Publicly hosting full copyrighted tracks is a legal risk; this path is
for a personal crate, not a public download site.

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
- **Cloudflare R2** needs an API token with Object Read & Write on the bucket —
  see [Cloudflare R2 delivery](#cloudflare-r2-delivery). Same env var names as
  the personal-site bucket, so those creds can be reused.
