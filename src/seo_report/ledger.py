"""The execution ledger of the production runner (R8.3): a local SQLite database, separate from the history/snapshot
database (docs/r8/ledger_schema.md).

What it stores: requests (with the response returned for them), one run per client and period, run attempts, an
append-only event history, the report-delivery mirror, locks and the outbox of status messages. It stores references
only (run IDs, hashes, file names, fixed codes): no client names, e-mail addresses, report text, source data, paths,
credentials or tokens.

Rules this module enforces
  * One run per `run_key` (`<client_key>/<period_id>`); one request per `idempotency_key`; one delivery per `delivery_key`.
  * Every write is a short `BEGIN IMMEDIATE` transaction. A state change is one transaction: the run row is updated only
    if it still has the expected state and `state_version` (optimistic check), the transition must be in
    `contracts.TRANSITIONS`, an event is appended and an outbox message may be enqueued. Nothing is held open across a
    network call, a render or a call into the engine.
  * Events cannot be updated or deleted (triggers). Runs cannot be deleted. `generation_date` and `finalized_at` can be
    written once and never changed (triggers), which is what keeps the render identity and the record timestamp stable
    across retries.
  * Locks (`runner:global`, `client:<client_key>`) have a lease. A lock is taken over only when its lease has expired AND
    its holder (same host) is no longer a running process.
  * Recovery (`recover_interrupted`) moves runs that were left in an in-progress state by a dead process back to their
    resume state and closes the attempt as interrupted.
  * SQLite is opened like the snapshot store: rollback journal (`DELETE`), busy timeout, foreign keys on, full sync; a
    database in a OneDrive folder is refused. A read-only open never creates the file.

All times come from the injected `clock` (an aware UTC `datetime`); this module never reads the system clock.
"""

import datetime as dt
import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from seo_report import contracts
from seo_report.snapshots.store import in_cloud_sync_folder

SCHEMA_VERSION = 2

_REQUESTS_TEMPLATE = """CREATE TABLE @NAME@ (
      request_id        TEXT PRIMARY KEY CHECK (length(request_id) = 36),
      idempotency_key   TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 128),
      client_key        TEXT NOT NULL,
      period_id         TEXT NOT NULL CHECK (period_id GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
      action            TEXT NOT NULL CHECK (action IN ('validate','dry_run','run','retry','publish','status','acknowledge','cancel','supersede')),
      requested_by      TEXT NOT NULL,
      requested_at      TEXT NOT NULL,
      received_at       TEXT NOT NULL,
      options_json      TEXT NOT NULL DEFAULT '{}',
      status            TEXT NOT NULL CHECK (status IN ('accepted','rejected','completed','replayed_only')),
      reject_code       TEXT,
      run_key           TEXT REFERENCES runs(run_key),
      response_json     TEXT,
      responded_at      TEXT)"""

_RUNS_TEMPLATE = """CREATE TABLE @NAME@ (
      run_key           TEXT PRIMARY KEY,
      client_key        TEXT NOT NULL,
      period_start      TEXT NOT NULL,
      period_end        TEXT NOT NULL,
      generation        INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1),
      supersedes_run_key TEXT REFERENCES runs(run_key),
      link_kind         TEXT CHECK (link_kind IS NULL OR link_kind IN ('supersede','restart')),
      link_operator     TEXT,
      link_reason       TEXT,
      acknowledged      INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0,1)),
      ack_operator      TEXT,
      ack_reason        TEXT,
      ack_at            TEXT,
      state             TEXT NOT NULL CHECK (state IN (
                          'requested','collecting','rendering','rendered','finalizing','finalized',
                          'auditing','audited','publishing','ready_for_delivery','delivered',
                          'failed','blocked','cancelled')),
      state_version     INTEGER NOT NULL DEFAULT 0,
      attempt_count     INTEGER NOT NULL DEFAULT 1,
      failed_stage      TEXT,
      error_category    TEXT,
      error_code        TEXT,
      retry_eligible    INTEGER NOT NULL DEFAULT 0 CHECK (retry_eligible IN (0,1)),
      retry_kind        TEXT CHECK (retry_kind IS NULL OR retry_kind IN ('collection','render','finalization','publish','delivery')),
      retry_after       TEXT,
      generation_date   TEXT,
      finalized_at      TEXT,
      replace_existing  INTEGER NOT NULL DEFAULT 0 CHECK (replace_existing IN (0,1)),
      render_input_sha256 TEXT, render_format_name TEXT, render_format_version INTEGER,
      engine_run_id     TEXT, engine_revision INTEGER,
      pdf_sha256        TEXT, pdf_bytes INTEGER, record_name TEXT,
      audit_outcome     TEXT, audit_finalized_valid INTEGER, audit_verification_complete INTEGER, audited_at TEXT,
      mode              TEXT NOT NULL DEFAULT 'production' CHECK (mode = 'production'),
      created_at        TEXT NOT NULL,
      updated_at        TEXT NOT NULL,
      finished_at       TEXT,
      UNIQUE (client_key, period_start, period_end, generation),
      CHECK (period_start <= period_end),
      CHECK (run_key = client_key || '/' || period_start || '_' || period_end
                       || CASE WHEN generation = 1 THEN '' ELSE '~' || generation END),
      CHECK ((generation = 1) = (supersedes_run_key IS NULL)),
      CHECK ((supersedes_run_key IS NULL) = (link_kind IS NULL)))"""


def _requests_ddl(name: str) -> str:
    return _REQUESTS_TEMPLATE.replace("@NAME@", name)


def _runs_ddl(name: str) -> str:
    return _RUNS_TEMPLATE.replace("@NAME@", name)


_REQUESTS_EXTRAS = (
    "CREATE INDEX requests_client_period ON requests(client_key, period_id)",
    "CREATE INDEX requests_status ON requests(status)",
)
_RUNS_EXTRAS = (
    "CREATE INDEX runs_state ON runs(state)",
    "CREATE INDEX runs_client ON runs(client_key, period_start)",
    "CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT, 'ledger runs are never deleted'); END",
    """CREATE TRIGGER runs_write_once BEFORE UPDATE OF generation_date, finalized_at ON runs
       WHEN (OLD.generation_date IS NOT NULL AND NEW.generation_date IS NOT OLD.generation_date)
         OR (OLD.finalized_at IS NOT NULL AND NEW.finalized_at IS NOT OLD.finalized_at)
       BEGIN SELECT RAISE(ABORT, 'generation_date and finalized_at are write-once'); END""",
)
_REQUEST_COLUMNS = ("request_id", "idempotency_key", "client_key", "period_id", "action", "requested_by", "requested_at",
                    "received_at", "options_json", "status", "reject_code", "run_key", "response_json", "responded_at")
