import copy
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_finalize_files import FaultOps
from seo_report import attempt_log as al
from seo_report import finalize_files as ff
from seo_report import output_layout as layout
from seo_report import record_schema as rs

RUN = "0198c1a2-7b3c-7d4e-8f5a-1234567890ab"
SHA = "ab" * 32
TS = "2026-02-03T09:30:00+00:00"
PERIOD = ("2026-01-01", "2026-01-31")
STATUS_CODES = ["draft_invalid", "output_unavailable", "persist_failed", "event_failed", "supersede_failed",
                "superseded_name_conflict", "superseded_variants_exhausted", "duplicate_draft_conflict",
                "promote_failed", "reviewer_record_conflict", "record_failed", "stale_bundle", "draft_partial_conflict",
                "existing_report_conflict", "reader_record_limit"]
STEPS = ["preflight", "verify_draft", "persist", "event", "supersede", "promote", "record"]


def make(**overrides):
    values = dict(client_key="example-client", report_period=PERIOD, attempted_at=TS, step="persist",
                  status_code="persist_failed", exception_class="OperationalError")
    values.update(overrides)
    return al.build_attempt_record(**values)


@pytest.fixture
def out(tmp_path):
    root = tmp_path / "out"
    folder = layout.attempt_dir(root, "example-client", *PERIOD)
    folder.mkdir(parents=True)
    return SimpleRoot(root, folder)


class SimpleRoot:
    def __init__(self, root, folder):
        self.root, self.folder = root, folder

    def names(self):
        return sorted(p.name for p in self.folder.iterdir())

    def state(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(self.root.parent.rglob("*"))
                if p.is_file()}


_DEFAULT = object()


def write(out, record=_DEFAULT, **kwargs):
    return al.write_attempt_record(make() if record is _DEFAULT else record, output_root=out.root, **kwargs)


# --- the approved vocabulary -----------------------------------------------------------------------------------------

def test_the_approved_codes_and_steps_are_the_schemas_own():
    assert list(al.STATUS_CODES) == STATUS_CODES and len(al.STATUS_CODES) == 15
    assert list(al.STEPS) == STEPS and len(al.STEPS) == 7
    assert al.STATUS_CODES is rs.ATTEMPT_STATUS_CODES and al.STEPS is rs.ATTEMPT_STEPS is layout.ATTEMPT_STEPS


@pytest.mark.parametrize("status", STATUS_CODES)
def test_every_approved_status_code_builds_a_valid_record(status):
    record = make(status_code=status)
    assert rs.validate_attempt_record(record) is record and record["status_code"] == status


@pytest.mark.parametrize("step", STEPS)
def test_every_approved_step_builds_a_valid_record(step):
    record = make(step=step)
    assert record["step"] == step and rs.validate_attempt_record(record) is record


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("status", STATUS_CODES)
def test_every_step_and_status_combination_is_accepted_without_extra_rules(step, status):
    assert al.build_attempt_record(client_key="k", report_period=PERIOD, attempted_at=TS, step=step,
                                   status_code=status)["status_code"] == status


@pytest.mark.parametrize("status", ["finalized", "succeeded", "success", "failed", "stale", "Persist_Failed",
                                    "persist_failed ", "", None, 5])
def test_unknown_status_codes_are_rejected(status):
    with pytest.raises(al.AttemptRecordError, match="status_code"):
        make(status_code=status)


@pytest.mark.parametrize("step", ["assemble", "draft_pdf", "persist_snapshot", "rendered_event", "Persist", "persist ",
                                  "", None, 3])
def test_unknown_steps_are_rejected(step):
    with pytest.raises(al.AttemptRecordError, match="step"):
        make(step=step)


# --- the record ----------------------------------------------------------------------------------------------------------

def test_a_record_has_exactly_the_schema_fields_and_is_never_finalized():
    record = make()
    assert list(record) == list(rs.ATTEMPT_FIELDS) and len(record) == 14
    assert record["finalized"] is False and record["record_type"] == "attempt" and record["schema_version"] == 1


