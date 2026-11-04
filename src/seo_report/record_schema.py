"""Strict validation of the two reviewer-side record types, as approved in Stage 6c Plan Revision 4, section 5.

    finalized_report  the immutable record of a report that completed every lifecycle step
    attempt           the record of a run that failed or was interrupted; never a finalized report

Both are closed schemas: every field is required, unknown fields are refused, and nothing is repaired,
defaulted, trimmed or normalized. A validator returns the record it was given (the same object, unchanged)
or raises RecordSchemaError naming the offending field. Client-facing text is kept exactly as supplied.

Revision 4 leaves the inside of several `history` parts as "...". They are validated against the structures the
existing Stage 6b code produces (HistoryConfig, BaselineRef, SkippedRun, DimensionStatus, RankingComparison
counts, snapshot diagnostics) and the earlier approved plan drafts:

    history.baselines   serializes dimension, run_id, revision, period_start, period_end, gap_days, rank and
                        same_length. BaselineRef.created_at (the store's own timestamp) and BaselineRef.kind (a
                        constant, "previous_report") are NOT serialized: neither is in the plan draft's field
                        list and neither is needed to identify the baseline.

`recommendation_annotations[].history_support` is a list of at most five closed items, as produced by
insights.history_recommendations (Stage 6c-6): classification, source, subject, dimension, baseline_run_id,
baseline_period, evidence and rules_version. An empty list means "no usable historical context" and stays valid.
The evidence of an item may use exactly the keys that its source produces (SUPPORT_EVIDENCE_KEYS), all of which
belong to the Stage 6c-4 evidence registry. Nothing is deferred any more (DEFERRED_CONTRACTS is empty).

`validation_failures` is a list of `{key, reason}` entries. `reason` is one of the seven codes that Stage 6c-4
defined (VALIDATION_FAILURE_REASONS); `key` is the non-empty key of the rejected historical statement.

File names found inside a record are never used to build a path. This module does no file, database or
network access.
"""

import datetime as dt
import math
import re
import uuid

from seo_report.client_config import ClientConfigError, validate_client_key
from seo_report.output_layout import ATTEMPT_STEPS, OutputLayoutError, hash16_of, validate_period_date, validate_sha256
from seo_report.insights.history_rules import EVIDENCE_KEYS
from seo_report.snapshots.eligibility import DIMENSIONS

SCHEMA_VERSION = 1  # the original version; attempt records and version 1 finalized records use it
# Finalized-report records exist in two versions. Version 1 carries `client_content_sha256` only; version 2 adds the
# render-input identity (`finalization.render_input_sha256` and `finalization.render_input_format`). Attempt records
# stay at version 1. The record schema version is not the render-input format version.
FINALIZED_SCHEMA_VERSIONS = (1, 2)
CURRENT_FINALIZED_SCHEMA_VERSION = 2
ATTEMPT_SCHEMA_VERSION = 1
# The format of the render-input hash (`render_input_hash.FORMAT_NAME` / `FORMAT_VERSION`). Kept as literals so that
# validating an old record never depends on the render stack; a test pins them to render_input_hash. A record finalized
# under format 2 stays valid after the hash format moves on, so the supported versions are a list, not "the current one".
RENDER_INPUT_FORMAT_NAME = "jasper.render-input"
SUPPORTED_RENDER_INPUT_VERSIONS = (2,)
FINALIZED_TYPE = "finalized_report"
ATTEMPT_TYPE = "attempt"

# Revision 4, section 5.
ATTEMPT_STATUS_CODES = ("draft_invalid", "output_unavailable", "persist_failed", "event_failed", "supersede_failed",
                        "superseded_name_conflict", "superseded_variants_exhausted", "duplicate_draft_conflict",
                        "promote_failed", "reviewer_record_conflict", "record_failed",
                        # Stage 6c-8: reconciliation before finalization
                        "stale_bundle", "draft_partial_conflict", "existing_report_conflict", "reader_record_limit")
# Revision 4, section 2: the reasons an unfinalized run is excluded from history.
EXCLUSION_REASON_CODES = ("no_rendered_event", "no_reviewer_record", "record_invalid", "record_mismatch",
                          "final_pdf_missing", "pdf_hash_mismatch")
# Verified in code: Snapshot.quality, insights.rules.ReviewerNote and the history stage's own notes.
QUALITY_LEVELS = ("complete", "partial", "incomplete")
NOTE_SEVERITIES = ("info", "warning")
# "engine" and "recommendations" are the sources of the notes the insight engine and the recommendation selector write.
NOTE_SOURCES = ("rankings", "gsc", "geography", "ga4", "history", "engine", "recommendations")
NOTE_CATEGORIES = ("suppressed", "data_quality", "diagnostic", "limitation")