_RUN_COLUMNS_V1 = (
    "run_key", "client_key", "period_start", "period_end", "state", "state_version", "attempt_count", "failed_stage",
    "error_category", "error_code", "retry_eligible", "retry_kind", "retry_after", "generation_date", "finalized_at",
    "replace_existing", "render_input_sha256", "render_format_name", "render_format_version", "engine_run_id",
    "engine_revision", "pdf_sha256", "pdf_bytes", "record_name", "audit_outcome", "audit_finalized_valid",
    "audit_verification_complete", "audited_at", "mode", "created_at", "updated_at", "finished_at")

_DDL = (
    _requests_ddl("requests"),
    *_REQUESTS_EXTRAS,
    _runs_ddl("runs"),
    *_RUNS_EXTRAS,
    """CREATE TABLE run_attempts (
      run_key           TEXT NOT NULL REFERENCES runs(run_key),
      attempt_no        INTEGER NOT NULL,
      started_at        TEXT NOT NULL,
      finished_at       TEXT,
      start_state       TEXT NOT NULL,
      outcome           TEXT CHECK (outcome IS NULL OR outcome IN ('succeeded','failed','blocked','interrupted')),
      error_category    TEXT,
      trigger_request   TEXT REFERENCES requests(request_id),
      PRIMARY KEY (run_key, attempt_no))""",
    """CREATE TABLE events (
      event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
      at                TEXT NOT NULL,
      run_key           TEXT REFERENCES runs(run_key),
      request_id        TEXT REFERENCES requests(request_id),
      attempt_no        INTEGER,
      kind              TEXT NOT NULL CHECK (kind IN ('state_change','info','warning','error','recovery','operator','lock')),
      from_state        TEXT,
      to_state          TEXT,
      code              TEXT NOT NULL,
      detail_json       TEXT NOT NULL DEFAULT '{}',
      CHECK (run_key IS NOT NULL OR request_id IS NOT NULL OR kind IN ('lock','recovery','info')))""",
    "CREATE INDEX events_run ON events(run_key, event_id)",
    "CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'ledger events are append-only'); END",
    "CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'ledger events are append-only'); END",
    """CREATE TABLE deliveries (
      delivery_key      TEXT PRIMARY KEY,
      run_key           TEXT NOT NULL REFERENCES runs(run_key),
      pdf_sha256        TEXT NOT NULL,
      pdf_bytes         INTEGER NOT NULL,
      manifest_sha256   TEXT NOT NULL,
      drive_folder_id   TEXT NOT NULL,
      drive_file_id     TEXT NOT NULL,
      file_name         TEXT NOT NULL,
      uploaded_at       TEXT NOT NULL,
      upload_confirmed  INTEGER NOT NULL CHECK (upload_confirmed IN (0,1)),
      recipient_set_hash16 TEXT,
      approval_status   TEXT CHECK (approval_status IS NULL OR approval_status IN ('pending','approved','rejected','expired')),
      approved_at       TEXT,
      delivery_status   TEXT CHECK (delivery_status IS NULL OR delivery_status IN ('not_started','sending','sent','failed','cancelled')),
      delivery_attempts INTEGER NOT NULL DEFAULT 0,
      sent_at           TEXT,
      last_error_category TEXT,
      mirrored_at       TEXT,
      UNIQUE (run_key, pdf_sha256, recipient_set_hash16))""",
    "CREATE INDEX deliveries_run ON deliveries(run_key)",
    "CREATE INDEX deliveries_status ON deliveries(delivery_status)",
    """CREATE TABLE locks (
      lock_name         TEXT PRIMARY KEY,
      owner_id          TEXT NOT NULL,
      pid               INTEGER NOT NULL,
      host              TEXT NOT NULL,
      acquired_at       TEXT NOT NULL,
      heartbeat_at      TEXT NOT NULL,
      expires_at        TEXT NOT NULL)""",
    """CREATE TABLE outbox (
      outbox_id         INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at        TEXT NOT NULL,
      run_key           TEXT REFERENCES runs(run_key),
      request_id        TEXT REFERENCES requests(request_id),
      destination       TEXT NOT NULL CHECK (destination IN ('make_status','make_delivery_ready')),
      dedupe_key        TEXT NOT NULL UNIQUE,
      payload_json      TEXT NOT NULL,
      attempts          INTEGER NOT NULL DEFAULT 0,
      next_attempt_at   TEXT NOT NULL,
      delivered_at      TEXT,
      last_error_category TEXT)""",
    "CREATE INDEX outbox_pending ON outbox(delivered_at, next_attempt_at)",
)
_TABLES = ("requests", "runs", "run_attempts", "events", "deliveries", "locks", "outbox")

# columns of `runs` that a transition may set
SETTABLE = frozenset({
    "failed_stage", "error_category", "error_code", "retry_eligible", "retry_kind", "retry_after", "generation_date",
    "finalized_at", "replace_existing", "render_input_sha256", "render_format_name", "render_format_version",
    "engine_run_id", "engine_revision", "pdf_sha256", "pdf_bytes", "record_name", "audit_outcome",
    "audit_finalized_valid", "audit_verification_complete", "audited_at"})
_FAILURE_COLUMNS = ("failed_stage", "error_category", "error_code", "retry_kind", "retry_after")
_SUCCESS_STATES = ("audited", "ready_for_delivery", "delivered")
_LOCK_NAME_PREFIXES = ("runner:global", "client:")


# Codes that mean the database itself (not one run's logic) failed. `ledger_busy` is the only transient one: the
# transaction did not happen and the same operation may succeed later. The others need an operator.
STORAGE_CODES = frozenset({"ledger_unavailable", "ledger_busy", "ledger_disk_full", "ledger_io_error", "ledger_corrupt",
                           "ledger_connection_unusable", "ledger_error"})
TRANSIENT_CODES = frozenset({"ledger_busy"})

# SQLite primary result codes (the low byte of `sqlite_errorcode`)
_BUSY, _LOCKED, _PERM, _READONLY, _IOERR, _CORRUPT, _FULL, _CANTOPEN, _CONSTRAINT, _NOTADB = (
    5, 6, 3, 8, 10, 11, 13, 14, 19, 26)


class LedgerError(RuntimeError):
    """A ledger operation failed. `code` is fixed; messages never hold client data or SQL text.

    `storage` is True when the database itself failed (busy, full, I/O, corrupt, unusable); `transient` is True when the
    same operation may succeed later (busy or locked). Logical errors (stale state, a lost lock, a duplicate key) have
    neither flag. The flags default from the code, and an explicit value overrides them.
    """

    def __init__(self, code: str, *, storage: bool | None = None, transient: bool | None = None):
        super().__init__(code)
        self.code = code
        self.storage = (code in STORAGE_CODES) if storage is None else bool(storage)
        self.transient = (code in TRANSIENT_CODES) if transient is None else bool(transient)


