"""Append-only attempt records for report finalization (Stage 6c-8c).

An attempt record says that a finalization attempt stopped, at which step, and why (an approved status code). It is
written to `attempts/<client_key>/<period>/` and is never read by the history filter. Records exist only for
attempts that failed or were stopped: the schema has no "succeeded" status, and a successful finalization is shown
by its finalized reviewer record instead.

  build_attempt_record   a strict, validated record dictionary (the schema's 14 fields, nothing else)
  safe_exception_class   the class name of an exception, never its message
  attempt_file_name      the approved name: `<timestamp>.<pdf16|nopdf>.<step>.attempt[.<n>].json`
  write_attempt_record   validate, serialize and create the file exclusively

Everything comes from the existing schema and layout:
  fields   record_schema.ATTEMPT_FIELDS (schema_version, record_type, client_key, report_period, attempted_at, step,
           status_code, exception_class, run_id, pdf_sha256, pdf_bytes, previous_report_restored,
           previous_report_preserved_as, finalized)
  steps    record_schema.ATTEMPT_STEPS (7)         status codes  record_schema.ATTEMPT_STATUS_CODES (15)
  names    output_layout.attempt_record_name / attempt_dir (collision numbers 2 to 999)
Nothing is invented and nothing is relaxed: the record is checked with `validate_attempt_record`, which holds no
free text (an exception is only a class-name identifier), so a message, path, address or secret has no field to
live in.

Writing: the record is validated, then serialized with the reviewer-record canonical JSON, then created with
`finalize_files.write_exclusive`. A name that exists (including a racing writer's file or an incomplete record from
a crash) moves on to the next collision number; nothing is overwritten, deleted, repaired or adopted. A failure
while writing leaves the incomplete file in place and raises the file primitive's error with `left_behind` set; the
next attempt then takes the next number. The attempt folder must already exist: no folder is created.

Pure apart from that one file creation: no clock (the caller supplies `attempted_at`), no database, no network.
"""

import copy
import datetime as dt
from dataclasses import dataclass

from seo_report import finalize_files as ff
from seo_report import output_layout as layout
from seo_report import record_schema as rs
from seo_report.reviewer_record import to_canonical_json

STEPS = rs.ATTEMPT_STEPS
STATUS_CODES = rs.ATTEMPT_STATUS_CODES
MAX_COLLISION = layout.MAX_ATTEMPT_COLLISION
FALLBACK_EXCEPTION_CLASS = "Exception"


class AttemptRecordError(ValueError):
    """The attempt record is not valid. The message names a field, never a value that could be sensitive."""


# --- building ---------------------------------------------------------------------------------------------------------------

_SKELETON = {
    "schema_version": rs.SCHEMA_VERSION, "record_type": rs.ATTEMPT_TYPE, "client_key": "client",
    "report_period": {"start": "2026-01-01", "end": "2026-01-31"}, "attempted_at": "2026-01-01T00:00:00+00:00",
    "step": "preflight", "status_code": "draft_invalid", "exception_class": None, "run_id": None, "pdf_sha256": None,
    "pdf_bytes": None, "previous_report_restored": None, "previous_report_preserved_as": None, "finalized": False}


def safe_exception_class(error) -> str:
    """The class name of an exception, and only that: never its message. A class name the schema cannot hold (too
    long, non-ASCII) is replaced by "Exception" rather than stored."""
    if not isinstance(error, BaseException):
        raise TypeError("an exception instance is required")
    name = type(error).__name__
    try:
        rs.validate_attempt_record(dict(_SKELETON, exception_class=name))
    except rs.RecordSchemaError:
        return FALLBACK_EXCEPTION_CLASS
    return name

def _period_dict(report_period) -> dict:
    if isinstance(report_period, dict):
        start, end = report_period.get("start"), report_period.get("end")
        if set(report_period) != {"start", "end"}:
            raise AttemptRecordError("report_period: must have exactly start and end")
    elif isinstance(report_period, (tuple, list)) and len(report_period) == 2:
        start, end = report_period
    else:
        raise AttemptRecordError("report_period: must be {start, end} or a (start, end) pair")
    return {"start": _iso(start), "end": _iso(end)}


