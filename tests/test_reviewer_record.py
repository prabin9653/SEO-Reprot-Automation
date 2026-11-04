import copy
import dataclasses
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_history_engine as teng
import test_history_recommendations as threc
import test_history_rules as thr
import test_report_run as trr
from test_history_access import env  # noqa: F401  (temporary history and output folders)
from seo_report import record_schema as rs
from seo_report import report_run
from seo_report import reviewer_record as rr
from seo_report.insights import history_engine as he
from seo_report.insights import history_recommendations as hrec
from seo_report.insights import history_rules as hr
from seo_report.insights.rules import ReviewerNote, Tone
from seo_report.snapshots import eligibility as el
from seo_report.snapshots.baselines import HistoryConfig

RUN = "0198c1a2-7b3c-7d4e-8f5a-1234567890ab"
SHA_PDF, SHA_CLIENT = "a" * 64, "c" * 64
TS = "2026-02-03T09:30:00+00:00"


def receipt(**overrides):
    values = dict(run_id=RUN, revision=1, finalized_at=TS, pdf_sha256=SHA_PDF, pdf_bytes=2048,
                  pdf_checks=rr.PdfChecks(True, True, True, True), client_content_sha256=SHA_CLIENT,
                  rendered_event_detail=f"Report rendered, PDF {SHA_PDF}")
    values.update(overrides)
    return rr.FinalizationReceipt(**values)


def supported_notes_only(bundle):
    """The real engine also writes notes from sources the record schema does not list (see the blocker test)."""
    base = tuple(n for n in bundle.reviewer_report.base_notes if n.source in rs.NOTE_SOURCES)
    return dataclasses.replace(bundle, reviewer_report=dataclasses.replace(bundle.reviewer_report, base_notes=base))


def pipeline(env, *, history=True):
    if history:
        trr.with_history(env)
    bundle = supported_notes_only(trr.assemble(env, **({} if history else {"history_db_path": env.tmp / "none.sqlite"})))
    rules = hr.evaluate_history_rules(
        comparison=bundle.historical_comparison, metrics=bundle.base_insights.metrics,
        base_insights=bundle.base_insights, crosscheck=bundle.reviewer_report.ranking_crosscheck,
        target_country="United States")
    composed = he.compose_history_insights(rule_output=rules, base_insights=bundle.base_insights)
    annotations = hrec.annotate_recommendations(
        recommendations=bundle.base_insights.recommendations, comparison=bundle.historical_comparison,
        history_insights=composed)
    return bundle, composed, annotations


def build(env, **overrides):
    bundle, composed, annotations = pipeline(env, history=overrides.pop("history", True))
    options = dict(bundle=bundle, receipt=receipt(), history_insights=composed, recommendation_annotations=annotations)
    options.update(overrides)
    return rr.build_reviewer_record(**options)


def statement_composition(**overrides):
    candidate = threc.teng.cand(**overrides) if overrides else threc.teng.cand()
    return he.compose_history_insights(rule_output=hr.HistoryRuleResult(client_candidates=(candidate,)))


# --- successful construction -------------------------------------------------------------------------------------------

def test_a_record_is_built_from_a_real_bundle_and_passes_the_real_validator(env):
    record = build(env)
    assert rs.validate_finalized_record(record) is record
    assert record["record_type"] == "finalized_report" and record["schema_version"] == 1
    assert list(record) == sorted(rs.FINALIZED_FIELDS, key=lambda name: list(record).index(name))
    assert set(record) == set(rs.FINALIZED_FIELDS)  # exactly the schema's fields, none added


def test_identity_comes_from_the_bundle_and_the_receipt(env):
    bundle, composed, annotations = pipeline(env)
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                      recommendation_annotations=annotations)
    snapshot = bundle.reviewer_report.snapshot
    assert record["client_key"] == "example-co"
    assert record["report_period"] == {"start": snapshot.period_start, "end": snapshot.period_end}
    previous = bundle.client_content.periods.previous
    assert record["comparison_period"] == {"start": previous.start.isoformat(), "end": previous.end.isoformat()}
    assert record["snapshot"] == {"run_id": RUN, "revision": 1, "content_hash": snapshot.content_hash,
                                  "quality": snapshot.quality}
    assert record["finalization"]["pdf_sha256"] == SHA_PDF and record["finalization"]["pdf_bytes"] == 2048
    assert record["finalization"]["client_content_sha256"] == SHA_CLIENT
    assert record["finalization"]["rendered_event"] == {"recorded": True, "detail": f"Report rendered, PDF {SHA_PDF}"}
    assert record["finalization"]["pdf_checks"] == {"non_empty": True, "header": True, "eof_marker": True,
                                                    "preliminary": True}
    assert record["finalization"]["superseded_previous_pdf"] is None
    assert record["finalization"]["duplicate_draft_preserved_as"] is None


