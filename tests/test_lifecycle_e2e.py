"""R7.5: end-to-end lifecycle verification. Tests only: the driver below lives in this file and is not a pipeline.

Chain under test (every step is an existing public API):
  assemble (`test_report_finalize.make_context` -> `ReportBundle`, composed history, annotations)
  -> `render_identity.build_render_input` -> `render_artifacts.produce_report_draft` (sidecar + draft)
  -> `report_finalize.finalize_report(render_input=<the DraftOutcome's own input>)`
  -> `render_state.finalized_render_hashes` -> finalized-only reader -> `finalization_audit.audit_period`.

Offline and synthetic, in temporary folders. The renderer is a stub that writes a tiny valid PDF, except in the single
real-Chromium test. Nothing here writes to the project, a production folder, a database outside the temporary tree, or
any network or Google service.
"""
import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import finalization_audit as fa
from seo_report import finalize_files as ff
from seo_report import history_access as ha
from seo_report import pdf_renderer as pr
from seo_report import pdf_validation as pv
from seo_report import record_schema as rs
from seo_report import record_writer as rw
from seo_report import render_artifacts as ra
from seo_report import render_input_hash as rh
from seo_report import render_state as rstate
from seo_report import report_finalize as rf
from seo_report import reviewer_record as rr
from seo_report.insights import history_engine as he
from seo_report.insights import history_recommendations as hrec
from seo_report.insights import history_rules as hr
from seo_report.snapshots import store as st
import test_report_run as trr
from test_finalization_audit import audit, tree
from test_finalize_files import FaultOps
from test_history_access import env  # noqa: F401
from test_render_artifacts import StubRenderer
from test_render_identity import FONTS, LOGO, PERSON  # noqa: F401
from test_report_finalize import (ctx, files, fin, finalized_ids, pdf, put_draft, read_record,  # noqa: F401
                                  records, rendered, runs, sha, sup)
from test_report_finalize_v2 import (FaultOpsFor, RENDERER, edited, fixed_renderer, make_input, produce, sidecar_path,
                                     state, stop, with_statements)
from test_reviewer_record import receipt

DRAFT = "report.unarchived.pdf"
FINAL = "report.pdf"


# --- the test-local driver ---------------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Life:
    value: object  # the candidate render input
    drafted: object  # the DraftOutcome of produce_report_draft
    result: object  # the FinalizationResult
    before: dict  # state() after any tampering and before finalization


def run_lifecycle(c, *, day=30, renderer=None, replace_existing=False, tamper=None, finalize_input=None,
                  history_insights="composed", finalize_history="composed", produce_options=None):
    """Steps 3 to 6 on an assembled context: build the render input, produce the sidecar and draft, finalize the input
    the DraftOutcome carries (never a rebuilt one), with finalized render hashes supplied through `render_state`."""
    value = make_input(c, day=day, history_insights=history_insights)
    drafted = produce(c, value, renderer=renderer, **(produce_options or {}))
    assert drafted.ok, (drafted.outcome, drafted.code)
    if tamper:
        tamper(c, value)
    before = state(c)
    subject = drafted.render_input if finalize_input is None else finalize_input
    insights = c.composed if finalize_history == "composed" else finalize_history
    result = fin(c, render_input=subject, replace_existing=replace_existing, history_insights=insights)
    return Life(value, drafted, result, before)


def reader_view(c):
    """What the finalized-only reader says about THIS period's runs (the client's earlier baseline periods are other
    periods with their own finalized runs; the audit, like this view, is per period)."""
    key = c.bundle.snapshot.client_key
    mine = {run[0] for run in runs(c)}
    access = ha.open_history(c.db, c.env.out)
    try:
        listed = sorted(i.run_id for i in access.reader.list_runs(key) if i.run_id in mine)
        excluded = {e.run_id: e.reason_code for e in access.reader.excluded_unfinalized(key) if e.run_id in mine}
    finally:
        access.close()
    return listed, excluded


def stored_run(c, run_id):
    reader = st.SnapshotReader(c.db)
    try:
        return reader.get(run_id), [e.detail for e in reader.events(run_id) if e.kind == "rendered"]
    finally:
        reader.close()


