"""Mint a long-lived Dropbox refresh token — run this ONCE, by hand.

Dropbox access tokens are short-lived; the durable credential is a *refresh*
token, which the SDK then trades for access tokens automatically at runtime
(no Redis, no manual refresh). This script walks the offline OAuth flow to get
that refresh token.

Prereqs (see server/.env.example):
  - A dedicated Dropbox app: Scoped access -> App folder, with the
    files.content.write + files.content.read permissions enabled.
  - Its App key + App secret, either exported as DROPBOX_ACCESS_KEY /
    DROPBOX_ACCESS_SECRET (or in server/.env), or pasted when prompted.

Run:
    uv run python scripts/mint_dropbox_token.py

It prints an authorize URL -> open it, click Allow, paste the code back. On
success it prints DROPBOX_REFRESH_TOKEN=... which you copy into server/.env.
The token is NEVER written to disk by this script (so it can't be committed).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dropbox import DropboxOAuth2FlowNoRedirect

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_env_file(path: Path) -> None:
    """Best-effort: populate os.environ from a .env file (no dependency needed)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _need(name: str, prompt: str) -> str:
    val = os.getenv(name) or input(prompt).strip()
    if not val:
        raise SystemExit(f"FAILED: {name} is required.")
    return val


def _build_flow() -> DropboxOAuth2FlowNoRedirect:
    app_key = _need("DROPBOX_ACCESS_KEY", "Dropbox App key: ")
    app_secret = _need("DROPBOX_ACCESS_SECRET", "Dropbox App secret: ")
    # No PKCE (we have a client secret), so the flow is stateless across
    # processes: --start and --code can run as two separate invocations.
    return DropboxOAuth2FlowNoRedirect(
        app_key,
        consumer_secret=app_secret,
        token_access_type="offline",  # <- this is what yields a refresh token
    )


def _finish(code: str) -> None:
    try:
        result = _build_flow().finish(code.strip())
    except Exception as exc:  # noqa: BLE001 - surface the real reason to the user
        raise SystemExit(f"FAILED to finish OAuth: {exc}")
    print("\nOK — authorized as account id:", result.account_id)
    print("\nAdd this line to server/.env (keep it secret, never commit it):\n")
    print(f"DROPBOX_REFRESH_TOKEN={result.refresh_token}")


def main() -> None:
    _load_env_file(ENV_PATH)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", action="store_true", help="Only print the authorize URL")
    parser.add_argument("--code", help="Exchange this authorization code for a refresh token")
    args = parser.parse_args()

    if args.code:  # non-interactive finish
        _finish(args.code)
        return

    if args.start:  # non-interactive start
        print(_build_flow().start())
        return

    # interactive: start -> paste code -> finish
    flow = _build_flow()
    print("\n1. Open this URL and click 'Allow' (you may need to log in):\n")
    print("   " + flow.start())
    print("\n2. Copy the authorization code Dropbox shows you.\n")
    _finish(input("Paste the authorization code here: ").strip())


if __name__ == "__main__":
    main()
