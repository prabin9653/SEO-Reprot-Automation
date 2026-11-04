import copy

import pytest

from seo_report import record_schema as rs
from seo_report.record_schema import RecordSchemaError
from seo_report.snapshots.eligibility import DIMENSIONS

RUN = "0198c1a2-7b3c-7d4e-8f5a-1234567890ab"
RUN2 = "0198c1a2-7b3c-7d4e-8f5a-1234567890ac"
RUN3 = "0198c1a2-7b3c-7d4e-8f5a-1234567890ad"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
TS = "2026-02-03T09:30:00+00:00"
APPROVED_STEPS = ["verify_draft", "preflight", "persist", "event", "supersede", "promote", "record"]
APPROVED_STATUS_CODES = [
    "draft_invalid", "output_unavailable", "persist_failed", "event_failed", "supersede_failed",
    "superseded_name_conflict", "superseded_variants_exhausted", "duplicate_draft_conflict", "promote_failed",
    "reviewer_record_conflict", "record_failed", "stale_bundle", "draft_partial_conflict",
    "existing_report_conflict", "reader_record_limit"]
ORIGINAL_STATUS_CODES = [
    "draft_invalid", "output_unavailable", "persist_failed", "event_failed", "supersede_failed",
    "superseded_name_conflict", "superseded_variants_exhausted", "duplicate_draft_conflict", "promote_failed",
    "reviewer_record_conflict", "record_failed"]
NEW_STATUS_CODES = ["stale_bundle", "draft_partial_conflict", "existing_report_conflict", "reader_record_limit"]
EXACT_TEXT = "  Up\u00a03 places \u2014 \u201cgreat\u201d,\n e\u0301 vs \u00e9;\ttwo  spaces  "


def baseline(**overrides):
    value = {"dimension": "rankings.keywords", "run_id": RUN2, "revision": 1, "period_start": "2025-12-01",
             "period_end": "2025-12-31", "gap_days": 0, "rank": 1, "same_length": True}
    value.update(overrides)
    return value


def finalized():
    status = [{"dimension": name, "eligible": name == "rankings.keywords",
               "reasons": [] if name == "rankings.keywords" else ["Search Console data was not provided"],
               "caveats": [], "baseline_run_id": RUN2 if name == "rankings.keywords" else None}
              for name in DIMENSIONS]
    return {
        "schema_version": 1,
        "record_type": "finalized_report",
        "client_key": "example-client",
        "report_period": {"start": "2026-01-01", "end": "2026-01-31"},
        "comparison_period": {"start": "2025-12-01", "end": "2025-12-31"},
        "snapshot": {"run_id": RUN, "revision": 1, "content_hash": SHA_B, "quality": "complete"},
        "finalization": {
            "finalized_at": TS,
            "pdf_sha256": SHA_A,
            "pdf_bytes": 123456,
            "pdf_checks": {"non_empty": True, "header": True, "eof_marker": True, "preliminary": True},
            "client_content_sha256": SHA_C,
            "rendered_event": {"recorded": True, "detail": f"Report rendered (PDF {SHA_A})"},
            "superseded_previous_pdf": None,
            "duplicate_draft_preserved_as": None,
        },
        "history": {
            "status": "ok", "failure_kind": None, "rules_version": "1",
            "config": {"lookback_periods": 3, "max_gap_days": 35, "list_limit": 10},
            "baselines": [baseline()],
            "skipped_runs": [{"run_id": RUN3, "revision": 1, "period_start": "2025-10-01",
                              "period_end": "2025-10-31", "reason": "voided"}],
            "excluded_unfinalized": [],
            "dimension_status": status,
            "ranking_crosscheck": {"fallback_used": 0, "sheet_previous_mismatch": 0, "new_keywords": 1,
                                   "no_longer_tracked": 0, "not_comparable": 2},
            "diagnostics": {"current": {"ga4": {"current": {"daily_sessions_diff": -3}, "previous": {}}},
                            "baselines": {RUN2: {"ga4": {"current": {"daily_sessions_diff": 0}}}}},
        },
        "client_facing_history": [{
            "section": "rankings", "key": "rankings.summary", "dimension": "rankings.keywords",
            "text": EXACT_TEXT, "baseline_period": {"start": "2025-12-01", "end": "2025-12-31"},
            "evidence": {"improved": 3, "share": 0.1 + 0.2}, "rules_version": "1"}],
        "ranking_table_markers": {
            "footnote_text": "\u25b2 moved up since last report ",
            "cells": [{"keyword": "roof repair", "target_location": "table1.row2", "text": "\u25b2 3",
                       "source": "rankings", "baseline_period": {"start": "2025-12-01", "end": "2025-12-31"}}]},
        "recommendation_annotations": [{"recommendation_key": "local_keywords", "history_support": []}],
        "notes": [{"id": "n1", "severity": "info", "source": "history", "category": "data_quality",
                   "message": "Run was not used.", "audience": "reviewer"}],
        "validation_failures": [],
    }


def attempt():
    return {
        "schema_version": 1,
        "record_type": "attempt",
        "client_key": "example-client",
        "report_period": {"start": "2026-01-01", "end": "2026-01-31"},
        "attempted_at": TS,
        "step": "persist",
        "status_code": "persist_failed",
        "exception_class": "OperationalError",
        "run_id": None,
        "pdf_sha256": SHA_A,
        "pdf_bytes": 2048,
        "previous_report_restored": None,
        "previous_report_preserved_as": None,
        "finalized": False,
    }


def mutated(base, path, value):
    record = copy.deepcopy(base)
    target = record
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return record


def removed(base, path):
    record = copy.deepcopy(base)
    target = record
    for step in path[:-1]:
        target = target[step]
    del target[path[-1]]
    return record


def bad_finalized(path, value, match=None):
    with pytest.raises(RecordSchemaError, match=match):
        rs.validate_finalized_record(mutated(finalized(), path, value))


def bad_attempt(path, value, match=None):
    with pytest.raises(RecordSchemaError, match=match):
        rs.validate_attempt_record(mutated(attempt(), path, value))


# --- approved constants -------------------------------------------------------------------------------------------

def test_approved_closed_sets_are_exactly_those_in_revision_4():
    assert sorted(rs.ATTEMPT_STEPS) == sorted(APPROVED_STEPS)
    assert len(rs.ATTEMPT_STEPS) == 7
    assert sorted(rs.ATTEMPT_STATUS_CODES) == sorted(APPROVED_STATUS_CODES)
    assert len(rs.ATTEMPT_STATUS_CODES) == 15
    assert list(rs.ATTEMPT_STATUS_CODES[:11]) == ORIGINAL_STATUS_CODES  # the original eleven are unchanged and first
    assert list(rs.ATTEMPT_STATUS_CODES[11:]) == NEW_STATUS_CODES
    assert sorted(rs.EXCLUSION_REASON_CODES) == sorted([
        "no_rendered_event", "no_reviewer_record", "record_invalid", "record_mismatch", "final_pdf_missing",
        "pdf_hash_mismatch"])


def test_approved_field_names():
    assert set(rs.ATTEMPT_FIELDS) == {
        "schema_version", "record_type", "client_key", "report_period", "attempted_at", "step", "status_code",
        "exception_class", "run_id", "pdf_sha256", "pdf_bytes", "previous_report_restored",
        "previous_report_preserved_as", "finalized"}
    assert set(rs.FINALIZED_FIELDS) == {
        "schema_version", "record_type", "client_key", "report_period", "comparison_period", "snapshot",
        "finalization", "history", "client_facing_history", "ranking_table_markers",
        "recommendation_annotations", "notes", "validation_failures"}
    assert set(rs.FINALIZATION_FIELDS) == {
        "finalized_at", "pdf_sha256", "pdf_bytes", "pdf_checks", "client_content_sha256", "rendered_event",
        "superseded_previous_pdf", "duplicate_draft_preserved_as"}
    assert set(rs.HISTORY_FIELDS) == {
        "status", "failure_kind", "rules_version", "config", "baselines", "skipped_runs", "excluded_unfinalized",
        "dimension_status", "ranking_crosscheck", "diagnostics"}


# --- valid records ------------------------------------------------------------------------------------------------

def test_valid_records_accepted_and_returned_unchanged():
    for record, validate in ((finalized(), rs.validate_finalized_record), (attempt(), rs.validate_attempt_record)):
        before = copy.deepcopy(record)
        assert validate(record) is record
        assert record == before


def test_cross_type_records_rejected():
    with pytest.raises(RecordSchemaError, match="attempt record"):
        rs.validate_finalized_record(attempt())
    with pytest.raises(RecordSchemaError, match="finalized report"):
        rs.validate_attempt_record(finalized())


@pytest.mark.parametrize("value", [None, [], "text", 5])
def test_non_object_records_rejected(value):
    with pytest.raises(RecordSchemaError):
        rs.validate_finalized_record(value)
    with pytest.raises(RecordSchemaError):
        rs.validate_attempt_record(value)


# --- attempt records ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("step", APPROVED_STEPS)
def test_every_approved_attempt_step_accepted(step):
    rs.validate_attempt_record(mutated(attempt(), ["step"], step))


@pytest.mark.parametrize("status", NEW_STATUS_CODES)
def test_each_new_finalization_status_code_is_accepted_and_kept_exactly(status):
    record = mutated(attempt(), ["status_code"], status)
    assert rs.validate_attempt_record(record)["status_code"] == status


@pytest.mark.parametrize("status", ORIGINAL_STATUS_CODES)
def test_the_original_status_codes_are_still_accepted(status):
    rs.validate_attempt_record(mutated(attempt(), ["status_code"], status))


