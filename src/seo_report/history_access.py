"""Read-only access to the report history, limited to runs that were really finalized.

Two things live here:

* `probe_history` looks at the history database without changing it and says whether it is `available`,
  `missing`, `unavailable` or has a `schema_failure`. It never creates, initializes or migrates anything.
* `FinalizedOnlyReader` wraps a `SnapshotReader` and offers Stage 6b's `compare_with_history` exactly the
  four methods it calls (`list_runs`, `verify_run`, `get`, `events`), but only for finalized runs. Stage 6b is
  used unchanged: the wrapper only narrows the list of candidate runs.

A stored run is finalized only if all of these hold (the exclusion reason is the first check that fails):

  1. a reviewer record `reviewer.<run_id>.<pdf16>.json` exists in the run's reviewer folder        no_reviewer_record
  2. it parses as strict JSON and passes `record_schema.validate_finalized_record`                   record_invalid
  3. it matches the stored run: run ID, revision, content hash, quality, client key, report period,
     and the file name's <pdf16> matches the record's PDF hash                                       record_mismatch
  4. a PDF named `report.pdf` or `report.superseded.<pdf16>[.<n>].pdf` (n = 2..99) exists            final_pdf_missing
  5. the full SHA-256 of such a file equals the record's `pdf_sha256`                                pdf_hash_mismatch
  6. the run has a `rendered` event whose detail contains that full PDF hash                         no_rendered_event

When a run has several reviewer records (the same snapshot rendered more than once) it is finalized if any one
of them passes; otherwise the reason of the record that got furthest is reported. The snapshot itself is checked
by the existing `verify_run`, which Stage 6b calls for every candidate before using it; a damaged run therefore
still appears in Stage 6b's skipped list with its usual "failed verification" reason.

Because unfinalized runs are removed before Stage 6b groups revisions, a newer unfinalized revision cannot hide
an earlier finalized one. Revision numbers and stored data are never changed.

Nothing here writes to the database or creates files or folders. Messages and exclusion details contain fixed
phrases, run IDs, dates and reason codes only: no raw exception text, SQL or file paths.
"""

import functools
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from seo_report import output_layout as layout
from seo_report import record_schema
from seo_report.snapshots import store as st

STATUS_AVAILABLE = "available"
STATUS_MISSING = "missing"
STATUS_UNAVAILABLE = "unavailable"
STATUS_SCHEMA_FAILURE = "schema_failure"
PROBE_STATUSES = (STATUS_AVAILABLE, STATUS_MISSING, STATUS_UNAVAILABLE, STATUS_SCHEMA_FAILURE)

NO_REVIEWER_RECORD = "no_reviewer_record"
RECORD_INVALID = "record_invalid"
RECORD_MISMATCH = "record_mismatch"
FINAL_PDF_MISSING = "final_pdf_missing"
PDF_HASH_MISMATCH = "pdf_hash_mismatch"
NO_RENDERED_EVENT = "no_rendered_event"
# In the order the checks run; a later code means a record got further.
REASON_ORDER = (NO_REVIEWER_RECORD, RECORD_INVALID, RECORD_MISMATCH, FINAL_PDF_MISSING, PDF_HASH_MISMATCH,
                NO_RENDERED_EVENT)

MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_RECORD_CANDIDATES = 20
_HASH_CHUNK = 1024 * 1024
_SHA256_TOKEN = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")


class HistoryAccessError(st.SnapshotStoreError):
    """The history could not be read. The message never contains SQL, paths or raw exception text."""


# --- database probe -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbeResult:
    status: str  # one of PROBE_STATUSES
    detail: str  # a fixed phrase
    user_version: int | None = None
    problems: tuple[str, ...] = ()  # schema problems: table, column and trigger names only

    @property
    def available(self) -> bool:
        return self.status == STATUS_AVAILABLE