def test_defaults_are_nulls_and_values_are_kept_exactly():
    record = al.build_attempt_record(client_key="k", report_period=PERIOD, attempted_at=TS, step="record",
                                     status_code="record_failed")
    assert (record["exception_class"], record["run_id"], record["pdf_sha256"], record["pdf_bytes"]) == (None,) * 4
    assert (record["previous_report_restored"], record["previous_report_preserved_as"]) == (None, None)
    full = make(run_id=RUN, pdf_sha256=SHA, pdf_bytes=0, previous_report_restored=False,
                previous_report_preserved_as={"sha256_16": "b" * 16, "variant_index": 99})
    assert (full["run_id"], full["pdf_sha256"], full["pdf_bytes"]) == (RUN, SHA, 0)  # zero stays zero
    assert full["previous_report_restored"] is False  # false stays false, not null
    assert full["previous_report_preserved_as"] == {"sha256_16": "b" * 16, "variant_index": 99}
    assert make(previous_report_restored=True)["previous_report_restored"] is True
    assert make(previous_report_restored=None)["previous_report_restored"] is None


def test_the_period_may_be_dates_a_pair_or_a_mapping():
    expected = {"start": "2026-01-01", "end": "2026-01-31"}
    assert make(report_period=(dt.date(2026, 1, 1), dt.date(2026, 1, 31)))["report_period"] == expected
    assert make(report_period=["2026-01-01", "2026-01-31"])["report_period"] == expected
    assert make(report_period={"end": "2026-01-31", "start": "2026-01-01"})["report_period"] == expected


@pytest.mark.parametrize("period", [("2026-02-01", "2026-01-31"), ("2026-1-1", "2026-01-31"), ("20260101", "2026-01-31"),
                                    ("2026-02-30", "2026-03-01"), (None, "2026-01-31"), ("2026-01-01",), (),
                                    "2026-01-01", None, {"start": "2026-01-01"}, {"start": "2026-01-01", "end": "x"},
                                    {"start": "2026-01-01", "end": "2026-01-31", "extra": 1},
                                    (dt.datetime(2026, 1, 1), dt.date(2026, 1, 31)), ("2026-01-01", "2026-01-31", "x")])
def test_invalid_periods_are_rejected(period):
    with pytest.raises(al.AttemptRecordError):
        make(report_period=period)


@pytest.mark.parametrize("key", ["", "Example-Client", "example client", "../x", "a/b", "con", "nul", "x" * 64, "-a", None, 5])
def test_invalid_client_keys_are_rejected(key):
    with pytest.raises(al.AttemptRecordError, match="client_key"):
        make(client_key=key)


@pytest.mark.parametrize("stamp", ["2026-02-03", "2026-02-03T09:30:00", "2026-02-03T09:30:00Z",
                                   "2026-02-03T09:30:00+01:00", "2026-02-03 09:30:00+00:00",
                                   "2026-02-30T09:30:00+00:00", "2026-02-03T09:30:00.5+00:00", "", None, 1770000000,
                                   dt.datetime(2026, 2, 3, 9, 30, tzinfo=dt.timezone.utc)])
def test_invalid_timestamps_are_rejected(stamp):
    with pytest.raises(al.AttemptRecordError, match="attempted_at"):
        make(attempted_at=stamp)


@pytest.mark.parametrize("run_id", ["", "run-001", RUN.upper(), RUN.replace("-", ""), RUN + " ", 5])
def test_invalid_run_ids_are_rejected(run_id):
    with pytest.raises(al.AttemptRecordError, match="run_id"):
        make(run_id=run_id)


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64, "g" * 64, " " + "a" * 63, 5])
def test_invalid_hashes_are_rejected(digest):
    with pytest.raises(al.AttemptRecordError, match="pdf_sha256"):
        make(pdf_sha256=digest)


