"""Append-only SQLite store for report snapshots (standard library only).

Rules the store enforces:
  * Every table has triggers that abort UPDATE and DELETE, so history cannot be edited or removed
    through normal use. `recursive_triggers` is on, so INSERT OR REPLACE cannot bypass them either.
    These triggers guard against accidents; anyone with file access can still drop them or rewrite
    the whole file.
  * A run and all its rows are written in one short `BEGIN IMMEDIATE` transaction: all or nothing.
  * Runs are linked by a hash chain (see `snapshot.chain_hash`). `verify()` recomputes content
    hashes, the chain, the queryable rows and the triggers. It detects accidental damage and edits to
    individual runs. It does NOT protect against someone who can rewrite the entire database and
    recompute every hash and chain link.
  * Re-running the same client and period either returns the existing run (identical content) or
    adds a new revision (changed content) and records a `superseded` event on the older one.
    Status changes and corrections are new rows in `run_events`, never edits.

The database lives outside OneDrive by default (%LOCALAPPDATA%\\JasperSEOReporting\\history\\) because
cloud sync can corrupt SQLite files. It uses the rollback journal (`DELETE`) and a busy timeout.
"""

import datetime as dt
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from seo_report.snapshots import snapshot as snap

SCHEMA_VERSION = 1
ENV_DB_PATH = "JASPER_SEO_HISTORY_DB"
PUBLIC_EVENT_KINDS = ("rendered", "reviewed", "delivered", "voided")
ALL_EVENT_KINDS = PUBLIC_EVENT_KINDS + ("superseded",)
DUPLICATE_MODES = ("return_existing", "error")


class SnapshotStoreError(RuntimeError):
    """The store could not complete an operation."""


class DuplicateSnapshotError(SnapshotStoreError):
    """The run's content is identical to the current revision for that client and period."""


# --- event details ---------------------------------------------------------------------------------------------
#
# Free text written with `add_event` is masked by `mask_event_detail`, not by the general snapshot sanitizer
# (`snap.mask_text`), whose broad phone and token rules corrupt run IDs, dates and hashes. Event details are
# identifiers and dates far more often than personal data, so the rules here are deliberately narrow.

_EVENT_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_EVENT_SECRETS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----|-----BEGIN [A-Z ]*PRIVATE KEY-----"),
     "[redacted]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]{8,}"), "[redacted]"),  # OAuth access tokens
    (re.compile(r"(?<![\w/])1//[A-Za-z0-9._-]{8,}"), "[redacted]"),  # OAuth refresh tokens
    (re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{8,}"), "[redacted]"),  # OAuth client secrets
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "[redacted]"),  # API keys
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), "[redacted]"),  # JSON web tokens
    (re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)\b((?:client[_ -]?secret|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|secret|token|"
                r"password|passwd)\s*[:=]\s*)\S+"), r"\1[redacted]"),
)
# Values that must survive unchanged. They are set aside before the phone and token rules run.
_EVENT_PROTECTED = re.compile("|".join((
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",  # UUID run IDs
    r"\b[0-9a-fA-F]{64}\b",  # SHA-256 hashes
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b",  # ISO dates and times
    r"\brun-\d{1,12}\b",  # short run IDs
)))
# Phone numbers are recognised by their formatting; plain digit strings are never treated as phone numbers.
_EVENT_PHONES = (
    re.compile(r"(?<![\d.])(?:\+?\d{1,3}[\s.-]?)?\(\d{3}\)[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"),  # (281) 555-0100
    re.compile(r"(?<![\w.+])(?:\+?\d{1,3}[\s.-])?\d{3}[\s.-]\d{3}[\s.-]\d{4}(?!\d)"),  # 281-555-0100, +1 281 555 0100
)
_EVENT_INTERNATIONAL = re.compile(r"(?<![\w.])\+\d[\d\s().-]{7,}\d")  # +44 20 7946 0958 (at least 9 digits)
_EVENT_LABELLED_PHONE = re.compile(
    r"(?i)(\b(?:phone|tel|telephone|mobile|cell|fax|call)\b[:\s#-]*)\+?\d[\d\s().-]{8,}\d")  # "call 2815550100"
