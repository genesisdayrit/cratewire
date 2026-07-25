"""The delivery ledger — a JSON record of every track we've put in Dropbox.

This is a deliberate stopgap before a real database. Its job: know what's been
delivered (and its identity + provenance) so that later we can reconcile three
surfaces — what's in Dropbox, what's on the USB, what's safe to clear — and
prune Dropbox storage without losing the memory that we once had a track.

Design notes (grilled 2026-07-25):
  - Identity is decoupled from location: an immutable `id` (future DB primary
    key) travels with the track, while `relative_path` is just where it lives
    right now. `content_sha256` is the robust identity that also matches a file
    sitting on a USB by its bytes.
  - Upsert by `relative_path` (one row per file currently in Dropbox).
  - Pruning from Dropbox flips `state.status` to "cleared" — the row STAYS, so
    we remember the hash/source of something whose bytes are gone.
  - Writes are atomic (temp file + os.replace) so a crash never truncates it.
  - `version` is the file's schema version — a migration marker for later field
    changes and the eventual DB import. Bump only when the *structure* changes.

Reserved fields (album, duration_sec, bitrate_kbps, cleared_at, on_usb,
usb_synced_at) are nullable now so reconcile/clear + lossless later just fill
them in — no migration.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    """UTC timestamp, second precision, e.g. 2026-07-25T12:34:56Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    """Short, stable, opaque id. Becomes the primary key when we move to a DB."""
    return uuid.uuid4().hex[:12]


def compute_sha256(path: Path, *, chunk: int = 1 << 20) -> str:
    """Hash the audio bytes — robust identity that survives renames and matches a USB copy."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class TrackSource:
    """Provenance: how and from where the audio was actually obtained."""

    engine: str  # spotdl | streamrip | ...
    input_url: Optional[str] = None  # what the user handed the engine (e.g. a Spotify URL)
    input_type: Optional[str] = None  # spotify_track | query | soundcloud_url | ...
    resolved_provider: Optional[str] = None  # where audio really came from, e.g. youtube
    resolved_source_id: Optional[str] = None  # e.g. the YouTube video id (best-effort)


@dataclass
class TrackState:
    """Lifecycle + per-surface presence, for reconciliation and safe clearing."""

    status: str = "active"  # active | cleared (cleared = bytes pruned from Dropbox)
    uploaded_at: str = field(default_factory=utc_now_iso)
    cleared_at: Optional[str] = None
    on_usb: Optional[bool] = None  # None = unknown; filled by a future USB reconcile
    usb_synced_at: Optional[str] = None
    last_verified_at: Optional[str] = None  # last time we confirmed it's in Dropbox


@dataclass
class TrackEntry:
    """One delivered track. `relative_path` is the human/join key; `id` is immutable."""

    relative_path: str  # e.g. "music/Rick Astley - Never Gonna Give You Up.mp3"
    filename: str
    artist: Optional[str] = None
    title: Optional[str] = None
    album: Optional[str] = None
    ext: Optional[str] = None
    size_bytes: Optional[int] = None
    duration_sec: Optional[int] = None
    bitrate_kbps: Optional[int] = None
    content_sha256: Optional[str] = None
    source: Optional[TrackSource] = None
    state: TrackState = field(default_factory=TrackState)
    id: str = field(default_factory=new_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TrackEntry":
        d = dict(d)
        src = d.pop("source", None)
        st = d.pop("state", None)
        entry = cls(**d)
        if src is not None:
            entry.source = TrackSource(**src)
        if st is not None:
            entry.state = TrackState(**st)
        return entry


class Ledger:
    """Load, upsert, and atomically persist the JSON ledger at `path`."""

    def __init__(self, path: Path, tracks: Optional[list[TrackEntry]] = None) -> None:
        self.path = path
        self.tracks: list[TrackEntry] = tracks or []

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        """Read the ledger, or return an empty one if the file doesn't exist yet."""
        if not path.exists():
            return cls(path)
        data = json.loads(path.read_text())
        version = data.get("version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"Ledger at {path} is schema version {version!r}, expected {SCHEMA_VERSION}. "
                "A migration is needed before this can be read."
            )
        tracks = [TrackEntry.from_dict(t) for t in data.get("tracks", [])]
        return cls(path, tracks)

    def get(self, relative_path: str) -> Optional[TrackEntry]:
        return next((t for t in self.tracks if t.relative_path == relative_path), None)

    def upsert(self, entry: TrackEntry) -> TrackEntry:
        """Insert, or update in place by `relative_path` (preserving the existing `id`)."""
        existing = self.get(entry.relative_path)
        if existing is None:
            self.tracks.append(entry)
            return entry
        entry.id = existing.id  # identity is immutable across re-uploads
        self.tracks[self.tracks.index(existing)] = entry
        return entry

    def save(self) -> None:
        """Write atomically: full temp file, then os.replace over the target."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": utc_now_iso(),
            "tracks": [t.to_dict() for t in self.tracks],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        os.replace(tmp, self.path)
