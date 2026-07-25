"""Proof-of-concept: rip a track with streamrip (SoundCloud, free / no login).

This is the lossless-capable engine we're evaluating alongside spotdl. For FREE
it can only pull *freely-streamable* SoundCloud tracks (indie / NCS / CC / edits
& bootlegs) at ~128 kbps — Go+/label-locked tracks have no free stream, and
lossless requires Qobuz/Tidal credentials in streamrip's config.

Run:
    uv run python scripts/smoke_streamrip.py                       # search+grab a free track
    uv run python scripts/smoke_streamrip.py --query "artist title"
    uv run python scripts/smoke_streamrip.py --url https://soundcloud.com/.../track
    uv run python scripts/smoke_streamrip.py --codec MP3           # force a codec

Notes:
- streamrip reads a global config (auto-created on first run) that holds the
  SoundCloud client_id. `rip config path` shows where.
- Files land in server/downloads/streamrip-test/ (gitignored).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DEFAULT_QUERY = "Elektronomia Sky High"  # NCS release — freely streamable, good smoke pick
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads" / "streamrip-test"

AUDIO_EXTS = (".mp3", ".m4a", ".opus", ".flac", ".wav", ".aac", ".ogg")


def rip_bin() -> str:
    """The streamrip console script, resolved next to the current interpreter."""
    candidate = Path(sys.executable).parent / "rip"
    return str(candidate) if candidate.exists() else "rip"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--query", default=DEFAULT_QUERY, help="SoundCloud search query")
    g.add_argument("--url", help="A direct SoundCloud track/playlist URL")
    parser.add_argument("--codec", help="Transcode to ALAC, FLAC, MP3, AAC, or OGG")
    args = parser.parse_args()

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    before = {p for p in DOWNLOAD_DIR.rglob("*") if p.suffix.lower() in AUDIO_EXTS}

    cmd = [rip_bin(), "--no-db", "--folder", str(DOWNLOAD_DIR)]
    if args.codec:
        cmd += ["--codec", args.codec]
    if args.url:
        cmd += ["url", args.url]
    else:
        cmd += ["search", "soundcloud", "track", args.query, "--first"]

    print("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, timeout=600)
    if proc.returncode != 0:
        raise SystemExit(f"FAILED: rip exited {proc.returncode}")

    after = {p for p in DOWNLOAD_DIR.rglob("*") if p.suffix.lower() in AUDIO_EXTS}
    new = sorted(after - before)
    print(f"\n=== result: {len(new)} new file(s) in {DOWNLOAD_DIR} ===")
    for f in new:
        print(f"  {f.name}  ({f.stat().st_size // 1024} KB)")
    if not new:
        raise SystemExit(
            "No audio produced. Likely a Go+/label-locked track (no free stream) — "
            "try a freely-streamable track, or configure a paid source for lossless."
        )
    print("\nOK — streamrip pipeline verified (SoundCloud, free).")


if __name__ == "__main__":
    main()
