# R8.2 — Execution ledger schema (local SQLite)

> **As-built status (updated R8.4.6).** This document was written at R8.2 as a **design**. The implemented system is described in [as_built_status.md](as_built_status.md); where this document and that one differ, **as_built_status.md is correct**. Labels used below: *Implemented and tested*, *Implemented, not live-tested*, *Proposed*, *Not yet implemented*, *Known limitation*. Sections not mentioned in the list are design only.
>
> * **Implemented and tested:** the seven tables, optimistic `state_version`, append-only `events`, `BEGIN IMMEDIATE` transactions (§4), locks with leases and same-host dead-holder takeover (§6), outbox (rows only), `deliveries` table (no caller).
> * **Changed since R8.2:** **schema version 2** (R8.4.2): `runs` gains `generation`, `supersedes_run_key`, `link_kind`, `link_operator`, `link_reason`, `acknowledged`, `ack_operator`, `ack_reason`, `ack_at`; uniqueness is `(client_key, period_start, period_end, generation)`; `requests.action` accepts `acknowledge|cancel|supersede`; version-1 ledgers migrate in one transaction. §7 *duplicate monthly requests* is now answered by generations (a replacement is a new linked run, never an edit of the old one). §6: liveness uses explicit-typed Windows API calls and treats unknown as alive; a background `LeaseKeeper` renews leases (`execution.heartbeat_interval_seconds`); the lock row's `acquired_at` is the lease identity; PID reuse is detected by process creation time.
> * **Errors:** SQLite failures are classified into `LedgerError(code, storage, transient)` (R8.4.1); only `ledger_busy` is transient.
> * **Known limitations:** lease times have one-second resolution; the creation time of the holder process is not stored in the lock row.
> * **Not yet implemented:** the §8 backup/retention jobs (no backup tool exists; see `deployment_readiness.md`).

Status: **design, not implemented.** No database is created by this document.

## 1. Principles

* A **separate file** from the history/snapshot database (`JASPER_SEO_HISTORY_DB`) and outside the output root;
  default `%LOCALAPPDATA%\JasperSEOReporting\ledger\ledger.sqlite`, override `JASPER_SEO_LEDGER_DB`. The loader
  refuses a path that overlaps the history database, the output root, the project tree or a OneDrive folder (the
  same rules as `output_layout.resolve_output_root` and `snapshots.store.in_cloud_sync_folder` [V]).
* The ledger stores **references only**: run IDs, hashes, file names, fixed codes. It never stores client names,
  e-mail addresses, report text, source data, paths outside the output root, credentials or tokens.
* It never reads from or writes to the snapshot database; the runner calls the engine and records the outcome.
* SQLite settings (as the existing store [V]): `PRAGMA journal_mode=DELETE` (rollback journal), `foreign_keys=ON`,
  `busy_timeout` (5 s), `synchronous=FULL`, explicit `BEGIN IMMEDIATE` for writes. `PRAGMA user_version` holds the
  schema version; opening a database with an unknown version fails closed.
* Only the runner writes. Read-only CLI commands open the file with `mode=ro` and never create it.

## 2. Tables