def test_preserved_copy_metadata_is_recorded(env):
    bundle, composed, annotations = pipeline(env)
    record = rr.build_reviewer_record(
        bundle=bundle, history_insights=composed, recommendation_annotations=annotations,
        receipt=receipt(superseded_previous_pdf=rr.PreservedPdf("b" * 16, 1),
                        duplicate_draft_preserved_as=rr.PreservedPdf(SHA_PDF[:16], 2)))
    assert record["finalization"]["superseded_previous_pdf"] == {"sha256_16": "b" * 16, "variant_index": 1}
    assert record["finalization"]["duplicate_draft_preserved_as"] == {"sha256_16": SHA_PDF[:16], "variant_index": 2}


# --- history states -------------------------------------------------------------------------------------------------------

def test_available_history_is_recorded_from_the_comparison(env):
    bundle, composed, annotations = pipeline(env)
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                      recommendation_annotations=annotations)
    history = record["history"]
    assert (history["status"], history["failure_kind"], history["rules_version"]) == ("available", None, "1")
    assert history["config"] == {"lookback_periods": 3, "max_gap_days": 35, "list_limit": 10}
    review = bundle.reviewer_report.history
    assert [b["dimension"] for b in history["baselines"]] == [d for d, _ in review.baselines]
    assert all(set(b) == {"dimension", "run_id", "revision", "period_start", "period_end", "gap_days", "rank",
                          "same_length"} for b in history["baselines"])
    assert [s["dimension"] for s in history["dimension_status"]] == list(el.DIMENSIONS)
    assert len(history["dimension_status"]) == 12
    eligible = {s["dimension"] for s in history["dimension_status"] if s["eligible"]}
    assert eligible == {b["dimension"] for b in history["baselines"]}
    for status in history["dimension_status"]:
        assert (status["baseline_run_id"] is not None) == status["eligible"]
    assert history["diagnostics"]["current"] == review.diagnostics["current"]
    assert set(history["diagnostics"]["baselines"]) <= {b["run_id"] for b in history["baselines"]}


def test_skipped_candidates_and_reasons_are_recorded(env):
    trr.with_history(env)
    older = trr.tha.write_run(env, trr.ti.P_B)
    trr.tha.finalize(env, older)
    env.store.add_event(older, "voided", "entered by mistake")
    bundle, composed, annotations = pipeline(env)
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                      recommendation_annotations=annotations)
    skipped = {s["run_id"]: s["reason"] for s in record["history"]["skipped_runs"]}
    assert skipped.get(older) == "voided"
    for entry in record["history"]["skipped_runs"]:
        assert set(entry) == {"run_id", "revision", "period_start", "period_end", "reason"}


def test_excluded_unfinalized_runs_are_recorded(env):
    trr.with_history(env)
    never_finalized = trr.tha.write_run(env, trr.ti.P_B)
    bundle, composed, annotations = pipeline(env)
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                      recommendation_annotations=annotations)
    (entry,) = record["history"]["excluded_unfinalized"]
    assert entry["run_id"] == never_finalized and entry["reason_code"] == "no_reviewer_record"
    assert set(entry) == {"run_id", "revision", "period_start", "period_end", "reason_code"}


