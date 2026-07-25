"""Storage: where downloaded audio goes, and the ledger of what we've delivered.

Deliberately concrete for now — one `DropboxStorage` backend and a JSON `Ledger`.
Both are shaped like adapters so a real interface can be extracted the day a
second backend (R2, local-folder) actually shows up. See the design memo:
grilled 2026-07-25, "dropbox-delivery-leg-design".
"""
