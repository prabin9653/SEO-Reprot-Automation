"""Building the finalized reviewer record from a report bundle and its Stage 6c outputs.

`build_reviewer_record` assembles the record described by `record_schema` (the finalized-report record) and
validates it with `record_schema.validate_finalized_record` before returning it. Nothing is repaired or dropped to
make it pass: a value the schema cannot accept raises `ReviewerRecordError`.

Inputs
  bundle                    the `ReportBundle` of Stage 6c-3
  receipt                   a `FinalizationReceipt` with what only the finalization step knows and the bundle
                            cannot hold: the stored snapshot's run ID and revision, the finalization time, the PDF
                            hash and size, the preliminary PDF checks, the client-content hash, the rendered-event
                            detail and the preserved-copy metadata (Stage 6c-8 creates it)
  history_insights          the composed `HistoryInsights` of Stage 6c-5 (None means no historical statements,
                            findings, notes or failures)
  recommendation_annotations  the `RecommendationHistoryAnnotations` of Stage 6c-6 (None means none were made)
  history_config            only used when no comparison exists, to fill `history.config`

Where each part of the record comes from
  identity                  reviewer_report.client_key / snapshot, client_content.periods, the receipt
  history.status            "available" when the Stage 6b comparison ran, otherwise "unavailable"; failure_kind is
                            None when available, otherwise the history state (missing, unavailable, schema_failure,
                            comparison_unavailable)
  history.config/baselines/skipped_runs/dimension_status/diagnostics
                            reviewer_report.history (the Stage 6b comparison). Without a comparison the baselines
                            and skipped runs are empty, every dimension is listed as not eligible with the history
                            status detail as its reason, and `diagnostics.current` is the base report's diagnostics.
  history.excluded_unfinalized, history.ranking_crosscheck
                            reviewer_report (Stage 6c-2 / 6c-3); only the counts the schema holds are stored
  client_facing_history     the composed client statements (text and evidence exactly as composed)
  ranking_table_markers     footnote and cells from ClientContent (empty in this stage)
  recommendation_annotations  the Stage 6c-6 annotations, exactly as `as_record()` gives them
  notes                     the base notes, the assembly and comparison notes, the history rule notes and one note
                            per reviewer finding (the record has no field for findings, so each becomes a note with
                            id = the finding key and a category from FINDING_NOTE_CATEGORY)
  validation_failures       the composed Stage 6c-4 failures

Not stored, because the closed schema has no place for it: per-keyword pending fallbacks and mismatches, finding
evidence, the engine's own validation failures and the statement audit.

The record is returned as a fresh dictionary (nothing in it is shared with the inputs). The module does no file,
database or network access and reads no clock.
"""

import copy
import json
import re
from dataclasses import dataclass

from seo_report import record_schema as rs
from seo_report.insights import history_engine as he
from seo_report.insights import history_recommendations as hrec
from seo_report.insights import history_rules as hr
from seo_report.report_run import ReportBundle
from seo_report.snapshots import eligibility as el
from seo_report.snapshots.baselines import HistoryConfig


class ReviewerRecordError(ValueError):
    """The reviewer record cannot be built or does not pass validation. The message holds no record values."""


@dataclass(frozen=True)
class PreservedPdf:
    sha256_16: str
    variant_index: int


@dataclass(frozen=True)
class PdfChecks:
    non_empty: bool
    header: bool
    eof_marker: bool
    preliminary: bool = True


@dataclass(frozen=True)
class RenderInputFormat:
    """The canonical form the render-input hash was computed in (`render_input_hash.FORMAT_NAME` / `FORMAT_VERSION`)."""

    name: str
    version: int


@dataclass(frozen=True)
class FinalizationReceipt:
    run_id: str
    revision: int
    finalized_at: str
    pdf_sha256: str
    pdf_bytes: int
    pdf_checks: PdfChecks
    client_content_sha256: str
    rendered_event_detail: str
    superseded_previous_pdf: PreservedPdf | None = None
    duplicate_draft_preserved_as: PreservedPdf | None = None
    rendered_event_recorded: bool = True
    # Both or neither. Supplied together they make a schema version 2 record; omitted, a version 1 record.
    render_input_sha256: str | None = None
    render_input_format: RenderInputFormat | None = None