@pytest.mark.parametrize("state", ["missing", "unavailable", "schema_failure"])
def test_unavailable_history_still_gives_a_valid_complete_record(env, tmp_path, state):
    path = {"missing": tmp_path / "none" / "x.sqlite", "unavailable": tmp_path, "schema_failure": None}[state]
    if state == "schema_failure":
        trr.with_history(env)
        import sqlite3
        raw = sqlite3.connect(env.db)
        raw.execute("PRAGMA user_version=2")
        raw.close()
        path = env.db
    bundle = supported_notes_only(trr.assemble(env, history_db_path=path))
    assert bundle.historical_comparison is None
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt())
    history = record["history"]
    assert (history["status"], history["failure_kind"]) == ("unavailable", state)
    assert history["baselines"] == [] and history["skipped_runs"] == [] and history["excluded_unfinalized"] == []
    assert len(history["dimension_status"]) == 12
    assert all(not s["eligible"] and s["baseline_run_id"] is None and s["caveats"] == [] and len(s["reasons"]) == 1
               for s in history["dimension_status"])
    assert history["dimension_status"][0]["reasons"] == [bundle.reviewer_report.history_status.detail]
    assert history["diagnostics"]["baselines"] == {} and history["diagnostics"]["current"] == bundle.base_insights.diagnostics
    assert history["ranking_crosscheck"] == {"fallback_used": 0, "sheet_previous_mismatch": None, "new_keywords": None,
                                             "no_longer_tracked": None, "not_comparable": None}
    assert rs.validate_finalized_record(record) is record


def test_the_history_config_used_is_recorded_when_there_is_no_comparison(env, tmp_path):
    bundle = supported_notes_only(trr.assemble(env, history_db_path=tmp_path / "none.sqlite"))
    default = rr.build_reviewer_record(bundle=bundle, receipt=receipt())
    assert default["history"]["config"] == {"lookback_periods": 3, "max_gap_days": 35, "list_limit": 10}
    custom = rr.build_reviewer_record(bundle=bundle, receipt=receipt(),
                                      history_config=HistoryConfig(lookback_periods=2, max_gap_days=40, list_limit=7))
    assert custom["history"]["config"] == {"lookback_periods": 2, "max_gap_days": 40, "list_limit": 7}


def test_a_comparison_that_failed_is_recorded_as_comparison_unavailable(env, monkeypatch):
    trr.with_history(env)
    monkeypatch.setattr(report_run, "compare_with_history",
                        lambda *a, **k: (_ for _ in ()).throw(report_run.st.SnapshotStoreError("x")))
    bundle = supported_notes_only(trr.assemble(env))
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt())
    assert record["history"]["status"] == "unavailable"
    assert record["history"]["failure_kind"] == "comparison_unavailable"


def test_no_eligible_baseline_gives_empty_baselines_and_ineligible_dimensions(env):
    trr.tha.write_run(env, trr.ti.P_A)  # stored but never finalized: not a baseline
    bundle = supported_notes_only(trr.assemble(env))
    assert bundle.historical_comparison is not None
    assert not any(s.eligible for s in bundle.historical_comparison.status.values())
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt())
    history = record["history"]
    assert history["status"] == "available" and history["baselines"] == []
    assert not any(s["eligible"] for s in history["dimension_status"])
    assert all(s["baseline_run_id"] is None for s in history["dimension_status"])
    assert [e["reason_code"] for e in history["excluded_unfinalized"]] == ["no_reviewer_record"]


# --- client-facing historical statements ------------------------------------------------------------------------------------

def test_client_statements_and_their_evidence_are_recorded_exactly(env):
    bundle = supported_notes_only(trr.assemble(env))
    text = "  Search impressions were down 31.8% from 2.28K \u2014 in the report for September 1\u201328, 2026."
    composed = statement_composition(text=text, evidence={"current_value": 6, "baseline_value": 4, "direction": 1,
                                                          "baseline_days": 28, "share_current": 0.1 + 0.2})
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed)
    (entry,) = record["client_facing_history"]
    assert entry["text"] == text  # not trimmed, not normalized
    assert entry["evidence"]["share_current"] == 0.30000000000000004
    assert entry == {"section": "search_console", "key": "history.gsc.clicks", "dimension": "gsc.totals",
                     "text": text, "baseline_period": {"start": "2026-09-01", "end": "2026-09-28"},
                     "evidence": entry["evidence"], "rules_version": "1"}
    assert set(entry) == set(rs.CLIENT_HISTORY_FIELDS)


def test_the_client_facing_history_is_empty_without_statements(env):
    assert build(env)["client_facing_history"] == []


def test_client_history_and_the_history_section_are_distinct_parts(env):
    bundle = supported_notes_only(trr.assemble(env))
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=statement_composition())
    assert "client_facing_history" in record and "history" in record
    assert "text" not in record["history"] and "baselines" not in record["client_facing_history"][0]
    assert not {"run_id", "blocked_reason", "base_keys"} & set(record["client_facing_history"][0])


