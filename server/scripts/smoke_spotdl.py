"""Smoke-test the spotdl pipeline end to end.

Proves, in order:
  1. spotdl is installed and its CLI runs      -> `spotdl --version`
  2. ffmpeg is discoverable on PATH            -> shutil.which
  3. Spotify metadata + YouTube matching work  -> `spotdl url <track>`   (no audio)
  4. (opt-in) a real download succeeds         -> `spotdl download ...`  (--download)

Run:
    uv run python scripts/smoke_spotdl.py                 # steps 1-3, fast, no audio
    uv run python scripts/smoke_spotdl.py --download      # + real single-track download

The default track is Rick Astley's "Never Gonna Give You Up" — the canonical
throwaway smoke-test song. Override with --track "<spotify url or query>".
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_TRACK = "https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8"  # Never Gonna Give You Up
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"


def run(label: str, args: list[str], timeout: int) -> str:
    """Run a spotdl subcommand, streaming nothing, returning stdout. Raises on failure."""
    print(f"\n=== {label} ===")
    print("$ " + " ".join(args))
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.strip())
    if proc.returncode != 0:
        raise SystemExit(f"FAILED [{label}]: exit code {proc.returncode}")
    return out


def spotdl(*args: str) -> list[str]:
    # Invoke via the current interpreter so we always hit this venv's spotdl.
    return [sys.executable, "-m", "spotdl", *args]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--track", default=DEFAULT_TRACK, help="Spotify URL or search query")
    parser.add_argument("--download", action="store_true", help="Actually download audio")
    parser.add_argument("--ffmpeg", help="Explicit path to ffmpeg (else PATH is used)")
    args = parser.parse_args()

    # 1. spotdl installed?
    run("spotdl version", spotdl("--version"), timeout=60)

    # 2. ffmpeg present?
    ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
    print("\n=== ffmpeg ===")
    if not ffmpeg:
        raise SystemExit("FAILED: ffmpeg not found on PATH (install it or pass --ffmpeg)")
    print(f"ffmpeg: {ffmpeg}")

    # 3. metadata + youtube match (no audio downloaded)
    run("resolve match (no download)", spotdl("url", args.track), timeout=180)

    if not args.download:
        print("\nOK — pipeline healthy. Re-run with --download for a real audio download.")
        return

    # 4. real download
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    dl_args = ["download", args.track, "--output", str(DOWNLOAD_DIR / "{artists} - {title}.{output-ext}")]
    if ffmpeg:
        dl_args += ["--ffmpeg", ffmpeg]
    run("download", spotdl(*dl_args), timeout=600)

    mp3s = list(DOWNLOAD_DIR.glob("*.mp3"))
    print(f"\n=== result ===\n{len(mp3s)} file(s) in {DOWNLOAD_DIR}:")
    for f in mp3s:
        print(f"  {f.name}  ({f.stat().st_size // 1024} KB)")
    if not mp3s:
        raise SystemExit("FAILED: download reported success but no .mp3 found")
    print("\nOK — full pipeline verified end to end.")


if __name__ == "__main__":
    main()
