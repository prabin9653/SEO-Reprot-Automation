import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_history_access as tha
import test_history_integration as ti
import test_report_run as trr
import test_reviewer_record as trv
from test_finalize_files import FaultOps
from test_history_access import env  # noqa: F401  (temporary history and output folders)
from seo_report import attempt_log as al
from seo_report import finalize_files as ff
from seo_report import history_access as ha
from seo_report import output_layout as layout
from seo_report import record_schema as rs
from seo_report import record_writer as rw
from seo_report import report_finalize as rf
from seo_report.snapshots import store as st

TS = "2026-10-30T12:00:00+00:00"
DRAFT, FINAL = layout.DRAFT_PDF_NAME, layout.STANDARD_PDF_NAME


def pdf(tag: str) -> bytes:
    return f"%PDF-1.4\n% {tag}\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n".encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sup(data: bytes, n: int = 1) -> str:
    return ff.variant_name("superseded", sha(data)[:16], n)


def dup(data: bytes, n: int = 1) -> str:
    return ff.variant_name("duplicate", sha(data)[:16], n)


class Faults:
    """Store faults: a method name fails once (or always) with an error whose text must never be shown."""

    def __init__(self):
        self.pending = {}

    def arm(self, method, error=None, times=1):
        self.pending[method] = [error or st.SnapshotStoreError("C:\\secret\\db.sqlite is locked"), times]

    def check(self, method):
        entry = self.pending.get(method)
        if entry and entry[1] > 0:
            entry[1] -= 1
            raise entry[0]


def make_context(env, history=True, empty=False):
    if history:
        bundle, composed, ann = trv.pipeline(env, history=True)
        db = env.db
    elif empty:
        bundle = trv.supported_notes_only(trr.assemble(env))
        rules = trv.hr.evaluate_history_rules(
            comparison=bundle.historical_comparison, metrics=bundle.base_insights.metrics,
            base_insights=bundle.base_insights, crosscheck=bundle.reviewer_report.ranking_crosscheck,
            target_country="United States")
        composed = trv.he.compose_history_insights(rule_output=rules, base_insights=bundle.base_insights)
        ann = trv.hrec.annotate_recommendations(recommendations=bundle.base_insights.recommendations,
                                                comparison=bundle.historical_comparison, history_insights=composed)
        db = env.db
    else:
        bundle, composed, ann = trv.pipeline(env, history=False)
        db = env.tmp / "none.sqlite"
    key, start, end = bundle.snapshot.client_key, bundle.snapshot.period_start, bundle.snapshot.period_end
    reports = layout.report_dir(env.out, key, start, end)
    reviewer = layout.reviewer_dir(env.out, key, start, end)
    attempts = layout.attempt_dir(env.out, key, start, end)
    for folder in (reports, reviewer, attempts):
        folder.mkdir(parents=True, exist_ok=True)
    ids = iter(f"0198c1a2-7b3c-7d4e-8f5a-{n:012d}" for n in range(1000, 1300))
    ticks = iter(f"2026-10-30T10:{i // 60:02d}:{i % 60:02d}+00:00" for i in range(3000))
    faults = Faults()

    def factory(path, **kwargs):
        class Store(st.SnapshotStore):
            def write(self, snapshot, **kw):
                faults.check("write")
                return super().write(snapshot, **kw)

            def add_event(self, run_id, kind, detail=None):
                faults.check("add_event")
                return super().add_event(run_id, kind, detail)

            def verify_run(self, run_id):
                faults.check("verify_run")
                return super().verify_run(run_id)

        return Store(path, now=lambda: next(ticks), new_run_id=lambda: next(ids), **kwargs)

    return SimpleNamespace(env=env, bundle=bundle, composed=composed, ann=ann, db=db, reports=reports,
                           reviewer=reviewer, attempts=attempts, draft=reports / DRAFT, factory=factory, faults=faults)


@pytest.fixture
def ctx(env):
    return make_context(env)


def put_draft(c, data):
    c.draft.write_bytes(data)
    return data


def fin(c, **overrides):
    options = dict(bundle=c.bundle, draft_path=c.draft, output_root=c.env.out, history_db_path=c.db, finalized_at=TS,
                   history_insights=c.composed, recommendation_annotations=c.ann, store_factory=c.factory,
                   attempt_ops=ff.FileOps())
    options.update(overrides)
    return rf.finalize_report(**options)


def files(folder):
    return {p.name: p.read_bytes() for p in Path(folder).iterdir() if p.is_file()}


def runs(c):
    if not Path(c.db).exists():
        return []
    reader = st.SnapshotReader(c.db)
    try:
        key = c.bundle.snapshot.client_key
        return [(i.run_id, i.revision, i.period_start) for i in reader.list_runs(key, include_voided=True)
                if (i.period_start, i.period_end) == (c.bundle.snapshot.period_start, c.bundle.snapshot.period_end)]
    finally:
        reader.close()