# --- reviewer-only findings, notes, failures --------------------------------------------------------------------------------

def test_reviewer_findings_become_notes_with_the_finding_key_as_the_id(env):
    bundle = supported_notes_only(trr.assemble(env))
    findings = (teng.finding("history.review.gsc_clicks", 110, section="search_console", kind="gsc_historical_change",
                             dimension="gsc.totals", blocked_reason="live_previous_not_empty"),
                teng.finding("history.review.ranking_new_keywords", 220, section="keywords", kind="ranking_new_keywords",
                             baseline_run_id=None),
                teng.finding("history.review.ga4_daily_discrepancy.current", 400, section="ga4",
                             kind="ga4_daily_discrepancy"))
    composed = he.compose_history_insights(rule_output=hr.HistoryRuleResult(reviewer_findings=findings))
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed)
    by_id = {n["id"]: n for n in record["notes"]}
    assert by_id["history.review.gsc_clicks"]["source"] == "gsc" and by_id["history.review.gsc_clicks"]["category"] == "suppressed"
    assert by_id["history.review.ranking_new_keywords"]["source"] == "rankings"
    assert by_id["history.review.ranking_new_keywords"]["category"] == "limitation"
    assert by_id["history.review.ga4_daily_discrepancy.current"]["category"] == "diagnostic"
    for note in by_id.values():
        assert note["audience"] == "reviewer" and set(note) == set(rs.NOTE_FIELDS)
    assert by_id["history.review.gsc_clicks"]["message"] == findings[0].message


def test_every_finding_kind_has_a_note_category_and_every_section_a_note_source():
    assert set(rr.FINDING_NOTE_CATEGORY) == set(hr.FINDING_KINDS)
    assert set(rr.FINDING_NOTE_CATEGORY.values()) <= set(rs.NOTE_CATEGORIES)
    assert set(rr.FINDING_NOTE_SOURCE) == set(hr.REVIEWER_SECTIONS)
    assert set(rr.FINDING_NOTE_SOURCE.values()) <= set(rs.NOTE_SOURCES)
    assert rr.FINDING_NOTE_SEVERITY in rs.NOTE_SEVERITIES


def test_notes_from_every_origin_are_kept_in_order_with_unique_ids(env):
    bundle, composed, annotations = pipeline(env)
    extra = he.compose_history_insights(rule_output=hr.HistoryRuleResult(
        notes=(ReviewerNote("info", "history", "suppressed", "A rule note."),
               ReviewerNote("info", "history", "suppressed", "A rule note.")),
        reviewer_findings=(teng.finding("history.review.a", 5, kind="baselines_differ"),)))
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=extra)
    ids = [n["id"] for n in record["notes"]]
    assert len(ids) == len(set(ids))
    base_count = len(bundle.reviewer_report.base_notes)
    stage_count = len(bundle.reviewer_report.notes)
    assert ids[:base_count] == [f"base.{n:03d}" for n in range(1, base_count + 1)]
    assert ids[base_count:base_count + stage_count] == [f"stage.{n:03d}" for n in range(1, stage_count + 1)]
    assert ids[-3:] == ["history.001", "history.002", "history.review.a"]
    assert [n["message"] for n in record["notes"][-3:-1]] == ["A rule note.", "A rule note."]  # repeats are kept


def test_stage_notes_and_comparison_notes_are_recorded(env, tmp_path):
    bundle = supported_notes_only(trr.assemble(env, history_db_path=tmp_path / "none.sqlite"))
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt())
    messages = [n["message"] for n in record["notes"]]
    assert any("No history comparison was made" in m for m in messages)


def test_validation_failures_keep_their_exact_keys_and_reasons(env):
    bundle = supported_notes_only(trr.assemble(env))
    failures = tuple(hr.HistoryValidationFailure(f"history.k{n}", reason)
                     for n, reason in enumerate(sorted(hr.VALIDATION_REASONS)))
    composed = he.compose_history_insights(rule_output=hr.HistoryRuleResult(validation_failures=failures + failures[:1]))
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed)
    assert [(f["key"], f["reason"]) for f in record["validation_failures"]] == [
        (f.key, f.reason) for f in composed.validation_failures]
    assert len(record["validation_failures"]) == 8 and {f["reason"] for f in record["validation_failures"]} == set(hr.VALIDATION_REASONS)
    assert all(set(f) == {"key", "reason"} for f in record["validation_failures"])


