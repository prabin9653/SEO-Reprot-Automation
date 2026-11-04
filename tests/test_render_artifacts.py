"""R7.2: durable render identity (sidecar) and safe draft handoff. Synthetic, offline, temporary folders only."""
import copy
import dataclasses
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seo_report import finalize_files as ff
from seo_report import pdf_renderer as pr
from seo_report import render_artifacts as ra
from seo_report import render_input as ri
from seo_report import render_input_hash as rh
from test_finalize_files import FaultOps
from test_render_input import sample

BROWSER = "156.0.8078.4"
DRAFT = "report.unarchived.pdf"


def make_input(day=dt.date(2026, 7, 5)):
    return dataclasses.replace(sample(), generation_date=day)


def tiny_pdf(tag: str) -> bytes:
    return b"%PDF-1.7\n% synthetic " + tag.encode() + b"\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class StubRenderer:
    """Writes a tiny synthetic PDF whose bytes are a function of the render input alone."""

    def __init__(self, issues=(), identity_override=None, salt=""):
        self.calls, self.issues, self.identity_override, self.salt = [], issues, identity_override, salt

    def __call__(self, render_input, *, workdir, logo_path, fonts):
        self.calls.append(render_input)
        digest = rh.render_input_sha256(render_input)
        data = tiny_pdf(digest + self.salt)
        path = Path(workdir) / "report.pdf"
        path.write_bytes(data)
        return SimpleNamespace(ok=not self.issues, pdf_path=path, pdf_sha256=hashlib.sha256(data).hexdigest(),
                               byte_count=len(data), browser_version=BROWSER,
                               render_input_sha256=self.identity_override or digest, issues=self.issues)