def rendered(c, run_id):
    reader = st.SnapshotReader(c.db)
    try:
        return [e.detail for e in reader.events(run_id) if e.kind == "rendered"]
    finally:
        reader.close()


def finalized_ids(c):
    access = ha.open_history(c.db, c.env.out)
    try:
        return [i.run_id for i in access.reader.list_runs(c.bundle.snapshot.client_key)]
    finally:
        access.close()


def records(c):
    return sorted(p.name for p in c.reviewer.iterdir() if p.name.startswith("reviewer."))


def attempt_files(c):
    return sorted(p.name for p in c.attempts.iterdir())


def read_record(c, name):
    return json.loads((c.reviewer / name).read_text(encoding="utf-8"))


# --- happy paths -----------------------------------------------------------------------------------------------------------------------

def test_first_time_finalization_runs_every_step_and_the_reader_confirms_it(ctx):
    data = put_draft(ctx, pdf("a"))
    result = fin(ctx)
    assert (result.outcome, result.finalized, result.ok) == (rf.OUTCOME_FINALIZED, True, True)
    assert (result.category, result.step, result.incomplete) == (None, None, False)
    assert (result.snapshot_created, result.event_recorded, result.revision) == (True, True, 1)
    assert (result.pdf_outcome, result.final_name, result.pdf_sha256, result.pdf_bytes) == \
        ("promoted", FINAL, sha(data), len(data))
    assert result.record_name == layout.reviewer_record_name(result.run_id, sha(data))
    assert result.record_outcome == "written" and result.attempt_log == rf.ATTEMPT_NOT_NEEDED
    assert files(ctx.reports) == {FINAL: data}
    assert records(ctx) == [result.record_name]
    assert rendered(ctx, result.run_id) == [f"Report rendered (PDF {sha(data)})"]
    assert result.run_id in finalized_ids(ctx)
    assert attempt_files(ctx) == []  # a success writes no attempt record


def test_the_record_matches_the_stored_snapshot_the_pdf_and_the_client_hash(ctx):
    data = put_draft(ctx, pdf("a"))
    result = fin(ctx)
    record = read_record(ctx, result.record_name)
    assert rs.validate_finalized_record(record)
    assert record["snapshot"]["run_id"] == result.run_id and record["snapshot"]["revision"] == 1
    assert record["snapshot"]["content_hash"] == ctx.bundle.snapshot.content_hash
    assert (record["finalization"]["pdf_sha256"], record["finalization"]["pdf_bytes"]) == (sha(data), len(data))
    assert record["finalization"]["finalized_at"] == TS
    assert record["finalization"]["rendered_event"]["detail"] == f"Report rendered (PDF {sha(data)})"
    from seo_report.client_content_hash import client_content_sha256
    assert record["finalization"]["client_content_sha256"] == client_content_sha256(ctx.bundle.client_content)
    assert record["finalization"]["superseded_previous_pdf"] is None


def test_a_second_identical_call_is_already_finalized_and_writes_nothing(ctx):
    data = put_draft(ctx, pdf("a"))
    first = fin(ctx)
    before = (files(ctx.reports), files(ctx.reviewer), runs(ctx), rendered(ctx, first.run_id))
    second = fin(ctx)
    assert (second.outcome, second.finalized) == (rf.OUTCOME_ALREADY, True)
    assert (second.snapshot_created, second.event_recorded, second.pdf_outcome, second.record_outcome) == \
        (False, False, "already_promoted", "already_written")
    assert (files(ctx.reports), files(ctx.reviewer), runs(ctx), rendered(ctx, first.run_id)) == before
    assert second.run_id == first.run_id and data


def test_an_identical_draft_next_to_the_finalized_report_is_kept_as_a_duplicate(ctx):
    data = put_draft(ctx, pdf("a"))
    first = fin(ctx)
    put_draft(ctx, data)
    second = fin(ctx)
    assert second.outcome == rf.OUTCOME_ALREADY and second.duplicate_draft == dup(data)
    assert second.pdf_outcome == "identical_existing"
    assert files(ctx.reports) == {FINAL: data, dup(data): data}
    assert records(ctx) == [first.record_name]


def test_replacement_is_off_by_default_and_changes_nothing(ctx):
    put_draft(ctx, pdf("a"))
    first = fin(ctx)
    put_draft(ctx, pdf("b"))
    before = (files(ctx.reports), files(ctx.reviewer), runs(ctx), rendered(ctx, first.run_id))
    result = fin(ctx)
    assert (result.outcome, result.category, result.status_code, result.step) == \
        (rf.OUTCOME_CONFLICT, "existing_report_differs", "existing_report_conflict", "promote")
    assert result.incomplete is False and not result.finalized
    assert (files(ctx.reports), files(ctx.reviewer), runs(ctx), rendered(ctx, first.run_id)) == before