def assert_record_consistent(c, record_name, value=None):
    """The snapshot, rendered event, PDF, sidecar and reviewer record all name the same run and the same bytes."""
    record = read_record(c, record_name)
    fin_ = record["finalization"]
    run_id = record["snapshot"]["run_id"]
    stored, details = stored_run(c, run_id)
    assert stored.content_hash == c.bundle.snapshot.content_hash == record["snapshot"]["content_hash"]
    assert (stored.revision, stored.client_key) == (record["snapshot"]["revision"], record["client_key"])
    assert any(fin_["pdf_sha256"] in d for d in details)  # the event names the exact PDF hash
    on_disk = [n for n, data in files(c.reports).items() if sha(data) == fin_["pdf_sha256"] and len(data) == fin_["pdf_bytes"]
               and (n == FINAL or n.startswith("report.superseded."))]
    assert on_disk, "a recognized PDF with the record's hash and size must exist"
    if record["schema_version"] == 1:
        assert len(rw.record_identity(record)) == 6 and "render_input_sha256" not in fin_
        return record
    digest = fin_["render_input_sha256"]
    assert fin_["render_input_format"] == {"name": rh.FORMAT_NAME, "version": rh.FORMAT_VERSION}
    sidecar = ra.read_sidecar(c.reports, ra.sidecar_name(digest))
    assert sidecar.render_input_sha256 == digest and rh.render_input_sha256(sidecar.render_input) == digest
    assert (sidecar.pdf_sha256, sidecar.pdf_bytes) == (fin_["pdf_sha256"], fin_["pdf_bytes"])
    assert rw.record_identity(record)[6:] == (digest, rh.FORMAT_NAME, rh.FORMAT_VERSION)
    if value is not None:
        assert digest == rh.render_input_sha256(value) and sidecar.generation_date == value.generation_date
    return record


def assert_reader_and_audit_agree(c, *, valid, allowed_extra=(), legacy=None):
    """The finalized-only reader and the audit say the same thing about which runs are finalized."""
    listed, excluded = reader_view(c)
    report = audit(c)
    audit_valid = sorted({f.run_id for f in report.by_category(fa.FINALIZED_VALID)})
    assert listed == audit_valid and report.finalized_valid == valid
    # information only: a rejected call leaves a valid failure-only attempt record, a draft may await finalization
    allowed = {fa.FINALIZED_VALID, fa.SUPERSEDED_PDF, fa.DUPLICATE_PDF, fa.UNARCHIVED_PDF, fa.ATTEMPT_VALID,
               *allowed_extra}
    assert set(f.category for f in report.findings) <= allowed, [(f.category, f.reason) for f in report.findings]
    if legacy is not None:
        assert report.legacy_records == legacy
    return report, listed, excluded


# --- successful lifecycles ---------------------------------------------------------------------------------------------------------

def test_a_successful_version_1_lifecycle(ctx):
    data = put_draft(ctx, pdf("legacy"))
    result = fin(ctx)
    record = assert_record_consistent(ctx, result.record_name)
    assert record["schema_version"] == 1 and result.finalized
    assert set(files(ctx.reports)) == {FINAL} and files(ctx.reports)[FINAL] == data  # no sidecar in the v1 flow
    report, listed, excluded = assert_reader_and_audit_agree(ctx, valid=1, legacy=1)
    assert (report.outcome, listed, excluded) == (fa.OUTCOME_CONSISTENT, [result.run_id], {})
    scan = rstate.scan_finalized_render_hashes(ctx.reviewer)
    assert (scan.hashes, scan.v1_records, scan.v2_records) == (frozenset(), 1, 0)


def test_a_successful_version_2_lifecycle(ctx):
    assert rstate.finalized_render_hashes(ctx.reviewer) == frozenset()
    life = run_lifecycle(ctx)
    assert life.drafted.outcome == "created" and life.drafted.render_input is life.value  # the exact object
    assert life.result.finalized and life.result.outcome == rf.OUTCOME_FINALIZED
    record = assert_record_consistent(ctx, life.result.record_name, life.value)
    digest = rh.render_input_sha256(life.value)
    assert record["schema_version"] == 2 and record["finalization"]["render_input_sha256"] == digest
    assert set(files(ctx.reports)) == {FINAL, ra.sidecar_name(digest)}
    report, listed, excluded = assert_reader_and_audit_agree(ctx, valid=1, legacy=0)
    assert (report.outcome, listed, excluded) == (fa.OUTCOME_CONSISTENT, [life.result.run_id], {})
    assert report.by_category(fa.FINALIZED_VALID)[0].record_version == 2
    assert rstate.finalized_render_hashes(ctx.reviewer) == {digest}  # the sidecar is closed from now on