@pytest.mark.parametrize("step", APPROVED_STEPS)
@pytest.mark.parametrize("status", NEW_STATUS_CODES)
def test_new_status_codes_work_with_every_approved_step(step, status):
    record = mutated(mutated(attempt(), ["step"], step), ["status_code"], status)
    assert rs.validate_attempt_record(record) is record


@pytest.mark.parametrize("status", [
    "stale", "Stale_Bundle", "stale_bundle ", " stale_bundle", "stale-bundle", "draft_partial", "partial_conflict",
    "existing_report", "report_conflict", "reader_limit", "record_limit", "reader_record_limit\n",
    "replace_not_allowed", "pdf_record_conflict", "unknown", "", None, 15, True, ["stale_bundle"]])
def test_other_status_values_are_still_rejected(status):
    bad_attempt(["status_code"], status, "status_code")


def test_the_attempt_steps_and_fields_are_unchanged_by_the_new_codes():
    assert sorted(rs.ATTEMPT_STEPS) == sorted(APPROVED_STEPS) and len(rs.ATTEMPT_STEPS) == 7
    assert len(rs.ATTEMPT_FIELDS) == 14 and "status_code" in rs.ATTEMPT_FIELDS


def test_new_status_codes_do_not_relax_the_other_attempt_rules():
    record = mutated(attempt(), ["status_code"], "reader_record_limit")
    bad_attempt(["finalized"], True, "finalized")
    with pytest.raises(RecordSchemaError, match="unexpected"):
        rs.validate_attempt_record(dict(record, extra=1))
    with pytest.raises(RecordSchemaError, match="missing"):
        rs.validate_attempt_record(removed(record, ["exception_class"]))
    with pytest.raises(RecordSchemaError, match="exception_class"):
        rs.validate_attempt_record(mutated(record, ["exception_class"], "OSError: disk full"))


def test_finalized_records_do_not_accept_attempt_status_codes():
    record = finalized()
    record["status_code"] = "stale_bundle"
    with pytest.raises(RecordSchemaError, match="unexpected"):
        rs.validate_finalized_record(record)

@pytest.mark.parametrize("status", APPROVED_STATUS_CODES)
def test_every_approved_attempt_status_code_accepted(status):
    rs.validate_attempt_record(mutated(attempt(), ["status_code"], status))


@pytest.mark.parametrize("step", ["assemble", "draft_pdf", "persist_snapshot", "rendered_event", "reviewer_record",
                                  "recovery", "Persist", "PERSIST", " persist", "persist ", "", None, 3, ["persist"]])
def test_unapproved_attempt_steps_rejected(step):
    bad_attempt(["step"], step, "step")


@pytest.mark.parametrize("status", ["validation_failed", "exception", "io_error", "interrupted", "rolled_back",
                                    "rollback_failed", "failed", "Draft_Invalid", "draft_invalid ", "", None, 1])
def test_unapproved_attempt_status_codes_rejected(status):
    bad_attempt(["status_code"], status, "status_code")


@pytest.mark.parametrize("field", rs.ATTEMPT_FIELDS)
def test_every_attempt_field_is_required(field):
    with pytest.raises(RecordSchemaError, match="missing"):
        rs.validate_attempt_record(removed(attempt(), [field]))


def test_unapproved_attempt_fields_rejected():
    for extra in ("attempt_id", "validation_failure_codes", "attempt_started_at", "attempt_finished_at",
                  "superseded_run_id", "restoration_attempted", "message", "path"):
        record = attempt()
        record[extra] = None
        with pytest.raises(RecordSchemaError, match="unexpected"):
            rs.validate_attempt_record(record)
    for path in (["report_period"], ["previous_report_preserved_as"]):
        record = attempt()
        record["previous_report_preserved_as"] = {"sha256_16": "a" * 16, "variant_index": 1}
        target = record
        for step in path:
            target = target[step]
        target["extra"] = 1
        with pytest.raises(RecordSchemaError, match="unexpected"):
            rs.validate_attempt_record(record)


def test_attempt_must_not_be_finalized():
    for bad in (True, "false", 0, None):
        bad_attempt(["finalized"], bad, "finalized")
    bad_attempt(["finalized"], True, "false")


def test_attempt_nullable_fields_accept_null_and_values():
    record = attempt()
    record.update(exception_class=None, run_id=RUN, pdf_sha256=None, pdf_bytes=None, previous_report_restored=True,
                  previous_report_preserved_as={"sha256_16": "0123456789abcdef", "variant_index": 99})
    rs.validate_attempt_record(record)
    for flag in (True, False, None):
        rs.validate_attempt_record(mutated(attempt(), ["previous_report_restored"], flag))


@pytest.mark.parametrize("name", ["", "os error", "OSError: disk full", "sqlite3.OperationalError", "1Error",
                                  "a" * 65, "Error\n", "C:\\path\\Error", 5, True])
def test_invalid_exception_class_rejected(name):
    bad_attempt(["exception_class"], name, "exception_class")


def test_exception_class_identifier_limit_is_64_characters():
    rs.validate_attempt_record(mutated(attempt(), ["exception_class"], "E" * 64))


@pytest.mark.parametrize("value", [1, "yes", 0, [True]])
def test_previous_report_restored_must_be_bool_or_null(value):
    bad_attempt(["previous_report_restored"], value, "previous_report_restored")


@pytest.mark.parametrize("value", [
    "x", {"sha256_16": "a" * 16}, {"variant_index": 1}, {"sha256_16": "A" * 16, "variant_index": 1},
    {"sha256_16": "a" * 15, "variant_index": 1}, {"sha256_16": "a" * 17, "variant_index": 1},
    {"sha256_16": "a" * 16, "variant_index": 0}, {"sha256_16": "a" * 16, "variant_index": 100},
    {"sha256_16": "a" * 16, "variant_index": True}, {"sha256_16": "a" * 16, "variant_index": "2"},
    {"sha256_16": "a" * 16, "variant_index": 1, "path": "C:\\x.pdf"}, {"sha256_16": SHA_A, "variant_index": 1}])
def test_invalid_previous_report_preserved_as_rejected(value):
    bad_attempt(["previous_report_preserved_as"], value)


@pytest.mark.parametrize("digest", ["a" * 63, "a" * 65, "A" * 64, "g" * 64, " " + "a" * 63, "", 5])
def test_invalid_attempt_pdf_sha256_rejected(digest):
    bad_attempt(["pdf_sha256"], digest)


@pytest.mark.parametrize("count", [-1, (1 << 30) + 1, 1.5, "10", True])
def test_invalid_attempt_pdf_bytes_rejected(count):
    bad_attempt(["pdf_bytes"], count)


def test_attempt_pdf_bytes_may_be_zero_for_an_empty_draft():
    rs.validate_attempt_record(mutated(attempt(), ["pdf_bytes"], 0))
    rs.validate_attempt_record(mutated(attempt(), ["pdf_bytes"], rs.MAX_PDF_BYTES))


@pytest.mark.parametrize("run_id", ["", "not-a-uuid", RUN.upper(), RUN.replace("-", ""), RUN + " ", 5])
def test_invalid_attempt_run_id_rejected(run_id):
    bad_attempt(["run_id"], run_id)


@pytest.mark.parametrize("stamp", ["2026-02-03", "2026-02-03T09:30:00", "2026-02-03T09:30:00Z",
                                   "2026-02-03T09:30:00+01:00", "2026-02-03 09:30:00+00:00",
                                   "2026-02-30T09:30:00+00:00", "2026-02-03T09:30:00.5+00:00", None, 5])
def test_invalid_attempted_at_rejected(stamp):
    bad_attempt(["attempted_at"], stamp, "attempted_at")


# --- shared scalar fields -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("version", [0, 2, "1", 1.0, True, None])
def test_invalid_schema_version_rejected(version):
    bad_finalized(["schema_version"], version, "schema_version")
    bad_attempt(["schema_version"], version, "schema_version")


@pytest.mark.parametrize("key", ["", "Example-Client", "a/b", "..", "con", "x" * 64, None, 3])
def test_invalid_client_key_rejected(key):
    bad_finalized(["client_key"], key)
    bad_attempt(["client_key"], key)


def test_periods_validated():
    bad_finalized(["report_period", "start"], "2026-02-30")
    bad_finalized(["report_period", "end"], "20260131")
    bad_finalized(["report_period"], {"start": "2026-02-01", "end": "2026-01-31"}, "after")
    bad_finalized(["comparison_period"], {"start": "2025-12-01"}, "missing")
    bad_finalized(["comparison_period"], None)
    bad_attempt(["report_period", "start"], "nope")
    bad_attempt(["report_period"], {"start": "2026-01-01", "end": "2026-01-31", "extra": 1}, "unexpected")


# --- finalized record: structure ----------------------------------------------------------------------------------

@pytest.mark.parametrize("field", rs.FINALIZED_FIELDS)
def test_every_finalized_top_level_field_is_required(field):
    with pytest.raises(RecordSchemaError, match="missing"):
        rs.validate_finalized_record(removed(finalized(), [field]))


def test_finalized_flag_and_other_extra_fields_rejected():
    for extra in ("finalized", "pdf", "pass", "attempt_id"):
        record = finalized()
        record[extra] = True
        with pytest.raises(RecordSchemaError, match="unexpected"):
            rs.validate_finalized_record(record)


def test_unexpected_nested_fields_rejected_at_every_closed_level():
    paths = (["snapshot"], ["finalization"], ["finalization", "pdf_checks"], ["finalization", "rendered_event"],
             ["history"], ["client_facing_history", 0], ["ranking_table_markers"],
             ["ranking_table_markers", "cells", 0], ["notes", 0], ["report_period"])
    for path in paths:
        record = finalized()
        target = record
        for step in path:
            target = target[step]
        target["surprise"] = 1
        with pytest.raises(RecordSchemaError, match="unexpected"):
            rs.validate_finalized_record(record)


