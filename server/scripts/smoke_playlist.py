"""Smoke-test the Dropbox delivery leg for a whole Spotify *playlist*.

This generalizes scripts/smoke_dropbox.py (single track, left intact as the
reference) to fan out over every track in a playlist. Per the session's decision:
land the batch in a throwaway smoke folder named after the playlist, and KEEP the
files there for the user to inspect later (no cleanup by default).

Proves, in order:
  1. spotdl downloads every track in the playlist into a local
     downloads/<playlist name>/ subfolder (one m4a per track).
  2. For each downloaded file we read metadata + identity (tags via mutagen,
     sha256, best-effort YouTube source id parsed from spotdl output).
  3. DropboxStorage uploads each file to /_smoke_test/<playlist name>/ via the API
     and verifies it landed.
  4. Each track is upserted into the smoke ledger (server/data/ledger.smoke.json),
     written incrementally so a crash mid-playlist keeps the tracks already done.
  5. ROUND-TRIP (batch): we wait for the Dropbox desktop app to sync the folder
     back down to ~/Dropbox/Apps/<AppName>/_smoke_test/<playlist name>/ and report
     how many of the uploaded files arrived on the Mac (USB-reachable).

Partial failure is expected at playlist scale: one track failing to match, being
unavailable, or throttled must not abort the batch. Each track is handled in its
own try/except; the run ends with a success/failure summary.

Run (needs the DROPBOX_* env vars set — see .env.example / mint script):
    uv run python scripts/smoke_playlist.py
    uv run python scripts/smoke_playlist.py --playlist "https://open.spotify.com/playlist/..."
    uv run python scripts/smoke_playlist.py --keep-local   # don't delete local temp downloads
    uv run python scripts/smoke_playlist.py --cleanup      # delete the folder from Dropbox at the end
    uv run python scripts/smoke_playlist.py --no-round-trip # skip the desktop-sync wait
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

# Make `app` importable when run as scripts/smoke_playlist.py from server/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage.dropbox import DropboxStorage  # noqa: E402
from app.storage.ledger import (  # noqa: E402
    Ledger,
    TrackEntry,
    TrackSource,
    compute_sha256,
    utc_now_iso,
)

# The playlist the session is targeting (share ?si= token is accepted as-is).
DEFAULT_PLAYLIST = "https://open.spotify.com/playlist/2wn2o6OxcBoVhVyYNPFYU6?si=47bdca74b47a45f8"
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

# A spotdl progress line for a finished track, e.g.
#   Downloaded "Basement Jaxx - Where's Your Head At": https://music.youtube.com/watch?v=abc...
YT_URL_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/|music\.youtube\.com/watch\?v=)([\w-]{11})")
DOWNLOADED_LINE_RE = re.compile(r'Downloaded\s+"(?P<name>.+?)":\s*(?P<url>\S+)')


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


def _norm(s: str) -> str:
    """Normalize a title/name for loose matching: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def spotdl_download_playlist(playlist: str, timeout: int) -> tuple[list[Path], str, str]:
    """Download every track in the playlist; return (new_files, output, playlist_name).

    Files land in DOWNLOAD_DIR/<playlist name>/ via spotdl's {list-name} template.
    We diff the tree before/after so we only act on files this run created.
    """
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    before = {p for p in DOWNLOAD_DIR.rglob("*") if p.suffix.lower() in AUDIO_EXTS}

    args = [
        sys.executable, "-m", "spotdl", "download", playlist,
        "--format", AUDIO_FORMAT,
        "--audio", *AUDIO_PROVIDERS,
        "--output", str(DOWNLOAD_DIR / "{list-name}" / "{artists} - {title}.{output-ext}"),
    ]
    print("$ " + " ".join(args))
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.strip())
    # spotdl exits non-zero if *some* tracks fail; don't abort — act on what landed.
    if proc.returncode != 0:
        print(f"(note: spotdl exited {proc.returncode} — continuing with whatever downloaded)")

    after = {p for p in DOWNLOAD_DIR.rglob("*") if p.suffix.lower() in AUDIO_EXTS}
    new = sorted(after - before, key=lambda p: p.stat().st_mtime)
    if not new:
        raise SystemExit("FAILED: spotdl produced no new audio files.")

    # The playlist name is the subfolder spotdl created ({list-name}). Expect one.
    parents = {p.parent.name for p in new}
    if len(parents) != 1:
        print(f"(note: files landed under multiple folders {parents}; using the first)")
    playlist_name = new[0].parent.name
    return new, out, playlist_name


def parse_yt_ids(output: str) -> dict[str, str]:
    """Map normalized 'Downloaded \"<name>\"' -> YouTube id, best-effort, for provenance."""
    ids: dict[str, str] = {}
    for m in DOWNLOADED_LINE_RE.finditer(output):
        url_m = YT_URL_RE.search(m.group("url"))
        if url_m:
            ids[_norm(m.group("name"))] = url_m.group(1)
    return ids


def match_yt_id(stem: str, yt_ids: dict[str, str]) -> str | None:
    """Loosely match a filename stem to a parsed 'Downloaded ...' entry."""
    key = _norm(stem)
    if key in yt_ids:
        return yt_ids[key]
    # Fall back to containment either way (spotdl's display name vs our template differ).
    for name, vid in yt_ids.items():
        if name and (name in key or key in name):
            return vid
    return None


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
        print(f"(note: tag read skipped for {path.name}: {exc})")
    return info


