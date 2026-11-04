"""R7.5: the contract of render-state discovery, pinned against real finalization states.

`render_state.scan_finalized_render_hashes` validates REVIEWER RECORDS only: file name, regular file, size, strict JSON,
schema, and agreement of the record with its own name. It does not look at the PDF, the snapshot, the rendered event or
the sidecar. The full verification of a finalization belongs to the finalized-only reader and to the audit, and the
sidecar and PDF identity checks to `finalize_report`. These tests make that division explicit and fail if the helper's
contract grows or shrinks. Synthetic data in temporary folders only.
"""
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import finalization_audit as fa
from seo_report import finalize_files as ff
from seo_report import output_layout as layout
from seo_report import render_artifacts as ra
from seo_report import render_input_hash as rh
from seo_report import render_state as rstate
from seo_report import report_finalize as rf
from seo_report.reviewer_record import to_canonical_json
from test_finalization_audit import audit, tree
from test_history_access import env  # noqa: F401
from test_lifecycle_e2e import reader_view, run_lifecycle
from test_report_finalize import ctx, files, fin, pdf, put_draft, read_record, records, sha, sup  # noqa: F401
from test_report_finalize_v2 import RENDERER, make_input, sidecar_path, stop

FINAL = "report.pdf"


def digest_of(life):
    return rh.render_input_sha256(life.value)


def craft(c, record_name, data, *, content_hash=None):
    """A second schema-valid v2 record for the same run and render identity, naming the PDF `data`."""
    record = copy.deepcopy(read_record(c, record_name))
    record["finalization"]["pdf_sha256"] = sha(data)
    record["finalization"]["pdf_bytes"] = len(data)
    record["finalization"]["rendered_event"]["detail"] = f"Report rendered (PDF {sha(data)})"
    if content_hash:
        record["snapshot"]["content_hash"] = content_hash
    name = layout.reviewer_record_name(record["snapshot"]["run_id"], sha(data))
    (c.reviewer / name).write_bytes((to_canonical_json(record) + "\n").encode("utf-8"))
    (c.reports / sup(data)).write_bytes(data)
    return name


def findings(report, category):
    return {(f.name, f.reason) for f in report.by_category(category)}


# --- what discovery does accept ---------------------------------------------------------------------------------------------------------

def test_a_valid_version_2_record_gives_its_hash_and_the_audit_agrees(ctx):
    life = run_lifecycle(ctx)
    scan = rstate.scan_finalized_render_hashes(ctx.reviewer)
    assert (scan.hashes, scan.v2_records, scan.v1_records, scan.ignored, scan.complete) == (
        {digest_of(life)}, 1, 0, (), True)
    report = audit(ctx)
    assert (report.outcome, report.finalized_valid) == (fa.OUTCOME_CONSISTENT, 1) and reader_view(ctx)[0] == [life.result.run_id]


def test_a_version_1_record_is_counted_and_gives_no_hash(ctx):
    put_draft(ctx, pdf("legacy"))
    fin(ctx)
    scan = rstate.scan_finalized_render_hashes(ctx.reviewer)
    assert (scan.hashes, scan.v1_records, scan.v2_records) == (frozenset(), 1, 0)
    assert audit(ctx).legacy_records == 1


def test_a_mixed_directory_returns_only_the_version_2_hashes(ctx):
    put_draft(ctx, pdf("legacy"))
    legacy = fin(ctx)
    life = run_lifecycle(ctx, replace_existing=True)
    scan = rstate.scan_finalized_render_hashes(ctx.reviewer)
    assert (scan.hashes, scan.v1_records, scan.v2_records) == ({digest_of(life)}, 1, 1)
    assert set(records(ctx)) == {legacy.record_name, life.result.record_name}
    assert audit(ctx).finalized_valid == 2


# --- record-level validity is NOT full verification: the audit and the reader catch what discovery does not ----------------------------

