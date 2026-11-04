"""R7.3a: schema version 2 finalized records (render-input identity): schema, builder, writer identity, reader.

Synthetic data and temporary folders only. Version 1 behaviour is covered by the existing suites; the tests here
check that it did not change and that version 2 sits next to it.
"""
import copy
import dataclasses
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import history_access as ha  # noqa: F401
from seo_report import output_layout as layout
from seo_report import record_schema as rs
from seo_report import record_writer as rw
from seo_report import render_input_hash as rh
from seo_report import reviewer_record as rr
from seo_report.record_schema import RecordSchemaError
from seo_report.reviewer_record import to_canonical_json
from test_history_access import P_B, env, excluded, finalize, listed, write_run  # noqa: F401
from test_record_schema import RUN, SHA_A, SHA_B, SHA_C, attempt, finalized, mutated, removed
from test_record_writer import AppearsBefore, NAME, data_of, filler, folder, listing  # noqa: F401
from test_render_input import sample
from test_reviewer_record import build, receipt  # noqa: F401

RENDER_SHA = "e" * 64
OTHER_RENDER_SHA = "f" * 64
FORMAT = {"name": "jasper.render-input", "version": 2}


def v2(record=None, render_sha=RENDER_SHA, fmt=None):
    value = copy.deepcopy(record if record is not None else finalized())
    value["schema_version"] = 2
    value["finalization"]["render_input_sha256"] = render_sha
    value["finalization"]["render_input_format"] = dict(FORMAT if fmt is None else fmt)
    return value


def fmt_receipt(sha=RENDER_SHA, version=2, name="jasper.render-input", **kw):
    return receipt(render_input_sha256=sha, render_input_format=rr.RenderInputFormat(name, version), **kw)


def bad(record):
    with pytest.raises(RecordSchemaError) as info:
        rs.validate_finalized_record(record)
    return str(info.value)


# --- constants and the exact version 1 structure -------------------------------------------------------------------------

def test_the_approved_constants():
    assert rs.FINALIZED_SCHEMA_VERSIONS == (1, 2)
    assert rs.CURRENT_FINALIZED_SCHEMA_VERSION == 2
    assert rs.ATTEMPT_SCHEMA_VERSION == 1 and rs.SCHEMA_VERSION == 1
    assert rs.FINALIZATION_FIELDS_V2 == rs.FINALIZATION_FIELDS + ("render_input_sha256", "render_input_format")
    assert rs.RENDER_INPUT_FORMAT_NAME == "jasper.render-input"
    assert rs.SUPPORTED_RENDER_INPUT_VERSIONS == (2,)


def test_the_version_1_field_structure_is_exactly_the_original():
    assert rs.FINALIZATION_FIELDS == (
        "finalized_at", "pdf_sha256", "pdf_bytes", "pdf_checks", "client_content_sha256", "rendered_event",
        "superseded_previous_pdf", "duplicate_draft_preserved_as")
    record = finalized()
    assert rs.validate_finalized_record(record) is record
    assert set(record["finalization"]) == set(rs.FINALIZATION_FIELDS)
    assert set(rs.FINALIZED_FIELDS) == set(v2()) == set(record)  # the top level is the same in both versions


def test_the_render_input_format_pin_agrees_with_render_input_hash():
    assert rs.RENDER_INPUT_FORMAT_NAME == rh.FORMAT_NAME
    assert rh.FORMAT_VERSION in rs.SUPPORTED_RENDER_INPUT_VERSIONS
    digest = rh.render_input_sha256(sample())
    record = v2(render_sha=digest, fmt={"name": rh.FORMAT_NAME, "version": rh.FORMAT_VERSION})
    assert rs.validate_finalized_record(record) is record


# --- version 2 validation ----------------------------------------------------------------------------------------------------------------

def test_a_valid_version_2_record_validates_and_keeps_client_content_sha256():
    record = v2()
    assert rs.validate_finalized_record(record) is record
    assert record["finalization"]["client_content_sha256"] == SHA_C
    assert rs.record_version(record) == 2 and rs.record_version(finalized()) == 1


@pytest.mark.parametrize("field", ["render_input_sha256", "render_input_format"])
def test_version_2_requires_both_new_fields(field):
    message = bad(removed(v2(), ["finalization", field]))
    assert field in message and "schema_version 2" in message


@pytest.mark.parametrize("field,value", [("render_input_sha256", RENDER_SHA), ("render_input_format", FORMAT)])
def test_version_1_rejects_version_2_fields(field, value):
    record = finalized()
    record["finalization"][field] = value
    assert field in bad(record) and "schema_version 1" in bad(record)


