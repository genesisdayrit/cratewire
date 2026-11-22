"""Unit tests for R2Storage. No live network — moto mocks S3, Stubber-style
fakes cover size-mismatch / missing-creds. Runnable via:

    uv run pytest tests/test_r2.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage.r2 import (  # noqa: E402
    DEFAULT_KEY_LAYOUT,
    R2Storage,
    content_type_for,
    public_url_for,
)


BUCKET = "cratewire-test"


def _storage(**kwargs) -> R2Storage:
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    return R2Storage(
        client,
        BUCKET,
        public_url=kwargs.get("public_url", "https://cdn.example"),
        key_prefix=kwargs.get("key_prefix", "prod"),
        key_layout=kwargs.get("key_layout", DEFAULT_KEY_LAYOUT),
    )


def _mp3(tmp_path: Path, name: str = "Artist - Title.mp3", body: bytes = b"ID3fake-audio") -> Path:
    path = tmp_path / name
    path.write_bytes(body)
    return path


@mock_aws
def test_upload_verifies_size_and_returns_key_etag_url(tmp_path: Path) -> None:
    storage = _storage()
    local = _mp3(tmp_path)
    key = storage.build_key(local.name, crate="House")
    result = storage.upload(local, key, delete_local=False)

    assert result.destination == "r2"
    assert result.key == key
    assert result.relative_path == key
    assert result.filename == local.name
    assert result.size_bytes == local.stat().st_size
    assert result.etag  # moto fills one
    assert result.public_url == public_url_for("https://cdn.example", key)

    head = storage.client.head_object(Bucket=BUCKET, Key=key)
    assert int(head["ContentLength"]) == result.size_bytes
    assert head["ContentType"] == "audio/mpeg"


@mock_aws
def test_upload_m4a_content_type(tmp_path: Path) -> None:
    storage = _storage()
    local = tmp_path / "track.m4a"
    local.write_bytes(b"ftyp")
    result = storage.upload(local, "prod/music/cratewire/crate/track.m4a", delete_local=False)
    head = storage.client.head_object(Bucket=BUCKET, Key=result.key)
    assert head["ContentType"] == "audio/mp4"


@mock_aws
def test_delete_local_after_verified_upload(tmp_path: Path) -> None:
    storage = _storage()
    local = _mp3(tmp_path)
    key = "prod/music/cratewire/_smoke_test/" + local.name
    storage.upload(local, key, delete_local=True)
    assert not local.exists()
    # object is still in the bucket
    head = storage.client.head_object(Bucket=BUCKET, Key=key)
    assert int(head["ContentLength"]) > 0


@mock_aws
def test_keep_local_leaves_file(tmp_path: Path) -> None:
    storage = _storage()
    local = _mp3(tmp_path)
    storage.upload(local, "k/" + local.name, delete_local=False)
    assert local.exists()


@mock_aws
def test_delete_removes_object(tmp_path: Path) -> None:
    storage = _storage()
    local = _mp3(tmp_path)
    key = "k/" + local.name
    storage.upload(local, key, delete_local=False)
    storage.delete(key)
    with pytest.raises(storage.client.exceptions.ClientError):
        storage.client.head_object(Bucket=BUCKET, Key=key)


def test_size_mismatch_raises(tmp_path: Path) -> None:
    local = _mp3(tmp_path)
    client = MagicMock()
    client.head_object.return_value = {"ContentLength": 1, "ETag": '"abc"'}
    storage = R2Storage(client, BUCKET)
    with pytest.raises(RuntimeError, match="size mismatch"):
        storage.upload(local, "k.mp3", delete_local=False)
    assert local.exists(), "must not delete local if verification failed"


def test_missing_file_raises(tmp_path: Path) -> None:
    storage = R2Storage(MagicMock(), BUCKET)
    with pytest.raises(FileNotFoundError):
        storage.upload(tmp_path / "nope.mp3", "k.mp3")


def test_from_env_missing_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(EnvironmentError, match="Missing R2 credentials"):
        R2Storage.from_env()


def test_from_env_builds_s3v4_auto_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct123")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "kid")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET_NAME", "my-bucket")
    monkeypatch.setenv("R2_PUBLIC_URL", "https://pub.example")
    monkeypatch.setenv("R2_KEY_PREFIX", "staging")
    captured: dict = {}

    def fake_client(service: str, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr("app.storage.r2.boto3.client", fake_client)
    storage = R2Storage.from_env()
    assert captured["service"] == "s3"
    assert captured["endpoint_url"] == "https://acct123.r2.cloudflarestorage.com"
    assert captured["region_name"] == "auto"
    assert captured["config"].signature_version == "s3v4"
    assert storage.bucket == "my-bucket"
    assert storage.public_url == "https://pub.example"
    assert storage.key_prefix == "staging"


def test_build_key_default_layout() -> None:
    storage = R2Storage(MagicMock(), BUCKET, key_prefix="prod")
    assert (
        storage.build_key("Artist - Title.mp3", crate="Deep House")
        == "prod/music/cratewire/Deep House/Artist - Title.mp3"
    )


def test_build_key_custom_layout() -> None:
    storage = R2Storage(
        MagicMock(),
        BUCKET,
        key_prefix="prod",
        key_layout="{prefix}/radio/{crate}/{filename}",
    )
    assert storage.build_key("x.mp3", crate="shows") == "prod/radio/shows/x.mp3"


def test_content_type_for() -> None:
    assert content_type_for(Path("a.mp3")) == "audio/mpeg"
    assert content_type_for(Path("a.m4a")) == "audio/mp4"
    assert content_type_for(Path("a.flac")) is None


def test_public_url_encodes_spaces() -> None:
    url = public_url_for("https://cdn.example", "prod/music/cratewire/c/Artist - Title.mp3")
    assert url == "https://cdn.example/prod/music/cratewire/c/Artist%20-%20Title.mp3"


def test_public_url_none_without_base() -> None:
    assert public_url_for(None, "k.mp3") is None
    assert public_url_for("", "k.mp3") is None