def test_authorized_replacement_preserves_the_old_report_and_resolves_both(ctx):
    old, new = put_draft(ctx, pdf("old")), None
    first = fin(ctx)
    new = put_draft(ctx, pdf("new"))
    result = fin(ctx, replace_existing=True)
    assert result.outcome == rf.OUTCOME_FINALIZED and result.pdf_outcome == "replaced"
    assert result.preserved_report == sup(old) and result.run_id == first.run_id
    assert (result.snapshot_created, result.event_recorded) == (False, True)
    assert files(ctx.reports) == {FINAL: new, sup(old): old}
    assert sorted(records(ctx)) == sorted([first.record_name, result.record_name])
    record = read_record(ctx, result.record_name)
    assert record["finalization"]["superseded_previous_pdf"] == {"sha256_16": sha(old)[:16], "variant_index": 1}
    assert len(rendered(ctx, first.run_id)) == 2
    assert ha.FinalizedOnlyReader._check_pdf(ctx.reports, sha(old), len(old)) == (True, True)
    assert first.run_id in finalized_ids(ctx)


def test_a_draft_that_exists_only_as_a_superseded_copy_is_a_conflict_even_with_replacement(ctx):
    old = put_draft(ctx, pdf("old"))
    fin(ctx)
    put_draft(ctx, pdf("new"))
    fin(ctx, replace_existing=True)
    put_draft(ctx, old)  # the first report comes back while report.pdf holds the second
    before = (files(ctx.reports), files(ctx.reviewer))
    result = fin(ctx, replace_existing=True)
    assert (result.outcome, result.category) == (rf.OUTCOME_CONFLICT, "superseded_only_copy")
    assert files(ctx.reports) == before[0] and files(ctx.reviewer) == before[1]


def test_a_missing_history_database_is_created_and_the_report_finalized(env):
    c = make_context(env, history=False)
    assert not c.db.exists()
    data = put_draft(c, pdf("a"))
    result = fin(c)
    assert result.outcome == rf.OUTCOME_FINALIZED and c.db.exists()
    assert result.run_id in finalized_ids(c) and files(c.reports) == {FINAL: data}


def test_an_empty_history_database_finalizes(env):
    c = make_context(env, history=False, empty=True)
    put_draft(c, pdf("a"))
    result = fin(c)
    assert result.outcome == rf.OUTCOME_FINALIZED and result.run_id in finalized_ids(c)


def test_an_existing_matching_snapshot_and_event_are_reused(ctx):
    data = put_draft(ctx, pdf("a"))
    ctx.faults.arm("add_event")
    failed = fin(ctx)
    assert failed.category == "event_failed" and failed.incomplete
    store = ctx.factory(ctx.db)
    store.add_event(failed.run_id, "rendered", f"Report rendered (PDF {sha(data)})")
    store.close()
    result = fin(ctx)
    assert result.outcome == rf.OUTCOME_FINALIZED
    assert (result.snapshot_created, result.event_recorded, result.run_id) == (False, False, failed.run_id)
    assert len(rendered(ctx, result.run_id)) == 1


def test_a_matching_reviewer_record_is_idempotent_only_after_reader_verification(ctx):
    put_draft(ctx, pdf("a"))
    first = fin(ctx)
    (ctx.reports / FINAL).unlink()  # the report disappears; the record and event remain
    assert first.run_id not in finalized_ids(ctx)
    put_draft(ctx, pdf("a"))
    result = fin(ctx)
    assert result.outcome == rf.OUTCOME_FINALIZED and result.record_outcome == "already_written"
    assert result.pdf_outcome == "promoted" and first.run_id in finalized_ids(ctx)


# --- stale bundle ----------------------------------------------------------------------------------------------------------------------

def assert_untouched_stale(c, result, before):
    assert (result.outcome, result.category, result.status_code, result.step) == \
        (rf.OUTCOME_STALE, "stale_bundle", "stale_bundle", "preflight")
    assert result.incomplete is False and result.run_id is None
    assert before == (files(c.reports), files(c.reviewer), runs(c))
    assert c.draft.exists()


def snapshot_state(c):
    return files(c.reports), files(c.reviewer), runs(c)


def test_unchanged_history_permits_finalization(ctx):
    put_draft(ctx, pdf("a"))
    assert fin(ctx).outcome == rf.OUTCOME_FINALIZED


def test_a_new_baseline_that_appears_after_assembly_is_stale(ctx, env):
    put_draft(ctx, pdf("a"))
    newer = tha.write_run(env, ti.P_B)  # an earlier period, not yet finalized: only the excluded list changes
    before = snapshot_state(ctx)
    result = fin(ctx)
    assert_untouched_stale(ctx, result, before)
    assert newer