def classify_sqlite_error(error: sqlite3.Error) -> str:
    """The ledger code for a SQLite error. Uses SQLite's own result code when the exception has one, else its text."""
    if isinstance(error, sqlite3.IntegrityError):
        return "ledger_constraint_failed"
    if isinstance(error, sqlite3.ProgrammingError):
        return "ledger_connection_unusable"  # for example a closed connection
    primary = getattr(error, "sqlite_errorcode", None)
    primary = None if not isinstance(primary, int) else primary & 0xFF
    text = str(error).lower()
    if primary in (_BUSY, _LOCKED) or (primary is None and ("locked" in text or "busy" in text)):
        return "ledger_busy"
    if primary == _FULL or (primary is None and "disk is full" in text):
        return "ledger_disk_full"
    if primary == _IOERR or (primary is None and "i/o error" in text):
        return "ledger_io_error"
    if primary in (_CORRUPT, _NOTADB) or (primary is None and ("malformed" in text or "not a database" in text
                                                               or "corrupt" in text)):
        return "ledger_corrupt"
    if primary in (_CANTOPEN, _READONLY, _PERM) or (primary is None and ("unable to open" in text
                                                                          or "readonly" in text or "read-only" in text)):
        return "ledger_unavailable"
    return "ledger_error"


def wrap_sqlite_error(error: sqlite3.Error) -> "LedgerError":
    """A structured `LedgerError` for a SQLite error (raise it `from` the original to keep the chain)."""
    return LedgerError(classify_sqlite_error(error))


class StaleStateError(LedgerError):
    """The run no longer has the expected state or version (another writer moved it)."""


class InvalidTransitionError(LedgerError):
    """The requested state change is not in `contracts.TRANSITIONS`."""


class LockHeldError(LedgerError):
    """The lock is held by another live owner."""


_WIN_ACCESS_DENIED = 5
_WIN_INVALID_PARAMETER = 87  # OpenProcess: no process has this id
_WIN_STILL_ACTIVE = 259
_WIN_QUERY_LIMITED = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION: enough for the exit code and times, nothing more
_WIN_EPOCH_100NS = 116444736000000000  # 1601-01-01 to 1970-01-01 in 100 ns ticks
_API = {}


def _windows_api():
    """kernel32 with explicit argument and return types. Handles are pointer sized, so leaving the defaults (C `int`)
    would truncate them on 64-bit Windows. `use_last_error` makes `ctypes.get_last_error()` reliable."""
    if "kernel32" not in _API:
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetExitCodeProcess.restype = wintypes.BOOL
        api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        api.GetProcessTimes.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        _API["kernel32"] = api
    return _API["kernel32"]


def _valid_pid(pid) -> bool:
    return isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid <= 0xFFFFFFFF


def probe_pid(pid, api=None) -> str:
    """'alive', 'dead' or 'unknown' for a process id. Only 'dead' is proof; everything uncertain is 'unknown'.

    Windows: `OpenProcess` fails with ERROR_INVALID_PARAMETER when no such process exists (dead) and with
    ERROR_ACCESS_DENIED when it exists but is not ours to inspect (alive); any other failure, an invalid id, or an exit code
    that cannot be read is 'unknown'. Nothing here can terminate or signal a process: no `os.kill`.
    Elsewhere the `/proc` table is consulted when it exists."""
    if not _valid_pid(pid):
        return "unknown"  # a corrupt id proves nothing about the holder
    if api is None and os.name != "nt":
        if not os.path.isdir("/proc"):
            return "unknown"
        return "alive" if os.path.exists(f"/proc/{pid}") else "dead"
    import ctypes
    from ctypes import wintypes
    api = api or _windows_api()
    handle = api.OpenProcess(_WIN_QUERY_LIMITED, False, pid)
    if not handle:
        error = api.last_error() if hasattr(api, "last_error") else ctypes.get_last_error()
        return {_WIN_INVALID_PARAMETER: "dead", _WIN_ACCESS_DENIED: "alive"}.get(error, "unknown")
    try:
        code = wintypes.DWORD()
        if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
            return "unknown"
        return "alive" if code.value == _WIN_STILL_ACTIVE else "dead"
    finally:
        api.CloseHandle(handle)


def pid_is_alive(pid) -> bool:
    """False only when the process is proven gone. 'Unknown' and 'access denied' count as alive (fail safe: a lock is
    never taken from a holder that might still be running)."""
    return probe_pid(pid) != "dead"


