"""Smoke-test the full Dropbox delivery leg, end to end.

Proves, in order:
  1. spotdl downloads a real track to a local temp file.
  2. We read its metadata + identity (tags via mutagen, sha256, best-effort
     YouTube source id from spotdl output).
  3. DropboxStorage uploads it to /_smoke_test/ via the API and verifies it.
  4. The entry is written to the smoke ledger (server/data/ledger.smoke.json),
     exercising the real upsert + atomic-write path.
  5. ROUND-TRIP: we wait for the Dropbox desktop app to sync the file back down
     to ~/Dropbox/Apps/<AppName>/_smoke_test/ — the actual bridge that makes a
     track reachable for a USB. This is the real success line.
  6. Cleanup: delete the throwaway file from Dropbox (the daemon then removes the
     local copy too).

Run (needs the DROPBOX_* env vars set — see .env.example / mint script):
    uv run python scripts/smoke_dropbox.py
    uv run python scripts/smoke_dropbox.py --track "https://open.spotify.com/track/..."
    uv run python scripts/smoke_dropbox.py --keep-local   # don't delete the temp download
    uv run python scripts/smoke_dropbox.py --keep-remote  # leave the file in Dropbox
    uv run python scripts/smoke_dropbox.py --local-sync-dir ~/Dropbox/Apps/Cratewire
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Make `app` importable when run as scripts/smoke_dropbox.py from server/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage.dropbox import DropboxStorage  # noqa: E402
from app.storage.ledger import (  # noqa: E402
    Ledger,
    TrackEntry,
    TrackSource,
    compute_sha256,
    utc_now_iso,
)

DEFAULT_TRACK = "https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8"  # Never Gonna Give You Up
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"
SMOKE_LEDGER = Path(__file__).resolve().parent.parent / "data" / "ledger.smoke.json"
SMOKE_BASE_PATH = "/_smoke_test"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

AUDIO_EXTS = (".mp3", ".m4a", ".opus", ".flac", ".wav", ".aac", ".ogg")

# Best *free* quality without a pointless re-encode: prefer YouTube Music (serves
# a higher-bitrate stream than plain YouTube), and output m4a/AAC — which is
# Rekordbox-compatible (unlike opus) so it needs no further transcode for DJing.
# NOTE: YouTube is lossy (~128-160 kbps); no setting exceeds the source. True
# lossless is a different engine (streamrip + Qobuz/Tidal), deferred.
AUDIO_FORMAT = "m4a"
AUDIO_PROVIDERS = ["youtube-music", "youtube"]
YT_ID_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/|music\.youtube\.com/watch\?v=)([\w-]{11})")


def load_env_file(path: Path) -> None:
    """Best-effort: populate os.environ from a .env file (no dependency needed)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def spotdl_download(track: str) -> tuple[Path, str]:
    """Download one track with spotdl; return (file_path, combined_output)."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    before = {p for p in DOWNLOAD_DIR.glob("*") if p.suffix.lower() in AUDIO_EXTS}

    args = [
        sys.executable, "-m", "spotdl", "download", track,
        "--format", AUDIO_FORMAT,
        "--audio", *AUDIO_PROVIDERS,
        "--output", str(DOWNLOAD_DIR / "{artists} - {title}.{output-ext}"),
    ]
    print("$ " + " ".join(args))
    proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.strip())
    if proc.returncode != 0:
        raise SystemExit(f"FAILED: spotdl exited {proc.returncode}")

    after = {p for p in DOWNLOAD_DIR.glob("*") if p.suffix.lower() in AUDIO_EXTS}
    new = sorted(after - before, key=lambda p: p.stat().st_mtime)
    if not new:
        raise SystemExit("FAILED: spotdl reported success but produced no audio file.")
    return new[-1], out


def read_tags(path: Path) -> dict[str, object]:
    """Best-effort tag/info read via mutagen (a spotdl dep). Missing -> None."""
    info: dict[str, object] = {"artist": None, "title": None, "album": None,
                               "duration_sec": None, "bitrate_kbps": None}
    try:
        import mutagen  # type: ignore

        easy = mutagen.File(path, easy=True)
        if easy is not None:
            info["artist"] = (easy.get("artist") or [None])[0]
            info["title"] = (easy.get("title") or [None])[0]
            info["album"] = (easy.get("album") or [None])[0]
        raw = mutagen.File(path)
        if raw is not None and getattr(raw, "info", None) is not None:
            if getattr(raw.info, "length", None):
                info["duration_sec"] = int(raw.info.length)
            if getattr(raw.info, "bitrate", None):
                info["bitrate_kbps"] = int(raw.info.bitrate / 1000)
    except Exception as exc:  # noqa: BLE001 - metadata is best-effort, never fatal
        print(f"(note: tag read skipped: {exc})")
    return info


def find_synced_copy(filename: str, explicit_dir: str | None, timeout: int) -> Path | None:
    """Poll for the file syncing down to the local Dropbox app folder."""
    if explicit_dir:
        patterns = [os.path.join(os.path.expanduser(explicit_dir), "_smoke_test", filename)]
    else:
        patterns = [os.path.expanduser(f"~/Dropbox/Apps/*/_smoke_test/{filename}")]

    deadline = time.time() + timeout
    while time.time() < deadline:
        for pat in patterns:
            hits = glob.glob(pat)
            if hits:
                return Path(hits[0])
        time.sleep(1.0)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--track", default=DEFAULT_TRACK, help="Spotify URL or search query")
    parser.add_argument("--keep-local", action="store_true", help="Don't delete the local temp download")
    parser.add_argument("--keep-remote", action="store_true", help="Leave the file in Dropbox (skip cleanup)")
    parser.add_argument("--local-sync-dir", help="Explicit ~/Dropbox/Apps/<AppName> dir for the round-trip check")
    parser.add_argument("--sync-timeout", type=int, default=120, help="Seconds to wait for desktop sync")
    args = parser.parse_args()

    load_env_file(ENV_PATH)

    # 1. download
    print("\n=== 1. spotdl download ===")
    local_path, stdout = spotdl_download(args.track)
    print(f"downloaded: {local_path.name}  ({local_path.stat().st_size // 1024} KB)")

    # 2. identity + metadata (compute BEFORE upload, which may delete the local file)
    print("\n=== 2. metadata + identity ===")
    tags = read_tags(local_path)
    sha256 = compute_sha256(local_path)
    size_bytes = local_path.stat().st_size
    yt = YT_ID_RE.search(stdout)
    is_spotify = "open.spotify.com/track" in args.track
    source = TrackSource(
        engine="spotdl",
        input_url=args.track,
        input_type="spotify_track" if is_spotify else "query",
        resolved_provider="youtube",
        resolved_source_id=yt.group(1) if yt else None,
    )
    print(f"artist={tags['artist']!r} title={tags['title']!r} sha256={sha256[:12]}… "
          f"yt={source.resolved_source_id}")

    # 3. upload to /_smoke_test and verify
    print("\n=== 3. upload to Dropbox (/_smoke_test) ===")
    storage = DropboxStorage.from_env()
    result = storage.upload(
        local_path,
        base_path=SMOKE_BASE_PATH,
        delete_local=not args.keep_local,
    )
    print(f"uploaded + verified: {result.dropbox_path}  ({result.size_bytes} bytes, rev {result.rev})")

    # 4. write the smoke ledger
    print("\n=== 4. ledger upsert ===")
    ledger = Ledger.load(SMOKE_LEDGER)
    entry = TrackEntry(
        relative_path=result.relative_path,
        filename=result.filename,
        artist=tags["artist"],  # type: ignore[arg-type]
        title=tags["title"],  # type: ignore[arg-type]
        album=tags["album"],  # type: ignore[arg-type]
        ext=local_path.suffix.lstrip("."),
        size_bytes=size_bytes,
        duration_sec=tags["duration_sec"],  # type: ignore[arg-type]
        bitrate_kbps=tags["bitrate_kbps"],  # type: ignore[arg-type]
        content_sha256=sha256,
        source=source,
    )
    entry.state.last_verified_at = utc_now_iso()
    ledger.upsert(entry)
    ledger.save()
    print(f"ledger: {SMOKE_LEDGER}  (id={entry.id}, {len(ledger.tracks)} track(s))")

    # 5. round-trip: wait for the desktop app to sync it back down to the Mac
    print(f"\n=== 5. round-trip (waiting up to {args.sync_timeout}s for desktop sync) ===")
    synced = find_synced_copy(result.filename, args.local_sync_dir, args.sync_timeout)
    if synced is None:
        raise SystemExit(
            "UPLOAD OK, but the file did not sync down to ~/Dropbox/Apps/<AppName>/_smoke_test/ "
            f"within {args.sync_timeout}s. Check the Dropbox desktop app is running and the app "
            "folder is syncing. (Pass --local-sync-dir to point at the exact folder.)"
        )
    print(f"synced to Mac: {synced}  ({synced.stat().st_size // 1024} KB) — USB-reachable ✅")

    # 6. cleanup
    if args.keep_remote:
        print("\n=== 6. cleanup skipped (--keep-remote) ===")
    else:
        print("\n=== 6. cleanup ===")
        storage.delete(result.dropbox_path)
        print(f"deleted from Dropbox: {result.dropbox_path}")

    print("\nOK — full Dropbox delivery leg verified end to end.")


if __name__ == "__main__":
    main()