@pytest.mark.parametrize("path", [
    ["snapshot", "run_id"], ["snapshot", "revision"], ["snapshot", "content_hash"], ["snapshot", "quality"],
    ["finalization", "finalized_at"], ["finalization", "pdf_sha256"], ["finalization", "pdf_bytes"],
    ["finalization", "pdf_checks"], ["finalization", "client_content_sha256"], ["finalization", "rendered_event"],
    ["finalization", "superseded_previous_pdf"], ["finalization", "duplicate_draft_preserved_as"],
    ["finalization", "pdf_checks", "non_empty"], ["finalization", "pdf_checks", "header"],
    ["finalization", "pdf_checks", "eof_marker"], ["finalization", "pdf_checks", "preliminary"],
    ["finalization", "rendered_event", "recorded"], ["finalization", "rendered_event", "detail"],
    *[["history", name] for name in rs.HISTORY_FIELDS],
    *[["client_facing_history", 0, name] for name in rs.CLIENT_HISTORY_FIELDS],
    ["ranking_table_markers", "footnote_text"], ["ranking_table_markers", "cells"],
    *[["ranking_table_markers", "cells", 0, name] for name in rs.MARKER_CELL_FIELDS],
    *[["notes", 0, name] for name in rs.NOTE_FIELDS]])
def test_every_nested_field_is_required(path):
    with pytest.raises(RecordSchemaError, match="missing"):
        rs.validate_finalized_record(removed(finalized(), path))


def test_old_field_names_are_not_accepted():
    record = finalized()
    record["pdf"] = record["finalization"]
    del record["finalization"]
    with pytest.raises(RecordSchemaError):
        rs.validate_finalized_record(record)
    record = finalized()
    record["reviewer_notes"] = record.pop("notes")
    with pytest.raises(RecordSchemaError):
        rs.validate_finalized_record(record)


@pytest.mark.parametrize("quality", ["complete", "partial", "incomplete"])
def test_snapshot_quality_levels_accepted(quality):
    rs.validate_finalized_record(mutated(finalized(), ["snapshot", "quality"], quality))


@pytest.mark.parametrize("quality", ["good", "Complete", "", None, {"level": "complete"}, 1])
def test_invalid_snapshot_quality_rejected(quality):
    bad_finalized(["snapshot", "quality"], quality, "quality")


@pytest.mark.parametrize("revision", [0, -1, 1.5, "1", True, None, rs.MAX_REVISION + 1])
def test_invalid_revisions_rejected(revision):
    bad_finalized(["snapshot", "revision"], revision)


@pytest.mark.parametrize("run_id", ["", "not-a-uuid", RUN.upper(), RUN.replace("-", ""), RUN + " ", None, 5])
def test_invalid_snapshot_run_id_rejected(run_id):
    bad_finalized(["snapshot", "run_id"], run_id)


@pytest.mark.parametrize("digest", ["a" * 63, "a" * 65, "A" * 64, "g" * 64, " " + "a" * 63, "", None, 5])
def test_invalid_sha256_values_rejected(digest):
    bad_finalized(["snapshot", "content_hash"], digest)
    bad_finalized(["finalization", "pdf_sha256"], digest)
    bad_finalized(["finalization", "client_content_sha256"], digest)


@pytest.mark.parametrize("stamp", ["2026-02-03", "2026-02-03T09:30:00", "2026-02-03T09:30:00Z",
                                   "2026-02-03T09:30:00+01:00", "2026-02-30T09:30:00+00:00", None, 5])
def test_invalid_finalized_at_rejected(stamp):
    bad_finalized(["finalization", "finalized_at"], stamp, "finalized_at")


@pytest.mark.parametrize("count", [0, -1, (1 << 30) + 1, 12.5, "100", True, None])
def test_invalid_pdf_bytes_rejected(count):
    bad_finalized(["finalization", "pdf_bytes"], count, "pdf_bytes")


# --- finalization.pdf_checks and rendered_event -------------------------------------------------------------------

@pytest.mark.parametrize("name", ["non_empty", "header", "eof_marker", "preliminary"])
def test_pdf_checks_must_all_be_true(name):
    for bad in (False, None, 1, "true"):
        bad_finalized(["finalization", "pdf_checks", name], bad, name)


def test_pdf_checks_must_be_an_object():
    bad_finalized(["finalization", "pdf_checks"], [{"name": "header", "passed": True}])
    bad_finalized(["finalization", "pdf_checks"], True)


def test_rendered_event_rules():
    bad_finalized(["finalization", "rendered_event", "recorded"], False, "recorded")
    bad_finalized(["finalization", "rendered_event", "recorded"], "yes")
    bad_finalized(["finalization", "rendered_event", "detail"], "no hash here", "SHA-256")
    bad_finalized(["finalization", "rendered_event", "detail"], f"PDF {SHA_B}", "SHA-256")
    bad_finalized(["finalization", "rendered_event", "detail"], "", "empty")
    bad_finalized(["finalization", "rendered_event", "detail"], None)
    bad_finalized(["finalization", "rendered_event", "detail"], SHA_A + "x" * rs.MAX_TEXT_CHARS, "longer")


# --- superseded and duplicate preservation ------------------------------------------------------------------------

def test_valid_superseded_and_duplicate_metadata_accepted():
    record = finalized()
    record["finalization"]["superseded_previous_pdf"] = {"sha256_16": "b" * 16, "variant_index": 1}
    record["finalization"]["duplicate_draft_preserved_as"] = {"sha256_16": "a" * 16, "variant_index": 99}
    rs.validate_finalized_record(record)


@pytest.mark.parametrize("field", ["superseded_previous_pdf", "duplicate_draft_preserved_as"])
@pytest.mark.parametrize("value", [
    "x", [], {}, {"sha256_16": "b" * 16}, {"variant_index": 1}, {"sha256_16": "B" * 16, "variant_index": 1},
    {"sha256_16": "b" * 15, "variant_index": 1}, {"sha256_16": "b" * 16, "variant_index": 0},
    {"sha256_16": "b" * 16, "variant_index": 100}, {"sha256_16": "b" * 16, "variant_index": True},
    {"sha256_16": "b" * 16, "variant_index": 1, "filename": "report.pdf"},
    {"sha256_16": "b" * 16, "variant_index": 1, "path": "C:\\x"}])
def test_invalid_preservation_metadata_rejected(field, value):
    bad_finalized(["finalization", field], value)


def test_preservation_hash_relationships():
    bad_finalized(["finalization", "superseded_previous_pdf"], {"sha256_16": "a" * 16, "variant_index": 1},
                  "differ")
    bad_finalized(["finalization", "duplicate_draft_preserved_as"], {"sha256_16": "b" * 16, "variant_index": 1},
                  "match")


# --- history ------------------------------------------------------------------------------------------------------

def test_history_top_level_fields_are_exactly_the_approved_ten():
    assert list(rs.HISTORY_FIELDS) == ["status", "failure_kind", "rules_version", "config", "baselines",
                                       "skipped_runs", "excluded_unfinalized", "dimension_status",
                                       "ranking_crosscheck", "diagnostics"]


def test_history_scalar_fields():
    bad_finalized(["history", "status"], "")
    bad_finalized(["history", "status"], None)
    bad_finalized(["history", "failure_kind"], "")
    bad_finalized(["history", "failure_kind"], 5)
    rs.validate_finalized_record(mutated(finalized(), ["history", "failure_kind"], "database_missing"))
    rs.validate_finalized_record(mutated(finalized(), ["history", "rules_version"], 2))
    for bad in (None, "", True, 0, 1.5, []):
        bad_finalized(["history", "rules_version"], bad)


def test_history_config_structure():
    path = ["history", "config"]
    assert rs.CONFIG_FIELDS == ("lookback_periods", "max_gap_days", "list_limit")
    for field in rs.CONFIG_FIELDS:
        with pytest.raises(RecordSchemaError, match="missing"):
            rs.validate_finalized_record(removed(finalized(), path + [field]))
    bad_finalized(path + ["thresholds"], {}, "unexpected")
    for field, minimum in (("lookback_periods", 1), ("max_gap_days", 0), ("list_limit", 1)):
        rs.validate_finalized_record(mutated(finalized(), path + [field], minimum))
        bad_finalized(path + [field], minimum - 1, field)
        for bad in (True, 1.0, "3", None):
            bad_finalized(path + [field], bad, field)
    bad_finalized(path, None)
    bad_finalized(path, [])


def test_baselines_structure():
    path = ["history", "baselines"]
    assert rs.BASELINE_FIELDS == ("dimension", "run_id", "revision", "period_start", "period_end", "gap_days",
                                  "rank", "same_length")
    for field in rs.BASELINE_FIELDS:
        with pytest.raises(RecordSchemaError, match="missing"):
            rs.validate_finalized_record(removed(finalized(), path + [0, field]))
    for extra in ("created_at", "kind"):  # BaselineRef fields that are deliberately not serialized
        bad_finalized(path + [0, extra], "x", "unexpected")
    bad_finalized(path, "x")
    bad_finalized(path, {})
    bad_finalized(path, [1])
    bad_finalized(path, [baseline(dimension="rankings.other")], "dimension")
    bad_finalized(path, [baseline(run_id="bad")])
    bad_finalized(path, [baseline(revision=0)])
    bad_finalized(path, [baseline(period_start="2025-12-32")])
    bad_finalized(path, [baseline(period_start="2026-01-01")], "after")
    bad_finalized(path, [baseline(gap_days=-1)])
    bad_finalized(path, [baseline(gap_days=1.5)])
    bad_finalized(path, [baseline(rank=0)])
    bad_finalized(path, [baseline(rank=True)])
    bad_finalized(path, [baseline(same_length="yes")])
    rs.validate_finalized_record(mutated(finalized(), path + [0, "gap_days"], 0))
    rs.validate_finalized_record(mutated(finalized(), path + [0, "same_length"], False))