_EVENT_TOKEN = re.compile(r"(?<![\w-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}(?![\w-])")


def mask_event_detail(value) -> str | None:
    """Mask the free text of an event detail while keeping run IDs, hashes and dates intact.

    Whitespace is collapsed. Email addresses, formatted phone numbers (parenthesised, separated 3-3-4,
    international with a leading +, or introduced by a word such as "call" or "phone") and known secret
    patterns (OAuth tokens and client secrets, API keys, bearer tokens, web tokens, private keys, and
    values after labels such as "password:") are replaced. Opaque strings of 32 or more letters and digits
    are also replaced. UUIDs, SHA-256 hashes, ISO dates and date-times and short run IDs (run-001) are
    preserved exactly. Unformatted digit strings are left alone, since they are far more often IDs or
    counts than phone numbers; other hash lengths (for example 40 hex characters) are treated as opaque
    tokens and replaced.
    """
    if value is None:
        return None
    text = " ".join(str(value).replace("\x00", "").split())
    text = _EVENT_EMAIL.sub("[email]", text)
    for pattern, replacement in _EVENT_SECRETS:
        text = pattern.sub(replacement, text)

    kept = []

    def shield(match):
        kept.append(match.group())
        return f"\x00{len(kept) - 1}\x00"

    text = _EVENT_PROTECTED.sub(shield, text)
    for pattern in _EVENT_PHONES:
        text = pattern.sub("[phone]", text)
    text = _EVENT_INTERNATIONAL.sub(
        lambda m: "[phone]" if sum(c.isdigit() for c in m.group()) >= 9 else m.group(), text)
    text = _EVENT_LABELLED_PHONE.sub(r"\1[phone]", text)
    text = _EVENT_TOKEN.sub("[redacted]", text)
    for index, original in enumerate(kept):
        text = text.replace(f"\x00{index}\x00", original)
    return text


# --- schema -------------------------------------------------------------------------------------------------

_KINDS = "('position','above_10','missing')"
_WHICH = "TEXT NOT NULL CHECK (\"which\" IN ('current','previous'))"
_FLAG = "INTEGER NOT NULL CHECK ({c} IN (0,1))"
_METRICS = ("sessions INTEGER", "total_users INTEGER", "engaged_sessions INTEGER", "engagement_rate REAL",
            "key_events INTEGER")
_GSC_METRICS = ("clicks INTEGER", "impressions INTEGER", "ctr REAL", "position REAL")


def _flag(name: str) -> str:
    return f"{name} " + _FLAG.format(c=f'"{name}"')


