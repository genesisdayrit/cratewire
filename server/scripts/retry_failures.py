"""Retry the deliveries the ledger recorded as failed.

The delivery pipeline (see smoke_playlist.py) now writes a `FailureEntry` to the
ledger whenever a track fails to download or upload, instead of just printing it.
This script is the other half: it reads the ledger's *pending* failures and
re-attempts each one, marking it resolved when it finally lands.

Two retry paths, chosen per failure by what's salvageable:
  - LOCAL PRESENT  — an upload-stage failure left the downloaded file on disk;
    retrying is just a re-upload (no re-download, no spotdl, no network fetch).
  - RE-DOWNLOAD    — a download-stage failure (or a local file since cleaned up)
    has a `track_ref`; we re-run spotdl for that query, then upload.

Each retry is independent: one still-failing track bumps its `attempts` and moves
on. Nothing aborts the batch.

Run (needs the DROPBOX_* env vars set — see .env.example / mint script):
    uv run python scripts/retry_failures.py
    uv run python scripts/retry_failures.py --dry-run       # just list what's pending
    uv run python scripts/retry_failures.py --ledger data/ledger.json
    uv run python scripts/retry_failures.py --keep-local    # don't delete local temp files
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

# Make `app` importable when run as scripts/retry_failures.py from server/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage.dropbox import DropboxStorage  # noqa: E402
from app.storage.ledger import (  # noqa: E402
    FailureEntry,
    Ledger,
    TrackEntry,
    TrackSource,
    compute_sha256,
    utc_now_iso,
)

DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"
RETRY_SUBDIR = DOWNLOAD_DIR / "_retries"
SMOKE_LEDGER = Path(__file__).resolve().parent.parent / "data" / "ledger.smoke.json"
SMOKE_BASE_PATH = "/_smoke_test"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

AUDIO_EXTS = (".mp3", ".m4a", ".opus", ".flac", ".wav", ".aac", ".ogg")

# Match the download engine's format/provider choices so a retry is byte-comparable
# to the original attempt (see smoke_playlist.py for the rationale).
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


def spotdl_download_query(query: str, timeout: int) -> tuple[Path, str]:
    """Re-download one track by query (a `track_ref`); return (file_path, output)."""
    RETRY_SUBDIR.mkdir(parents=True, exist_ok=True)
    before = {p for p in RETRY_SUBDIR.glob("*") if p.suffix.lower() in AUDIO_EXTS}

    args = [
        sys.executable, "-m", "spotdl", "download", query,
        "--format", AUDIO_FORMAT,
        "--audio", *AUDIO_PROVIDERS,
        "--output", str(RETRY_SUBDIR / "{artists} - {title}.{output-ext}"),
    ]
    print("    $ " + " ".join(args))
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "") + (proc.stderr or "")
    after = {p for p in RETRY_SUBDIR.glob("*") if p.suffix.lower() in AUDIO_EXTS}
    new = sorted(after - before, key=lambda p: p.stat().st_mtime)
    if not new:
        raise RuntimeError(f"spotdl produced no file for query {query!r} (exit {proc.returncode})")
    return new[-1], out


def _base_path_for(failure: FailureEntry) -> str:
    """Dropbox folder to re-upload into: the failure's original folder if known."""
    if failure.relative_path:
        parent = str(PurePosixPath(failure.relative_path).parent)
        if parent and parent != ".":
            return "/" + parent.lstrip("/")
    # Download failures never had a destination folder; keep retries together.
    return f"{SMOKE_BASE_PATH}/_retries"