def test_a_missing_pdf_does_not_change_discovery_but_the_audit_and_reader_report_it(ctx):
    life = run_lifecycle(ctx)
    before = rstate.scan_finalized_render_hashes(ctx.reviewer)
    (ctx.reports / FINAL).unlink()
    assert rstate.scan_finalized_render_hashes(ctx.reviewer) == before  # unchanged: discovery never looks at the PDF
    report = audit(ctx)
    assert findings(report, fa.RECORD_WITHOUT_PDF) == {(life.result.record_name, "no_recognized_pdf")}
    assert report.finalized_valid == 0 and report.outcome == fa.OUTCOME_ATTENTION
    listed, excluded = reader_view(ctx)
    assert listed == [] and excluded == {life.result.run_id: "final_pdf_missing"}


def test_an_event_that_names_another_pdf_does_not_change_discovery_but_the_audit_reports_it(ctx):
    life = run_lifecycle(ctx)
    extra = craft(ctx, life.result.record_name, pdf("a different report"))  # valid record; no event names its PDF
    scan = rstate.scan_finalized_render_hashes(ctx.reviewer)
    assert scan.hashes == {digest_of(life)} and scan.v2_records == 2 and scan.ignored == ()
    report = audit(ctx)
    assert findings(report, fa.EVENT_HASH_MISMATCH) == {(extra, "event_for_other_pdf")}
    assert findings(report, fa.FINALIZED_VALID) == {(life.result.record_name, "all_checks_passed")}


def test_a_record_that_disagrees_with_the_stored_snapshot_does_not_change_discovery_but_the_audit_reports_it(ctx):
    life = run_lifecycle(ctx)
    extra = craft(ctx, life.result.record_name, pdf("another"), content_hash="e" * 64)
    scan = rstate.scan_finalized_render_hashes(ctx.reviewer)
    assert scan.hashes == {digest_of(life)} and scan.v2_records == 2
    report = audit(ctx)
    assert findings(report, fa.RECORD_IDENTITY_MISMATCH) == {(extra, "snapshot_hash_mismatch")}
    assert report.finalized_valid == 1


def test_a_run_without_its_rendered_event_does_not_change_discovery(ctx):
    ctx.faults.arm("add_event")
    value = make_input(ctx)
    from test_report_finalize_v2 import produce
    drafted = produce(ctx, value)
    assert not fin(ctx, render_input=drafted.render_input).finalized
    # no record exists, so there is nothing to discover; the audit and reader both say "not finalized"
    assert rstate.scan_finalized_render_hashes(ctx.reviewer).hashes == frozenset()
    assert audit(ctx).finalized_valid == 0 and reader_view(ctx)[0] == []


# --- malformed, duplicate and unreadable records --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("content", [b"{broken", b"", b'{"a":1,"a":2}'])
def test_a_malformed_record_is_ignored_by_discovery_and_invalid_for_the_audit(ctx, content):
    life = run_lifecycle(ctx)
    bad = layout.reviewer_record_name(life.result.run_id, "c" * 64)
    (ctx.reviewer / bad).write_bytes(content)
    scan = rstate.scan_finalized_render_hashes(ctx.reviewer)
    assert scan.hashes == {digest_of(life)} and scan.ignored == ((bad, "invalid_record"),) and scan.complete
    report = audit(ctx)
    assert any(f.name == bad for f in report.by_category(fa.FINALIZED_INVALID))
    assert report.finalized_valid == 1


def test_duplicate_render_identities_are_handled_deterministically(ctx):
    life = run_lifecycle(ctx)
    craft(ctx, life.result.record_name, pdf("copy one"))
    craft(ctx, life.result.record_name, pdf("copy two"))
    first = rstate.scan_finalized_render_hashes(ctx.reviewer)
    second = rstate.scan_finalized_render_hashes(ctx.reviewer)
    assert first == second and first.hashes == {digest_of(life)} and first.v2_records == 3
    assert list(first.ignored) == sorted(first.ignored)


