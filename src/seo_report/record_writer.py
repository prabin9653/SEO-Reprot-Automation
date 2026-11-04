"""Writing a finalized reviewer record to disk, safely and idempotently (Stage 6c-8e).

`write_reviewer_record(directory, record)` persists one already-built finalized reviewer record in a period's
reviewer folder (`output_layout.reviewer_dir(...)`, which must already exist). It builds nothing, repairs nothing and
changes no field: the record is validated with `record_schema.validate_finalized_record`, serialized with the
reviewer-record canonical JSON (plus one trailing newline, like the attempt records), and created without ever
overwriting a file.

Name and identity
  File name   `output_layout.reviewer_record_name(run_id, pdf_sha256)` = `reviewer.<run_id>.<pdf16>.json`, from the
              record's own `snapshot.run_id` and `finalization.pdf_sha256`. This is exactly the pattern the
              finalized-only reader scans for.
  Identity    snapshot.run_id, snapshot.revision, snapshot.content_hash, finalization.pdf_sha256,
              finalization.pdf_bytes, finalization.client_content_sha256; for a schema version 2 record also
              finalization.render_input_sha256 and the render-input format name and version. `finalized_at` and every
              other field are not compared: a record written again later with a new timestamp is the same record.
              A version 1 and a version 2 record never have the same identity.

Outcomes (`RecordWriteResult.outcome`)
  written          this call created the file
  already_written  the target already holds a valid record with the same identity; nothing was written
  conflict         `record_identity_differs`, `existing_record_invalid`, `existing_record_not_regular` or
                   `reader_record_limit` (nothing at the target was changed)
  failed           an operating-system failure with a fixed category (`directory_unusable`, `scan_failed`,
                   `existing_record_unreadable`, `create_failed`, `write_failed` (includes the flush), `close_failed`,
                   `promote_failed`)
  invalid_record   the record is not a valid finalized reviewer record (`record_invalid`, `record_too_large`)

Order of work
  1. validate, derive the name, serialize (no file is touched until this succeeds);
  2. if the target exists: it must be a regular, non-link, size-limited file holding strictly parsed (no duplicate
     keys, no NaN/Infinity), schema-valid JSON with the same identity, else a conflict. It is never overwritten,
     deleted, repaired or adopted;
  3. otherwise count the run's reviewer records and stop at 20 (the finalized-only reader looks at no more than 20);
  4. write the bytes to a new unique partial file (`finalize_files.partial_name`; never a reader-eligible name) with
     exclusive creation, flush and close;
  5. immediately before promotion check the target and the count again (a record that appeared meanwhile is
     reconciled by the same identity rules, or stops at the limit);
  6. rename the partial file to the final name without replacing anything. If the name appeared at the last moment
     it is reconciled again.

Reader limit
  The reader considers files named `reviewer.<this run_id>.<16 hex>.json` that are regular files (not links), counted
  in the folder whether or not their content is valid. The same rule is used here. Partial files, other runs' records
  and other names do not count. With 19 existing records a 20th may be written; with 20 a new name is refused
  (`reader_record_limit`). Only the supplied folder is read, and nothing is renamed or removed to make room.

Failures and partial files
  A failed write or promotion leaves its partial file in place (`partial_left_behind`, `partial_name`, a file name
  only). Stale partial files are never deleted, adopted or counted; a retry writes a fresh partial under a new name.

Results hold fixed categories, file names and the record's hashes only: no paths, no operating-system text, no
exception messages, no record content.
"""

import copy
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from seo_report import finalize_files as ff
from seo_report import output_layout as layout
from seo_report import record_schema as rs
from seo_report.reviewer_record import ReviewerRecordError, to_canonical_json

MAX_RECORD_BYTES = 4 * 1024 * 1024  # the reader's own limit (history_access.MAX_RECORD_BYTES)
MAX_RECORDS_PER_RUN = 20  # the reader's own limit (history_access.MAX_RECORD_CANDIDATES)

OUTCOME_WRITTEN = "written"
OUTCOME_ALREADY = "already_written"
OUTCOME_CONFLICT = "conflict"
OUTCOME_FAILED = "failed"
OUTCOME_INVALID = "invalid_record"
OUTCOMES = (OUTCOME_WRITTEN, OUTCOME_ALREADY, OUTCOME_CONFLICT, OUTCOME_FAILED, OUTCOME_INVALID)

