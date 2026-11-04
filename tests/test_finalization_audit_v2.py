"""R7.3b: the audit verifies version 2 reviewer records against their render-input sidecars (read-only).

Synthetic lifecycles in temporary folders: a normal (version 1) finalization is run, and for the version 2 cases the
synthetic record file in the temporary tree is rewritten as a version 2 record next to a sidecar built by
`render_artifacts`. No real data, no network, no production folders.
"""
import dataclasses
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import seo_report
from seo_report import finalization_audit as fa
from seo_report import finalize_files as ff
from seo_report import record_schema as rs
from seo_report import render_artifacts as ra
from seo_report import render_input_hash as rh
from seo_report.reviewer_record import to_canonical_json
from test_finalization_audit import audit, audit_unchanged, cats, one, tree
from test_history_access import env  # noqa: F401
from test_render_input import sample
from test_report_finalize import ctx, files, fin, pdf, put_draft, records, sha, sup  # noqa: F401

RENDERER = {"browser": "156.0.8078.4", "playwright": "1.64.1", "python": "3.14.0", "system": "Windows"}
FORMAT = {"name": "jasper.render-input", "version": 2}
OLD_CATEGORIES = (
    "finalized_valid", "finalized_invalid", "snapshot_without_finalization", "rendered_event_without_finalization",
    "pdf_without_reviewer_record", "reviewer_record_without_pdf", "reviewer_record_identity_mismatch",
    "pdf_hash_mismatch", "rendered_event_hash_mismatch", "attempt_record_valid", "attempt_record_invalid",
    "partial_file_present", "superseded_pdf_present", "duplicate_pdf_present", "unarchived_pdf_present",
    "voided_snapshot", "verification_unavailable", "snapshot_verification_failed", "pdf_not_regular_file",
    "reader_record_limit_reached", "database_integrity_failed")


def render_input(day=5):
    return dataclasses.replace(sample(), generation_date=sample().generation_date.replace(day=day))


def sidecar_for(value, data, *, pdf_sha=None, pdf_bytes=None):
    return ra.build_sidecar_bytes(value, renderer=RENDERER, pdf_sha256=pdf_sha or sha(data),
                                  pdf_bytes=len(data) if pdf_bytes is None else pdf_bytes)


