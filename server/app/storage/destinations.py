"""Which delivery backends a download should hit.

`DELIVERY_DESTINATIONS=dropbox,r2` (comma-separated) fans a file out to both.
Default is Dropbox-only, matching the pre-R2 behavior. Unknown names fail
loudly rather than silently skipping a dest.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Sequence

from app.storage.base import UploadResult

VALID_DESTINATIONS = ("dropbox", "r2")
DEFAULT_DESTINATIONS = ("dropbox",)


def parse_destinations(raw: Optional[str] = None) -> tuple[str, ...]:
    """Parse `DELIVERY_DESTINATIONS` into a de-duplicated, order-preserving tuple."""
    if raw is None:
        raw = os.getenv("DELIVERY_DESTINATIONS", ",".join(DEFAULT_DESTINATIONS))
    names = tuple(dict.fromkeys(n.strip().lower() for n in raw.split(",") if n.strip()))
    if not names:
        raise ValueError(
            "DELIVERY_DESTINATIONS is empty; expected dropbox, r2, or both "
            "(e.g. DELIVERY_DESTINATIONS=dropbox,r2)."
        )
    unknown = [n for n in names if n not in VALID_DESTINATIONS]
    if unknown:
        raise ValueError(
            f"Unknown delivery destination(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(VALID_DESTINATIONS)}."
        )
    return names


def load_storages(names: Optional[Sequence[str]] = None) -> dict[str, Any]:
    """Construct the named backends from env. Only the requested dests are built."""
    resolved = tuple(names) if names is not None else parse_destinations()
    storages: dict[str, Any] = {}
    for name in resolved:
        if name == "dropbox":
            from app.storage.dropbox import DropboxStorage

            storages[name] = DropboxStorage.from_env()
        elif name == "r2":
            from app.storage.r2 import R2Storage

            storages[name] = R2Storage.from_env()
        else:
            raise ValueError(f"Unknown delivery destination: {name!r}")
    return storages


def upload_to(
    name: str,
    storage: Any,
    local_path: Path,
    *,
    dropbox_base_path: str,
    r2_crate: str,
    delete_local: bool = False,
) -> UploadResult:
    """Dispatch one upload to a named backend. Default is keep-local (fan-out).

    Callers that hit multiple destinations should pass `delete_local=False` and
    unlink the file themselves only after every dest succeeded.
    """
    if name == "dropbox":
        return storage.upload(
            local_path, base_path=dropbox_base_path, delete_local=delete_local
        )
    if name == "r2":
        key = storage.build_key(local_path.name, crate=r2_crate)
        return storage.upload(local_path, key, delete_local=delete_local)
    raise ValueError(f"Unknown delivery destination: {name!r}")


def failure_key(slot: str, destination: str) -> str:
    """Stable ledger failure key, including which dest the attempt targeted."""
    return f"deliver:{slot}:{destination}"