@pytest.mark.parametrize("count", [-1, (1 << 30) + 1, 1.5, "10", True])
def test_invalid_byte_counts_are_rejected(count):
    with pytest.raises(al.AttemptRecordError, match="pdf_bytes"):
        make(pdf_bytes=count)


@pytest.mark.parametrize("value", ["x", {"sha256_16": "a" * 16}, {"variant_index": 1},
                                   {"sha256_16": "A" * 16, "variant_index": 1}, {"sha256_16": "a" * 16, "variant_index": 0},
                                   {"sha256_16": "a" * 16, "variant_index": 100},
                                   {"sha256_16": "a" * 16, "variant_index": 1, "path": "C:\\x.pdf"}])
def test_invalid_preserved_copy_metadata_is_rejected(value):
    with pytest.raises(al.AttemptRecordError, match="previous_report_preserved_as"):
        make(previous_report_preserved_as=value)


@pytest.mark.parametrize("value", [1, 0, "yes", [True]])
def test_the_restored_flag_must_be_true_false_or_null(value):
    with pytest.raises(al.AttemptRecordError, match="previous_report_restored"):
        make(previous_report_restored=value)


def test_unknown_fields_and_missing_required_arguments_are_rejected():
    with pytest.raises(TypeError):
        make(note="free text")
    with pytest.raises(TypeError):
        al.build_attempt_record(client_key="k", report_period=PERIOD, attempted_at=TS, step="persist")
    with pytest.raises(TypeError):
        make(finalized=True)  # the flag is not an argument: it is always False


def test_a_hand_made_record_is_checked_by_the_same_validator(out):
    good = make()
    for tampered in (dict(good, extra=1), dict(good, finalized=True), {k: v for k, v in good.items() if k != "step"},
                     dict(good, record_type="finalized_report"), dict(good, status_code="nope"), None, [], "x"):
        before = out.names()
        with pytest.raises(al.AttemptRecordError):
            write(out, tampered)
        assert out.names() == before == []  # rejected before any file is created


def test_the_builder_does_not_change_its_arguments():
    preserved = {"sha256_16": "b" * 16, "variant_index": 2}
    period = {"start": "2026-01-01", "end": "2026-01-31"}
    before = (copy.deepcopy(preserved), copy.deepcopy(period))
    record = make(previous_report_preserved_as=preserved, report_period=period)
    record["previous_report_preserved_as"]["variant_index"] = 99
    record["report_period"]["start"] = "2000-01-01"
    assert (preserved, period) == before


def test_building_is_deterministic():
    one, two = make(run_id=RUN, pdf_sha256=SHA, pdf_bytes=5), make(run_id=RUN, pdf_sha256=SHA, pdf_bytes=5)
    assert one == two and al.attempt_file_name(one) == al.attempt_file_name(two)
    from seo_report.reviewer_record import to_canonical_json
    assert to_canonical_json(one) == to_canonical_json(two)


def test_validated_returns_the_same_object_for_a_good_record():
    record = make()
    assert al.validated(record) is record


# --- exception classes ---------------------------------------------------------------------------------------------------------

def test_only_the_class_name_of_an_exception_is_kept():
    secret = "client_secret=topsecret ya29.fake-token jane@example.test (555) 010-0199 C:\\Users\\me\\x.pdf"
    for error, expected in ((OSError(secret), "OSError"), (ValueError(secret), "ValueError"),
                            (ff.FileConflictError(secret, kind="destination_exists", operation="rename"),
                             "FileConflictError")):
        assert al.safe_exception_class(error) == expected
        record = make(exception_class=al.safe_exception_class(error))
        assert secret not in json.dumps(record) and "topsecret" not in json.dumps(record)


