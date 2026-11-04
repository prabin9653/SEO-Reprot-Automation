"""A read-only audit of one client's report period (Stage 6c-8g).

`audit_period(...)` looks at what finalization left behind for one client and one reporting period and says, with
fixed categories, which parts are consistent and which are not. It changes nothing: no file or folder is created,
renamed, repaired or deleted, partial files are only listed, the history database is opened with SQLite's read-only
mode (the existing `SnapshotReader` and `history_access` readers), and no event or snapshot is written.

What is inspected (names and hashes only are reported; never paths, record content or exception text)

  reports/<client>/<period>/    `report.pdf`, `report.superseded.<h16>[.n].pdf`, `report.duplicate.<h16>[.n].pdf`,
                                `report.unarchived.pdf` and `report.unarchived.<h16>[.n].pdf`, partial files
  reviewer/<client>/<period>/   `reviewer.<run_id>.<pdf16>.json` records
  attempts/<client>/<period>/   `<timestamp>.<pdf16|nopdf>.<step>.attempt[.<n>].json` failure records, partial files
  history database              the period's stored runs, their events and per-run verification

Checks for every reviewer record (the first failing check decides the category; a record is `finalized_valid` only
when all pass and the finalized-only reader lists its run):

  1 strict JSON            invalid JSON, bad encoding, duplicate object keys and NaN/Infinity are rejected
  2 schema                 `validate_finalized_record`
  3 name and period        the file name's run ID and PDF prefix, the client and the period match the record
  4 stored snapshot        the run exists; revision, content hash, quality and client match
  5 snapshot verification  `verify_run` (canonical payload hash, projections, hash chain) for that run only
  6 PDF                    a recognized name (`report.pdf` or the superseded variants of the record's prefix) that is
                           a regular, non-link file with the record's byte count and full SHA-256
  7 rendered event         an event of the run whose detail contains the record's exact full PDF hash
  8 reader                 the finalized-only reader lists the run
  9 render identity        schema version 2 records only: the sidecar `report.render-input.<first 16 hex of the
                           record's render_input_sha256>.json` in the reports folder exists, passes
                           `render_artifacts.parse_sidecar` (structure, versions, payload hash), carries the record's
                           full render-input hash and format, and names the record's PDF SHA-256 and byte count.
                           Version 1 records have no render identity: nothing is checked and nothing is reported
                           against them beyond `AuditReport.legacy_records`. The sidecar is only read; a missing or
                           invalid one is reported, never recreated or repaired.

A PDF that exists is never taken as proof of finalization. Database-wide integrity (`SnapshotReader.verify`) walks
every run in the database, so it runs only when `deep_database_check=True` is passed.

Categories (`AuditFinding.category`)
  problems     finalized_invalid, reviewer_record_identity_mismatch, reviewer_record_without_pdf, pdf_hash_mismatch,
               rendered_event_hash_mismatch, snapshot_verification_failed, snapshot_without_finalization (latest
               revision), rendered_event_without_finalization, pdf_without_reviewer_record, pdf_not_regular_file,
               attempt_record_invalid, partial_file_present, reader_record_limit_reached, database_integrity_failed,
              render_sidecar_missing, render_sidecar_invalid, render_input_hash_mismatch, render_input_format_invalid
              (the last four: version 2 records only; `reviewer_record_identity_mismatch` also covers a sidecar whose
              PDF hash or size differs from the record's)
  information  finalized_valid, attempt_record_valid (an earlier failed attempt), superseded_pdf_present,
               duplicate_pdf_present, unarchived_pdf_present, voided_snapshot, snapshot_without_finalization (older
               or superseded revision)
  limits       verification_unavailable (a check could not be made; the audit never reports success for it)

Outcome (`AuditReport.outcome`): `unavailable` (the location cannot be audited), `attention_needed` (at least one
problem finding), `limited` (no problem found, but a check was unavailable), `consistent` (everything checked, no
problem). `verification_complete` is False whenever any check was unavailable. An empty period (nothing on disk and
nothing stored) is `consistent`. A missing history database with reports or records present is `limited`.

Attempt records are failure-only: a clean attempts folder does not prove that anything succeeded, and a valid attempt
record does not prove that a report is not finalized. The two are reported separately.
"""

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from seo_report import finalize_files as ff
from seo_report import history_access as ha
from seo_report import output_layout as layout
from seo_report import record_schema as rs
from seo_report.snapshots import store as st

