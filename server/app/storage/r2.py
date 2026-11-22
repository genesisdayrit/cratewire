"""Cloudflare R2 delivery backend — S3-compatible upload of a local audio file.

R2 is the website/radio destination (Dropbox remains the DJ/USB path). Auth is
an R2 API token (access key + secret) against
`https://<account_id>.r2.cloudflarestorage.com`. Env var names match the
personal-site bucket so the same creds can be reused: `R2_ACCOUNT_ID`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, plus optional
`R2_PUBLIC_URL` and `R2_KEY_PREFIX` (default `prod`).

Default object key:
    {prefix}/music/cratewire/{crate}/{artists} - {title}.{ext}

Override the layout with `R2_KEY_LAYOUT` (Python format string with
`{prefix}`, `{crate}`, `{filename}`). Filename follows the same
`{artists} - {title}.{ext}` convention as Dropbox (whatever spotdl wrote).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from app.storage.base import UploadResult

DEFAULT_KEY_PREFIX = "prod"
DEFAULT_KEY_LAYOUT = "{prefix}/music/cratewire/{crate}/{filename}"

_CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
}

_REQUIRED_ENV = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME",
)


def content_type_for(path: Path) -> Optional[str]:
    """audio/mpeg for .mp3, audio/mp4 for .m4a/.mp4; None for anything else."""
    return _CONTENT_TYPES.get(Path(path).suffix.lower())


def public_url_for(public_base: Optional[str], key: str) -> Optional[str]:
    """Join `R2_PUBLIC_URL` and the object key; None if no public base is set."""
    if not public_base:
        return None
    encoded = "/".join(quote(part, safe="") for part in key.split("/") if part)
    return f"{public_base.rstrip('/')}/{encoded}"


class R2Storage:
    """Concrete, adapter-shaped R2 uploader (S3 API, region `auto`, sig v4)."""

    name = "r2"

    def __init__(
        self,
        client: BaseClient,
        bucket: str,
        *,
        public_url: Optional[str] = None,
        key_prefix: str = DEFAULT_KEY_PREFIX,
        key_layout: str = DEFAULT_KEY_LAYOUT,
    ) -> None:
        self.client = client
        self.bucket = bucket
        self.public_url = public_url or None
        self.key_prefix = key_prefix or DEFAULT_KEY_PREFIX
        self.key_layout = key_layout or DEFAULT_KEY_LAYOUT

    @classmethod
    def from_env(cls) -> "R2Storage":
        """Build from R2_* env vars. Raises EnvironmentError if creds are missing."""
        missing = [name for name in _REQUIRED_ENV if not os.getenv(name)]
        if missing:
            raise EnvironmentError(
                "Missing R2 credentials: "
                + ", ".join(missing)
                + ". Set them in server/.env (see .env.example). "
                "R2_PUBLIC_URL and R2_KEY_PREFIX are optional."
            )
        account_id = os.environ["R2_ACCOUNT_ID"]
        endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
        public_url = os.getenv("R2_PUBLIC_URL") or None
        key_prefix = os.getenv("R2_KEY_PREFIX") or DEFAULT_KEY_PREFIX
        key_layout = os.getenv("R2_KEY_LAYOUT") or DEFAULT_KEY_LAYOUT
        return cls(
            client,
            os.environ["R2_BUCKET_NAME"],
            public_url=public_url,
            key_prefix=key_prefix,
            key_layout=key_layout,
        )

    def build_key(self, filename: str, *, crate: str = "crate") -> str:
        """Render the object key from the configured layout."""
        prefix = self.key_prefix.strip("/")
        crate = crate.strip("/") or "crate"
        key = self.key_layout.format(prefix=prefix, crate=crate, filename=filename)
        return key.lstrip("/")

    def upload(
        self,
        local_path: Path,
        key: str,
        *,
        delete_local: bool = True,
    ) -> UploadResult:
        """Upload one file, verify via head_object (size match), then maybe delete local.

        `delete_local=True` is the production default (keeps a small VPS disk from
        filling with audio already delivered). Pass False to leave the local file
        for inspection or a second destination.
        """
        local_path = Path(local_path)
        if not local_path.is_file():
            raise FileNotFoundError(f"Nothing to upload at {local_path}")
        if not key or key.endswith("/"):
            raise ValueError(f"R2 object key must be a file key, got {key!r}")

        local_size = local_path.stat().st_size
        extra: dict[str, Any] = {}
        content_type = content_type_for(local_path)
        if content_type:
            extra["ContentType"] = content_type

        upload_kwargs: dict[str, Any] = {}
        if extra:
            upload_kwargs["ExtraArgs"] = extra
        self.client.upload_file(str(local_path), self.bucket, key, **upload_kwargs)

        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            raise RuntimeError(f"Uploaded {key} but head_object failed: {exc}") from exc

        remote_size = int(head["ContentLength"])
        if remote_size != local_size:
            raise RuntimeError(
                f"Uploaded {key} but size mismatch: local={local_size} remote={remote_size}"
            )

        etag = (head.get("ETag") or "").strip('"')
        result = UploadResult(
            destination="r2",
            relative_path=key,
            filename=local_path.name,
            size_bytes=remote_size,
            key=key,
            etag=etag,
            public_url=public_url_for(self.public_url, key),
        )

        if delete_local:
            local_path.unlink(missing_ok=True)

        return result

    def delete(self, key: str) -> None:
        """Remove an object from R2 (used by the smoke test to clean up)."""
        self.client.delete_object(Bucket=self.bucket, Key=key)