MIN_VARIANT_INDEX = 1
MAX_VARIANT_INDEX = 99
MAX_PDF_BYTES = 1 << 30
MAX_REVISION = 1_000_000
MAX_LIST_ITEMS = 1000
MAX_TEXT_CHARS = 5000
MAX_KEYWORD_CHARS = 300
MAX_LABEL_CHARS = 100
MAX_STATUS_TEXT_CHARS = 1000
MAX_ANNOTATIONS = 5  # Revision 4: at most five recommendations
_NO_UPPER_LIMIT = 2**63 - 1  # a type sanity bound where the existing code states no maximum

DEFERRED_CONTRACTS = {}  # Stage 6c-6 defined history_support; nothing is deferred now

# Stage 6c-6: the recommendation history support item (insights.history_recommendations is the producer).
SUPPORT_CLASSIFICATIONS = ("supports", "qualifies")
SUPPORT_SOURCES = ("gsc_position", "gsc_ctr", "ga4_engagement_rate", "ranking_keyword")
SUPPORT_SOURCE_DIMENSIONS = {"gsc_position": "gsc.totals", "gsc_ctr": "gsc.totals",
                             "ga4_engagement_rate": "ga4.site", "ranking_keyword": "rankings.keywords"}
SUPPORT_FIELDS = ("classification", "source", "subject", "dimension", "baseline_run_id", "baseline_period",
                  "evidence", "rules_version")
SUPPORT_EVIDENCE_KEYS = {
    "gsc_position": ("current_value", "baseline_value", "direction", "baseline_days"),
    "gsc_ctr": ("current_value", "baseline_value", "direction", "baseline_days"),
    "ga4_engagement_rate": ("current_value", "baseline_value", "direction", "baseline_days"),
    "ranking_keyword": ("baseline_value", "baseline_days"),
}
MAX_SUPPORT_ITEMS = 5

# Stage 6c-4: the reasons a historical statement can be rejected (insights.history_rules.VALIDATION_REASONS).
VALIDATION_FAILURE_REASONS = ("invalid_evidence", "untraced_number", "unsafe_wording", "missing_baseline_period",
                              "duplicate_of_base_insight", "inconsistent_comparison", "invalid_identity")

FINALIZED_FIELDS = (
    "schema_version", "record_type", "client_key", "report_period", "comparison_period", "snapshot",
    "finalization", "history", "client_facing_history", "ranking_table_markers", "recommendation_annotations",
    "notes", "validation_failures")
SNAPSHOT_FIELDS = ("run_id", "revision", "content_hash", "quality")
FINALIZATION_FIELDS = (
    "finalized_at", "pdf_sha256", "pdf_bytes", "pdf_checks", "client_content_sha256", "rendered_event",
    "superseded_previous_pdf", "duplicate_draft_preserved_as")
FINALIZATION_FIELDS_V2 = FINALIZATION_FIELDS + ("render_input_sha256", "render_input_format")
RENDER_INPUT_FORMAT_FIELDS = ("name", "version")
PDF_CHECK_FIELDS = ("non_empty", "header", "eof_marker", "preliminary")
RENDERED_EVENT_FIELDS = ("recorded", "detail")
PRESERVED_AS_FIELDS = ("sha256_16", "variant_index")
HISTORY_FIELDS = ("status", "failure_kind", "rules_version", "config", "baselines", "skipped_runs",
                  "excluded_unfinalized", "dimension_status", "ranking_crosscheck", "diagnostics")
CONFIG_FIELDS = ("lookback_periods", "max_gap_days", "list_limit")
BASELINE_FIELDS = ("dimension", "run_id", "revision", "period_start", "period_end", "gap_days", "rank",
                   "same_length")
SKIPPED_FIELDS = ("run_id", "revision", "period_start", "period_end", "reason")
STATUS_FIELDS = ("dimension", "eligible", "reasons", "caveats", "baseline_run_id")
CROSSCHECK_FIELDS = ("fallback_used", "sheet_previous_mismatch", "new_keywords", "no_longer_tracked",
                     "not_comparable")