def test_version_1_with_both_extra_fields_is_rejected_and_stays_version_1():
    record = finalized()
    record["finalization"].update(render_input_sha256=RENDER_SHA, render_input_format=FORMAT)
    bad(record)


def test_a_version_1_shaped_record_claiming_version_2_is_rejected_naming_the_version():
    record = finalized()
    record["schema_version"] = 2
    assert "schema_version" in bad(record)


def test_a_version_2_shaped_record_claiming_version_1_is_rejected():
    record = v2()
    record["schema_version"] = 1
    bad(record)


@pytest.mark.parametrize("version", [0, 3, -1, 99, "1", "2", 1.0, 2.0, True, False, None, [2], {"v": 2}])
def test_unsupported_or_mistyped_schema_versions_are_rejected(version):
    for base in (finalized(), v2()):
        assert "schema_version" in bad(mutated(base, ["schema_version"], version))
    with pytest.raises(RecordSchemaError):
        rs.record_version({"schema_version": version})


@pytest.mark.parametrize("value", ["E" * 64, "e" * 63, "e" * 65, "g" * 64, "", " " + "e" * 63, "e" * 64 + "\n",
                                   None, 5, True, ["e" * 64], "sha256:" + "e" * 57])
def test_invalid_render_input_hashes_are_rejected(value):
    assert "render_input_sha256" in bad(mutated(v2(), ["finalization", "render_input_sha256"], value))


@pytest.mark.parametrize("name", ["other", "", "Jasper.render-input", "jasper.render-input ", None, 2, True])
def test_invalid_render_input_format_names_are_rejected(name):
    assert "render_input_format.name" in bad(mutated(v2(), ["finalization", "render_input_format", "name"], name))


@pytest.mark.parametrize("version", [0, 1, 3, 99, -2, "2", 2.0, True, False, None, [2]])
def test_invalid_render_input_format_versions_are_rejected(version):
    assert "render_input_format.version" in bad(
        mutated(v2(), ["finalization", "render_input_format", "version"], version))


@pytest.mark.parametrize("value", [None, [], "jasper.render-input", 2, {"name": "jasper.render-input"},
                                   {"version": 2}, {"name": "jasper.render-input", "version": 2, "extra": 1}, {}])
def test_the_render_input_format_is_a_closed_object(value):
    bad(mutated(v2(), ["finalization", "render_input_format"], value))


def test_the_record_schema_version_is_not_the_render_input_format_version():
    record = v2(fmt={"name": "jasper.render-input", "version": 1})  # format 1 is not supported
    bad(record)
    assert rs.validate_finalized_record(v2(fmt={"name": "jasper.render-input", "version": 2}))["schema_version"] == 2


def test_version_2_keeps_every_other_version_1_check():
    for path, value in ((["finalization", "pdf_sha256"], "x"), (["finalization", "pdf_bytes"], True),
                        (["finalization", "client_content_sha256"], "bad"), (["finalization", "pdf_bytes"], 0),
                        (["snapshot", "quality"], "great"), (["client_key"], "Bad Key!")):
        bad(mutated(v2(), path, value))
    removed_client = removed(v2(), ["finalization", "client_content_sha256"])
    assert "client_content_sha256" in bad(removed_client)


# --- the attempt record stays at version 1 ---------------------------------------------------------------------------------------

def test_attempt_records_remain_version_1_only():
    record = attempt()
    assert rs.validate_attempt_record(record) is record and record["schema_version"] == rs.ATTEMPT_SCHEMA_VERSION == 1
    for version in (0, 2, 3, "1", 1.0, True, None):
        with pytest.raises(RecordSchemaError, match="schema_version"):
            rs.validate_attempt_record(mutated(attempt(), ["schema_version"], version))


def test_attempt_records_reject_finalization_fields_and_finalized_records_are_not_attempts():
    record = attempt()
    record["render_input_sha256"] = RENDER_SHA
    with pytest.raises(RecordSchemaError):
        rs.validate_attempt_record(record)
    with pytest.raises(RecordSchemaError):
        rs.validate_attempt_record(v2())
    with pytest.raises(RecordSchemaError):
        rs.validate_finalized_record(attempt())


# --- the builder -------------------------------------------------------------------------------------------------------------------------

def test_without_render_identity_the_builder_makes_the_exact_version_1_record(env):
    record = build(env)
    assert record["schema_version"] == 1
    assert set(record["finalization"]) == set(rs.FINALIZATION_FIELDS)
    assert set(record) == set(rs.FINALIZED_FIELDS)
    assert "render_input_sha256" not in record["finalization"]