def test_class_names_the_schema_cannot_hold_fall_back_to_a_fixed_category():
    long_name = type("L" * 80, (Exception,), {})
    odd = type("\u00c9chec", (Exception,), {})
    assert al.safe_exception_class(long_name("x")) == "Exception"
    assert al.safe_exception_class(odd("x")) == "Exception"
    assert al.FALLBACK_EXCEPTION_CLASS == "Exception"
    with pytest.raises(TypeError):
        al.safe_exception_class("OSError")
    with pytest.raises(TypeError):
        al.safe_exception_class(OSError)  # a class, not an instance


@pytest.mark.parametrize("name", ["OSError: disk full", "os error", "Traceback (most recent call last)", "a.b.c.d.e.f.g",
                                  "1Error", "", "x" * 65, "jane@example.test"])
def test_free_text_cannot_be_passed_off_as_an_exception_class(name):
    with pytest.raises(al.AttemptRecordError, match="exception_class"):
        make(exception_class=name)


# --- file names and folders ------------------------------------------------------------------------------------------------------

def test_file_names_follow_the_approved_convention():
    record = make(pdf_sha256=SHA)
    assert al.attempt_file_name(record) == f"20260203T093000Z.{SHA[:16]}.persist.attempt.json"
    assert al.attempt_file_name(record, 2) == f"20260203T093000Z.{SHA[:16]}.persist.attempt.2.json"
    assert al.attempt_file_name(make()) == "20260203T093000Z.nopdf.persist.attempt.json"
    assert al.attempt_file_name(make(), 999).endswith(".attempt.999.json")
    for bad in (0, 1000, True, 2.0, "2", None):
        with pytest.raises(al.AttemptRecordError):
            al.attempt_file_name(make(), bad)
    assert al.MAX_COLLISION == 999 == layout.MAX_ATTEMPT_COLLISION


def test_every_step_appears_in_the_file_name():
    for step in STEPS:
        assert f".{step}.attempt" in al.attempt_file_name(make(step=step))


def test_the_folder_is_the_approved_attempts_folder(tmp_path):
    record = make()
    assert al.attempt_directory(tmp_path / "out", record) == tmp_path / "out" / "attempts" / "example-client" / "2026-01-01_2026-01-31"
    assert not (tmp_path / "out").exists()  # computing the location creates nothing
    with pytest.raises(al.AttemptRecordError, match="folder"):
        al.attempt_directory("relative/out", record)


# --- writing --------------------------------------------------------------------------------------------------------------------

def test_a_record_is_written_exclusively_as_canonical_json(out):
    record = make(run_id=RUN, pdf_sha256=SHA, pdf_bytes=2048, step="promote", status_code="promote_failed")
    result = write(out, record)
    assert result.name == f"20260203T093000Z.{SHA[:16]}.promote.attempt.json" and result.collision == 1
    text = (out.folder / result.name).read_text(encoding="utf-8")
    from seo_report.reviewer_record import to_canonical_json
    assert text == to_canonical_json(record) + "\n"
    assert result.byte_count == len(text.encode("utf-8")) and result.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert rs.validate_attempt_record(json.loads(text)) == record
    assert out.names() == [result.name]
    assert os.sep not in result.name and str(out.root) not in repr(result)


def test_repeated_attempts_never_overwrite_earlier_records(out):
    record = make()
    results = [write(out, record) for _ in range(4)]
    assert [r.collision for r in results] == [1, 2, 3, 4]
    assert [r.name for r in results] == [al.attempt_file_name(record, n) for n in (1, 2, 3, 4)]
    assert len(out.names()) == 4
    first = (out.folder / results[0].name).read_bytes()
    write(out, record)
    assert (out.folder / results[0].name).read_bytes() == first


def test_an_earlier_different_record_with_the_same_name_is_left_alone(out):
    name = al.attempt_file_name(make())
    (out.folder / name).write_bytes(b"an earlier record or someone else's file")
    result = write(out)
    assert result.collision == 2 and (out.folder / name).read_bytes() == b"an earlier record or someone else's file"


def test_different_steps_and_hashes_get_their_own_names(out):
    write(out, make(step="persist"))
    write(out, make(step="event", status_code="event_failed"))
    write(out, make(pdf_sha256=SHA))
    assert len(out.names()) == 3 and all(".attempt.json" in n for n in out.names())


