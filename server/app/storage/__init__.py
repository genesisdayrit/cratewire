"""Storage: where downloaded audio goes, and the ledger of what we've delivered.

Backends (`DropboxStorage`, `R2Storage`) share the adapter types in `base.py`.
`Ledger` records each delivery (and failure), including which destination it
went to. `DELIVERY_DESTINATIONS` selects Dropbox, R2, or both.
"""