def test_baselines_one_per_dimension_and_none_allowed_when_nothing_is_eligible():
    path = ["history", "baselines"]
    bad_finalized(path, [baseline(), baseline(run_id=RUN3)], "repeats")
    all_dims = [baseline(dimension=d) for d in DIMENSIONS]
    record = mutated(finalized(), path, all_dims)
    for entry in record["history"]["dimension_status"]:
        entry.update(eligible=True, reasons=[], baseline_run_id=RUN2)
    rs.validate_finalized_record(record)
    bad_finalized(path, all_dims + [baseline()], "at most")
    record = finalized()
    record["history"]["baselines"] = []
    for entry in record["history"]["dimension_status"]:
        entry.update(eligible=False, baseline_run_id=None)
    record["history"]["diagnostics"]["baselines"] = {}
    rs.validate_finalized_record(record)


def consistent_record(eligible_dimensions):
    """A record where exactly `eligible_dimensions` are eligible, each with its own baseline."""
    record = finalized()
    history = record["history"]
    history["baselines"] = [baseline(dimension=d) for d in eligible_dimensions]
    for entry in history["dimension_status"]:
        on = entry["dimension"] in eligible_dimensions
        entry.update(eligible=on, reasons=[] if on else ["not eligible"], baseline_run_id=RUN2 if on else None)
    if not eligible_dimensions:
        history["diagnostics"]["baselines"] = {}
    return record


@pytest.mark.parametrize("eligible", [(), ("rankings.keywords",), ("gsc.totals", "ga4.daily"), tuple(DIMENSIONS)])
def test_fully_consistent_records_are_accepted(eligible):
    record = consistent_record(eligible)
    assert rs.validate_finalized_record(record) is record


def test_ineligible_dimension_with_a_baseline_is_rejected():
    record = consistent_record(("rankings.keywords",))
    record["history"]["baselines"].append(baseline(dimension="gsc.totals"))
    with pytest.raises(RecordSchemaError, match="not eligible"):
        rs.validate_finalized_record(record)


def test_ineligible_dimension_with_a_baseline_is_rejected_even_when_run_ids_agree():
    record = consistent_record(())
    record["history"]["baselines"] = [baseline(dimension="ga4.site", run_id=RUN2)]
    record["history"]["dimension_status"][DIMENSIONS.index("ga4.site")]["baseline_run_id"] = RUN2
    with pytest.raises(RecordSchemaError, match="baseline_run_id"):  # null is required for an ineligible dimension
        rs.validate_finalized_record(record)


def test_eligible_dimension_without_a_baseline_is_rejected():
    record = consistent_record(("rankings.keywords", "gsc.totals"))
    record["history"]["baselines"] = [baseline(dimension="rankings.keywords")]
    with pytest.raises(RecordSchemaError, match="baseline listed"):
        rs.validate_finalized_record(record)
    record = consistent_record(("rankings.keywords",))
    record["history"]["baselines"] = []
    with pytest.raises(RecordSchemaError, match="baseline listed"):
        rs.validate_finalized_record(record)


def test_baseline_for_a_dimension_missing_from_the_status_list_is_rejected():
    record = consistent_record(("rankings.keywords", "gsc.totals"))
    record["history"]["dimension_status"] = [e for e in record["history"]["dimension_status"]
                                             if e["dimension"] != "gsc.totals"]
    with pytest.raises(RecordSchemaError, match="all 12"):
        rs.validate_finalized_record(record)


def test_duplicate_baselines_for_one_dimension_are_rejected():
    record = consistent_record(("rankings.keywords",))
    record["history"]["baselines"].append(baseline(dimension="rankings.keywords", run_id=RUN3))
    with pytest.raises(RecordSchemaError, match="repeats"):
        rs.validate_finalized_record(record)
    record["history"]["baselines"][1] = baseline(dimension="rankings.keywords")  # an exact copy
    with pytest.raises(RecordSchemaError, match="repeats"):
        rs.validate_finalized_record(record)


def test_baseline_run_id_must_equal_the_dimension_status_run_id():
    record = consistent_record(("rankings.keywords",))
    record["history"]["baselines"][0]["run_id"] = RUN3
    with pytest.raises(RecordSchemaError, match="baseline listed"):
        rs.validate_finalized_record(record)
    record = consistent_record(("rankings.keywords", "gsc.totals"))
    record["history"]["baselines"][1]["run_id"] = RUN3  # status says RUN2
    with pytest.raises(RecordSchemaError, match="baseline listed"):
        rs.validate_finalized_record(record)


def test_two_dimensions_may_share_a_baseline_run_and_diagnostics_follow_the_listed_runs():
    record = consistent_record(("rankings.keywords", "gsc.totals"))
    rs.validate_finalized_record(record)
    record["history"]["baselines"][1]["run_id"] = RUN3
    record["history"]["dimension_status"][DIMENSIONS.index("gsc.totals")]["baseline_run_id"] = RUN3
    record["history"]["diagnostics"]["baselines"] = {RUN2: group(), RUN3: group()}
    rs.validate_finalized_record(record)
    record["history"]["diagnostics"]["baselines"][RUN3 + "x"] = group()
    with pytest.raises(RecordSchemaError):
        rs.validate_finalized_record(record)
    del record["history"]["diagnostics"]["baselines"][RUN3 + "x"]
    record["history"]["baselines"].pop(1)  # RUN3 is no longer a listed baseline run
    with pytest.raises(RecordSchemaError):
        rs.validate_finalized_record(record)


SKIP_REASONS = [
    "voided", "superseded by revision 2", "same period as the current report", "overlaps the current period",
    "later than the current period", "outside the lookback of 3 earlier periods",
    "a gap of 40 days exceeds the 35-day limit", "failed verification (2 problem(s)); not used",
    "unsupported snapshot format"]


def skipped(**overrides):
    value = {"run_id": RUN3, "revision": 1, "period_start": "2025-10-01", "period_end": "2025-10-31",
             "reason": "voided"}
    value.update(overrides)
    return value


@pytest.mark.parametrize("reason", SKIP_REASONS)
def test_every_skip_reason_the_history_stage_produces_is_accepted(reason):
    rs.validate_finalized_record(mutated(finalized(), ["history", "skipped_runs"], [skipped(reason=reason)]))


def test_skipped_runs_structure():
    path = ["history", "skipped_runs"]
    for field in rs.SKIPPED_FIELDS:
        with pytest.raises(RecordSchemaError, match="missing"):
            rs.validate_finalized_record(removed(finalized(), path + [0, field]))
    bad_finalized(path + [0, "extra"], 1, "unexpected")
    bad_finalized(path, "x")
    bad_finalized(path, [None])
    bad_finalized(path, [skipped(run_id="bad")])
    bad_finalized(path, [skipped(revision=0)])
    bad_finalized(path, [skipped(period_end="2025-09-30")], "after")
    bad_finalized(path, [skipped(period_start="x")])
    rs.validate_finalized_record(mutated(finalized(), path, []))
    for bad in ("", "Voided", "voided ", "superseded by revision x", "superseded by revision ", "overlaps",
                "a gap of -3 days exceeds the 35-day limit", "failed verification; not used",
                "failed verification (2 problems); not used", "unknown reason", None, 5, ["voided"]):
        bad_finalized(path, [skipped(reason=bad)], "reason")


def test_ranking_crosscheck_structure():
    path = ["history", "ranking_crosscheck"]
    assert rs.CROSSCHECK_FIELDS == ("fallback_used", "sheet_previous_mismatch", "new_keywords",
                                    "no_longer_tracked", "not_comparable")
    for field in rs.CROSSCHECK_FIELDS:
        with pytest.raises(RecordSchemaError, match="missing"):
            rs.validate_finalized_record(removed(finalized(), path + [field]))
    bad_finalized(path + ["agrees"], True, "unexpected")
    bad_finalized(path, None)
    bad_finalized(path, [])


def test_fallback_used_is_a_non_negative_count_where_zero_means_none():
    path = ["history", "ranking_crosscheck", "fallback_used"]
    rs.validate_finalized_record(mutated(finalized(), path, 0))
    rs.validate_finalized_record(mutated(finalized(), path, 7))
    for bad in (-1, None, True, False, 1.0, "0"):
        bad_finalized(path, bad, "fallback_used")


@pytest.mark.parametrize("field", ["sheet_previous_mismatch", "new_keywords", "no_longer_tracked", "not_comparable"])
def test_crosscheck_counts_are_non_negative_or_null(field):
    path = ["history", "ranking_crosscheck", field]
    for good in (0, 12, None):
        rs.validate_finalized_record(mutated(finalized(), path, good))
    for bad in (-1, True, 1.5, "1", [], {}):
        bad_finalized(path, bad, field)


def group(**changes):
    value = {"ga4": {"current": {"daily_sessions_diff": -3}, "previous": {"daily_sessions_diff": 2}}}
    value.update(changes)
    return value