_SPECS = {
    "run_sections": ("ordinal INTEGER NOT NULL", "key TEXT NOT NULL", "title TEXT NOT NULL",
                     "status TEXT NOT NULL CHECK (\"status\" IN ('ok','unavailable'))", _flag("has_strength"),
                     "paragraphs_json TEXT NOT NULL", "omitted_json TEXT NOT NULL"),
    "run_keywords": ("ordinal INTEGER NOT NULL", "keyword TEXT NOT NULL", "target_location TEXT",
                     f"prev_kind TEXT NOT NULL CHECK (\"prev_kind\" IN {_KINDS})",
                     "prev_position INTEGER CHECK (\"prev_position\" IS NULL OR \"prev_position\" >= 1)",
                     "prev_label TEXT",
                     f"cur_kind TEXT NOT NULL CHECK (\"cur_kind\" IN {_KINDS})",
                     "cur_position INTEGER CHECK (\"cur_position\" IS NULL OR \"cur_position\" >= 1)",
                     "cur_label TEXT"),
    "gsc_periods": (f"which {_WHICH}",
                    "status TEXT NOT NULL CHECK (\"status\" IN ('ok','empty','incomplete','truncated'))",
                    *_GSC_METRICS, "latest_data_date TEXT", "source_truncated_json TEXT NOT NULL",
                    "unsafe_queries_dropped INTEGER NOT NULL", "unsafe_pages_dropped INTEGER NOT NULL"),
    "gsc_daily": (f"which {_WHICH}", "date TEXT NOT NULL", _flag("has_data"), *_GSC_METRICS),
    "gsc_countries": (f"which {_WHICH}", "code TEXT NOT NULL", "name TEXT NOT NULL", *_GSC_METRICS),
    "gsc_queries": (f"which {_WHICH}", "label TEXT NOT NULL", *_GSC_METRICS),
    "gsc_pages": (f"which {_WHICH}", "path TEXT NOT NULL", *_GSC_METRICS),
    "ga4_periods": (f"which {_WHICH}", "scope TEXT NOT NULL CHECK (\"scope\" IN ('site','organic'))",
                    "status TEXT NOT NULL CHECK (\"status\" IN ('ok','empty','incomplete','truncated'))",
                    "time_zone TEXT", *_METRICS),
    "ga4_channels": (f"which {_WHICH}", "channel TEXT NOT NULL", *_METRICS),
    "ga4_daily": (f"which {_WHICH}", "date TEXT NOT NULL", _flag("has_data"), "sessions INTEGER",
                  "organic_sessions INTEGER"),
    "ga4_landing_pages": (f"which {_WHICH}", "scope TEXT NOT NULL CHECK (\"scope\" IN ('site','organic'))",
                          "path TEXT NOT NULL", _flag("is_placeholder"), *_METRICS),
    "run_insights": ("section TEXT NOT NULL", "ordinal INTEGER NOT NULL", "key TEXT NOT NULL",
                     "tone TEXT NOT NULL CHECK (\"tone\" IN ('positive','neutral','opportunity'))",
                     "priority INTEGER NOT NULL", "text TEXT NOT NULL", "evidence_json TEXT NOT NULL",
                     "tags_json TEXT NOT NULL"),
    "run_recommendations": ("ordinal INTEGER NOT NULL", "key TEXT NOT NULL",
                            "kind TEXT NOT NULL CHECK (\"kind\" IN ('evidence','standing'))",
                            "priority INTEGER NOT NULL", "title TEXT NOT NULL", "action TEXT NOT NULL",
                            "trigger TEXT NOT NULL", "evidence_json TEXT NOT NULL",
                            "supporting_json TEXT NOT NULL"),
    "run_notes": ("ordinal INTEGER NOT NULL", "severity TEXT NOT NULL CHECK (\"severity\" IN ('info','warning'))",
                  "source TEXT NOT NULL", "category TEXT NOT NULL", "message TEXT NOT NULL"),
    "run_diagnostics": ("source TEXT NOT NULL", f"which {_WHICH}", "name TEXT NOT NULL", "value INTEGER NOT NULL"),
    "run_warnings": ("source TEXT NOT NULL", f"which {_WHICH}", "code TEXT NOT NULL", "message TEXT", "detail TEXT"),
}

_RUN_FIELDS = ("seq", "run_id", *snap.RUN_COLUMNS, "revision", "content_hash", "prev_chain_hash", "chain_hash",
               "created_at", "payload_json")

_INSERT_RUN = ('INSERT INTO "runs" (' + ", ".join(f'"{c}"' for c in _RUN_FIELDS) + ") VALUES ("
               + ", ".join("?" for _ in _RUN_FIELDS) + ")")

