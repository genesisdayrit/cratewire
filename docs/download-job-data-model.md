# Download-job data model — a self-grilling

> Status: **proposal for review**. Nothing here is wired into the app yet. This
> is the output of a `/grill-me` session run in self-grilling mode (I posed the
> questions and answered them with recommendations) because you were on the road.
> Walk the decision tree below; for each **Decision** either accept my
> recommendation or redirect it. Decisions are ordered so that dependencies
> resolve first — later decisions assume the earlier ones went the recommended
> way, and I note where a different choice upstream changes them.

---

## 0. What this model has to do (the brief, restated)

From your ask, the model exists to answer these questions durably, forever:

1. **What work did I run?** A history of download jobs — what I asked for, when,
   which engine, and how it turned out.
2. **What succeeded and what failed?** Per-track outcomes inside a job, including
   failures *with reasons* (today failures are printed and thrown away).
3. **Where does each track live right now?** Presence across multiple
   destinations — Dropbox (→ USB for DJing) **and** Cloudflare (→ website) — not
   one baked-in destination.
4. **Can I reconcile after I clear things?** If I prune Dropbox or Cloudflare, I
   must still remember the track existed, its identity, and be able to detect
   "gone from destination D as of time T" and re-deliver it.
5. **Where are my logs?** A clear story for operational logs vs. domain history.

The frame: this becomes the backend a client (DJ ops + personal-website music
ops) is built on. So: production-grade, but staged — don't build the warehouse
before there's freight.

---

## 1. Where we are today (facts, looked up — not asking you)

- The server (`app/main.py`) exposes only `/health` and `/version`. All real
  download→upload→record work lives in `scripts/smoke_*.py`. **A "job" is
  currently an ephemeral script run** — nothing about it is persisted.
- `app/storage/ledger.py` is a **track-centric JSON ledger**. One `TrackEntry`
  row per delivered file, upserted by `relative_path`.
- A single `TrackEntry` fuses **three concerns**: identity (`id`,
  `content_sha256`, tags), provenance (`TrackSource`), and **one** Dropbox
  placement (`TrackState.uploaded_at`, `on_usb`, `status: active|cleared`).
- **Failures are never persisted.** In `smoke_playlist.py` they land in a local
  `failures` list, get printed, and are lost when the process exits.
- Destination is hard-coded to Dropbox. The ledger's `state` models exactly one
  placement, so "also in Cloudflare" has nowhere to go.
- Writes are atomic (temp + `os.replace`) and there's a `SCHEMA_VERSION` guard —
  good instincts that carry straight over to the new model.

**The gap in one sentence:** we have a *current-state file of delivered Dropbox
tracks*, and what we need is a *history of jobs, per-track outcomes (including
failures), and per-destination presence over time.*

---

## 2. The proposed shape (one diagram, then the grilling)

```
Job ──< JobItem >── Track ──< File >──< Placement >── Destination
 │         │          │        │           │
 │         │          │        │           └─ present | cleared | missing, per (file, destination)
 │         │          │        └─ physical bytes actually downloaded (sha256, ext, bitrate…)
 │         │          └─ the logical song (identity / dedup lives here)
 │         └─ one attempt to get one track within the job: ok | failed(+reason)
 └─ one run over one input (playlist / url / query): queued|running|done|partial|failed

events ─ append-only log of everything that happened (audit / history / reconcile trail)
```

Read it as: **a Job fans out into JobItems; a successful JobItem yields a File of
a Track; a File is placed into one or more Destinations; every state change also
drops a row in `events`.**

Each of the six modeling forks below is a **Decision** for you.

---

## Decision 1 — Storage substrate: SQLite (recommended), not JSON, not Postgres yet

**Question:** what stores this — keep JSON files, move to SQLite, or jump to Postgres?

**Recommendation: SQLite now**, behind a thin repository layer so Postgres later
is a swap, not a rewrite.