def edited(data, change):
    document = json.loads(data)
    change(document)
    return (json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def make_v2(c, data, *, sidecar="valid", value=None, record_change=None, replace=False):
    """Finalize `data` (a version 1 record), then rewrite that synthetic record as version 2 and add its sidecar."""
    put_draft(c, data)
    result = fin(c, replace_existing=replace)
    value = value or render_input()
    digest = rh.render_input_sha256(value)
    path = c.reviewer / result.record_name
    record = json.loads(path.read_text(encoding="utf-8"))
    record["schema_version"] = 2
    record["finalization"]["render_input_sha256"] = digest
    record["finalization"]["render_input_format"] = dict(FORMAT)
    if record_change:
        record_change(record)
    path.write_text(to_canonical_json(record) + "\n", encoding="utf-8")
    name = ra.sidecar_name(digest)
    if sidecar == "valid":
        (c.reports / name).write_bytes(sidecar_for(value, data))
    elif sidecar is not None:
        (c.reports / name).write_bytes(sidecar(value, data) if callable(sidecar) else sidecar)
    return result, name


def reasons(report):
    return sorted((f.category, f.reason) for f in report.findings)


# --- version 1 (unchanged) ----------------------------------------------------------------------------------------------------

def test_a_valid_version_1_audit_is_unchanged(ctx):
    put_draft(ctx, pdf("a"))
    result = fin(ctx)
    report = audit_unchanged(ctx)
    assert (report.outcome, report.finalized_valid, report.unresolved) == (fa.OUTCOME_CONSISTENT, 1, 0)
    assert cats(report) == [fa.FINALIZED_VALID]
    finding = one(report, fa.FINALIZED_VALID)
    assert (finding.reason, finding.severity, finding.name) == ("all_checks_passed", fa.SEVERITY_INFO,
                                                               result.record_name)


def test_version_1_records_are_counted_as_legacy_and_carry_their_version(ctx):
    put_draft(ctx, pdf("a"))
    fin(ctx)
    report = audit(ctx)
    assert report.legacy_records == 1
    assert one(report, fa.FINALIZED_VALID).record_version == 1


def test_a_version_1_record_never_gets_a_sidecar_finding(ctx):
    put_draft(ctx, pdf("a"))
    fin(ctx)
    report = audit(ctx)
    assert not any(f.category.startswith("render_") for f in report.findings)
    assert not [n for n in files(ctx.reports) if n.startswith("report.render-input.")]


def test_the_existing_categories_keep_their_names_and_order():
    assert fa.CATEGORIES[:len(OLD_CATEGORIES)] == OLD_CATEGORIES
    assert fa.CATEGORIES[len(OLD_CATEGORIES):] == ("render_sidecar_missing", "render_sidecar_invalid",
                                                    "render_input_hash_mismatch", "render_input_format_invalid")
    assert fa._INFO_CATEGORIES == {fa.FINALIZED_VALID, fa.ATTEMPT_VALID, fa.SUPERSEDED_PDF, fa.DUPLICATE_PDF,
                                   fa.UNARCHIVED_PDF, fa.VOIDED_SNAPSHOT}


def test_an_empty_period_has_no_legacy_records(env):  # noqa: F811
    from test_report_finalize import make_context
    c = make_context(env, history=False)
    report = audit_unchanged(c)
    assert (report.legacy_records, report.findings, report.outcome) == (0, (), fa.OUTCOME_CONSISTENT)


# --- version 2: valid -------------------------------------------------------------------------------------------------------------

def test_a_valid_version_2_record_with_its_sidecar_is_finalized_valid(ctx):
    data = pdf("a")
    result, name = make_v2(ctx, data)
    report = audit_unchanged(ctx)
    assert (report.outcome, report.ok, report.verification_complete) == (fa.OUTCOME_CONSISTENT, True, True)
    assert (report.finalized_valid, report.unresolved, report.legacy_records) == (1, 0, 0)
    assert cats(report) == [fa.FINALIZED_VALID]
    finding = one(report, fa.FINALIZED_VALID)
    assert (finding.record_version, finding.severity, finding.name) == (2, fa.SEVERITY_INFO, result.record_name)
    assert name in files(ctx.reports)  # the sidecar is only looked at; it stays where it was


def test_a_mixed_period_counts_one_legacy_record_and_two_valid_ones(ctx):
    old, new = pdf("old"), pdf("new")
    put_draft(ctx, old)
    first = fin(ctx)
    make_v2(ctx, new, replace=True)
    report = audit_unchanged(ctx)
    assert (report.outcome, report.finalized_valid, report.legacy_records) == (fa.OUTCOME_CONSISTENT, 2, 1)
    versions = {f.name: f.record_version for f in report.by_category(fa.FINALIZED_VALID)}
    assert sorted(versions.values()) == [1, 2] and versions[first.record_name] == 1
    assert one(report, fa.SUPERSEDED_PDF).name == sup(old)


# --- version 2: sidecar problems --------------------------------------------------------------------------------------------------

def test_a_missing_sidecar_is_reported(ctx):
    result, name = make_v2(ctx, pdf("a"), sidecar=None)
    report = audit_unchanged(ctx)
    finding = one(report, fa.RENDER_SIDECAR_MISSING)
    assert (finding.reason, finding.severity, finding.name, finding.record_version) == (
        "no_sidecar_for_record", fa.SEVERITY_ATTENTION, name, 2)
    assert finding.run_id == result.run_id and finding.revision == 1
    assert (report.outcome, report.finalized_valid) == (fa.OUTCOME_ATTENTION, 0) and report.unresolved >= 1
    assert fa.FINALIZED_VALID not in cats(report)
    assert name not in files(ctx.reports)  # no recovery: the sidecar is not recreated


@pytest.mark.parametrize("content,reason", [
    (b"{broken", "sidecar_malformed"), (b"", "sidecar_malformed"), (b"[]", "sidecar_malformed"),
    (b"\xff\xfe\x00", "sidecar_malformed"), (b'{"a":1,"a":2}', "sidecar_malformed")])
def test_a_malformed_sidecar_is_invalid(ctx, content, reason):
    make_v2(ctx, pdf("a"), sidecar=content)
    report = audit_unchanged(ctx)
    finding = one(report, fa.RENDER_SIDECAR_INVALID)
    assert (finding.reason, finding.severity) == (reason, fa.SEVERITY_ATTENTION)
    assert fa.FINALIZED_VALID not in cats(report) and report.outcome == fa.OUTCOME_ATTENTION


def test_a_sidecar_with_a_missing_member_or_unsupported_sidecar_version_is_invalid(ctx):
    make_v2(ctx, pdf("a"), sidecar=lambda v, d: edited(sidecar_for(v, d), lambda doc: doc.pop("pdf")))
    assert one(audit(ctx), fa.RENDER_SIDECAR_INVALID).reason == "sidecar_malformed"


def test_an_unsupported_sidecar_schema_version_is_invalid(ctx):
    make_v2(ctx, pdf("a"), sidecar=lambda v, d: edited(sidecar_for(v, d), lambda doc: doc.update(version=2)))
    assert one(audit(ctx), fa.RENDER_SIDECAR_INVALID).reason == "unsupported_sidecar_version"


def test_a_sidecar_whose_date_or_template_disagrees_with_its_payload_is_invalid(ctx):
    make_v2(ctx, pdf("a"), sidecar=lambda v, d: edited(sidecar_for(v, d),
                                                        lambda doc: doc.update(generation_date="2020-01-01")))
    assert one(audit(ctx), fa.RENDER_SIDECAR_INVALID).reason == "sidecar_identity_inconsistent"


def test_a_sidecar_that_is_a_folder_is_invalid_and_left_alone(ctx):
    digest = rh.render_input_sha256(render_input())
    make_v2(ctx, pdf("a"), sidecar=None)
    (ctx.reports / ra.sidecar_name(digest)).mkdir()
    report = audit_unchanged(ctx)
    assert one(report, fa.RENDER_SIDECAR_INVALID).reason == "not_regular"


def test_an_incorrect_embedded_payload_hash_is_a_hash_mismatch(ctx):
    def change(doc):
        doc["render_input"]["input"]["client"]["name"] = "Someone Else"
    make_v2(ctx, pdf("a"), sidecar=lambda v, d: edited(sidecar_for(v, d), change))
    finding = one(audit_unchanged(ctx), fa.RENDER_INPUT_HASH_MISMATCH)
    assert (finding.reason, finding.severity) == ("payload_hash_mismatch", fa.SEVERITY_ATTENTION)


def test_a_declared_hash_that_does_not_match_the_payload_is_a_hash_mismatch(ctx):
    make_v2(ctx, pdf("a"), sidecar=lambda v, d: edited(sidecar_for(v, d),
                                                        lambda doc: doc.update(render_input_sha256="0" * 64)))
    assert one(audit(ctx), fa.RENDER_INPUT_HASH_MISMATCH).reason == "payload_hash_mismatch"


def test_a_sidecar_for_another_render_input_differs_from_the_record(ctx):
    other = render_input(day=6)
    make_v2(ctx, pdf("a"), sidecar=lambda v, d: sidecar_for(other, d))  # valid, but another input's sidecar
    finding = one(audit_unchanged(ctx), fa.RENDER_INPUT_HASH_MISMATCH)
    assert finding.reason == "sidecar_hash_differs_from_record"


@pytest.mark.parametrize("change", [
    lambda doc: doc["render_input_format"].update(version=1),
    lambda doc: doc["render_input_format"].update(version=99),
    lambda doc: doc["render_input_format"].update(name="other"),
    lambda doc: doc["render_input"].update(version=1),
    lambda doc: doc["render_input"].update(format="other")])
def test_an_unsupported_sidecar_format_is_reported(ctx, change):
    make_v2(ctx, pdf("a"), sidecar=lambda v, d: edited(sidecar_for(v, d), change))
    finding = one(audit_unchanged(ctx), fa.RENDER_INPUT_FORMAT_INVALID)
    assert (finding.reason, finding.severity) == ("unsupported_format", fa.SEVERITY_ATTENTION)


def test_a_record_format_that_differs_from_the_sidecars_is_reported(ctx, monkeypatch):
    monkeypatch.setattr(rs, "SUPPORTED_RENDER_INPUT_VERSIONS", (2, 3))  # let the schema accept a record claiming 3
    make_v2(ctx, pdf("a"), record_change=lambda r: r["finalization"]["render_input_format"].update(version=3))
    finding = one(audit_unchanged(ctx), fa.RENDER_INPUT_FORMAT_INVALID)
    assert finding.reason == "format_differs_from_record"


def test_a_sidecar_pdf_hash_that_differs_from_the_record_is_an_identity_mismatch(ctx):
    make_v2(ctx, pdf("a"), sidecar=lambda v, d: sidecar_for(v, d, pdf_sha="9" * 64))
    finding = one(audit_unchanged(ctx), fa.RECORD_IDENTITY_MISMATCH)
    assert (finding.reason, finding.severity, finding.record_version) == (
        "sidecar_pdf_hash_differs", fa.SEVERITY_ATTENTION, 2)


def test_a_sidecar_pdf_byte_count_that_differs_from_the_record_is_an_identity_mismatch(ctx):
    data = pdf("a")
    make_v2(ctx, data, sidecar=lambda v, d: sidecar_for(v, d, pdf_bytes=len(d) + 1))
    assert one(audit_unchanged(ctx), fa.RECORD_IDENTITY_MISMATCH).reason == "sidecar_pdf_size_differs"


# --- unreadable sidecar and unavailable checks --------------------------------------------------------------------------------------

class SidecarOps(ff.FileOps):
    """Fails for the sidecar only: the record, the PDF and the folder read normally."""

    def __init__(self, method):
        self.method = method

    def lstat(self, path):
        if self.method == "lstat" and "render-input" in Path(path).name:
            raise OSError(13, "injected")
        return super().lstat(path)

    def open_read(self, path):
        if self.method == "open_read" and "render-input" in Path(path).name:
            raise OSError(13, "injected")
        return super().open_read(path)


@pytest.mark.parametrize("method", ["open_read", "lstat"])
def test_an_unreadable_sidecar_is_a_limit_not_a_success(ctx, method):
    make_v2(ctx, pdf("a"))
    before = tree(ctx)
    report = audit(ctx, ops=SidecarOps(method))
    assert tree(ctx) == before
    finding = one(report, fa.VERIFICATION_UNAVAILABLE)
    assert (finding.reason, finding.severity, finding.record_version) == ("sidecar_unreadable", fa.SEVERITY_LIMIT, 2)
    assert (report.outcome, report.verification_complete) == (fa.OUTCOME_LIMITED, False)
    assert fa.FINALIZED_VALID not in cats(report) and fa.RENDER_SIDECAR_MISSING not in cats(report)


def test_an_unavailable_sidecar_parser_is_a_limit(ctx, monkeypatch):
    make_v2(ctx, pdf("a"))
    monkeypatch.setitem(sys.modules, "seo_report.render_artifacts", None)
    monkeypatch.delattr(seo_report, "render_artifacts", raising=False)
    report = audit(ctx)
    assert one(report, fa.VERIFICATION_UNAVAILABLE).reason == "render_sidecar_check_unavailable"
    assert (report.outcome, report.verification_complete) == (fa.OUTCOME_LIMITED, False)


def test_version_1_audits_do_not_need_the_sidecar_parser(ctx, monkeypatch):
    put_draft(ctx, pdf("a"))
    fin(ctx)
    monkeypatch.setitem(sys.modules, "seo_report.render_artifacts", None)
    monkeypatch.delattr(seo_report, "render_artifacts", raising=False)
    report = audit(ctx)
    assert (report.outcome, report.finalized_valid) == (fa.OUTCOME_CONSISTENT, 1)


# --- conventions across findings ------------------------------------------------------------------------------------------------------

def test_a_sidecar_problem_follows_the_existing_conventions_for_a_failed_record(ctx):
    make_v2(ctx, pdf("a"), sidecar=None)
    report = audit(ctx)
    assert fa.RENDER_SIDECAR_MISSING in cats(report) and fa.FINALIZED_VALID not in cats(report)
    assert set(cats(report)) <= {fa.RENDER_SIDECAR_MISSING, fa.SNAPSHOT_WITHOUT_FINALIZATION,
                                 fa.RENDERED_EVENT_WITHOUT_FINALIZATION}
    assert report.outcome == fa.OUTCOME_ATTENTION and report.unresolved == len(
        [f for f in report.findings if f.severity == fa.SEVERITY_ATTENTION])
    assert report.finalized_valid == 0


def test_a_sidecar_problem_on_one_record_leaves_the_other_valid(ctx):
    put_draft(ctx, pdf("old"))
    first = fin(ctx)
    make_v2(ctx, pdf("new"), sidecar=None, replace=True)
    report = audit(ctx)
    assert [f.name for f in report.by_category(fa.FINALIZED_VALID)] == [first.record_name]
    assert report.finalized_valid == 1 and report.legacy_records == 1
    assert one(report, fa.RENDER_SIDECAR_MISSING).record_version == 2


def test_a_damaged_pdf_is_still_reported_before_any_sidecar_check(ctx):
    make_v2(ctx, pdf("a"), sidecar=None)
    (ctx.reports / "report.pdf").write_bytes(pdf("changed"))
    report = audit(ctx)
    assert fa.PDF_HASH_MISMATCH in cats(report) and fa.RENDER_SIDECAR_MISSING not in cats(report)


def test_reason_codes_are_fixed_and_hold_no_client_data_or_paths(ctx):
    make_v2(ctx, pdf("a"), sidecar=None)
    report = audit(ctx)
    secrets = [sample().client.name, str(ctx.env.tmp), "C:\\", "Traceback"]
    for finding in report.findings:
        assert re.fullmatch(r"[a-z_0-9]+", finding.reason) and finding.category in fa.CATEGORIES
        assert not any(s and s in repr(finding) for s in secrets)


# --- read-only ---------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sidecar", [
    "valid", None, b"{broken", lambda v, d: sidecar_for(v, d, pdf_sha="9" * 64),
    lambda v, d: sidecar_for(render_input(day=6), d),
    lambda v, d: edited(sidecar_for(v, d), lambda doc: doc["render_input_format"].update(version=1))])