OUTCOME_CONSISTENT = "consistent"
OUTCOME_ATTENTION = "attention_needed"
OUTCOME_LIMITED = "limited"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOMES = (OUTCOME_CONSISTENT, OUTCOME_ATTENTION, OUTCOME_LIMITED, OUTCOME_UNAVAILABLE)

SEVERITY_INFO, SEVERITY_ATTENTION, SEVERITY_LIMIT = "info", "attention", "limit"

FINALIZED_VALID = "finalized_valid"
FINALIZED_INVALID = "finalized_invalid"
SNAPSHOT_WITHOUT_FINALIZATION = "snapshot_without_finalization"
RENDERED_EVENT_WITHOUT_FINALIZATION = "rendered_event_without_finalization"
PDF_WITHOUT_RECORD = "pdf_without_reviewer_record"
RECORD_WITHOUT_PDF = "reviewer_record_without_pdf"
RECORD_IDENTITY_MISMATCH = "reviewer_record_identity_mismatch"
PDF_HASH_MISMATCH = "pdf_hash_mismatch"
EVENT_HASH_MISMATCH = "rendered_event_hash_mismatch"
ATTEMPT_VALID = "attempt_record_valid"
ATTEMPT_INVALID = "attempt_record_invalid"
PARTIAL_FILE = "partial_file_present"
SUPERSEDED_PDF = "superseded_pdf_present"
DUPLICATE_PDF = "duplicate_pdf_present"
UNARCHIVED_PDF = "unarchived_pdf_present"
VOIDED_SNAPSHOT = "voided_snapshot"
VERIFICATION_UNAVAILABLE = "verification_unavailable"
SNAPSHOT_VERIFICATION_FAILED = "snapshot_verification_failed"
PDF_NOT_REGULAR = "pdf_not_regular_file"
RECORD_LIMIT_REACHED = "reader_record_limit_reached"
DATABASE_INTEGRITY_FAILED = "database_integrity_failed"
RENDER_SIDECAR_MISSING = "render_sidecar_missing"
RENDER_SIDECAR_INVALID = "render_sidecar_invalid"
RENDER_INPUT_HASH_MISMATCH = "render_input_hash_mismatch"
RENDER_INPUT_FORMAT_INVALID = "render_input_format_invalid"

CATEGORIES = (
    FINALIZED_VALID, FINALIZED_INVALID, SNAPSHOT_WITHOUT_FINALIZATION, RENDERED_EVENT_WITHOUT_FINALIZATION,
    PDF_WITHOUT_RECORD, RECORD_WITHOUT_PDF, RECORD_IDENTITY_MISMATCH, PDF_HASH_MISMATCH, EVENT_HASH_MISMATCH,
    ATTEMPT_VALID, ATTEMPT_INVALID, PARTIAL_FILE, SUPERSEDED_PDF, DUPLICATE_PDF, UNARCHIVED_PDF, VOIDED_SNAPSHOT,
    VERIFICATION_UNAVAILABLE, SNAPSHOT_VERIFICATION_FAILED, PDF_NOT_REGULAR, RECORD_LIMIT_REACHED,
    DATABASE_INTEGRITY_FAILED, RENDER_SIDECAR_MISSING, RENDER_SIDECAR_INVALID, RENDER_INPUT_HASH_MISMATCH,
    RENDER_INPUT_FORMAT_INVALID)
_INFO_CATEGORIES = {FINALIZED_VALID, ATTEMPT_VALID, SUPERSEDED_PDF, DUPLICATE_PDF, UNARCHIVED_PDF, VOIDED_SNAPSHOT}

_RECORD_NAME = re.compile(r"reviewer\.([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.([0-9a-f]{16})\.json")
_ATTEMPT_NAME = re.compile(r"(\d{8}T\d{6}Z)\.(nopdf|[0-9a-f]{16})\.(" + "|".join(layout.ATTEMPT_STEPS)
                           + r")\.attempt(?:\.([0-9]{1,3}))?\.json")
_SHA_TOKEN = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")

_scandir = os.scandir  # replaceable in tests