```sql
-- schema version 1 (PRAGMA user_version = 1)

CREATE TABLE requests (
  request_id        TEXT PRIMARY KEY CHECK (length(request_id) = 36),
  idempotency_key   TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 128),
  client_key        TEXT NOT NULL,
  period_id         TEXT NOT NULL CHECK (period_id GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  action            TEXT NOT NULL CHECK (action IN ('validate','dry_run','run','retry','publish','status')),
  requested_by      TEXT NOT NULL,
  requested_at      TEXT NOT NULL,
  received_at       TEXT NOT NULL,
  options_json      TEXT NOT NULL DEFAULT '{}',
  status            TEXT NOT NULL CHECK (status IN ('accepted','rejected','completed','replayed_only')),
  reject_code       TEXT,
  run_key           TEXT REFERENCES runs(run_key),
  response_json     TEXT,            -- the stored jasper.run-response; returned verbatim on replay
  responded_at      TEXT
);
CREATE INDEX requests_client_period ON requests(client_key, period_id);
CREATE INDEX requests_status ON requests(status);

CREATE TABLE runs (
  run_key           TEXT PRIMARY KEY,                -- '<client_key>/<period_id>'
  client_key        TEXT NOT NULL,
  period_start      TEXT NOT NULL,
  period_end        TEXT NOT NULL,
  state             TEXT NOT NULL CHECK (state IN (
                      'requested','collecting','rendering','rendered','finalizing','finalized',
                      'auditing','audited','publishing','ready_for_delivery','delivered',
                      'failed','blocked','cancelled')),
  state_version     INTEGER NOT NULL DEFAULT 0,      -- optimistic-concurrency counter, +1 per transition
  attempt_count     INTEGER NOT NULL DEFAULT 1,
  failed_stage      TEXT,
  error_category    TEXT,
  error_code        TEXT,
  retry_eligible    INTEGER NOT NULL DEFAULT 0 CHECK (retry_eligible IN (0,1)),
  retry_kind        TEXT CHECK (retry_kind IS NULL OR retry_kind IN ('collection','render','finalization','publish','delivery')),
  retry_after       TEXT,
  generation_date   TEXT,                            -- set once at first render; never changed
  finalized_at      TEXT,                            -- set once before the first finalize call; reused on retry
  replace_existing  INTEGER NOT NULL DEFAULT 0,
  render_input_sha256 TEXT, render_format_name TEXT, render_format_version INTEGER,
  engine_run_id     TEXT, engine_revision INTEGER,
  pdf_sha256        TEXT, pdf_bytes INTEGER, record_name TEXT,
  audit_outcome     TEXT, audit_finalized_valid INTEGER, audit_verification_complete INTEGER, audited_at TEXT,
  mode              TEXT NOT NULL DEFAULT 'production' CHECK (mode = 'production'),   -- dry runs are never recorded here
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  finished_at       TEXT,
  UNIQUE (client_key, period_start, period_end),
  CHECK (period_start <= period_end),
  CHECK (run_key = client_key || '/' || period_start || '_' || period_end)
);
CREATE INDEX runs_state ON runs(state);
CREATE INDEX runs_client ON runs(client_key, period_start);

CREATE TABLE run_attempts (
  run_key           TEXT NOT NULL REFERENCES runs(run_key),
  attempt_no        INTEGER NOT NULL,
  started_at        TEXT NOT NULL,
  finished_at       TEXT,
  start_state       TEXT NOT NULL,
  outcome           TEXT CHECK (outcome IS NULL OR outcome IN ('succeeded','failed','blocked','interrupted')),
  error_category    TEXT,
  trigger_request   TEXT REFERENCES requests(request_id),
  PRIMARY KEY (run_key, attempt_no)
);

CREATE TABLE events (                                 -- append-only
  event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  at                TEXT NOT NULL,
  run_key           TEXT REFERENCES runs(run_key),
  request_id        TEXT REFERENCES requests(request_id),
  attempt_no        INTEGER,
  kind              TEXT NOT NULL CHECK (kind IN ('state_change','info','warning','error','recovery','operator','lock')),
  from_state        TEXT,
  to_state          TEXT,
  code              TEXT NOT NULL,                   -- fixed vocabulary, never free text
  detail_json       TEXT NOT NULL DEFAULT '{}',      -- hashes / counts / fixed codes only
  CHECK (run_key IS NOT NULL OR request_id IS NOT NULL)
);
CREATE INDEX events_run ON events(run_key, event_id);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'ledger events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'ledger events are append-only'); END;

CREATE TABLE deliveries (                             -- runner-owned columns + mirror of the Make-owned Delivery tab
  delivery_key      TEXT PRIMARY KEY,                -- '<run_key>/<pdf16>/<recipient_set_hash16>'
  run_key           TEXT NOT NULL REFERENCES runs(run_key),
  pdf_sha256        TEXT NOT NULL,
  pdf_bytes         INTEGER NOT NULL,
  manifest_sha256   TEXT NOT NULL,
  drive_folder_id   TEXT NOT NULL,
  drive_file_id     TEXT NOT NULL,
  file_name         TEXT NOT NULL,
  uploaded_at       TEXT NOT NULL,
  upload_confirmed  INTEGER NOT NULL CHECK (upload_confirmed IN (0,1)),
  -- mirrored from the sheet (read-only on the runner side)
  recipient_set_hash16 TEXT,
  approval_status   TEXT CHECK (approval_status IS NULL OR approval_status IN ('pending','approved','rejected','expired')),
  approved_at       TEXT,
  delivery_status   TEXT CHECK (delivery_status IS NULL OR delivery_status IN ('not_started','sending','sent','failed','cancelled')),
  delivery_attempts INTEGER NOT NULL DEFAULT 0,
  sent_at           TEXT,
  last_error_category TEXT,
  mirrored_at       TEXT,
  UNIQUE (run_key, pdf_sha256, recipient_set_hash16)
);
CREATE INDEX deliveries_run ON deliveries(run_key);
CREATE INDEX deliveries_status ON deliveries(delivery_status);

CREATE TABLE locks (
  lock_name         TEXT PRIMARY KEY,                -- 'runner:global' or 'client:<client_key>'
  owner_id          TEXT NOT NULL,                   -- random UUID per process start
  pid               INTEGER NOT NULL,
  host              TEXT NOT NULL,
  acquired_at       TEXT NOT NULL,
  heartbeat_at      TEXT NOT NULL,
  expires_at        TEXT NOT NULL
);

CREATE TABLE outbox (                                 -- status messages to the Make.com status webhook
  outbox_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at        TEXT NOT NULL,
  run_key           TEXT REFERENCES runs(run_key),
  request_id        TEXT REFERENCES requests(request_id),
  destination       TEXT NOT NULL CHECK (destination IN ('make_status','make_delivery_ready')),
  dedupe_key        TEXT NOT NULL UNIQUE,            -- '<run_key>#<state_version>#<destination>'
  payload_json      TEXT NOT NULL,                   -- a jasper.run-response / jasper.delivery-ready message
  attempts          INTEGER NOT NULL DEFAULT 0,
  next_attempt_at   TEXT NOT NULL,
  delivered_at      TEXT,
  last_error_category TEXT
);
CREATE INDEX outbox_pending ON outbox(delivered_at, next_attempt_at);
```