def test_the_audit_never_changes_records_sidecars_pdfs_or_the_database(ctx, sidecar):
    make_v2(ctx, pdf("a"), sidecar=sidecar)
    before = tree(ctx)
    names_before = sorted(p.name for p in ctx.reports.iterdir())
    first = audit(ctx)
    second = audit(ctx, deep_database_check=True)
    assert tree(ctx) == before and sorted(p.name for p in ctx.reports.iterdir()) == names_before
    assert reasons(audit(ctx)) == reasons(first) and second.verification_complete in (True, False)


def test_a_missing_sidecar_is_not_recreated_repaired_or_migrated(ctx):
    _, name = make_v2(ctx, pdf("a"), sidecar=None)
    record_path = next(ctx.reviewer.glob("reviewer.*.json"))
    record_before = record_path.read_bytes()
    audit(ctx)
    audit(ctx)
    assert not (ctx.reports / name).exists() and record_path.read_bytes() == record_before
    assert json.loads(record_before)["schema_version"] == 2


def test_a_version_1_record_is_not_migrated_by_the_audit(ctx):
    put_draft(ctx, pdf("a"))
    fin(ctx)
    path = next(ctx.reviewer.glob("reviewer.*.json"))
    before = path.read_bytes()
    audit(ctx)
    assert path.read_bytes() == before and json.loads(before)["schema_version"] == 1


def test_the_audit_source_has_no_write_or_delete_calls_for_sidecars():
    source = Path(fa.__file__).read_text(encoding="utf-8")
    for word in ("write_bytes", "write_text", "os.remove", "os.unlink", "os.replace", "shutil", "mkdir", "write_sidecar",
                 "handoff_draft", "produce_report_draft", "build_sidecar_bytes"):
        assert word not in source