**Why.** The moment we have five related entities with joins ("show me every
failed item across jobs for tracks not currently in Cloudflare"), a list-of-dicts
JSON file is the wrong tool — every query is a full-file scan you hand-write, and
two writers (the server plus a future client) racing on one file is a corruption
waiting to happen even with atomic replace. SQLite gives real SQL, transactions,
indexes, and foreign keys in a **single zero-ops file** you can back up by
copying, commit-adjacent, and even let the client open read-only. Postgres buys
concurrency and network access we don't need until the downloader runs 24/7 on a
host separate from the client — deferred, per the README's own "deferred until
needed" list.

**What it costs.** A migration off JSON (small — one backfill script; §Decision 8),
and the discipline of a repository layer instead of poking the file directly.

**If you pick differently:** staying on JSON keeps the diff tiny but caps us at
one writer and hand-rolled queries — fine for one more slice, painful by the time
the client lands. Jumping to Postgres is defensible if you *know* the downloader
will be a separate always-on host very soon; then do it now to skip a second
migration.

**Downstream impact:** everything below is written assuming a relational store.
If you keep JSON, the *entities* still hold but become nested documents.

---

## Decision 2 — History: hybrid state + append-only events (recommended)

**Question:** do rows just hold current state (mutate in place), or do we record
every change as an event (event-sourced), or both?

**Recommendation: hybrid.** Normalized **state** tables (`jobs`, `job_items`,
`tracks`, `files`, `placements`) answer "what's true *now*" cheaply. One
append-only **`events`** table records every meaningful transition
(`job.started`, `track.downloaded`, `upload.succeeded`, `placement.cleared`,
`reconcile.observed_missing`) and is the **history + audit + reconcile trail**.

**Why.** Your two headline requirements pull in opposite directions. "Where does
this track live right now?" wants mutable current state (fast, simple). "Give me
the history of jobs and the trail of when things went missing" wants immutable
history. Pure state loses the past; pure event-sourcing makes "what's here now" a
replay you have to fold every read. The hybrid is the standard pragmatic
answer: mutate state for the present, append events for the past. Events are also
the safety net — if a state row is ever wrong, the event log is the source of
truth to rebuild it.

**What it costs.** Every write happens in a transaction that touches two places
(update the state row *and* insert an event). One helper enforces it so call
sites can't forget.

**If you pick differently:** state-only is less code and fine *if* you accept you
can't answer "when did this first fail / when did it vanish." Given "history" is
an explicit goal, I wouldn't.

**Depends on:** Decision 1 (needs a store with a real append-only table +
transactions — i.e. SQLite/Postgres, not JSON).

---

## Decision 3 — Entity decomposition: split the monolith into Job / JobItem / Track / File / Placement

This is the core of the redesign, so it has three sub-decisions.

### 3a — Separate **Track** (logical song) from **File** (physical bytes)? — recommended: **yes**

**Question:** is the thing we track the *song* or the *downloaded file*? Today
they're the same row.

**Recommendation: separate them.** A **Track** is the logical song (identity,
dedup, "I have this"). A **File** is a specific downloaded artifact of it (bytes,
`sha256`, ext, bitrate, which engine/provider produced it).

**Why.** They diverge constantly in your world:
- You download a track today from YouTube (lossy m4a), and **re-download it later
  lossless** from Qobuz — same song, two files. One-row-per-file can't say "same
  track, better copy now."
- The **same file** goes to **two destinations** (Dropbox + Cloudflare). Identity
  shouldn't be tangled with any one placement.
- A re-download for any reason produces new bytes (different `sha256`) of a track
  you already "have." You want to know you have the *song* independent of which
  *file* is current.

**What it costs.** One more table and a join. For the common case (one file per
track) it's a 1:1 that feels like overhead — but it's the seam that makes
"lossy now, lossless later" and multi-destination clean instead of hacked.

**If you pick differently (collapse Track+File):** simpler today, and tolerable
if you decide a re-download just *replaces* the row. But then "do I already have
this song, in any form?" and "keep both the lossy and lossless copies" get hard,
and you'll likely re-split later under load.

### 3b — Playlist fan-out: **JobItem rows**, not child jobs — recommended

**Question:** a playlist download is really N track downloads. Model it as one
Job with N **JobItems**, or as a parent Job spawning N child Jobs?

**Recommendation: one Job, N JobItems.** The Job is the run over the input
(playlist/url/query). Each **JobItem** is one attempt to obtain one track within
it — and crucially **JobItem is where a failure lives** (`status: ok | failed`,
`error_code`, `error_detail`, `retryable`). A single-track download is just a Job
with one JobItem — no special case.

**Why.** It matches how the engines behave (spotdl fans out internally, exits
non-zero if *some* tracks fail, and you already tolerate per-track failure). It
gives you the counts you want for free (requested/ok/failed per job) and a clean
home for the failure records that are currently thrown away. Child-jobs would
duplicate job machinery for no gain and make "how did *this playlist run* go?" a
group-by instead of a single parent row.

### 3c — JobItem ↔ Track linkage

A JobItem that **succeeds** points at the Track it produced (and the File). A
JobItem that **fails** may have no Track yet (couldn't find/download it) — so it
carries enough raw identity (input query, spotify id, intended artist/title) to
retry and to show "this is what I was trying to get." Link is nullable on failure.

**Depends on:** Decision 2 (JobItems + events together are how history reads).

---

## Decision 4 — Track identity / dedup key: surrogate PK + natural-key precedence, never auto-merge on fuzzy

**Question:** what makes two downloads "the same Track"? This is the subtlest one.

**Recommendation:** every Track gets an opaque surrogate primary key (like today's
`id`). Dedup uses a **precedence of natural keys**, strongest first:

1. **ISRC** if we can read it from tags (globally unique per recording — gold).
2. **Provider source id** — `(resolved_provider, resolved_source_id)`, e.g. the
   YouTube video id or Spotify track id. Stable, already captured in `TrackSource`.
3. **Normalized `artist + title + rounded duration`** — a *soft* signal only.

Rule: **auto-merge only on ISRC or provider-source-id match. Never silently merge
on the fuzzy artist/title key** — at most flag "possible duplicate" for review.

**Why.** `content_sha256` is byte identity — perfect for "is this the exact file
on my USB," useless for "is this the same *song*" (two YouTube encodes of one
track differ byte-for-byte). Fuzzy metadata over-merges (live vs. studio, remixes,
"feat." variants) — merging is destructive and hard to undo, so the bar for
*automatic* merge must be high. Surrogate PK keeps identity stable even as we
learn better keys later.

**What it costs.** A little logic to compute/compare keys on insert, and accepting
that some genuine dupes slip through rather than risk wrong merges.

**If you pick differently:** "dedup purely on content hash" is simplest but treats
every re-encode as a new song — you'll accrete duplicates. "Merge aggressively on
metadata" keeps the library tidy but *will* fuse tracks that shouldn't be, and
you won't notice until you're DJing the wrong version.

**Depends on:** Decision 3a (identity lives on Track, hash lives on File).

---

## Decision 5 — Destinations: a `destinations` table + generic `placements`, modeling the sync chain

**Question:** how do we represent Dropbox, Cloudflare R2, the Mac's synced
folder, and the USB — given a track goes to several?

**Recommendation:** a small **`destinations`** table (`kind`, `config`) and a
**`placements`** table = one row per **(File × Destination)** with its own
location and lifecycle: `remote_path/key`, `remote_id` (Dropbox `rev` / R2
`etag`), `status: present | cleared | missing`, `uploaded_at`, `cleared_at`,
`last_verified_at`, and the destination-side content hash for integrity.

Model the chain honestly — destinations come in two kinds:
- **`api_upload`** — we push to it directly: **Dropbox**, **Cloudflare R2**. We
  know exactly when a placement was created and can verify it via API.
- **`sync_derived`** — it appears because something *else* syncs: the **Mac
  Dropbox folder** and the **USB** are downstream of the Dropbox placement, not
  things we upload to. Presence here is *observed*, not *created*, by
  reconciliation. Today's `on_usb` / `usb_synced_at` become a `sync_derived`
  placement instead of two fields hard-wired into track state.

**Why.** This is the change your "Dropbox *or* Cloudflare" line demands. Pulling
placement out of `TrackState` and into its own per-destination table is what lets
one track be present in Dropbox, cleared from Cloudflare, and synced-to-USB all at
once — each independently reconcilable. Distinguishing upload-destinations from
sync-derived ones stops us from pretending we "uploaded to USB."

**What it costs.** A join to answer "where is this track," and a tiny bit of
config (registering each destination once).

**If you pick differently:** a fixed `dropbox_*` / `cloudflare_*` column pair on
the track is faster to write and readable at a glance — but every new destination
is a schema change, and "which destinations still have this" can't be a clean
query. For a system explicitly built around ≥2 destinations, the table wins.

**Depends on:** Decision 3a (placement attaches to File).

---

## Decision 6 — Reconciliation: current status on the placement **plus** verification events

**Question:** when we check "is this still in Dropbox/Cloudflare," do we just
overwrite `last_verified_at`, or record each check as an event?

**Recommendation: both.** The `placements` row holds the **current** verdict
(`status`, `last_verified_at`) for fast "what's live now." Each *check* also
appends a `reconcile.observed_*` **event** (present / missing / hash-mismatch)
with a timestamp — so you get the **trail**: when a track was last seen, when it
went missing, whether it flapped.

The reconcile loop (later, but the model must support it now):
1. List what a destination actually holds (Dropbox API / R2 `ListObjects` /
   `glob` the USB).
2. Diff against `placements` for that destination.
3. In-destination-but-not-in-DB → surface as untracked (adopt or ignore).
4. In-DB-but-not-in-destination → flip placement to `missing`, append event. This
   is the "I cleared Dropbox, now reconcile" path — the Track and File survive; we
   just know its bytes are gone from *that* destination and can re-deliver from
   another placement or re-download.
5. Present-but-hash-mismatch → append a `hash_mismatch` event (corruption/replace).

**Why.** "Reconcile after I clear things" is a core requirement, and the useful
version isn't just "is it there right now" but "*when* did it go, and can I get it
back." Current status answers the first; events answer the second. Re-using the
`events` table from Decision 2 means no new machinery.

**What it costs.** Reconcile writes both a status update and an event (same
transaction helper as Decision 2).

**If you pick differently:** status-only reconciliation is fine for "re-deliver
what's missing" but can't tell you *when* or *how often* — which you'll want the
first time something silently disappears.

**Depends on:** Decisions 2 and 5.

---

## Decision 7 — Failure taxonomy: an `error_code` enum + retryable flag + raw detail on JobItem

**Question:** how do we record *why* a track failed, usefully enough to act on?

**Recommendation:** on a failed JobItem, store a structured **`error_code`** from a
small enum, a **`retryable`** boolean, and free-text **`error_detail`** (the raw
engine message). Starter enum:

| `error_code`       | Meaning                                            | Retryable? |
|--------------------|----------------------------------------------------|------------|
| `not_found`        | No match on the provider for the query/id          | no         |
| `unavailable`      | Geoblocked / removed / region-locked               | maybe      |
| `throttled`        | Rate-limited by the provider                       | yes        |
| `download_error`   | Stream/network failure mid-download                | yes        |
| `transcode_error`  | ffmpeg failed                                       | maybe      |
| `tagging_error`    | Downloaded but metadata write failed               | yes        |
| `upload_error`     | Got the file, destination upload failed            | yes        |
| `timeout`          | Exceeded the job's time budget                      | yes        |
| `unknown`          | Uncaught — keep `error_detail` for triage           | maybe      |

**Why.** "What failed" is only actionable if you can tell *retry-this-later*
(throttled) from *never-going-to-work* (not_found) from *bug-in-my-pipeline*
(transcode_error). A bare error string can't drive a retry policy or a
"re-runnable failures" view in the client; a code can. Keeping the raw text
alongside means you never lose detail the enum didn't anticipate.

**What it costs.** Mapping engine errors → codes (a best-effort classifier;
default `unknown` + keep the text). Refine the enum as real failures show up.

**Depends on:** Decision 3b (JobItem is where this lives).

---

## Decision 8 — Migration & rollout: additive, keep JSON working, backfill, then cut over

**Question:** how much do we build now, and what happens to the existing ledger?

**Recommendation — phased, nothing ripped out until the new path is proven:**

- **Phase 0 (this doc):** agree the model. No code cutover.
- **Phase 1:** add SQLite schema + a repository layer as a **new** module
  (`app/storage/db.py` + `schema.sql`), alongside the JSON ledger. Write a
  one-shot **backfill** that reads `ledger.json` / `ledger.smoke.json` and
  populates `tracks`/`files`/`placements` (existing rows become a Dropbox
  `present` placement; `content_sha256`→File, `on_usb`→a `sync_derived`
  placement). The JSON `id` carries over as the Track surrogate key.
- **Phase 2:** teach the download/upload path to write jobs + job_items + events
  (start in `smoke_playlist.py` since it's the real end-to-end flow) — now
  **failures get persisted**. Keep dual-writing the JSON ledger until trust is
  built.
- **Phase 3:** expose read endpoints (`GET /jobs`, `GET /tracks`,
  `GET /tracks/{id}/placements`) — the client's API surface.
- **Phase 4:** the reconcile loop (Decision 6) and Cloudflare R2 as the second
  destination.
- **Phase 5:** retire the JSON ledger once SQLite is the source of truth.

**Why.** Keeps every step shippable and reversible, honoring the README's "defer
until needed." The backfill means no lost history. Failures start being recorded
at Phase 2 — the soonest concrete payoff.

**If you pick differently:** big-bang cutover is faster but throws away the safety
of dual-writing; not worth it for a system you're going to depend on.

---

## Decision 9 — Logs: three distinct layers, don't conflate them

You asked for help *thinking about logs*. The trap is treating "logs" as one
thing. There are three, with different owners and lifetimes:

| Layer | What it is | Where | Lifetime | Answers |
|-------|-----------|-------|----------|---------|
| **App logs** | Structured stdout (JSON lines) from the server/scripts | stdout / log file / log service | short (days–weeks) | "what's the process doing right now / why did it crash" |
| **Domain events** | The `events` table (Decision 2) | SQLite/Postgres | forever | "history of jobs, when a track failed, when a placement went missing" |
| **Engine output** | Raw spotdl/streamrip stdout+stderr per job | a `job.log_blob` column or a file keyed by job id | medium (keep while a job's fresh) | "what did the downloader actually print when this job partially failed" |

**Recommendation:** implement all three but keep them separate. App logs are for
operating the system and are disposable. The **events table is the durable
history** and the thing the client reads — it's not really "logs," it's your
domain audit trail, which is why it belongs in the DB, not a log file. Capture
raw engine output per job (you already parse spotdl's stdout for YouTube ids;
persisting it makes failures debuggable after the fact). Use structured
(key-value / JSON) app logs from the start — future-you grepping `job_id=…` will
thank present-you.

---

## The decision tree, at a glance (what I need from you)

Walk these in order; each "if you change it" note points at what shifts upstream.

1. **D1 Storage** → *SQLite now* (vs. keep JSON / vs. Postgres now)
2. **D2 History** → *hybrid state + events* (vs. state-only) — needs D1 relational
3. **D3a Track vs File** → *separate* (vs. collapse) — the pivotal split
4. **D3b Fan-out** → *Job + JobItems* (vs. child jobs)
5. **D4 Identity** → *surrogate + natural-key precedence, no fuzzy auto-merge*
6. **D5 Destinations** → *destinations table + generic placements, api_upload vs sync_derived*
7. **D6 Reconcile** → *current status + verification events*
8. **D7 Failures** → *error_code enum + retryable + raw detail*
9. **D8 Rollout** → *additive, backfill, dual-write, staged cutover*
10. **D9 Logs** → *three layers: app logs / events table / engine output*

My default if you say "looks right, go": build **Phase 1** (SQLite schema +
repository + backfill) exactly as speced in Decision 8, leaving the JSON ledger
untouched and working. Section below is the concrete schema that Phase 1 would
create — review it as the tangible version of everything above.

---

## Appendix — proposed SQLite schema (Phase 1 target, for review)

Illustrative DDL so the recommendations aren't hand-wavy. Not yet applied.

```sql
-- ---- Jobs: one run over one input -------------------------------------------
CREATE TABLE jobs (
  id            TEXT PRIMARY KEY,              -- surrogate (uuid hex, like today)
  input_url     TEXT,                          -- the playlist/track/url handed in
  input_query   TEXT,                          -- or a free-text search query
  input_type    TEXT NOT NULL,                 -- spotify_playlist|spotify_track|soundcloud_url|query|...
  engine        TEXT NOT NULL,                 -- spotdl|streamrip
  params_json   TEXT,                          -- format, providers, etc. (opaque JSON)
  status        TEXT NOT NULL DEFAULT 'queued',-- queued|running|done|partial|failed
  requested_count INTEGER,                     -- tracks the input resolved to (if known)
  ok_count      INTEGER NOT NULL DEFAULT 0,
  failed_count  INTEGER NOT NULL DEFAULT 0,
  log_blob      TEXT,                          -- raw engine stdout+stderr (Decision 9)
  created_at    TEXT NOT NULL,
  started_at    TEXT,
  finished_at   TEXT
);

-- ---- Tracks: the logical song; identity/dedup lives here (Decision 4) -------
CREATE TABLE tracks (
  id            TEXT PRIMARY KEY,              -- surrogate; stable forever
  artist        TEXT,
  title         TEXT,
  album         TEXT,
  duration_sec  INTEGER,
  isrc          TEXT,                          -- strongest natural key when present
  norm_key      TEXT,                          -- normalized artist+title+dur (soft dup signal)
  first_seen_at TEXT NOT NULL,
  UNIQUE (isrc)                                -- enforced only when isrc is non-null
);
CREATE INDEX idx_tracks_norm_key ON tracks (norm_key);

-- ---- Files: physical downloaded bytes of a track (Decision 3a) --------------
CREATE TABLE files (
  id             TEXT PRIMARY KEY,
  track_id       TEXT NOT NULL REFERENCES tracks(id),
  content_sha256 TEXT,                         -- byte identity (matches a USB copy)
  ext            TEXT,
  size_bytes     INTEGER,
  bitrate_kbps   INTEGER,
  is_lossless    INTEGER NOT NULL DEFAULT 0,
  engine         TEXT,                         -- how these bytes were produced
  resolved_provider  TEXT,                     -- youtube|youtube-music|soundcloud|qobuz|...
  resolved_source_id TEXT,                     -- e.g. youtube video id (provenance + dup key)
  input_url      TEXT,                         -- what the user handed the engine
  input_type     TEXT,
  downloaded_at  TEXT NOT NULL,
  UNIQUE (track_id, content_sha256)
);
CREATE INDEX idx_files_provider ON files (resolved_provider, resolved_source_id);

-- ---- Destinations: Dropbox / Cloudflare R2 / USB / Mac-sync (Decision 5) ----
CREATE TABLE destinations (
  id          TEXT PRIMARY KEY,                -- 'dropbox' | 'r2' | 'usb' | 'mac_sync'
  kind        TEXT NOT NULL,                   -- api_upload | sync_derived
  config_json TEXT,                            -- base path / bucket / etc.
  created_at  TEXT NOT NULL
);

-- ---- Placements: one file's presence in one destination (Decisions 5 & 6) ---
CREATE TABLE placements (
  id               TEXT PRIMARY KEY,
  file_id          TEXT NOT NULL REFERENCES files(id),
  destination_id   TEXT NOT NULL REFERENCES destinations(id),
  remote_path      TEXT,                       -- dropbox path / r2 key / usb rel path
  remote_id        TEXT,                       -- dropbox rev / r2 etag
  remote_hash      TEXT,                       -- destination-side content hash (integrity)
  status           TEXT NOT NULL DEFAULT 'present', -- present|cleared|missing
  uploaded_at      TEXT,
  cleared_at       TEXT,
  last_verified_at TEXT,
  UNIQUE (destination_id, remote_path)
);
CREATE INDEX idx_placements_file ON placements (file_id);
CREATE INDEX idx_placements_status ON placements (destination_id, status);

-- ---- Job items: per-track attempt within a job; FAILURES LIVE HERE (D3b/D7) -
CREATE TABLE job_items (
  id            TEXT PRIMARY KEY,
  job_id        TEXT NOT NULL REFERENCES jobs(id),
  track_id      TEXT REFERENCES tracks(id),    -- null when the item failed to resolve
  file_id       TEXT REFERENCES files(id),     -- null unless it succeeded
  intended_artist TEXT,                        -- what we were trying to get (for retry/display)
  intended_title  TEXT,
  source_input  TEXT,                          -- the per-track query/id we attempted
  status        TEXT NOT NULL,                 -- ok | failed | skipped
  error_code    TEXT,                          -- enum from Decision 7
  retryable     INTEGER,                       -- 0|1
  error_detail  TEXT,                          -- raw engine message
  created_at    TEXT NOT NULL
);
CREATE INDEX idx_job_items_job ON job_items (job_id);
CREATE INDEX idx_job_items_status ON job_items (status, error_code);

-- ---- Events: append-only history / audit / reconcile trail (Decisions 2,6,9)-
CREATE TABLE events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  at           TEXT NOT NULL,
  type         TEXT NOT NULL,                  -- job.started|track.downloaded|upload.succeeded|
                                               -- placement.cleared|reconcile.observed_missing|...
  job_id       TEXT REFERENCES jobs(id),
  track_id     TEXT REFERENCES tracks(id),
  file_id      TEXT REFERENCES files(id),
  placement_id TEXT REFERENCES placements(id),
  detail_json  TEXT                            -- type-specific payload
);
CREATE INDEX idx_events_at ON events (at);
CREATE INDEX idx_events_type ON events (type);
```

**Example queries this unlocks (the point of all the above):**

```sql
-- Everything I failed to get, still worth retrying, newest first:
SELECT job_id, intended_artist, intended_title, error_code, error_detail
FROM job_items WHERE status='failed' AND retryable=1 ORDER BY created_at DESC;

-- Tracks that exist in Dropbox but NOT in Cloudflare (need website upload):
SELECT t.artist, t.title FROM tracks t
JOIN files f ON f.track_id=t.id
JOIN placements pd ON pd.file_id=f.id AND pd.destination_id='dropbox' AND pd.status='present'
WHERE NOT EXISTS (
  SELECT 1 FROM placements pr
  WHERE pr.file_id=f.id AND pr.destination_id='r2' AND pr.status='present');

-- When did each currently-missing Dropbox placement go away?
SELECT track_id, at FROM events
WHERE type='reconcile.observed_missing' ORDER BY at DESC;
```