def retry_one(
    failure: FailureEntry,
    *,
    storage: DropboxStorage,
    ledger: Ledger,
    download_timeout: int,
    keep_local: bool,
) -> tuple[bool, str]:
    """Re-attempt a single failed delivery. Returns (succeeded, human note)."""
    # 1. get a local file: the one left behind, or a fresh re-download.
    local = Path(failure.local_path) if failure.local_path else None
    if local is not None and local.exists():
        how = "re-upload (local file present)"
        source_id = None
    else:
        if not failure.track_ref:
            return False, "not retriable: no local file and no track_ref to re-download"
        local, out = spotdl_download_query(failure.track_ref, download_timeout)
        yt = YT_ID_RE.search(out)
        source_id = yt.group(1) if yt else None
        how = f"re-download + upload ({failure.track_ref!r})"

    # 2. identity + metadata BEFORE upload (upload may delete the local file).
    tags = read_tags(local)
    sha256 = compute_sha256(local)
    size_bytes = local.stat().st_size

    # 3. upload to the failure's folder (or the retries folder for downloads).
    result = storage.upload(
        local,
        base_path=_base_path_for(failure),
        delete_local=not keep_local,
    )

    # 4. record the delivered track + resolve the failure.
    entry = TrackEntry(
        relative_path=result.relative_path,
        filename=result.filename,
        artist=tags["artist"],  # type: ignore[arg-type]
        title=tags["title"],  # type: ignore[arg-type]
        album=tags["album"],  # type: ignore[arg-type]
        ext=Path(result.filename).suffix.lstrip("."),
        size_bytes=size_bytes,
        duration_sec=tags["duration_sec"],  # type: ignore[arg-type]
        bitrate_kbps=tags["bitrate_kbps"],  # type: ignore[arg-type]
        content_sha256=sha256,
        source=TrackSource(
            engine=failure.engine or "spotdl",
            input_url=failure.input_url,
            input_type=failure.input_type,
            resolved_provider="youtube",
            resolved_source_id=source_id,
        ),
    )
    entry.state.last_verified_at = utc_now_iso()
    ledger.upsert(entry)
    ledger.resolve_failure(failure.key)
    ledger.save()
    return True, f"{how} -> {result.relative_path}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", default=str(SMOKE_LEDGER),
                        help=f"Ledger JSON to retry from (default: {SMOKE_LEDGER})")
    parser.add_argument("--dry-run", action="store_true", help="List pending failures, retry nothing")
    parser.add_argument("--keep-local", action="store_true", help="Don't delete local temp downloads")
    parser.add_argument("--download-timeout", type=int, default=600,
                        help="Seconds to allow a single re-download to run")
    args = parser.parse_args()

    load_env_file(ENV_PATH)

    ledger_path = Path(args.ledger)
    ledger = Ledger.load(ledger_path)
    pending = ledger.pending_failures()

    print(f"ledger: {ledger_path}  ({len(pending)} pending failure(s))")
    if not pending:
        print("nothing to retry.")
        return

    for f in pending:
        where = f.local_path if (f.local_path and Path(f.local_path).exists()) else f"query={f.track_ref!r}"
        print(f"  - [{f.stage}] {f.key}  (attempts={f.attempts}; {where})")

    if args.dry_run:
        print("\n--dry-run: retried nothing.")
        return

    storage = DropboxStorage.from_env()
    ok = 0
    still_failing = 0
    print(f"\n=== retrying {len(pending)} failure(s) ===")
    for i, failure in enumerate(pending, 1):
        try:
            succeeded, note = retry_one(
                failure,
                storage=storage,
                ledger=ledger,
                download_timeout=args.download_timeout,
                keep_local=args.keep_local,
            )
        except Exception as exc:  # noqa: BLE001 - one still-failing track must not abort the batch
            # Re-log against the same key: bumps attempts, refreshes the error.
            ledger.record_failure(failure.key, failure.stage, exc)
            ledger.save()
            still_failing += 1
            print(f"  [{i}/{len(pending)}] still failing: {failure.key} — {exc}")
            continue

        if succeeded:
            ok += 1
            print(f"  [{i}/{len(pending)}] resolved: {note}")
        else:
            still_failing += 1
            print(f"  [{i}/{len(pending)}] skipped: {failure.key} — {note}")

    remaining = len(ledger.pending_failures())
    print("\n" + "=" * 60)
    print("RETRY SUMMARY")
    print(f"  resolved:      {ok}")
    print(f"  still failing: {still_failing}")
    print(f"  now pending:   {remaining}")
    print("=" * 60)


if __name__ == "__main__":
    main()