# Reviewer findings become notes. Section -> note source; kind -> note category.
FINDING_NOTE_SOURCE = {"keywords": "rankings", "search_console": "gsc", "geography": "geography", "ga4": "ga4",
                       "history": "history"}
FINDING_NOTE_CATEGORY = {
    "gsc_historical_change": "suppressed", "ga4_historical_change": "suppressed",
    "geography_share_change": "suppressed", "ranking_pending_fallback": "suppressed",
    "ranking_sheet_mismatch": "data_quality", "ranking_new_keywords": "limitation",
    "ranking_lost_keywords": "limitation", "ranking_not_comparable": "limitation",
    "baselines_differ": "data_quality", "dimensions_unusable": "limitation",
    "incomplete_comparison_data": "data_quality", "different_period_length": "data_quality",
    "ga4_daily_discrepancy": "diagnostic",
}
FINDING_NOTE_SEVERITY = "info"

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\(\d{3}\)\s*\d{3}[-.\s]\d{4}|(?<![\w.-])\d{3}[-.\s]\d{3}[-.\s]\d{4}(?![\w-])"
                    r"|\+\d[\d\s().-]{7,}\d")
_PATH = re.compile(r"[A-Za-z]:\\|\\\\[\w.-]+\\|(?<![\w.])/(?:home|Users|etc|var|tmp|usr|root)/")
_CREDENTIAL = re.compile(r"ya29\.|1//[\w-]{10,}|client_secret|access_token|refresh_token|Bearer\s+\S|api[_-]?key",
                         re.IGNORECASE)
_TRACEBACK = re.compile(r"Traceback|File \"|\bat line \d+")
_IDENTIFIER = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{64}")
_TOKEN = re.compile(r"(?<![\w-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}(?![\w-])")


# --- safety ---------------------------------------------------------------------------------------------------------------

def _unsafe_reason(text: str, *, allow_identifiers: bool = False) -> str | None:
    """Why a piece of text must not be stored in the record, or None. Never returns the text itself."""
    if _EMAIL.search(text):
        return "an email address"
    if _PHONE.search(text):
        return "a phone number"
    if _PATH.search(text):
        return "a file path"
    if _CREDENTIAL.search(text):
        return "a credential marker"
    if _TRACEBACK.search(text):
        return "exception text"
    if not allow_identifiers and _TOKEN.search(_IDENTIFIER.sub(" ", text)):
        return "an opaque token"
    return None


def _scan(texts) -> None:
    for where, text, allow_identifiers in texts:
        reason = _unsafe_reason(text, allow_identifiers=allow_identifiers)
        if reason:
            raise ReviewerRecordError(f"{where} contains {reason}, which must not be stored in a reviewer record")


# --- building -----------------------------------------------------------------------------------------------------------------

def _preserved(value: PreservedPdf | None):
    return None if value is None else {"sha256_16": value.sha256_16, "variant_index": value.variant_index}


def _history_config(bundle, history_config) -> dict:
    review = bundle.reviewer_report.history
    if review is not None:
        return dict(review.limits)
    config = history_config if history_config is not None else HistoryConfig()
    return {"lookback_periods": config.lookback_periods, "max_gap_days": config.max_gap_days,
            "list_limit": config.list_limit}