# --- rankings ---------------------------------------------------------------------------------------------------------------

def test_the_ranking_cross_check_counts_are_recorded(env):
    bundle, composed, annotations = pipeline(env)
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                      recommendation_annotations=annotations)
    check = bundle.reviewer_report.ranking_crosscheck
    assert record["history"]["ranking_crosscheck"] == {
        "fallback_used": 0, "sheet_previous_mismatch": check.sheet_previous_mismatch,
        "new_keywords": check.new_keywords, "no_longer_tracked": check.no_longer_tracked,
        "not_comparable": check.not_comparable}
    assert check.pending_fallbacks  # the per-keyword fallbacks exist in the bundle ...
    assert "pending_fallbacks" not in record["history"]["ranking_crosscheck"]  # ... but the closed schema has no place for them
    assert "Roof Repair" not in json.dumps(record)  # no keyword names leak into the record


def test_pending_fallback_information_survives_as_a_reviewer_note(env):
    bundle, composed, annotations = pipeline(env)
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed)
    fallback = [n for n in record["notes"] if n["id"] == "history.review.ranking_pending_fallback"]
    assert len(fallback) == 1 and fallback[0]["source"] == "rankings" and fallback[0]["category"] == "suppressed"
    assert "blank Previous Ranking" in fallback[0]["message"]


def test_ranking_markers_and_footnote_are_empty_without_client_markers(env):
    record = build(env)
    assert record["ranking_table_markers"] == {"footnote_text": "", "cells": []}


def test_a_footnote_is_recorded_exactly_and_markers_of_unknown_type_are_refused(env):
    bundle = supported_notes_only(trr.assemble(env))
    with_footnote = dataclasses.replace(bundle, client_content=dataclasses.replace(
        bundle.client_content, ranking_footnote="\u2020 Compared with the report for X. "))
    record = rr.build_reviewer_record(bundle=with_footnote, receipt=receipt())
    assert record["ranking_table_markers"]["footnote_text"] == "\u2020 Compared with the report for X. "
    with_markers = dataclasses.replace(bundle, client_content=dataclasses.replace(
        bundle.client_content, ranking_markers=(object(),)))
    with pytest.raises(rr.ReviewerRecordError, match="ranking markers"):
        rr.build_reviewer_record(bundle=with_markers, receipt=receipt())


# --- recommendation annotations ---------------------------------------------------------------------------------------------

def test_annotations_without_support_are_recorded_as_empty_lists(env):
    bundle, composed, annotations = pipeline(env)
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                      recommendation_annotations=annotations)
    assert record["recommendation_annotations"] == annotations.as_record()
    assert [e["recommendation_key"] for e in record["recommendation_annotations"]] == [
        r.key for r in bundle.client_content.recommendations]
    assert all(set(e) == {"recommendation_key", "history_support"} for e in record["recommendation_annotations"])


def test_annotations_with_non_empty_support_are_recorded_and_validated(env):
    bundle, composed, _ = pipeline(env)
    key = bundle.client_content.recommendations[0].key
    item = hrec.HistorySupport("supports", "gsc_ctr", None, "gsc.totals", RUN, "2026-09-01", "2026-09-28",
                               {"current_value": 0.002, "baseline_value": 0.004, "direction": -1, "baseline_days": 28})
    annotations = hrec.RecommendationHistoryAnnotations((hrec.RecommendationAnnotation(key, (item,)),))
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                      recommendation_annotations=annotations)
    assert record["recommendation_annotations"] == [{"recommendation_key": key, "history_support": [item.as_record()]}]


def test_an_invalid_support_item_is_refused_not_repaired(env):
    bundle, composed, _ = pipeline(env)
    key = bundle.client_content.recommendations[0].key
    bad = hrec.HistorySupport("supports", "gsc_ctr", None, "ga4.site", RUN, "2026-09-01", "2026-09-28",
                              {"current_value": 1, "baseline_value": 2, "direction": 1, "baseline_days": 28})
    annotations = hrec.RecommendationHistoryAnnotations((hrec.RecommendationAnnotation(key, (bad,)),))
    with pytest.raises(rr.ReviewerRecordError, match="dimension"):
        rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                 recommendation_annotations=annotations)