def test_diagnostics_structure():
    path = ["history", "diagnostics"]
    assert rs.DIAGNOSTICS_FIELDS == ("current", "baselines")
    for field in rs.DIAGNOSTICS_FIELDS:
        with pytest.raises(RecordSchemaError, match="missing"):
            rs.validate_finalized_record(removed(finalized(), path + [field]))
    bad_finalized(path + ["previous"], {}, "unexpected")
    bad_finalized(path, None)
    bad_finalized(path, [])
    bad_finalized(path + ["current"], None)
    bad_finalized(path + ["current"], [])
    bad_finalized(path + ["baselines"], None)
    bad_finalized(path + ["baselines"], [])
    rs.validate_finalized_record(mutated(finalized(), path + ["current"], {}))
    rs.validate_finalized_record(mutated(finalized(), path + ["current"], group()))
    rs.validate_finalized_record(mutated(finalized(), path + ["baselines"], {}))


def test_diagnostic_group_shape_is_source_then_which_then_integer_counters():
    path = ["history", "diagnostics", "current"]
    bad_finalized(path, {"ga4": 5})
    bad_finalized(path, {"ga4": []})
    bad_finalized(path, {"ga4": {"today": {"x": 1}}}, "period key")
    bad_finalized(path, {"ga4": {"current": 5}})
    bad_finalized(path, {"ga4": {"current": {"x": 1.5}}})
    bad_finalized(path, {"ga4": {"current": {"x": True}}})
    bad_finalized(path, {"ga4": {"current": {"x": "1"}}})
    bad_finalized(path, {"ga4": {"current": {"x": None}}})
    bad_finalized(path, {"ga4": {"current": {"x": {"deep": 1}}}})
    bad_finalized(path, {"": {"current": {"x": 1}}})
    bad_finalized(path, {"ga4": {"current": {"": 1}}})
    bad_finalized(path, {1: {"current": {"x": 1}}})
    for which in rs.DIAGNOSTIC_WHICH:
        rs.validate_finalized_record(mutated(finalized(), path, {"gsc": {which: {"rows": 0, "negative": -4}}}))


def test_baseline_diagnostics_must_belong_to_listed_baseline_runs():
    path = ["history", "diagnostics", "baselines"]
    bad_finalized(path, {RUN3: group()}, "not in history.baselines")
    bad_finalized(path, {"bad": group()})
    bad_finalized(path, {RUN2: []})
    bad_finalized(path, {RUN2: {"ga4": {"current": {"x": 1.5}}}})
    rs.validate_finalized_record(mutated(finalized(), path, {RUN2: group()}))


def test_excluded_unfinalized_structure():
    good = {"run_id": RUN2, "revision": 2, "period_start": "2025-11-01", "period_end": "2025-11-30",
            "reason_code": "no_rendered_event"}
    record = mutated(finalized(), ["history", "excluded_unfinalized"], [good])
    rs.validate_finalized_record(record)
    for reason in rs.EXCLUSION_REASON_CODES:
        rs.validate_finalized_record(mutated(finalized(), ["history", "excluded_unfinalized"],
                                             [dict(good, reason_code=reason)]))
    path = ["history", "excluded_unfinalized"]
    for bad in (dict(good, reason_code="never_rendered"), dict(good, reason_code="NO_RENDERED_EVENT"),
                dict(good, reason_code=None), dict(good, run_id="bad"), dict(good, revision=0),
                dict(good, period_start="2025-11-31"), dict(good, period_end="2025-10-31"),
                dict(good, extra=1), {k: v for k, v in good.items() if k != "reason_code"},
                {k: v for k, v in good.items() if k != "period_start"},
                dict(good, period={"start": "2025-11-01", "end": "2025-11-30"}), None, "x"):
        bad_finalized(path, [bad])
    bad_finalized(path, "none")


def status_entries(**changes):
    entries = copy.deepcopy(finalized()["history"]["dimension_status"])
    for entry in entries:
        entry.update(changes.get(entry["dimension"], {}))
    return entries


def test_dimension_status_has_exactly_twelve_entries_one_per_dimension():
    assert len(DIMENSIONS) == 12
    path = ["history", "dimension_status"]
    entries = finalized()["history"]["dimension_status"]
    assert [e["dimension"] for e in entries] == list(DIMENSIONS)
    rs.validate_finalized_record(finalized())
    bad_finalized(path, entries[:11], "all 12")
    bad_finalized(path, [], "all 12")
    bad_finalized(path, entries + [copy.deepcopy(entries[0])], "at most")
    duplicated = copy.deepcopy(entries)
    duplicated[1] = copy.deepcopy(duplicated[0])
    bad_finalized(path, duplicated, "repeats")
    unknown = copy.deepcopy(entries)
    unknown[0]["dimension"] = "rankings.other"
    bad_finalized(path, unknown, "dimension")
    bad_finalized(path, {"rankings.keywords": {}}, "list")
    bad_finalized(path, ["rankings.keywords"], "object")


def test_dimension_status_entries_are_closed():
    path = ["history", "dimension_status", 3]
    for field in rs.STATUS_FIELDS:
        with pytest.raises(RecordSchemaError, match="missing"):
            rs.validate_finalized_record(removed(finalized(), path + [field]))
    for extra in ("baseline", "reason", "run_id"):
        bad_finalized(path + [extra], None, "unexpected")


def test_dimension_status_text_lists():
    path = ["history", "dimension_status", 3]
    bad_finalized(path + ["reasons"], "text")
    bad_finalized(path + ["reasons"], [""], "empty")
    bad_finalized(path + ["reasons"], [None])
    bad_finalized(path + ["reasons"], [5])
    bad_finalized(path + ["caveats"], [""], "empty")
    bad_finalized(path + ["caveats"], "text")
    bad_finalized(path + ["caveats"], ["x"] * 51, "at most")
    bad_finalized(path + ["reasons"], ["x" * (rs.MAX_STATUS_TEXT_CHARS + 1)], "longer")
    record = mutated(mutated(finalized(), path + ["reasons"], ["Any variable text, e.g. 12 days"]),
                     path + ["caveats"], ["GA4 sampled this data, so it is not exact"])
    rs.validate_finalized_record(record)  # variable text is accepted: the existing code has no closed set


def test_dimension_status_baseline_consistency():
    ranking = ["history", "dimension_status", 0]
    other = ["history", "dimension_status", 3]
    bad_finalized(ranking + ["baseline_run_id"], None, "UUID")  # eligible needs a baseline
    bad_finalized(ranking + ["baseline_run_id"], RUN3, "baseline listed")  # not the listed baseline run
    bad_finalized(ranking + ["baseline_run_id"], "bad")
    bad_finalized(ranking + ["eligible"], "yes")
    bad_finalized(other + ["baseline_run_id"], RUN2, "null")  # ineligible must be null
    bad_finalized(other + ["baseline_run_id"], "")
    # an eligible dimension needs a baseline entry for that same dimension
    record = mutated(finalized(), other + ["eligible"], True)
    record["history"]["dimension_status"][3]["baseline_run_id"] = RUN2
    with pytest.raises(RecordSchemaError, match="baseline listed"):
        rs.validate_finalized_record(record)
    record["history"]["baselines"].append(baseline(dimension=DIMENSIONS[3]))
    rs.validate_finalized_record(record)
    # a baseline of another dimension does not count, even with the same run id
    record["history"]["baselines"][1]["run_id"] = RUN3
    with pytest.raises(RecordSchemaError, match="baseline listed"):
        rs.validate_finalized_record(record)



# --- client-facing history: exact text ----------------------------------------------------------------------------

def test_client_facing_text_preserved_exactly():
    record = finalized()
    result = rs.validate_finalized_record(record)
    stored = result["client_facing_history"][0]["text"]
    assert stored == EXACT_TEXT
    assert stored.encode("utf-8") == EXACT_TEXT.encode("utf-8")  # same code points, no normalization
    assert result["ranking_table_markers"]["footnote_text"] == "\u25b2 moved up since last report "
    assert result["ranking_table_markers"]["cells"][0]["text"] == "\u25b2 3"


def test_unusual_but_non_empty_text_is_not_rejected_or_rewritten():
    for text in (" ", "\n", "\u200b", "tab\there", "  padded  ", "e\u0301", "caf\u00e9", "x" * rs.MAX_TEXT_CHARS):
        record = mutated(finalized(), ["client_facing_history", 0, "text"], text)
        assert rs.validate_finalized_record(record)["client_facing_history"][0]["text"] == text


def test_client_facing_history_structure():
    path = ["client_facing_history", 0]
    bad_finalized(["client_facing_history"], "text")
    bad_finalized(path + ["text"], "")
    bad_finalized(path + ["text"], None)
    bad_finalized(path + ["text"], "x" * (rs.MAX_TEXT_CHARS + 1), "longer")
    bad_finalized(path + ["dimension"], "rankings.other")
    bad_finalized(path + ["dimension"], None)
    bad_finalized(path + ["section"], "")
    bad_finalized(path + ["key"], None)
    bad_finalized(path + ["baseline_period"], {"start": "2025-12-31", "end": "2025-12-01"})
    bad_finalized(path + ["baseline_period"], None)
    bad_finalized(path + ["evidence"], None, "object")
    bad_finalized(path + ["evidence"], [1], "object")
    bad_finalized(path + ["evidence"], {"": 1})
    rs.validate_finalized_record(mutated(finalized(), path + ["evidence"], {}))


EVIDENCE = ["client_facing_history", 0, "evidence"]


def test_evidence_accepts_finite_numbers_without_rounding():
    values = {"int": 3, "zero": 0, "negative": -7, "float": 0.1 + 0.2, "tiny": 1e-300, "huge": 10**30,
              "third": 1 / 3, "neg_float": -0.0}
    record = mutated(finalized(), EVIDENCE, values)
    stored = rs.validate_finalized_record(record)["client_facing_history"][0]["evidence"]
    assert stored is values
    assert stored["float"] == 0.30000000000000004 and stored["third"] == 1 / 3
    assert all(type(stored[k]) is type(values[k]) for k in values)  # ints stay ints, floats stay floats


