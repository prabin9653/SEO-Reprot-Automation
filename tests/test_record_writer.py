import copy
import dataclasses
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import finalize_files as ff
from seo_report import history_access as ha
from seo_report import output_layout as layout
from seo_report import record_schema as rs
from seo_report import record_writer as rw
from seo_report.reviewer_record import to_canonical_json
from test_finalize_files import FaultOps, link_or_skip
from test_record_schema import RUN, RUN2, SHA_A, SHA_B, SHA_C, finalized, mutated, removed

NAME = layout.reviewer_record_name(RUN, SHA_A)
OTHER_PDF = "d" * 64


def record(**changes):
    base = finalized()
    for path, value in changes.items():
        base = mutated(base, path.split("."), value)
    return base


def with_pdf(sha):
    base = record(**{"finalization.pdf_sha256": sha})
    base["finalization"]["rendered_event"]["detail"] = f"Report rendered (PDF {sha})"
    return base


def data_of(rec):
    return (to_canonical_json(rec) + "\n").encode("utf-8")


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "reviewer"
    path.mkdir()
    return path


def listing(folder):
    return {p.name: p.read_bytes() for p in folder.iterdir()}


def filler(folder, count, run=RUN, start=0x1000, content=b"{}"):
    for i in range(count):
        (folder / f"reviewer.{run}.{start + i:016x}.json").write_bytes(content)


class AppearsBefore(ff.FileOps):
    """Creates `name` in the folder just before the nth call of `method` (1-based)."""

    def __init__(self, method, n, action):
        self.method, self.n, self.action, self.count = method, n, action, 0

    def rename_noreplace(self, source, destination):
        if self.method == "rename_noreplace":
            self.count += 1
            if self.count == self.n:
                self.action()
        super().rename_noreplace(source, destination)

    def close(self, handle):
        super().close(handle)
        if self.method == "close":
            self.count += 1
            if self.count == self.n:
                self.action()


# --- validation and serialization -----------------------------------------------------------------------------------------------

def test_a_valid_record_is_written_with_the_reader_file_name(folder):
    rec = finalized()
    result = rw.write_reviewer_record(folder, rec)
    assert (result.outcome, result.written, result.name, result.run_id) == (rw.OUTCOME_WRITTEN, True, NAME, RUN)
    assert re.fullmatch(rf"reviewer\.{RUN}\.[0-9a-f]{{16}}\.json", result.name)
    assert result.name == f"reviewer.{RUN}.{SHA_A[:16]}.json" and result.ok
    assert listing(folder) == {NAME: data_of(rec)}
    assert result.byte_count == len(data_of(rec))


def test_the_written_bytes_are_canonical_valid_and_deterministic(folder, tmp_path):
    rec = finalized()
    rw.write_reviewer_record(folder, rec)
    other = tmp_path / "second"
    other.mkdir()
    rw.write_reviewer_record(other, copy.deepcopy(rec))
    first = (folder / NAME).read_bytes()
    assert first == (other / NAME).read_bytes() and first.endswith(b"\n")
    assert json.loads(first) == rec
    assert first.decode().rstrip("\n") == to_canonical_json(rec)
    assert rs.validate_finalized_record(json.loads(first))


def test_the_input_record_is_not_changed(folder):
    rec = finalized()
    before = copy.deepcopy(rec)
    rw.write_reviewer_record(folder, rec)
    assert rec == before


def test_missing_fields_are_refused_and_nothing_is_written(folder):
    for path in (["notes"], ["snapshot"], ["finalization", "client_content_sha256"], ["snapshot", "run_id"]):
        result = rw.write_reviewer_record(folder, removed(finalized(), path))
        assert (result.outcome, result.invalid) == (rw.OUTCOME_INVALID, "record_invalid")
        assert result.name is None and not result.ok
    assert listing(folder) == {}


def test_unknown_fields_are_refused_not_dropped(folder):
    rec = finalized()
    rec["extra"] = 1
    assert rw.write_reviewer_record(folder, rec).invalid == "record_invalid"
    rec = finalized()
    rec["snapshot"]["extra"] = 1
    assert rw.write_reviewer_record(folder, rec).invalid == "record_invalid"
    assert listing(folder) == {}


@pytest.mark.parametrize("bad", [None, [], "text", 5, {}, finalized().get("notes")])
def test_non_records_are_refused(folder, bad):
    assert rw.write_reviewer_record(folder, bad).outcome == rw.OUTCOME_INVALID
    assert listing(folder) == {}