CONFLICT_CATEGORIES = ("record_identity_differs", "existing_record_invalid", "existing_record_not_regular",
                       "reader_record_limit")
FAILURE_CATEGORIES = ("directory_unusable", "scan_failed", "existing_record_unreadable", "create_failed",
                      "write_failed", "close_failed", "promote_failed")
INVALID_CATEGORIES = ("record_invalid", "record_too_large")

IDENTITY_FIELDS = (("snapshot", "run_id"), ("snapshot", "revision"), ("snapshot", "content_hash"),
                   ("finalization", "pdf_sha256"), ("finalization", "pdf_bytes"),
                   ("finalization", "client_content_sha256"))

_OPERATION_FAILURE = {"create": "create_failed", "write": "write_failed", "close": "close_failed",
                      "rename": "promote_failed"}


@dataclass(frozen=True)
class RecordWriteResult:
    outcome: str  # one of OUTCOMES
    written: bool  # this call created the record file
    name: str | None  # `reviewer.<run_id>.<pdf16>.json`
    run_id: str | None
    record_sha256: str | None  # SHA-256 of the bytes this call would write / wrote
    byte_count: int | None
    conflict: str | None = None  # one of CONFLICT_CATEGORIES
    failure: str | None = None  # one of FAILURE_CATEGORIES
    invalid: str | None = None  # one of INVALID_CATEGORIES
    existing_records: int | None = None  # the run's record count when it was last checked
    partial_name: str | None = None  # a partial file this call created and left in place
    partial_left_behind: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome in (OUTCOME_WRITTEN, OUTCOME_ALREADY)


# --- identity and parsing -----------------------------------------------------------------------------------------------------

def record_identity(record: dict) -> tuple:
    """The identity of a validated finalized record (never `finalized_at`).

    Version 1: exactly the six `IDENTITY_FIELDS` values. Version 2: those six, then the render-input hash and the
    render-input format name and version (nine values), so a version 2 identity never equals a version 1 identity and
    records with different render-input hashes are never the same record.
    """
    identity = tuple(record[section][field] for section, field in IDENTITY_FIELDS)
    if record["schema_version"] == 2:
        fin = record["finalization"]
        identity += (fin["render_input_sha256"], fin["render_input_format"]["name"],
                     fin["render_input_format"]["version"])
    return identity


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


def parse_record_bytes(data: bytes):
    """A schema-valid finalized record from file bytes, or None. Duplicate keys, NaN/Infinity, bad UTF-8, invalid JSON
    and schema violations all give None."""
    try:
        record = json.loads(data.decode("utf-8"), object_pairs_hook=_strict_object, parse_constant=_refuse_constant)
        return rs.validate_finalized_record(record)
    except (UnicodeDecodeError, ValueError, RecursionError, rs.RecordSchemaError, TypeError):
        return None


# --- reading what is already there --------------------------------------------------------------------------------------------------

def _read_existing(path, st, ops):
    """('ok', record) | ('not_regular',) | ('unreadable',) | ('invalid',)"""
    if ff._is_link(st) or not stat.S_ISREG(st.st_mode):
        return ("not_regular", None)
    if st.st_size > MAX_RECORD_BYTES:
        return ("invalid", None)
    try:
        with ops.open_read(path) as handle:
            data = handle.read(MAX_RECORD_BYTES + 1)
    except OSError:
        return ("unreadable", None)
    record = parse_record_bytes(data) if len(data) <= MAX_RECORD_BYTES else None
    return ("ok", record) if record is not None else ("invalid", None)


def count_run_records(directory, run_id: str) -> int:
    """How many files the finalized-only reader would see as this run's reviewer records: regular files (not links)
    named `reviewer.<run_id>.<16 hex>.json`, valid or not. Partial files never match. Raises OSError."""
    pattern = re.compile(rf"reviewer\.{re.escape(run_id)}\.[0-9a-f]{{16}}\.json")
    count = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if pattern.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                count += 1
    return count


# --- the operation ------------------------------------------------------------------------------------------------------------------------