@pytest.mark.parametrize("bad", [
    True, False, "3", "", None, [], [1], {"nested": 1}, (1,), float("nan"), float("inf"), float("-inf"),
    complex(1, 0), b"1"])
def test_evidence_rejects_everything_but_finite_numbers(bad):
    bad_finalized(EVIDENCE, {"figure": bad}, "figure")


@pytest.mark.parametrize("bad", [None, [], [1, 2], "x", 5, True])
def test_evidence_must_be_an_object(bad):
    bad_finalized(EVIDENCE, bad, "object")


def test_evidence_keys_must_be_non_empty_short_text():
    bad_finalized(EVIDENCE, {"": 1})
    bad_finalized(EVIDENCE, {"k" * (rs.MAX_LABEL_CHARS + 1): 1}, "longer")
    bad_finalized(EVIDENCE, {1: 1})
    rs.validate_finalized_record(mutated(finalized(), EVIDENCE, {"k" * rs.MAX_LABEL_CHARS: 1}))


def test_evidence_is_not_checked_against_a_key_registry_yet():
    # Allowed keys per statement belong to the Stage 6c-4 history evidence registry.
    rs.validate_finalized_record(mutated(finalized(), EVIDENCE, {"any_key_at_all": 1}))


def test_target_location_is_kept_exactly_as_the_sheet_has_it():
    path = ["ranking_table_markers", "cells", 0, "target_location"]
    for text in ("", " ", "  Leeds,  UK ", "Zürich\u00a0Altstadt", "e\u0301", "../..\\evil", "C:\\x\\y.pdf", "a\nb"):
        stored = rs.validate_finalized_record(mutated(finalized(), path, text))
        assert stored["ranking_table_markers"]["cells"][0]["target_location"] == text


def test_records_hold_no_path_fields_and_text_is_never_turned_into_one():
    record = finalized()
    names = {name for part in (rs.FINALIZED_FIELDS, rs.FINALIZATION_FIELDS, rs.HISTORY_FIELDS, rs.BASELINE_FIELDS,
                               rs.SKIPPED_FIELDS, rs.STATUS_FIELDS, rs.CLIENT_HISTORY_FIELDS, rs.MARKER_CELL_FIELDS,
                               rs.ATTEMPT_FIELDS) for name in part}
    assert not {n for n in names if "path" in n or "file" in n}
    assert rs.validate_finalized_record(record) is record


# --- ranking table markers ----------------------------------------------------------------------------------------

def test_ranking_table_markers_structure():
    base = ["ranking_table_markers"]
    bad_finalized(base, [])
    bad_finalized(base + ["footnote_text"], None)
    bad_finalized(base + ["cells"], {})
    cell = base + ["cells", 0]
    bad_finalized(cell + ["keyword"], "")
    bad_finalized(cell + ["keyword"], "k" * (rs.MAX_KEYWORD_CHARS + 1))
    bad_finalized(cell + ["target_location"], None, "text")
    bad_finalized(cell + ["target_location"], {"table": 1}, "text")
    bad_finalized(cell + ["target_location"], 5, "text")
    bad_finalized(cell + ["target_location"], "x" * (rs.MAX_KEYWORD_CHARS + 1), "longer")
    bad_finalized(cell + ["text"], "")
    bad_finalized(cell + ["source"], "")
    bad_finalized(cell + ["baseline_period"], {"start": "x", "end": "y"})
    bad_finalized(cell + ["baseline_period"], None)
    rs.validate_finalized_record(mutated(finalized(), base + ["footnote_text"], ""))
    rs.validate_finalized_record(mutated(finalized(), base + ["cells"], []))
    rs.validate_finalized_record(mutated(finalized(), cell + ["target_location"], ""))


# --- recommendation annotations, notes, validation failures -------------------------------------------------------

def test_nothing_is_deferred_any_more():
    assert rs.DEFERRED_CONTRACTS == {}
    assert "history_support" in rs.__doc__ and "Nothing is deferred" in rs.__doc__


def test_recommendation_annotation_structure_and_empty_support():
    path = ["recommendation_annotations"]
    rs.validate_finalized_record(mutated(finalized(), path, []))
    one = {"recommendation_key": "local_keywords", "history_support": []}
    rs.validate_finalized_record(mutated(finalized(), path, [one, dict(one, recommendation_key="other")]))
    bad_finalized(path, "x")
    bad_finalized(path, {})
    bad_finalized(path, ["x"])
    bad_finalized(path, [{"recommendation_key": "a"}], "missing")
    bad_finalized(path, [{"history_support": []}], "missing")
    bad_finalized(path, [dict(one, extra=1)], "unexpected")
    bad_finalized(path, [dict(one, recommendation_key="")])
    bad_finalized(path, [dict(one, recommendation_key=None)])
    bad_finalized(path, [dict(one, history_support=None)])
    bad_finalized(path, [dict(one, history_support={})])
    bad_finalized(path, [one, dict(one)], "repeats")
    for content in (["x"], [1], [{}], [None]):  # anything that is not a closed support item
        bad_finalized(path, [dict(one, history_support=content)])


SUPPORT_RUN = "0198c1a2-7b3c-7d4e-8f5a-1234567890ab"


def support(source="gsc_ctr", **overrides):
    values = {
        "gsc_position": ("gsc.totals", None, {"current_value": 35.0, "baseline_value": 29.0, "direction": 1,
                                              "baseline_days": 28}),
        "gsc_ctr": ("gsc.totals", None, {"current_value": 0.002, "baseline_value": 0.004, "direction": -1,
                                         "baseline_days": 28}),
        "ga4_engagement_rate": ("ga4.site", None, {"current_value": 0.2, "baseline_value": 0.4, "direction": -1,
                                                   "baseline_days": 28}),
        "ranking_keyword": ("rankings.keywords", "roof repair", {"baseline_value": 7, "baseline_days": 28}),
    }[source]
    item = {"classification": "supports", "source": source, "subject": values[1], "dimension": values[0],
            "baseline_run_id": SUPPORT_RUN, "baseline_period": {"start": "2026-09-01", "end": "2026-09-28"},
            "evidence": dict(values[2]), "rules_version": "1"}
    item.update(overrides)
    return item


def annotated(*items, key="accelerate_positions"):
    return [{"recommendation_key": key, "history_support": list(items)}]


def bad_support(item, match=None):
    with pytest.raises(RecordSchemaError, match=match):
        rs.validate_finalized_record(mutated(finalized(), ["recommendation_annotations"], annotated(item)))


SOURCES = ["gsc_position", "gsc_ctr", "ga4_engagement_rate", "ranking_keyword"]


def test_the_support_constants_describe_the_approved_item():
    assert rs.SUPPORT_FIELDS == ("classification", "source", "subject", "dimension", "baseline_run_id",
                                 "baseline_period", "evidence", "rules_version")
    assert rs.SUPPORT_CLASSIFICATIONS == ("supports", "qualifies") and rs.SUPPORT_SOURCES == tuple(SOURCES)
    assert rs.SUPPORT_SOURCE_DIMENSIONS == {"gsc_position": "gsc.totals", "gsc_ctr": "gsc.totals",
                                            "ga4_engagement_rate": "ga4.site", "ranking_keyword": "rankings.keywords"}
    assert rs.MAX_SUPPORT_ITEMS == 5
    from seo_report.insights import history_rules
    assert all(set(keys) <= set(history_rules.EVIDENCE_KEYS) for keys in rs.SUPPORT_EVIDENCE_KEYS.values())


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("classification", ["supports", "qualifies"])
def test_a_valid_support_item_is_accepted_for_each_source_and_classification(source, classification):
    record = mutated(finalized(), ["recommendation_annotations"],
                     annotated(support(source, classification=classification)))
    assert rs.validate_finalized_record(record) is record


@pytest.mark.parametrize("field", rs.SUPPORT_FIELDS)
def test_every_support_field_is_required(field):
    item = support()
    del item[field]
    bad_support(item, "missing")


@pytest.mark.parametrize("extra", ["note", "baseline_value", "run_id", "path", "message"])
def test_extra_support_fields_are_rejected(extra):
    bad_support(dict(support(), **{extra: "x"}), "unexpected")


@pytest.mark.parametrize("value", ["support", "Supports", "complicates", "no_usable_support", "", None, 1, True])
def test_invalid_classification_is_rejected(value):
    bad_support(support(classification=value), "classification")


@pytest.mark.parametrize("value", ["gsc_clicks", "GSC_CTR", "ga4_sessions", "keyword", "", None, 5])
def test_invalid_source_is_rejected(value):
    bad_support(dict(support(), source=value), "source")


@pytest.mark.parametrize("source,wrong", [
    ("gsc_position", "ga4.site"), ("gsc_position", "rankings.keywords"), ("gsc_ctr", "gsc.countries"),
    ("ga4_engagement_rate", "gsc.totals"), ("ga4_engagement_rate", "ga4.organic"),
    ("ranking_keyword", "gsc.totals"), ("ranking_keyword", "ga4.site")])
def test_a_dimension_that_does_not_match_the_source_is_rejected(source, wrong):
    bad_support(support(source, dimension=wrong), "dimension")


@pytest.mark.parametrize("value", ["gsc.other", "", None, 5, ["gsc.totals"]])
def test_an_unknown_or_malformed_dimension_is_rejected(value):
    bad_support(support(dimension=value), "dimension")