def test_a_missing_attempts_folder_is_an_error_and_nothing_is_created(tmp_path):
    root = tmp_path / "out"
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(ff.FileOperationError):
        al.write_attempt_record(make(), output_root=root)
    assert not root.exists() and sorted(p.name for p in tmp_path.iterdir()) == before
    root.mkdir()
    with pytest.raises(ff.FileOperationError):
        al.write_attempt_record(make(), output_root=root)  # the period folders are still not created
    assert list(root.iterdir()) == []


def test_nothing_outside_the_attempts_folder_is_touched(out):
    sibling = out.root / "reviewer"
    sibling.mkdir()
    (sibling / "reviewer.x.json").write_bytes(b"keep")
    (out.root / "reports").mkdir()
    (out.root / "reports" / "report.pdf").write_bytes(b"%PDF-keep")
    before = out.state()
    write(out)
    write(out)
    after = out.state()
    changed = {k for k in after if k not in before}
    assert len(changed) == 2 and all(str(out.folder) in k for k in changed)
    assert {k: v for k, v in after.items() if k in before} == before


def test_an_invalid_record_creates_no_file(out):
    for bad in (dict(make(), status_code="nope"), dict(make(), client_key="BAD"), dict(make(), extra=1)):
        with pytest.raises(al.AttemptRecordError):
            write(out, bad)
    assert out.names() == []


def test_all_999_names_taken_is_a_clear_conflict(out):
    record = make()
    for n in range(1, 1000):
        (out.folder / al.attempt_file_name(record, n)).write_bytes(b"x")
    before = out.state()
    with pytest.raises(ff.FileConflictError) as raised:
        write(out, record)
    assert raised.value.kind == "variants_exhausted" and out.state() == before
    assert len(out.names()) == 999


# --- fault injection -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fail", ["write", "write_half", "sync", "close"])
def test_a_failure_while_writing_leaves_the_incomplete_file_and_reports_it(out, fail):
    with pytest.raises(ff.FileOperationError) as raised:
        write(out, ops=FaultOps(fail=fail))
    assert raised.value.left_behind is True and "injected" not in str(raised.value)
    assert out.names() == [al.attempt_file_name(make())]  # still there: never deleted, repaired or adopted


@pytest.mark.parametrize("fail", ["write", "write_half", "sync", "close"])
def test_the_next_attempt_after_an_incomplete_record_uses_the_next_name_and_keeps_the_old_file(out, fail):
    with pytest.raises(ff.FileOperationError):
        write(out, ops=FaultOps(fail=fail))
    incomplete = out.folder / al.attempt_file_name(make())
    before = incomplete.read_bytes()
    result = write(out)
    assert result.collision == 2 and incomplete.read_bytes() == before
    good = json.loads((out.folder / result.name).read_text(encoding="utf-8"))
    assert rs.validate_attempt_record(good) == make()


def test_a_half_written_record_is_really_incomplete_and_is_never_completed(out):
    with pytest.raises(ff.FileOperationError):
        write(out, ops=FaultOps(fail="write_half"))
    path = out.folder / al.attempt_file_name(make())
    data = path.read_bytes()
    assert 0 < len(data) < len(al.to_canonical_json(make())) and not data.endswith(b"\n")
    with pytest.raises(ValueError):
        json.loads(data.decode("utf-8"))
    write(out)
    assert path.read_bytes() == data  # untouched by the later attempt


def test_a_failure_before_the_file_exists_leaves_nothing(out):
    with pytest.raises(ff.FileOperationError) as raised:
        write(out, ops=FaultOps(fail="create_exclusive"))
    assert raised.value.left_behind is False and out.names() == []