def test_with_both_receipt_fields_the_builder_makes_a_version_2_record(env):
    one, two = build(env), build(env, receipt=fmt_receipt())
    assert two["schema_version"] == 2
    assert set(two["finalization"]) == set(rs.FINALIZATION_FIELDS_V2)
    assert two["finalization"]["render_input_sha256"] == RENDER_SHA
    assert two["finalization"]["render_input_format"] == FORMAT
    assert two["finalization"]["client_content_sha256"] == SHA_C == one["finalization"]["client_content_sha256"]
    assert rs.validate_finalized_record(two) is two
    stripped = copy.deepcopy(two)
    stripped["schema_version"] = 1
    del stripped["finalization"]["render_input_sha256"], stripped["finalization"]["render_input_format"]
    assert stripped == one  # nothing else differs between the two versions


def test_the_version_2_record_serializes_canonically_and_round_trips(env):
    record = build(env, receipt=fmt_receipt())
    text = to_canonical_json(record)
    assert json.loads(text) == record and text == to_canonical_json(copy.deepcopy(record))


def test_supplying_only_one_receipt_field_is_refused(env):
    with pytest.raises(rr.ReviewerRecordError, match="together"):
        build(env, receipt=receipt(render_input_sha256=RENDER_SHA))
    with pytest.raises(rr.ReviewerRecordError, match="together"):
        build(env, receipt=receipt(render_input_format=rr.RenderInputFormat("jasper.render-input", 2)))


def test_a_non_format_object_is_refused(env):
    with pytest.raises(TypeError):
        build(env, receipt=receipt(render_input_sha256=RENDER_SHA, render_input_format=dict(FORMAT)))


@pytest.mark.parametrize("kwargs", [dict(sha="E" * 64), dict(sha="e" * 63), dict(sha=5), dict(name="other"),
                                    dict(version=1), dict(version=True), dict(version=2.0), dict(version="2")])
def test_the_builder_validates_the_render_identity_with_the_version_2_schema(env, kwargs):
    with pytest.raises(rr.ReviewerRecordError, match="not valid"):
        build(env, receipt=fmt_receipt(**kwargs))


def test_the_receipt_is_immutable_and_defaults_keep_existing_callers_working():
    assert receipt().render_input_sha256 is None and receipt().render_input_format is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt().render_input_sha256 = RENDER_SHA


# --- identity ------------------------------------------------------------------------------------------------------------------------------

def test_the_version_1_identity_is_exactly_the_original_six_values():
    record = finalized()
    assert rw.record_identity(record) == (RUN, 1, SHA_B, SHA_A, 123456, SHA_C)
    assert rw.IDENTITY_FIELDS == (("snapshot", "run_id"), ("snapshot", "revision"), ("snapshot", "content_hash"),
                                  ("finalization", "pdf_sha256"), ("finalization", "pdf_bytes"),
                                  ("finalization", "client_content_sha256"))


def test_the_version_2_identity_adds_the_render_input_hash_and_format():
    assert rw.record_identity(v2()) == (RUN, 1, SHA_B, SHA_A, 123456, SHA_C, RENDER_SHA, "jasper.render-input", 2)


def test_different_render_input_hashes_give_different_identities():
    assert rw.record_identity(v2(render_sha=RENDER_SHA)) != rw.record_identity(v2(render_sha=OTHER_RENDER_SHA))


def test_the_render_input_format_is_part_of_the_identity():
    other = v2(fmt={"name": "jasper.render-input", "version": 3})  # identity only; not schema-validated here
    assert rw.record_identity(other) != rw.record_identity(v2())


def test_version_1_and_version_2_identities_never_compare_equal():
    assert rw.record_identity(finalized()) != rw.record_identity(v2())
    assert len(rw.record_identity(finalized())) == 6 and len(rw.record_identity(v2())) == 9
    assert rw.record_identity(v2())[:6] == rw.record_identity(finalized())


def test_identity_still_ignores_finalized_at_and_other_fields():
    later = mutated(v2(), ["finalization", "finalized_at"], "2027-01-01T00:00:00+00:00")
    later["notes"] = []
    assert rw.record_identity(later) == rw.record_identity(v2())


# --- the writer ---------------------------------------------------------------------------------------------------------------------------------