@dataclass(frozen=True)
class AuditFinding:
    category: str  # one of CATEGORIES
    reason: str  # a fixed code
    severity: str  # info, attention or limit
    run_id: str | None = None
    revision: int | None = None
    name: str | None = None  # a file name (never a path)
    record_version: int | None = None  # schema version of the reviewer record the finding is about, when it was read


@dataclass(frozen=True)
class AuditReport:
    client_key: str | None
    period_start: str | None
    period_end: str | None
    outcome: str  # one of OUTCOMES
    verification_complete: bool
    finalized_valid: int  # reviewer records that passed every check
    unresolved: int  # findings that need attention
    attempt_records: int  # files that follow the attempt-record name pattern
    attempt_records_valid: int
    partial_files: int
    stored_runs: int  # runs stored for this client and period (voided ones included)
    database_check: str  # not_requested, ok, failed or unavailable
    findings: tuple[AuditFinding, ...]
    legacy_records: int = 0  # schema-valid version 1 reviewer records (no render identity); informational only

    @property
    def ok(self) -> bool:
        return self.outcome == OUTCOME_CONSISTENT

    def by_category(self, category: str) -> tuple[AuditFinding, ...]:
        return tuple(f for f in self.findings if f.category == category)


# --- strict parsing -----------------------------------------------------------------------------------------------------------------

class _DuplicateKey(ValueError):
    pass


class _Constant(ValueError):
    pass


def _object(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKey()
        seen[key] = value
    return seen


def _constant(_name):
    raise _Constant()


def _parse(data: bytes):
    """(value, None) or (None, reason): invalid_encoding, duplicate_key, nonstandard_constant, invalid_json."""
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant), None
    except UnicodeDecodeError:
        return None, "invalid_encoding"
    except _DuplicateKey:
        return None, "duplicate_key"
    except _Constant:
        return None, "nonstandard_constant"
    except (ValueError, RecursionError):
        return None, "invalid_json"


# --- reading files -----------------------------------------------------------------------------------------------------------------------

def _list_folder(folder):
    """('absent', ()) | ('ok', ((name, is_regular_file), ...)) | ('unavailable', reason). Read-only."""
    try:
        with _scandir(folder) as entries:
            return "ok", tuple(sorted((e.name, e.is_file(follow_symlinks=False)) for e in entries))
    except FileNotFoundError:
        return "absent", ()
    except NotADirectoryError:
        return "unavailable", "directory_not_a_folder"
    except OSError:
        return "unavailable", "directory_unreadable"


def _read_file(path, ops, limit):
    """('ok', bytes) | ('not_regular', None) | ('too_large', None) | ('unreadable', None)."""
    try:
        info = ops.lstat(path)
    except OSError:
        return "unreadable", None
    if ff._is_link(info) or not stat.S_ISREG(info.st_mode):
        return "not_regular", None
    if info.st_size > limit:
        return "too_large", None
    try:
        with ops.open_read(path) as handle:
            data = handle.read(limit + 1)
    except OSError:
        return "unreadable", None
    return ("ok", data) if len(data) <= limit else ("too_large", None)


class _Collector:
    def __init__(self):
        self.items = []

    def add(self, category, reason, severity=None, **fields):
        if severity is None:
            severity = SEVERITY_INFO if category in _INFO_CATEGORIES else (
                SEVERITY_LIMIT if category == VERIFICATION_UNAVAILABLE else SEVERITY_ATTENTION)
        self.items.append(AuditFinding(category, reason, severity, **fields))


# --- the audit -----------------------------------------------------------------------------------------------------------------------------