@pytest.mark.parametrize("subject", [None, "", "   ", 5, True, ["roof"], {"a": 1}, "k" * (rs.MAX_KEYWORD_CHARS + 1)])
def test_a_ranking_keyword_needs_a_non_empty_text_subject(subject):
    bad_support(support("ranking_keyword", subject=subject), "subject")


def test_a_keyword_subject_is_kept_exactly():
    subject = "  Roof  Repair — Dallas TX "
    record = mutated(finalized(), ["recommendation_annotations"],
                     annotated(support("ranking_keyword", subject=subject)))
    assert rs.validate_finalized_record(record)["recommendation_annotations"][0]["history_support"][0]["subject"] == subject


@pytest.mark.parametrize("source", ["gsc_position", "gsc_ctr", "ga4_engagement_rate"])
@pytest.mark.parametrize("subject", ["roof repair", "", 5, False, []])
def test_a_non_keyword_source_requires_a_null_subject(source, subject):
    bad_support(support(source, subject=subject), "subject")


@pytest.mark.parametrize("run_id", ["run-1", "", "not-a-uuid", SUPPORT_RUN.upper(), SUPPORT_RUN.replace("-", ""),
                                    SUPPORT_RUN + " ", None, 5])
def test_the_baseline_run_id_must_be_a_uuid(run_id):
    bad_support(support(baseline_run_id=run_id), "baseline_run_id")


@pytest.mark.parametrize("period", [
    {"start": "2026-09-01"}, {"end": "2026-09-28"}, {}, None, "2026-09-01", ["2026-09-01", "2026-09-28"],
    {"start": "2026-09-01", "end": "2026-09-28", "extra": 1}, {"start": "2026-9-1", "end": "2026-09-28"},
    {"start": "2026-02-30", "end": "2026-03-28"}, {"start": "20260901", "end": "2026-09-28"},
    {"start": "2026-09-01", "end": "not a date"}, {"start": 20260901, "end": "2026-09-28"},
    {"start": "2026-09-28", "end": "2026-09-01"}])
def test_invalid_or_reversed_baseline_periods_are_rejected(period):
    bad_support(support(baseline_period=period), "baseline_period")


def test_a_one_day_baseline_period_is_allowed():
    rs.validate_finalized_record(mutated(
        finalized(), ["recommendation_annotations"],
        annotated(support(baseline_period={"start": "2026-09-01", "end": "2026-09-01"}))))


@pytest.mark.parametrize("key", ["made_up", "pending_fallbacks", "min_impressions", "sheet_mismatches",
                                 "daily_sessions_diff", "baseline_runs", "new_keywords"])
def test_unknown_and_reviewer_only_evidence_keys_are_rejected(key):
    item = support()
    item["evidence"][key] = 1
    bad_support(item, "evidence")


@pytest.mark.parametrize("source,key", [
    ("gsc_ctr", "change_percent"), ("gsc_position", "share_current"), ("ga4_engagement_rate", "impressions_current"),
    ("ranking_keyword", "current_value"), ("ranking_keyword", "direction")])
def test_registered_keys_that_the_source_does_not_use_are_rejected(source, key):
    item = support(source)
    item["evidence"][key] = 1
    bad_support(item, "may not use")


@pytest.mark.parametrize("source", SOURCES)
def test_every_evidence_key_of_a_source_is_required(source):
    for key in rs.SUPPORT_EVIDENCE_KEYS[source]:
        item = support(source)
        del item["evidence"][key]
        bad_support(item, "missing required")


@pytest.mark.parametrize("value", [{"a": 1}, [1], (1,), "6", None, b"1"])
def test_nested_or_non_numeric_evidence_is_rejected(value):
    for key in ("current_value", "baseline_value"):
        item = support()
        item["evidence"][key] = value
        bad_support(item, "evidence")


@pytest.mark.parametrize("value", [True, False])
def test_boolean_evidence_is_rejected(value):
    for key in ("current_value", "baseline_value", "direction", "baseline_days"):
        item = support()
        item["evidence"][key] = value
        bad_support(item, "evidence")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinity_are_rejected(value):
    for key in ("current_value", "baseline_value"):
        item = support()
        item["evidence"][key] = value
        bad_support(item, "evidence")


def test_the_evidence_must_be_an_object():
    for value in (None, [], "x", 5):
        bad_support(support(evidence=value), "evidence")
    bad_support(support(evidence={}), "missing required")


@pytest.mark.parametrize("value", [2, -2, 0.5, "1", None])
def test_direction_must_be_minus_one_zero_or_one(value):
    item = support()
    item["evidence"]["direction"] = value
    bad_support(item, "direction")
    for good in (-1, 0, 1):
        ok = support()
        ok["evidence"]["direction"] = good
        rs.validate_finalized_record(mutated(finalized(), ["recommendation_annotations"], annotated(ok)))


@pytest.mark.parametrize("days", [0, -1, 28.0, 1.5, "28", None])
def test_baseline_days_must_be_a_positive_whole_number(days):
    item = support()
    item["evidence"]["baseline_days"] = days
    bad_support(item, "baseline_days")


@pytest.mark.parametrize("value", [0, -3, 7.0, 7.5, "7", None])
def test_a_keyword_baseline_position_must_be_a_positive_whole_number(value):
    item = support("ranking_keyword")
    item["evidence"]["baseline_value"] = value
    bad_support(item, "baseline_value")


@pytest.mark.parametrize("version", ["", None, 1, True, ["1"], "v" * 65])
def test_empty_or_invalid_rules_versions_are_rejected(version):
    bad_support(support(rules_version=version), "rules_version")


def test_multiple_valid_support_items_are_accepted():
    items = [support("gsc_position"), support("gsc_ctr", classification="qualifies"),
             support("ga4_engagement_rate"), support("ranking_keyword", subject="a"),
             support("ranking_keyword", subject="b")]
    record = mutated(finalized(), ["recommendation_annotations"], annotated(*items))
    assert rs.validate_finalized_record(record) is record


def test_at_most_five_support_items_per_recommendation():
    items = [support("ranking_keyword", subject=f"keyword {n}") for n in range(6)]
    rs.validate_finalized_record(mutated(finalized(), ["recommendation_annotations"], annotated(*items[:5])))
    bad_support_list = mutated(finalized(), ["recommendation_annotations"], annotated(*items))
    with pytest.raises(RecordSchemaError, match="at most 5"):
        rs.validate_finalized_record(bad_support_list)


def test_support_items_for_several_recommendations_and_duplicate_keys():
    entries = annotated(support(), key="improve_ctr") + annotated(key="local_seo") + annotated(
        support("ranking_keyword"), key="review_x")
    rs.validate_finalized_record(mutated(finalized(), ["recommendation_annotations"], entries))
    repeated = annotated(support(), key="improve_ctr") + annotated(support("gsc_position"), key="improve_ctr")
    with pytest.raises(RecordSchemaError, match="repeats"):
        rs.validate_finalized_record(mutated(finalized(), ["recommendation_annotations"], repeated))
    six = [{"recommendation_key": f"r{n}", "history_support": []} for n in range(6)]
    with pytest.raises(RecordSchemaError, match="at most 5"):
        rs.validate_finalized_record(mutated(finalized(), ["recommendation_annotations"], six))


def test_an_empty_support_list_remains_valid_and_errors_name_the_field():
    rs.validate_finalized_record(mutated(finalized(), ["recommendation_annotations"], annotated()))
    with pytest.raises(RecordSchemaError, match=r"recommendation_annotations\[0\]\.history_support\[1\]\.source"):
        rs.validate_finalized_record(mutated(
            finalized(), ["recommendation_annotations"], annotated(support(), dict(support(), source="nope"))))


def test_attempt_records_still_reject_recommendation_annotations():
    record = attempt()
    record["recommendation_annotations"] = annotated(support())
    with pytest.raises(RecordSchemaError, match="unexpected"):
        rs.validate_attempt_record(record)

def test_recommendation_annotations_limited_to_five():
    items = [{"recommendation_key": f"rec{n}", "history_support": []} for n in range(6)]
    rs.validate_finalized_record(mutated(finalized(), ["recommendation_annotations"], items[:5]))
    bad_finalized(["recommendation_annotations"], items, "at most 5")


FAILURE_REASONS = ["invalid_evidence", "untraced_number", "unsafe_wording", "missing_baseline_period",
                   "duplicate_of_base_insight", "inconsistent_comparison", "invalid_identity"]


def failure(**overrides):
    value = {"key": "history.gsc.clicks", "reason": "unsafe_wording"}
    value.update(overrides)
    return value


def test_the_seven_approved_reason_codes_are_exactly_the_stage_6c_4_codes():
    from seo_report.insights import history_rules
    assert list(rs.VALIDATION_FAILURE_REASONS) == FAILURE_REASONS
    assert set(rs.VALIDATION_FAILURE_REASONS) == set(history_rules.VALIDATION_REASONS)
    assert rs.VALIDATION_FAILURE_FIELDS == ("key", "reason")


def test_an_empty_validation_failure_list_remains_valid():
    record = finalized()
    assert record["validation_failures"] == []
    assert rs.validate_finalized_record(record) is record


@pytest.mark.parametrize("reason", FAILURE_REASONS)
def test_each_approved_reason_code_is_accepted(reason):
    record = mutated(finalized(), ["validation_failures"], [failure(reason=reason)])
    assert rs.validate_finalized_record(record) is record


def test_multiple_valid_entries_are_accepted_and_returned_unchanged():
    entries = [failure(key=f"history.k{n}", reason=reason) for n, reason in enumerate(FAILURE_REASONS)]
    entries.append(failure())  # the same statement may fail for more than one reason
    entries.append(failure(reason="invalid_evidence"))
    record = mutated(finalized(), ["validation_failures"], entries)
    before = copy.deepcopy(record)
    assert rs.validate_finalized_record(record) is record and record == before