_RUNS_DDL = """CREATE TABLE IF NOT EXISTS "runs" (
    "seq" INTEGER NOT NULL UNIQUE,
    "run_id" TEXT PRIMARY KEY,
    "client_key" TEXT NOT NULL,
    "client_name" TEXT, "website" TEXT, "gsc_property" TEXT, "ga4_property_id" TEXT, "status_at_run" TEXT,
    "target_country" TEXT, "target_city" TEXT,
    "period_start" TEXT NOT NULL, "period_end" TEXT NOT NULL, "prev_start" TEXT NOT NULL, "prev_end" TEXT NOT NULL,
    "format_version" INTEGER NOT NULL, "engine_version" TEXT NOT NULL,
    "quality" TEXT NOT NULL CHECK ("quality" IN ('complete','partial','incomplete')),
    "eligible_rankings" INTEGER NOT NULL CHECK ("eligible_rankings" IN (0,1)),
    "eligible_gsc" INTEGER NOT NULL CHECK ("eligible_gsc" IN (0,1)),
    "eligible_ga4" INTEGER NOT NULL CHECK ("eligible_ga4" IN (0,1)),
    "revision" INTEGER NOT NULL CHECK ("revision" >= 1),
    "content_hash" TEXT NOT NULL CHECK (length("content_hash") = 64),
    "prev_chain_hash" TEXT NOT NULL, "chain_hash" TEXT NOT NULL CHECK (length("chain_hash") = 64),
    "created_at" TEXT NOT NULL, "payload_json" TEXT NOT NULL,
    UNIQUE ("client_key", "period_start", "period_end", "revision"))"""

_EVENTS_DDL = f"""CREATE TABLE IF NOT EXISTS "run_events" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT,
    "run_id" TEXT NOT NULL REFERENCES "runs"("run_id"),
    "kind" TEXT NOT NULL CHECK ("kind" IN {ALL_EVENT_KINDS}),
    "detail" TEXT, "created_at" TEXT NOT NULL)"""

_META_DDL = 'CREATE TABLE IF NOT EXISTS "meta" ("key" TEXT PRIMARY KEY, "value" TEXT NOT NULL)'

ALL_TABLES = ("meta", "runs", "run_events", *_SPECS)
TRIGGER_NAMES = tuple(f"{t}_no_{op}" for t in ALL_TABLES for op in ("update", "delete"))


def _column_names(spec) -> tuple[str, ...]:
    return tuple(decl.split()[0] for decl in spec)


def _schema_statements() -> list[str]:
    statements = [_META_DDL, _RUNS_DDL, _EVENTS_DDL,
                  'CREATE INDEX IF NOT EXISTS "runs_by_period" ON "runs"("client_key", "period_start", "period_end")',
                  'CREATE INDEX IF NOT EXISTS "events_by_run" ON "run_events"("run_id")']
    for name, spec in _SPECS.items():
        if _column_names(spec) != snap.PROJECTION_COLUMNS[name]:
            raise AssertionError(f"schema for {name} does not match the snapshot projection columns")
        columns = ", ".join(f'"{decl.split()[0]}" {decl.split(maxsplit=1)[1]}' for decl in spec)
        statements.append(f'CREATE TABLE IF NOT EXISTS "{name}" ("id" INTEGER PRIMARY KEY AUTOINCREMENT, '
                          f'"run_id" TEXT NOT NULL REFERENCES "runs"("run_id"), {columns})')
        statements.append(f'CREATE INDEX IF NOT EXISTS "{name}_by_run" ON "{name}"("run_id")')
    for table in ALL_TABLES:
        for op in ("update", "delete"):
            statements.append(
                f'CREATE TRIGGER IF NOT EXISTS "{table}_no_{op}" BEFORE {op.upper()} ON "{table}" '
                f"BEGIN SELECT RAISE(ABORT, 'snapshot records are append-only'); END")
    return statements


# --- paths and connections ------------------------------------------------------------------------------------

def resolve_db_path(path=None, environ=None) -> Path:
    """Explicit path, else $JASPER_SEO_HISTORY_DB, else %LOCALAPPDATA%\\JasperSEOReporting\\history\\."""
    environ = os.environ if environ is None else environ
    if path:
        return Path(path)
    if environ.get(ENV_DB_PATH):
        return Path(environ[ENV_DB_PATH])
    base = environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "JasperSEOReporting" / "history" / "seo_reports.sqlite"


def in_cloud_sync_folder(path, environ=None) -> bool:
    """True when the path is inside a OneDrive folder (cloud sync can corrupt SQLite files)."""
    environ = os.environ if environ is None else environ
    resolved = Path(path).resolve()
    if any(part.casefold().startswith("onedrive") for part in resolved.parts):
        return True
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = environ.get(name)
        if root and resolved.is_relative_to(Path(root).resolve()):
            return True
    return False