@pytest.fixture
def folders(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    return reports, work


def run(reports, work, candidate=None, renderer=None, **kw):
    work_dir = Path(work)
    if any(work_dir.iterdir()):
        work_dir = work_dir.parent / f"work{len(list(work_dir.parent.iterdir()))}"
        work_dir.mkdir()
    return ra.produce_report_draft(reports, candidate or make_input(), workdir=work_dir, logo_path=None, fonts=(),
                                   renderer=renderer or StubRenderer(), **kw)


def listing(folder):
    return {p.name: p.read_bytes() for p in Path(folder).iterdir()}


def sidecar_bytes(value=None, pdf=None):
    value = value or make_input()
    data = pdf or tiny_pdf(rh.render_input_sha256(value))
    return ra.build_sidecar_bytes(value, renderer=ra.renderer_identity(BROWSER),
                                  pdf_sha256=hashlib.sha256(data).hexdigest(), pdf_bytes=len(data))


# --- sidecar format ---------------------------------------------------------------------------------------------------------------

def test_the_sidecar_is_canonical_json_with_the_documented_members():
    data = sidecar_bytes()
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    document = json.loads(data)
    assert list(document) == sorted(document)
    assert set(document) == set(ra._MEMBERS)
    text = data.decode("utf-8")[:-1]
    assert text == json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def test_the_embedded_payload_is_the_exact_hashed_object():
    value = make_input()
    document = json.loads(sidecar_bytes(value))
    assert document["render_input"] == rh.build_payload(value)
    canonical = json.dumps(document["render_input"], sort_keys=True, ensure_ascii=False, allow_nan=False,
                           separators=(",", ":"))
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == document["render_input_sha256"]
    assert document["render_input_sha256"] == rh.render_input_sha256(value)


def test_the_sidecar_records_date_template_chromium_and_environment():
    value = make_input()
    document = json.loads(sidecar_bytes(value))
    assert document["generation_date"] == "2026-07-05"
    assert document["template_fingerprint"] == value.fingerprints.template_version
    assert document["renderer"]["browser"] == BROWSER
    assert set(document["renderer"]) == {"browser", "playwright", "python", "system"}
    assert document["render_input_format"] == {"name": rh.FORMAT_NAME, "version": rh.FORMAT_VERSION}


def test_the_sidecar_holds_no_machine_path_or_credential_fields():
    text = sidecar_bytes().decode("utf-8").lower()
    for word in ("password", "token", "secret", "api_key", "credential", "c:\\", "users\\"):
        assert word not in text


def test_the_name_is_deterministic_from_the_hash():
    digest = rh.render_input_sha256(make_input())
    assert ra.sidecar_name(digest) == f"report.render-input.{digest[:16]}.json"
    assert ra.sidecar_name(digest) == ra.sidecar_name(digest)
    other = rh.render_input_sha256(make_input(dt.date(2026, 7, 6)))
    assert ra.sidecar_name(other) != ra.sidecar_name(digest)
    with pytest.raises(ra.RenderArtifactError):
        ra.sidecar_name("abc")


# --- round trip --------------------------------------------------------------------------------------------------------------------

def test_parsing_reconstructs_an_input_with_the_same_hash():
    value = make_input()
    parsed = ra.parse_sidecar(sidecar_bytes(value), expected_name=ra.sidecar_name(rh.render_input_sha256(value)))
    assert rh.render_input_sha256(parsed.render_input) == rh.render_input_sha256(value)
    assert parsed.generation_date == dt.date(2026, 7, 5)
    assert parsed.template_fingerprint == value.fingerprints.template_version
    assert rh.canonical_render_input_json(parsed.render_input) == rh.canonical_render_input_json(value)
    assert parsed.render_input.operational == ri.RenderOperationalMetadata()  # operational data is never stored


def test_the_round_trip_equals_the_original_apart_from_operational_metadata():
    value = make_input()
    rebuilt = ra.parse_sidecar(sidecar_bytes(value)).render_input
    assert dataclasses.replace(value, operational=ri.RenderOperationalMetadata()) == rebuilt


def test_a_full_report_round_trips(tmp_path):
    from test_report_document import full
    value = full()
    data = ra.build_sidecar_bytes(value, renderer=ra.renderer_identity(BROWSER), pdf_sha256="a" * 64, pdf_bytes=10)
    assert rh.render_input_sha256(ra.parse_sidecar(data).render_input) == rh.render_input_sha256(value)


# --- strict reading ------------------------------------------------------------------------------------------------------------------

def mutate(change):
    document = json.loads(sidecar_bytes())
    change(document)
    return (json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def code_of(data, **kw):
    with pytest.raises(ra.RenderArtifactError) as info:
        ra.parse_sidecar(data, **kw)
    return info.value.code


def test_malformed_json_and_encodings_are_rejected():
    assert code_of(b"") == "sidecar_malformed"
    assert code_of(b"{not json") == "sidecar_malformed"
    assert code_of(b"\xff\xfe\x00") == "sidecar_malformed"
    assert code_of(b"[]") == "sidecar_malformed"
    assert code_of(b'{"a":1,"a":2}') == "sidecar_malformed"
    assert code_of(b'{"a":NaN}') == "sidecar_malformed"
    assert code_of("x".encode() * 1) == "sidecar_malformed"
    assert code_of("not bytes") == "sidecar_unreadable"


def test_missing_and_extra_members_are_rejected():
    for member in ra._MEMBERS:
        assert code_of(mutate(lambda d, m=member: d.pop(m))) == "sidecar_malformed"
    assert code_of(mutate(lambda d: d.update(extra=1))) == "sidecar_malformed"


def test_unsupported_versions_are_rejected():
    assert code_of(mutate(lambda d: d.update(version=2))) == "unsupported_version"
    assert code_of(mutate(lambda d: d.update(schema="other"))) == "unsupported_version"
    assert code_of(mutate(lambda d: d.update(render_input_format={"name": rh.FORMAT_NAME, "version": 1}))) \
        == "unsupported_version"
    assert code_of(mutate(lambda d: d.update(version=True))) == "unsupported_version"


def test_a_wrong_hash_is_rejected():
    assert code_of(mutate(lambda d: d.update(render_input_sha256="0" * 64))) == "hash_mismatch"
    assert code_of(mutate(lambda d: d.update(render_input_sha256="xyz"))) == "sidecar_malformed"
    assert code_of(mutate(lambda d: d["render_input"]["input"]["client"].update(name="Someone Else"))) == "hash_mismatch"


def test_a_payload_edited_together_with_its_hash_is_still_checked_against_the_model():
    def change(d):
        d["render_input"]["input"]["client"]["unknown"] = "x"
        canonical = json.dumps(d["render_input"], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        d["render_input_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    assert code_of(mutate(change)) == "payload_invalid"


def test_identity_members_that_disagree_with_the_payload_are_rejected():
    assert code_of(mutate(lambda d: d.update(generation_date="2026-07-06"))) == "identity_mismatch"
    assert code_of(mutate(lambda d: d.update(template_fingerprint="t-0000000000000000"))) == "identity_mismatch"
    assert code_of(sidecar_bytes(), expected_name="report.render-input.0000000000000000.json") == "identity_mismatch"


def test_bad_renderer_and_pdf_members_are_rejected():
    assert code_of(mutate(lambda d: d["renderer"].update(browser=""))) == "sidecar_malformed"
    assert code_of(mutate(lambda d: d["renderer"].pop("system"))) == "sidecar_malformed"
    assert code_of(mutate(lambda d: d["pdf"].update(sha256="1234"))) == "sidecar_malformed"
    assert code_of(mutate(lambda d: d["pdf"].update(bytes=0))) == "sidecar_malformed"
    assert code_of(mutate(lambda d: d["pdf"].update(bytes=True))) == "sidecar_malformed"


def test_errors_carry_fixed_codes_and_no_client_data():
    value = make_input()
    secrets_ = [value.client.name, value.client.website]
    bad = [mutate(lambda d: d["render_input"]["input"]["client"].update(name="X")),
           mutate(lambda d: d.update(render_input_sha256="0" * 64)), b"{" + value.client.name.encode()]
    for data in bad:
        with pytest.raises(ra.RenderArtifactError) as info:
            ra.parse_sidecar(data)
        shown = str(info.value) + info.value.code + info.value.group
        assert all(s not in shown for s in secrets_ if s)


def test_the_loader_is_strict_about_types_and_keys():
    payload = rh.build_payload(make_input())
    for change in (lambda p: p["input"].update(generation_date="2026-7-5"),
                   lambda p: p["input"].update(generation_date=20260705),
                   lambda p: p["input"].pop("keywords"),
                   lambda p: p["input"].update(operational={}),
                   lambda p: p.update(version=99)):
        broken = copy.deepcopy(payload)
        change(broken)
        with pytest.raises(ra.RenderArtifactError):
            ra.render_input_from_payload(broken)
    with pytest.raises(ra.RenderArtifactError):
        ra.render_input_from_payload([])


# --- exclusive writing -----------------------------------------------------------------------------------------------------------------

def test_the_sidecar_is_created_exclusively_and_validated(folders):
    reports, _ = folders
    data = sidecar_bytes()
    result = ra.write_sidecar(reports, data)
    assert result.outcome == "written" and (reports / result.name).read_bytes() == data
    assert ra.read_sidecar(reports, result.name).render_input_sha256 == rh.render_input_sha256(make_input())


def test_writing_never_creates_the_reports_directory(tmp_path):
    with pytest.raises(ra.RenderArtifactError) as info:
        ra.write_sidecar(tmp_path / "missing", sidecar_bytes())
    assert info.value.code == "reports_dir_missing" and not (tmp_path / "missing").exists()
    with pytest.raises(ra.RenderArtifactError):
        ra.write_sidecar("relative", sidecar_bytes())
    plain = tmp_path / "file"
    plain.write_text("x")
    with pytest.raises(ra.RenderArtifactError):
        ra.write_sidecar(plain, sidecar_bytes())


def test_an_identical_existing_sidecar_is_idempotent(folders):
    reports, _ = folders
    data = sidecar_bytes()
    ra.write_sidecar(reports, data)
    assert ra.write_sidecar(reports, data).outcome == "already_present"
    assert len(list(reports.iterdir())) == 1


def test_a_different_existing_sidecar_is_preserved_as_a_conflict(folders):
    reports, _ = folders
    value = make_input()
    first = sidecar_bytes(value)
    ra.write_sidecar(reports, first)
    other_pdf = tiny_pdf("other")
    second = sidecar_bytes(value, pdf=other_pdf)
    assert second != first
    with pytest.raises(ra.RenderArtifactError) as info:
        ra.write_sidecar(reports, second)
    assert info.value.code == "sidecar_exists_differs"
    assert (reports / ra.sidecar_name(rh.render_input_sha256(value))).read_bytes() == first
    assert len(list(reports.iterdir())) == 1


def test_invalid_bytes_are_never_written(folders):
    reports, _ = folders
    with pytest.raises(ra.RenderArtifactError):
        ra.write_sidecar(reports, b"{}")
    assert list(reports.iterdir()) == []


@pytest.mark.parametrize("fail", ["write", "write_half", "sync"])
def test_an_interrupted_write_keeps_the_partial_and_creates_no_sidecar(folders, fail):
    reports, _ = folders
    with pytest.raises(ra.RenderArtifactError) as info:
        ra.write_sidecar(reports, sidecar_bytes(), ops=FaultOps(fail=fail))
    assert info.value.code == "sidecar_write_incomplete"
    names = [p.name for p in reports.iterdir()]
    assert len(names) == 1 and ff.is_partial_name(names[0])
    assert info.value.partial_names == tuple(names)
    assert not any(ra.SIDECAR_NAME.fullmatch(n) for n in names)


def test_a_sidecar_that_changes_after_writing_is_reported(folders):
    reports, _ = folders
    data = sidecar_bytes()
    name = ra.sidecar_name(rh.render_input_sha256(make_input()))

    def damage():
        (reports / name).write_bytes(b"damaged")
    with pytest.raises(ra.RenderArtifactError) as info:
        ra.write_sidecar(reports, data, ops=FaultOps(hook=("open_read", damage)))
    assert info.value.code == "sidecar_invalid_after_write"


def test_reading_validates_the_file_name_and_content(folders):
    reports, _ = folders
    with pytest.raises(ra.RenderArtifactError):
        ra.read_sidecar(reports, "../x.json")
    name = "report.render-input." + "0" * 16 + ".json"
    (reports / name).write_bytes(sidecar_bytes())
    with pytest.raises(ra.RenderArtifactError) as info:
        ra.read_sidecar(reports, name)
    assert info.value.code == "identity_mismatch"
    with pytest.raises(ra.RenderArtifactError):
        ra.read_sidecar(reports, "report.render-input." + "1" * 16 + ".json")


# --- draft handoff ---------------------------------------------------------------------------------------------------------------------

def source_pdf(tmp_path, tag="a"):
    folder = tmp_path / "render"
    folder.mkdir(exist_ok=True)
    path = folder / "report.pdf"
    path.write_bytes(tiny_pdf(tag))
    return path


def test_the_pdf_is_copied_with_matching_hashes_and_the_source_is_kept(tmp_path, folders):
    reports, _ = folders
    source = source_pdf(tmp_path)
    before = source.read_bytes()
    result = ra.handoff_draft(source, reports)
    assert result.outcome == "copied"
    assert (reports / DRAFT).read_bytes() == before == source.read_bytes()
    assert result.pdf_sha256 == hashlib.sha256(before).hexdigest() and result.pdf_bytes == len(before)
    assert [p.name for p in reports.iterdir()] == [DRAFT]


def test_an_existing_identical_draft_is_left_alone(tmp_path, folders):
    reports, _ = folders
    source = source_pdf(tmp_path)
    (reports / DRAFT).write_bytes(source.read_bytes())
    assert ra.handoff_draft(source, reports).outcome == "already_present"


def test_an_existing_different_draft_is_never_overwritten(tmp_path, folders):
    reports, _ = folders
    source = source_pdf(tmp_path)
    (reports / DRAFT).write_bytes(tiny_pdf("existing"))
    with pytest.raises(ra.RenderArtifactError) as info:
        ra.handoff_draft(source, reports)
    assert info.value.code == "draft_exists_differs"
    assert (reports / DRAFT).read_bytes() == tiny_pdf("existing")
    assert source.read_bytes() == tiny_pdf("a")


def test_an_invalid_or_missing_source_is_refused(tmp_path, folders):
    reports, _ = folders
    bad = tmp_path / "render" / "report.pdf"
    bad.parent.mkdir()
    bad.write_bytes(b"not a pdf")
    for path in (bad, tmp_path / "render" / "none.pdf"):
        with pytest.raises(ra.RenderArtifactError) as info:
            ra.handoff_draft(path, reports)
        assert info.value.code == "source_invalid"
    assert list(reports.iterdir()) == []


def test_a_source_inside_the_reports_folder_is_refused(folders):
    reports, _ = folders
    final = reports / "report.pdf"
    final.write_bytes(tiny_pdf("final"))
    with pytest.raises(ra.RenderArtifactError) as info:
        ra.handoff_draft(final, reports)
    assert info.value.code == "source_in_reports_dir"
    assert [p.name for p in reports.iterdir()] == ["report.pdf"]


def test_the_handoff_never_creates_the_reports_folder(tmp_path):
    source = source_pdf(tmp_path)
    with pytest.raises(ra.RenderArtifactError) as info:
        ra.handoff_draft(source, tmp_path / "missing")
    assert info.value.code == "reports_dir_missing" and not (tmp_path / "missing").exists()


@pytest.mark.parametrize("fail", ["write", "write_half", "sync"])
def test_an_interrupted_handoff_keeps_the_source_and_the_partial_and_no_draft(tmp_path, folders, fail):
    reports, _ = folders
    source = source_pdf(tmp_path)
    original = source.read_bytes()
    with pytest.raises(ra.RenderArtifactError) as info:
        ra.handoff_draft(source, reports, ops=FaultOps(fail=fail))
    assert info.value.code == "handoff_incomplete"
    names = [p.name for p in reports.iterdir()]
    assert len(names) == 1 and ff.is_partial_name(names[0]) and info.value.partial_names == tuple(names)
    assert source.read_bytes() == original and not (reports / DRAFT).exists()


def test_a_draft_that_appears_during_the_handoff_is_not_replaced(tmp_path, folders):
    reports, _ = folders
    source = source_pdf(tmp_path)

    def appear():
        (reports / DRAFT).write_bytes(tiny_pdf("raced"))
    ops = FaultOps(hook=("create_exclusive", appear))
    with pytest.raises(ra.RenderArtifactError) as info:
        ra.handoff_draft(source, reports, ops=ops)
    assert info.value.code in ("draft_exists_differs", "handoff_incomplete")
    assert (reports / DRAFT).read_bytes() == tiny_pdf("raced")
    assert source.read_bytes() == tiny_pdf("a")


def test_a_source_changed_during_the_copy_is_detected(tmp_path, folders):
    reports, _ = folders
    source = source_pdf(tmp_path)
    calls = []

    def change():
        calls.append(1)
        if len(calls) == 4:  # the verification read of the source after the copy
            source.write_bytes(tiny_pdf("changed-after"))
    with pytest.raises(ra.RenderArtifactError) as info:
        ra.handoff_draft(source, reports, ops=FaultOps(hook=("open_read", change)))
    assert info.value.code in ("handoff_verification_failed", "source_invalid")
    assert (reports / DRAFT).exists()  # the copy is kept, nothing is deleted


# --- retry and crash states ---------------------------------------------------------------------------------------------------------

def test_state_1_nothing_exists_creates_the_identity_and_renders(folders):
    reports, work = folders
    stub = StubRenderer()
    out = run(reports, work, renderer=stub)
    value = make_input()
    assert out.outcome == "created" and out.ok and out.code is None
    assert out.created_sidecar and out.handed_off and len(stub.calls) == 1
    assert out.render_input_sha256 == rh.render_input_sha256(value)
    assert out.generation_date == value.generation_date
    files = listing(reports)
    assert set(files) == {DRAFT, ra.sidecar_name(out.render_input_sha256)}
    sidecar = ra.read_sidecar(reports, out.sidecar_name)
    assert (sidecar.pdf_sha256, sidecar.pdf_bytes) == (hashlib.sha256(files[DRAFT]).hexdigest(), len(files[DRAFT]))
    assert out.pdf_sha256 == sidecar.pdf_sha256


def test_state_2_a_sidecar_without_a_draft_reuses_the_original_identity_and_date(folders):
    reports, work = folders
    first = make_input(dt.date(2026, 7, 5))
    run(reports, work, first)
    (reports / DRAFT).rename(reports / "moved-away.bin")  # the crash: the draft handoff never happened
    sidecar_before = listing(reports)[ra.sidecar_name(rh.render_input_sha256(first))]
    later = dataclasses.replace(first, generation_date=dt.date(2026, 9, 1))
    stub = StubRenderer()
    out = run(reports, work, later, renderer=stub)
    assert out.outcome == "resumed" and out.ok and not out.created_sidecar and out.handed_off
    assert out.generation_date == dt.date(2026, 7, 5)  # the original date, not today's
    assert out.render_input_sha256 == rh.render_input_sha256(first)
    assert out.candidate_matches is False
    assert rh.render_input_sha256(stub.calls[0]) == rh.render_input_sha256(first)
    files = listing(reports)
    assert files[ra.sidecar_name(rh.render_input_sha256(first))] == sidecar_before
    assert DRAFT in files and len([n for n in files if n.startswith(ra.SIDECAR_PREFIX)]) == 1


def test_state_2_with_a_matching_candidate_reports_the_match(folders):
    reports, work = folders
    value = make_input()
    run(reports, work, value)
    (reports / DRAFT).rename(reports / "moved-away.bin")
    out = run(reports, work, value)
    assert out.outcome == "resumed" and out.candidate_matches is True


def test_state_3_sidecar_and_draft_present_are_validated_and_nothing_is_written(folders):
    reports, work = folders
    run(reports, work)
    before = listing(reports)
    stub = StubRenderer()
    out = run(reports, work, renderer=stub)
    assert out.outcome == "already_prepared" and out.ok and not out.created_sidecar and not out.handed_off
    assert stub.calls == [] and listing(reports) == before
    assert out.candidate_matches is True


def test_state_3_a_different_candidate_never_replaces_the_original(folders):
    reports, work = folders
    run(reports, work, make_input(dt.date(2026, 7, 5)))
    before = listing(reports)
    out = run(reports, work, make_input(dt.date(2026, 8, 1)))
    assert out.outcome == "already_prepared" and out.candidate_matches is False
    assert out.generation_date == dt.date(2026, 7, 5)
    assert listing(reports) == before


def test_state_4_a_draft_without_a_sidecar_stops_and_invents_no_identity(folders):
    reports, work = folders
    (reports / DRAFT).write_bytes(tiny_pdf("orphan"))
    stub = StubRenderer()
    out = run(reports, work, renderer=stub)
    assert out.outcome == "conflict" and out.code == "draft_without_sidecar" and not out.ok
    assert stub.calls == [] and out.render_input_sha256 is None
    assert list(listing(reports)) == [DRAFT]


def test_state_5_a_sidecar_with_a_wrong_hash_is_rejected_and_kept(folders):
    reports, work = folders
    run(reports, work)
    name = next(n for n in listing(reports) if n.startswith(ra.SIDECAR_PREFIX))
    damaged = json.loads((reports / name).read_bytes())
    damaged["render_input_sha256"] = "0" * 64
    bad = (json.dumps(damaged, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    (reports / name).write_bytes(bad)
    before = listing(reports)
    stub = StubRenderer()
    out = run(reports, work, renderer=stub)
    assert out.outcome == "conflict" and out.code == "sidecar_invalid" and stub.calls == []
    assert listing(reports) == before


def test_state_5_a_malformed_sidecar_blocks_even_without_a_draft(folders):
    reports, work = folders
    name = "report.render-input." + "a" * 16 + ".json"
    (reports / name).write_bytes(b"{broken")
    out = run(reports, work)
    assert out.outcome == "conflict" and out.code == "sidecar_invalid"
    assert list(listing(reports)) == [name]


def test_state_6_a_draft_whose_hash_differs_from_the_sidecar_is_rejected(folders):
    reports, work = folders
    run(reports, work)
    (reports / DRAFT).write_bytes(tiny_pdf("tampered"))
    before = listing(reports)
    out = run(reports, work)
    assert out.outcome == "conflict" and out.code == "draft_pdf_mismatch" and not out.ok
    assert listing(reports) == before


def test_state_6_a_draft_of_a_different_size_is_rejected(folders):
    reports, work = folders
    run(reports, work)
    (reports / DRAFT).write_bytes((reports / DRAFT).read_bytes() + b"\n")
    assert run(reports, work).code == "draft_pdf_mismatch"


def test_state_7_a_sidecar_collision_with_different_content_is_preserved(folders):
    reports, work = folders
    value = make_input()
    name = ra.sidecar_name(rh.render_input_sha256(value))
    other = sidecar_bytes(value, pdf=tiny_pdf("different-render"))
    (reports / name).write_bytes(other)
    # the existing sidecar is valid and open, so it is resumed; its PDF hash is not what this renderer produces
    out = run(reports, work, value)
    assert out.outcome == "conflict" and out.code == "render_not_reproducible"
    assert listing(reports) == {name: other}


def test_state_7_a_closed_sidecar_of_the_same_input_is_a_conflict_and_preserved(folders):
    reports, work = folders
    value = make_input()
    run(reports, work, value)
    before = listing(reports)
    digest = rh.render_input_sha256(value)
    out = run(reports, work, value, finalized_render_hashes={digest})
    assert out.outcome == "conflict" and out.code in ("render_input_already_finalized", "draft_without_sidecar")
    assert listing(reports) == before


def test_state_7_a_sidecar_that_appears_during_the_run_is_not_replaced(folders):
    reports, work = folders
    value = make_input()
    name = ra.sidecar_name(rh.render_input_sha256(value))
    squatter = sidecar_bytes(value, pdf=tiny_pdf("squatter"))

    class Racing(ff.FileOps):
        done = False

        def create_exclusive(self, path):
            if not Racing.done and Path(path).name.startswith("."):
                Racing.done = True
                (Path(path).parent / name).write_bytes(squatter)
            return super().create_exclusive(path)
    out = run(reports, work, value, ops=Racing())
    assert out.outcome == "conflict" and out.code == "sidecar_conflict"
    assert listing(reports)[name] == squatter and DRAFT not in listing(reports)


def test_state_8_an_interrupted_sidecar_write_keeps_the_partial_and_reports_it(folders):
    reports, work = folders
    out = run(reports, work, ops=FaultOps(fail="write_half"))
    assert out.outcome == "failed" and out.code == "sidecar_write_incomplete" and not out.ok
    names = list(listing(reports))
    assert len(names) == 1 and ff.is_partial_name(names[0]) and out.partial_names == tuple(names)
    assert not out.created_sidecar and not out.handed_off


def test_state_8_after_an_interrupted_sidecar_write_a_retry_starts_cleanly(folders):
    reports, work = folders
    run(reports, work, ops=FaultOps(fail="write_half"))
    partial = list(listing(reports))
    out = run(reports, work)
    assert out.outcome == "created" and out.ok
    assert set(listing(reports)) == set(partial) | {DRAFT, out.sidecar_name}  # the partial is never deleted


def test_state_9_an_interrupted_handoff_keeps_the_source_the_sidecar_and_the_partial(folders):
    reports, work = folders

    class FailSecondWrite(FaultOps):
        count = 0

        def write(self, handle, data):
            FailSecondWrite.count += 1
            if FailSecondWrite.count == 2:  # the first write is the sidecar, the second the draft copy
                raise OSError(5, "injected")
            super().write(handle, data)
    stub = StubRenderer()
    out = run(reports, work, renderer=stub, ops=FailSecondWrite())
    assert out.outcome == "failed" and out.code == "handoff_incomplete"
    assert out.created_sidecar and not out.handed_off and len(out.partial_names) == 1
    files = listing(reports)
    assert DRAFT not in files and out.sidecar_name in files
    assert any(ff.is_partial_name(n) for n in files)


def test_state_9_after_an_interrupted_handoff_a_retry_reuses_the_sidecar_and_finishes(folders):
    reports, work = folders

    class FailSecondWrite(FaultOps):
        count = 0

        def write(self, handle, data):
            FailSecondWrite.count += 1
            if FailSecondWrite.count == 2:
                raise OSError(5, "injected")
            super().write(handle, data)
    first = make_input(dt.date(2026, 7, 5))
    run(reports, work, first, ops=FailSecondWrite())
    out = run(reports, work, make_input(dt.date(2026, 9, 9)))
    assert out.outcome == "resumed" and out.ok and out.handed_off and not out.created_sidecar
    assert out.generation_date == dt.date(2026, 7, 5)
    files = listing(reports)
    assert DRAFT in files and sum(1 for n in files if ff.is_partial_name(n)) == 1


def test_a_changed_input_never_replaces_the_original_sidecar_or_draft(folders):
    reports, work = folders
    run(reports, work, make_input(dt.date(2026, 7, 5)))
    before = listing(reports)
    changed = dataclasses.replace(make_input(), client=dataclasses.replace(make_input().client, name="Changed Co"))
    out = run(reports, work, changed)
    assert out.ok and out.candidate_matches is False
    assert listing(reports) == before


def test_more_than_one_open_sidecar_is_a_conflict_and_the_closed_one_is_ignored(folders):
    reports, work = folders
    one, two = make_input(dt.date(2026, 7, 5)), make_input(dt.date(2026, 7, 6))
    for value in (one, two):
        ra.write_sidecar(reports, sidecar_bytes(value))
    assert run(reports, work).code == "multiple_open_sidecars"
    closed = {rh.render_input_sha256(one)}
    (reports / DRAFT).write_bytes(tiny_pdf(rh.render_input_sha256(two)))
    out = run(reports, work, finalized_render_hashes=closed)
    assert out.outcome == "already_prepared" and out.render_input_sha256 == rh.render_input_sha256(two)


def test_a_render_with_layout_issues_is_not_handed_off(folders):
    reports, work = folders
    out = run(reports, work, renderer=StubRenderer(issues=("overflow",)))
    assert out.outcome == "conflict" and out.code == "render_has_layout_issues"
    assert list(listing(reports)) == []


def test_a_renderer_reporting_another_identity_is_refused(folders):
    reports, work = folders
    out = run(reports, work, renderer=StubRenderer(identity_override="f" * 64))
    assert out.code == "render_identity_mismatch" and list(listing(reports)) == []


def test_a_render_failure_is_reported_without_writing(folders):
    reports, work = folders

    def failing(render_input, *, workdir, logo_path, fonts):
        raise pr.PdfRenderError("browser_unavailable", "no browser")
    out = run(reports, work, renderer=failing)
    assert out.outcome == "failed" and out.code == "render_failed" and list(listing(reports)) == []


def test_the_reports_directory_is_never_created_by_the_retry_function(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ra.RenderArtifactError) as info:
        ra.produce_report_draft(tmp_path / "missing", make_input(), workdir=work, logo_path=None, fonts=(),
                                renderer=StubRenderer())
    assert info.value.code == "reports_dir_missing" and not (tmp_path / "missing").exists()


def test_the_final_and_superseded_pdfs_are_never_touched(folders):
    reports, work = folders
    final = {"report.pdf": tiny_pdf("final"), "report.superseded." + "a" * 16 + ".pdf": tiny_pdf("old")}
    for name, data in final.items():
        (reports / name).write_bytes(data)
    out = run(reports, work)
    assert out.outcome == "created"
    files = listing(reports)
    assert all(files[n] == d for n, d in final.items())


def test_a_non_input_candidate_is_rejected(folders):
    reports, work = folders
    with pytest.raises(TypeError):
        ra.produce_report_draft(reports, "x", workdir=work, logo_path=None, fonts=(), renderer=StubRenderer())


def test_outcomes_hold_no_client_text(folders):
    reports, work = folders
    value = make_input()
    (reports / DRAFT).write_bytes(tiny_pdf("orphan"))
    out = run(reports, work, value)
    text = repr(dataclasses.replace(out, render_input=None))
    assert value.client.name not in text


def test_the_module_uses_no_network_clock_database_or_finalization():
    source = (Path(ra.__file__)).read_text(encoding="utf-8")
    for word in ("socket", "urllib", "requests", "datetime.now", "date.today", "time.time", "sqlite3", "os.remove",
                 "os.unlink", "os.replace", "shutil", "report_finalize", "record_writer", "reviewer_record"):
        assert word not in source


# --- real renderer (Chromium) --------------------------------------------------------------------------------------------------------

def test_the_real_renderer_round_trip_and_retry(tmp_path):
    from test_pdf_renderer import LOGO, SOURCES, FONT_FILES
    from seo_report import asset_fingerprints as af
    fp = af.measure_fingerprints(template_version="t-0123456789abcdef", logo_path=LOGO, font_paths=FONT_FILES)
    value = dataclasses.replace(sample(), fingerprints=fp)
    reports = tmp_path / "reports"
    reports.mkdir()
    try:
        out = ra.produce_report_draft(reports, value, workdir=_empty(tmp_path, "w1"), logo_path=LOGO, fonts=SOURCES)
    except Exception as err:
        if type(err).__module__.startswith("playwright"):
            pytest.skip("Chromium could not be started")
        raise
    assert out.outcome == "created", out
    sidecar = ra.read_sidecar(reports, out.sidecar_name)
    draft = (reports / DRAFT).read_bytes()
    assert hashlib.sha256(draft).hexdigest() == sidecar.pdf_sha256 and len(draft) == sidecar.pdf_bytes
    assert sidecar.renderer_dict()["browser"] if hasattr(sidecar, "renderer_dict") else dict(sidecar.renderer)["browser"]
    assert rh.render_input_sha256(sidecar.render_input) == rh.render_input_sha256(value)
    # crash after the sidecar: a retry re-renders from the sidecar and reproduces the same PDF bytes
    (reports / DRAFT).rename(reports / "kept-aside.bin")
    again = ra.produce_report_draft(reports, value, workdir=_empty(tmp_path, "w2"), logo_path=LOGO, fonts=SOURCES)
    assert again.outcome == "resumed" and again.handed_off
    assert (reports / DRAFT).read_bytes() == draft


def _empty(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path