def _iso(value):
    if isinstance(value, dt.datetime):
        raise AttemptRecordError("report_period: dates, not datetimes")
    return value.isoformat() if isinstance(value, dt.date) else value


def build_attempt_record(*, client_key, report_period, attempted_at, step, status_code, exception_class=None,
                         run_id=None, pdf_sha256=None, pdf_bytes=None, previous_report_restored=None,
                         previous_report_preserved_as=None) -> dict:
    """A validated attempt record (a new dictionary; the arguments are not changed).

    `report_period` is {"start", "end"} or a (start, end) pair of dates or ISO text. `exception_class` is a class
    name or None (see `safe_exception_class`). `previous_report_preserved_as` is None or
    {"sha256_16", "variant_index"}. `finalized` is always False. Anything the schema rejects raises
    AttemptRecordError: nothing is repaired or defaulted beyond the schema's own nulls.
    """
    record = {
        "schema_version": rs.SCHEMA_VERSION,
        "record_type": rs.ATTEMPT_TYPE,
        "client_key": client_key,
        "report_period": _period_dict(report_period),
        "attempted_at": attempted_at,
        "step": step,
        "status_code": status_code,
        "exception_class": exception_class,
        "run_id": run_id,
        "pdf_sha256": pdf_sha256,
        "pdf_bytes": pdf_bytes,
        "previous_report_restored": previous_report_restored,
        "previous_report_preserved_as": copy.deepcopy(previous_report_preserved_as),
        "finalized": False,
    }
    return validated(record)


def validated(record) -> dict:
    """The record itself if the real attempt-record validator accepts it, otherwise AttemptRecordError."""
    try:
        return rs.validate_attempt_record(record)
    except rs.RecordSchemaError as err:
        raise AttemptRecordError(f"the attempt record is not valid: {err}") from None


# --- naming and writing -----------------------------------------------------------------------------------------------------

def attempt_file_name(record: dict, collision: int = 1) -> str:
    """`<timestamp>.<pdf16|nopdf>.<step>.attempt[.<n>].json`, from the approved layout helper."""
    validated(record)
    try:
        return layout.attempt_record_name(record["attempted_at"], record["pdf_sha256"], record["step"], collision)
    except layout.OutputLayoutError as err:
        raise AttemptRecordError(f"no valid attempt file name: {err}") from None


def attempt_directory(output_root, record: dict):
    """The approved folder for this record (`<root>/attempts/<client_key>/<period>`); nothing is created."""
    validated(record)
    period = record["report_period"]
    try:
        return layout.attempt_dir(output_root, record["client_key"], period["start"], period["end"])
    except layout.OutputLayoutError as err:
        raise AttemptRecordError(f"no valid attempt folder: {err}") from None


@dataclass(frozen=True)
class AttemptWriteResult:
    name: str  # file name only, never a path
    collision: int  # 1 for the plain name, 2 and up when earlier files already existed
    byte_count: int
    sha256: str


def write_attempt_record(record: dict, *, output_root, ops: ff.FileOps | None = None) -> AttemptWriteResult:
    """Validate `record` and create its file in the attempts folder, never overwriting anything.

    Raises AttemptRecordError (nothing is created) for an invalid record or an unusable location,
    `finalize_files.FileOperationError` when the folder is missing or a write fails (`left_behind` tells whether an
    incomplete file remains), and `finalize_files.FileConflictError` when all 999 names are taken.
    """
    record = validated(record)
    folder = attempt_directory(output_root, record)
    data = (to_canonical_json(record) + "\n").encode("utf-8")
    for collision in range(1, MAX_COLLISION + 1):
        name = attempt_file_name(record, collision)
        try:
            written = ff.write_exclusive(folder, name, data, ops=ops)
        except ff.FileConflictError as err:
            if err.kind != "destination_exists":
                raise
            continue  # the name is taken (an earlier attempt, a crash leftover or a racing writer): use the next
        return AttemptWriteResult(name, collision, written.byte_count, written.sha256)
    raise ff.FileConflictError(f"all {MAX_COLLISION} attempt file names are taken", kind="variants_exhausted",
                               operation="create")