def test_an_annotation_for_a_recommendation_that_is_not_in_the_report_is_refused(env):
    bundle, composed, _ = pipeline(env)
    annotations = hrec.RecommendationHistoryAnnotations((hrec.RecommendationAnnotation("not_in_the_report"),))
    with pytest.raises(rr.ReviewerRecordError, match="not in the report"):
        rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                 recommendation_annotations=annotations)


def test_missing_annotations_give_an_empty_list(env):
    bundle, composed, _ = pipeline(env)
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed)
    assert record["recommendation_annotations"] == []


# --- missing optional information -----------------------------------------------------------------------------------------------

def test_history_insights_and_annotations_are_optional(env):
    bundle = supported_notes_only(trr.assemble(env))
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt())
    assert record["client_facing_history"] == [] and record["validation_failures"] == []
    assert record["recommendation_annotations"] == [] and record["history"]["rules_version"] == "1"
    assert rs.validate_finalized_record(record) is record


# --- the real engine's notes (the blocker) ------------------------------------------------------------------------------------------

def test_notes_from_the_engine_and_the_recommendation_selector_are_preserved_exactly(env):
    bundle = supported_notes_only(trr.assemble(env))
    engine_note = ReviewerNote("info", "engine", "suppressed",
                               "Keyword Rankings: left out by the paragraph limits: keywords.moved_up.")
    recommendation_note = ReviewerNote("info", "recommendations", "suppressed",
                                       "The local SEO recommendation was not made: none of the keywords labelled "
                                       "\u201cAbove 10\u201d has a printable target location.")
    other = ReviewerNote("warning", "recommendations", "suppressed",
                         "Recommendations based on Search Console evidence were suppressed: the data was not provided.")
    kept = bundle.reviewer_report.base_notes
    note_bundle = dataclasses.replace(bundle, reviewer_report=dataclasses.replace(
        bundle.reviewer_report, base_notes=(*kept, engine_note, recommendation_note, other)))
    before = copy.deepcopy(note_bundle)
    record = rr.build_reviewer_record(bundle=note_bundle, receipt=receipt())
    assert note_bundle == before and rs.validate_finalized_record(record) is record
    stored = record["notes"][len(kept):len(kept) + 3]
    assert [(n["severity"], n["source"], n["category"], n["message"]) for n in stored] == [
        (note.severity, note.source, note.category, note.message) for note in (engine_note, recommendation_note, other)]
    assert [n["source"] for n in stored] == ["engine", "recommendations", "recommendations"]
    assert [n["id"] for n in stored] == [f"base.{len(kept) + n:03d}" for n in (1, 2, 3)]
    assert all(n["audience"] == "reviewer" and set(n) == set(rs.NOTE_FIELDS) for n in stored)
    messages = [n["message"] for n in record["notes"]]
    assert all(note.message in messages for note in (engine_note, recommendation_note, other))  # none dropped
    assert len(record["notes"]) == len(kept) + 3 + len(bundle.reviewer_report.notes)  # none added or rewritten

# --- safety ----------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("message,reason", [
    ("Contact jane.doe@example.test about it.", "email address"),
    ("Call (555) 010-0199 today.", "phone number"), ("Call 555-010-0199 today.", "phone number"),
    ("Saved to C:\\Users\\someone\\report.pdf", "file path"), ("Saved to /home/someone/report.pdf", "file path"),
    ("Saved to \\\\server\\share\\report.pdf", "file path"),
    ("The token ya29.a0AfH6SMBx was used.", "credential marker"), ("client_secret was set", "credential marker"),
    ("Traceback (most recent call last):", "exception text"),
    ("Opaque abcdefghij1234567890ABCDEFGHIJ12 value", "opaque token"),
])
def test_unsafe_text_is_refused_without_echoing_it(env, message, reason):
    bundle = supported_notes_only(trr.assemble(env))
    note = ReviewerNote("info", "history", "limitation", message)
    unsafe = dataclasses.replace(bundle, reviewer_report=dataclasses.replace(bundle.reviewer_report, notes=(note,)))
    with pytest.raises(rr.ReviewerRecordError) as raised:
        rr.build_reviewer_record(bundle=unsafe, receipt=receipt())
    assert reason in str(raised.value) and message not in str(raised.value)