@pytest.mark.parametrize("reason", ["unknown", "", "Unsafe_Wording", "unsafe_wording ", "invalid_evidence\n",
                                    "duplicate", "validation_failed", None, 5, True, ["unsafe_wording"]])
def test_unknown_or_malformed_reason_codes_are_rejected(reason):
    bad_finalized(["validation_failures"], [failure(reason=reason)], "reason")


def test_a_missing_key_or_reason_is_rejected():
    bad_finalized(["validation_failures"], [{"reason": "unsafe_wording"}], "missing")
    bad_finalized(["validation_failures"], [{"key": "history.gsc.clicks"}], "missing")
    bad_finalized(["validation_failures"], [{}], "missing")


@pytest.mark.parametrize("extra", ["detail", "message", "path", "code", "severity"])
def test_additional_fields_are_rejected(extra):
    bad_finalized(["validation_failures"], [failure(**{extra: "x"})], "unexpected")


@pytest.mark.parametrize("key", ["", " ", "   ", "\t", "\n", " "])
def test_empty_or_whitespace_only_keys_are_rejected(key):
    bad_finalized(["validation_failures"], [failure(key=key)], "key")


@pytest.mark.parametrize("key", [None, 5, 1.5, True, ["history.gsc.clicks"], {"a": 1}, b"history"])
def test_invalid_key_types_are_rejected(key):
    bad_finalized(["validation_failures"], [failure(key=key)], "key")


def test_overlong_keys_are_rejected():
    rs.validate_finalized_record(mutated(finalized(), ["validation_failures"],
                                         [failure(key="k" * rs.MAX_LABEL_CHARS)]))
    bad_finalized(["validation_failures"], [failure(key="k" * (rs.MAX_LABEL_CHARS + 1))], "longer")


@pytest.mark.parametrize("value", [None, "none", {}, 5, True, "history.gsc.clicks"])
def test_the_list_itself_must_be_a_list(value):
    bad_finalized(["validation_failures"], value, "list")


@pytest.mark.parametrize("entry", [None, "x", 5, ["key", "reason"], ("key", "reason")])
def test_each_entry_must_be_an_object(entry):
    bad_finalized(["validation_failures"], [entry], "object")


def test_the_entry_count_is_limited_like_other_lists():
    entries = [failure(key=f"history.k{n}") for n in range(rs.MAX_LIST_ITEMS)]
    rs.validate_finalized_record(mutated(finalized(), ["validation_failures"], entries))
    bad_finalized(["validation_failures"], entries + [failure()], "at most")


def test_errors_name_the_offending_entry_and_field():
    with pytest.raises(RecordSchemaError, match=r"validation_failures\[1\]\.reason"):
        rs.validate_finalized_record(mutated(
            finalized(), ["validation_failures"], [failure(), failure(reason="nope")]))


def test_validation_failure_checks_do_not_touch_the_other_sections():
    record = mutated(finalized(), ["validation_failures"], [failure()])
    record["recommendation_annotations"] = [{"recommendation_key": "a", "history_support": []}]
    record["notes"] = []
    assert rs.validate_finalized_record(record) is record
    bad_finalized(["recommendation_annotations", 0, "history_support"], [{"x": 1}], "missing")
    rs.validate_finalized_record(mutated(finalized(), ["recommendation_annotations"], annotated(support())))


def test_attempt_records_have_no_validation_failures_field():
    record = attempt()
    record["validation_failures"] = [failure()]
    with pytest.raises(RecordSchemaError, match="unexpected"):
        rs.validate_attempt_record(record)

def test_notes_structure():
    path = ["notes", 0]
    for severity in ("info", "warning"):
        rs.validate_finalized_record(mutated(finalized(), path + ["severity"], severity))
    for source in ("rankings", "gsc", "geography", "ga4", "history", "engine", "recommendations"):
        rs.validate_finalized_record(mutated(finalized(), path + ["source"], source))
    for category in ("suppressed", "data_quality", "diagnostic", "limitation"):
        rs.validate_finalized_record(mutated(finalized(), path + ["category"], category))
    bad_finalized(path + ["severity"], "error")
    bad_finalized(path + ["source"], "gsa")
    bad_finalized(path + ["category"], "other")
    bad_finalized(path + ["audience"], "client", "reviewer")
    bad_finalized(path + ["audience"], None)
    bad_finalized(path + ["message"], "")
    bad_finalized(path + ["id"], "")
    bad_finalized(["notes"], "note")
    bad_finalized(["notes"], ["note"])
    rs.validate_finalized_record(mutated(finalized(), ["notes"], []))


ALL_NOTE_SOURCES = ["rankings", "gsc", "geography", "ga4", "history", "engine", "recommendations"]


def test_the_allowed_note_sources_are_exactly_the_seven_approved_values():
    assert list(rs.NOTE_SOURCES) == ALL_NOTE_SOURCES


@pytest.mark.parametrize("source", ALL_NOTE_SOURCES)
def test_every_approved_note_source_is_accepted_and_kept_exactly(source):
    record = mutated(finalized(), ["notes", 0, "source"], source)
    result = rs.validate_finalized_record(record)
    assert result["notes"][0]["source"] == source


def test_engine_and_recommendation_notes_like_the_real_ones_are_accepted():
    notes = [
        {"id": "base.001", "severity": "info", "source": "engine", "category": "suppressed",
         "message": "Keyword Rankings: left out by the paragraph limits: keywords.moved_up.", "audience": "reviewer"},
        {"id": "base.002", "severity": "warning", "source": "engine", "category": "suppressed",
         "message": "Insight gsc.clicks was left out: the underlying data is not available.", "audience": "reviewer"},
        {"id": "base.003", "severity": "info", "source": "recommendations", "category": "suppressed",
         "message": "The local SEO recommendation was not made: none of the keywords has a printable target location.",
         "audience": "reviewer"},
        {"id": "base.004", "severity": "warning", "source": "recommendations", "category": "suppressed",
         "message": "Recommendations based on Search Console evidence were suppressed: the data was not provided.",
         "audience": "reviewer"}]
    record = mutated(finalized(), ["notes"], notes)
    assert rs.validate_finalized_record(record) is record
    assert [n["source"] for n in record["notes"]] == ["engine", "engine", "recommendations", "recommendations"]


@pytest.mark.parametrize("source", ["Engine", "ENGINE", "engines", "recommendation", "Recommendations", "engine ",
                                    " engine", "engine\n", "gsa", "base", "snapshot", "", "all", None, 5, True,
                                    ["engine"], {"source": "engine"}])
def test_unknown_malformed_or_mistyped_note_sources_are_still_rejected(source):
    bad_finalized(["notes", 0, "source"], source, "source")


def test_a_note_without_a_source_is_still_rejected():
    with pytest.raises(RecordSchemaError, match="missing"):
        rs.validate_finalized_record(removed(finalized(), ["notes", 0, "source"]))


@pytest.mark.parametrize("source", ["engine", "recommendations"])
def test_the_other_note_rules_still_apply_to_the_new_sources(source):
    path = ["notes", 0]
    record = mutated(finalized(), path + ["source"], source)
    for field, bad in (("severity", "error"), ("category", "other"), ("audience", "client"), ("message", ""),
                       ("id", ""), ("message", None), ("severity", None)):
        with pytest.raises(RecordSchemaError, match=field):
            rs.validate_finalized_record(mutated(record, path + [field], bad))
    with pytest.raises(RecordSchemaError, match="unexpected"):
        rs.validate_finalized_record(mutated(record, path + ["extra"], 1))
    with pytest.raises(RecordSchemaError, match="missing"):
        rs.validate_finalized_record(removed(record, path + ["category"]))
    for severity in ("info", "warning"):
        for category in rs.NOTE_CATEGORIES:
            ok = mutated(mutated(record, path + ["severity"], severity), path + ["category"], category)
            assert rs.validate_finalized_record(ok) is ok


def test_attempt_records_are_not_affected_by_the_note_sources():
    record = attempt()
    record["notes"] = [{"id": "n", "severity": "info", "source": "engine", "category": "suppressed", "message": "m",
                        "audience": "reviewer"}]
    with pytest.raises(RecordSchemaError, match="unexpected"):
        rs.validate_attempt_record(record)

# --- general behavior ---------------------------------------------------------------------------------------------

def test_errors_name_the_offending_field():
    with pytest.raises(RecordSchemaError, match=r"finalization\.pdf_bytes"):
        rs.validate_finalized_record(mutated(finalized(), ["finalization", "pdf_bytes"], 0))
    with pytest.raises(RecordSchemaError, match=r"history\.excluded_unfinalized\[0\]\.reason_code"):
        bad = mutated(finalized(), ["history", "excluded_unfinalized"],
                      [{"run_id": RUN2, "revision": 1, "period_start": "2025-11-01", "period_end": "2025-11-30",
                        "reason_code": "nope"}])
        rs.validate_finalized_record(bad)
    with pytest.raises(RecordSchemaError, match="status_code"):
        rs.validate_attempt_record(mutated(attempt(), ["status_code"], "nope"))


def test_validators_do_not_mutate_invalid_input():
    for record, validate in ((mutated(finalized(), ["finalization", "pdf_bytes"], 0), rs.validate_finalized_record),
                             (mutated(attempt(), ["step"], "nope"), rs.validate_attempt_record)):
        before = copy.deepcopy(record)
        with pytest.raises(RecordSchemaError):
            validate(record)
        assert record == before
