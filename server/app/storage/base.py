"""Shared storage adapter types.

Dropbox and R2 both: upload a local file, verify it landed, optionally delete
the local copy, return an `UploadResult` the ledger can record. Dest-specific
kwargs (`base_path` vs `key`) stay on the concrete classes — unifying them
into one leaky parameter isn't worth it. The Protocol is the seam; extraction
later is mechanical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class UploadResult:
    """What an upload produced, in terms the ledger cares about."""

    relative_path: str  # ledger key: Dropbox path without leading slash, or R2 object key
    filename: str
    size_bytes: int
    destination: str = "dropbox"  # dropbox | r2
    # Dropbox-specific (empty on R2).
    dropbox_path: str = ""  # API path, leading slash, e.g. "/music/foo.mp3"
    rev: str = ""  # Dropbox revision id
    content_hash: str = ""  # Dropbox's own content hash (not sha256)
    # R2-specific (empty on Dropbox).
    key: str = ""
    etag: str = ""
    public_url: Optional[str] = None


class Storage(Protocol):
    """Upload adapter seam shared by DropboxStorage and R2Storage."""

    name: str

    def delete(self, remote_path: str) -> None:
        """Remove the uploaded object (smoke-test cleanup)."""
        ...