DIAGNOSTICS_FIELDS = ("current", "baselines")
DIAGNOSTIC_WHICH = ("current", "previous")
ANNOTATION_FIELDS = ("recommendation_key", "history_support")
EXCLUDED_FIELDS = ("run_id", "revision", "period_start", "period_end", "reason_code")
CLIENT_HISTORY_FIELDS = ("section", "key", "dimension", "text", "baseline_period", "evidence", "rules_version")
MARKERS_FIELDS = ("footnote_text", "cells")
MARKER_CELL_FIELDS = ("keyword", "target_location", "text", "source", "baseline_period")
VALIDATION_FAILURE_FIELDS = ("key", "reason")
NOTE_FIELDS = ("id", "severity", "source", "category", "message", "audience")
ATTEMPT_FIELDS = (
    "schema_version", "record_type", "client_key", "report_period", "attempted_at", "step", "status_code",
    "exception_class", "run_id", "pdf_sha256", "pdf_bytes", "previous_report_restored",
    "previous_report_preserved_as", "finalized")

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", re.ASCII)
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_EXCEPTION_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# The reasons snapshots.baselines.plan_candidates and snapshots.history.compare_with_history give for skipping a run.
_SKIP_REASON_PATTERNS = tuple(re.compile(pattern, re.ASCII) for pattern in (
    r"voided",
    r"superseded by revision \d+",
    r"same period as the current report",
    r"overlaps the current period",
    r"later than the current period",
    r"outside the lookback of \d+ earlier periods",
    r"a gap of \d+ days exceeds the \d+-day limit",
    r"failed verification \(\d+ problem\(s\)\); not used",
    r"unsupported snapshot format",
))


class RecordSchemaError(ValueError):
    """A reviewer record does not match its schema."""


# --- primitives ---------------------------------------------------------------------------------------------------

def _fail(where: str, problem: str):
    raise RecordSchemaError(f"{where}: {problem}")