def process_start_time(pid, api=None) -> dt.datetime | None:
    """When the process with this id was created (aware UTC), or None when that cannot be read. A process that was
    created AFTER a lock was acquired cannot be the lock's holder: its id was reused."""
    if not _valid_pid(pid) or (api is None and os.name != "nt"):
        return None
    import ctypes
    from ctypes import wintypes
    api = api or _windows_api()
    handle = api.OpenProcess(_WIN_QUERY_LIMITED, False, pid)
    if not handle:
        return None
    try:
        created, other = wintypes.FILETIME(), [wintypes.FILETIME() for _ in range(3)]
        if not api.GetProcessTimes(handle, ctypes.byref(created), *[ctypes.byref(o) for o in other]):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return dt.datetime.fromtimestamp((ticks - _WIN_EPOCH_100NS) / 1e7, dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    finally:
        api.CloseHandle(handle)

def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _row(row) -> SimpleNamespace | None:
    return None if row is None else SimpleNamespace(**dict(row))


class LeaseKeeper:
    """Renews leases in the background for one run. Lifecycle, synchronisation and shutdown:

      * ONE daemon thread per keeper, started by `start()` and joined by `stop()` (bounded wait); `keep_alive` calls `stop()`
        in a `finally`, so KeyboardInterrupt and SystemExit end it as well. Nothing else is started: no processes.
      * The thread opens its OWN ledger connection (SQLite connections are not shared between threads) as the same owner,
        and closes it itself. Renewals are single short `BEGIN IMMEDIATE` transactions; the main thread's transactions and
        these serialize through SQLite's own locking. The only shared Python state is two `threading.Event`s.
      * Each renewal verifies ownership AND the lease identity (`acquired_at`). A lease that is gone, taken by someone else
        or no longer ours sets `lost`; so does a thread that could not renew for three quarters of the lease. A lost lease is
        never recovered here: `check()` raises `lock_lost` and the run stops before its next stage or write.
      * If the process dies the thread dies with it, so a dead run's lease is never extended: it expires as before."""

    def __init__(self, ledger, names, *, ttl_seconds: float, interval_seconds: float):
        self._ledger, self._ttl, self._interval = ledger, float(ttl_seconds), float(interval_seconds)
        self._stop, self._lost = threading.Event(), threading.Event()
        self.lost_reason: str | None = None
        self._identity = {}
        for name in names:  # the leases must be ours right now; their identity is what is renewed
            lock = ledger.get_lock(name)
            if lock is None or lock.owner_id != ledger.owner_id:
                raise LockHeldError("lock_lost")
            self._identity[name] = lock.acquired_at
        self._thread = threading.Thread(target=self._run, name="ledger-lease-keeper", daemon=True)

    def start(self):
        self._thread.start()

    def check(self) -> None:
        """Raise `lock_lost` if any lease was lost (or the renewing thread stopped without being asked to)."""
        if not self._lost.is_set() and self._thread.is_alive() is False and not self._stop.is_set() \
                and self._thread.ident is not None:
            self._set_lost("renewal_stopped")
        if self._lost.is_set():
            raise LockHeldError("lock_lost")

    def _set_lost(self, reason: str) -> None:
        self.lost_reason = self.lost_reason or reason
        self._lost.set()

    def _run(self):
        led = self._ledger
        try:
            mine = Ledger(led.path, clock=led.clock, owner_id=led.owner_id, host=led.host, pid=led.pid,
                          pid_alive=led._pid_alive, pid_started=led._pid_started, busy_timeout=led._busy_timeout,
                          allow_cloud_sync=led._allow_cloud_sync, environ=led._environ, create=False)
        except Exception:
            self._set_lost("renewal_unavailable")
            return
        last_ok = time.monotonic()
        try:
            while not self._stop.wait(self._interval):
                for name, acquired in self._identity.items():
                    try:
                        mine.heartbeat(name, ttl_seconds=self._ttl, acquired_at=acquired)
                        last_ok = time.monotonic()
                    except LockHeldError:
                        self._set_lost("lease_lost")
                        return
                    except Exception:  # busy or a storage problem: try again, but not past the lease
                        if time.monotonic() - last_ok >= self._ttl * 0.75:
                            self._set_lost("renewal_failed")
                            return
        finally:
            mine.close()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(30.0, self._interval))