def audit_period(*, output_root, history_db_path, client_key, period_start, period_end,
                 deep_database_check: bool = False, environ=None, allow_onedrive_output: bool = False,
                 ops: ff.FileOps | None = None) -> AuditReport:
    """Audit one client's report period. Read-only; see the module docstring.

    `output_root` and `history_db_path` follow the usual rules (`output_layout.resolve_output_root`,
    `store.resolve_db_path`). An unusable location gives an `unavailable` report, never an exception.
    """
    ops = ops or ff.FileOps()
    try:
        start = layout.validate_period_date(period_start)
        end = layout.validate_period_date(period_end)
        root = layout.resolve_output_root(output_root, environ=environ, db_path=history_db_path,
                                          allow_onedrive_output=allow_onedrive_output)
        reports = layout.report_dir(root, client_key, start, end)
        reviewer = layout.reviewer_dir(root, client_key, start, end)
        attempts = layout.attempt_dir(root, client_key, start, end)
    except (layout.OutputLayoutError, TypeError, ValueError):
        finding = AuditFinding(VERIFICATION_UNAVAILABLE, "output_location_invalid", SEVERITY_LIMIT)
        return AuditReport(None, None, None, OUTCOME_UNAVAILABLE, False, 0, 0, 0, 0, 0, 0, "not_requested", (finding,))

    found = _Collector()
    state = _Audit(found, client_key, start, end, reports, reviewer, attempts, ops)
    state.open_history(history_db_path, root, environ, allow_onedrive_output, deep_database_check)
    try:
        for scan in (state.scan_reports, state.scan_reviewer_records, state.scan_snapshots, state.scan_attempts):
            try:
                scan()
            except Exception:  # an unexpected failure is a limit of the audit, never a clean result and never raised
                found.add(VERIFICATION_UNAVAILABLE, "scan_failed")
                if scan.__name__ in ("scan_reviewer_records", "scan_reports"):
                    state.records_scan_failed = True
    finally:
        state.close()
    return state.report()