def test_run_ids_hashes_dates_and_dimension_names_in_notes_are_not_mistaken_for_unsafe_values(env):
    bundle = supported_notes_only(trr.assemble(env))
    message = (f"Baseline run {RUN} (2026-09-01 to 2026-09-28) hash {SHA_PDF} in gsc.totals ends 3 day(s) before "
               "the current period starts.")
    note = ReviewerNote("info", "history", "data_quality", message)
    ok = dataclasses.replace(bundle, reviewer_report=dataclasses.replace(bundle.reviewer_report, notes=(note,)))
    record = rr.build_reviewer_record(bundle=ok, receipt=receipt())
    assert any(n["message"] == message for n in record["notes"])


def test_unsafe_client_text_and_subjects_are_refused(env):
    bundle = supported_notes_only(trr.assemble(env))
    composed = statement_composition(text="Write to someone@example.test for the report for September 1\u201328, 2026.")
    with pytest.raises(rr.ReviewerRecordError, match="client_facing_history"):
        rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed)


def test_invalid_receipt_values_are_refused_by_the_real_validator(env):
    bundle = supported_notes_only(trr.assemble(env))
    for bad in (receipt(run_id="run-1"), receipt(revision=0), receipt(pdf_sha256="A" * 64), receipt(pdf_bytes=0),
                receipt(finalized_at="2026-02-03"), receipt(client_content_sha256="x"),
                receipt(rendered_event_detail="no hash here"), receipt(rendered_event_recorded=False),
                receipt(pdf_checks=rr.PdfChecks(True, False, True)),
                receipt(duplicate_draft_preserved_as=rr.PreservedPdf("b" * 16, 1)),
                receipt(superseded_previous_pdf=rr.PreservedPdf(SHA_PDF[:16], 1)),
                receipt(superseded_previous_pdf=rr.PreservedPdf("b" * 16, 0))):
        with pytest.raises(rr.ReviewerRecordError, match="not valid"):
            rr.build_reviewer_record(bundle=bundle, receipt=bad)


def test_wrong_input_types_are_programming_errors(env):
    bundle = supported_notes_only(trr.assemble(env))
    with pytest.raises(TypeError):
        rr.build_reviewer_record(bundle=object(), receipt=receipt())
    with pytest.raises(TypeError):
        rr.build_reviewer_record(bundle=bundle, receipt={"run_id": RUN})
    with pytest.raises(TypeError):
        rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=hr.HistoryRuleResult())
    with pytest.raises(TypeError):
        rr.build_reviewer_record(bundle=bundle, receipt=receipt(), recommendation_annotations=[])


def test_the_record_holds_no_secrets_contact_details_or_paths(env):
    record = build(env)
    dump = json.dumps(record)
    for forbidden in ("sender@example.test", "manager@example.test", "Manager Person", "555-010", "topsecret", "ya29.",
                      "1//fake", "SpreadsheetIdSecret", "Traceback", str(env.tmp), "seo_reports.sqlite",
                      "an internal sheet note"):
        assert forbidden not in dump, forbidden
    assert not re.search(r"[A-Za-z]:\\", dump)


# --- determinism, isolation and separation ------------------------------------------------------------------------------------------

def test_the_output_is_deterministic(env):
    bundle, composed, annotations = pipeline(env)
    first = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                     recommendation_annotations=annotations)
    second = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                      recommendation_annotations=annotations)
    assert first == second and rr.to_canonical_json(first) == rr.to_canonical_json(second)
    text = rr.to_canonical_json(first)
    assert json.loads(text) == first and not re.search(r"\s\"|\":\s", text) and text == json.dumps(
        first, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def test_canonical_json_keeps_text_as_written_and_refuses_nan():
    assert rr.to_canonical_json({"b": "\u2014 caf\u00e9", "a": [1, 2.5]}) == '{"a":[1,2.5],"b":"\u2014 caf\u00e9"}'
    with pytest.raises(rr.ReviewerRecordError):
        rr.to_canonical_json({"a": float("nan")})
    with pytest.raises(rr.ReviewerRecordError):
        rr.to_canonical_json({"a": {1, 2}})


def test_the_inputs_are_not_mutated_and_the_record_shares_nothing_with_them(env):
    bundle, composed, annotations = pipeline(env)
    receipt_value = receipt()
    before = (copy.deepcopy(bundle), copy.deepcopy(composed), copy.deepcopy(annotations), copy.deepcopy(receipt_value))
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt_value, history_insights=composed,
                                      recommendation_annotations=annotations)
    assert (bundle, composed, annotations, receipt_value) == before
    record["history"]["diagnostics"]["injected"] = 1
    record["history"]["config"]["injected"] = 1
    record["notes"].clear()
    record["recommendation_annotations"].clear()
    assert (bundle, composed, annotations, receipt_value) == before
    assert "injected" not in bundle.reviewer_report.history.diagnostics
    assert "injected" not in bundle.historical_comparison.limits