def _connect(path: Path, *, readonly: bool, busy_timeout: float) -> sqlite3.Connection:
    try:
        if readonly:
            con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=busy_timeout,
                                  isolation_level=None)
        else:
            con = sqlite3.connect(str(path), timeout=busy_timeout, isolation_level=None)
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA recursive_triggers=ON")
        con.execute(f"PRAGMA busy_timeout={int(busy_timeout * 1000)}")
        if not readonly:
            mode = con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(mode).lower() != "delete":
                raise SnapshotStoreError(f"could not use the rollback journal (journal_mode={mode})")
            con.execute("PRAGMA synchronous=FULL")
        return con
    except sqlite3.Error as err:
        raise SnapshotStoreError(f"could not open the snapshot database: {err}") from None


# --- result types -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class WriteResult:
    run_id: str
    revision: int
    created: bool  # False when an identical run already existed
    duplicate_of: str | None  # the existing run's ID when created is False
    superseded: str | None  # the earlier revision this run replaced as the latest
    notes: tuple[str, ...]  # e.g. overlapping periods, for the reviewer


@dataclass(frozen=True)
class StoredEvent:
    kind: str
    detail: str | None
    created_at: str


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    seq: int
    revision: int
    client_key: str
    period_start: str
    period_end: str
    created_at: str
    quality: str
    content_hash: str
    voided: bool


@dataclass(frozen=True)
class StoredRun:
    run_id: str
    seq: int
    revision: int
    client_key: str
    period_start: str
    period_end: str
    created_at: str
    quality: str
    content_hash: str
    prev_chain_hash: str
    chain_hash: str
    eligibility: dict  # {"rankings": bool, "gsc": bool, "ga4": bool}: usable as a historical baseline
    payload: dict
    events: tuple[StoredEvent, ...]
    voided: bool


@dataclass(frozen=True)
class VerificationReport:
    ok: bool
    runs_checked: int
    problems: tuple[str, ...]


# --- reading ------------------------------------------------------------------------------------------------------

