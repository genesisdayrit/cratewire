"""Tests for the APScheduler lifespan wiring.

Proves the scheduler starts with the app, `register_jobs` is the hook for
later work, and shutdown is clean. No real jobs — this is scaffold only.
Runnable two ways:

    uv run pytest tests/test_scheduler.py     # if pytest is installed
    uv run python tests/test_scheduler.py      # plain runner, no extra deps
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Make `app` importable when run from server/ (mirrors the scripts/ convention).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402

from app.main import app, lifespan  # noqa: E402
from app.scheduler import register_jobs, scheduler  # noqa: E402


def test_register_jobs_is_the_hook_and_adds_nothing_yet() -> None:
    sched = BackgroundScheduler()
    register_jobs(sched)
    assert sched.get_jobs() == []


def test_lifespan_starts_and_stops_scheduler() -> None:
    async def _run() -> None:
        assert not scheduler.running
        async with lifespan(app):
            assert scheduler.running
        assert not scheduler.running

    asyncio.run(_run())


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