class _Audit:
    def __init__(self, found, client_key, start, end, reports, reviewer, attempts, ops):
        self.found, self.client_key, self.start, self.end = found, client_key, start, end
        self.reports, self.reviewer, self.attempts, self.ops = reports, reviewer, attempts, ops
        self.raw = None  # SnapshotReader
        self.access = None  # HistoryAccess
        self.db_state = "unavailable"  # available or unavailable
        self.db_reason = None
        self.database_check = "not_requested"
        self.runs = {}  # run_id -> RunInfo of this client and period
        self.finalized_runs = set()  # listed by the finalized-only reader
        self.events = {}  # run_id -> tuple of events
        self.verified = {}  # run_id -> bool
        self.pdf_digests = {}  # file name -> FileDigest | None
        self.record_hashes = set()  # PDF hashes named by schema-valid records
        self.valid_records = 0
        self.legacy_records = 0
        self.run_has_valid_record = set()
        self.run_record_names = {}  # run_id -> [file names]
        self.attempt_total = self.attempt_valid = 0
        self.partials = 0
        self.records_scan_failed = False  # the reviewer or report folder could not be scanned: no "missing" claims
        self.records_incomplete = False  # some record could not be checked: it might name a PDF
        self.unchecked_runs = set()  # runs with a record whose checks could not be completed

    # -- database ------------------------------------------------------------------------------------------------------------------

    def open_history(self, db_path, root, environ, allow_onedrive_output, deep):
        try:
            access = ha.open_history(db_path, root, environ=environ, allow_onedrive_output=allow_onedrive_output)
        except Exception:
            self.db_reason = "history_database_unavailable"
            return
        self.access = access
        probe = access.probe
        if probe.status == ha.STATUS_MISSING:
            self.db_reason = "history_database_missing"
        elif probe.status == ha.STATUS_SCHEMA_FAILURE:
            self.db_reason = "history_schema_failure"
        elif access.reader is None:
            self.db_reason = "history_database_unavailable"
        else:
            try:
                self.raw = st.SnapshotReader(db_path, environ=environ)
                infos = self.raw.list_runs(self.client_key, include_voided=True)
                self.runs = {i.run_id: i for i in infos if (i.period_start, i.period_end) == (self.start, self.end)}
                self.finalized_runs = {i.run_id for i in access.reader.list_runs(self.client_key)}
                self.db_state = "available"
            except Exception:
                self.db_reason = "snapshot_read_failed"
                self.runs = {}
        if self.db_state == "available" and deep:
            try:
                report = self.raw.verify()
                self.database_check = "ok" if report.ok else "failed"
                if not report.ok:
                    self.found.add(DATABASE_INTEGRITY_FAILED, "database_verify_failed")
            except Exception:
                self.database_check = "unavailable"
                self.found.add(VERIFICATION_UNAVAILABLE, "snapshot_read_failed")

    def close(self):
        for item in (self.access, self.raw):
            try:
                if item is not None:
                    item.close()
            except Exception:
                pass

    def unavailable(self, reason, **fields):
        self.found.add(VERIFICATION_UNAVAILABLE, reason, **fields)

    def run_events(self, run_id):
        if run_id not in self.events:
            try:
                self.events[run_id] = tuple(self.raw.events(run_id))
            except Exception:
                self.events[run_id] = None
        return self.events[run_id]

    def run_verified(self, run_id):
        if run_id not in self.verified:
            try:
                self.verified[run_id] = bool(self.raw.verify_run(run_id).ok)
            except Exception:
                self.verified[run_id] = None
        return self.verified[run_id]

    # -- reports folder --------------------------------------------------------------------------------------------------------------

    def digest(self, name):
        if name not in self.pdf_digests:
            try:
                self.pdf_digests[name] = ff.sha256_file(self.reports / name, ops=self.ops)
            except Exception:
                self.pdf_digests[name] = None
        return self.pdf_digests[name]

    def scan_reports(self):
        status, items = _list_folder(self.reports)
        if status == "unavailable":
            self.unavailable(items)
            self.reports_listing = ()
            self.records_scan_failed = True
            return
        self.reports_listing = items
        for name, regular in items:
            if name == layout.STANDARD_PDF_NAME or name == layout.DRAFT_PDF_NAME:
                kind = "final" if name == layout.STANDARD_PDF_NAME else "unarchived"
            elif ff.is_partial_name(name) or name.endswith(".partial"):
                self.partials += 1
                self.found.add(PARTIAL_FILE, "partial_in_reports", name=name)
                continue
            else:
                try:
                    kind = ff.parse_variant_name(name).family
                except ff.FileOperationError:
                    continue  # not a name this system creates
            if kind == "unarchived":
                self.found.add(UNARCHIVED_PDF, "draft_awaiting_finalization" if name == layout.DRAFT_PDF_NAME
                               else "unarchived_copy", name=name)
                continue
            if kind == "duplicate":
                self.found.add(DUPLICATE_PDF, "duplicate_copy", name=name)
                continue
            if kind == "superseded":
                self.found.add(SUPERSEDED_PDF, "superseded_copy", name=name)
            if not regular:
                self.found.add(PDF_NOT_REGULAR, "not_a_regular_file", name=name)

    # -- reviewer records ----------------------------------------------------------------------------------------------------------------

    def scan_reviewer_records(self):
        status, items = _list_folder(self.reviewer)
        if status == "unavailable":
            self.unavailable(items)
            self.records_scan_failed = True
            return
        per_run = {}
        for name, regular in items:
            match = _RECORD_NAME.fullmatch(name)
            if match is not None:
                per_run.setdefault(match.group(1), []).append((name, match.group(2), regular))
            elif ff.is_partial_name(name) or name.endswith(".partial"):
                self.partials += 1
                self.found.add(PARTIAL_FILE, "partial_in_reviewer", name=name)
        for run_id, records in per_run.items():
            records.sort()
            self.run_record_names[run_id] = [r[0] for r in records]
            if len(records) > ha.MAX_RECORD_CANDIDATES:
                self.found.add(RECORD_LIMIT_REACHED, "more_than_reader_limit", run_id=run_id)
            for position, (name, h16, regular) in enumerate(records):
                before = len(self.found.items)
                try:
                    self.check_record(run_id, name, h16, regular, beyond=position >= ha.MAX_RECORD_CANDIDATES)
                finally:
                    if any(f.category == VERIFICATION_UNAVAILABLE for f in self.found.items[before:]) \
                            or len(self.found.items) == before:
                        self.records_incomplete = True
                        self.unchecked_runs.add(run_id)
        self.check_pdfs_have_records()

    def check_record(self, run_id, name, h16, regular, *, beyond):
        add = self.found.add
        kind, data = _read_file(self.reviewer / name, self.ops, ha.MAX_RECORD_BYTES)
        if kind == "unreadable":
            return self.unavailable("record_unreadable", run_id=run_id, name=name)
        if kind != "ok":
            return add(FINALIZED_INVALID, kind, run_id=run_id, name=name)
        value, reason = _parse(data)
        if reason:
            return add(FINALIZED_INVALID, reason, run_id=run_id, name=name)
        try:
            record = rs.validate_finalized_record(value)
        except rs.RecordSchemaError:
            return add(FINALIZED_INVALID, "schema_invalid", run_id=run_id, name=name)
        snapshot, fin = record["snapshot"], record["finalization"]
        sha, size, revision = fin["pdf_sha256"], fin["pdf_bytes"], snapshot["revision"]
        self.record_hashes.add(sha)
        version = record["schema_version"]
        if version == 1:
            self.legacy_records += 1
        fields = dict(run_id=run_id, revision=revision, name=name, record_version=version)
        if snapshot["run_id"] != run_id or sha[:16] != h16:
            return add(RECORD_IDENTITY_MISMATCH, "name_mismatch", **fields)
        if record["client_key"] != self.client_key or record["report_period"] != {"start": self.start, "end": self.end}:
            return add(RECORD_IDENTITY_MISMATCH, "client_or_period_mismatch", **fields)
        if self.db_state != "available":
            return self.unavailable(self.db_reason, **fields)
        info = self.runs.get(run_id)
        if info is None:
            return add(RECORD_IDENTITY_MISMATCH, "snapshot_missing", **fields)
        if info.revision != revision:
            return add(RECORD_IDENTITY_MISMATCH, "revision_mismatch", **fields)
        if info.content_hash != snapshot["content_hash"] or info.quality != snapshot["quality"]:
            return add(RECORD_IDENTITY_MISMATCH, "snapshot_hash_mismatch", **fields)
        if info.voided:
            return add(FINALIZED_INVALID, "run_voided", **fields)
        verified = self.run_verified(run_id)
        if verified is None:
            return self.unavailable("snapshot_verification_unavailable", **fields)
        if not verified:
            return add(SNAPSHOT_VERIFICATION_FAILED, "run_failed_verification", **fields)
        # the PDF
        found_any = matched = False
        for candidate in (layout.STANDARD_PDF_NAME, *layout.variant_candidates("superseded", h16)):
            try:
                st_ = self.ops.lstat(self.reports / candidate)
            except FileNotFoundError:
                continue
            except OSError:
                return self.unavailable("pdf_unreadable", **fields)
            if ff._is_link(st_) or not stat.S_ISREG(st_.st_mode):
                continue
            found_any = True
            if st_.st_size != size:
                continue
            digest = self.digest(candidate)
            if digest is None:
                return self.unavailable("pdf_unreadable", **fields)
            if digest.sha256 == sha:
                matched = True
                break
        if not found_any:
            return add(RECORD_WITHOUT_PDF, "no_recognized_pdf", **fields)
        if not matched:
            return add(PDF_HASH_MISMATCH, "pdf_content_differs", **fields)
        # the rendered event
        events = self.run_events(run_id)
        if events is None:
            return self.unavailable("snapshot_read_failed", **fields)
        rendered = [e for e in events if e.kind == "rendered"]
        if not any(e.detail and sha in _SHA_TOKEN.findall(e.detail) for e in rendered):
            return add(EVENT_HASH_MISMATCH if rendered else FINALIZED_INVALID,
                       "event_for_other_pdf" if rendered else "rendered_event_missing", **fields)
        if run_id not in self.finalized_runs:
            return add(FINALIZED_INVALID, "reader_does_not_list_run", **fields)
        if version == 2 and not self.check_render_identity(fin, sha, size, fields):
            return
        if beyond:
            return add(FINALIZED_INVALID, "beyond_reader_limit", **fields)
        self.valid_records += 1
        self.run_has_valid_record.add(run_id)
        add(FINALIZED_VALID, "all_checks_passed", **fields)

    def check_render_identity(self, fin, sha, size, fields) -> bool:
        """Check 9 (version 2 records): the record's sidecar. True when consistent, otherwise a finding was added.

        The sidecar is looked up by the record's render-input hash and only read (nothing is created or repaired).
        Reasons are fixed codes: no sidecar content and no client data is ever copied into a finding.
        """
        add = self.found.add
        try:
            from seo_report import render_artifacts as ra  # imported here: a failure is a limit, not a crash
            from seo_report import render_input_hash as rh
        except Exception:
            self.unavailable("render_sidecar_check_unavailable", **fields)
            return False
        digest = fin["render_input_sha256"]
        name = ra.sidecar_name(digest)
        fields = dict(fields, name=name)
        try:
            info = self.ops.lstat(self.reports / name)
        except FileNotFoundError:
            add(RENDER_SIDECAR_MISSING, "no_sidecar_for_record", **fields)
            return False
        except OSError:
            self.unavailable("sidecar_unreadable", **fields)
            return False
        kind, data = _read_file(self.reports / name, self.ops, ra.MAX_SIDECAR_BYTES)
        if kind == "unreadable":
            self.unavailable("sidecar_unreadable", **fields)
            return False
        if kind != "ok":  # not_regular or too_large
            add(RENDER_SIDECAR_INVALID, kind, **fields)
            return False
        try:
            sidecar = ra.parse_sidecar(data)
        except ra.RenderArtifactError as err:
            if err.code == "hash_mismatch":
                add(RENDER_INPUT_HASH_MISMATCH, "payload_hash_mismatch", **fields)
            elif err.code == "unsupported_version" and err.group == "render_input_format":
                add(RENDER_INPUT_FORMAT_INVALID, "unsupported_format", **fields)
            elif err.code == "unsupported_version":
                add(RENDER_SIDECAR_INVALID, "unsupported_sidecar_version", **fields)
            elif err.code == "identity_mismatch":
                add(RENDER_SIDECAR_INVALID, "sidecar_identity_inconsistent", **fields)
            else:
                add(RENDER_SIDECAR_INVALID, err.code if re.fullmatch(r"[a-z_0-9]+", err.code) else "sidecar_invalid",
                    **fields)
            return False
        if sidecar.render_input_sha256 != digest:
            add(RENDER_INPUT_HASH_MISMATCH, "sidecar_hash_differs_from_record", **fields)
            return False
        # parse_sidecar accepts only the render-input format of `render_input_hash`: that is the sidecar's format
        recorded = fin["render_input_format"]
        if (recorded["name"], recorded["version"]) != (rh.FORMAT_NAME, rh.FORMAT_VERSION):
            add(RENDER_INPUT_FORMAT_INVALID, "format_differs_from_record", **fields)
            return False
        if sidecar.pdf_sha256 != sha:
            add(RECORD_IDENTITY_MISMATCH, "sidecar_pdf_hash_differs", **fields)
            return False
        if sidecar.pdf_bytes != size:
            add(RECORD_IDENTITY_MISMATCH, "sidecar_pdf_size_differs", **fields)
            return False
        return True

    def check_pdfs_have_records(self):
        if self.records_incomplete or self.records_scan_failed:
            return  # a record that could not be read might name this PDF
        for name, regular in getattr(self, "reports_listing", ()):
            is_final = name == layout.STANDARD_PDF_NAME
            try:
                is_superseded = ff.parse_variant_name(name).family == "superseded"
            except ff.FileOperationError:
                is_superseded = False
            if not (is_final or is_superseded) or not regular:
                continue
            digest = self.digest(name)
            if digest is None:
                self.unavailable("pdf_unreadable", name=name)
            elif digest.sha256 not in self.record_hashes:
                self.found.add(PDF_WITHOUT_RECORD, "no_valid_record_for_hash", name=name)

    # -- snapshots -------------------------------------------------------------------------------------------------------------------------

    def scan_snapshots(self):
        if self.db_state != "available":
            return
        live = [i for i in self.runs.values() if not i.voided]
        newest = max((i.revision for i in live), default=None)
        for info in sorted(self.runs.values(), key=lambda i: i.revision):
            fields = dict(run_id=info.run_id, revision=info.revision)
            if info.voided:
                self.found.add(VOIDED_SNAPSHOT, "voided_run", **fields)
                continue
            if info.run_id in self.run_has_valid_record or info.run_id in self.unchecked_runs \
                    or self.records_scan_failed:
                continue  # finalized, or its records could not be checked: nothing is claimed about it
            events = self.run_events(info.run_id)
            if events is None:
                self.unavailable("snapshot_read_failed", **fields)
                continue
            known = info.run_id in self.run_record_names
            reason = "record_failed_checks" if known else "no_reviewer_record"
            severity = SEVERITY_ATTENTION if info.revision == newest else SEVERITY_INFO
            self.found.add(SNAPSHOT_WITHOUT_FINALIZATION, reason if info.revision == newest else "superseded_revision",
                           severity, **fields)
            if any(e.kind == "rendered" for e in events):
                self.found.add(RENDERED_EVENT_WITHOUT_FINALIZATION, "run_not_finalized", **fields)

    # -- attempts ------------------------------------------------------------------------------------------------------------------------------

    def scan_attempts(self):
        status, items = _list_folder(self.attempts)
        if status == "unavailable":
            self.unavailable(items)
            return
        for name, regular in items:
            match = _ATTEMPT_NAME.fullmatch(name)
            if match is None:
                if ff.is_partial_name(name) or name.endswith(".partial"):
                    self.partials += 1
                    self.found.add(PARTIAL_FILE, "partial_in_attempts", name=name)
                continue
            self.attempt_total += 1
            self.check_attempt(name, match, regular)

    def check_attempt(self, name, match, regular):
        add = self.found.add
        number = match.group(4)
        if number is not None and (str(int(number)) != number
                                   or not 2 <= int(number) <= layout.MAX_ATTEMPT_COLLISION):
            return add(ATTEMPT_INVALID, "bad_collision_number", name=name)
        kind, data = _read_file(self.attempts / name, self.ops, ha.MAX_RECORD_BYTES)
        if kind == "unreadable":
            return self.unavailable("attempt_unreadable", name=name)
        if kind != "ok":
            return add(ATTEMPT_INVALID, kind, name=name)
        value, reason = _parse(data)
        if reason:
            return add(ATTEMPT_INVALID, reason, name=name)
        try:
            record = rs.validate_attempt_record(value)
        except rs.RecordSchemaError:
            return add(ATTEMPT_INVALID, "schema_invalid", name=name)
        if record["client_key"] != self.client_key or record["report_period"] != {"start": self.start,
                                                                                  "end": self.end}:
            return add(ATTEMPT_INVALID, "client_or_period_mismatch", name=name)
        try:
            expected = layout.attempt_record_name(record["attempted_at"], record["pdf_sha256"], record["step"],
                                                  1 if number is None else int(number))
        except layout.OutputLayoutError:
            return add(ATTEMPT_INVALID, "name_mismatch", name=name)
        if expected != name:
            return add(ATTEMPT_INVALID, "name_mismatch", name=name)
        self.attempt_valid += 1
        add(ATTEMPT_VALID, "collision_variant" if number else "failure_recorded", name=name,
            run_id=record["run_id"])

    # -- result --------------------------------------------------------------------------------------------------------------------------------

    def report(self) -> AuditReport:
        # nothing on disk and nothing stored is a clean, empty period; reports/records that cannot be checked are not
        findings = tuple(sorted(self.found.items, key=lambda f: (f.category, f.reason, f.run_id or "",
                                                                  f.revision or 0, f.name or "")))
        problems = sum(1 for f in findings if f.severity == SEVERITY_ATTENTION)
        limits = sum(1 for f in findings if f.severity == SEVERITY_LIMIT)
        benign = self.db_reason == "history_database_missing" and not self.has_stored_evidence()
        if self.db_state != "available" and self.db_reason and not benign and not limits:
            findings += (AuditFinding(VERIFICATION_UNAVAILABLE, self.db_reason, SEVERITY_LIMIT),)
            limits += 1
        outcome = (OUTCOME_ATTENTION if problems else OUTCOME_LIMITED if limits else OUTCOME_CONSISTENT)
        return AuditReport(
            client_key=self.client_key, period_start=self.start, period_end=self.end, outcome=outcome,
            verification_complete=limits == 0, finalized_valid=self.valid_records, unresolved=problems,
            attempt_records=self.attempt_total, attempt_records_valid=self.attempt_valid,
            partial_files=self.partials, stored_runs=len(self.runs), database_check=self.database_check,
            findings=findings, legacy_records=self.legacy_records)

    def has_stored_evidence(self) -> bool:
        """True when files exist that a history database should explain (so its absence limits the audit)."""
        names = [n for n, _ in getattr(self, "reports_listing", ())]
        return bool(self.run_record_names) or any(n == layout.STANDARD_PDF_NAME or n.startswith("report.superseded.")
                                                  for n in names)