class _ReadMixin:
    _con: sqlite3.Connection

    def _fetch(self, sql: str, params=()) -> list[tuple]:
        try:
            return self._con.execute(sql, params).fetchall()
        except sqlite3.Error as err:
            raise SnapshotStoreError(f"snapshot database error: {err}") from None

    def _run_row(self, run_id: str) -> dict | None:
        rows = self._fetch(f"SELECT {', '.join(_RUN_FIELDS)} FROM runs WHERE run_id = ?", (run_id,))
        return dict(zip(_RUN_FIELDS, rows[0])) if rows else None

    def _events(self, run_id: str) -> tuple[StoredEvent, ...]:
        return tuple(StoredEvent(*r) for r in self._fetch(
            "SELECT kind, detail, created_at FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)))

    def _is_voided(self, run_id: str) -> bool:
        return bool(self._fetch("SELECT 1 FROM run_events WHERE run_id = ? AND kind = 'voided'", (run_id,)))

    def get(self, run_id: str) -> StoredRun:
        row = self._run_row(run_id)
        if row is None:
            raise SnapshotStoreError(f"no snapshot with run ID {run_id!r}")
        events = self._events(run_id)
        return StoredRun(
            run_id=row["run_id"], seq=row["seq"], revision=row["revision"], client_key=row["client_key"],
            period_start=row["period_start"], period_end=row["period_end"], created_at=row["created_at"],
            quality=row["quality"], content_hash=row["content_hash"], prev_chain_hash=row["prev_chain_hash"],
            chain_hash=row["chain_hash"],
            eligibility={"rankings": bool(row["eligible_rankings"]), "gsc": bool(row["eligible_gsc"]),
                         "ga4": bool(row["eligible_ga4"])},
            payload=json.loads(row["payload_json"]), events=events, voided=any(e.kind == "voided" for e in events))

    def latest(self, client_key: str, period_start: str, period_end: str) -> StoredRun | None:
        """The highest non-voided revision for this client and period, or None."""
        rows = self._fetch(
            'SELECT run_id FROM runs r WHERE client_key = ? AND period_start = ? AND period_end = ? '
            "AND NOT EXISTS (SELECT 1 FROM run_events e WHERE e.run_id = r.run_id AND e.kind = 'voided') "
            "ORDER BY revision DESC LIMIT 1", (client_key, period_start, period_end))
        return self.get(rows[0][0]) if rows else None

    def list_runs(self, client_key: str | None = None, *, include_voided: bool = False) -> list[RunInfo]:
        where, params = ("WHERE client_key = ?", (client_key,)) if client_key else ("", ())
        infos = []
        for r in self._fetch("SELECT run_id, seq, revision, client_key, period_start, period_end, created_at, "
                             f"quality, content_hash FROM runs {where} ORDER BY seq", params):
            voided = self._is_voided(r[0])
            if include_voided or not voided:
                infos.append(RunInfo(*r, voided=voided))
        return infos

    def events(self, run_id: str) -> tuple[StoredEvent, ...]:
        return self._events(run_id)

    def _check_run(self, row: dict) -> list[str]:
        run_id, problems = row["run_id"], []
        text = row["payload_json"]
        try:
            parsed = json.loads(text)
        except ValueError:
            return [f"run {run_id}: the stored payload is not valid JSON"]
        if snap.canonical_json(parsed) != text:
            problems.append(f"run {run_id}: the stored payload is not in canonical form")
        if snap.sha256_hex(text) != row["content_hash"]:
            problems.append(f"run {run_id}: the payload does not match its content hash")
        if snap.run_columns(parsed) != tuple(row[c] for c in snap.RUN_COLUMNS):
            problems.append(f"run {run_id}: the descriptive columns differ from the payload")
        for table, expected in snap.projection_rows(parsed).items():
            columns = ", ".join(f'"{c}"' for c in snap.PROJECTION_COLUMNS[table])
            actual = [tuple(r) for r in self._fetch(
                f'SELECT {columns} FROM "{table}" WHERE run_id = ? ORDER BY id', (run_id,))]
            if actual != [tuple(e) for e in expected]:
                problems.append(f"run {run_id}: rows in {table} differ from the payload")
        expected_chain = snap.chain_hash(
            row["prev_chain_hash"], seq=row["seq"], run_id=run_id, client_key=row["client_key"],
            period_start=row["period_start"], period_end=row["period_end"], revision=row["revision"],
            content_hash=row["content_hash"], created_at=row["created_at"])
        if expected_chain != row["chain_hash"]:
            problems.append(f"run {run_id}: the chain hash does not match its fields")
        return problems

    def verify_run(self, run_id: str) -> VerificationReport:
        row = self._run_row(run_id)
        if row is None:
            raise SnapshotStoreError(f"no snapshot with run ID {run_id!r}")
        problems = self._check_run(row)
        previous = self._fetch("SELECT chain_hash FROM runs WHERE seq = ?", (row["seq"] - 1,))
        expected_prev = previous[0][0] if previous else snap.GENESIS
        if row["prev_chain_hash"] != expected_prev:
            problems.append(f"run {run_id}: the chain does not link to the previous run")
        return VerificationReport(not problems, 1, tuple(problems))

    def verify(self) -> VerificationReport:
        """Check database integrity, triggers, every content hash, the chain and the queryable rows."""
        problems = []
        if self._fetch("PRAGMA integrity_check") != [("ok",)]:
            problems.append("SQLite integrity_check reported problems")
        if self._fetch("PRAGMA foreign_key_check"):
            problems.append("foreign key violations were found")
        present = {r[0] for r in self._fetch("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        missing = sorted(set(TRIGGER_NAMES) - present)
        if missing:
            problems.append(f"append-only triggers are missing: {', '.join(missing)}")
        expected_seq, prev_chain, checked = 1, snap.GENESIS, 0
        for values in self._fetch(f"SELECT {', '.join(_RUN_FIELDS)} FROM runs ORDER BY seq"):
            row = dict(zip(_RUN_FIELDS, values))
            checked += 1
            if row["seq"] != expected_seq:
                problems.append(f"the run sequence has a gap before run {row['run_id']} (expected {expected_seq}, "
                                f"found {row['seq']})")
                expected_seq = row["seq"]
            if row["prev_chain_hash"] != prev_chain:
                problems.append(f"run {row['run_id']}: the chain does not link to the previous run")
            problems += self._check_run(row)
            expected_seq, prev_chain = expected_seq + 1, row["chain_hash"]
        return VerificationReport(not problems, checked, tuple(problems))

    def backup(self, destination) -> Path:
        """Copy the database with SQLite's backup API. An existing file is never overwritten."""
        destination = Path(destination)
        if destination.exists():
            raise SnapshotStoreError(f"backup destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(destination))
        try:
            self._con.backup(target)
        except sqlite3.Error as err:
            target.close()
            destination.unlink(missing_ok=True)
            raise SnapshotStoreError(f"backup failed: {err}") from None
        target.close()
        return destination

    def close(self) -> None:
        self._con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SnapshotReader(_ReadMixin):
    """A read-only connection: it can retrieve, verify and back up, but cannot write."""

    def __init__(self, path=None, *, busy_timeout: float = 5.0, environ=None):
        self.path = resolve_db_path(path, environ)
        if not self.path.exists():
            raise SnapshotStoreError(f"snapshot database not found: {self.path}")
        self._con = _connect(self.path, readonly=True, busy_timeout=busy_timeout)
        version = self._fetch("PRAGMA user_version")[0][0]
        if version != SCHEMA_VERSION:
            self._con.close()
            raise SnapshotStoreError(f"unsupported snapshot schema version {version}")


# --- writing ---------------------------------------------------------------------------------------------------------

def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _new_run_id() -> str:
    return str(uuid.uuid7())


class SnapshotStore(_ReadMixin):
    """The writable store. `now` and `new_run_id` can be replaced to make tests deterministic."""

    def __init__(self, path=None, *, now=None, new_run_id=None, busy_timeout: float = 5.0,
                 allow_cloud_sync: bool = False, environ=None):
        self.path = resolve_db_path(path, environ)
        if not allow_cloud_sync and in_cloud_sync_folder(self.path, environ):
            raise SnapshotStoreError(
                f"{self.path} is inside a OneDrive folder; cloud sync can corrupt SQLite databases. "
                "Choose a local folder, or pass allow_cloud_sync=True to accept the risk.")
        self._now = now or _utc_now
        self._new_run_id = new_run_id or _new_run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = _connect(self.path, readonly=False, busy_timeout=busy_timeout)
        self._initialize()

    def _initialize(self) -> None:
        version = self._fetch("PRAGMA user_version")[0][0]
        if version > SCHEMA_VERSION:
            self._con.close()
            raise SnapshotStoreError(f"the database uses a newer schema version ({version})")
        if version == SCHEMA_VERSION:
            return
        if self._fetch("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"):
            self._con.close()
            raise SnapshotStoreError("the file is a database, but not a snapshot database this code can use")
        self._begin()
        try:
            for statement in _schema_statements():
                self._con.execute(statement)
            self._con.execute('INSERT INTO "meta" ("key", "value") VALUES (?, ?)', ("format", str(snap.FORMAT_VERSION)))
            self._con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._con.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise

    def _begin(self) -> None:
        try:
            self._con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as err:
            raise SnapshotStoreError(f"the snapshot database is busy or locked: {err}") from None

    def _rollback(self) -> None:
        if self._con.in_transaction:
            self._con.execute("ROLLBACK")

    def _insert_projection(self, run_id: str, payload: dict) -> None:
        for table, rows in snap.projection_rows(payload).items():
            if not rows:
                continue
            columns = ", ".join(f'"{c}"' for c in ("run_id", *snap.PROJECTION_COLUMNS[table]))
            marks = ", ".join("?" for _ in range(len(snap.PROJECTION_COLUMNS[table]) + 1))
            self._con.executemany(f'INSERT INTO "{table}" ({columns}) VALUES ({marks})',
                                  [(run_id, *row) for row in rows])

    def write(self, snapshot: snap.Snapshot, *, on_duplicate: str = "return_existing") -> WriteResult:
        """Append a run. Identical content for the latest revision of the same client and period
        returns that run (or raises with on_duplicate="error"); changed content adds a revision."""
        if on_duplicate not in DUPLICATE_MODES:
            raise ValueError(f"on_duplicate must be one of {DUPLICATE_MODES}")
        if snap.compute_content_hash(snapshot.payload) != snapshot.content_hash:
            raise SnapshotStoreError("the snapshot's content does not match its hash")
        run_id, created_at = self._new_run_id(), self._now()
        payload_json = snapshot.payload_json
        key = (snapshot.client_key, snapshot.period_start, snapshot.period_end)
        self._begin()
        try:
            revisions = self._con.execute(
                "SELECT run_id, revision, content_hash FROM runs "
                "WHERE client_key = ? AND period_start = ? AND period_end = ? ORDER BY revision", key).fetchall()
            live = [r for r in revisions if not self._is_voided(r[0])]
            latest = live[-1] if live else None
            if latest is not None and latest[2] == snapshot.content_hash:
                self._rollback()
                if on_duplicate == "error":
                    raise DuplicateSnapshotError(
                        f"run {latest[0]} (revision {latest[1]}) already holds identical content for {key}")
                return WriteResult(latest[0], latest[1], False, latest[0], None, ())
            revision = revisions[-1][1] + 1 if revisions else 1
            last = self._con.execute("SELECT seq, chain_hash FROM runs ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev_chain = (last[0] + 1, last[1]) if last else (1, snap.GENESIS)
            chain = snap.chain_hash(prev_chain, seq=seq, run_id=run_id, client_key=key[0], period_start=key[1],
                                    period_end=key[2], revision=revision, content_hash=snapshot.content_hash,
                                    created_at=created_at)
            values = (seq, run_id, *snap.run_columns(snapshot.payload), revision, snapshot.content_hash,
                      prev_chain, chain, created_at, payload_json)
            self._con.execute(_INSERT_RUN, values)
            self._insert_projection(run_id, snapshot.payload)
            superseded = latest[0] if latest else None
            if superseded:
                self._con.execute('INSERT INTO "run_events" ("run_id", "kind", "detail", "created_at") '
                                  "VALUES (?, 'superseded', ?, ?)", (superseded, run_id, created_at))
            overlaps = self._con.execute(
                "SELECT run_id, period_start, period_end, revision FROM runs r "
                "WHERE client_key = ? AND NOT (period_start = ? AND period_end = ?) "
                "AND period_start <= ? AND period_end >= ? "
                "AND NOT EXISTS (SELECT 1 FROM run_events e WHERE e.run_id = r.run_id AND e.kind = 'voided') "
                "ORDER BY seq", (key[0], key[1], key[2], key[2], key[1])).fetchall()
            self._con.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        notes = tuple(f"This period overlaps run {r[0]} ({r[1]} to {r[2]}, revision {r[3]})." for r in overlaps)
        return WriteResult(run_id, revision, True, None, superseded, notes)

    def add_event(self, run_id: str, kind: str, detail: str | None = None) -> StoredEvent:
        """Record a status change (rendered, reviewed, delivered, voided). 'voided' needs a reason."""
        if kind not in PUBLIC_EVENT_KINDS:
            raise ValueError(f"kind must be one of {PUBLIC_EVENT_KINDS}")
        text = mask_event_detail(detail) if detail else None
        if kind == "voided" and not text:
            raise ValueError("a voided event needs a reason")
        created_at = self._now()
        self._begin()
        try:
            if self._con.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                raise SnapshotStoreError(f"no snapshot with run ID {run_id!r}")
            self._con.execute('INSERT INTO "run_events" ("run_id", "kind", "detail", "created_at") VALUES (?, ?, ?, ?)',
                              (run_id, kind, text, created_at))
            self._con.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise
        return StoredEvent(kind, text, created_at)