(`requests.run_key` references `runs`, and `runs` is created first in the real script; the order above is for
readability. `PRAGMA foreign_keys=ON` is required.)

## 3. Keys and uniqueness

| Rule | Mechanism |
|---|---|
| One run per client and period | `runs.run_key` PK and `UNIQUE(client_key, period_start, period_end)`; `CHECK` ties `run_key` to its parts |
| One request per intended action | `requests.idempotency_key UNIQUE`; replay returns `response_json` |
| One delivery per report and recipient set | `deliveries.delivery_key` PK and `UNIQUE(run_key, pdf_sha256, recipient_set_hash16)` |
| One status message per state version | `outbox.dedupe_key UNIQUE` |
| One lock holder | `locks.lock_name PK` |
| Dry runs never recorded as runs | `runs.mode CHECK (mode = 'production')`; `dry_run` produces `events` rows only (with `request_id`) |

## 4. Transaction boundaries

Every write is a short `BEGIN IMMEDIATE … COMMIT`. **No transaction is held across** a network call, a render, or a
call into the engine.

1. **Intake:** one transaction inserts the `requests` row (and, for `run`, creates the `runs` row in state
   `requested` and an `events` row). A duplicate `idempotency_key` is a unique-constraint hit → return the stored
   response.
2. **Transition:** one transaction: `UPDATE runs SET state = :to, state_version = state_version + 1, … WHERE run_key = :k AND state = :from AND state_version = :v`
   (0 rows updated ⇒ somebody else moved it ⇒ abort the attempt, no side effects), then `INSERT events`, then
   `INSERT outbox` (dedupe key includes the new `state_version`).
3. **Stage execution** runs between two transitions with no transaction open: `collecting` is entered (committed)
   before collection starts and `rendered` is committed after `produce_report_draft` returned.
4. **Lock:** acquire = one transaction `INSERT` (or takeover `UPDATE … WHERE expires_at < :now`); heartbeat =
   `UPDATE locks SET heartbeat_at, expires_at WHERE lock_name AND owner_id`; release = `DELETE … WHERE owner_id`.
5. **Publish:** `deliveries` row is inserted in the same transaction that moves `publishing → ready_for_delivery`,
   after the upload was confirmed (see [delivery_architecture](delivery_architecture.md)).

