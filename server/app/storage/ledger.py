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
  - Failures are recorded too, not just successes. A `FailureEntry` is a durable
    retry unit: enough to re-attempt a download/upload that fell over (throttling,
    no match, a transient API error) without re-deriving it. They live under a
    separate `failures` list, keyed so a retry updates the row in place instead of
    piling up one row per attempt. This is what makes "retry the stragglers later"
    possible — before, failures were printed and forgotten.

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

SCHEMA_VERSION = 2

# On-disk versions this build can still read directly. v1 predates the `failures`
# list; the track shape is unchanged between v1 and v2, so a v1 file loads as-is
# and is rewritten as v2 (with an empty `failures` list) on the next save — a
# migration-free upgrade. Add older versions here only if they stay readable.
_READABLE_VERSIONS = frozenset({1, 2})

# Error strings can be huge (stack-ish provider dumps). Cap what we persist so the
# ledger stays human-readable; the truncated tail is rarely needed to retry.
_MAX_ERROR_CHARS = 500


def utc_now_iso() -> str:
    """UTC timestamp, second precision, e.g. 2026-07-25T12:34:56Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(text: str, limit: int = _MAX_ERROR_CHARS) -> str:
    """Collapse whitespace-strip and cap a string for storage in the ledger."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


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


@dataclass
class FailureEntry:
    """A durable record of a delivery that failed, so it can be retried later.

    The happy path records tracks that landed in Dropbox. But at playlist scale,
    tracks routinely fail — throttling, no match, a transient upload error — and a
    failure is not a track (its bytes may not exist yet), so it can't be a
    `TrackEntry`. A `FailureEntry` carries just enough to re-attempt the work.

    Keyed by `key`, a stable identity for the *attempt target* (not the file's
    bytes): the intended `relative_path` for an upload that failed, or something
    like `download:<track>` for a download that never produced a file. Re-recording
    the same key updates the row in place — bump `attempts`, refresh `last_error*`
    and `last_attempt_at` — instead of piling up a row per retry.

    Retriability lives in the fields: `local_path` set (and still on disk) means a
    retry only needs to re-upload; otherwise `track_ref` + `input_*` are what a
    re-download needs.
    """

    key: str
    stage: str  # download | metadata | upload | ledger | unknown
    last_error_type: str  # exception class name, e.g. "ApiError"
    last_error_message: str  # str(exc), collapsed + truncated to keep the ledger readable
    engine: Optional[str] = None  # spotdl | streamrip | ...
    input_url: Optional[str] = None  # what the user handed the engine (playlist/track URL)
    input_type: Optional[str] = None  # spotify_playlist | spotify_track | query | ...
    track_ref: Optional[str] = None  # human label / spotdl query, e.g. "Artist - Title"
    relative_path: Optional[str] = None  # intended Dropbox destination, if known
    filename: Optional[str] = None
    local_path: Optional[str] = None  # downloaded file still on disk -> retry skips re-download
    status: str = "pending"  # pending | resolved (a retry succeeded) | abandoned (given up)
    attempts: int = 1
    first_seen_at: str = field(default_factory=utc_now_iso)
    last_attempt_at: str = field(default_factory=utc_now_iso)
    resolved_at: Optional[str] = None
    id: str = field(default_factory=new_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FailureEntry":
        return cls(**d)


class Ledger:
    """Load, upsert, and atomically persist the JSON ledger at `path`."""

    def __init__(
        self,
        path: Path,
        tracks: Optional[list[TrackEntry]] = None,
        failures: Optional[list[FailureEntry]] = None,
    ) -> None:
        self.path = path
        self.tracks: list[TrackEntry] = tracks or []
        self.failures: list[FailureEntry] = failures or []

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        """Read the ledger, or return an empty one if the file doesn't exist yet."""
        if not path.exists():
            return cls(path)
        data = json.loads(path.read_text())
        version = data.get("version")
        if version not in _READABLE_VERSIONS:
            raise ValueError(
                f"Ledger at {path} is schema version {version!r}; this build reads "
                f"{sorted(_READABLE_VERSIONS)} (writes v{SCHEMA_VERSION}). "
                "A migration is needed before this can be read."
            )
        tracks = [TrackEntry.from_dict(t) for t in data.get("tracks", [])]
        failures = [FailureEntry.from_dict(f) for f in data.get("failures", [])]
        return cls(path, tracks, failures)

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

    def get_failure(self, key: str) -> Optional[FailureEntry]:
        return next((f for f in self.failures if f.key == key), None)

    def record_failure(
        self,
        key: str,
        stage: str,
        error: BaseException,
        *,
        engine: Optional[str] = None,
        input_url: Optional[str] = None,
        input_type: Optional[str] = None,
        track_ref: Optional[str] = None,
        relative_path: Optional[str] = None,
        filename: Optional[str] = None,
        local_path: Optional[str] = None,
    ) -> FailureEntry:
        """Log a failed delivery under `key`, or re-log a retry of the same key.

        New key -> append a `pending` row. Existing key -> bump `attempts`, refresh
        `last_error*`/`last_attempt_at`, and reopen it to `pending` if a prior retry
        had marked it `resolved` (it's failing again). Optional fields are
        fill-forward: a non-None value overwrites, but None means "no new info" so a
        re-log never erases what the first attempt learned (e.g. a `local_path`).

        Does not save — the caller decides when to persist (usually right after, so
        a crash mid-batch keeps the failures already seen).
        """
        error_type = type(error).__name__
        error_message = _truncate(str(error))
        existing = self.get_failure(key)
        if existing is None:
            entry = FailureEntry(
                key=key,
                stage=stage,
                last_error_type=error_type,
                last_error_message=error_message,
                engine=engine,
                input_url=input_url,
                input_type=input_type,
                track_ref=track_ref,
                relative_path=relative_path,
                filename=filename,
                local_path=local_path,
            )
            self.failures.append(entry)
            return entry

        existing.stage = stage
        existing.last_error_type = error_type
        existing.last_error_message = error_message
        existing.attempts += 1
        existing.last_attempt_at = utc_now_iso()
        if existing.status == "resolved":  # regressed: succeeded once, failing again
            existing.status = "pending"
            existing.resolved_at = None
        for name, val in (
            ("engine", engine),
            ("input_url", input_url),
            ("input_type", input_type),
            ("track_ref", track_ref),
            ("relative_path", relative_path),
            ("filename", filename),
            ("local_path", local_path),
        ):
            if val is not None:
                setattr(existing, name, val)
        return existing

    def resolve_failure(self, key: str) -> Optional[FailureEntry]:
        """Mark the failure under `key` resolved — a later attempt succeeded.

        No-op returning None if there's no such failure, so a caller can resolve
        unconditionally after any success without first checking one was recorded.
        """
        existing = self.get_failure(key)
        if existing is None:
            return None
        existing.status = "resolved"
        existing.resolved_at = utc_now_iso()
        return existing

    def pending_failures(self) -> list[FailureEntry]:
        """Failures still awaiting a successful retry (status == 'pending')."""
        return [f for f in self.failures if f.status == "pending"]

    def save(self) -> None:
        """Write atomically: full temp file, then os.replace over the target."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": utc_now_iso(),
            "tracks": [t.to_dict() for t in self.tracks],
            "failures": [f.to_dict() for f in self.failures],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        os.replace(tmp, self.path)