def test_after_finalization_the_input_is_not_produced_again_as_open(ctx):
    life = run_lifecycle(ctx)
    closed = rstate.finalized_render_hashes(ctx.reviewer)
    assert rstate.open_sidecars(ctx.reports, finalized=closed) == ()
    before = state(ctx)
    again = produce(ctx, life.value)  # the same input: its sidecar is closed, so nothing is created or replaced
    assert (again.outcome, again.code) == ("conflict", "render_input_already_finalized")
    assert state(ctx) == before


# --- sidecar and draft problems: nothing changes ---------------------------------------------------------------------------------

def tamper_missing(c, value):
    sidecar_path(c, value).unlink()


def tamper_malformed(c, value):
    sidecar_path(c, value).write_bytes(b"{broken")


def tamper_other_input(c, value):
    other = make_input(c, day=29)
    data = c.draft.read_bytes()
    sidecar_path(c, value).write_bytes(ra.build_sidecar_bytes(other, renderer=RENDERER, pdf_sha256=sha(data),
                                                              pdf_bytes=len(data)))


def tamper_payload(c, value):
    path = sidecar_path(c, value)
    path.write_bytes(edited(path.read_bytes(), lambda d: d["render_input"]["input"]["client"].update(name="X")))


def tamper_pdf_hash(c, value):
    path = sidecar_path(c, value)
    path.write_bytes(edited(path.read_bytes(), lambda d: d["pdf"].update(sha256="9" * 64)))


def tamper_pdf_bytes(c, value):
    path = sidecar_path(c, value)
    path.write_bytes(edited(path.read_bytes(), lambda d: d["pdf"].update(bytes=d["pdf"]["bytes"] + 1)))


def tamper_draft(c, value):
    c.draft.write_bytes(pdf("somebody else's draft"))


def tamper_format(c, value):
    path = sidecar_path(c, value)
    path.write_bytes(edited(path.read_bytes(), lambda d: d["render_input_format"].update(version=1)))


@pytest.mark.parametrize("tamper,category", [
    (tamper_missing, "render_sidecar_missing"), (tamper_malformed, "render_sidecar_invalid"),
    (tamper_other_input, "render_identity_mismatch"), (tamper_payload, "render_identity_mismatch"),
    (tamper_pdf_hash, "render_pdf_mismatch"), (tamper_pdf_bytes, "render_pdf_mismatch"),
    (tamper_draft, "render_pdf_mismatch"), (tamper_format, "render_input_format_invalid")])
def test_a_sidecar_or_draft_problem_stops_before_any_change(ctx, tamper, category):
    life = run_lifecycle(ctx, tamper=tamper)
    stop(life.result, category)
    assert (life.result.step, life.result.status_code) == ("verify_draft", "draft_invalid")
    assert state(ctx) == life.before  # reports, records, database and runs are exactly as before the call
    assert records(ctx) == [] and runs(ctx) == [] and ctx.draft.exists()
    report, listed, excluded = assert_reader_and_audit_agree(ctx, valid=0)
    assert listed == [] and report.outcome == fa.OUTCOME_CONSISTENT  # only the draft awaiting finalization
    assert rstate.finalized_render_hashes(ctx.reviewer) == frozenset()


def test_a_render_input_that_does_not_match_the_bundle_stops_before_any_change(ctx):
    value = make_input(ctx)
    changed = dataclasses.replace(value, client=dataclasses.replace(value.client, name="Another Client"))
    life = run_lifecycle(ctx, finalize_input=changed)
    stop(life.result, "render_input_bundle_mismatch")
    assert state(ctx) == life.before and records(ctx) == []
    assert_reader_and_audit_agree(ctx, valid=0)


def test_historical_statements_that_differ_from_the_rendered_ones_stop_before_any_change(ctx):
    insights = with_statements(ctx)
    life = run_lifecycle(ctx, history_insights=insights, finalize_history=None)
    stop(life.result, "render_statements_mismatch")
    assert state(ctx) == life.before and records(ctx) == []
    assert_reader_and_audit_agree(ctx, valid=0)