def test_a_finalized_older_report_that_changes_no_baseline_is_not_relevant(ctx, env):
    put_draft(ctx, pdf("a"))
    run = tha.write_run(env, ti.P_B)  # older than the baseline in use; it was already listed as unfinalized? no: new
    tha.finalize(env, run)
    result = fin(ctx)
    assert result.outcome == rf.OUTCOME_FINALIZED  # no selected baseline, status or skipped entry differs


def test_a_voided_baseline_is_stale(ctx, env):
    put_draft(ctx, pdf("a"))
    baseline = [i.run_id for i in env.store.list_runs("example-co") if i.period_start == ti.P_A.current.start.isoformat()]
    env.store.add_event(baseline[0], "voided", "entered by mistake")
    before = snapshot_state(ctx)
    assert_untouched_stale(ctx, fin(ctx), before)


def test_a_changed_baseline_revision_is_stale(ctx, env):
    put_draft(ctx, pdf("a"))
    run = tha.write_run(env, ti.P_A, keywords=[ti.te.kw("another keyword", "", 3, "Dallas")])  # revision 2
    tha.finalize(env, run)
    before = snapshot_state(ctx)
    assert_untouched_stale(ctx, fin(ctx), before)


def test_history_that_becomes_available_after_assembly_is_stale(env):
    first = make_context(env, history=True)  # puts a finalized baseline into env.db
    c = make_context(env, history=False)
    put_draft(c, pdf("a"))
    before = snapshot_state(c)
    result = fin(c, history_db_path=env.db)  # the assembled bundle saw no history; now there is some
    assert_untouched_stale(c, result, before)
    assert first


def test_a_changed_unfinalized_run_list_is_stale(ctx, env):
    put_draft(ctx, pdf("a"))
    tha.write_run(env, ti.P_C)
    assert_untouched_stale(ctx, fin(ctx), snapshot_state(ctx))


def test_an_orphan_of_the_same_period_is_ignored_by_the_stale_check(ctx, env):
    put_draft(ctx, pdf("a"))
    orphan = env.store.write(trr.ti.make_snapshot(trr.CURRENT, keywords=[ti.te.kw("orphan", "", 1, "Dallas")])).run_id
    result = fin(ctx)
    assert result.outcome == rf.OUTCOME_FINALIZED and result.run_id != orphan and result.revision == 2
    assert orphan not in finalized_ids(ctx)


def test_a_stale_bundle_is_never_rebuilt_or_changed(ctx, env):
    put_draft(ctx, pdf("a"))
    tha.write_run(env, ti.P_C)
    bundle = copy.deepcopy(ctx.bundle)
    fin(ctx)
    assert ctx.bundle == bundle


# --- reconciliation ---------------------------------------------------------------------------------------------------------------------

def test_a_renderer_partial_draft_stops_finalization_and_is_left_alone(ctx):
    put_draft(ctx, pdf("a"))
    (ctx.reports / layout.DRAFT_PARTIAL_NAME).write_bytes(b"half")
    before = snapshot_state(ctx)
    result = fin(ctx)
    assert (result.outcome, result.category, result.status_code) == (rf.OUTCOME_CONFLICT, "draft_partial_conflict",
                                                                    "draft_partial_conflict")
    assert snapshot_state(ctx) == before and (ctx.reports / layout.DRAFT_PARTIAL_NAME).read_bytes() == b"half"


def test_stale_partial_files_are_untouched_and_not_adopted(ctx):
    put_draft(ctx, pdf("a"))
    (ctx.reviewer / ".12345678.0123456789ab.partial").write_bytes(b'{"half"')
    (ctx.reports / ".12345678.0123456789cd.partial").write_bytes(b"%PDF half")
    result = fin(ctx)
    assert result.outcome == rf.OUTCOME_FINALIZED
    assert (ctx.reviewer / ".12345678.0123456789ab.partial").read_bytes() == b'{"half"'
    assert (ctx.reports / ".12345678.0123456789cd.partial").read_bytes() == b"%PDF half"


def test_a_missing_draft_without_a_promoted_report_is_rejected(ctx):
    before = snapshot_state(ctx)
    result = fin(ctx)
    assert (result.outcome, result.category, result.status_code, result.step) == \
        (rf.OUTCOME_REJECTED, "draft_invalid", "draft_invalid", "verify_draft")
    assert result.draft_problems == ("not_found",) and snapshot_state(ctx) == before


@pytest.mark.parametrize("data,problem", [(b"", "empty"), (b"nope %%EOF", "bad_header"), (b"%PDF-1.4 x", "missing_eof")])
def test_an_invalid_draft_is_rejected_before_any_change(ctx, data, problem):
    put_draft(ctx, data)
    before = snapshot_state(ctx)
    result = fin(ctx)
    assert result.outcome == rf.OUTCOME_REJECTED and problem in result.draft_problems
    assert snapshot_state(ctx) == before and not result.incomplete