def _history_section(bundle: ReportBundle, history_insights, history_config) -> dict:
    reviewer = bundle.reviewer_report
    status = reviewer.history_status
    review = reviewer.history
    available = status.state == "available" and review is not None
    if review is not None:
        baselines = [{"dimension": dimension, "run_id": ref.run_id, "revision": ref.revision,
                      "period_start": ref.period_start, "period_end": ref.period_end, "gap_days": ref.gap_days,
                      "rank": ref.rank, "same_length": ref.same_length} for dimension, ref in review.baselines]
        skipped = [{"run_id": s.run_id, "revision": s.revision, "period_start": s.period_start,
                    "period_end": s.period_end, "reason": s.reason} for s in review.skipped_runs]
        statuses = [{"dimension": s.dimension, "eligible": s.eligible, "reasons": list(s.reasons),
                     "caveats": list(s.caveats), "baseline_run_id": s.baseline.run_id if s.baseline else None}
                    for s in review.dimension_status]
        diagnostics = copy.deepcopy(review.diagnostics)
    else:
        baselines, skipped = [], []
        statuses = [{"dimension": dimension, "eligible": False, "reasons": [status.detail], "caveats": [],
                     "baseline_run_id": None} for dimension in el.DIMENSIONS]
        diagnostics = {"current": copy.deepcopy(reviewer.diagnostics), "baselines": {}}
    check = reviewer.ranking_crosscheck
    return {
        "status": "available" if available else "unavailable",
        "failure_kind": None if available else status.state,
        "rules_version": history_insights.rules_version,
        "config": _history_config(bundle, history_config),
        "baselines": baselines,
        "skipped_runs": skipped,
        "excluded_unfinalized": [e.as_record() for e in reviewer.excluded_unfinalized],
        "dimension_status": statuses,
        "ranking_crosscheck": {"fallback_used": check.fallback_used,
                               "sheet_previous_mismatch": check.sheet_previous_mismatch,
                               "new_keywords": check.new_keywords, "no_longer_tracked": check.no_longer_tracked,
                               "not_comparable": check.not_comparable},
        "diagnostics": diagnostics,
    }


def _client_facing_history(history_insights) -> list:
    return [{"section": s.section, "key": s.key, "dimension": s.dimension, "text": s.text,
             "baseline_period": {"start": s.baseline_period_start, "end": s.baseline_period_end},
             "evidence": dict(s.evidence), "rules_version": s.rules_version}
            for s in history_insights.client_statements]


def _ranking_table_markers(bundle: ReportBundle) -> dict:
    content = bundle.client_content
    if content.ranking_markers:
        raise ReviewerRecordError("ranking markers cannot be recorded: their element type is not defined yet")
    return {"footnote_text": content.ranking_footnote or "", "cells": []}


def _notes(bundle: ReportBundle, history_insights) -> list:
    reviewer = bundle.reviewer_report
    notes = []
    for prefix, group in (("base", reviewer.base_notes), ("stage", reviewer.notes),
                          ("history", history_insights.notes)):
        for index, note in enumerate(group, start=1):
            notes.append({"id": f"{prefix}.{index:03d}", "severity": note.severity, "source": note.source,
                          "category": note.category, "message": note.message, "audience": "reviewer"})
    for finding in history_insights.reviewer_findings:
        notes.append({"id": finding.key, "severity": FINDING_NOTE_SEVERITY,
                      "source": FINDING_NOTE_SOURCE.get(finding.section, finding.section),
                      "category": FINDING_NOTE_CATEGORY.get(finding.kind, finding.kind),
                      "message": finding.message, "audience": "reviewer"})
    return notes


def _annotations(bundle: ReportBundle, annotations) -> list:
    if annotations is None:
        return []
    known = {recommendation.key for recommendation in bundle.client_content.recommendations}
    entries = annotations.as_record()
    for entry in entries:
        if entry["recommendation_key"] not in known:
            raise ReviewerRecordError("a recommendation annotation refers to a recommendation that is not in the "
                                      "report")
    return entries


def _texts_to_scan(record: dict):
    for position, note in enumerate(record["notes"]):
        yield f"notes[{position}].message", note["message"], False
        yield f"notes[{position}].id", note["id"], False
    for position, status in enumerate(record["history"]["dimension_status"]):
        for field in ("reasons", "caveats"):
            for index, text in enumerate(status[field]):
                yield f"history.dimension_status[{position}].{field}[{index}]", text, False
    for position, statement in enumerate(record["client_facing_history"]):
        yield f"client_facing_history[{position}].text", statement["text"], False
    yield "ranking_table_markers.footnote_text", record["ranking_table_markers"]["footnote_text"], False
    yield "finalization.rendered_event.detail", record["finalization"]["rendered_event"]["detail"], True
    for position, entry in enumerate(record["recommendation_annotations"]):
        for index, item in enumerate(entry["history_support"]):
            if item["subject"] is not None:
                yield f"recommendation_annotations[{position}].history_support[{index}].subject", item["subject"], False