@functools.lru_cache(maxsize=1)
def _expected_columns() -> dict:
    """Table name -> column names, taken from the store's own DDL (built in memory, never on disk)."""
    con = sqlite3.connect(":memory:")
    try:
        for statement in st._schema_statements():
            con.execute(statement)
        return {table: tuple(row[1] for row in con.execute(f'PRAGMA table_info("{table}")'))
                for table in st.ALL_TABLES}
    finally:
        con.close()


def _unavailable(detail: str) -> ProbeResult:
    return ProbeResult(STATUS_UNAVAILABLE, detail)


def probe_history(path=None, *, environ=None, busy_timeout: float = 1.0) -> ProbeResult:
    """Classify the history database without changing it.

    The path comes from `store.resolve_db_path` (explicit path, $JASPER_SEO_HISTORY_DB, then the default). The
    file is opened with SQLite's read-only URI mode and `PRAGMA query_only=ON`, so a missing file is never
    created and nothing is initialized, migrated or written.
    """
    db_path = st.resolve_db_path(path, environ)
    try:
        if not db_path.exists():
            return ProbeResult(STATUS_MISSING, "the history database does not exist")
        if not db_path.is_file():
            return _unavailable("the history database path is not a file")
        uri = db_path.resolve().as_uri() + "?mode=ro"
    except OSError:
        return _unavailable("the history database could not be reached")
    con = None
    try:
        con = sqlite3.connect(uri, uri=True, timeout=busy_timeout, isolation_level=None)
        con.execute("PRAGMA query_only=ON")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version != st.SCHEMA_VERSION:
            return ProbeResult(STATUS_SCHEMA_FAILURE, "the history database uses an unsupported schema version",
                               user_version=version)
        problems = _schema_problems(con)
        if problems:
            return ProbeResult(STATUS_SCHEMA_FAILURE, "the history database does not have the expected structure",
                               user_version=version, problems=tuple(problems))
        return ProbeResult(STATUS_AVAILABLE, "the history database has the expected structure",
                           user_version=version)
    except sqlite3.OperationalError:
        return _unavailable("the history database could not be opened or is busy")
    except sqlite3.Error:
        return _unavailable("the history database could not be read")
    finally:
        if con is not None:
            con.close()


def _schema_problems(con) -> list[str]:
    problems = []
    present = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    expected = _expected_columns()
    for table in st.ALL_TABLES:
        if table not in present:
            problems.append(f"missing table: {table}")
            continue
        have = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
        problems += [f"missing column: {table}.{column}" for column in expected[table] if column not in have]
    triggers = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    problems += [f"missing trigger: {name}" for name in st.TRIGGER_NAMES if name not in triggers]
    return problems


# --- finalization ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ExcludedRun:
    """A stored run that is not (yet) a finalized report, as the reviewer record lists it."""

    run_id: str
    revision: int
    period_start: str
    period_end: str
    reason_code: str

    def as_record(self) -> dict:
        """The `history.excluded_unfinalized[]` entry of a finalized reviewer record."""
        return {"run_id": self.run_id, "revision": self.revision, "period_start": self.period_start,
                "period_end": self.period_end, "reason_code": self.reason_code}


class _DuplicateKey(ValueError):
    pass


def _strict_object(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKey(key)
        seen[key] = value
    return seen


def _refuse_constant(name):
    raise ValueError(name)


def _load_record(path: Path):
    """Parse a reviewer record file strictly. Returns the record or None when it cannot be used."""
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            return None
        text = path.read_bytes().decode("utf-8")
        record = json.loads(text, object_pairs_hook=_strict_object, parse_constant=_refuse_constant)
        return record_schema.validate_finalized_record(record)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError, record_schema.RecordSchemaError):
        return None