class Ledger:
    """One connection to the ledger database. Not shared between threads."""

    def __init__(self, path, *, clock, readonly: bool = False, create: bool = True, busy_timeout: float = 5.0,
                 pid_alive=pid_is_alive, owner_id: str | None = None, host: str | None = None, pid: int | None = None,
                 allow_cloud_sync: bool = False, environ=None, pid_started=None):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise LedgerError("ledger_path_not_absolute")
        from seo_report import runner_config  # storage-location rules live with the settings that name the paths
        try:  # a database on a network share or an unidentifiable drive is refused before any file access
            runner_config.require_local_database(self.path, "ledger")
        except runner_config.RunnerConfigError:
            raise LedgerError("ledger_path_not_local") from None
        if not allow_cloud_sync and in_cloud_sync_folder(self.path, environ):
            raise LedgerError("ledger_in_cloud_sync")
        self.clock, self.readonly, self._pid_alive = clock, readonly, pid_alive
        # the creation-time check for reused process ids only makes sense with the real liveness probe
        self._pid_started = pid_started if pid_started is not None else (
            process_start_time if pid_alive is pid_is_alive else None)
        self._allow_cloud_sync, self._environ, self._busy_timeout = allow_cloud_sync, environ, busy_timeout
        self._unusable = False
        self.owner_id = owner_id or str(uuid.uuid4())
        self.host = host or socket.gethostname()
        self.pid = os.getpid() if pid is None else pid
        exists = self.path.exists()
        if readonly and not exists:
            raise LedgerError("ledger_missing")
        if not exists and not create:
            raise LedgerError("ledger_missing")
        try:
            if readonly:
                self._con = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=busy_timeout,
                                            isolation_level=None)
                self._con.execute("PRAGMA query_only=ON")
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._con = sqlite3.connect(str(self.path), timeout=busy_timeout, isolation_level=None)
            self._con.row_factory = sqlite3.Row
            self._con.execute(f"PRAGMA busy_timeout={int(busy_timeout * 1000)}")
            self._con.execute("PRAGMA foreign_keys=ON")
            if not readonly:
                mode = self._con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                if str(mode).lower() != "delete":
                    raise LedgerError("ledger_journal_mode")
                self._con.execute("PRAGMA synchronous=FULL")
            self._open_schema()
        except LedgerError:
            self._con.close()  # a refused schema (or failed migration) must not leave the file open
            raise
        except sqlite3.Error as error:
            try:
                self._con.close()
            except (AttributeError, sqlite3.Error):
                pass
            code = classify_sqlite_error(error)
            raise LedgerError(code if code in STORAGE_CODES else "ledger_unavailable") from error

    # -- setup -------------------------------------------------------------------------------------------------------------

    def _open_schema(self):
        version = self._con.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in self._con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version == 0 and not tables:
            if self.readonly:
                raise LedgerError("ledger_not_initialized")
            with self._tx():
                for statement in _DDL:
                    self._con.execute(statement)
                self._con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        elif version == 1 and set(_TABLES) <= tables and not self.readonly:
            self._migrate_v1()
        elif version != SCHEMA_VERSION or not set(_TABLES) <= tables:
            raise LedgerError("ledger_schema_unsupported")

    def _migrate_v1(self):
        """Schema 1 -> 2 in one transaction: `requests` and `runs` are rebuilt (their CHECK constraints widen) and every
        existing row is copied unchanged as generation 1. Nothing is deleted from any other table."""
        self._con.execute("PRAGMA foreign_keys=OFF")  # cannot change inside a transaction; restored below
        try:
            with self._tx():
                if self._con.execute("PRAGMA user_version").fetchone()[0] != 1:
                    return  # another process migrated first
                requests = ", ".join(_REQUEST_COLUMNS)
                runs = ", ".join(_RUN_COLUMNS_V1)
                self._con.execute(_requests_ddl("requests_v2"))
                self._con.execute(_runs_ddl("runs_v2"))
                self._con.execute(f"INSERT INTO requests_v2({requests}) SELECT {requests} FROM requests")
                self._con.execute(f"INSERT INTO runs_v2({runs}) SELECT {runs} FROM runs")
                self._con.execute("DROP TABLE requests")
                self._con.execute("DROP TABLE runs")
                self._con.execute("ALTER TABLE requests_v2 RENAME TO requests")
                self._con.execute("ALTER TABLE runs_v2 RENAME TO runs")
                for statement in (*_REQUESTS_EXTRAS, *_RUNS_EXTRAS):
                    self._con.execute(statement)
                if self._con.execute("PRAGMA foreign_key_check").fetchall():
                    raise LedgerError("ledger_migration_failed")
                self._con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        finally:
            self._con.execute("PRAGMA foreign_keys=ON")

    def close(self):
        try:
            self._con.close()
        except sqlite3.Error:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- internals ---------------------------------------------------------------------------------------------------------

    def now(self) -> str:
        return contracts.timestamp_of(self.clock())

    @contextmanager
    def _tx(self):
        """One short write transaction (`BEGIN IMMEDIATE` ... `COMMIT`).

        Every way out leaves the connection outside a transaction, or marks it unusable:
          * `BEGIN` failing starts nothing; it is raised as a classified `LedgerError` (busy is transient).
          * An exception from the body (any `BaseException`, including KeyboardInterrupt) triggers a rollback and is then
            re-raised unchanged; a SQLite error from the body is re-raised as a classified `LedgerError` chained to it.
          * A failing `COMMIT` is rolled back too, then raised as a classified `LedgerError` chained to the SQLite error.
          * If the rollback itself fails, the original error is still the one raised (the failure is noted on it), the
            connection is closed so SQLite discards the open transaction, and the ledger refuses further writes
            (`ledger_connection_unusable`) until it is reopened.
        Nothing is retried here: a transaction that failed half-way is never repeated automatically.
        """
        if self.readonly:
            raise LedgerError("ledger_read_only")
        if self._unusable:
            raise LedgerError("ledger_connection_unusable")
        try:
            self._con.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            raise wrap_sqlite_error(error) from error
        try:
            yield
        except sqlite3.Error as error:
            self._rollback(error)
            raise wrap_sqlite_error(error) from error
        except BaseException as error:
            self._rollback(error)
            raise
        try:
            self._con.execute("COMMIT")
        except sqlite3.Error as error:
            self._rollback(error)
            raise wrap_sqlite_error(error) from error

    def _rollback(self, original: BaseException) -> None:
        """Roll back an open transaction; never raises, never replaces `original`."""
        try:
            if self._con.in_transaction:
                self._con.execute("ROLLBACK")
            if not self._con.in_transaction:
                return
        except sqlite3.Error:
            pass
        self._unusable = True  # the transaction state is unknown: closing the connection makes SQLite discard it
        try:
            self._con.close()
        except sqlite3.Error:
            pass
        try:
            original.add_note("ledger rollback failed; the connection was closed")
        except Exception:  # a note is a convenience only
            pass

    def _one(self, sql, args=()):
        try:
            return _row(self._con.execute(sql, args).fetchone())
        except sqlite3.Error as error:
            raise wrap_sqlite_error(error) from error

    def _all(self, sql, args=()):
        try:
            return [_row(r) for r in self._con.execute(sql, args).fetchall()]
        except sqlite3.Error as error:
            raise wrap_sqlite_error(error) from error

    def _event(self, *, kind, code, run_key=None, request_id=None, attempt_no=None, from_state=None, to_state=None,
               detail=None):
        contracts._pattern(code, contracts._CODE, "code_invalid", "event.code")  # fixed vocabulary only
        self._con.execute(
            "INSERT INTO events(at, run_key, request_id, attempt_no, kind, from_state, to_state, code, detail_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.now(), run_key, request_id, attempt_no, kind, from_state, to_state, code, _canonical(detail or {})))

    # -- requests ----------------------------------------------------------------------------------------------------------

    def register_request(self, request: contracts.RunRequest) -> SimpleNamespace:
        """Store a request once. Returns `kind` = 'new' or 'replay'; a replay carries the stored response (or None).

        A replay with a different client, period or action under the same idempotency key is refused
        (`idempotency_key_reused`): the key identifies one intended action.
        """
        with self._tx():
            existing = self._one("SELECT * FROM requests WHERE idempotency_key = ?", (request.idempotency_key,))
            if existing is not None:
                if (existing.client_key, existing.period_id, existing.action) != (
                        request.client_key, request.period_id, request.action):
                    raise LedgerError("idempotency_key_reused")
                response = json.loads(existing.response_json) if existing.response_json else None
                return SimpleNamespace(kind="replay", request_id=existing.request_id, status=existing.status,
                                       response=response)
            try:
                self._con.execute(
                    "INSERT INTO requests(request_id, idempotency_key, client_key, period_id, action, requested_by, "
                    "requested_at, received_at, options_json, status) VALUES (?,?,?,?,?,?,?,?,?,'accepted')",
                    (request.request_id, request.idempotency_key, request.client_key, request.period_id, request.action,
                     request.requested_by, request.requested_at, self.now(),
                     _canonical(request.to_dict()["options"] if "options" in request.to_dict()
                                else {"replace_existing": request.replace_existing})))
            except sqlite3.IntegrityError:
                raise LedgerError("request_id_conflict") from None
            self._event(kind="info", code="request_registered", request_id=request.request_id,
                        detail={"action": request.action})
            return SimpleNamespace(kind="new", request_id=request.request_id, status="accepted", response=None)

    def finish_request(self, request_id: str, *, status: str, response: dict, run_key: str | None = None,
                       reject_code: str | None = None) -> None:
        """Store the response returned for a request (returned verbatim on a replay)."""
        if status not in ("accepted", "rejected", "completed", "replayed_only"):
            raise LedgerError("request_status_invalid")
        with self._tx():
            changed = self._con.execute(
                "UPDATE requests SET status = ?, reject_code = ?, run_key = ?, response_json = ?, responded_at = ? "
                "WHERE request_id = ?", (status, reject_code, run_key, _canonical(response), self.now(), request_id)
            ).rowcount
            if changed != 1:
                raise LedgerError("request_missing")

    def get_request(self, request_id: str):
        return self._one("SELECT * FROM requests WHERE request_id = ?", (request_id,))

    def requests_by_key(self, idempotency_key: str):
        return self._one("SELECT * FROM requests WHERE idempotency_key = ?", (idempotency_key,))

    # -- runs --------------------------------------------------------------------------------------------------------------

    def get_run(self, run_key: str):
        return self._one("SELECT * FROM runs WHERE run_key = ?", (run_key,))

    def list_runs(self, states=None):
        if states is None:
            return self._all("SELECT * FROM runs ORDER BY run_key")
        marks = ",".join("?" * len(states))
        return self._all(f"SELECT * FROM runs WHERE state IN ({marks}) ORDER BY run_key", tuple(states))

    def runs_for_period(self, client_key: str, period_id: str):
        """Every generation of a (client, period), oldest first."""
        start, end = contracts.parse_period_id(period_id)
        return self._all("SELECT * FROM runs WHERE client_key = ? AND period_start = ? AND period_end = ? "
                         "ORDER BY generation", (client_key, start.isoformat(), end.isoformat()))

    def latest_run(self, client_key: str, period_id: str):
        """The highest generation of a (client, period), or None."""
        runs = self.runs_for_period(client_key, period_id)
        return runs[-1] if runs else None

    def create_run(self, client_key: str, period_id: str, *, replace_existing: bool = False,
                   request_id: str | None = None, supersedes: str | None = None, link_kind: str | None = None,
                   operator: str | None = None, reason: str | None = None, baseline: dict | None = None):
        """Create generation 1 of a period, or (with `supersedes`) the next generation linked to the latest one.

        A linked run has its own run key (`...~N`), idempotency scope, attempts and history; the run it follows is never
        touched. `supersedes` must name the current latest generation of the same period (otherwise `run_not_latest`).
        """
        start, end = contracts.parse_period_id(period_id)
        if (supersedes is None) != (link_kind is None) or link_kind not in (None, "supersede", "restart"):
            raise LedgerError("link_invalid")
        with self._tx():
            now = self.now()
            generation = 1
            if supersedes is not None:
                latest = self.latest_run(client_key, period_id)
                if latest is None or latest.run_key != supersedes:
                    raise LedgerError("run_not_latest")
                generation = latest.generation + 1
            run_key = contracts.run_key_of(client_key, period_id, generation)
            try:
                self._con.execute(
                    "INSERT INTO runs(run_key, client_key, period_start, period_end, generation, supersedes_run_key, "
                    "link_kind, link_operator, link_reason, state, replace_existing, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?, 'requested', ?, ?, ?)",
                    (run_key, client_key, start.isoformat(), end.isoformat(), generation, supersedes, link_kind,
                     operator, reason, int(replace_existing), now, now))
            except sqlite3.IntegrityError:
                raise LedgerError("run_exists") from None
            self._con.execute("INSERT INTO run_attempts(run_key, attempt_no, started_at, start_state, trigger_request) "
                              "VALUES (?,1,?,'requested',?)", (run_key, now, request_id))
            self._event(kind="state_change", code="run_created", run_key=run_key, request_id=request_id, attempt_no=1,
                        to_state="requested")
            if supersedes is not None:
                self._event(kind="operator", code=f"run_{link_kind}_created", run_key=run_key, request_id=request_id,
                            attempt_no=1, detail={"operator": operator, "reason": reason, "follows": supersedes,
                                                  **({"baseline": baseline} if baseline else {})})
                self._event(kind="operator", code=f"run_{link_kind}_requested", run_key=supersedes,
                            request_id=request_id, detail={"operator": operator, "reason": reason, "run": run_key})
            return self.get_run(run_key)

    def acknowledge(self, run_key: str, *, expected_state: str, expected_version: int, operator: str, reason: str,
                    request_id: str | None = None):
        """Record that an operator has reviewed a blocked run. Returns `(run, changed)`.

        Acknowledgement never moves the run: it stays `blocked` until a retry is admitted, and the retry re-checks the
        blocking condition. It is idempotent (a repeat is logged and changes nothing) and version-checked.
        """
        if not operator or reason not in contracts.REASON_CODES:
            raise LedgerError("operator_details_required")
        with self._tx():
            run = self.get_run(run_key)
            if run is None:
                raise LedgerError("run_missing")
            if run.state != "blocked":
                raise InvalidTransitionError("not_blocked")
            if run.acknowledged and run.state == expected_state:
                self._event(kind="operator", code="acknowledge_repeated", run_key=run_key, request_id=request_id,
                            attempt_no=run.attempt_count, detail={"operator": operator, "reason": reason})
                return run, False
            if run.state != expected_state or run.state_version != expected_version:
                raise StaleStateError("stale_state")
            now = self.now()
            self._con.execute(
                "UPDATE runs SET acknowledged = 1, ack_operator = ?, ack_reason = ?, ack_at = ?, "
                "state_version = state_version + 1, updated_at = ? WHERE run_key = ?",
                (operator, reason, now, now, run_key))
            self._event(kind="operator", code="run_acknowledged", run_key=run_key, request_id=request_id,
                        attempt_no=run.attempt_count, from_state="blocked", to_state="blocked",
                        detail={"operator": operator, "reason": reason, "error_category": run.error_category})
            return self.get_run(run_key), True

    def transition(self, run_key: str, to_state: str, *, expected_state: str, expected_version: int, code: str,
                   values: dict | None = None, detail: dict | None = None, request_id: str | None = None,
                   new_attempt: bool = False, outbox=None, event_kind: str | None = None):
        """Move a run to `to_state` atomically; returns the updated run.

        `values` sets columns from `SETTABLE`. `new_attempt` (only from failed/blocked) starts attempt N+1 and clears the
        failure columns. `outbox(run)` may return a status payload to enqueue in the same transaction.
        """
        values = dict(values or {})
        unknown = sorted(set(values) - SETTABLE)
        if unknown:
            raise LedgerError("column_not_settable")
        if to_state not in contracts.STATES:
            raise InvalidTransitionError("state_unknown")
        with self._tx():
            run = self.get_run(run_key)
            if run is None:
                raise LedgerError("run_missing")
            if run.state != expected_state or run.state_version != expected_version:
                raise StaleStateError("stale_state")
            if to_state not in contracts.TRANSITIONS[run.state]:
                raise InvalidTransitionError("transition_not_allowed")
            if new_attempt != (run.state in ("failed", "blocked") and to_state != "cancelled"):
                raise InvalidTransitionError("attempt_rule")
            now = self.now()
            recovery = run.state in contracts.IN_PROGRESS_STATES and to_state == contracts.RESUME_STATE.get(run.state)
            attempt_no = run.attempt_count + 1 if (new_attempt or recovery) else run.attempt_count
            if recovery:  # the interrupted attempt is closed; the resumed work is the next attempt
                self._con.execute(
                    "UPDATE run_attempts SET finished_at = ?, outcome = 'interrupted' "
                    "WHERE run_key = ? AND attempt_no = ? AND outcome IS NULL", (now, run_key, run.attempt_count))
                self._con.execute(
                    "INSERT INTO run_attempts(run_key, attempt_no, started_at, start_state, trigger_request) "
                    "VALUES (?,?,?,?,?)", (run_key, attempt_no, now, to_state, request_id))
            elif new_attempt:
                for column in _FAILURE_COLUMNS:
                    values.setdefault(column, None)
                values.setdefault("retry_eligible", 0)
                self._con.execute(
                    "INSERT INTO run_attempts(run_key, attempt_no, started_at, start_state, trigger_request) "
                    "VALUES (?,?,?,?,?)", (run_key, attempt_no, now, to_state, request_id))
            elif to_state in ("failed", "blocked", *_SUCCESS_STATES) or run.state in contracts.IN_PROGRESS_STATES \
                    and to_state == contracts.RESUME_STATE.get(run.state):
                outcome = ("blocked" if to_state == "blocked" else "failed" if to_state == "failed"
                           else "succeeded" if to_state in _SUCCESS_STATES else "interrupted")
                self._con.execute(
                    "UPDATE run_attempts SET finished_at = ?, outcome = ?, error_category = ? "
                    "WHERE run_key = ? AND attempt_no = ? AND outcome IS NULL",
                    (now, outcome, values.get("error_category"), run_key, attempt_no))
            elif to_state == "cancelled":  # an attempt still open when the run is cancelled ends as interrupted
                self._con.execute(
                    "UPDATE run_attempts SET finished_at = ?, outcome = 'interrupted' "
                    "WHERE run_key = ? AND attempt_no = ? AND outcome IS NULL", (now, run_key, run.attempt_count))
            finished = now if to_state in ("failed", "blocked", "delivered", "cancelled") else None
            columns = dict(values)
            # an acknowledgement belongs to the blocked state it was given for; any move ends it
            columns.update(state=to_state, state_version=run.state_version + 1, attempt_count=attempt_no,
                           updated_at=now, finished_at=finished, acknowledged=0)
            names = ", ".join(f"{name} = ?" for name in columns)
            try:
                self._con.execute(f"UPDATE runs SET {names} WHERE run_key = ?", (*columns.values(), run_key))
            except sqlite3.IntegrityError:
                raise LedgerError("value_rejected") from None
            self._event(kind=event_kind or ("recovery" if (run.state in contracts.IN_PROGRESS_STATES
                                                           and to_state == contracts.RESUME_STATE.get(run.state))
                                            else "state_change"),
                        code=code, run_key=run_key, request_id=request_id, attempt_no=attempt_no,
                        from_state=run.state, to_state=to_state, detail=detail)
            updated = self.get_run(run_key)
            if outbox is not None:
                payload = outbox(updated)
                if payload is not None:
                    self._enqueue(destination="make_status", payload=payload, run_key=run_key, request_id=request_id,
                                  dedupe_key=f"{run_key}#{updated.state_version}#make_status")
            return updated

    def append_event(self, *, kind: str, code: str, run_key: str | None = None, request_id: str | None = None,
                     detail: dict | None = None) -> None:
        with self._tx():
            run = self.get_run(run_key) if run_key else None
            self._event(kind=kind, code=code, run_key=run_key, request_id=request_id,
                        attempt_no=None if run is None else run.attempt_count, detail=detail)

    def events_for(self, run_key: str | None = None, request_id: str | None = None):
        if run_key is not None:
            return self._all("SELECT * FROM events WHERE run_key = ? ORDER BY event_id", (run_key,))
        if request_id is not None:
            return self._all("SELECT * FROM events WHERE request_id = ? ORDER BY event_id", (request_id,))
        return self._all("SELECT * FROM events ORDER BY event_id")

    def attempts_for(self, run_key: str):
        return self._all("SELECT * FROM run_attempts WHERE run_key = ? ORDER BY attempt_no", (run_key,))

    # -- locks -------------------------------------------------------------------------------------------------------------

    def _stale(self, lock) -> bool:
        """Expired lease AND a holder that is gone (another host's lock is never judged stale by pid)."""
        if lock.expires_at >= self.now():
            return False
        if lock.host != self.host:
            return False  # another machine's process cannot be judged from here
        if not self._pid_alive(lock.pid):
            return True
        return self._pid_was_reused(lock)

    def _pid_was_reused(self, lock) -> bool:
        """A live process whose creation is later than the lock's acquisition is not the holder: the id was reused."""
        if self._pid_started is None:
            return False
        try:
            started = self._pid_started(lock.pid)
            acquired = dt.datetime.fromisoformat(lock.acquired_at)
        except (ValueError, TypeError, OSError):
            return False
        return started is not None and started > acquired + dt.timedelta(seconds=5)  # margin for clock differences

    def acquire_lock(self, name: str, *, ttl_seconds: float):
        if not isinstance(name, str) or not name.startswith(_LOCK_NAME_PREFIXES) or len(name) > 100:
            raise LedgerError("lock_name_invalid")
        with self._tx():
            now = self.clock()
            expires = contracts.timestamp_of(now + dt.timedelta(seconds=ttl_seconds))
            stamp = contracts.timestamp_of(now)
            lock = self._one("SELECT * FROM locks WHERE lock_name = ?", (name,))
            if lock is None:
                self._con.execute("INSERT INTO locks VALUES (?,?,?,?,?,?,?)",
                                  (name, self.owner_id, self.pid, self.host, stamp, stamp, expires))
            elif lock.owner_id == self.owner_id:
                self._con.execute("UPDATE locks SET heartbeat_at = ?, expires_at = ? WHERE lock_name = ?",
                                  (stamp, expires, name))
            elif self._stale(lock):
                self._con.execute(
                    "UPDATE locks SET owner_id=?, pid=?, host=?, acquired_at=?, heartbeat_at=?, expires_at=? "
                    "WHERE lock_name = ?", (self.owner_id, self.pid, self.host, stamp, stamp, expires, name))
                self._event(kind="lock", code="lock_takeover", detail={"lock": name})
            else:
                raise LockHeldError("lock_held")
            acquired = self._one("SELECT acquired_at FROM locks WHERE lock_name = ?", (name,)).acquired_at
        return SimpleNamespace(name=name, owner_id=self.owner_id, acquired_at=acquired)

    def heartbeat(self, name: str, *, ttl_seconds: float, acquired_at: str | None = None) -> None:
        """Renew a lease this owner holds. With `acquired_at` (the identity of the lease as acquired) a lock that was lost
        and taken again is not renewed. A lease that is no longer ours raises `lock_lost`; nothing is ever extended for
        someone else."""
        with self._tx():
            now = self.clock()
            sql = "UPDATE locks SET heartbeat_at = ?, expires_at = ? WHERE lock_name = ? AND owner_id = ?"
            args = [contracts.timestamp_of(now), contracts.timestamp_of(now + dt.timedelta(seconds=ttl_seconds)), name,
                    self.owner_id]
            if acquired_at is not None:
                sql += " AND acquired_at = ?"
                args.append(acquired_at)
            if self._con.execute(sql, args).rowcount != 1:
                raise LockHeldError("lock_lost")

    @contextmanager
    def keep_alive(self, names, *, ttl_seconds: float, interval_seconds: float):
        """Renew the named leases (all held by this owner) every `interval_seconds` while the block runs, so that a stage
        longer than the lease keeps it. See `LeaseKeeper`; `check()` on the yielded keeper raises `lock_lost` once any
        lease was lost. The renewing thread is ended and joined when the block ends, however it ends."""
        keeper = LeaseKeeper(self, tuple(names), ttl_seconds=ttl_seconds, interval_seconds=interval_seconds)
        keeper.start()
        try:
            yield keeper
        finally:
            keeper.stop()

    def release_lock(self, name: str) -> None:
        with self._tx():
            self._con.execute("DELETE FROM locks WHERE lock_name = ? AND owner_id = ?", (name, self.owner_id))

    @contextmanager
    def lock(self, name: str, *, ttl_seconds: float):
        self.acquire_lock(name, ttl_seconds=ttl_seconds)
        try:
            yield
        finally:
            try:
                self.release_lock(name)
            except LedgerError:
                pass

    def get_lock(self, name: str):
        return self._one("SELECT * FROM locks WHERE lock_name = ?", (name,))

    # -- recovery ----------------------------------------------------------------------------------------------------------

    def recover_interrupted(self):
        """Move runs left in an in-progress state by a dead process back to their resume state.

        Skipped: runs whose client lock is held by this process or by a live/unexpired owner. Returns
        `[(run_key, from_state, to_state)]`.
        """
        recovered = []
        marks = ",".join("?" * len(contracts.IN_PROGRESS_STATES))
        for run in self._all(f"SELECT * FROM runs WHERE state IN ({marks}) ORDER BY run_key",
                             contracts.IN_PROGRESS_STATES):
            lock = self.get_lock(f"client:{run.client_key}")
            if lock is not None and lock.owner_id != self.owner_id and not self._stale(lock):
                continue
            if lock is not None and lock.owner_id == self.owner_id:
                continue
            if lock is not None:
                with self._tx():
                    self._con.execute("DELETE FROM locks WHERE lock_name = ? AND owner_id = ?",
                                      (lock.lock_name, lock.owner_id))
                    self._event(kind="lock", code="stale_lock_cleared", detail={"lock": lock.lock_name})
            target = contracts.RESUME_STATE[run.state]
            try:
                self.transition(run.run_key, target, expected_state=run.state, expected_version=run.state_version,
                                code="interrupted_run_recovered", detail={"from": run.state})
            except StaleStateError:
                continue
            recovered.append((run.run_key, run.state, target))
        return recovered

    # -- outbox ------------------------------------------------------------------------------------------------------------

    def _enqueue(self, *, destination, payload, dedupe_key, run_key=None, request_id=None, next_attempt_at=None):
        self._con.execute(
            "INSERT OR IGNORE INTO outbox(created_at, run_key, request_id, destination, dedupe_key, payload_json, "
            "next_attempt_at) VALUES (?,?,?,?,?,?,?)",
            (self.now(), run_key, request_id, destination, dedupe_key, _canonical(payload),
             next_attempt_at or self.now()))

    def enqueue_outbox(self, *, destination: str, payload: dict, dedupe_key: str, run_key: str | None = None,
                       request_id: str | None = None) -> None:
        with self._tx():
            self._enqueue(destination=destination, payload=payload, dedupe_key=dedupe_key, run_key=run_key,
                          request_id=request_id)

    def pending_outbox(self, limit: int = 100):
        return self._all("SELECT * FROM outbox WHERE delivered_at IS NULL AND next_attempt_at <= ? "
                         "ORDER BY outbox_id LIMIT ?", (self.now(), limit))

    def all_outbox(self):
        return self._all("SELECT * FROM outbox ORDER BY outbox_id")

    def mark_outbox_delivered(self, outbox_id: int) -> None:
        with self._tx():
            self._con.execute("UPDATE outbox SET delivered_at = ?, attempts = attempts + 1 WHERE outbox_id = ?",
                              (self.now(), outbox_id))

    def mark_outbox_failed(self, outbox_id: int, *, category: str, retry_in_seconds: float) -> None:
        with self._tx():
            when = contracts.timestamp_of(self.clock() + dt.timedelta(seconds=retry_in_seconds))
            self._con.execute("UPDATE outbox SET attempts = attempts + 1, last_error_category = ?, next_attempt_at = ? "
                              "WHERE outbox_id = ?", (category, when, outbox_id))

    # -- deliveries (runner-owned columns; the mirror columns are filled from the Delivery tab in a later stage) ----------------

    def record_delivery(self, *, delivery_key, run_key, pdf_sha256, pdf_bytes, manifest_sha256, drive_folder_id,
                        drive_file_id, file_name, uploaded_at, recipient_set_hash16=None, upload_confirmed=True):
        """Insert a delivery once. The same key again with identical content is a no-op; different content is refused."""
        with self._tx():
            existing = self._one("SELECT * FROM deliveries WHERE delivery_key = ?", (delivery_key,))
            wanted = (run_key, pdf_sha256, pdf_bytes, manifest_sha256, drive_folder_id, drive_file_id, file_name)
            if existing is not None:
                have = (existing.run_key, existing.pdf_sha256, existing.pdf_bytes, existing.manifest_sha256,
                        existing.drive_folder_id, existing.drive_file_id, existing.file_name)
                if have != wanted:
                    raise LedgerError("delivery_conflict")
                return existing
            try:
                self._con.execute(
                    "INSERT INTO deliveries(delivery_key, run_key, pdf_sha256, pdf_bytes, manifest_sha256, "
                    "drive_folder_id, drive_file_id, file_name, uploaded_at, upload_confirmed, recipient_set_hash16) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (delivery_key, run_key, pdf_sha256, pdf_bytes, manifest_sha256, drive_folder_id, drive_file_id,
                     file_name, uploaded_at, int(upload_confirmed), recipient_set_hash16))
            except sqlite3.IntegrityError:
                raise LedgerError("delivery_conflict") from None
            return self._one("SELECT * FROM deliveries WHERE delivery_key = ?", (delivery_key,))

    def get_delivery(self, delivery_key: str):
        return self._one("SELECT * FROM deliveries WHERE delivery_key = ?", (delivery_key,))

    def integrity_check(self) -> bool:
        try:
            return self._con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except sqlite3.Error as error:
            raise wrap_sqlite_error(error) from error
