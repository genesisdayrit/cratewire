"""Tests for the ledger's failure-logging API (the retry-later foundation).

Stdlib-only and self-contained: exercises `record_failure` / `resolve_failure`
/ `pending_failures`, the fill-forward + reopen semantics, atomic persistence,
and v1 -> v2 back-compat load — all without touching Dropbox, spotdl, or the
network. Runnable two ways:

    uv run pytest tests/test_ledger_failures.py     # if pytest is installed
    uv run python tests/test_ledger_failures.py      # plain runner, no deps
    python3 tests/test_ledger_failures.py            # ledger.py is stdlib-only
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Make `app` importable when run from server/ (mirrors the scripts/ convention).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage.ledger import (  # noqa: E402
    SCHEMA_VERSION,
    FailureEntry,
    Ledger,
    TrackEntry,
)


def _fresh_ledger() -> Ledger:
    tmp = Path(tempfile.mkdtemp()) / "ledger.json"
    return Ledger.load(tmp)


def test_record_new_failure() -> None:
    led = _fresh_ledger()
    led.record_failure(
        "download:foo",
        "download",
        LookupError("no results found for song: foo"),
        engine="spotdl",
        track_ref="Foo - Bar",
    )
    assert len(led.failures) == 1
    f = led.failures[0]
    assert f.key == "download:foo"
    assert f.stage == "download"
    assert f.status == "pending"
    assert f.attempts == 1
    assert f.last_error_type == "LookupError"
    assert "no results" in f.last_error_message
    assert f.track_ref == "Foo - Bar"
    assert f.id  # got an opaque id


def test_relog_same_key_bumps_not_duplicates() -> None:
    led = _fresh_ledger()
    led.record_failure("upload:x", "upload", RuntimeError("boom 1"))
    first_seen = led.failures[0].first_seen_at
    led.record_failure("upload:x", "upload", RuntimeError("boom 2"))
    assert len(led.failures) == 1, "same key must update in place, not duplicate"
    f = led.failures[0]
    assert f.attempts == 2
    assert f.last_error_message == "boom 2"
    assert f.first_seen_at == first_seen  # first_seen is sticky


def test_fill_forward_preserves_prior_info() -> None:
    led = _fresh_ledger()
    # First attempt learns the local file location; a retry has nothing new.
    led.record_failure("upload:x", "upload", RuntimeError("boom"), local_path="/tmp/x.m4a")
    led.record_failure("upload:x", "upload", RuntimeError("boom again"))
    f = led.failures[0]
    assert f.local_path == "/tmp/x.m4a", "None on re-log must not erase prior info"


def test_resolve_and_pending() -> None:
    led = _fresh_ledger()
    led.record_failure("a", "upload", RuntimeError("e"))
    led.record_failure("b", "download", RuntimeError("e"))
    assert {f.key for f in led.pending_failures()} == {"a", "b"}

    resolved = led.resolve_failure("a")
    assert resolved is not None and resolved.status == "resolved"
    assert resolved.resolved_at is not None
    assert {f.key for f in led.pending_failures()} == {"b"}


def test_resolve_missing_is_noop() -> None:
    led = _fresh_ledger()
    assert led.resolve_failure("nope") is None  # safe to call unconditionally


def test_reopen_resolved_on_regression() -> None:
    led = _fresh_ledger()
    led.record_failure("a", "upload", RuntimeError("e"))
    led.resolve_failure("a")
    led.record_failure("a", "upload", RuntimeError("failing again"))
    f = led.failures[0]
    assert f.status == "pending", "a resolved failure that fails again must reopen"
    assert f.resolved_at is None
    assert f.attempts == 2


def test_error_message_is_truncated() -> None:
    led = _fresh_ledger()
    led.record_failure("a", "upload", RuntimeError("x" * 5000))
    assert len(led.failures[0].last_error_message) <= 500


def test_roundtrip_persists_failures_and_tracks() -> None:
    led = _fresh_ledger()
    led.upsert(TrackEntry(relative_path="music/a.m4a", filename="a.m4a"))
    led.record_failure("download:b", "download", RuntimeError("nope"), track_ref="B")
    led.save()

    reloaded = Ledger.load(led.path)
    assert len(reloaded.tracks) == 1
    assert len(reloaded.failures) == 1
    assert reloaded.failures[0].key == "download:b"
    assert reloaded.failures[0].track_ref == "B"

    on_disk = json.loads(led.path.read_text())
    assert on_disk["version"] == SCHEMA_VERSION
    assert "failures" in on_disk


def test_loads_v1_file_without_failures() -> None:
    """A pre-failures (v1) ledger must load and upgrade to v2 on next save."""
    tmp = Path(tempfile.mkdtemp()) / "ledger.json"
    tmp.write_text(json.dumps({
        "version": 1,
        "updated_at": "2026-07-25T00:00:00Z",
        "tracks": [{"relative_path": "music/old.m4a", "filename": "old.m4a"}],
    }))
    led = Ledger.load(tmp)
    assert len(led.tracks) == 1
    assert led.failures == []

    led.save()  # should rewrite as current schema
    assert json.loads(tmp.read_text())["version"] == SCHEMA_VERSION


def test_from_dict_roundtrip() -> None:
    f = FailureEntry(key="k", stage="upload", last_error_type="E", last_error_message="m")
    assert FailureEntry.from_dict(f.to_dict()) == f


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