@pytest.mark.parametrize("path,value", [(["snapshot", "run_id"], "not-a-uuid"), (["snapshot", "revision"], 0),
                                        (["snapshot", "content_hash"], "short"),
                                        (["finalization", "pdf_sha256"], "A" * 64),
                                        (["finalization", "pdf_bytes"], -1)])
def test_an_invalid_identity_is_refused(folder, path, value):
    result = rw.write_reviewer_record(folder, mutated(finalized(), path, value))
    assert result.outcome == rw.OUTCOME_INVALID
    assert listing(folder) == {}


def test_an_unserializable_record_is_refused(folder):
    rec = finalized()
    rec["client_facing_history"][0]["evidence"]["share"] = float("nan")
    assert rw.write_reviewer_record(folder, rec).outcome == rw.OUTCOME_INVALID
    assert listing(folder) == {}


def test_identity_ignores_finalized_at_and_other_fields():
    a, b = finalized(), record(**{"finalization.finalized_at": "2026-02-04T10:00:00+00:00"})
    b["notes"] = []
    assert rw.record_identity(a) == rw.record_identity(b)
    assert rw.record_identity(a) == (RUN, 1, SHA_B, SHA_A, 123456, SHA_C)


# --- idempotency and conflicts ---------------------------------------------------------------------------------------------------

def test_an_exact_existing_record_is_treated_as_already_written(folder):
    rec = finalized()
    rw.write_reviewer_record(folder, rec)
    before = listing(folder)
    result = rw.write_reviewer_record(folder, copy.deepcopy(rec))
    assert (result.outcome, result.written, result.ok) == (rw.OUTCOME_ALREADY, False, True)
    assert listing(folder) == before


def test_a_different_finalized_at_is_still_the_same_record(folder):
    rw.write_reviewer_record(folder, finalized())
    before = listing(folder)
    later = record(**{"finalization.finalized_at": "2026-02-04T10:00:00+00:00"})
    result = rw.write_reviewer_record(folder, later)
    assert result.outcome == rw.OUTCOME_ALREADY
    assert listing(folder) == before  # the original file keeps its original timestamp


@pytest.mark.parametrize("path,value", [
    (["snapshot", "revision"], 2), (["snapshot", "content_hash"], "e" * 64),
    (["finalization", "pdf_bytes"], 999), (["finalization", "client_content_sha256"], "e" * 64)])
def test_the_same_name_with_a_different_identity_is_a_conflict(folder, path, value):
    rw.write_reviewer_record(folder, finalized())
    before = listing(folder)
    result = rw.write_reviewer_record(folder, mutated(finalized(), path, value))
    assert (result.outcome, result.conflict, result.written) == (rw.OUTCOME_CONFLICT, "record_identity_differs", False)
    assert listing(folder) == before


@pytest.mark.parametrize("content", [
    b"", b"not json", b"{}", b'{"a": NaN}', b'{"a": Infinity}', b"\xff\xfe", b"[1,2]", b"null"])
def test_a_malformed_existing_record_is_never_accepted_or_replaced(folder, content):
    (folder / NAME).write_bytes(content)
    result = rw.write_reviewer_record(folder, finalized())
    assert (result.outcome, result.conflict) == (rw.OUTCOME_CONFLICT, "existing_record_invalid")
    assert listing(folder) == {NAME: content}


def test_duplicate_keys_in_an_existing_record_are_rejected(folder):
    text = data_of(finalized()).decode().rstrip("\n")
    duplicated = text.replace('"record_type":"finalized_report"',
                              '"record_type":"finalized_report","record_type":"finalized_report"', 1)
    assert duplicated != text
    (folder / NAME).write_bytes(duplicated.encode())
    result = rw.write_reviewer_record(folder, finalized())
    assert result.conflict == "existing_record_invalid"
    assert (folder / NAME).read_bytes() == duplicated.encode()


def test_an_existing_schema_invalid_record_with_matching_text_is_invalid(folder):
    broken = finalized()
    broken["notes"][0]["audience"] = "client"
    (folder / NAME).write_bytes(json.dumps(broken).encode())
    assert rw.write_reviewer_record(folder, finalized()).conflict == "existing_record_invalid"


def test_an_oversize_existing_record_is_invalid(folder, monkeypatch):
    monkeypatch.setattr(rw, "MAX_RECORD_BYTES", 100)
    (folder / NAME).write_bytes(b"x" * 101)
    assert rw.write_reviewer_record(folder, finalized()).outcome in (rw.OUTCOME_CONFLICT, rw.OUTCOME_INVALID)
    assert (folder / NAME).read_bytes() == b"x" * 101


