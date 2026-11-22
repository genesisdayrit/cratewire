"""Smoke-test the Cloudflare R2 delivery leg, end to end.

Proves, in order:
  1. R2_* env vars are present (fail fast, before spending a download).
  2. spotdl downloads a real track as mp3 — same invocation as smoke_spotdl.py.
     spotdl can exit 0 on a failed download, so we also assert a .mp3 exists.
  3. R2Storage uploads it, head_object verifies the size, and we print the
     object key + public URL (if R2_PUBLIC_URL is set).
  4. Optional `--cleanup` deletes the object afterward.

Run (needs the R2_* env vars set — see .env.example):
    uv run python scripts/smoke_r2.py
    uv run python scripts/smoke_r2.py --url "https://open.spotify.com/track/..."
    SMOKE_SPOTIFY_URL=https://open.spotify.com/track/... uv run python scripts/smoke_r2.py
    uv run python scripts/smoke_r2.py --keep-local   # don't delete the temp download
    uv run python scripts/smoke_r2.py --cleanup      # delete the object from R2 after
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Make `app` importable when run as scripts/smoke_r2.py from server/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage.r2 import R2Storage  # noqa: E402

DEFAULT_TRACK = "https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8"  # Never Gonna Give You Up
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"
SMOKE_CRATE = "_smoke_test"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


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


def spotdl(*args: str) -> list[str]:
    # Invoke via the current interpreter so we always hit this venv's spotdl.
    return [sys.executable, "-m", "spotdl", *args]


def spotdl_download_mp3(track: str, ffmpeg: str | None) -> Path:
    """Download one track as mp3, matching smoke_spotdl.py. Require a real file."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    before = {p for p in DOWNLOAD_DIR.glob("*.mp3")}

    dl_args = [
        "download",
        track,
        "--output",
        str(DOWNLOAD_DIR / "{artists} - {title}.{output-ext}"),
    ]
    if ffmpeg:
        dl_args += ["--ffmpeg", ffmpeg]

    cmd = spotdl(*dl_args)
    print("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.strip())
    if proc.returncode != 0:
        raise SystemExit(f"FAILED: spotdl exited {proc.returncode}")

    after = {p for p in DOWNLOAD_DIR.glob("*.mp3")}
    new = sorted(after - before, key=lambda p: p.stat().st_mtime)
    if not new:
        # spotdl has been observed exiting 0 when the download actually failed.
        raise SystemExit(
            "FAILED: spotdl exited 0 but produced no .mp3 file. "
            "Check the output above — a match/throttling failure is the usual cause."
        )
    return new[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.getenv("SMOKE_SPOTIFY_URL", DEFAULT_TRACK),
        help="Spotify track URL (or SMOKE_SPOTIFY_URL). Default: Rick Astley.",
    )
    parser.add_argument("--keep-local", action="store_true", help="Don't delete the local temp download")
    parser.add_argument("--cleanup", action="store_true", help="Delete the object from R2 after verifying")
    parser.add_argument("--ffmpeg", help="Explicit path to ffmpeg (else PATH is used)")
    parser.add_argument(
        "--crate",
        default=SMOKE_CRATE,
        help=f"R2 crate folder segment (default: {SMOKE_CRATE})",
    )
    args = parser.parse_args()

    load_env_file(ENV_PATH)

    # 1. creds first — don't spend a YouTube fetch if R2 isn't configured.
    print("\n=== 1. R2 credentials ===")
    storage = R2Storage.from_env()
    print(f"bucket={storage.bucket} prefix={storage.key_prefix} "
          f"public_url={storage.public_url or '(not set)'}")

    ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
    print("\n=== ffmpeg ===")
    if not ffmpeg:
        raise SystemExit("FAILED: ffmpeg not found on PATH (install it or pass --ffmpeg)")
    print(f"ffmpeg: {ffmpeg}")

    # 2. download (mp3, same as smoke_spotdl.py --download)
    print("\n=== 2. spotdl download (mp3) ===")
    local_path = spotdl_download_mp3(args.url, ffmpeg)
    print(f"downloaded: {local_path.name}  ({local_path.stat().st_size // 1024} KB)")

    # 3. upload + head_object verify
    key = storage.build_key(local_path.name, crate=args.crate)
    print(f"\n=== 3. upload to R2 ({key}) ===")
    result = storage.upload(local_path, key, delete_local=not args.keep_local)
    print(f"uploaded + verified: key={result.key}")
    print(f"  etag={result.etag}  size={result.size_bytes} bytes")
    if result.public_url:
        print(f"  public_url={result.public_url}")
    else:
        print("  public_url=(not set — R2_PUBLIC_URL empty; bucket stays private)")

    # 4. optional cleanup
    if args.cleanup:
        print("\n=== 4. cleanup ===")
        storage.delete(result.key)
        print(f"deleted from R2: {result.key}")
    else:
        print(f"\n=== kept in R2: {result.key} ===")

    print("\nOK — R2 delivery leg verified end to end.")


if __name__ == "__main__":
    main()