def test_a_draft_path_that_is_not_the_standard_draft_is_rejected(ctx):
    other = ctx.reports / "other.pdf"
    other.write_bytes(pdf("a"))
    result = fin(ctx, draft_path=other)
    assert (result.outcome, result.category) == (rf.OUTCOME_REJECTED, "draft_invalid")
    assert other.exists()


def test_an_invalid_existing_reviewer_record_stops_before_any_change(ctx):
    put_draft(ctx, pdf("a"))
    first = fin(ctx)
    (ctx.reviewer / first.record_name).write_bytes(b"not json")
    before = (files(ctx.reports), files(ctx.reviewer), runs(ctx), rendered(ctx, first.run_id))
    result = fin(ctx)
    assert (result.outcome, result.category, result.status_code, result.step) == \
        (rf.OUTCOME_CONFLICT, "existing_record_invalid", "reviewer_record_conflict", "record")
    assert (files(ctx.reports), files(ctx.reviewer), runs(ctx), rendered(ctx, first.run_id)) == before


def test_an_existing_record_with_another_identity_is_a_conflict(ctx):
    put_draft(ctx, pdf("a"))
    first = fin(ctx)
    record = read_record(ctx, first.record_name)
    record["finalization"]["pdf_bytes"] += 1
    (ctx.reviewer / first.record_name).write_text(json.dumps(record), encoding="utf-8")
    snapshot = (files(ctx.reports), files(ctx.reviewer))
    result = fin(ctx)
    assert (result.outcome, result.category) == (rf.OUTCOME_CONFLICT, "record_identity_differs")
    assert (files(ctx.reports), files(ctx.reviewer)) == snapshot


def test_a_record_for_a_missing_pdf_is_not_finalized_until_the_pdf_is_restored(ctx):
    put_draft(ctx, pdf("a"))
    first = fin(ctx)
    (ctx.reports / FINAL).unlink()
    result = fin(ctx)  # no draft and no report
    assert result.outcome == rf.OUTCOME_REJECTED and first.run_id not in finalized_ids(ctx)


def test_more_than_twenty_matching_records_block_a_new_record_before_any_change(ctx):
    put_draft(ctx, pdf("a"))
    first = fin(ctx)
    for i in range(19):
        (ctx.reviewer / f"reviewer.{first.run_id}.{0x1000 + i:016x}.json").write_bytes(b"{}")
    put_draft(ctx, pdf("b"))
    before = (files(ctx.reports), files(ctx.reviewer), rendered(ctx, first.run_id))
    result = fin(ctx, replace_existing=True)
    assert (result.outcome, result.category, result.status_code) == (rf.OUTCOME_CONFLICT, "reader_record_limit",
                                                                    "reader_record_limit")
    assert result.incomplete is False
    assert (files(ctx.reports), files(ctx.reviewer), rendered(ctx, first.run_id)) == before


def test_nineteen_records_still_permit_one_more(ctx):
    put_draft(ctx, pdf("a"))
    first = fin(ctx)
    for i in range(18):
        (ctx.reviewer / f"reviewer.{first.run_id}.{0x1000 + i:016x}.json").write_bytes(b"{}")
    put_draft(ctx, pdf("b"))
    assert fin(ctx, replace_existing=True).outcome == rf.OUTCOME_FINALIZED


def test_an_existing_report_that_is_a_folder_is_a_conflict_before_any_change(ctx):
    put_draft(ctx, pdf("a"))
    (ctx.reports / FINAL).mkdir()
    result = fin(ctx)
    assert (result.outcome, result.category) == (rf.OUTCOME_CONFLICT, "existing_report_not_regular")
    assert runs(ctx) == [] and ctx.draft.exists()


def test_exhausted_superseded_names_stop_before_any_change(ctx):
    old = put_draft(ctx, pdf("old"))
    fin(ctx)
    for n in range(1, 100):
        (ctx.reports / sup(old, n)).write_bytes(old)
    put_draft(ctx, pdf("new"))
    before = (files(ctx.reports), files(ctx.reviewer))
    result = fin(ctx, replace_existing=True)
    assert (result.outcome, result.category, result.status_code) == \
        (rf.OUTCOME_CONFLICT, "superseded_variants_exhausted", "superseded_variants_exhausted")
    assert (files(ctx.reports), files(ctx.reviewer)) == before


def test_a_superseded_name_with_other_content_is_a_conflict_before_any_change(ctx):
    old = put_draft(ctx, pdf("old"))
    fin(ctx)
    (ctx.reports / sup(old)).write_bytes(pdf("squatter"))
    put_draft(ctx, pdf("new"))
    before = (files(ctx.reports), files(ctx.reviewer))
    result = fin(ctx, replace_existing=True)
    assert (result.outcome, result.category) == (rf.OUTCOME_CONFLICT, "superseded_name_conflict")
    assert (files(ctx.reports), files(ctx.reviewer)) == before