def test_an_existing_folder_at_the_target_is_a_conflict(folder):
    (folder / NAME).mkdir()
    result = rw.write_reviewer_record(folder, finalized())
    assert (result.outcome, result.conflict) == (rw.OUTCOME_CONFLICT, "existing_record_not_regular")
    assert [p.name for p in folder.iterdir()] == [NAME] and (folder / NAME).is_dir()


def test_an_existing_link_at_the_target_is_a_conflict(folder, tmp_path):
    target = tmp_path / "t.json"
    target.write_bytes(data_of(finalized()))
    link_or_skip(folder / NAME, target)
    assert rw.write_reviewer_record(folder, finalized()).conflict == "existing_record_not_regular"
    assert target.read_bytes() == data_of(finalized())


def test_an_unreadable_existing_record_is_a_failure_that_changes_nothing(folder):
    (folder / NAME).write_bytes(data_of(finalized()))

    class Unreadable(ff.FileOps):
        def open_read(self, path):
            raise OSError(13, "C:\\secret\\path denied")

    result = rw.write_reviewer_record(folder, finalized(), ops=Unreadable())
    assert (result.outcome, result.failure) == (rw.OUTCOME_FAILED, "existing_record_unreadable")
    assert "secret" not in repr(result)
    assert listing(folder) == {NAME: data_of(finalized())}


def test_a_destination_that_appears_during_promotion_with_the_same_identity_is_already_written(folder):
    rec = finalized()
    ops = AppearsBefore("rename_noreplace", 1, lambda: (folder / NAME).write_bytes(data_of(rec)))
    result = rw.write_reviewer_record(folder, rec, ops=ops)
    assert (result.outcome, result.written) == (rw.OUTCOME_ALREADY, False)
    assert result.partial_left_behind and ff.is_partial_name(result.partial_name)
    assert listing(folder)[NAME] == data_of(rec)


def test_a_destination_that_appears_during_promotion_with_another_identity_is_a_conflict(folder):
    other = mutated(finalized(), ["snapshot", "revision"], 2)
    ops = AppearsBefore("rename_noreplace", 1, lambda: (folder / NAME).write_bytes(data_of(other)))
    result = rw.write_reviewer_record(folder, finalized(), ops=ops)
    assert (result.outcome, result.conflict) == (rw.OUTCOME_CONFLICT, "record_identity_differs")
    assert (folder / NAME).read_bytes() == data_of(other)


def test_a_destination_that_appears_after_the_partial_is_written_is_reconciled(folder):
    rec = finalized()
    ops = AppearsBefore("close", 1, lambda: (folder / NAME).write_bytes(b"garbage"))
    result = rw.write_reviewer_record(folder, rec, ops=ops)
    assert (result.outcome, result.conflict) == (rw.OUTCOME_CONFLICT, "existing_record_invalid")
    assert (folder / NAME).read_bytes() == b"garbage" and result.partial_left_behind


# --- partial files ------------------------------------------------------------------------------------------------------------------

def test_a_successful_write_leaves_no_partial_file(folder):
    rw.write_reviewer_record(folder, finalized())
    assert [p.name for p in folder.iterdir()] == [NAME]


def test_partial_names_are_unique_and_never_reader_eligible(folder):
    for fail in ("close", "sync", "write"):
        rw.write_reviewer_record(folder, finalized(), ops=FaultOps(fail=fail))
    names = [p.name for p in folder.iterdir()]
    assert len(names) == 3 and len(set(names)) == 3
    assert all(ff.is_partial_name(n) for n in names)
    assert not any(re.fullmatch(rf"reviewer\.{RUN}\.[0-9a-f]{{16}}\.json", n) for n in names)


@pytest.mark.parametrize("fail,category", [("write", "write_failed"), ("sync", "write_failed"),
                                           ("close", "close_failed"), ("write_half", "write_failed")])
def test_write_sync_and_close_failures_keep_the_partial_and_never_create_the_record(folder, fail, category):
    result = rw.write_reviewer_record(folder, finalized(), ops=FaultOps(fail=fail))
    assert (result.outcome, result.failure, result.written) == (rw.OUTCOME_FAILED, category, False)
    assert result.partial_left_behind and ff.is_partial_name(result.partial_name)
    assert [p.name for p in folder.iterdir()] == [result.partial_name]
    assert "injected" not in repr(result) and str(folder) not in repr(result)


def test_a_create_failure_leaves_nothing(folder):
    result = rw.write_reviewer_record(folder, finalized(), ops=FaultOps(fail="create_exclusive"))
    assert (result.outcome, result.failure, result.partial_left_behind) == (rw.OUTCOME_FAILED, "create_failed", False)
    assert listing(folder) == {}