def test_matching_historical_statements_finalize_and_are_recorded(ctx):
    insights = with_statements(ctx)
    life = run_lifecycle(ctx, history_insights=insights, finalize_history=insights)
    assert life.result.finalized
    record = assert_record_consistent(ctx, life.result.record_name, life.value)
    assert [s["text"] for s in record["client_facing_history"]] == [s.text for s in life.value.historical_statements]
    assert_reader_and_audit_agree(ctx, valid=1)


# --- interruptions in sidecar creation and draft handoff -----------------------------------------------------------------------------------

def test_an_interrupted_sidecar_write_leaves_a_partial_and_a_retry_creates_the_original_identity(ctx):
    value = make_input(ctx, day=28)
    failed = produce(ctx, value, ops=FaultOps(fail="write_half"))
    assert (failed.outcome, failed.code) == ("failed", "sidecar_write_incomplete") and not failed.ok
    partials = [n for n in files(ctx.reports) if ff.is_partial_name(n)]
    assert len(partials) == 1 and not sidecar_path(ctx, value).exists() and not ctx.draft.exists()
    blocked = fin(ctx, render_input=value)  # no draft exists yet: the existing draft check stops first
    stop(blocked, "draft_invalid")
    assert blocked.draft_problems == ("not_found",)
    assert records(ctx) == [] and runs(ctx) == []
    life_retry = produce(ctx, value)
    assert life_retry.outcome == "created"
    result = fin(ctx, render_input=life_retry.render_input)
    assert result.finalized
    assert_record_consistent(ctx, result.record_name, value)
    report, _, _ = assert_reader_and_audit_agree(ctx, valid=1, allowed_extra=(fa.PARTIAL_FILE,))
    assert [f.name for f in report.by_category(fa.PARTIAL_FILE)] == partials  # kept, reported, never deleted
    assert [n for n in files(ctx.reports) if ff.is_partial_name(n)] == partials


class FailNthWrite(FaultOps):
    def __init__(self, n):
        super().__init__()
        self.n, self.count = n, 0

    def write(self, handle, data):
        self.count += 1
        if self.count == self.n:
            raise OSError(5, "injected")
        super().write(handle, data)


def test_an_interrupted_draft_handoff_keeps_the_sidecar_and_a_retry_keeps_its_date(ctx):
    first = make_input(ctx, day=28)
    failed = produce(ctx, first, ops=FailNthWrite(2))  # write 1 is the sidecar, write 2 the draft copy
    assert (failed.outcome, failed.code) == ("failed", "handoff_incomplete") and failed.created_sidecar
    original = sidecar_path(ctx, first).read_bytes()
    assert not ctx.draft.exists() and len([n for n in files(ctx.reports) if ff.is_partial_name(n)]) == 1
    later = make_input(ctx, day=30)
    retry = produce(ctx, later)
    assert retry.outcome == "resumed" and retry.generation_date == dt.date(2026, 10, 28)
    assert retry.render_input_sha256 == rh.render_input_sha256(first) and retry.candidate_matches is False
    result = fin(ctx, render_input=retry.render_input)
    assert result.finalized
    assert_record_consistent(ctx, result.record_name, first)
    assert sidecar_path(ctx, first).read_bytes() == original and not sidecar_path(ctx, later).exists()
    assert_reader_and_audit_agree(ctx, valid=1, allowed_extra=(fa.PARTIAL_FILE,))


# --- interruptions in finalization, retried with the original identity -------------------------------------------------------------------------

def interrupt_snapshot(c):
    c.faults.arm("write")
    return {}


def interrupt_event(c):
    c.faults.arm("add_event")
    return {}


def interrupt_promotion(c):
    return {"ops": FaultOpsFor("rename_noreplace", 0)}


def interrupt_record_create(c):
    return {"ops": FaultOpsFor("create_exclusive", 0)}


def interrupt_record_promotion(c):
    return {"ops": FaultOpsFor("rename_noreplace", 1)}


@pytest.mark.parametrize("interrupt,draft_remains", [
    (interrupt_snapshot, True), (interrupt_event, True), (interrupt_promotion, True),
    (interrupt_record_create, False), (interrupt_record_promotion, False)])