def deliver_track(
    local_path: Path,
    *,
    playlist_url: str,
    playlist_name: str,
    yt_ids: dict[str, str],
    storage: DropboxStorage,
    ledger: Ledger,
    keep_local: bool,
) -> str:
    """Upload one downloaded track + upsert its ledger row. Returns its relative_path."""
    tags = read_tags(local_path)
    sha256 = compute_sha256(local_path)
    size_bytes = local_path.stat().st_size

    source = TrackSource(
        engine="spotdl",
        input_url=playlist_url,
        input_type="spotify_playlist",
        resolved_provider="youtube",
        resolved_source_id=match_yt_id(local_path.stem, yt_ids),
    )

    result = storage.upload(
        local_path,
        base_path=f"{SMOKE_BASE_PATH}/{playlist_name}",
        delete_local=not keep_local,
    )

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
    ledger.save()  # incremental: a crash mid-playlist keeps what's already done
    return result.relative_path


def count_synced(playlist_name: str, explicit_dir: str | None, expected: int, timeout: int) -> int:
    """Poll once (batch) for the playlist folder to sync down; return files present."""
    if explicit_dir:
        pattern = os.path.join(os.path.expanduser(explicit_dir), "_smoke_test", playlist_name, "*")
    else:
        pattern = os.path.expanduser(f"~/Dropbox/Apps/*/_smoke_test/{playlist_name}/*")

    deadline = time.time() + timeout
    best = 0
    while time.time() < deadline:
        hits = [h for h in glob.glob(pattern) if Path(h).suffix.lower() in AUDIO_EXTS]
        best = max(best, len(hits))
        if best >= expected:
            return best
        time.sleep(2.0)
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playlist", default=DEFAULT_PLAYLIST, help="Spotify playlist URL")
    parser.add_argument("--keep-local", action="store_true", help="Don't delete local temp downloads")
    parser.add_argument("--cleanup", action="store_true",
                        help="Delete the playlist folder from Dropbox at the end (default: keep)")
    parser.add_argument("--no-round-trip", action="store_true", help="Skip the desktop-sync wait")
    parser.add_argument("--local-sync-dir", help="Explicit ~/Dropbox/Apps/<AppName> dir for the round-trip check")
    parser.add_argument("--sync-timeout", type=int, default=300, help="Seconds to wait for desktop sync")
    parser.add_argument("--download-timeout", type=int, default=2400, help="Seconds to allow spotdl to run")
    args = parser.parse_args()

    load_env_file(ENV_PATH)

    # 1. download the whole playlist
    print("\n=== 1. spotdl download (playlist) ===")
    new_files, output, playlist_name = spotdl_download_playlist(args.playlist, args.download_timeout)
    yt_ids = parse_yt_ids(output)
    print(f"\ndownloaded {len(new_files)} file(s) into downloads/{playlist_name}/")

    # 2-4. deliver each track (upload + ledger), tolerating per-track failure
    print(f"\n=== 2. deliver {len(new_files)} track(s) to /_smoke_test/{playlist_name}/ ===")
    storage = DropboxStorage.from_env()
    ledger = Ledger.load(SMOKE_LEDGER)
    delivered: list[str] = []
    failures: list[tuple[str, str]] = []
    for i, local_path in enumerate(new_files, 1):
        try:
            rel = deliver_track(
                local_path,
                playlist_url=args.playlist,
                playlist_name=playlist_name,
                yt_ids=yt_ids,
                storage=storage,
                ledger=ledger,
                keep_local=args.keep_local,
            )
            delivered.append(rel)
            print(f"  [{i}/{len(new_files)}] ok: {rel}")
        except Exception as exc:  # noqa: BLE001 - one bad track must not abort the batch
            failures.append((local_path.name, str(exc)))
            print(f"  [{i}/{len(new_files)}] FAIL: {local_path.name} — {exc}")

    print(f"\nledger: {SMOKE_LEDGER}  ({len(ledger.tracks)} track(s) total)")

    # 5. round-trip (batch): how many synced back down to the Mac
    synced = None
    if args.no_round_trip:
        print("\n=== 3. round-trip skipped (--no-round-trip) ===")
    else:
        print(f"\n=== 3. round-trip (waiting up to {args.sync_timeout}s for desktop sync) ===")
        synced = count_synced(playlist_name, args.local_sync_dir, len(delivered), args.sync_timeout)
        loc = args.local_sync_dir or "~/Dropbox/Apps/<AppName>"
        print(f"synced to Mac: {synced}/{len(delivered)} in {loc}/_smoke_test/{playlist_name}/")

    # 6. optional cleanup (default: keep the files for the user to inspect)
    if args.cleanup and delivered:
        print("\n=== 4. cleanup ===")
        storage.delete(f"{SMOKE_BASE_PATH}/{playlist_name}")
        print(f"deleted from Dropbox: {SMOKE_BASE_PATH}/{playlist_name}")
    else:
        print(f"\n=== kept in Dropbox: {SMOKE_BASE_PATH}/{playlist_name}/ (inspect later) ===")

    # summary
    print("\n" + "=" * 60)
    print(f"SUMMARY  playlist={playlist_name!r}")
    print(f"  downloaded: {len(new_files)}")
    print(f"  delivered:  {len(delivered)}")
    print(f"  failed:     {len(failures)}")
    if synced is not None:
        print(f"  synced back to Mac: {synced}/{len(delivered)}")
    for name, err in failures:
        print(f"    - {name}: {err}")
    print("=" * 60)

    if failures and not delivered:
        raise SystemExit("FAILED: no tracks delivered.")


if __name__ == "__main__":
    main()