def test_a_promotion_failure_keeps_the_complete_partial(folder):
    rec = finalized()
    result = rw.write_reviewer_record(folder, rec, ops=FaultOps(fail="rename_noreplace"))
    assert (result.outcome, result.failure) == (rw.OUTCOME_FAILED, "promote_failed")
    assert listing(folder) == {result.partial_name: data_of(rec)}


def test_stale_partial_files_stay_untouched_and_are_not_adopted(folder):
    stale = ff.partial_name(NAME, "0123456789ab")
    (folder / stale).write_bytes(b'{"half"')
    result = rw.write_reviewer_record(folder, finalized())
    assert result.outcome == rw.OUTCOME_WRITTEN
    assert (folder / stale).read_bytes() == b'{"half"'
    assert listing(folder).keys() == {stale, NAME}


def test_retry_after_an_interrupted_write_succeeds_and_keeps_the_old_partial(folder):
    first = rw.write_reviewer_record(folder, finalized(), ops=FaultOps(fail="sync"))
    second = rw.write_reviewer_record(folder, finalized())
    assert second.outcome == rw.OUTCOME_WRITTEN
    assert (folder / first.partial_name).exists() and (folder / NAME).read_bytes() == data_of(finalized())
    assert rw.write_reviewer_record(folder, finalized()).outcome == rw.OUTCOME_ALREADY


def test_a_partial_does_not_count_toward_the_reader_limit(folder):
    filler(folder, 19)
    for i in range(30):
        (folder / ff.partial_name(NAME, f"{i:012x}")).write_bytes(b"x")
    assert rw.count_run_records(folder, RUN) == 19
    assert rw.write_reviewer_record(folder, finalized()).outcome == rw.OUTCOME_WRITTEN


# --- the reader limit ---------------------------------------------------------------------------------------------------------------

def test_nineteen_records_permit_the_twentieth(folder):
    filler(folder, 19)
    result = rw.write_reviewer_record(folder, finalized())
    assert result.outcome == rw.OUTCOME_WRITTEN and result.existing_records == 19
    assert rw.count_run_records(folder, RUN) == 20


def test_twenty_records_block_a_new_record(folder):
    filler(folder, 20)
    before = listing(folder)
    result = rw.write_reviewer_record(folder, finalized())
    assert (result.outcome, result.conflict, result.existing_records) == (rw.OUTCOME_CONFLICT, "reader_record_limit", 20)
    assert listing(folder) == before  # no partial was created either


def test_invalid_matching_records_count(folder):
    filler(folder, 20, content=b"not json")
    assert rw.write_reviewer_record(folder, finalized()).conflict == "reader_record_limit"


def test_other_runs_and_other_names_do_not_count(folder):
    filler(folder, 30, run=RUN2)
    filler(folder, 5)
    (folder / f"reviewer.{RUN}.XYZ.json").write_bytes(b"{}")
    (folder / f"reviewer.{RUN}.{'a' * 16}.json.bak").write_bytes(b"{}")
    (folder / "notes.txt").write_bytes(b"x")
    assert rw.count_run_records(folder, RUN) == 5
    assert rw.write_reviewer_record(folder, finalized()).outcome == rw.OUTCOME_WRITTEN


def test_a_folder_with_a_matching_name_does_not_count_as_the_reader_ignores_it(folder):
    filler(folder, 19)
    (folder / f"reviewer.{RUN}.{'f' * 16}.json").mkdir()
    assert rw.count_run_records(folder, RUN) == 19


def test_an_exact_existing_record_is_idempotent_even_at_the_limit(folder):
    rw.write_reviewer_record(folder, finalized())
    filler(folder, 19)
    assert rw.count_run_records(folder, RUN) == 20
    result = rw.write_reviewer_record(folder, finalized())
    assert result.outcome == rw.OUTCOME_ALREADY
    assert rw.count_run_records(folder, RUN) == 20


def test_a_conflicting_record_at_the_limit_reports_the_identity_conflict(folder):
    rw.write_reviewer_record(folder, finalized())
    filler(folder, 19)
    result = rw.write_reviewer_record(folder, mutated(finalized(), ["snapshot", "revision"], 2))
    assert result.conflict == "record_identity_differs"


