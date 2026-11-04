import copy
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_history_access as tha
import test_history_integration as ti
from test_finalize_files import FaultOps
from test_history_access import env  # noqa: F401  (temporary history and output folders)
from test_report_finalize import (DRAFT, FINAL, attempt_files, ctx, dup, files, fin, finalized_ids,  # noqa: F401
                                  make_context, pdf, put_draft, read_record, records, rendered, runs, sha, sup)
from seo_report import attempt_log as al
from seo_report import finalization_audit as fa
from seo_report import finalize_files as ff
from seo_report import history_access as ha
from seo_report import output_layout as layout
from seo_report import report_finalize as rf
from seo_report.snapshots import store as st

TS = "2026-10-30T12:00:00+00:00"


def audit(c, **kw):
    options = dict(output_root=c.env.out, history_db_path=c.db, client_key=c.bundle.snapshot.client_key,
                   period_start=c.bundle.snapshot.period_start, period_end=c.bundle.snapshot.period_end)
    options.update(kw)
    return fa.audit_period(**options)


def tree(c):
    """Every file and folder under the temporary root, with content hashes: proves an audit changed nothing."""
    state = {}
    for path in sorted(Path(c.env.tmp).rglob("*")):
        state[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
    return state


def cats(report):
    return sorted(f.category for f in report.findings)


def one(report, category):
    found = report.by_category(category)
    assert len(found) == 1, (category, [f.category for f in report.findings])
    return found[0]


def audit_unchanged(c, **kw):
    before = tree(c)
    report = audit(c, **kw)
    assert tree(c) == before
    return report


# --- lifecycle -------------------------------------------------------------------------------------------------------------------------

def test_a_successful_lifecycle_audits_as_finalized_valid(ctx):
    data = put_draft(ctx, pdf("a"))
    result = fin(ctx)
    report = audit_unchanged(ctx)
    assert result.run_id in finalized_ids(ctx)
    assert (report.outcome, report.ok, report.verification_complete) == (fa.OUTCOME_CONSISTENT, True, True)
    assert (report.finalized_valid, report.unresolved, report.stored_runs) == (1, 0, 1)
    finding = one(report, fa.FINALIZED_VALID)
    assert (finding.run_id, finding.revision, finding.name, finding.severity) == \
        (result.run_id, 1, result.record_name, fa.SEVERITY_INFO)
    assert cats(report) == [fa.FINALIZED_VALID] and data
    assert (report.client_key, report.period_start) == (ctx.bundle.snapshot.client_key, ctx.bundle.snapshot.period_start)


def test_a_replacement_lifecycle_keeps_both_reports_resolvable_and_audits_clean(ctx):
    old = put_draft(ctx, pdf("old"))
    first = fin(ctx)
    new = put_draft(ctx, pdf("new"))
    second = fin(ctx, replace_existing=True)
    report = audit_unchanged(ctx)
    assert (report.outcome, report.finalized_valid) == (fa.OUTCOME_CONSISTENT, 2)
    assert one(report, fa.SUPERSEDED_PDF).name == sup(old)
    assert sorted(f.name for f in report.by_category(fa.FINALIZED_VALID)) == sorted([first.record_name,
                                                                                    second.record_name])
    assert files(ctx.reports) == {FINAL: new, sup(old): old}
    assert first.run_id in finalized_ids(ctx) and first.run_id == second.run_id


def test_an_idempotent_retry_leaves_one_snapshot_one_event_one_pdf_and_one_record(ctx):
    put_draft(ctx, pdf("a"))
    first = fin(ctx)
    second = fin(ctx)
    assert second.outcome == rf.OUTCOME_ALREADY
    report = audit_unchanged(ctx)
    assert (report.outcome, report.finalized_valid, report.stored_runs) == (fa.OUTCOME_CONSISTENT, 1, 1)
    assert len(rendered(ctx, first.run_id)) == 1 and len(records(ctx)) == 1 and list(files(ctx.reports)) == [FINAL]


def test_an_empty_period_is_consistent(env):
    c = make_context(env, history=False)
    report = audit_unchanged(c)
    assert (report.outcome, report.findings, report.verification_complete) == (fa.OUTCOME_CONSISTENT, (), True)
    assert not c.db.exists()


def test_a_leftover_draft_is_information_not_a_problem(ctx):
    put_draft(ctx, pdf("a"))
    report = audit_unchanged(ctx)
    assert cats(report) == [fa.UNARCHIVED_PDF] and report.outcome == fa.OUTCOME_CONSISTENT
    assert one(report, fa.UNARCHIVED_PDF).reason == "draft_awaiting_finalization"


# --- interrupted lifecycles --------------------------------------------------------------------------------------------------------------

def test_after_a_snapshot_failure_nothing_is_stored(ctx):
    put_draft(ctx, pdf("a"))
    ctx.faults.arm("write")
    fin(ctx)
    report = audit_unchanged(ctx)
    assert (report.stored_runs, report.finalized_valid, report.outcome) == (0, 0, fa.OUTCOME_CONSISTENT)


def test_after_the_snapshot_without_an_event(ctx):
    put_draft(ctx, pdf("a"))
    ctx.faults.arm("add_event")
    result = fin(ctx)
    report = audit_unchanged(ctx)
    assert report.outcome == fa.OUTCOME_ATTENTION and report.unresolved == 1
    finding = one(report, fa.SNAPSHOT_WITHOUT_FINALIZATION)
    assert (finding.run_id, finding.reason, finding.severity) == (result.run_id, "no_reviewer_record",
                                                                 fa.SEVERITY_ATTENTION)
    assert fa.RENDERED_EVENT_WITHOUT_FINALIZATION not in cats(report)


def test_after_the_event_before_the_pdf_is_promoted(ctx):
    put_draft(ctx, pdf("a"))
    result = fin(ctx, ops=FaultOps(fail="rename_noreplace"))
    report = audit_unchanged(ctx)
    assert sorted(cats(report)) == sorted([fa.RENDERED_EVENT_WITHOUT_FINALIZATION, fa.SNAPSHOT_WITHOUT_FINALIZATION,
                                           fa.UNARCHIVED_PDF, fa.ATTEMPT_VALID])
    assert one(report, fa.RENDERED_EVENT_WITHOUT_FINALIZATION).run_id == result.run_id
    assert report.outcome == fa.OUTCOME_ATTENTION


def test_after_old_report_preservation_the_first_report_is_still_valid(ctx):
    old = put_draft(ctx, pdf("old"))
    first = fin(ctx)
    put_draft(ctx, pdf("new"))
    fin(ctx, replace_existing=True, ops=FaultOps(fail="rename_noreplace", after=1))
    report = audit_unchanged(ctx)
    assert report.finalized_valid == 1 and one(report, fa.FINALIZED_VALID).name == first.record_name
    assert one(report, fa.SUPERSEDED_PDF).name == sup(old) and one(report, fa.UNARCHIVED_PDF)
    assert report.outcome == fa.OUTCOME_CONSISTENT  # the first report resolves through its superseded name


def test_after_draft_promotion_without_a_record(ctx):
    data = put_draft(ctx, pdf("a"))
    fin(ctx, ops=FaultOps(fail="create_exclusive"))
    report = audit_unchanged(ctx)
    assert one(report, fa.PDF_WITHOUT_RECORD).name == FINAL and sha(data)
    assert {fa.SNAPSHOT_WITHOUT_FINALIZATION, fa.RENDERED_EVENT_WITHOUT_FINALIZATION} <= set(cats(report))
    assert report.outcome == fa.OUTCOME_ATTENTION and report.finalized_valid == 0


@pytest.mark.parametrize("fault", ["write", "sync", "close"])
def test_a_reviewer_partial_is_reported_and_left_in_place(ctx, fault):
    put_draft(ctx, pdf("a"))
    fin(ctx, ops=FaultOps(fail=fault))
    partials = sorted(n for n in files(ctx.reviewer) if ff.is_partial_name(n))
    report = audit_unchanged(ctx)
    assert [f.name for f in report.by_category(fa.PARTIAL_FILE)] == partials and report.partial_files == 1
    assert one(report, fa.PDF_WITHOUT_RECORD) and report.outcome == fa.OUTCOME_ATTENTION


def test_a_complete_but_unpromoted_reviewer_partial_is_not_adopted(ctx):
    put_draft(ctx, pdf("a"))
    fin(ctx, ops=FaultOps(fail="rename_noreplace", after=1))
    report = audit_unchanged(ctx)
    assert report.partial_files == 1 and report.finalized_valid == 0 and one(report, fa.PDF_WITHOUT_RECORD)


def test_after_a_failed_final_verification_the_state_audits_valid(ctx, monkeypatch):
    put_draft(ctx, pdf("a"))
    monkeypatch.setattr(rf.ha, "open_history", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    failed = fin(ctx)
    monkeypatch.undo()
    assert failed.finalized is False
    report = audit_unchanged(ctx)
    assert (report.outcome, report.finalized_valid) == (fa.OUTCOME_CONSISTENT, 1)


def test_a_recorded_failure_attempt_is_reported_separately(ctx):
    put_draft(ctx, pdf("a"))
    fin(ctx)
    put_draft(ctx, pdf("b"))
    fin(ctx)  # a conflict: replacement is off
    report = audit_unchanged(ctx)
    attempt = one(report, fa.ATTEMPT_VALID)
    assert attempt.reason == "failure_recorded" and attempt.name.endswith(".promote.attempt.json")
    assert (report.attempt_records, report.attempt_records_valid, report.finalized_valid) == (1, 1, 1)
    assert report.outcome == fa.OUTCOME_CONSISTENT  # a valid attempt record is information only


# --- tampering and corruption ---------------------------------------------------------------------------------------------------------------

def finalized(ctx):
    data = put_draft(ctx, pdf("a"))
    return data, fin(ctx)


def test_changed_pdf_bytes_are_a_hash_mismatch(ctx):
    data, result = finalized(ctx)
    (ctx.reports / FINAL).write_bytes(data + b"%tampered")
    report = audit_unchanged(ctx)
    assert one(report, fa.PDF_HASH_MISMATCH).run_id == result.run_id
    assert report.finalized_valid == 0 and fa.PDF_WITHOUT_RECORD in cats(report)
    assert report.outcome == fa.OUTCOME_ATTENTION


def test_same_size_but_different_content_is_a_hash_mismatch(ctx):
    data, _ = finalized(ctx)
    (ctx.reports / FINAL).write_bytes(bytes([data[0] ^ 1]) + data[1:])
    assert audit_unchanged(ctx).by_category(fa.PDF_HASH_MISMATCH)


def test_a_removed_pdf_leaves_a_record_without_a_pdf(ctx):
    _, result = finalized(ctx)
    (ctx.reports / FINAL).unlink()
    report = audit_unchanged(ctx)
    assert one(report, fa.RECORD_WITHOUT_PDF).name == result.record_name and report.finalized_valid == 0


def test_a_pdf_that_is_a_folder_is_not_accepted(ctx):
    finalized(ctx)
    (ctx.reports / FINAL).unlink()
    (ctx.reports / FINAL).mkdir()
    report = audit_unchanged(ctx)
    assert one(report, fa.PDF_NOT_REGULAR).name == FINAL and fa.RECORD_WITHOUT_PDF in cats(report)


@pytest.mark.parametrize("content,reason", [
    (b"not json", "invalid_json"), (b"\xff\xfe\x00", "invalid_encoding"), (b'{"a": NaN}', "nonstandard_constant"),
    (b'{"a": Infinity}', "nonstandard_constant"), (b"{}", "schema_invalid"), (b"[]", "schema_invalid"),
    (b"", "invalid_json")])
def test_corrupt_reviewer_json_is_finalized_invalid(ctx, content, reason):
    _, result = finalized(ctx)
    (ctx.reviewer / result.record_name).write_bytes(content)
    report = audit_unchanged(ctx)
    finding = one(report, fa.FINALIZED_INVALID)
    assert (finding.reason, finding.name) == (reason, result.record_name)
    assert report.finalized_valid == 0 and report.outcome == fa.OUTCOME_ATTENTION


def test_duplicate_json_keys_are_rejected(ctx):
    _, result = finalized(ctx)
    text = (ctx.reviewer / result.record_name).read_text(encoding="utf-8")
    duplicated = text.replace('"record_type":"finalized_report"',
                              '"record_type":"finalized_report","record_type":"finalized_report"', 1)
    assert duplicated != text
    (ctx.reviewer / result.record_name).write_text(duplicated, encoding="utf-8")
    assert one(audit_unchanged(ctx), fa.FINALIZED_INVALID).reason == "duplicate_key"


def test_a_record_that_is_too_large_is_invalid(ctx, monkeypatch):
    _, result = finalized(ctx)
    monkeypatch.setattr(ha, "MAX_RECORD_BYTES", 10)
    assert one(audit_unchanged(ctx), fa.FINALIZED_INVALID).reason == "too_large"


@pytest.mark.parametrize("path,value,reason", [
    (("snapshot", "revision"), 2, "revision_mismatch"),
    (("snapshot", "content_hash"), "e" * 64, "snapshot_hash_mismatch"),
    (("snapshot", "quality"), "incomplete", "snapshot_hash_mismatch"),
    (("client_key",), "other-client", "client_or_period_mismatch"),
    (("report_period", "start"), "2026-01-01", "client_or_period_mismatch")])
def test_a_changed_reviewer_identity_is_a_mismatch(ctx, path, value, reason):
    _, result = finalized(ctx)
    record = read_record(ctx, result.record_name)
    target = record
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    (ctx.reviewer / result.record_name).write_text(json.dumps(record), encoding="utf-8")
    report = audit_unchanged(ctx)
    finding = one(report, fa.RECORD_IDENTITY_MISMATCH)
    assert finding.reason == reason and report.finalized_valid == 0


def test_a_record_stored_under_the_wrong_name_is_a_mismatch(ctx):
    _, result = finalized(ctx)
    other = "reviewer." + result.run_id + "." + "f" * 16 + ".json"
    (ctx.reviewer / other).write_bytes((ctx.reviewer / result.record_name).read_bytes())
    report = audit_unchanged(ctx)
    assert any(f.name == other and f.reason == "name_mismatch" for f in report.by_category(fa.RECORD_IDENTITY_MISMATCH))
    assert report.finalized_valid == 1 and report.outcome == fa.OUTCOME_ATTENTION


def test_a_record_for_an_unknown_run_is_a_snapshot_mismatch(ctx):
    _, result = finalized(ctx)
    record = read_record(ctx, result.record_name)
    record["snapshot"]["run_id"] = "0198c1a2-7b3c-7d4e-8f5a-999999999999"
    name = layout.reviewer_record_name(record["snapshot"]["run_id"], record["finalization"]["pdf_sha256"])
    (ctx.reviewer / name).write_text(json.dumps(record), encoding="utf-8")
    assert any(f.reason == "snapshot_missing" for f in audit_unchanged(ctx).by_category(fa.RECORD_IDENTITY_MISMATCH))


def test_a_record_for_a_pdf_whose_render_was_never_recorded_is_an_event_mismatch(ctx):
    _, result = finalized(ctx)
    other = pdf("other")
    record = read_record(ctx, result.record_name)
    record["finalization"].update(pdf_sha256=sha(other), pdf_bytes=len(other))
    record["finalization"]["rendered_event"]["detail"] = f"Report rendered (PDF {sha(other)})"
    name = layout.reviewer_record_name(result.run_id, sha(other))
    (ctx.reviewer / name).write_text(json.dumps(record), encoding="utf-8")
    (ctx.reports / sup(other)).write_bytes(other)
    report = audit_unchanged(ctx)
    finding = one(report, fa.EVENT_HASH_MISMATCH)
    assert (finding.name, finding.reason) == (name, "event_for_other_pdf") and report.finalized_valid == 1


def test_a_missing_rendered_event_is_reported(ctx, monkeypatch):
    finalized(ctx)
    monkeypatch.setattr(fa._Audit, "run_events", lambda self, run_id: ())
    report = audit_unchanged(ctx)
    assert one(report, fa.FINALIZED_INVALID).reason == "rendered_event_missing"


def test_a_voided_snapshot_is_reported_and_the_run_is_not_finalized(ctx, env):
    _, result = finalized(ctx)
    env.store.add_event(result.run_id, "voided", "entered by mistake")
    report = audit(ctx)
    assert one(report, fa.VOIDED_SNAPSHOT).run_id == result.run_id
    assert one(report, fa.FINALIZED_INVALID).reason == "run_voided" and report.finalized_valid == 0
    assert fa.SNAPSHOT_WITHOUT_FINALIZATION not in cats(report)


def test_an_invalid_attempt_record_is_reported_without_being_touched(ctx):
    bad = ctx.attempts / "20261030T120000Z.nopdf.persist.attempt.json"
    bad.write_bytes(b'{"half"')
    report = audit_unchanged(ctx)
    finding = one(report, fa.ATTEMPT_INVALID)
    assert (finding.name, finding.reason) == (bad.name, "invalid_json")
    assert report.attempt_records == 1 and report.attempt_records_valid == 0
    assert report.outcome == fa.OUTCOME_ATTENTION and bad.read_bytes() == b'{"half"'


def test_attempt_records_with_duplicate_keys_nan_wrong_identity_or_wrong_name_are_invalid(ctx):
    record = al.build_attempt_record(client_key=ctx.bundle.snapshot.client_key, report_period=(
        ctx.bundle.snapshot.period_start, ctx.bundle.snapshot.period_end), attempted_at=TS, step="persist",
        status_code="persist_failed")
    good = al.attempt_file_name(record)
    text = json.dumps(record, sort_keys=True)
    (ctx.attempts / "20261030T120001Z.nopdf.persist.attempt.json").write_text(
        text.replace('"finalized": false', '"finalized": false, "finalized": false'), encoding="utf-8")
    (ctx.attempts / "20261030T120002Z.nopdf.persist.attempt.json").write_text(
        text.replace('"finalized": false', '"finalized": NaN'), encoding="utf-8")
    other = dict(record, client_key="someone-else")
    (ctx.attempts / "20261030T120000Z.nopdf.persist.attempt.2.json").write_text(json.dumps(other), encoding="utf-8")
    (ctx.attempts / "20261030T120003Z.nopdf.persist.attempt.json").write_text(text, encoding="utf-8")  # name != time
    (ctx.attempts / good).write_text(text, encoding="utf-8")
    report = audit_unchanged(ctx)
    reasons = sorted(f.reason for f in report.by_category(fa.ATTEMPT_INVALID))
    assert reasons == ["client_or_period_mismatch", "duplicate_key", "name_mismatch", "nonstandard_constant"]
    assert one(report, fa.ATTEMPT_VALID).name == good and report.attempt_records == 5


def test_files_that_only_look_like_attempts_are_not_counted(ctx):
    (ctx.attempts / "notes.txt").write_text("x")
    (ctx.attempts / "20261030T120000Z.nopdf.bogus.attempt.json").write_text("{}")
    (ctx.attempts / "20261030T120000Z.nopdf.persist.attempt.1.json").write_text("{}")
    report = audit_unchanged(ctx)
    assert report.attempt_records == 1 and cats(report) == [fa.ATTEMPT_INVALID]  # only the `.1` name matches loosely
    assert one(report, fa.ATTEMPT_INVALID).reason == "bad_collision_number"


def test_partial_files_are_reported_in_each_folder(ctx):
    (ctx.reports / ".12345678.0123456789ab.partial").write_bytes(b"x")
    (ctx.reviewer / ".12345678.0123456789cd.partial").write_bytes(b"x")
    (ctx.attempts / ".12345678.0123456789ef.partial").write_bytes(b"x")
    (ctx.reports / layout.DRAFT_PARTIAL_NAME).write_bytes(b"x")
    report = audit_unchanged(ctx)
    assert report.partial_files == 4 and sorted(f.reason for f in report.by_category(fa.PARTIAL_FILE)) == [
        "partial_in_attempts", "partial_in_reports", "partial_in_reports", "partial_in_reviewer"]
    assert report.outcome == fa.OUTCOME_ATTENTION


def test_superseded_and_duplicate_names_with_foreign_content_are_reported(ctx):
    data, _ = finalized(ctx)
    foreign = pdf("foreign")
    (ctx.reports / sup(data)).write_bytes(foreign)  # a name that claims the PDF's hash prefix, with other content
    (ctx.reports / dup(data)).write_bytes(data)
    report = audit_unchanged(ctx)
    assert one(report, fa.SUPERSEDED_PDF).name == sup(data) and one(report, fa.DUPLICATE_PDF).name == dup(data)
    assert one(report, fa.PDF_WITHOUT_RECORD).name == sup(data)
    assert report.finalized_valid == 1


def test_more_than_twenty_records_for_a_run_are_flagged(ctx):
    _, result = finalized(ctx)
    for i in range(21):
        (ctx.reviewer / f"reviewer.{result.run_id}.{0x2000 + i:016x}.json").write_bytes(b"{}")
    report = audit_unchanged(ctx)
    assert one(report, fa.RECORD_LIMIT_REACHED).run_id == result.run_id


def test_a_damaged_snapshot_fails_its_verification(ctx, env):
    _, result = finalized(ctx)
    import sqlite3
    con = sqlite3.connect(env.db)
    con.execute("DROP TRIGGER runs_no_update")
    con.execute("UPDATE runs SET quality = 'partial' WHERE run_id = ?", (result.run_id,))
    con.commit()
    con.close()
    report = audit(ctx)
    assert report.outcome == fa.OUTCOME_LIMITED and report.finalized_valid == 0  # the schema probe sees the damage
    assert any(f.reason == "history_schema_failure" for f in report.findings)


def test_a_run_that_fails_its_own_verification_is_a_problem(ctx, monkeypatch):
    finalized(ctx)
    monkeypatch.setattr(st.SnapshotReader, "verify_run",
                        lambda self, run_id: st.VerificationReport(False, 1, ("problem",)))
    report = audit(ctx)
    assert one(report, fa.SNAPSHOT_VERIFICATION_FAILED).reason == "run_failed_verification"
    assert report.outcome == fa.OUTCOME_ATTENTION and report.finalized_valid == 0


def test_the_optional_database_check_runs_only_when_requested(ctx):
    finalized(ctx)
    assert audit(ctx).database_check == "not_requested"
    deep = audit(ctx, deep_database_check=True)
    assert (deep.database_check, deep.outcome) == ("ok", fa.OUTCOME_CONSISTENT)


# --- verification limits ---------------------------------------------------------------------------------------------------------------------

def test_a_missing_database_with_reports_on_disk_is_limited_not_consistent(env):
    c = make_context(env, history=False)
    data = put_draft(c, pdf("a"))
    (c.reports / FINAL).write_bytes(data)
    report = audit_unchanged(c)
    assert report.outcome == fa.OUTCOME_ATTENTION  # a problem was found even though the audit is also limited
    assert fa.PDF_WITHOUT_RECORD in cats(report) and fa.VERIFICATION_UNAVAILABLE in cats(report)
    assert report.verification_complete is False


def test_records_cannot_be_checked_without_the_database(ctx, env):
    _, result = finalized(ctx)
    report = audit_unchanged(ctx, history_db_path=env.tmp / "nowhere.sqlite")
    assert report.outcome == fa.OUTCOME_LIMITED and report.finalized_valid == 0
    assert {f.reason for f in report.by_category(fa.VERIFICATION_UNAVAILABLE)} == {"history_database_missing"}
    assert not report.verification_complete and result


def test_a_sqlite_failure_is_a_limited_audit_never_a_clean_one(ctx, monkeypatch):
    finalized(ctx)

    def broken(*args, **kwargs):
        raise st.SnapshotStoreError("C:\\secret\\history.sqlite password=hunter2")

    monkeypatch.setattr(fa.st, "SnapshotReader", broken)
    report = audit(ctx)
    assert report.outcome == fa.OUTCOME_LIMITED and report.verification_complete is False
    assert report.finalized_valid == 0 and fa.VERIFICATION_UNAVAILABLE in cats(report)
    text = repr(report)
    assert "hunter2" not in text and "secret" not in text


def test_a_sqlite_failure_with_nothing_else_present_is_still_not_clean(env, monkeypatch):
    c = make_context(env)
    monkeypatch.setattr(fa.st, "SnapshotReader", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    report = audit(c)
    assert report.outcome == fa.OUTCOME_LIMITED and fa.VERIFICATION_UNAVAILABLE in cats(report)


def test_a_snapshot_verification_failure_is_limited(ctx, monkeypatch):
    finalized(ctx)
    real = st.SnapshotReader.verify_run

    def failing(self, run_id):
        raise st.SnapshotStoreError("hidden")

    monkeypatch.setattr(st.SnapshotReader, "verify_run", failing)
    report = audit(ctx)
    assert report.outcome == fa.OUTCOME_LIMITED and report.finalized_valid == 0
    assert any(f.reason == "snapshot_verification_unavailable" for f in report.findings)
    monkeypatch.setattr(st.SnapshotReader, "verify_run", real)


def test_a_reviewer_json_read_failure_is_limited(ctx):
    finalized(ctx)

    class Ops(ff.FileOps):
        def open_read(self, path):
            if Path(path).suffix == ".json":
                raise OSError(13, "C:\\private\\x denied")
            return super().open_read(path)

    report = audit(ctx, ops=Ops())
    assert report.outcome == fa.OUTCOME_LIMITED and any(f.reason == "record_unreadable" for f in report.findings)
    assert "private" not in repr(report)


def test_a_parse_crash_is_a_limit_not_an_exception(ctx, monkeypatch):
    finalized(ctx)
    monkeypatch.setattr(fa, "_parse", lambda data: (_ for _ in ()).throw(MemoryError("secret")))
    report = audit(ctx)
    assert report.outcome == fa.OUTCOME_LIMITED and any(f.reason == "scan_failed" for f in report.findings)
    assert "secret" not in repr(report)


def test_a_pdf_hashing_failure_is_limited(ctx):
    finalized(ctx)

    class Ops(ff.FileOps):
        def open_read(self, path):
            if Path(path).suffix == ".pdf":
                raise OSError(13, "denied")
            return super().open_read(path)

    report = audit(ctx, ops=Ops())
    assert report.outcome == fa.OUTCOME_LIMITED and any(f.reason == "pdf_unreadable" for f in report.findings)
    assert report.finalized_valid == 0


def test_a_directory_scan_failure_is_limited(ctx, monkeypatch):
    finalized(ctx)

    def failing(path):
        raise PermissionError(13, "C:\\x")

    monkeypatch.setattr(fa, "_scandir", failing)
    report = audit(ctx)
    assert report.outcome == fa.OUTCOME_LIMITED
    assert {f.reason for f in report.by_category(fa.VERIFICATION_UNAVAILABLE)} >= {"directory_unreadable"}
    assert report.finalized_valid == 0 and report.stored_runs == 1


def test_an_attempt_record_read_failure_is_limited(ctx):
    put_draft(ctx, pdf("a"))
    fin(ctx)
    put_draft(ctx, pdf("b"))
    fin(ctx)

    class Ops(ff.FileOps):
        def open_read(self, path):
            if ".attempt" in Path(path).name:
                raise OSError(13, "denied")
            return super().open_read(path)

    report = audit(ctx, ops=Ops())
    assert any(f.reason == "attempt_unreadable" for f in report.findings) and report.outcome == fa.OUTCOME_LIMITED


def test_a_failure_never_turns_into_an_empty_clean_result(ctx, monkeypatch):
    finalized(ctx)
    monkeypatch.setattr(fa, "_list_folder", lambda folder: ("unavailable", "directory_unreadable"))
    report = audit(ctx)
    assert report.findings and report.outcome != fa.OUTCOME_CONSISTENT


def test_an_unusable_location_gives_an_unavailable_report(env):
    report = fa.audit_period(output_root=env.out, history_db_path=env.db, client_key="Bad Key!",
                             period_start="2026-01-01", period_end="2026-01-31")
    assert report.outcome == fa.OUTCOME_UNAVAILABLE and report.client_key is None
    assert report.findings[0].reason == "output_location_invalid" and not report.verification_complete
    report = fa.audit_period(output_root=env.out, history_db_path=env.db, client_key="example-co",
                             period_start="not a date", period_end="2026-01-31")
    assert report.outcome == fa.OUTCOME_UNAVAILABLE
    assert not env.out.exists()


# --- read-only and privacy guarantees ---------------------------------------------------------------------------------------------------------

def test_the_audit_works_on_read_only_files_and_changes_nothing(ctx, env):
    finalized(ctx)
    put_draft(ctx, pdf("b"))
    (ctx.reviewer / ".12345678.0123456789ab.partial").write_bytes(b"x")
    before = tree(ctx)
    paths = [env.db, *ctx.reports.iterdir(), *ctx.reviewer.iterdir()]
    for path in paths:
        os.chmod(path, stat.S_IREAD)
    try:
        report = audit(ctx)
    finally:
        for path in paths:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    assert report.finalized_valid == 1 and tree(ctx) == before


def test_the_audit_creates_nothing_and_adds_no_events_or_revisions(ctx):
    finalized(ctx)
    before_state = (tree(ctx), runs(ctx))
    reader = st.SnapshotReader(ctx.db)
    events_before = {i.run_id: reader.events(i.run_id) for i in reader.list_runs(include_voided=True)}
    reader.close()
    for _ in range(3):
        audit(ctx, deep_database_check=True)
    reader = st.SnapshotReader(ctx.db)
    events_after = {i.run_id: reader.events(i.run_id) for i in reader.list_runs(include_voided=True)}
    reader.close()
    assert (tree(ctx), runs(ctx)) == before_state and events_before == events_after


def test_a_missing_output_root_is_not_created(env):
    report = fa.audit_period(output_root=env.out, history_db_path=env.tmp / "none.sqlite", client_key="example-co",
                             period_start="2026-09-29", period_end="2026-10-26")
    assert report.outcome == fa.OUTCOME_CONSISTENT and not env.out.exists() and not (env.tmp / "none.sqlite").exists()


def test_the_database_is_opened_read_only(ctx, monkeypatch):
    finalized(ctx)
    modes = []
    real = st._connect

    def spy(path, *, readonly, busy_timeout):
        modes.append(readonly)
        return real(path, readonly=readonly, busy_timeout=busy_timeout)

    monkeypatch.setattr(st, "_connect", spy)
    audit(ctx, deep_database_check=True)
    assert modes and all(modes)


def test_reports_hold_no_paths_secrets_or_system_text(ctx):
    finalized(ctx)
    (ctx.reviewer / ".12345678.0123456789ab.partial").write_bytes(b"x")
    (ctx.attempts / "20261030T120000Z.nopdf.persist.attempt.json").write_bytes(b"password=hunter2 C:\\x")
    report = audit(ctx)
    text = repr(report)
    for bad in (str(ctx.env.tmp), "\\\\", "Errno", "Traceback", "hunter2", "@", "password"):
        assert bad not in text
    for finding in report.findings:
        assert finding.category in fa.CATEGORIES and re.fullmatch(r"[a-z_0-9]+", finding.reason)
        assert finding.severity in (fa.SEVERITY_INFO, fa.SEVERITY_ATTENTION, fa.SEVERITY_LIMIT)
    assert report.outcome in fa.OUTCOMES


def test_the_report_is_frozen_and_holds_no_record_content(ctx):
    import dataclasses
    finalized(ctx)
    report = audit(ctx)
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.outcome = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.findings[0].category = "x"
    assert "notes" not in repr(report) and "payload" not in repr(report)


def test_an_audit_does_not_infer_success_from_the_absence_of_attempts(ctx):
    put_draft(ctx, pdf("a"))
    ctx.faults.arm("add_event")
    fin(ctx, attempt_ops=FaultOps(fail="write"))  # the failure could not even be logged
    report = audit(ctx)
    # the failed attempt write left an incomplete file: reported as invalid, never repaired or counted as success
    assert (report.attempt_records, report.attempt_records_valid) == (1, 0)
    assert one(report, fa.ATTEMPT_INVALID).reason == "invalid_json"
    assert report.outcome == fa.OUTCOME_ATTENTION and report.finalized_valid == 0


def test_the_module_has_no_write_calls():
    source = (ROOT / "src" / "seo_report" / "finalization_audit.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("os.replace", ".replace(", "unlink", "remove(", "rmtree", "shutil", "mkdir", "makedirs",
                      "write_text", "write_bytes", "write_exclusive", "rename", "add_event", "SnapshotStore",
                      ".write(", "open(", "requests", "urllib", "google", "subprocess", "environ["):
        assert forbidden not in code, forbidden