def test_interrupted_finalization_is_retried_with_the_original_hash_and_date(ctx, interrupt, draft_remains):
    original = make_input(ctx, day=28)
    drafted = produce(ctx, original)
    digest = rh.render_input_sha256(original)
    sidecar_before = sidecar_path(ctx, original).read_bytes()
    failed = fin(ctx, render_input=drafted.render_input, **interrupt(ctx))
    assert not failed.finalized and failed.outcome in (rf.OUTCOME_FAILED, rf.OUTCOME_CONFLICT)
    assert ctx.draft.exists() == draft_remains and sidecar_path(ctx, original).read_bytes() == sidecar_before
    mid = audit(ctx)  # the audit works on any partial state and never claims success for it
    assert mid.finalized_valid == 0 and records_valid(ctx) == 0
    if draft_remains:  # a retry on another day builds another candidate: the sidecar's identity still wins
        again = produce(ctx, make_input(ctx, day=30))
        assert again.outcome == "already_prepared" and again.render_input_sha256 == digest
        assert again.generation_date == dt.date(2026, 10, 28) and again.candidate_matches is False
        subject = again.render_input
    else:  # the draft was promoted: the identity is read from the open sidecar
        opened = rstate.open_sidecars(ctx.reports, finalized=rstate.finalized_render_hashes(ctx.reviewer))
        assert [s.render_input_sha256 for s in opened] == [digest]
        subject = opened[0].render_input
    result = fin(ctx, render_input=subject)
    assert result.finalized
    record = assert_record_consistent(ctx, result.record_name, original)
    assert record["finalization"]["render_input_sha256"] == digest
    assert sidecar_path(ctx, original).read_bytes() == sidecar_before and len(records(ctx)) == 1
    assert_reader_and_audit_agree(ctx, valid=1, allowed_extra=(fa.PARTIAL_FILE, fa.SNAPSHOT_WITHOUT_FINALIZATION,
                                                               fa.RENDERED_EVENT_WITHOUT_FINALIZATION))


def records_valid(c):
    return len([n for n in records(c) if (c.reviewer / n).exists() and not ff.is_partial_name(n)
                and read_record(c, n)["snapshot"]["run_id"] in reader_view(c)[0]])


# --- duplicate, supersede, mixed history, G1 -----------------------------------------------------------------------------------------------

def test_duplicate_finalization_changes_nothing(ctx):
    life = run_lifecycle(ctx)
    before = state(ctx)
    tree_before = tree(ctx)
    again = fin(ctx, render_input=life.drafted.render_input)
    assert again.outcome == rf.OUTCOME_ALREADY and again.record_outcome == rw.OUTCOME_ALREADY
    assert state(ctx) == before and tree(ctx) == tree_before
    assert_record_consistent(ctx, again.record_name, life.value)
    assert_reader_and_audit_agree(ctx, valid=1)


def test_supersede_keeps_two_valid_historical_records(ctx):
    first = run_lifecycle(ctx, day=28)
    old_pdf = files(ctx.reports)[FINAL]
    second = run_lifecycle(ctx, day=30, replace_existing=True)
    assert first.result.finalized and second.result.finalized and first.result.run_id == second.result.run_id
    assert files(ctx.reports)[sup(old_pdf)] == old_pdf and files(ctx.reports)[FINAL] != old_pdf
    for life in (first, second):
        assert_record_consistent(ctx, life.result.record_name, life.value)
    names = {first.result.record_name, second.result.record_name}
    assert set(records(ctx)) == names and len(names) == 2
    report, listed, _ = assert_reader_and_audit_agree(ctx, valid=2, legacy=0)
    assert listed == [first.result.run_id] and report.outcome == fa.OUTCOME_CONSISTENT
    assert rstate.finalized_render_hashes(ctx.reviewer) == {rh.render_input_sha256(first.value),
                                                           rh.render_input_sha256(second.value)}
    assert len(report.by_category(fa.SUPERSEDED_PDF)) == 1


def test_a_version_1_record_followed_by_a_version_2_record_in_one_period(ctx):
    legacy_pdf = put_draft(ctx, pdf("legacy"))
    legacy = fin(ctx)
    life = run_lifecycle(ctx, day=30, replace_existing=True)
    assert legacy.finalized and life.result.finalized
    assert_record_consistent(ctx, legacy.record_name)
    assert_record_consistent(ctx, life.result.record_name, life.value)
    report, _, _ = assert_reader_and_audit_agree(ctx, valid=2, legacy=1)
    assert report.outcome == fa.OUTCOME_CONSISTENT and files(ctx.reports)[sup(legacy_pdf)] == legacy_pdf
    versions = {f.name: f.record_version for f in report.by_category(fa.FINALIZED_VALID)}
    assert versions == {legacy.record_name: 1, life.result.record_name: 2}
    scan = rstate.scan_finalized_render_hashes(ctx.reviewer)
    assert (scan.hashes, scan.v1_records, scan.v2_records) == ({rh.render_input_sha256(life.value)}, 1, 1)


