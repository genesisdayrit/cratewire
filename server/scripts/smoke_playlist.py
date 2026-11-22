"""Smoke-test the delivery leg for a whole Spotify *playlist*.

Honors `DELIVERY_DESTINATIONS` (dropbox, r2, or both). Dropbox is still the
default. Dual-dest uploads keep the local file until every dest succeeds.

This generalizes scripts/smoke_dropbox.py (single track, left intact as the
reference) to fan out over every track in a playlist. Per the session's decision:
land the batch in a throwaway smoke folder named after the playlist, and KEEP the
files there for the user to inspect later (no cleanup by default).

Proves, in order:
  1. spotdl downloads every track in the playlist into a local
     downloads/<playlist name>/ subfolder (one m4a per track).
  2. For each downloaded file we read metadata + identity (tags via mutagen,
     sha256, best-effort YouTube source id parsed from spotdl output).
  3. Each configured destination (Dropbox and/or R2) gets the file; Dropbox
     lands under /_smoke_test/<playlist name>/, R2 under
     {prefix}/music/cratewire/<playlist>/.
  4. Each track is upserted into the smoke ledger (server/data/ledger.smoke.json),
     written incrementally so a crash mid-playlist keeps the tracks already done.
  5. ROUND-TRIP (batch): we wait for the Dropbox desktop app to sync the folder
     back down to ~/Dropbox/Apps/<AppName>/_smoke_test/<playlist name>/ and report
     how many of the uploaded files arrived on the Mac (USB-reachable).

Partial failure is expected at playlist scale: one track failing to match, being
unavailable, or throttled must not abort the batch. Each track is handled in its
own try/except; the run ends with a success/failure summary.

Failures are RECORDED to the ledger (not just printed): download-stage no-matches
and per-track delivery failures land as `FailureEntry` rows, tagged with the stage
and — for delivery failures — the local file left on disk. That makes them a
durable work-list: `scripts/retry_failures.py` picks up the pending ones later.

Run (needs creds for whatever DELIVERY_DESTINATIONS lists — default dropbox):
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

from app.storage.destinations import (  # noqa: E402
    failure_key,
    load_storages,
    parse_destinations,
    upload_to,
)
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

# spotdl prints, for a track it couldn't fetch, a canonical "no match" line like:
#   LookupError: No results found for song: Artist - Title
# Best-effort: these are download-stage failures — tracks that never produced a
# file — and they're the most valuable thing to log for a later retry.
NO_RESULTS_RE = re.compile(r"No results found for song:\s*(?P<name>.+)")


class DeliveryError(Exception):
    """A per-track delivery failure tagged with the stage it happened at.

    The stage (metadata | upload | ledger) tells a later retry how much work is
    salvageable — an upload-stage failure leaves the downloaded file on disk, so
    retrying is just a re-upload. `destination` is set when the failure is
    specific to one backend (Dropbox vs R2).
    """

    def __init__(self, stage: str, cause: Exception, destination: str | None = None) -> None:
        super().__init__(f"{stage}: {cause}")
        self.stage = stage
        self.cause = cause
        self.destination = destination


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


def parse_download_failures(output: str) -> list[str]:
    """Best-effort: track names spotdl reported it couldn't match, de-duplicated.

    Returns [] if the output has no recognizable "no match" lines — we'd rather log
    nothing than log noise, so unrecognized failure phrasings are simply skipped.
    """
    seen: set[str] = set()
    names: list[str] = []
    for m in NO_RESULTS_RE.finditer(output):
        name = m.group("name").strip()
        norm = _norm(name)
        if norm and norm not in seen:
            seen.add(norm)
            names.append(name)
    return names


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


def _intended_path(dest_name: str, playlist_name: str, filename: str, storages: dict) -> str:
    """Best-effort remote path/key a retry would target for this dest."""
    if dest_name == "r2" and "r2" in storages:
        return storages["r2"].build_key(filename, crate=playlist_name)
    return f"{SMOKE_BASE_PATH.lstrip('/')}/{playlist_name}/{filename}"


def deliver_track(
    local_path: Path,
    *,
    playlist_url: str,
    playlist_name: str,
    yt_ids: dict[str, str],
    destinations: tuple[str, ...],
    storages: dict,
    ledger: Ledger,
    keep_local: bool,
) -> tuple[list[str], list[tuple[str, Exception]]]:
    """Upload one downloaded track to each configured dest + upsert ledger rows.

    Returns (dropbox relative_paths that landed, per-dest upload errors). Each
    dest is independent: a Dropbox success is recorded even if R2 fails, and
    vice versa. Local delete happens only after every dest succeeded.
    """
    try:
        tags = read_tags(local_path)  # best-effort internally; grouped here anyway
        sha256 = compute_sha256(local_path)
        size_bytes = local_path.stat().st_size
    except Exception as exc:  # noqa: BLE001 - tagged + re-raised for the caller to log
        raise DeliveryError("metadata", exc) from exc

    source = TrackSource(
        engine="spotdl",
        input_url=playlist_url,
        input_type="spotify_playlist",
        resolved_provider="youtube",
        resolved_source_id=match_yt_id(local_path.stem, yt_ids),
    )

    dropbox_paths: list[str] = []
    dest_errors: list[tuple[str, Exception]] = []
    for dest_name in destinations:
        try:
            result = upload_to(
                dest_name,
                storages[dest_name],
                local_path,
                dropbox_base_path=f"{SMOKE_BASE_PATH}/{playlist_name}",
                r2_crate=playlist_name,
                delete_local=False,
            )
        except Exception as exc:  # noqa: BLE001 - a failed upload leaves the local file for retry
            dest_errors.append((dest_name, exc))
            continue

        try:
            entry = TrackEntry(
                relative_path=result.relative_path,
                filename=result.filename,
                destination=result.destination,
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
            ledger.resolve_failure(failure_key(f"{playlist_name}/{local_path.name}", dest_name))
            ledger.save()  # incremental: a crash mid-playlist keeps what's already done
        except Exception as exc:  # noqa: BLE001 - bytes landed but the record didn't persist
            raise DeliveryError("ledger", exc) from exc
        if result.destination == "dropbox":
            dropbox_paths.append(result.relative_path)

    if not keep_local and not dest_errors:
        local_path.unlink(missing_ok=True)
    return dropbox_paths, dest_errors


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

    destinations = parse_destinations()
    print(f"destinations: {', '.join(destinations)}")
    storages = load_storages(destinations)
    ledger = Ledger.load(SMOKE_LEDGER)

    # 2. log download-stage failures (tracks spotdl never produced a file for) so
    #    they're retriable later — before, these vanished into spotdl's output.
    download_failures = parse_download_failures(output)
    for name in download_failures:
        ledger.record_failure(
            f"download:{_norm(name)}",
            "download",
            LookupError(f"spotdl found no match for {name!r}"),
            engine="spotdl",
            input_url=args.playlist,
            input_type="spotify_playlist",
            track_ref=name,
        )
    if download_failures:
        ledger.save()
        print(f"logged {len(download_failures)} download failure(s) to the ledger for retry")

    # 3. deliver each downloaded track (upload + ledger), tolerating per-track failure.
    #    Failures are recorded to the ledger (not just printed) so they can be retried.
    dest_note = ", ".join(destinations)
    print(f"\n=== 3. deliver {len(new_files)} track(s) to [{dest_note}] ({playlist_name}) ===")
    delivered: list[str] = []
    failures: list[tuple[str, str]] = []
    for i, local_path in enumerate(new_files, 1):
        slot = f"{playlist_name}/{local_path.name}"
        try:
            rels, dest_errors = deliver_track(
                local_path,
                playlist_url=args.playlist,
                playlist_name=playlist_name,
                yt_ids=yt_ids,
                destinations=destinations,
                storages=storages,
                ledger=ledger,
                keep_local=args.keep_local,
            )
        except DeliveryError as exc:  # noqa: PERF203 - metadata/ledger must not abort the batch
            dest = exc.destination or destinations[0]
            ledger.record_failure(
                failure_key(slot, dest),
                exc.stage,
                exc.cause,
                engine="spotdl",
                input_url=args.playlist,
                input_type="spotify_playlist",
                track_ref=local_path.stem,
                relative_path=_intended_path(dest, playlist_name, local_path.name, storages),
                filename=local_path.name,
                destination=dest,
                local_path=str(local_path) if local_path.exists() else None,
            )
            ledger.save()
            failures.append((local_path.name, str(exc)))
            print(f"  [{i}/{len(new_files)}] FAIL ({exc.stage}/{dest}): {local_path.name} — {exc.cause}")
            continue

        delivered.extend(rels)
        for dest, cause in dest_errors:
            ledger.record_failure(
                failure_key(slot, dest),
                "upload",
                cause,
                engine="spotdl",
                input_url=args.playlist,
                input_type="spotify_playlist",
                track_ref=local_path.stem,
                relative_path=_intended_path(dest, playlist_name, local_path.name, storages),
                filename=local_path.name,
                destination=dest,
                local_path=str(local_path) if local_path.exists() else None,
            )
            ledger.save()
            failures.append((local_path.name, f"upload/{dest}: {cause}"))
            print(f"  [{i}/{len(new_files)}] FAIL (upload/{dest}): {local_path.name} — {cause}")
        if not dest_errors:
            print(f"  [{i}/{len(new_files)}] ok: {local_path.name} -> {dest_note}")

    print(f"\nledger: {SMOKE_LEDGER}  ({len(ledger.tracks)} track(s), "
          f"{len(ledger.pending_failures())} pending failure(s))")

    # 4. round-trip (batch): how many synced back down to the Mac (Dropbox only)
    synced = None
    if "dropbox" not in destinations or args.no_round_trip:
        print("\n=== 4. round-trip skipped ===")
    else:
        print(f"\n=== 4. round-trip (waiting up to {args.sync_timeout}s for desktop sync) ===")
        synced = count_synced(playlist_name, args.local_sync_dir, len(delivered), args.sync_timeout)
        loc = args.local_sync_dir or "~/Dropbox/Apps/<AppName>"
        print(f"synced to Mac: {synced}/{len(delivered)} in {loc}/_smoke_test/{playlist_name}/")

    # 5. optional cleanup (default: keep the files for the user to inspect)
    if args.cleanup:
        print("\n=== 5. cleanup ===")
        if "dropbox" in storages:
            storages["dropbox"].delete(f"{SMOKE_BASE_PATH}/{playlist_name}")
            print(f"deleted from Dropbox: {SMOKE_BASE_PATH}/{playlist_name}")
        if "r2" in storages:
            print("R2 cleanup: use scripts/smoke_r2.py --cleanup for a single object; "
                  "playlist objects are left in place so they can be inspected.")
    else:
        if "dropbox" in destinations:
            print(f"\n=== kept in Dropbox: {SMOKE_BASE_PATH}/{playlist_name}/ (inspect later) ===")
        if "r2" in destinations:
            print(f"=== kept in R2 under crate {playlist_name!r} (inspect later) ===")

    # summary
    pending = ledger.pending_failures()
    print("\n" + "=" * 60)
    print(f"SUMMARY  playlist={playlist_name!r}")
    print(f"  downloaded:        {len(new_files)}")
    print(f"  delivered:         {len(delivered)}")
    print(f"  download failures: {len(download_failures)}")
    print(f"  deliver failures:  {len(failures)}")
    if synced is not None:
        print(f"  synced back to Mac: {synced}/{len(delivered)}")
    for name, err in failures:
        print(f"    - {name}: {err}")
    if pending:
        print(f"  ledger pending failures: {len(pending)} — retry later with:")
        print("    uv run python scripts/retry_failures.py")
    print("=" * 60)

    if failures and not delivered:
        raise SystemExit("FAILED: no tracks delivered.")


if __name__ == "__main__":
    main()