def test_an_unreadable_record_makes_the_scan_incomplete_and_the_audit_unavailable(ctx):
    life = run_lifecycle(ctx)

    class Unreadable(ff.FileOps):
        def open_read(self, path):
            if Path(path).name == life.result.record_name:
                raise OSError(13, "injected")
            return super().open_read(path)
    scan = rstate.scan_finalized_render_hashes(ctx.reviewer, ops=Unreadable())
    assert scan.hashes == frozenset() and scan.ignored == ((life.result.record_name, "unreadable"),) and not scan.complete
    with pytest.raises(rstate.RenderStateError) as info:
        rstate.finalized_render_hashes(ctx.reviewer, ops=Unreadable(), require_complete=True)
    assert info.value.code == "scan_incomplete"
    report = audit(ctx, ops=Unreadable())
    assert findings(report, fa.VERIFICATION_UNAVAILABLE) >= {(life.result.record_name, "record_unreadable")}
    assert report.outcome == fa.OUTCOME_LIMITED and report.finalized_valid == 0


# --- the sidecar belongs to finalization and the audit, not to discovery --------------------------------------------------------------------------

def test_a_missing_sidecar_is_found_by_the_audit_and_finalization_not_by_discovery(ctx):
    life = run_lifecycle(ctx)
    before = rstate.scan_finalized_render_hashes(ctx.reviewer)
    sidecar_path(ctx, life.value).unlink()
    assert rstate.scan_finalized_render_hashes(ctx.reviewer) == before
    report = audit(ctx)
    assert findings(report, fa.RENDER_SIDECAR_MISSING) == {(ra.sidecar_name(digest_of(life)), "no_sidecar_for_record")}
    assert report.finalized_valid == 0
    again = fin(ctx, render_input=life.drafted.render_input)  # finalization refuses to proceed without the sidecar
    stop(again, "render_sidecar_missing")
    assert files(ctx.reports).get(ra.sidecar_name(digest_of(life))) is None  # not recreated


def test_an_inconsistent_sidecar_is_found_by_the_audit_and_finalization_not_by_discovery(ctx):
    life = run_lifecycle(ctx)
    before = rstate.scan_finalized_render_hashes(ctx.reviewer)
    other = make_input(ctx, day=29)
    data = files(ctx.reports)[FINAL]
    sidecar_path(ctx, life.value).write_bytes(ra.build_sidecar_bytes(other, renderer=RENDERER, pdf_sha256=sha(data),
                                                                     pdf_bytes=len(data)))
    assert rstate.scan_finalized_render_hashes(ctx.reviewer) == before
    report = audit(ctx)
    assert findings(report, fa.RENDER_INPUT_HASH_MISMATCH) == {
        (ra.sidecar_name(digest_of(life)), "sidecar_hash_differs_from_record")}
    stop(fin(ctx, render_input=life.drafted.render_input), "render_identity_mismatch")


def test_discovery_depends_on_the_reviewer_folder_alone(ctx):
    life = run_lifecycle(ctx)
    before = rstate.scan_finalized_render_hashes(ctx.reviewer)
    (ctx.reports / FINAL).unlink()
    sidecar_path(ctx, life.value).unlink()
    assert rstate.scan_finalized_render_hashes(ctx.reviewer) == before
    source = Path(rstate.__file__).read_text(encoding="utf-8")
    scan_body = source.split("def scan_finalized_render_hashes")[1].split("def finalized_render_hashes")[0]
    for word in ("SnapshotReader", "SnapshotStore", "sha256_file", "report.pdf", "read_sidecar", "history_access",
                 "FinalizedOnlyReader", "events("):
        assert word not in scan_body


def test_discovery_changes_nothing(ctx):
    run_lifecycle(ctx)
    craft(ctx, records(ctx)[0], pdf("another"))
    (ctx.reviewer / layout.reviewer_record_name(read_record(ctx, records(ctx)[0])["snapshot"]["run_id"], "d" * 64)
     ).write_bytes(b"{broken")
    before = tree(ctx)
    rstate.scan_finalized_render_hashes(ctx.reviewer)
    rstate.finalized_render_hashes(ctx.reviewer)
    assert tree(ctx) == before