def test_a_version_2_finalization_of_the_pdf_of_a_version_1_record_is_a_conflict_that_changes_nothing(ctx):
    data = put_draft(ctx, pdf("same bytes"))
    legacy = fin(ctx)
    value = make_input(ctx)
    drafted = produce(ctx, value, renderer=fixed_renderer(data))
    assert drafted.outcome == "created"
    before = state(ctx)
    events = [e for r in runs(ctx) for e in rendered(ctx, r[0])]
    result = fin(ctx, render_input=drafted.render_input)
    assert (result.outcome, result.category) == (rf.OUTCOME_CONFLICT, "record_identity_differs") and not result.incomplete
    assert state(ctx) == before and [e for r in runs(ctx) for e in rendered(ctx, r[0])] == events
    assert records(ctx) == [legacy.record_name] and read_record(ctx, legacy.record_name)["schema_version"] == 1
    assert_reader_and_audit_agree(ctx, valid=1, legacy=1)


def test_g1_the_duplicate_draft_after_promotion_is_benign_and_finalizes_consistently(ctx):
    first = make_input(ctx, day=28)
    drafted = produce(ctx, first)
    failed = fin(ctx, render_input=drafted.render_input, ops=FaultOpsFor("rename_noreplace", 1))
    assert failed.incomplete and DRAFT not in files(ctx.reports) and FINAL in files(ctx.reports)
    again = produce(ctx, make_input(ctx, day=30))  # the sidecar is still open, so a duplicate draft is handed off
    assert again.outcome == "resumed" and again.handed_off and again.generation_date == dt.date(2026, 10, 28)
    result = fin(ctx, render_input=again.render_input)
    assert result.finalized and result.duplicate_draft
    assert files(ctx.reports)[result.duplicate_draft] == files(ctx.reports)[FINAL]
    assert_record_consistent(ctx, result.record_name, first)
    report, _, _ = assert_reader_and_audit_agree(ctx, valid=1, allowed_extra=(fa.PARTIAL_FILE,))
    assert len(report.by_category(fa.DUPLICATE_PDF)) == 1


# --- the reader and the audit agree across the lifecycle --------------------------------------------------------------------------------------

def test_reader_and_audit_agree_before_during_and_after_a_lifecycle(ctx):
    assert reader_view(ctx) == ([], {}) or reader_view(ctx)[0] == []
    assert audit(ctx).finalized_valid == 0
    value = make_input(ctx)
    drafted = produce(ctx, value)
    assert_reader_and_audit_agree(ctx, valid=0)  # a draft awaiting finalization is information, not finalization
    ctx.faults.arm("add_event")
    assert not fin(ctx, render_input=drafted.render_input).finalized
    listed, excluded = reader_view(ctx)
    assert listed == [] and len(excluded) == 1  # stored, but not finalized
    assert audit(ctx).finalized_valid == 0
    assert fin(ctx, render_input=drafted.render_input).finalized
    assert_reader_and_audit_agree(ctx, valid=1)


def test_the_audit_and_discovery_are_read_only_across_a_lifecycle(ctx):
    run_lifecycle(ctx)
    before = tree(ctx)
    audit(ctx)
    audit(ctx, deep_database_check=True)
    rstate.scan_finalized_render_hashes(ctx.reviewer)
    rstate.open_sidecars(ctx.reports)
    reader_view(ctx)
    assert tree(ctx) == before


# --- R2: can a production-shaped bundle build its reviewer record without the test helper's note filter? -------------------------------

