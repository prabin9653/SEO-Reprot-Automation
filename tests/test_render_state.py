"""R7.4: the read-only render-state helpers (finalized render hashes, sidecar lookup, operational restore)."""
import copy
import dataclasses
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import finalize_files as ff
from seo_report import output_layout as layout
from seo_report import record_writer as rw
from seo_report import render_artifacts as ra
from seo_report import render_input as ri
from seo_report import render_input_hash as rh
from seo_report import render_state as rs_
from seo_report.reviewer_record import to_canonical_json
from test_finalize_files import FaultOps
from test_record_schema import RUN, RUN2, SHA_A, SHA_B, SHA_C, finalized, mutated
from test_record_v2 import RENDER_SHA, OTHER_RENDER_SHA, v2
from test_render_input import sample

RENDERER = {"browser": "156.0.8078.4", "playwright": "1.64.1", "python": "3.14.0", "system": "Windows"}
PDF_A, PDF_B, PDF_C = "a" * 64, "b" * 64, "c" * 64


def snapshot_of(folder):
    return {p.name: (p.read_bytes() if p.is_file() else None) for p in sorted(Path(folder).iterdir())}


@pytest.fixture
def reviewer(tmp_path):
    folder = tmp_path / "reviewer"
    folder.mkdir()
    return folder