def test_a_version_2_record_is_written_with_the_same_file_name_and_canonical_bytes(folder):
    record = v2()
    result = rw.write_reviewer_record(folder, record)
    assert (result.outcome, result.written, result.name) == (rw.OUTCOME_WRITTEN, True, NAME)
    assert listing(folder) == {NAME: data_of(record)}
    assert rw.parse_record_bytes((folder / NAME).read_bytes()) == record
    assert not [n for n in listing(folder) if n != NAME]


def test_the_written_version_2_record_is_not_changed_by_the_writer(folder):
    record = v2()
    before = copy.deepcopy(record)
    rw.write_reviewer_record(folder, record)
    assert record == before


def test_the_same_version_2_record_again_is_already_written_even_with_a_new_timestamp(folder):
    rw.write_reviewer_record(folder, v2())
    first = listing(folder)
    again = rw.write_reviewer_record(folder, mutated(v2(), ["finalization", "finalized_at"],
                                                     "2027-01-01T00:00:00+00:00"))
    assert again.outcome == rw.OUTCOME_ALREADY and not again.written and listing(folder) == first


def test_a_different_render_input_hash_at_the_same_name_is_a_conflict_and_nothing_changes(folder):
    rw.write_reviewer_record(folder, v2())
    first = listing(folder)
    result = rw.write_reviewer_record(folder, v2(render_sha=OTHER_RENDER_SHA))
    assert (result.outcome, result.conflict) == (rw.OUTCOME_CONFLICT, "record_identity_differs")
    assert listing(folder) == first


def test_a_version_1_record_is_never_replaced_by_a_version_2_record_and_the_reverse(folder):
    rw.write_reviewer_record(folder, finalized())
    first = listing(folder)
    result = rw.write_reviewer_record(folder, v2())
    assert (result.outcome, result.conflict) == (rw.OUTCOME_CONFLICT, "record_identity_differs")
    assert listing(folder) == first
    other = folder.parent / "other"
    other.mkdir()
    rw.write_reviewer_record(other, v2())
    second = listing(other)
    result = rw.write_reviewer_record(other, finalized())
    assert (result.outcome, result.conflict) == (rw.OUTCOME_CONFLICT, "record_identity_differs")
    assert listing(other) == second


def test_a_version_1_record_is_still_written_and_idempotent_exactly_as_before(folder):
    assert rw.write_reviewer_record(folder, finalized()).outcome == rw.OUTCOME_WRITTEN
    assert rw.write_reviewer_record(folder, finalized()).outcome == rw.OUTCOME_ALREADY
    assert rw.record_identity(rw.parse_record_bytes((folder / NAME).read_bytes())) == rw.record_identity(finalized())


@pytest.mark.parametrize("change", [lambda r: r["finalization"].pop("render_input_format"),
                                    lambda r: r["finalization"].update(render_input_sha256="E" * 64),
                                    lambda r: r["finalization"]["render_input_format"].update(version=True),
                                    lambda r: r.update(schema_version=3)])
def test_a_malformed_version_2_record_is_refused_and_nothing_is_written(folder, change):
    record = v2()
    change(record)
    result = rw.write_reviewer_record(folder, record)
    assert (result.outcome, result.invalid) == (rw.OUTCOME_INVALID, "record_invalid")
    assert listing(folder) == {}


def test_a_malformed_existing_version_2_record_is_never_accepted_or_replaced(folder):
    broken = v2()
    broken["finalization"]["render_input_sha256"] = "bad"
    (folder / NAME).write_bytes(data_of(broken))
    before = listing(folder)
    result = rw.write_reviewer_record(folder, v2())
    assert (result.outcome, result.conflict) == (rw.OUTCOME_CONFLICT, "existing_record_invalid")
    assert listing(folder) == before


def test_a_record_that_appears_during_the_write_is_reconciled_by_version_2_identity(folder):
    other = v2(render_sha=OTHER_RENDER_SHA)
    ops = AppearsBefore("rename_noreplace", 1, lambda: (folder / NAME).write_bytes(data_of(other)))
    result = rw.write_reviewer_record(folder, v2(), ops=ops)
    assert (result.outcome, result.conflict) == (rw.OUTCOME_CONFLICT, "record_identity_differs")
    assert (folder / NAME).read_bytes() == data_of(other)


def test_the_20_record_limit_is_unchanged_for_version_2_records(folder):
    filler(folder, 19)
    assert rw.write_reviewer_record(folder, v2()).outcome == rw.OUTCOME_WRITTEN
    other = folder.parent / "full"
    other.mkdir()
    filler(other, 20)
    result = rw.write_reviewer_record(other, v2())
    assert (result.outcome, result.conflict, result.existing_records) == (rw.OUTCOME_CONFLICT,
                                                                           "reader_record_limit", 20)
    assert len(list(other.iterdir())) == 20 and NAME not in listing(other)


