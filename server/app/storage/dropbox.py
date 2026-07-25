"""Dropbox delivery backend — upload a local audio file into the app folder.

Why the API at all (vs just dropping files in ~/Dropbox): the downloader is
meant to run headless on a box that has no Dropbox folder mounted. Uploading via
the API from anywhere lands the file in Dropbox, and your Mac's desktop app then
syncs it down — the bridge that gets tracks onto a USB for DJing.

Auth is a refresh token (minted once via scripts/mint_dropbox_token.py). The SDK
trades it for short-lived access tokens automatically — no Redis, no manual
refresh. With App-folder access, all paths are relative to the app root, e.g.
"/music/foo.mp3" -> ~/Dropbox/Apps/<AppName>/music/foo.mp3.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import dropbox
from dropbox.exceptions import ApiError
from dropbox.files import FileMetadata, WriteMode


@dataclass
class UploadResult:
    """What an upload produced, in terms the ledger cares about."""

    dropbox_path: str  # API path, leading slash, e.g. "/music/foo.mp3"
    relative_path: str  # ledger key, no leading slash, e.g. "music/foo.mp3"
    filename: str
    size_bytes: int
    rev: str  # Dropbox revision id
    content_hash: str  # Dropbox's own content hash (not sha256; useful for integrity)


def _join(base_path: str, filename: str) -> str:
    """Build a clean Dropbox API path: single leading slash, no doubles."""
    base = "/" + base_path.strip("/") if base_path.strip("/") else ""
    return f"{base}/{filename}"


class DropboxStorage:
    """Concrete, adapter-shaped Dropbox uploader.

    Not abstracted behind a protocol yet (YAGNI) — but `upload()` is the seam a
    future `Storage` interface would expose, so extraction later is mechanical.
    """

    def __init__(self, dbx: dropbox.Dropbox, base_path: str = "/music") -> None:
        self.dbx = dbx
        self.base_path = base_path

    @classmethod
    def from_env(cls, base_path: Optional[str] = None) -> "DropboxStorage":
        """Build from DROPBOX_* env vars. The SDK auto-refreshes the access token."""
        app_key = os.getenv("DROPBOX_ACCESS_KEY")
        app_secret = os.getenv("DROPBOX_ACCESS_SECRET")
        refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
        missing = [
            name
            for name, val in [
                ("DROPBOX_ACCESS_KEY", app_key),
                ("DROPBOX_ACCESS_SECRET", app_secret),
                ("DROPBOX_REFRESH_TOKEN", refresh_token),
            ]
            if not val
        ]
        if missing:
            raise EnvironmentError(
                "Missing Dropbox credentials: "
                + ", ".join(missing)
                + ". See server/.env.example and scripts/mint_dropbox_token.py."
            )
        dbx = dropbox.Dropbox(
            oauth2_refresh_token=refresh_token,
            app_key=app_key,
            app_secret=app_secret,
        )
        return cls(dbx, base_path or os.getenv("DROPBOX_BASE_PATH") or "/music")

    def upload(
        self,
        local_path: Path,
        *,
        base_path: Optional[str] = None,
        delete_local: bool = True,
    ) -> UploadResult:
        """Upload one file, verify it landed, then optionally delete the local copy.

        `delete_local=True` is the production default (keeps a small VPS disk from
        filling with audio already delivered). Pass False (the smoke test's
        --keep-local) to leave the local file for inspection.
        """
        local_path = Path(local_path)
        if not local_path.is_file():
            raise FileNotFoundError(f"Nothing to upload at {local_path}")

        dest = _join(base_path if base_path is not None else self.base_path, local_path.name)
        data = local_path.read_bytes()

        # overwrite makes re-uploads idempotent (same path -> replace, not "(1)").
        self.dbx.files_upload(data, dest, mode=WriteMode.overwrite)

        # Verify independently rather than trusting the upload return.
        verified = self.dbx.files_get_metadata(dest)
        if not isinstance(verified, FileMetadata):
            raise RuntimeError(f"Uploaded {dest} but metadata lookup didn't return a file.")

        actual_path = verified.path_display or dest
        result = UploadResult(
            dropbox_path=actual_path,
            relative_path=actual_path.lstrip("/"),
            filename=local_path.name,
            size_bytes=verified.size,
            rev=verified.rev,
            content_hash=verified.content_hash or "",
        )

        if delete_local:
            local_path.unlink(missing_ok=True)

        return result

    def get_metadata(self, dropbox_path: str) -> Optional[FileMetadata]:
        """Return file metadata, or None if it isn't there."""
        try:
            meta = self.dbx.files_get_metadata(dropbox_path)
        except ApiError:
            return None
        return meta if isinstance(meta, FileMetadata) else None

    def delete(self, dropbox_path: str) -> None:
        """Remove a file from Dropbox (used by the smoke test to clean up)."""
        self.dbx.files_delete_v2(dropbox_path)