def test_nothing_is_written_or_created(env):
    bundle, composed, annotations = pipeline(env)
    before = (trr.tha.tree(env.out, env.db.parent), trr.tha.file_digest(env.db), env.db.stat().st_mtime_ns)
    for _ in range(2):
        rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                 recommendation_annotations=annotations)
    assert (trr.tha.tree(env.out, env.db.parent), trr.tha.file_digest(env.db), env.db.stat().st_mtime_ns) == before

def test_reviewer_only_information_does_not_appear_in_the_client_content(env):
    bundle, composed, annotations = pipeline(env)
    client_before = copy.deepcopy(bundle.client_content)
    record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                      recommendation_annotations=annotations)
    assert bundle.client_content == client_before
    client_dump = "\n".join(trr.strings(bundle.client_content))
    reviewer_only = [RUN, SHA_PDF, SHA_CLIENT, bundle.reviewer_report.snapshot.content_hash,
                     *[b["run_id"] for b in record["history"]["baselines"]],
                     *[n["message"] for n in record["notes"]], "history.review"]
    for value in reviewer_only:
        assert value not in client_dump, value
    names = trr.field_names(bundle.client_content)
    assert not names & {"notes", "history", "validation_failures", "recommendation_annotations", "snapshot",
                        "finalization"}


def test_no_additional_schema_fields_are_introduced(env):
    record = build(env)
    assert set(record) == set(rs.FINALIZED_FIELDS)
    assert set(record["snapshot"]) == set(rs.SNAPSHOT_FIELDS)
    assert set(record["finalization"]) == set(rs.FINALIZATION_FIELDS)
    assert set(record["finalization"]["pdf_checks"]) == set(rs.PDF_CHECK_FIELDS)
    assert set(record["finalization"]["rendered_event"]) == set(rs.RENDERED_EVENT_FIELDS)
    assert set(record["history"]) == set(rs.HISTORY_FIELDS)
    assert set(record["history"]["ranking_crosscheck"]) == set(rs.CROSSCHECK_FIELDS)
    assert set(record["history"]["diagnostics"]) == set(rs.DIAGNOSTICS_FIELDS)
    assert set(record["ranking_table_markers"]) == set(rs.MARKERS_FIELDS)
    for note in record["notes"]:
        assert set(note) == set(rs.NOTE_FIELDS)


def test_the_schema_module_is_not_duplicated_or_bypassed(monkeypatch, env):
    bundle, composed, annotations = pipeline(env)
    inputs = dict(bundle=bundle, receipt=receipt(), history_insights=composed, recommendation_annotations=annotations)
    calls = []
    original = rs.validate_finalized_record
    monkeypatch.setattr(rr.rs, "validate_finalized_record", lambda record: calls.append(1) or original(record))
    rr.build_reviewer_record(**inputs)
    assert calls == [1]
    monkeypatch.setattr(rr.rs, "validate_finalized_record",
                        lambda record: (_ for _ in ()).throw(rs.RecordSchemaError("forced failure")))
    with pytest.raises(rr.ReviewerRecordError, match="forced failure"):
        rr.build_reviewer_record(**inputs)

def test_the_module_is_pure():
    source = (ROOT / "src" / "seo_report" / "reviewer_record.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for pattern in (r"\.now\(", r"today\(", r"\btime\.", r"\bopen\(", r"write_text", r"write_bytes", r"\.mkdir\(",
                    r"sqlite", r"requests", r"googleapiclient", r"\bos\.", r"environ", r"datetime"):
        assert not re.search(pattern, code), pattern