def test_the_file_name_convention_is_unchanged(folder):
    assert NAME == layout.reviewer_record_name(RUN, SHA_A)
    assert rw.write_reviewer_record(folder, v2()).name == NAME == f"reviewer.{RUN}.{SHA_A[:16]}.json"


# --- the finalized-only reader (history_access.py is unchanged) --------------------------------------------------------------------------

def convert(**options):
    def mutate(rec):
        rec["schema_version"] = 2
        rec["finalization"]["render_input_sha256"] = options.get("sha", RENDER_SHA)
        rec["finalization"]["render_input_format"] = dict(options.get("fmt", FORMAT))
        for change in options.get("changes", ()):
            change(rec)
    return mutate


def test_the_reader_accepts_a_valid_version_1_record(env):
    run = write_run(env)
    finalize(env, run)
    assert excluded(env.reader()) == {} and listed(env.reader()) == [run]


def test_the_reader_accepts_a_valid_version_2_record(env):
    run = write_run(env)
    finalize(env, run, mutate=convert())
    assert excluded(env.reader()) == {} and listed(env.reader()) == [run]


def test_the_reader_accepts_a_version_1_and_a_version_2_record_for_different_runs(env):
    first, second = write_run(env), write_run(env, P_B)
    finalize(env, first)
    finalize(env, second, mutate=convert())
    assert excluded(env.reader()) == {} and sorted(listed(env.reader())) == sorted([first, second])


@pytest.mark.parametrize("change", [
    lambda rec: rec.update(schema_version=3),
    lambda rec: rec.update(schema_version="2"),
    lambda rec: rec.update(schema_version=True),
    lambda rec: rec["finalization"].pop("render_input_sha256"),
    lambda rec: rec["finalization"].pop("render_input_format"),
    lambda rec: rec["finalization"].update(render_input_sha256="E" * 64),
    lambda rec: rec["finalization"]["render_input_format"].update(name="other"),
    lambda rec: rec["finalization"]["render_input_format"].update(version=1),
    lambda rec: rec["finalization"]["render_input_format"].update(version=True),
    lambda rec: rec["finalization"].update(unexpected=1)])
def test_the_reader_rejects_malformed_or_unsupported_version_2_records(env, change):
    run = write_run(env)
    finalize(env, run, mutate=convert(changes=(change,)))
    assert excluded(env.reader()) == {run: "record_invalid"}


def test_the_reader_rejects_a_version_1_record_that_carries_version_2_fields(env):
    run = write_run(env)

    def extra(rec):
        rec["finalization"]["render_input_sha256"] = RENDER_SHA
    finalize(env, run, mutate=extra)
    assert excluded(env.reader()) == {run: "record_invalid"}


def test_the_reader_still_verifies_the_pdf_hash_for_version_2_records(env):
    run = write_run(env)
    finalize(env, run, mutate=convert(), disk_pdf=b"%PDF-1.4 a different file\n%%EOF\n")
    assert excluded(env.reader()) == {run: "pdf_hash_mismatch"}


def test_the_reader_still_verifies_the_pdf_byte_count_for_version_2_records(env):
    run = write_run(env)

    def wrong_size(rec):
        convert()(rec)
        rec["finalization"]["pdf_bytes"] += 1
    finalize(env, run, mutate=wrong_size)
    assert excluded(env.reader()) == {run: "pdf_hash_mismatch"}


def test_the_reader_still_requires_the_pdf_and_the_rendered_event_for_version_2_records(env):
    first = write_run(env)
    finalize(env, first, mutate=convert(), pdf_name=None)
    assert excluded(env.reader()) == {first: "final_pdf_missing"}


def test_the_reader_still_requires_a_rendered_event_for_version_2_records(env):
    run = write_run(env)
    finalize(env, run, mutate=convert(), event=False)
    assert excluded(env.reader()) == {run: "no_rendered_event"}


def test_the_reader_still_matches_the_stored_run_for_version_2_records(env):
    run = write_run(env)
    finalize(env, run, mutate=convert(changes=(lambda rec: rec["snapshot"].update(revision=7),)))
    assert excluded(env.reader()) == {run: "record_mismatch"}


def test_reading_leaves_version_2_record_files_unchanged(env):
    run = write_run(env)
    _, path = finalize(env, run, mutate=convert())
    before = path.read_bytes()
    listed(env.reader())
    excluded(env.reader())
    assert path.read_bytes() == before
