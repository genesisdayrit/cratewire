"""In-process scheduler — APScheduler wired to the FastAPI lifespan.

This is the Python equivalent of node-cron: jobs run inside the server process.
No broker, no Redis, no Celery. Real download / Dropbox / ledger jobs land later;
this module is just the wiring and the hook to register them.

BackgroundScheduler (not AsyncIOScheduler) because the rest of the app is
synchronous — download engines and Dropbox I/O are blocking. A background
thread keeps that work off uvicorn's event loop. Switch to AsyncIOScheduler
if jobs become async coroutines.
"""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

# Module-level so lifespan start/stop and tests share one instance.
scheduler = BackgroundScheduler()


def register_jobs(sched: BackgroundScheduler) -> None:
    """Register recurring jobs. Add `sched.add_job(...)` calls here.

    Example (leave commented until you have a real callable):

        # sched.add_job(
        #     some_sync_function,
        #     trigger="cron",
        #     hour=3,
        #     id="nightly-sync",
        #     replace_existing=True,
        # )
    """
    # No jobs yet — add sched.add_job(...) calls above this line.
    _ = sched


def start_scheduler() -> None:
    """Register jobs and start the scheduler. Idempotent."""
    if scheduler.running:
        return
    register_jobs(scheduler)
    scheduler.start()


def shutdown_scheduler() -> None:
    """Stop the scheduler without waiting for in-flight jobs. Idempotent."""
    if not scheduler.running:
        return
    scheduler.shutdown(wait=False)
