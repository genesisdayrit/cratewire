"""Smoke-test the SoundCloud pipeline via yt-dlp (free, no login).

This is the RELIABLE SoundCloud path. It exists because streamrip (see
scripts/smoke_streamrip.py) downloads SoundCloud's HLS stream — a list of ~10s
segments — and reassembles them in the WRONG ORDER: the file decodes cleanly and
has the right total duration, but sections play out of sequence. Proven on
2026-07-26; there is no progressive option in streamrip to avoid it.

yt-dlp fixes this structurally by preferring SoundCloud's `http_mp3_1_0` format —
a single *progressive* MP3 with no segments to reorder. If only HLS is available
yt-dlp still concatenates it in correct playlist order (unlike streamrip), so the
fallback is safe too.

Two reliability properties this script guarantees:
  1. It invokes yt-dlp as `python -m yt_dlp` through THIS venv's interpreter, so a
     moved/renamed venv can never break it via a stale console-script shebang
     (the failure mode that bit `rip` after the SpotWire -> Cratewire rename).
  2. It VERIFIES the output: a "success" that produced a scrambled or truncated
     stitch used to pass silently (a file appeared, it decoded fine). We now read
     the source's true duration from yt-dlp's info JSON and assert the downloaded
     file matches within a tolerance — truncation/duplication fails loudly.

yt-dlp is already in the venv (a spotdl dependency), so no new package is needed.

Run:
    uv run python scripts/smoke_soundcloud.py                       # default track
    uv run python scripts/smoke_soundcloud.py --url https://soundcloud.com/.../track
    uv run python scripts/smoke_soundcloud.py --format bestaudio    # override format selection

Files land in server/downloads/soundcloud-test/ (gitignored).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# A freely-streamable SoundCloud track for a default smoke run (NCS-style edit).
DEFAULT_URL = "https://soundcloud.com/unreleasedids10/jimi-jules-looking-for-you"
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads" / "soundcloud-test"

AUDIO_EXTS = (".mp3", ".m4a", ".opus", ".flac", ".wav", ".aac", ".ogg")

# Prefer SoundCloud's single progressive MP3 (no segments -> cannot be reordered);
# fall back to whatever best audio exists. yt-dlp orders HLS correctly if it wins.
DEFAULT_FORMAT = "http_mp3_1_0/bestaudio/best"

# The downloaded file must land within this many seconds of the source's reported
# duration. Generous enough for encoder/container rounding, tight enough to catch
# a dropped/duplicated segment or a truncated stream.
DURATION_TOLERANCE_SEC = 3.0


def ytdlp(*args: str) -> list[str]:
    """Invoke yt-dlp via the current interpreter so we always hit this venv's copy.

    Mirrors smoke_spotdl.py's `spotdl()` — using `-m` means there is no console
    script and therefore no hardcoded shebang to go stale when the venv moves.
    """
    return [sys.executable, "-m", "yt_dlp", *args]


def probe_duration(path: Path) -> float:
    """Duration of a media file in seconds, via ffprobe. Raises on failure."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise SystemExit(f"FAILED: ffprobe could not read duration of {path.name}\n{out.stderr.strip()}")
    return float(out.stdout.strip())


def fetch_info(url: str, timeout: int) -> dict:
    """yt-dlp's info JSON for a track (no download): source duration, title, etc."""
    print("\n=== 1. resolve (yt-dlp info, no download) ===")
    args = ytdlp("-J", "--no-warnings", url)
    print("$ " + " ".join(args))
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise SystemExit(f"FAILED: yt-dlp could not resolve {url}\n{proc.stderr.strip()}")
    info = json.loads(proc.stdout)
    extractor = info.get("extractor", "?")
    dur = info.get("duration")
    print(f"extractor: {extractor}   title: {info.get('title')!r}   "
          f"source duration: {dur}s")
    # Guard against pointing this SoundCloud smoke at some other site by mistake.
    if not str(extractor).startswith("soundcloud"):
        print(f"(note: extractor is {extractor!r}, not SoundCloud — continuing anyway)")
    if not dur:
        raise SystemExit("FAILED: source reported no duration; cannot verify the download.")
    return info


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="A SoundCloud track URL")
    parser.add_argument("--format", default=DEFAULT_FORMAT, help="yt-dlp format selector")
    parser.add_argument("--resolve-timeout", type=int, default=120, help="Seconds for the info fetch")
    parser.add_argument("--download-timeout", type=int, default=600, help="Seconds for the download")
    args = parser.parse_args()

    # 0. ffmpeg present? (yt-dlp needs it to embed metadata / concat any HLS fallback)
    if not shutil.which("ffmpeg"):
        raise SystemExit("FAILED: ffmpeg not found on PATH (brew install ffmpeg)")

    # 1. resolve source metadata (gives us the true duration to verify against)
    info = fetch_info(args.url, args.resolve_timeout)
    source_duration = float(info["duration"])

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    before = {p for p in DOWNLOAD_DIR.rglob("*") if p.suffix.lower() in AUDIO_EXTS}

    # 2. download the progressive stream, embedding tags + cover art
    print("\n=== 2. download (progressive preferred) ===")
    dl_args = ytdlp(
        "--no-warnings",
        "-f", args.format,
        "--embed-metadata",
        "--embed-thumbnail",
        "-o", str(DOWNLOAD_DIR / "%(uploader)s - %(title)s.%(ext)s"),
        args.url,
    )
    print("$ " + " ".join(dl_args))
    proc = subprocess.run(dl_args, timeout=args.download_timeout)
    if proc.returncode != 0:
        raise SystemExit(f"FAILED: yt-dlp exited {proc.returncode}")

    after = {p for p in DOWNLOAD_DIR.rglob("*") if p.suffix.lower() in AUDIO_EXTS}
    new = sorted(after - before)
    if not new:
        raise SystemExit("FAILED: yt-dlp reported success but produced no audio file.")

    # 3. VERIFY: the downloaded file's duration must match the source. This is the
    #    guard that would have caught the streamrip scramble's truncated cousins.
    print("\n=== 3. verify duration against source ===")
    ok = True
    for f in new:
        got = probe_duration(f)
        delta = abs(got - source_duration)
        status = "ok" if delta <= DURATION_TOLERANCE_SEC else "MISMATCH"
        if status != "ok":
            ok = False
        print(f"  {f.name}  ({f.stat().st_size // 1024} KB)  "
              f"duration {got:.1f}s vs source {source_duration:.1f}s  [{status}]")
    if not ok:
        raise SystemExit(
            f"FAILED: downloaded duration differs from source by more than "
            f"{DURATION_TOLERANCE_SEC}s — likely a truncated or bad stream."
        )

    print(f"\n=== result: {len(new)} file(s) in {DOWNLOAD_DIR} ===")
    print("\nOK — SoundCloud pipeline verified (yt-dlp, progressive + duration check).")


if __name__ == "__main__":
    main()