def raw_pipeline(env, history=True):
    """`test_reviewer_record.pipeline` minus `supported_notes_only`: the bundle exactly as `assemble_report` returns it."""
    if history:
        trr.with_history(env)
    bundle = trr.assemble(env, **({} if history else {"history_db_path": env.tmp / "none.sqlite"}))
    rules = hr.evaluate_history_rules(
        comparison=bundle.historical_comparison, metrics=bundle.base_insights.metrics,
        base_insights=bundle.base_insights, crosscheck=bundle.reviewer_report.ranking_crosscheck,
        target_country="United States")
    composed = he.compose_history_insights(rule_output=rules, base_insights=bundle.base_insights)
    annotations = hrec.annotate_recommendations(
        recommendations=bundle.base_insights.recommendations, comparison=bundle.historical_comparison,
        history_insights=composed)
    return bundle, composed, annotations


@pytest.mark.parametrize("history", [True, False])
def test_r2_probe_a_production_shaped_bundle_builds_its_record_without_the_note_filter(env, history):
    bundle, composed, annotations = raw_pipeline(env, history)
    notes = [("base", n) for n in bundle.reviewer_report.base_notes] + [("stage", n) for n in bundle.reviewer_report.notes] \
        + [("history", n) for n in composed.notes]
    unsupported = [(group, n.source, n.category) for group, n in notes
                   if n.source not in rs.NOTE_SOURCES or n.category not in rs.NOTE_CATEGORIES
                   or n.severity not in rs.NOTE_SEVERITIES]
    error = None
    record = None
    try:
        record = rr.build_reviewer_record(bundle=bundle, receipt=receipt(), history_insights=composed,
                                          recommendation_annotations=annotations)
    except Exception as err:  # reported with its exact text; never swallowed and never repaired
        error = f"{type(err).__name__}: {err}"
    print(f"R2 PROBE history={history}: {len(notes)} notes, unsupported={unsupported}, error={error}")
    assert (unsupported, error) == ([], None), (unsupported, error)
    assert rs.validate_finalized_record(record) is record


# --- the single real-Chromium lifecycle -------------------------------------------------------------------------------------------------------

def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            browser.close()
        return True
    except Exception:
        return False


def test_real_chromium_lifecycle_from_a_genuine_bundle(ctx):
    if not chromium_available():
        pytest.skip("Chromium could not be started")
    from test_pdf_renderer import SOURCES
    captured = []

    def real(render_input, *, workdir, logo_path, fonts):
        outcome = pr.render_report_pdf(render_input, workdir=workdir, logo_path=LOGO, fonts=SOURCES)
        captured.append(outcome)
        return outcome

    value = make_input(ctx)  # built from the genuine bundle by render_identity.build_render_input
    drafted = produce(ctx, value, renderer=real)
    assert drafted.outcome == "created", (drafted.outcome, drafted.code)  # a render with layout issues is not handed off
    outcome = captured[0]
    assert outcome.ok and outcome.issues == () and outcome.blocked_requests == 0
    assert outcome.render_input_sha256 == rh.render_input_sha256(value) == drafted.render_input_sha256
    draft = ctx.draft.read_bytes()
    info = pv.inspect_pdf(draft)
    print(f"REAL CHROMIUM: {info.page_count} A4 pages, {len(draft)} bytes, browser {outcome.browser_version}, "
          f"layout issues {len(outcome.issues)}")
    assert info.page_count == outcome.pdf.page_count >= 3 and len(outcome.measures) == info.page_count
    assert all(pv.is_a4(box) for box in info.page_boxes) and info.xref_consistent and info.ends_with_eof
    assert info.fonts == ("DejaVuSans", "DejaVuSans-Bold") and info.has_image
    assert (sha(draft), len(draft)) == (outcome.pdf_sha256, outcome.byte_count) == (drafted.pdf_sha256, drafted.pdf_bytes)
    sidecar = ra.read_sidecar(ctx.reports, drafted.sidecar_name)
    assert dict(sidecar.renderer)["browser"] == outcome.browser_version
    result = fin(ctx, render_input=drafted.render_input)
    assert result.finalized and files(ctx.reports)[FINAL] == draft
    record = assert_record_consistent(ctx, result.record_name, value)
    assert record["finalization"]["render_input_sha256"] == outcome.render_input_sha256
    assert (record["finalization"]["pdf_sha256"], record["finalization"]["pdf_bytes"]) == (outcome.pdf_sha256,
                                                                                         outcome.byte_count)
    report, listed, excluded = assert_reader_and_audit_agree(ctx, valid=1, legacy=0)
    assert (report.outcome, listed, excluded) == (fa.OUTCOME_CONSISTENT, [result.run_id], {})