def _mapping(value, where: str, fields) -> dict:
    if not isinstance(value, dict):
        _fail(where, f"must be an object, got {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            _fail(where, f"has a non-text field name {key!r}")
    missing = [name for name in fields if name not in value]
    if missing:
        _fail(where, f"is missing required field(s) {missing}")
    extra = sorted(key for key in value if key not in fields)
    if extra:
        _fail(where, f"has unexpected field(s) {extra}")
    return value


def _bool(value, where: str) -> bool:
    if not isinstance(value, bool):
        _fail(where, f"must be true or false, got {type(value).__name__}")
    return value


def _true(value, where: str) -> bool:
    if _bool(value, where) is not True:
        _fail(where, "must be true")
    return value


def _int(value, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(where, f"must be a whole number, got {type(value).__name__}")
    if not low <= value <= high:
        _fail(where, f"must be between {low} and {high}, got {value}")
    return value


def _choice(value, where: str, allowed) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(where, f"must be one of {list(allowed)}, got {value!r}")
    return value


def _list(value, where: str, max_items: int = MAX_LIST_ITEMS) -> list:
    if not isinstance(value, list):
        _fail(where, f"must be a list, got {type(value).__name__}")
    if len(value) > max_items:
        _fail(where, f"has {len(value)} items; at most {max_items} are allowed")
    return value


def _exact_text(value, where: str, *, max_chars: int = MAX_TEXT_CHARS, allow_empty: bool = False) -> str:
    """Text that is stored exactly as given: only its type, emptiness and length are checked."""
    if not isinstance(value, str):
        _fail(where, f"must be text, got {type(value).__name__}")
    if not value and not allow_empty:
        _fail(where, "must not be empty")
    if len(value) > max_chars:
        _fail(where, f"is longer than {max_chars} characters")
    return value


def _label(value, where: str) -> str:
    return _exact_text(value, where, max_chars=MAX_LABEL_CHARS)


def _sha256(value, where: str) -> str:
    try:
        return validate_sha256(value, where=where)
    except OutputLayoutError as err:
        raise RecordSchemaError(str(err)) from None


def _date(value, where: str) -> str:
    if not isinstance(value, str):
        _fail(where, f"must be a YYYY-MM-DD date, got {type(value).__name__}")
    try:
        return validate_period_date(value, where=where)
    except OutputLayoutError as err:
        raise RecordSchemaError(str(err)) from None


def _timestamp(value, where: str) -> str:
    """UTC timestamp written as YYYY-MM-DDTHH:MM:SS+00:00, the snapshot store's format (assumed)."""
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        _fail(where, "must be a UTC timestamp like 2026-01-31T09:30:00+00:00")
    try:
        dt.datetime.fromisoformat(value)
    except ValueError:
        _fail(where, f"{value!r} is not a real date and time")
    return value


def _uuid(value, where: str) -> str:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        _fail(where, "must be a lowercase hyphenated UUID")
    try:
        uuid.UUID(value)
    except ValueError:
        _fail(where, f"{value!r} is not a valid UUID")
    return value


def _client_key(value, where: str) -> str:
    try:
        return validate_client_key(value, where=where)
    except ClientConfigError as err:
        raise RecordSchemaError(str(err)) from None


def _schema_version(value, where: str = "schema_version", allowed=(ATTEMPT_SCHEMA_VERSION,)) -> int:
    """A whole number from `allowed`: booleans, floats such as 1.0 and text such as "1" are refused."""
    if isinstance(value, bool) or not isinstance(value, int) or value not in allowed:
        supported = allowed[0] if len(allowed) == 1 else list(allowed)
        _fail(where, f"must be {supported} (the supported version{'s' if len(allowed) > 1 else ''}), got {value!r}")
    return value


def record_version(record) -> int:
    """The schema version of a record (validated or not); raises RecordSchemaError when it is not a supported one."""
    if not isinstance(record, dict):
        _fail("record", f"must be an object, got {type(record).__name__}")
    return _schema_version(record.get("schema_version"), "schema_version", FINALIZED_SCHEMA_VERSIONS)


def _render_input_format(value, where: str) -> dict:
    """`{name, version}`: the canonical form the render-input hash was computed in (not the record's schema version)."""
    _mapping(value, where, RENDER_INPUT_FORMAT_FIELDS)
    _choice(value["name"], f"{where}.name", (RENDER_INPUT_FORMAT_NAME,))
    version = value["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version not in SUPPORTED_RENDER_INPUT_VERSIONS:
        _fail(f"{where}.version", f"must be one of {list(SUPPORTED_RENDER_INPUT_VERSIONS)} (a whole number), "
                                  f"got {version!r}")
    return value


def _period(value, where: str) -> dict:
    _mapping(value, where, ("start", "end"))
    start, end = _date(value["start"], f"{where}.start"), _date(value["end"], f"{where}.end")
    if start > end:
        _fail(where, f"start {start} is after end {end}")
    return value


def _rules_version(value, where: str):
    if isinstance(value, str):
        return _exact_text(value, where, max_chars=64)
    return _int(value, where, 1, 1_000_000)


def _preserved_as(value, where: str) -> dict:
    """`{sha256_16, variant_index}`: a preserved report named by hash and variant number, never by path."""
    _mapping(value, where, PRESERVED_AS_FIELDS)
    digest = value["sha256_16"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{16}", digest):
        _fail(f"{where}.sha256_16", "must be exactly 16 lowercase hexadecimal characters")
    _int(value["variant_index"], f"{where}.variant_index", MIN_VARIANT_INDEX, MAX_VARIANT_INDEX)
    return value


# --- finalized report record --------------------------------------------------------------------------------------

def _snapshot_section(value, where: str) -> dict:
    _mapping(value, where, SNAPSHOT_FIELDS)
    _uuid(value["run_id"], f"{where}.run_id")
    _int(value["revision"], f"{where}.revision", 1, MAX_REVISION)
    _sha256(value["content_hash"], f"{where}.content_hash")
    _choice(value["quality"], f"{where}.quality", QUALITY_LEVELS)
    return value


def _finalization_section(value, where: str, version: int = 1) -> dict:
    fields = FINALIZATION_FIELDS_V2 if version == 2 else FINALIZATION_FIELDS
    try:
        _mapping(value, where, fields)
    except RecordSchemaError as err:
        raise RecordSchemaError(f"{err} (schema_version {version} finalization fields)") from None
    _timestamp(value["finalized_at"], f"{where}.finalized_at")
    pdf_sha = _sha256(value["pdf_sha256"], f"{where}.pdf_sha256")
    _int(value["pdf_bytes"], f"{where}.pdf_bytes", 1, MAX_PDF_BYTES)  # non_empty is true, so at least 1 byte

    checks = _mapping(value["pdf_checks"], f"{where}.pdf_checks", PDF_CHECK_FIELDS)
    for name in PDF_CHECK_FIELDS:  # a draft that failed a check is never finalized
        _true(checks[name], f"{where}.pdf_checks.{name}")

    _sha256(value["client_content_sha256"], f"{where}.client_content_sha256")
    if version == 2:
        _sha256(value["render_input_sha256"], f"{where}.render_input_sha256")
        _render_input_format(value["render_input_format"], f"{where}.render_input_format")

    event = _mapping(value["rendered_event"], f"{where}.rendered_event", RENDERED_EVENT_FIELDS)
    _true(event["recorded"], f"{where}.rendered_event.recorded")
    detail = _exact_text(event["detail"], f"{where}.rendered_event.detail")
    if pdf_sha not in detail:  # Revision 4, section 2: the event detail carries the full PDF hash
        _fail(f"{where}.rendered_event.detail", "must contain the PDF's full SHA-256")

    short = hash16_of(pdf_sha)
    superseded = value["superseded_previous_pdf"]
    if superseded is not None:
        _preserved_as(superseded, f"{where}.superseded_previous_pdf")
        if superseded["sha256_16"] == short:  # a superseded report differs from the one that replaced it
            _fail(f"{where}.superseded_previous_pdf.sha256_16", "must differ from the new report's hash")
    duplicate = value["duplicate_draft_preserved_as"]
    if duplicate is not None:
        _preserved_as(duplicate, f"{where}.duplicate_draft_preserved_as")
        if duplicate["sha256_16"] != short:  # a duplicate draft is identical to the promoted report
            _fail(f"{where}.duplicate_draft_preserved_as.sha256_16", "must match the report's own hash")
    return value


def _excluded_unfinalized(value, where: str) -> list:
    items = _list(value, where)
    for position, item in enumerate(items):
        w = f"{where}[{position}]"
        _mapping(item, w, EXCLUDED_FIELDS)
        _uuid(item["run_id"], f"{w}.run_id")
        _int(item["revision"], f"{w}.revision", 1, MAX_REVISION)
        start, end = _date(item["period_start"], f"{w}.period_start"), _date(item["period_end"], f"{w}.period_end")
        if start > end:
            _fail(w, f"period_start {start} is after period_end {end}")
        _choice(item["reason_code"], f"{w}.reason_code", EXCLUSION_REASON_CODES)
    return items


def _history_config(value, where: str) -> dict:
    """The limits history was compared under (`HistoryConfig`; comparison.limits)."""
    _mapping(value, where, CONFIG_FIELDS)
    _int(value["lookback_periods"], f"{where}.lookback_periods", 1, _NO_UPPER_LIMIT)
    _int(value["max_gap_days"], f"{where}.max_gap_days", 0, _NO_UPPER_LIMIT)
    _int(value["list_limit"], f"{where}.list_limit", 1, _NO_UPPER_LIMIT)
    return value


def _baselines(value, where: str) -> dict:
    """The baseline chosen per dimension (at most one entry per dimension). Returns {dimension: entry}."""
    items = _list(value, where, len(DIMENSIONS))
    by_dimension = {}
    for position, item in enumerate(items):
        w = f"{where}[{position}]"
        _mapping(item, w, BASELINE_FIELDS)
        dimension = _choice(item["dimension"], f"{w}.dimension", DIMENSIONS)
        if dimension in by_dimension:
            _fail(w, f"repeats the dimension {dimension!r}")
        by_dimension[dimension] = item
        _uuid(item["run_id"], f"{w}.run_id")
        _int(item["revision"], f"{w}.revision", 1, MAX_REVISION)
        start, end = _date(item["period_start"], f"{w}.period_start"), _date(item["period_end"], f"{w}.period_end")
        if start > end:
            _fail(w, f"period_start {start} is after period_end {end}")
        _int(item["gap_days"], f"{w}.gap_days", 0, _NO_UPPER_LIMIT)
        _int(item["rank"], f"{w}.rank", 1, _NO_UPPER_LIMIT)
        _bool(item["same_length"], f"{w}.same_length")
    return by_dimension


def _skipped_runs(value, where: str) -> list:
    items = _list(value, where)
    for position, item in enumerate(items):
        w = f"{where}[{position}]"
        _mapping(item, w, SKIPPED_FIELDS)
        _uuid(item["run_id"], f"{w}.run_id")
        _int(item["revision"], f"{w}.revision", 1, MAX_REVISION)
        start, end = _date(item["period_start"], f"{w}.period_start"), _date(item["period_end"], f"{w}.period_end")
        if start > end:
            _fail(w, f"period_start {start} is after period_end {end}")
        reason = item["reason"]
        if not isinstance(reason, str) or not any(p.fullmatch(reason) for p in _SKIP_REASON_PATTERNS):
            _fail(f"{w}.reason", f"must be one of the history stage's skip reasons, got {reason!r}")
    return items


def _dimension_status(value, where: str, baselines: dict) -> list:
    """Exactly one entry per dimension (`DimensionStatus`), consistent with the chosen baselines."""
    items = _list(value, where, len(DIMENSIONS))
    seen = set()
    for position, item in enumerate(items):
        w = f"{where}[{position}]"
        _mapping(item, w, STATUS_FIELDS)
        dimension = _choice(item["dimension"], f"{w}.dimension", DIMENSIONS)
        if dimension in seen:
            _fail(w, f"repeats the dimension {dimension!r}")
        seen.add(dimension)
        eligible = _bool(item["eligible"], f"{w}.eligible")
        for name in ("reasons", "caveats"):  # variable text in the existing code: any non-empty text
            texts = _list(item[name], f"{w}.{name}", 50)
            for index, text in enumerate(texts):
                _exact_text(text, f"{w}.{name}[{index}]", max_chars=MAX_STATUS_TEXT_CHARS)
        baseline_run_id = item["baseline_run_id"]
        if eligible:
            _uuid(baseline_run_id, f"{w}.baseline_run_id")
            entry = baselines.get(dimension)
            if entry is None or entry["run_id"] != baseline_run_id:
                _fail(f"{w}.baseline_run_id", f"is not the baseline listed for {dimension!r} in history.baselines")
        elif baseline_run_id is not None:
            _fail(f"{w}.baseline_run_id", "must be null for a dimension that is not eligible")
    if len(seen) != len(DIMENSIONS):
        _fail(where, f"must list all {len(DIMENSIONS)} dimensions, missing {sorted(set(DIMENSIONS) - seen)}")
    # The other direction: a dimension has a baseline if and only if it is eligible, and the run ids agree.
    by_dimension = {item["dimension"]: item for item in items}
    for dimension, entry in baselines.items():
        status = by_dimension[dimension]
        if not status["eligible"]:
            _fail("history.baselines", f"has a baseline for {dimension!r}, whose dimension_status is not eligible")
        if status["baseline_run_id"] != entry["run_id"]:
            _fail("history.baselines", f"the baseline run for {dimension!r} differs from its dimension_status "
                                       "baseline_run_id")
    return items


def _count_or_null(value, where: str):
    return None if value is None else _int(value, where, 0, _NO_UPPER_LIMIT)


def _ranking_crosscheck(value, where: str) -> dict:
    """Sheet-versus-history ranking cross-check. `fallback_used` counts keywords whose blank Previous Ranking was
    filled from history; zero means no fallback was used. The other counts are null when rankings were
    unavailable (RankingComparison.counts)."""
    _mapping(value, where, CROSSCHECK_FIELDS)
    _int(value["fallback_used"], f"{where}.fallback_used", 0, _NO_UPPER_LIMIT)
    for name in CROSSCHECK_FIELDS[1:]:
        _count_or_null(value[name], f"{where}.{name}")
    return value


def _diagnostic_group(value, where: str) -> dict:
    """`{source: {which: {name: whole number}}}` as stored in a snapshot payload (which = current/previous)."""
    if not isinstance(value, dict):
        _fail(where, f"must be an object, got {type(value).__name__}")
    for source, per in value.items():
        w = f"{where}.{source}"
        _label(source, f"{where} source name")
        if not isinstance(per, dict):
            _fail(w, f"must be an object, got {type(per).__name__}")
        for which, counters in per.items():
            _choice(which, f"{w} period key", DIAGNOSTIC_WHICH)
            if not isinstance(counters, dict):
                _fail(f"{w}.{which}", f"must be an object, got {type(counters).__name__}")
            for name, number in counters.items():
                _label(name, f"{w}.{which} counter name")
                _int(number, f"{w}.{which}.{name}", -_NO_UPPER_LIMIT, _NO_UPPER_LIMIT)
    return value


def _diagnostics(value, where: str, baselines: dict) -> dict:
    _mapping(value, where, DIAGNOSTICS_FIELDS)
    _diagnostic_group(value["current"], f"{where}.current")
    per_run = value["baselines"]
    if not isinstance(per_run, dict):
        _fail(f"{where}.baselines", f"must be an object, got {type(per_run).__name__}")
    listed = {entry["run_id"] for entry in baselines.values()}
    for run_id, group in per_run.items():
        _uuid(run_id, f"{where}.baselines key")
        if run_id not in listed:
            _fail(f"{where}.baselines", f"has diagnostics for run {run_id}, which is not in history.baselines")
        _diagnostic_group(group, f"{where}.baselines.{run_id}")
    return value


def _history_section(value, where: str) -> dict:
    _mapping(value, where, HISTORY_FIELDS)
    _exact_text(value["status"], f"{where}.status", max_chars=64)
    if value["failure_kind"] is not None:
        _exact_text(value["failure_kind"], f"{where}.failure_kind", max_chars=64)
    _rules_version(value["rules_version"], f"{where}.rules_version")
    _history_config(value["config"], f"{where}.config")
    baselines = _baselines(value["baselines"], f"{where}.baselines")
    _skipped_runs(value["skipped_runs"], f"{where}.skipped_runs")
    _excluded_unfinalized(value["excluded_unfinalized"], f"{where}.excluded_unfinalized")
    _dimension_status(value["dimension_status"], f"{where}.dimension_status", baselines)
    _ranking_crosscheck(value["ranking_crosscheck"], f"{where}.ranking_crosscheck")
    _diagnostics(value["diagnostics"], f"{where}.diagnostics", baselines)
    return value


def _evidence(value, where: str) -> dict:
    """Figures behind a history statement: a flat object of finite numbers, stored without rounding.

    Which keys a statement may use is decided by the history evidence registry (Stage 6c-4).
    """
    if not isinstance(value, dict):
        _fail(where, f"must be an object, got {type(value).__name__}")
    for name, number in value.items():
        _exact_text(name, f"{where} key", max_chars=MAX_LABEL_CHARS)
        w = f"{where}.{name}"
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            _fail(w, f"must be a number (not text, true/false, null, a list or an object), got "
                     f"{type(number).__name__}")
        if isinstance(number, float) and not math.isfinite(number):
            _fail(w, "must be finite (not NaN or infinity)")
    return value


def _history_support_item(item, where: str) -> dict:
    """One support item, closed and fully required; its dimension and evidence keys depend on its source."""
    _mapping(item, where, SUPPORT_FIELDS)
    _choice(item["classification"], f"{where}.classification", SUPPORT_CLASSIFICATIONS)
    source = _choice(item["source"], f"{where}.source", SUPPORT_SOURCES)
    subject = item["subject"]
    if source == "ranking_keyword":
        if not isinstance(subject, str) or not subject.strip():
            _fail(f"{where}.subject", "must be the keyword, a non-empty text, for a ranking_keyword item")
        _exact_text(subject, f"{where}.subject", max_chars=MAX_KEYWORD_CHARS)
    elif subject is not None:
        _fail(f"{where}.subject", f"must be null for a {source} item")
    expected = SUPPORT_SOURCE_DIMENSIONS[source]
    if _choice(item["dimension"], f"{where}.dimension", DIMENSIONS) != expected:
        _fail(f"{where}.dimension", f"must be {expected!r} for a {source} item")
    _uuid(item["baseline_run_id"], f"{where}.baseline_run_id")
    _period(item["baseline_period"], f"{where}.baseline_period")
    evidence = item["evidence"]
    if not isinstance(evidence, dict):
        _fail(f"{where}.evidence", f"must be an object, got {type(evidence).__name__}")
    allowed = SUPPORT_EVIDENCE_KEYS[source]
    unknown = sorted(str(name) for name in evidence if name not in EVIDENCE_KEYS)
    if unknown:
        _fail(f"{where}.evidence", f"has key(s) {unknown} that are not registered history evidence")
    stray = sorted(name for name in evidence if name not in allowed)
    if stray:
        _fail(f"{where}.evidence", f"has key(s) {stray} that a {source} item may not use")
    missing = [name for name in allowed if name not in evidence]
    if missing:
        _fail(f"{where}.evidence", f"is missing required key(s) {missing}")
    _evidence(evidence, f"{where}.evidence")  # flat, finite numbers, no booleans
    if "direction" in evidence:
        _int(evidence["direction"], f"{where}.evidence.direction", -1, 1)
    _int(evidence["baseline_days"], f"{where}.evidence.baseline_days", 1, _NO_UPPER_LIMIT)
    if source == "ranking_keyword":
        _int(evidence["baseline_value"], f"{where}.evidence.baseline_value", 1, _NO_UPPER_LIMIT)
    _exact_text(item["rules_version"], f"{where}.rules_version", max_chars=64)
    return item


def _recommendation_annotations(value, where: str) -> list:
    """Reviewer-only. Each entry is `{recommendation_key, history_support}`; `history_support` is a list of at most
    MAX_SUPPORT_ITEMS closed items and may be empty ("no usable historical context")."""
    items = _list(value, where, MAX_ANNOTATIONS)
    seen = set()
    for position, item in enumerate(items):
        w = f"{where}[{position}]"
        _mapping(item, w, ANNOTATION_FIELDS)
        key = _label(item["recommendation_key"], f"{w}.recommendation_key")
        if key in seen:
            _fail(w, f"repeats recommendation_key {key!r}")
        seen.add(key)
        support = _list(item["history_support"], f"{w}.history_support", MAX_SUPPORT_ITEMS)
        for index, support_item in enumerate(support):
            _history_support_item(support_item, f"{w}.history_support[{index}]")
    return items

def _validation_failures(value, where: str) -> list:
    """Zero or more `{key, reason}` entries: the statement key and one of the Stage 6c-4 reason codes."""
    items = _list(value, where)
    for position, item in enumerate(items):
        w = f"{where}[{position}]"
        _mapping(item, w, VALIDATION_FAILURE_FIELDS)
        key = _label(item["key"], f"{w}.key")
        if not key.strip():
            _fail(f"{w}.key", "must not be empty or only whitespace")
        _choice(item["reason"], f"{w}.reason", VALIDATION_FAILURE_REASONS)
    return items

def _client_facing_history(value, where: str) -> list:
    """Statements shown to the client about earlier periods; `text` is kept word for word."""
    items = _list(value, where)
    for position, item in enumerate(items):
        w = f"{where}[{position}]"
        _mapping(item, w, CLIENT_HISTORY_FIELDS)
        _label(item["section"], f"{w}.section")
        _label(item["key"], f"{w}.key")
        _choice(item["dimension"], f"{w}.dimension", DIMENSIONS)
        _exact_text(item["text"], f"{w}.text")
        _period(item["baseline_period"], f"{w}.baseline_period")
        _evidence(item["evidence"], f"{w}.evidence")
        _rules_version(item["rules_version"], f"{w}.rules_version")
    return items


def _ranking_table_markers(value, where: str) -> dict:
    _mapping(value, where, MARKERS_FIELDS)
    _exact_text(value["footnote_text"], f"{where}.footnote_text", allow_empty=True)
    cells = _list(value["cells"], f"{where}.cells")
    for position, cell in enumerate(cells):
        w = f"{where}.cells[{position}]"
        _mapping(cell, w, MARKER_CELL_FIELDS)
        _exact_text(cell["keyword"], f"{w}.keyword", max_chars=MAX_KEYWORD_CHARS)
        # The sheet's Target Location cell, kept exactly as the sheet has it (it may be empty). Never a path.
        _exact_text(cell["target_location"], f"{w}.target_location", max_chars=MAX_KEYWORD_CHARS,
                    allow_empty=True)
        _exact_text(cell["text"], f"{w}.text")
        _label(cell["source"], f"{w}.source")
        _period(cell["baseline_period"], f"{w}.baseline_period")
    return value


def _notes(value, where: str) -> list:
    items = _list(value, where)
    for position, item in enumerate(items):
        w = f"{where}[{position}]"
        _mapping(item, w, NOTE_FIELDS)
        _label(item["id"], f"{w}.id")
        _choice(item["severity"], f"{w}.severity", NOTE_SEVERITIES)
        _choice(item["source"], f"{w}.source", NOTE_SOURCES)
        _choice(item["category"], f"{w}.category", NOTE_CATEGORIES)
        _exact_text(item["message"], f"{w}.message")
        _choice(item["audience"], f"{w}.audience", ("reviewer",))
    return items

def validate_finalized_record(record) -> dict:
    """Return `record` if it is a valid finalized-report record, otherwise raise RecordSchemaError."""
    if isinstance(record, dict) and record.get("record_type") == ATTEMPT_TYPE:
        _fail("record", "is an attempt record, not a finalized report")
    _mapping(record, "record", FINALIZED_FIELDS)
    version = _schema_version(record["schema_version"], "schema_version", FINALIZED_SCHEMA_VERSIONS)
    _choice(record["record_type"], "record_type", (FINALIZED_TYPE,))
    _client_key(record["client_key"], "client_key")
    _period(record["report_period"], "report_period")
    _period(record["comparison_period"], "comparison_period")
    _snapshot_section(record["snapshot"], "snapshot")
    _finalization_section(record["finalization"], "finalization", version)
    _history_section(record["history"], "history")
    _client_facing_history(record["client_facing_history"], "client_facing_history")
    _ranking_table_markers(record["ranking_table_markers"], "ranking_table_markers")
    _recommendation_annotations(record["recommendation_annotations"], "recommendation_annotations")
    _notes(record["notes"], "notes")
    _validation_failures(record["validation_failures"], "validation_failures")
    return record


# --- attempt record -----------------------------------------------------------------------------------------------

def validate_attempt_record(record) -> dict:
    """Return `record` if it is a valid attempt record, otherwise raise RecordSchemaError.

    An attempt record is never a finalized report: `finalized` must be false.
    """
    if isinstance(record, dict) and record.get("record_type") == FINALIZED_TYPE:
        _fail("record", "is a finalized report record, not an attempt")
    _mapping(record, "record", ATTEMPT_FIELDS)
    _schema_version(record["schema_version"])
    _choice(record["record_type"], "record_type", (ATTEMPT_TYPE,))
    _client_key(record["client_key"], "client_key")
    _period(record["report_period"], "report_period")
    _timestamp(record["attempted_at"], "attempted_at")
    _choice(record["step"], "step", ATTEMPT_STEPS)
    _choice(record["status_code"], "status_code", ATTEMPT_STATUS_CODES)

    exception_class = record["exception_class"]
    if exception_class is not None and (not isinstance(exception_class, str)
                                        or not _EXCEPTION_RE.fullmatch(exception_class)):
        _fail("exception_class", "must be null or an identifier of at most 64 characters (a class name only, "
                                 "never a message)")
    if record["run_id"] is not None:
        _uuid(record["run_id"], "run_id")
    if record["pdf_sha256"] is not None:
        _sha256(record["pdf_sha256"], "pdf_sha256")
    if record["pdf_bytes"] is not None:  # an empty draft (0 bytes) is a reason for `draft_invalid`
        _int(record["pdf_bytes"], "pdf_bytes", 0, MAX_PDF_BYTES)
    if record["previous_report_restored"] is not None:
        _bool(record["previous_report_restored"], "previous_report_restored")
    if record["previous_report_preserved_as"] is not None:
        _preserved_as(record["previous_report_preserved_as"], "previous_report_preserved_as")
    if _bool(record["finalized"], "finalized") is not False:
        _fail("finalized", "an attempt record must have finalized set to false")
    return record