def test_output_folders_are_never_created(env):
    c = make_context(env, history=False)
    import shutil
    shutil.rmtree(c.reviewer)  # test setup only
    c.draft.write_bytes(pdf("a"))
    result = fin(c)
    assert (result.outcome, result.category) == (rf.OUTCOME_REJECTED, "output_unavailable")
    assert not c.reviewer.exists()


# --- fault injection: retry after every persistence boundary ------------------------------------------------------------------------------

def test_a_snapshot_failure_changes_nothing_and_retry_completes(ctx):
    data = put_draft(ctx, pdf("a"))
    ctx.faults.arm("write")
    failed = fin(ctx)
    assert (failed.outcome, failed.step, failed.status_code, failed.category) == \
        (rf.OUTCOME_FAILED, "persist", "persist_failed", "snapshot_persist_failed")
    assert failed.run_id is None and files(ctx.reports) == {DRAFT: data} and records(ctx) == []
    assert fin(ctx).outcome == rf.OUTCOME_FINALIZED


def test_failure_after_the_snapshot_keeps_it_and_retry_adds_the_event(ctx):
    data = put_draft(ctx, pdf("a"))
    ctx.faults.arm("add_event")
    failed = fin(ctx)
    assert (failed.step, failed.status_code, failed.incomplete) == ("event", "event_failed", True)
    assert [r[1] for r in runs(ctx)] == [1] and rendered(ctx, failed.run_id) == []
    assert files(ctx.reports) == {DRAFT: data} and failed.run_id not in finalized_ids(ctx)
    done = fin(ctx)
    assert done.outcome == rf.OUTCOME_FINALIZED and done.run_id == failed.run_id
    assert (done.snapshot_created, done.event_recorded) == (False, True) and len(runs(ctx)) == 1


def test_failure_after_the_event_leaves_the_draft_and_retry_promotes_it(ctx):
    data = put_draft(ctx, pdf("a"))
    failed = fin(ctx, ops=FaultOps(fail="rename_noreplace"))
    assert (failed.step, failed.status_code, failed.incomplete) == ("promote", "promote_failed", True)
    assert files(ctx.reports) == {DRAFT: data} and len(rendered(ctx, failed.run_id)) == 1
    assert records(ctx) == [] and failed.run_id not in finalized_ids(ctx)
    done = fin(ctx)
    assert done.outcome == rf.OUTCOME_FINALIZED and (done.snapshot_created, done.event_recorded) == (False, False)
    assert files(ctx.reports) == {FINAL: data} and len(rendered(ctx, done.run_id)) == 1


def test_failure_after_the_old_report_was_preserved_is_reported_and_retry_completes(ctx):
    old = put_draft(ctx, pdf("old"))
    first = fin(ctx)
    new = put_draft(ctx, pdf("new"))
    failed = fin(ctx, replace_existing=True, ops=FaultOps(fail="rename_noreplace", after=1))
    assert (failed.outcome, failed.step, failed.status_code, failed.incomplete) == \
        (rf.OUTCOME_FAILED, "promote", "promote_failed", True)
    assert failed.preserved_report == sup(old)
    assert files(ctx.reports) == {DRAFT: new, sup(old): old}  # no report.pdf; nothing deleted or restored
    assert first.run_id in finalized_ids(ctx)  # the previously finalized report still resolves (superseded name)
    done = fin(ctx)
    assert done.outcome == rf.OUTCOME_FINALIZED and files(ctx.reports) == {FINAL: new, sup(old): old}
    assert first.run_id in finalized_ids(ctx)


def test_failure_after_the_draft_was_promoted_retry_writes_the_record_without_a_draft(ctx):
    data = put_draft(ctx, pdf("a"))
    failed = fin(ctx, ops=FaultOps(fail="create_exclusive"))
    assert (failed.step, failed.status_code, failed.incomplete) == ("record", "record_failed", True)
    assert files(ctx.reports) == {FINAL: data} and records(ctx) == []
    done = fin(ctx)
    assert done.outcome == rf.OUTCOME_FINALIZED and done.pdf_outcome == "already_promoted"
    assert done.record_outcome == "written" and done.run_id in finalized_ids(ctx)
    assert len(runs(ctx)) == 1 and len(rendered(ctx, done.run_id)) == 1


@pytest.mark.parametrize("fault", ["write", "sync", "close"])
def test_reviewer_record_partial_failures_keep_the_partial_and_retry_completes(ctx, fault):
    put_draft(ctx, pdf("a"))
    failed = fin(ctx, ops=FaultOps(fail=fault))
    assert (failed.step, failed.status_code, failed.incomplete) == ("record", "record_failed", True)
    partials = [n for n in files(ctx.reviewer) if ff.is_partial_name(n)]
    assert len(partials) == 1 and records(ctx) == []
    kept = (ctx.reviewer / partials[0]).read_bytes()
    done = fin(ctx)
    assert done.outcome == rf.OUTCOME_FINALIZED
    assert (ctx.reviewer / partials[0]).read_bytes() == kept  # never deleted, adopted or changed
    assert records(ctx) == [done.record_name]


