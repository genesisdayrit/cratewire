"""Tests for DELIVERY_DESTINATIONS parsing and upload dispatch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage.base import UploadResult  # noqa: E402
from app.storage.destinations import (  # noqa: E402
    failure_key,
    parse_destinations,
    upload_to,
)


def test_parse_default_is_dropbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DELIVERY_DESTINATIONS", raising=False)
    assert parse_destinations() == ("dropbox",)


def test_parse_both_dedupes_and_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELIVERY_DESTINATIONS", "dropbox, r2, dropbox")
    assert parse_destinations() == ("dropbox", "r2")


def test_parse_r2_only() -> None:
    assert parse_destinations("r2") == ("r2",)


def test_parse_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown delivery destination"):
        parse_destinations("dropbox,s3")


def test_parse_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_destinations("  ,  ")


def test_failure_key_includes_destination() -> None:
    assert failure_key("House/a.mp3", "r2") == "deliver:House/a.mp3:r2"


class _FakeDropbox:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def upload(self, local_path, *, base_path, delete_local=True):
        self.calls.append((local_path, base_path, delete_local))
        return UploadResult(
            destination="dropbox",
            relative_path=base_path.lstrip("/") + "/" + local_path.name,
            filename=local_path.name,
            size_bytes=1,
            dropbox_path=base_path + "/" + local_path.name,
        )


class _FakeR2:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def build_key(self, filename: str, *, crate: str = "crate") -> str:
        return f"prod/music/cratewire/{crate}/{filename}"

    def upload(self, local_path, key, *, delete_local=True):
        self.calls.append((local_path, key, delete_local))
        return UploadResult(
            destination="r2",
            relative_path=key,
            filename=local_path.name,
            size_bytes=1,
            key=key,
        )


def test_upload_to_dropbox_keeps_local_by_default(tmp_path: Path) -> None:
    local = tmp_path / "a.mp3"
    local.write_bytes(b"x")
    fake = _FakeDropbox()
    result = upload_to(
        "dropbox", fake, local, dropbox_base_path="/music", r2_crate="crate"
    )
    assert result.destination == "dropbox"
    assert fake.calls[0][1] == "/music"
    assert fake.calls[0][2] is False  # delete_local default False for fan-out
    assert local.exists()


def test_upload_to_r2_uses_build_key(tmp_path: Path) -> None:
    local = tmp_path / "Artist - Title.mp3"
    local.write_bytes(b"x")
    fake = _FakeR2()
    result = upload_to(
        "r2", fake, local, dropbox_base_path="/music", r2_crate="Deep House"
    )
    assert result.key == "prod/music/cratewire/Deep House/Artist - Title.mp3"
    assert fake.calls[0][2] is False