def build_reviewer_record(*, bundle, receipt, history_insights=None, recommendation_annotations=None,
                          history_config=None) -> dict:
    """Build and validate the finalized reviewer record. Returns a new dictionary; the inputs are not changed."""
    if not isinstance(bundle, ReportBundle):
        raise TypeError(f"bundle must be a ReportBundle, got {type(bundle).__name__}")
    if not isinstance(receipt, FinalizationReceipt):
        raise TypeError(f"receipt must be a FinalizationReceipt, got {type(receipt).__name__}")
    history_insights = he.HistoryInsights() if history_insights is None else history_insights
    if not isinstance(history_insights, he.HistoryInsights):
        raise TypeError("history_insights must be a HistoryInsights")
    if recommendation_annotations is not None and not isinstance(
            recommendation_annotations, hrec.RecommendationHistoryAnnotations):
        raise TypeError("recommendation_annotations must be a RecommendationHistoryAnnotations")

    reviewer = bundle.reviewer_report
    snapshot = reviewer.snapshot
    periods = bundle.client_content.periods
    checks = receipt.pdf_checks
    has_hash, has_format = receipt.render_input_sha256 is not None, receipt.render_input_format is not None
    if has_hash != has_format:
        raise ReviewerRecordError("render_input_sha256 and render_input_format must be supplied together or not at all")
    if has_format and not isinstance(receipt.render_input_format, RenderInputFormat):
        raise TypeError("render_input_format must be a RenderInputFormat")
    record = {
        "schema_version": rs.CURRENT_FINALIZED_SCHEMA_VERSION if has_hash else rs.SCHEMA_VERSION,
        "record_type": rs.FINALIZED_TYPE,
        "client_key": reviewer.client_key,
        "report_period": {"start": snapshot.period_start, "end": snapshot.period_end},
        "comparison_period": {"start": periods.previous.start.isoformat(), "end": periods.previous.end.isoformat()},
        "snapshot": {"run_id": receipt.run_id, "revision": receipt.revision, "content_hash": snapshot.content_hash,
                     "quality": snapshot.quality},
        "finalization": {
            "finalized_at": receipt.finalized_at, "pdf_sha256": receipt.pdf_sha256, "pdf_bytes": receipt.pdf_bytes,
            "pdf_checks": {"non_empty": checks.non_empty, "header": checks.header, "eof_marker": checks.eof_marker,
                           "preliminary": checks.preliminary},
            "client_content_sha256": receipt.client_content_sha256,
            "rendered_event": {"recorded": receipt.rendered_event_recorded, "detail": receipt.rendered_event_detail},
            "superseded_previous_pdf": _preserved(receipt.superseded_previous_pdf),
            "duplicate_draft_preserved_as": _preserved(receipt.duplicate_draft_preserved_as),
        },
        "history": _history_section(bundle, history_insights, history_config),
        "client_facing_history": _client_facing_history(history_insights),
        "ranking_table_markers": _ranking_table_markers(bundle),
        "recommendation_annotations": _annotations(bundle, recommendation_annotations),
        "notes": _notes(bundle, history_insights),
        "validation_failures": [{"key": f.key, "reason": f.reason} for f in history_insights.validation_failures],
    }
    if has_hash:  # schema version 2: the render-input identity, next to (never instead of) client_content_sha256
        record["finalization"]["render_input_sha256"] = receipt.render_input_sha256
        record["finalization"]["render_input_format"] = {"name": receipt.render_input_format.name,
                                                         "version": receipt.render_input_format.version}
    record = copy.deepcopy(record)  # nothing is shared with the inputs
    _scan(_texts_to_scan(record))
    try:
        rs.validate_finalized_record(record)
    except rs.RecordSchemaError as err:
        raise ReviewerRecordError(f"the reviewer record is not valid: {err}") from None
    return record


def to_canonical_json(record: dict) -> str:
    """Deterministic text for a validated record: sorted keys, no NaN, text kept as written (not ASCII-escaped)."""
    try:
        return json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as err:
        raise ReviewerRecordError(f"the record cannot be serialized: {type(err).__name__}") from None