## 5. State transition enforcement

The allowed transitions are the table in [execution_contract](execution_contract.md) §4, implemented as a closed
dictionary in `ledger.py`; an unlisted transition raises before any SQL is run. `failed`/`blocked` store
`failed_stage`, `error_category`, `error_code`, `retry_*`. `generation_date` and `finalized_at` are written once
(`WHERE generation_date IS NULL`) and then immutable; a trigger rejects changing them.

## 6. Locks and recovery after interruption

* Locks: `runner:global` (one runner process per machine, also protecting the engine's single-writer databases)
  and `client:<client_key>` (one run per client). Lease 10 minutes, heartbeat every 60 s from a daemon thread that
  only touches the ledger. A lock is *stale* when `expires_at < now` **and** its `pid` is not a running process on
  this host. Takeover writes a `lock` event and triggers recovery for the client's non-terminal run.
* Recovery on runner start (before accepting work): every run in an in-progress state (`collecting`, `rendering`,
  `finalizing`, `auditing`, `publishing`) whose lock is free/stale is set back to its **resume state** and an
  `attempt` is closed as `interrupted`:

| Interrupted in | Resume state | What the next attempt does |
|---|---|---|
| `collecting` | `requested` | re-collect and re-assemble (nothing was persisted by the engine) |
| `rendering` | `requested` (the next attempt starts at `collecting`) | re-assemble, then `render_service` uses the **open sidecar** (original hash and generation date) |
| `rendered` | `rendered` | go straight to `finalizing` with the stored `generation_date`; the sidecar and draft are verified by `finalize_report` |
| `finalizing` | `rendered` | call `finalize_report` again with the stored `render_input`, `finalized_at`, `replace_existing`; the engine's own reconciliation continues (snapshot/event/promote/record are idempotent [V]) |
| `finalized` | `finalized` | run the audit |
| `auditing` | `finalized` | run the audit again (read-only) |
| `publishing` | `audited` | look up the Drive file by `appProperties`; reuse it if complete, else upload; never overwrite |

  `rendering` resumes at `collecting` because the draft and sidecar on disk are the durable record, and the
  render service decides from them (R7.2 case table [V]); no partially rendered state is trusted.
* A run found `failed` with `retry_eligible` and `retry_after` in the past is picked up by the next poll as a new
  attempt only if a `retry` request exists (manual) **or** the automatic policy of
  [production_architecture](production_architecture.md) §8 allows it (`attempt_count` ≤ 3 and automatic-eligible).

## 7. Duplicate monthly requests

| Situation | Result |
|---|---|
| Same `idempotency_key` | stored response returned (`replayed: true`); no new work |
| New key, same `run_key`, run in progress | request stored as `rejected` with `run_in_progress`; nothing else changes |
| New key, same `run_key`, run `audited` or later | request `completed` with the stored result (`already_complete`); no regeneration |
| New key, same `run_key`, run `failed`, retry eligible, action `run` | rejected `use_retry` (explicit, auditable) |
| Two runner processes | the second fails to take `runner:global` and exits with code 50 |

Duplicate protection is therefore layered: sheet idempotency key → `requests` unique key → `runs` unique key →
engine idempotency (`already_finalized`, sidecar identity) → Drive lookup → delivery key.

## 8. Retention and backup

| Item | Policy |
|---|---|
| `runs`, `run_attempts`, `deliveries` | kept indefinitely (small) |
| `events` | kept 24 months, then exported to a compressed JSON-lines archive before any deletion (deletion is an explicit operator command, not automatic) [P] |
| `outbox` | delivered rows kept 90 days |
| Backups | nightly SQLite online backup (`sqlite3.Connection.backup`) to `%LOCALAPPDATA%\JasperSEOReporting\backups\ledger\ledger-YYYYMMDD.sqlite`, keep 30 daily + 12 monthly; the history database and the output root need their own scheduled copy to a second drive or encrypted off-machine location [U] |
| Integrity | `PRAGMA integrity_check` weekly; the `validate` command reports it |

The ledger can be rebuilt from the engine's artifacts only partially (run IDs, hashes and states of finalized
reports are recoverable from reviewer records and the audit; request history and attempts are not), which is why
it is backed up.