def test_the_recheck_catches_records_that_appear_during_the_operation(folder):
    filler(folder, 19)
    ops = AppearsBefore("close", 1, lambda: filler(folder, 1, start=0x9000))
    result = rw.write_reviewer_record(folder, finalized(), ops=ops)
    assert (result.outcome, result.conflict, result.existing_records) == (rw.OUTCOME_CONFLICT, "reader_record_limit", 20)
    assert result.partial_left_behind and not (folder / NAME).exists()
    assert rw.count_run_records(folder, RUN) == 20


def test_a_scan_failure_is_reported_safely(folder, monkeypatch):
    def broken(directory, run_id):
        raise OSError(5, "C:\\private\\path")
    monkeypatch.setattr(rw, "count_run_records", broken)
    result = rw.write_reviewer_record(folder, finalized())
    assert (result.outcome, result.failure) == (rw.OUTCOME_FAILED, "scan_failed")
    assert "private" not in repr(result) and listing(folder) == {}


def test_the_limits_match_the_reader():
    assert rw.MAX_RECORDS_PER_RUN == ha.MAX_RECORD_CANDIDATES
    assert rw.MAX_RECORD_BYTES == ha.MAX_RECORD_BYTES


def test_the_written_record_is_seen_by_the_reader_name_scan(folder):
    rw.write_reviewer_record(folder, finalized())
    assert ha.FinalizedOnlyReader._record_names(folder, RUN) == [(NAME, SHA_A[:16])]
    assert ha._load_record(folder / NAME) == finalized()


def test_existing_finalized_records_stay_unchanged_and_readable(folder):
    first = finalized()
    second = with_pdf(OTHER_PDF)
    rw.write_reviewer_record(folder, first)
    before = (folder / NAME).read_bytes()
    result = rw.write_reviewer_record(folder, second)
    assert result.outcome == rw.OUTCOME_WRITTEN and result.name != NAME
    assert (folder / NAME).read_bytes() == before
    assert {n for n, _ in ha.FinalizedOnlyReader._record_names(folder, RUN)} == {NAME, result.name}
    assert ha._load_record(folder / result.name) == second


# --- privacy and filesystem safety --------------------------------------------------------------------------------------------------

def test_results_hold_no_paths_or_system_text(folder):
    for ops in (None, FaultOps(fail="close"), FaultOps(fail="rename_noreplace"), FaultOps(fail="create_exclusive")):
        text = repr(rw.write_reviewer_record(folder, finalized(), ops=ops))
        assert str(folder) not in text and "Errno" not in text and "injected" not in text
        assert "\\" not in text and "Traceback" not in text and "client_key" not in text


def test_result_is_frozen_with_fixed_categories(folder):
    result = rw.write_reviewer_record(folder, finalized())
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.outcome = "x"
    assert result.outcome in rw.OUTCOMES
    for category in (rw.CONFLICT_CATEGORIES + rw.FAILURE_CATEGORIES + rw.INVALID_CATEGORIES):
        assert re.fullmatch(r"[a-z_]+", category)


def test_no_folder_is_created_and_nothing_is_written_outside(tmp_path):
    missing = tmp_path / "missing"
    result = rw.write_reviewer_record(missing, finalized())
    assert (result.outcome, result.failure) == (rw.OUTCOME_FAILED, "directory_unusable")
    assert not missing.exists()
    result = rw.write_reviewer_record(Path("relative"), finalized())
    assert result.failure == "directory_unusable"
    assert list(tmp_path.iterdir()) == []


def test_only_the_supplied_folder_is_touched(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "reviewer"
    sibling = outer / "reports"
    inner.mkdir(parents=True)
    sibling.mkdir()
    (sibling / "report.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (outer / "keep.txt").write_bytes(b"x")
    snapshot = {str(p): (p.read_bytes() if p.is_file() else None) for p in outer.rglob("*")}
    rw.write_reviewer_record(inner, finalized())
    after = {str(p): (p.read_bytes() if p.is_file() else None) for p in outer.rglob("*")}
    assert {k: v for k, v in after.items() if k != str(inner / NAME)} == snapshot
    assert (inner / NAME).exists()


def test_a_relative_or_linked_folder_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link_or_skip(link, real)
    result = rw.write_reviewer_record(link, finalized())
    assert result.failure == "directory_unusable" and list(real.iterdir()) == []


def test_the_module_never_deletes_overwrites_or_touches_other_systems():
    source = (ROOT / "src" / "seo_report" / "record_writer.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("os.replace", ".replace(", "unlink", "remove(", "rmtree", "shutil", "mkdir", "makedirs",
                      "write_text", "write_bytes", "open(", "sqlite", "requests", "urllib", "google", "environ"):
        assert forbidden not in code, forbidden