@pytest.fixture
def reports(tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    return folder


def put(folder, record, run=RUN, pdf=None):
    pdf = pdf or record["finalization"]["pdf_sha256"]
    record = copy.deepcopy(record)
    name = layout.reviewer_record_name(run, pdf)
    (folder / name).write_bytes((to_canonical_json(record) + "\n").encode("utf-8"))
    return name


def record_with(version, run=RUN, pdf=PDF_A, render=RENDER_SHA):
    base = finalized() if version == 1 else v2(render_sha=render)
    base = mutated(base, ["snapshot", "run_id"], run)
    base = mutated(base, ["finalization", "pdf_sha256"], pdf)
    base["finalization"]["rendered_event"]["detail"] = f"Report rendered (PDF {pdf})"
    return base


def input_with(day):
    return dataclasses.replace(sample(), generation_date=sample().generation_date.replace(day=day))


def sidecar_bytes(value, data=b"%PDF-1.7\n%%EOF\n"):
    return ra.build_sidecar_bytes(value, renderer=RENDERER, pdf_sha256=hashlib.sha256(data).hexdigest(),
                                  pdf_bytes=len(data))


# --- finalized render hashes ---------------------------------------------------------------------------------------------------------

def test_an_empty_reviewer_folder_has_no_finalized_hashes(reviewer):
    scan = rs_.scan_finalized_render_hashes(reviewer)
    assert (scan.hashes, scan.v1_records, scan.v2_records, scan.ignored, scan.complete) == (frozenset(), 0, 0, (), True)


def test_version_2_hashes_are_discovered_and_version_1_records_are_only_counted(reviewer):
    put(reviewer, record_with(2, pdf=PDF_A, render=RENDER_SHA))
    put(reviewer, record_with(2, pdf=PDF_B, render=OTHER_RENDER_SHA))
    put(reviewer, record_with(1, pdf=PDF_C))
    scan = rs_.scan_finalized_render_hashes(reviewer)
    assert scan.hashes == {RENDER_SHA, OTHER_RENDER_SHA}
    assert (scan.v1_records, scan.v2_records, scan.ignored, scan.complete) == (1, 2, (), True)
    assert rs_.finalized_render_hashes(reviewer) == scan.hashes


def test_a_folder_with_only_version_1_records_has_no_render_identity(reviewer):
    put(reviewer, record_with(1))
    assert rs_.finalized_render_hashes(reviewer) == frozenset()


@pytest.mark.parametrize("make,reason", [
    (lambda r: b"{broken", "invalid_record"),
    (lambda r: b"", "invalid_record"),
    (lambda r: b'{"a":1,"a":2}', "invalid_record"),
    (lambda r: (to_canonical_json(mutated(record_with(2), ["schema_version"], 3)) + "\n").encode(), "invalid_record"),
    (lambda r: (to_canonical_json(mutated(record_with(2), ["finalization", "render_input_sha256"], "bad")) + "\n")
     .encode(), "invalid_record"),
    (lambda r: (to_canonical_json({k: v for k, v in record_with(2).items() if k != "history"}) + "\n").encode(),
     "invalid_record"),
    (lambda r: (to_canonical_json(mutated(record_with(2), ["finalization", "render_input_format", "version"], 1))
                + "\n").encode(), "invalid_record")])
def test_malformed_or_unsupported_records_never_contribute_a_hash(reviewer, make, reason):
    name = layout.reviewer_record_name(RUN, PDF_A)
    (reviewer / name).write_bytes(make(reviewer))
    scan = rs_.scan_finalized_render_hashes(reviewer)
    assert scan.hashes == frozenset() and scan.ignored == ((name, reason),) and scan.complete


def test_a_record_whose_content_disagrees_with_its_file_name_is_not_a_finalized_identity(reviewer):
    wrong_pdf = layout.reviewer_record_name(RUN, PDF_B)
    (reviewer / wrong_pdf).write_bytes((to_canonical_json(record_with(2, pdf=PDF_A)) + "\n").encode())
    wrong_run = layout.reviewer_record_name(RUN2, PDF_A)
    (reviewer / wrong_run).write_bytes((to_canonical_json(record_with(2, run=RUN, pdf=PDF_A)) + "\n").encode())
    scan = rs_.scan_finalized_render_hashes(reviewer)
    assert scan.hashes == frozenset() and dict(scan.ignored) == {wrong_pdf: "name_mismatch", wrong_run: "name_mismatch"}


def test_a_non_regular_or_oversized_candidate_is_ignored(reviewer, monkeypatch):
    folder_name = layout.reviewer_record_name(RUN, PDF_A)
    (reviewer / folder_name).mkdir()
    big = layout.reviewer_record_name(RUN, PDF_B)
    (reviewer / big).write_bytes((to_canonical_json(record_with(2, pdf=PDF_B)) + "\n").encode())
    monkeypatch.setattr(rw, "MAX_RECORD_BYTES", 100)
    scan = rs_.scan_finalized_render_hashes(reviewer)
    assert scan.hashes == frozenset() and dict(scan.ignored) == {folder_name: "not_regular_file", big: "too_large"}


def test_partial_files_and_other_names_are_not_candidates(reviewer):
    put(reviewer, record_with(2))
    (reviewer / ff.partial_name(layout.reviewer_record_name(RUN, PDF_B), "0123456789ab")).write_bytes(b"{}")
    (reviewer / "notes.txt").write_text("x")
    (reviewer / "reviewer.not-a-run.json").write_text("{}")
    scan = rs_.scan_finalized_render_hashes(reviewer)
    assert scan.hashes == {RENDER_SHA} and scan.ignored == () and scan.v2_records == 1


def test_an_unreadable_candidate_makes_the_scan_incomplete_and_gives_no_hash(reviewer):
    put(reviewer, record_with(2, pdf=PDF_A, render=RENDER_SHA))
    bad = put(reviewer, record_with(2, pdf=PDF_B, render=OTHER_RENDER_SHA))

    class Unreadable(ff.FileOps):
        def open_read(self, path):
            if Path(path).name == bad:
                raise OSError(13, "injected")
            return super().open_read(path)
    scan = rs_.scan_finalized_render_hashes(reviewer, ops=Unreadable())
    assert scan.hashes == {RENDER_SHA} and scan.ignored == ((bad, "unreadable"),) and not scan.complete
    with pytest.raises(rs_.RenderStateError) as info:
        rs_.finalized_render_hashes(reviewer, ops=Unreadable(), require_complete=True)
    assert info.value.code == "scan_incomplete"


def test_folder_problems_are_explicit_errors(tmp_path):
    for path, code in ((tmp_path / "missing", "reviewer_dir_missing"), ("relative", "reviewer_dir_invalid")):
        with pytest.raises(rs_.RenderStateError) as info:
            rs_.scan_finalized_render_hashes(path)
        assert info.value.code == code
    plain = tmp_path / "file"
    plain.write_text("x")
    with pytest.raises(rs_.RenderStateError) as info:
        rs_.scan_finalized_render_hashes(plain)
    assert info.value.code == "reviewer_dir_invalid"
    assert not (tmp_path / "missing").exists()  # never created


def test_scanning_changes_nothing(reviewer):
    put(reviewer, record_with(2))
    put(reviewer, record_with(1, pdf=PDF_B))
    (reviewer / "reviewer.broken").write_bytes(b"{")
    before = snapshot_of(reviewer)
    rs_.scan_finalized_render_hashes(reviewer)
    rs_.finalized_render_hashes(reviewer)
    assert snapshot_of(reviewer) == before


# --- sidecars ----------------------------------------------------------------------------------------------------------------------------

def test_a_valid_sidecar_is_read_by_its_render_input_hash(reports):
    value = input_with(5)
    digest = rh.render_input_sha256(value)
    (reports / ra.sidecar_name(digest)).write_bytes(sidecar_bytes(value))
    sidecar = rs_.read_sidecar_for(reports, digest)
    assert sidecar.render_input_sha256 == digest and rh.render_input_sha256(sidecar.render_input) == digest


@pytest.mark.parametrize("digest,code", [("abc", "hash_invalid"), (None, "hash_invalid"), (5, "hash_invalid"),
                                         ("f" * 64, "sidecar_missing")])
def test_a_bad_hash_or_missing_sidecar_is_an_explicit_error(reports, digest, code):
    with pytest.raises(rs_.RenderStateError) as info:
        rs_.read_sidecar_for(reports, digest)
    assert info.value.code == code


def test_a_missing_reports_folder_is_never_created(tmp_path):
    with pytest.raises(rs_.RenderStateError) as info:
        rs_.read_sidecar_for(tmp_path / "none", "f" * 64)
    assert info.value.code == "reports_dir_missing" and not (tmp_path / "none").exists()


def test_malformed_and_foreign_sidecars_are_refused(reports):
    value, other = input_with(5), input_with(6)
    digest = rh.render_input_sha256(value)
    path = reports / ra.sidecar_name(digest)
    path.write_bytes(b"{broken")
    with pytest.raises(rs_.RenderStateError) as info:
        rs_.read_sidecar_for(reports, digest)
    assert info.value.code == "sidecar_malformed"
    path.write_bytes(sidecar_bytes(other))  # a valid sidecar of another input at this name
    with pytest.raises(rs_.RenderStateError) as info:
        rs_.read_sidecar_for(reports, digest)
    assert info.value.code in ("identity_mismatch",)


def test_an_unreadable_sidecar_is_reported_as_unreadable(reports):
    value = input_with(5)
    digest = rh.render_input_sha256(value)
    (reports / ra.sidecar_name(digest)).write_bytes(sidecar_bytes(value))

    class Unreadable(ff.FileOps):
        def open_read(self, path):
            raise OSError(13, "injected")
    with pytest.raises(rs_.RenderStateError) as info:
        rs_.read_sidecar_for(reports, digest, ops=Unreadable())
    assert info.value.code == "sidecar_unreadable"


def test_a_finalized_hash_has_no_open_sidecar(reports):
    value = input_with(5)
    digest = rh.render_input_sha256(value)
    (reports / ra.sidecar_name(digest)).write_bytes(sidecar_bytes(value))
    assert rs_.find_open_sidecar(reports, digest).render_input_sha256 == digest
    with pytest.raises(rs_.RenderStateError) as info:
        rs_.find_open_sidecar(reports, digest, finalized={digest})
    assert info.value.code == "render_input_finalized"
    assert rs_.read_sidecar_for(reports, digest).render_input_sha256 == digest  # still readable (idempotent retry)


def test_open_sidecars_lists_the_valid_open_ones_and_refuses_an_invalid_file(reports):
    one, two = input_with(5), input_with(6)
    for value in (one, two):
        (reports / ra.sidecar_name(rh.render_input_sha256(value))).write_bytes(sidecar_bytes(value))
    found = rs_.open_sidecars(reports)
    assert sorted(s.render_input_sha256 for s in found) == sorted([rh.render_input_sha256(one), rh.render_input_sha256(two)])
    closed = {rh.render_input_sha256(one)}
    assert [s.render_input_sha256 for s in rs_.open_sidecars(reports, finalized=closed)] == [rh.render_input_sha256(two)]
    bad = "report.render-input." + "0" * 16 + ".json"
    (reports / bad).write_bytes(b"{broken")
    with pytest.raises(rs_.RenderStateError) as info:
        rs_.open_sidecars(reports)
    assert (info.value.code, info.value.group) == ("sidecar_invalid", bad)


def test_the_sidecar_helpers_change_nothing(reports):
    value = input_with(5)
    digest = rh.render_input_sha256(value)
    (reports / ra.sidecar_name(digest)).write_bytes(sidecar_bytes(value))
    (reports / "report.pdf").write_bytes(b"%PDF-1.7\n%%EOF\n")
    before = snapshot_of(reports)
    rs_.read_sidecar_for(reports, digest)
    rs_.find_open_sidecar(reports, digest)
    rs_.open_sidecars(reports)
    assert snapshot_of(reports) == before


# --- operational identity --------------------------------------------------------------------------------------------------------------------

def test_a_rebuilt_input_gets_its_snapshot_identity_without_changing_its_hash():
    value = sample()
    rebuilt = ra.parse_sidecar(sidecar_bytes(value)).render_input
    assert rebuilt.operational == ri.RenderOperationalMetadata()
    restored = rs_.with_operational(rebuilt, client_key="example-client", snapshot_content_hash="b" * 64)
    assert (restored.operational.client_key, restored.operational.snapshot_content_hash) == ("example-client", "b" * 64)
    assert rh.render_input_sha256(restored) == rh.render_input_sha256(rebuilt) == rh.render_input_sha256(value)
    assert dataclasses.replace(restored, operational=ri.RenderOperationalMetadata()) == rebuilt  # nothing else changed
    assert rebuilt.operational == ri.RenderOperationalMetadata()  # the argument is immutable


def test_an_input_that_already_has_the_identity_is_returned_as_is_and_another_identity_is_refused():
    value = dataclasses.replace(sample(), operational=ri.RenderOperationalMetadata(
        client_key="example-client", snapshot_content_hash="b" * 64))
    assert rs_.with_operational(value, client_key="example-client", snapshot_content_hash="b" * 64) is value
    for key, digest in (("other", "b" * 64), ("example-client", "c" * 64)):
        with pytest.raises(rs_.RenderStateError) as info:
            rs_.with_operational(value, client_key=key, snapshot_content_hash=digest)
        assert info.value.code == "operational_mismatch"
    with pytest.raises(TypeError):
        rs_.with_operational("x", client_key="a", snapshot_content_hash="b" * 64)


def test_errors_carry_fixed_codes_and_no_paths(reports, tmp_path):
    with pytest.raises(rs_.RenderStateError) as info:
        rs_.read_sidecar_for(reports, "f" * 64)
    assert str(tmp_path) not in str(info.value) and info.value.code == "sidecar_missing"


def test_the_module_source_has_no_write_delete_or_database_calls():
    source = Path(rs_.__file__).read_text(encoding="utf-8")
    for word in ("write_bytes", "write_text", "write_partial", "write_exclusive", "write_sidecar", "os.remove",
                 "os.unlink", "os.replace", "rename_noreplace", "os.rename", ".mkdir(", "shutil", "sqlite3",
                 "SnapshotStore", "open(" + "'w"):
        assert word not in source