def write_reviewer_record(directory, record, *, ops: ff.FileOps | None = None) -> RecordWriteResult:
    """Write `record` as `reviewer.<run_id>.<pdf16>.json` in `directory`, following the rules in the module docstring.

    `directory` is the period's reviewer folder (absolute, existing). The record is not changed. Returns a
    RecordWriteResult for every outcome, including invalid input, conflicts and failures.
    """
    ops = ops or ff.FileOps()

    def invalid(category):
        return RecordWriteResult(OUTCOME_INVALID, False, None, None, None, None, invalid=category)

    # 1. validate, name, serialize
    try:
        checked = rs.validate_finalized_record(copy.deepcopy(record))
        identity = record_identity(checked)
        name = layout.reviewer_record_name(checked["snapshot"]["run_id"], checked["finalization"]["pdf_sha256"])
        data = (to_canonical_json(checked) + "\n").encode("utf-8")
    except (rs.RecordSchemaError, ReviewerRecordError, layout.OutputLayoutError, TypeError, ValueError, KeyError,
            RecursionError):
        return invalid("record_invalid")
    run_id = checked["snapshot"]["run_id"]
    if len(data) > MAX_RECORD_BYTES:
        return invalid("record_too_large")
    digest = hashlib.sha256(data).hexdigest()

    def result(outcome, **fields):
        return RecordWriteResult(outcome, outcome == OUTCOME_WRITTEN, name, run_id, digest, len(data), **fields)

    try:
        target = ff._location(directory, name, ops)
    except ff.FileOperationError:
        return result(OUTCOME_FAILED, failure="directory_unusable")

    def reconcile(counted=None, partial=None):
        """The target exists: already written (same identity) or a conflict. Never changes it."""
        extra = {"existing_records": counted, "partial_name": partial, "partial_left_behind": partial is not None}
        try:
            st = ops.lstat(target)
        except OSError:
            return result(OUTCOME_FAILED, failure="existing_record_unreadable", **extra)
        kind, existing = _read_existing(target, st, ops)
        if kind == "not_regular":
            return result(OUTCOME_CONFLICT, conflict="existing_record_not_regular", **extra)
        if kind == "unreadable":
            return result(OUTCOME_FAILED, failure="existing_record_unreadable", **extra)
        if kind == "invalid":
            return result(OUTCOME_CONFLICT, conflict="existing_record_invalid", **extra)
        if record_identity(existing) == identity:
            return result(OUTCOME_ALREADY, **extra)
        return result(OUTCOME_CONFLICT, conflict="record_identity_differs", **extra)

    def exists():
        try:
            ops.lstat(target)
        except FileNotFoundError:
            return False
        return True

    # 2. an existing target
    try:
        if exists():
            return reconcile()
    except OSError:
        return result(OUTCOME_FAILED, failure="existing_record_unreadable")

    # 3. the reader limit
    def limit_reached():
        return count_run_records(directory, run_id)

    try:
        counted = limit_reached()
    except OSError:
        return result(OUTCOME_FAILED, failure="scan_failed")
    if counted >= MAX_RECORDS_PER_RUN:
        return result(OUTCOME_CONFLICT, conflict="reader_record_limit", existing_records=counted)

    # 4. the partial file
    partial = ff.partial_name(name, ff.new_nonce())
    try:
        ff.write_exclusive(directory, partial, data, ops=ops)
    except ff.FileConflictError:  # an unrelated file with this unique name: nothing was created by this call
        return result(OUTCOME_FAILED, failure="create_failed", existing_records=counted)
    except ff.FileOperationError as err:
        return result(OUTCOME_FAILED, failure=_OPERATION_FAILURE.get(err.operation, "write_failed"),
                      existing_records=counted, partial_name=partial if err.left_behind else None,
                      partial_left_behind=err.left_behind)

    # 5. the last checks before promotion
    try:
        if exists():
            return reconcile(counted, partial)
    except OSError:
        return result(OUTCOME_FAILED, failure="existing_record_unreadable", existing_records=counted,
                      partial_name=partial, partial_left_behind=True)
    try:
        counted = limit_reached()
    except OSError:
        return result(OUTCOME_FAILED, failure="scan_failed", partial_name=partial, partial_left_behind=True)
    if counted >= MAX_RECORDS_PER_RUN:
        return result(OUTCOME_CONFLICT, conflict="reader_record_limit", existing_records=counted,
                      partial_name=partial, partial_left_behind=True)

    # 6. promotion without replacing
    try:
        ff.rename_noreplace(directory, partial, name, ops=ops)
    except ff.FileConflictError:
        return reconcile(counted, partial)
    except ff.FileOperationError:
        return result(OUTCOME_FAILED, failure="promote_failed", existing_records=counted, partial_name=partial,
                      partial_left_behind=True)
    return result(OUTCOME_WRITTEN, existing_records=counted)