def _sha256_of(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


class FinalizedOnlyReader:
    """What Stage 6b's `compare_with_history` sees: only finalized runs, through a read-only connection.

    `compare_with_history` calls exactly `list_runs`, `verify_run` and `get` (and the reader offers `events`).
    No other reader method is available here: the underlying reader's `verify`, `latest`, `backup`, and every
    write method are unreachable. `verify_run`, `get` and `events` work only for runs that `list_runs` has
    exposed as finalized.
    """

    __slots__ = ("_reader", "_root", "_finalized")

    def __init__(self, reader, output_root):
        root = Path(os.fspath(output_root))
        if not root.is_absolute():
            raise HistoryAccessError("the output root must be an absolute path")
        self._reader = reader
        self._root = root
        self._finalized = {}

    # -- what Stage 6b consumes ---------------------------------------------------------------------------------

    def list_runs(self, client_key: str | None = None, *, include_voided: bool = False) -> list:
        finalized, _ = self._scan(client_key)
        self._finalized.update({info.run_id: info for info in finalized})
        return [info for info in finalized if include_voided or not info.voided]

    def verify_run(self, run_id: str):
        self._require_finalized(run_id)
        return self._read(self._reader.verify_run, run_id)

    def get(self, run_id: str):
        self._require_finalized(run_id)
        return self._read(self._reader.get, run_id)

    def events(self, run_id: str):
        self._require_finalized(run_id)
        return self._read(self._reader.events, run_id)

    # -- for the reviewer record ------------------------------------------------------------------------------------

    def excluded_unfinalized(self, client_key: str | None = None) -> tuple[ExcludedRun, ...]:
        """Runs that are not finalized, with the first failing check. Voided runs are not reported."""
        _, excluded = self._scan(client_key)
        return excluded

    def close(self) -> None:
        self._reader.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- internals ---------------------------------------------------------------------------------------------------

    @staticmethod
    def _read(call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except st.SnapshotStoreError:
            raise HistoryAccessError("the history database could not be read") from None
        except (ValueError, sqlite3.Error):
            raise HistoryAccessError("a stored history run could not be read") from None

    def _require_finalized(self, run_id) -> None:
        if run_id not in self._finalized:
            raise HistoryAccessError("the run is not a finalized report")

    def _scan(self, client_key):
        infos = self._read(self._reader.list_runs, client_key, include_voided=True)
        finalized, excluded = [], []
        for info in infos:
            reason = self._assess(info)
            if reason is None:
                finalized.append(info)
            elif not info.voided:
                excluded.append(ExcludedRun(info.run_id, info.revision, info.period_start, info.period_end, reason))
        return finalized, tuple(excluded)

    def _assess(self, info) -> str | None:
        """None when the run is finalized, otherwise the reason code of the check that failed."""
        try:
            reviewer = layout.reviewer_dir(self._root, info.client_key, info.period_start, info.period_end)
            reports = layout.report_dir(self._root, info.client_key, info.period_start, info.period_end)
        except layout.OutputLayoutError:
            return NO_REVIEWER_RECORD
        names = self._record_names(reviewer, info.run_id)
        if not names:
            return NO_REVIEWER_RECORD
        furthest = RECORD_INVALID
        for name, short in names:
            reason = self._assess_record(info, reviewer / name, short, reports)
            if reason is None:
                return None
            if REASON_ORDER.index(reason) > REASON_ORDER.index(furthest):
                furthest = reason
        return furthest

    @staticmethod
    def _record_names(folder: Path, run_id: str) -> list[tuple[str, str]]:
        """Regular files named `reviewer.<run_id>.<pdf16>.json`, as (file name, pdf16), in name order."""
        pattern = re.compile(rf"reviewer\.{re.escape(run_id)}\.([0-9a-f]{{16}})\.json")
        found = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    match = pattern.fullmatch(entry.name)
                    if match and entry.is_file(follow_symlinks=False):
                        found.append((entry.name, match.group(1)))
        except OSError:
            return []
        return sorted(found)[:MAX_RECORD_CANDIDATES]

    def _assess_record(self, info, record_path: Path, short: str, reports: Path) -> str | None:
        record = _load_record(record_path)
        if record is None:
            return RECORD_INVALID
        if not self._matches_run(record, info) or record["finalization"]["pdf_sha256"][:16] != short:
            return RECORD_MISMATCH
        pdf_sha256 = record["finalization"]["pdf_sha256"]
        pdf_bytes = record["finalization"]["pdf_bytes"]
        found, matched = self._check_pdf(reports, pdf_sha256, pdf_bytes)
        if not found:
            return FINAL_PDF_MISSING
        if not matched:
            return PDF_HASH_MISMATCH
        if not self._has_rendered_event(info.run_id, pdf_sha256):
            return NO_RENDERED_EVENT
        return None

    @staticmethod
    def _matches_run(record: dict, info) -> bool:
        snapshot = record["snapshot"]
        return (snapshot["run_id"] == info.run_id and snapshot["revision"] == info.revision
                and snapshot["content_hash"] == info.content_hash and snapshot["quality"] == info.quality
                and record["client_key"] == info.client_key
                and record["report_period"] == {"start": info.period_start, "end": info.period_end})

    @staticmethod
    def _check_pdf(reports: Path, pdf_sha256: str, pdf_bytes: int) -> tuple[bool, bool]:
        """(a recognized PDF file exists, one of them has exactly this full SHA-256).

        Only `report.pdf` and `report.superseded.<pdf16>[.<n>].pdf` are looked at, by exact name.
        """
        wanted = {layout.STANDARD_PDF_NAME,
                  *layout.variant_candidates("superseded", layout.hash16_of(pdf_sha256))}
        found = matched = False
        try:
            with os.scandir(reports) as entries:
                candidates = sorted((e for e in entries if e.name in wanted), key=lambda e: e.name)
                for entry in candidates:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    found = True
                    if entry.stat(follow_symlinks=False).st_size != pdf_bytes:
                        continue  # a different size cannot have the same hash
                    if _sha256_of(Path(entry.path)) == pdf_sha256:
                        matched = True
                        break
        except OSError:
            return found, matched
        return found, matched

    def _has_rendered_event(self, run_id: str, pdf_sha256: str) -> bool:
        try:
            events = self._reader.events(run_id)
        except st.SnapshotStoreError:
            return False
        return any(event.kind == "rendered" and event.detail
                   and pdf_sha256 in _SHA256_TOKEN.findall(event.detail) for event in events)


# --- opening --------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class HistoryAccess:
    """The outcome of opening the history: the probe result and, only when it is available, the reader."""

    probe: ProbeResult
    reader: FinalizedOnlyReader | None

    def close(self) -> None:
        if self.reader is not None:
            self.reader.close()


def open_history(db_path=None, output_root=None, *, environ=None, allow_onedrive_output: bool = False,
                 busy_timeout: float = 5.0) -> HistoryAccess:
    """Probe the history database and, if it is usable, open a finalized-only reader on it.

    The database path follows `store.resolve_db_path`; the output root follows `output_layout.resolve_output_root`
    (an invalid output root raises OutputLayoutError). A database that is missing, unavailable or has a schema
    problem yields a result with `reader=None` and never raises.
    """
    probe = probe_history(db_path, environ=environ)
    if not probe.available:
        return HistoryAccess(probe, None)
    root = layout.resolve_output_root(output_root, environ=environ, db_path=db_path,
                                      allow_onedrive_output=allow_onedrive_output)
    try:
        reader = st.SnapshotReader(db_path, busy_timeout=busy_timeout, environ=environ)
        reader._con.execute("PRAGMA query_only=ON")  # the connection is already mode=ro; this is a second guard
    except (st.SnapshotStoreError, sqlite3.Error):
        return HistoryAccess(_unavailable("the history database could not be opened"), None)
    return HistoryAccess(probe, FinalizedOnlyReader(reader, root))