def test_reviewer_record_promotion_failure_keeps_the_complete_partial_and_retry_completes(ctx):
    put_draft(ctx, pdf("a"))
    failed = fin(ctx, ops=FaultOps(fail="rename_noreplace", after=1))  # rename 1 = the PDF, rename 2 = the record
    assert (failed.step, failed.status_code) == ("record", "record_failed") and failed.incomplete
    assert records(ctx) == [] and any(ff.is_partial_name(n) for n in files(ctx.reviewer))
    assert files(ctx.reports) == {FINAL: pdf("a")}
    done = fin(ctx)
    assert done.outcome == rf.OUTCOME_FINALIZED and done.run_id in finalized_ids(ctx)


def test_failure_during_final_reader_verification_is_not_success_and_retry_is_idempotent(ctx, monkeypatch):
    put_draft(ctx, pdf("a"))
    real = ha.open_history
    calls = []

    def failing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:  # the stale check uses report_run's own import; this is the final verification
            raise RuntimeError("C:\\secret\\boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(rf.ha, "open_history", failing)
    failed = fin(ctx)
    assert failed.finalized is False and failed.outcome == rf.OUTCOME_FAILED and failed.incomplete
    assert (failed.step, failed.category, failed.status_code) == ("record", "reader_verification_failed", "record_failed")
    assert "secret" not in repr(failed)
    monkeypatch.setattr(rf.ha, "open_history", real)
    done = fin(ctx)
    assert done.outcome == rf.OUTCOME_ALREADY and done.finalized
    assert len(runs(ctx)) == 1 and len(records(ctx)) == 1


def test_a_failed_verification_when_the_event_is_removed_from_the_picture_is_not_success(ctx, monkeypatch):
    put_draft(ctx, pdf("a"))
    real = ha.open_history

    def blind(*args, **kwargs):
        access = real(*args, **kwargs)
        return ha.HistoryAccess(access.probe, SimpleNamespace(list_runs=lambda *a, **k: [], close=access.close,
                                                              events=access.reader.events))

    monkeypatch.setattr(rf.ha, "open_history", blind)
    result = fin(ctx)
    assert result.finalized is False and result.category == "reader_verification_failed"


def test_the_store_error_text_is_never_exposed(ctx):
    put_draft(ctx, pdf("a"))
    ctx.faults.arm("write", st.SnapshotStoreError("C:\\Users\\someone\\secret.sqlite password=hunter2"))
    result = fin(ctx)
    text = repr(result)
    assert "hunter2" not in text and "secret" not in text and "Users" not in text


def test_an_unexpected_error_becomes_a_safe_result(ctx):
    put_draft(ctx, pdf("a"))
    ctx.faults.arm("add_event", ValueError("token=ya29.abcdefghijk C:\\x"))
    result = fin(ctx)
    assert result.outcome == rf.OUTCOME_FAILED and "ya29" not in repr(result)
    names = attempt_files(ctx)
    assert len(names) == 1
    assert json.loads((ctx.attempts / names[0]).read_text())["exception_class"] == "ValueError"


# --- attempt records ---------------------------------------------------------------------------------------------------------------------

def test_a_failure_writes_one_valid_failure_only_attempt_record(ctx):
    put_draft(ctx, pdf("a"))
    ctx.faults.arm("add_event")
    result = fin(ctx)
    assert result.attempt_log == rf.ATTEMPT_WRITTEN
    names = attempt_files(ctx)
    assert len(names) == 1 and names[0].endswith(".event.attempt.json")
    record = json.loads((ctx.attempts / names[0]).read_text(encoding="utf-8"))
    assert rs.validate_attempt_record(record) and record["finalized"] is False
    assert (record["step"], record["status_code"]) == ("event", "event_failed")
    assert record["run_id"] == result.run_id and record["pdf_sha256"] == sha(pdf("a"))
    assert record["exception_class"] == "SnapshotStoreError"
    assert "secret" not in json.dumps(record) and "\\" not in json.dumps(record)


def test_a_conflict_is_recorded_with_the_approved_status_code(ctx):
    put_draft(ctx, pdf("a"))
    fin(ctx)
    put_draft(ctx, pdf("b"))
    result = fin(ctx)
    assert result.attempt_log == rf.ATTEMPT_WRITTEN
    record = json.loads((ctx.attempts / attempt_files(ctx)[0]).read_text(encoding="utf-8"))
    assert (record["step"], record["status_code"]) == ("promote", "existing_report_conflict")


def test_a_successful_finalization_writes_no_attempt_record(ctx):
    put_draft(ctx, pdf("a"))
    assert fin(ctx).attempt_log == rf.ATTEMPT_NOT_NEEDED and attempt_files(ctx) == []


def test_a_missing_attempts_folder_is_reported_and_not_created(ctx):
    import shutil
    shutil.rmtree(ctx.attempts)  # test setup only
    put_draft(ctx, pdf("a"))
    ctx.faults.arm("add_event")
    result = fin(ctx)
    assert result.attempt_log == rf.ATTEMPT_NO_DIRECTORY and result.category == "event_failed"
    assert not ctx.attempts.exists()


def test_a_missing_attempts_folder_can_be_made_a_preflight_requirement(ctx):
    import shutil
    shutil.rmtree(ctx.attempts)
    put_draft(ctx, pdf("a"))
    result = fin(ctx, require_attempt_directory=True)
    assert (result.outcome, result.category) == (rf.OUTCOME_REJECTED, "output_unavailable")
    assert runs(ctx) == []


def test_an_attempt_log_failure_never_hides_the_original_failure(ctx):
    put_draft(ctx, pdf("a"))
    ctx.faults.arm("add_event")
    result = fin(ctx, attempt_ops=FaultOps(fail="write"))
    assert (result.outcome, result.category, result.status_code) == (rf.OUTCOME_FAILED, "event_failed", "event_failed")
    assert result.attempt_log == rf.ATTEMPT_FAILED and result.incomplete


def test_stops_without_an_approved_code_write_no_attempt_record(ctx):
    put_draft(ctx, pdf("a"))
    result = fin(ctx, bundle=__import__("dataclasses").replace(
        ctx.bundle, client_content=__import__("dataclasses").replace(
            ctx.bundle.client_content, periods=trr.ti.P_A)))  # a bundle whose parts disagree
    assert (result.outcome, result.category, result.status_code) == (rf.OUTCOME_REJECTED, "bundle_inconsistent", None)
    assert result.attempt_log == rf.ATTEMPT_NOT_RECORDED and attempt_files(ctx) == []
    assert ctx.draft.exists()


# --- privacy, purity and scope ---------------------------------------------------------------------------------------------------------

def test_results_hold_no_paths_secrets_or_system_text(ctx):
    put_draft(ctx, pdf("a"))
    results = [fin(ctx, ops=FaultOps(fail="rename_noreplace")), fin(ctx)]
    put_draft(ctx, pdf("b"))
    results.append(fin(ctx))
    for result in results:
        text = repr(result)
        for bad in (str(ctx.env.tmp), "\\\\", "Errno", "Traceback", "injected", "password", "token"):
            assert bad not in text
        assert result.outcome in rf.OUTCOMES and result.attempt_log in rf.ATTEMPT_OUTCOMES
        assert result.category is None or result.category in rf.CATEGORIES


def test_the_client_content_hash_excludes_reviewer_only_data(ctx):
    from seo_report.client_content_hash import client_content_sha256
    put_draft(ctx, pdf("a"))
    changed = copy.deepcopy(ctx.bundle)
    import dataclasses
    reviewer = dataclasses.replace(changed.reviewer_report, validation_failures=(("x", "y"),), excluded_unfinalized=())
    changed = dataclasses.replace(changed, reviewer_report=reviewer)
    assert client_content_sha256(changed.client_content) == client_content_sha256(ctx.bundle.client_content)


def test_inputs_are_not_mutated(ctx):
    put_draft(ctx, pdf("a"))
    bundle, composed, ann = copy.deepcopy(ctx.bundle), copy.deepcopy(ctx.composed), copy.deepcopy(ctx.ann)
    fin(ctx)
    assert (ctx.bundle, ctx.composed, ctx.ann) == (bundle, composed, ann)


def test_argument_types_are_checked(ctx):
    with pytest.raises(TypeError):
        fin(ctx, bundle=object())
    with pytest.raises(TypeError):
        fin(ctx, replace_existing="yes")
    assert not ctx.draft.exists()


def test_nothing_is_written_outside_the_period_folders_and_database(ctx):
    put_draft(ctx, pdf("a"))
    before = {p for p in ctx.env.tmp.rglob("*")}
    result = fin(ctx)
    created = {p for p in ctx.env.tmp.rglob("*")} - before
    allowed = {ctx.reports, ctx.reviewer, ctx.attempts}
    for path in created:
        assert any(folder in path.parents for folder in allowed) or path.parent == ctx.db.parent or path == ctx.db \
            or path.name.startswith(ctx.db.name), path
    assert result.outcome == rf.OUTCOME_FINALIZED


def test_the_module_has_no_forbidden_calls():
    source = (ROOT / "src" / "seo_report" / "report_finalize.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("os.replace", ".replace(", "unlink", "remove(", "rmtree", "shutil", "mkdir", "makedirs",
                      "write_text", "write_bytes", "requests", "urllib", "googleapiclient", "google.", "subprocess",
                      "git "):
        assert forbidden not in code, forbidden