def test_a_file_that_appears_between_the_check_and_the_create_is_not_overwritten(out):
    name = al.attempt_file_name(make())
    ops = FaultOps(hook=("create_exclusive", lambda: (out.folder / name).write_bytes(b"raced in")))
    result = write(out, ops=ops)
    assert result.collision == 2 and (out.folder / name).read_bytes() == b"raced in"
    assert out.names() == sorted([name, al.attempt_file_name(make(), 2)])


def test_an_error_on_the_first_name_is_not_hidden_by_trying_the_next(out):
    ops = FaultOps(fail="create_exclusive", error=PermissionError(13, "denied"))
    with pytest.raises(ff.FileOperationError) as raised:
        write(out, ops=ops)
    assert raised.value.cause_class == "PermissionError" and out.names() == []
    assert ops.calls.count("create_exclusive") == 1  # it did not keep trying other names


# --- privacy ------------------------------------------------------------------------------------------------------------------------

def test_a_persisted_record_holds_no_secrets_contacts_paths_or_exception_text(out):
    error = RuntimeError("Traceback (most recent call last): client_secret=topsecret access_token=ya29.x "
                         "jane@example.test (555) 010-0199 C:\\Users\\me\\report.pdf")
    record = make(exception_class=al.safe_exception_class(error), run_id=RUN, pdf_sha256=SHA, pdf_bytes=10,
                  step="record", status_code="record_failed")
    result = write(out, record)
    text = (out.folder / result.name).read_text(encoding="utf-8")
    for forbidden in ("topsecret", "ya29", "jane@example.test", "555", "Traceback", "C:\\", "report.pdf", "client_secret",
                      str(out.root), "token", "Users"):
        assert forbidden not in text, forbidden
    assert not re.search(r"[A-Za-z]:\\|\\\\|/home/|/Users/", text)
    parsed = json.loads(text)
    assert set(parsed) == set(rs.ATTEMPT_FIELDS) and parsed["exception_class"] == "RuntimeError"


def test_the_record_has_no_field_that_could_hold_free_text():
    free_text = [name for name in rs.ATTEMPT_FIELDS if name not in (
        "schema_version", "record_type", "client_key", "report_period", "attempted_at", "step", "status_code",
        "exception_class", "run_id", "pdf_sha256", "pdf_bytes", "previous_report_restored",
        "previous_report_preserved_as", "finalized")]
    assert free_text == []
    for field in ("message", "detail", "error", "path", "reason", "note"):
        assert field not in rs.ATTEMPT_FIELDS


def test_errors_use_fixed_categories_and_no_paths(out):
    messages = []
    for call in (lambda: al.write_attempt_record(make(), output_root=out.root.parent / "missing"),
                 lambda: write(out, dict(make(), status_code="nope")),
                 lambda: write(out, ops=FaultOps(fail="write"))):
        with pytest.raises((al.AttemptRecordError, ff.FinalizeFileError)) as raised:
            call()
        messages.append(str(raised.value))
    for message in messages:
        assert str(out.root) not in message and "injected" not in message and "WinError" not in message
        assert "Errno" not in message


# --- scope ----------------------------------------------------------------------------------------------------------------------------

def test_the_module_never_overwrites_deletes_or_creates_folders():
    source = (ROOT / "src" / "seo_report" / "attempt_log.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for pattern in (r"os\.replace", r"shutil", r"os\.remove", r"unlink", r"rmtree", r"rmdir", r"\.mkdir\(", r"makedirs",
                    r"open\(", r"write_text", r"write_bytes", r"\.now\(", r"today\(", r"\btime\.", r"sqlite", r"requests",
                    r"googleapiclient", r"environ", r"secrets\.", r"random"):
        assert not re.search(pattern, code), pattern


def test_the_only_write_path_is_the_exclusive_file_primitive(out, monkeypatch):
    calls = []
    original = ff.write_exclusive
    monkeypatch.setattr(al.ff, "write_exclusive", lambda *a, **k: calls.append(a[1]) or original(*a, **k))
    write(out)
    assert calls == [al.attempt_file_name(make())]
