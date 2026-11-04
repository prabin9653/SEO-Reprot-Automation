import hashlib
import os
import re
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import finalize_files as ff
from seo_report import history_access as ha
from seo_report import output_layout as layout
from seo_report.reviewer_record import PdfChecks

PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
H = "0123456789abcdef"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "reports"
    path.mkdir()
    return path


def put(folder, name, data=PDF):
    (folder / name).write_bytes(data)
    return folder / name


def state(*roots):
    """Every file under the roots with its content hash and size: proves nothing was touched."""
    found = {}
    for root in roots:
        for path in sorted(Path(root).rglob("*")):
            found[str(path)] = (sha(path.read_bytes()) if path.is_file() else "dir", path.stat().st_size)
    return found


def link_or_skip(link: Path, target: Path):
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")


class FaultOps(ff.FileOps):
    """Fails (or runs a hook) at a chosen file operation."""

    def __init__(self, fail=None, after=0, error=None, hook=None):
        self.fail, self.after, self.hook = fail, after, hook
        self.error = error or OSError(13, "injected failure")
        self.calls = []

    def _maybe(self, name):
        self.calls.append(name)
        if self.hook and self.hook[0] == name:
            self.hook[1]()
        if name == self.fail:
            if self.after:
                self.after -= 1
            else:
                raise self.error

    def open_read(self, path):
        self._maybe("open_read")
        return super().open_read(path)

    def create_exclusive(self, path):
        self._maybe("create_exclusive")
        return super().create_exclusive(path)

    def write(self, handle, data):
        self._maybe("write")
        if self.fail == "write_half":
            handle.write(data[: len(data) // 2])
            handle.flush()
            raise self.error
        super().write(handle, data)

    def sync(self, handle):
        self._maybe("sync")
        super().sync(handle)

    def close(self, handle):
        super().close(handle)
        self._maybe("close")

    def rename_noreplace(self, source, destination):
        self._maybe("rename_noreplace")
        super().rename_noreplace(source, destination)


# --- hashing ------------------------------------------------------------------------------------------------------------

def test_an_empty_file_has_the_known_digest_and_zero_bytes(folder):
    result = ff.sha256_file(put(folder, "empty.bin", b""))
    assert result == ff.FileDigest(hashlib.sha256(b"").hexdigest(), 0)
    assert result.sha256 == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_the_digest_and_byte_count_match_the_content(folder):
    data = bytes(range(256)) * 41 + b"tail"
    result = ff.sha256_file(put(folder, "a.bin", data))
    assert result.sha256 == sha(data) and result.byte_count == len(data) == 10500
    assert re.fullmatch(r"[0-9a-f]{64}", result.sha256)


def test_a_large_file_is_hashed_correctly(folder):
    data = (b"0123456789abcdef" * 65536) * 5 + b"end"  # 5 MiB and a bit
    result = ff.sha256_file(put(folder, "large.bin", data))
    assert result.byte_count == len(data) and result.sha256 == sha(data)


def test_results_are_stable_across_calls(folder):
    path = put(folder, "a.bin")
    assert ff.sha256_file(path) == ff.sha256_file(path) == ff.FileDigest(sha(PDF), len(PDF))


def test_files_are_read_in_chunks_never_whole(folder, monkeypatch):
    monkeypatch.setattr(ff, "HASH_CHUNK", 1000)
    sizes = []

    class Spy(ff.FileOps):
        def open_read(self, path):
            handle = super().open_read(path)
            real = handle.read

            class Wrapper:
                def read(self, n=-1):
                    sizes.append(n)
                    return real(n)

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    handle.close()

            return Wrapper()

    data = os.urandom(5300)
    assert ff.sha256_file(put(folder, "a.bin", data), ops=Spy()).sha256 == sha(data)
    assert sizes and set(sizes) == {1000} and len(sizes) == 7  # 5 full chunks, one partial, one empty read


def test_missing_files_folders_and_links_cannot_be_hashed(folder, tmp_path):
    with pytest.raises(ff.FileOperationError) as raised:
        ff.sha256_file(folder / "missing.bin")
    assert raised.value.cause_class == "FileNotFoundError" and str(tmp_path) not in str(raised.value)
    with pytest.raises(ff.FileOperationError, match="regular file"):
        ff.sha256_file(folder)
    link_or_skip(folder / "link.bin", put(folder, "real.bin"))
    with pytest.raises(ff.FileOperationError, match="regular file"):
        ff.sha256_file(folder / "link.bin")


def test_a_file_that_changes_while_it_is_read_is_refused(folder):
    class ShortRead(ff.FileOps):
        def open_read(self, path):
            data = (folder / "a.bin").read_bytes()[:-3]
            import io
            return io.BytesIO(data)

    path = put(folder, "a.bin")
    with pytest.raises(ff.FileOperationError, match="changed"):
        ff.sha256_file(path, ops=ShortRead())


def test_read_errors_are_wrapped_without_system_text(folder):
    path = put(folder, "a.bin")
    with pytest.raises(ff.FileOperationError) as raised:
        ff.sha256_file(path, ops=FaultOps(fail="open_read"))
    assert raised.value.cause_class == "PermissionError" and "injected" not in str(raised.value)  # errno 13


# --- draft PDF checks ---------------------------------------------------------------------------------------------------

def test_a_valid_draft_passes_with_hash_size_and_checks(folder):
    result = ff.validate_draft_pdf(put(folder, "report.unarchived.pdf"))
    assert result.ok and result.problems == () and (result.sha256, result.byte_count) == (sha(PDF), len(PDF))
    assert isinstance(result.checks, PdfChecks)
    assert result.checks == PdfChecks(non_empty=True, header=True, eof_marker=True, preliminary=True)


def test_the_check_reuses_the_reviewer_record_type_unchanged():
    from seo_report import reviewer_record
    assert ff.PdfChecks is reviewer_record.PdfChecks


@pytest.mark.parametrize("data,problem", [
    (b"%PDF", "bad_header"), (b"PDF-1.4 ... %%EOF", "bad_header"), (b" %PDF-1.4 %%EOF", "bad_header"),
    (b"%pdf-1.4 %%EOF", "bad_header"), (b"%PDF-1.4 no trailer here", "missing_eof"),
    (b"%PDF-1.4 %%EO", "missing_eof"), (b"%PDF-1.4 %%eof", "missing_eof")])
def test_invalid_headers_and_missing_markers_are_reported(folder, data, problem):
    result = ff.validate_draft_pdf(put(folder, "d.pdf", data))
    assert not result.ok and problem in result.problems
    assert result.checks.non_empty is True and result.sha256 == sha(data) and result.byte_count == len(data)


def test_both_problems_are_reported_together(folder):
    result = ff.validate_draft_pdf(put(folder, "d.pdf", b"nothing useful here"))
    assert result.problems == ("bad_header", "missing_eof")
    assert (result.checks.header, result.checks.eof_marker) == (False, False)


def test_the_eof_marker_must_be_within_the_last_1024_bytes(folder):
    head = b"%PDF-1.4\n"
    inside = head + b"x" * 5000 + b"%%EOF" + b"y" * (1024 - 5)  # marker starts exactly 1024 bytes from the end
    outside = head + b"x" * 5000 + b"%%EOF" + b"y" * (1024 - 4)  # one byte earlier: partly outside the window
    assert ff.validate_draft_pdf(put(folder, "in.pdf", inside)).ok
    result = ff.validate_draft_pdf(put(folder, "out.pdf", outside))
    assert result.problems == ("missing_eof",)
    assert ff.validate_draft_pdf(put(folder, "end.pdf", head + b"z" * 3000 + b"%%EOF")).ok


def test_a_small_file_with_header_and_marker_passes(folder):
    assert ff.validate_draft_pdf(put(folder, "tiny.pdf", b"%PDF-%%EOF")).ok


def test_an_empty_file_is_refused(folder):
    result = ff.validate_draft_pdf(put(folder, "e.pdf", b""))
    assert not result.ok and result.problems == ("empty",) and result.checks.non_empty is False
    assert result.sha256 is None and result.byte_count is None


def test_missing_folder_and_link_drafts_are_refused(folder):
    assert ff.validate_draft_pdf(folder / "nope.pdf").problems == ("not_found",)
    assert ff.validate_draft_pdf(folder).problems == ("not_regular_file",)
    link_or_skip(folder / "link.pdf", put(folder, "real.pdf"))
    result = ff.validate_draft_pdf(folder / "link.pdf")
    assert result.problems == ("symlink",) and not result.ok and result.sha256 is None


def test_oversized_drafts_are_refused_at_the_limit(folder, monkeypatch):
    monkeypatch.setattr(ff, "MAX_PDF_BYTES", 100)
    exact = b"%PDF-" + b"a" * 90 + b"%%EOF"
    assert len(exact) == 100 and ff.validate_draft_pdf(put(folder, "exact.pdf", exact)).ok
    result = ff.validate_draft_pdf(put(folder, "big.pdf", exact + b"x"))
    assert result.problems == ("too_large",) and result.sha256 is None and result.checks.non_empty is True


def test_the_default_size_limit_is_the_record_schemas():
    from seo_report import record_schema
    assert ff.MAX_PDF_BYTES == record_schema.MAX_PDF_BYTES == 1 << 30


def test_unreadable_drafts_are_reported_not_raised(folder):
    path = put(folder, "d.pdf")
    result = ff.validate_draft_pdf(path, ops=FaultOps(fail="open_read"))
    assert result.problems == ("unreadable",) and not result.ok


def test_a_draft_that_changes_while_read_is_refused(folder):
    class Shrinks(ff.FileOps):
        def open_read(self, path):
            import io
            return io.BytesIO(PDF[:-2])

    result = ff.validate_draft_pdf(put(folder, "d.pdf"), ops=Shrinks())
    assert not result.ok and result.problems[0] in ("changed_while_reading", "missing_eof")


def test_every_problem_code_is_documented():
    assert set(ff.DRAFT_PROBLEMS) >= {"not_found", "symlink", "not_regular_file", "empty", "too_large", "bad_header",
                                      "missing_eof", "unreadable", "changed_while_reading"}


def test_draft_checks_never_modify_the_file(folder):
    path = put(folder, "d.pdf")
    before = (state(folder), path.stat().st_mtime_ns)
    ff.validate_draft_pdf(path)
    assert (state(folder), path.stat().st_mtime_ns) == before


# --- no-overwrite rename ---------------------------------------------------------------------------------------------------

def test_a_rename_moves_the_file_when_the_destination_is_free(folder):
    put(folder, "a.pdf")
    ff.rename_noreplace(folder, "a.pdf", "b.pdf")
    assert not (folder / "a.pdf").exists() and (folder / "b.pdf").read_bytes() == PDF


def test_an_existing_destination_is_a_conflict_and_nothing_changes(folder):
    put(folder, "a.pdf", PDF)
    put(folder, "b.pdf", b"different existing content")
    before = state(folder)
    with pytest.raises(ff.FileConflictError) as raised:
        ff.rename_noreplace(folder, "a.pdf", "b.pdf")
    assert raised.value.kind == "destination_exists" and "b.pdf" in str(raised.value)
    assert state(folder) == before


def test_an_existing_destination_is_a_conflict_even_when_it_is_a_folder(folder):
    put(folder, "a.pdf")
    (folder / "b.pdf").mkdir()
    before = state(folder)
    with pytest.raises(ff.FileConflictError):
        ff.rename_noreplace(folder, "a.pdf", "b.pdf")
    assert state(folder) == before


def test_identical_content_at_the_destination_is_still_a_conflict(folder):
    put(folder, "a.pdf")
    put(folder, "b.pdf")
    with pytest.raises(ff.FileConflictError):
        ff.rename_noreplace(folder, "a.pdf", "b.pdf")
    assert (folder / "a.pdf").exists()


def test_the_same_name_or_a_case_only_change_is_refused(folder):
    put(folder, "a.pdf")
    for destination in ("a.pdf", "A.pdf" if os.name == "nt" else "a.pdf"):
        with pytest.raises(ff.FileConflictError) as raised:
            ff.rename_noreplace(folder, "a.pdf", destination)
        assert raised.value.kind == "same_path"
    assert (folder / "a.pdf").read_bytes() == PDF


def test_a_missing_source_folder_source_or_link_source_is_refused(folder):
    with pytest.raises(ff.FileOperationError):
        ff.rename_noreplace(folder, "missing.pdf", "b.pdf")
    (folder / "sub").mkdir()
    with pytest.raises(ff.FileOperationError, match="regular file"):
        ff.rename_noreplace(folder, "sub", "b.pdf")
    link_or_skip(folder / "l.pdf", put(folder, "r.pdf"))
    with pytest.raises(ff.FileOperationError, match="regular file"):
        ff.rename_noreplace(folder, "l.pdf", "b.pdf")
    assert not (folder / "b.pdf").exists()


def test_a_failure_during_the_rename_leaves_everything_as_it_was(folder):
    put(folder, "a.pdf")
    put(folder, "keep.pdf", b"keep me")
    before = state(folder)
    with pytest.raises(ff.FileOperationError) as raised:
        ff.rename_noreplace(folder, "a.pdf", "b.pdf", ops=FaultOps(fail="rename_noreplace"))
    assert raised.value.operation == "rename" and raised.value.cause_class == "PermissionError"
    assert state(folder) == before


def test_a_destination_created_between_the_check_and_the_rename_is_not_overwritten(folder):
    put(folder, "a.pdf")
    ops = FaultOps(hook=("rename_noreplace", lambda: put(folder, "b.pdf", b"raced in")))
    with pytest.raises(ff.FileConflictError) as raised:
        ff.rename_noreplace(folder, "a.pdf", "b.pdf", ops=ops)
    assert raised.value.kind == "destination_exists"
    assert (folder / "b.pdf").read_bytes() == b"raced in" and (folder / "a.pdf").read_bytes() == PDF


def test_a_destination_refused_by_the_system_is_a_conflict(folder):
    put(folder, "a.pdf")
    ops = FaultOps(fail="rename_noreplace", error=FileExistsError(17, "exists"))
    with pytest.raises(ff.FileConflictError):
        ff.rename_noreplace(folder, "a.pdf", "b.pdf", ops=ops)
    assert (folder / "a.pdf").exists()


def test_the_real_rename_really_refuses_to_overwrite(folder):
    put(folder, "a.pdf", b"source")
    put(folder, "b.pdf", b"destination")
    with pytest.raises(FileExistsError if os.name == "nt" else OSError):
        if os.name == "nt":
            ff.FileOps().rename_noreplace(folder / "a.pdf", folder / "b.pdf")
        else:
            ff.FileOps().rename_noreplace(folder / "a.pdf", folder / "b.pdf")
    assert (folder / "b.pdf").read_bytes() == b"destination" and (folder / "a.pdf").read_bytes() == b"source"


def test_os_replace_is_never_used(folder, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("os.replace must not be used")

    monkeypatch.setattr(os, "replace", forbidden)
    put(folder, "a.pdf")
    ff.rename_noreplace(folder, "a.pdf", "b.pdf")
    ff.write_partial_then_promote(folder, "c.json", b"{}", nonce="0123456789ab")
    assert (folder / "b.pdf").exists() and (folder / "c.json").exists()


@pytest.mark.parametrize("name", ["../x.pdf", "sub/x.pdf", "sub\\x.pdf", "C:x.pdf", "x.pdf:stream", "", ".", "..",
                                  "x.pdf.", "x y.pdf", "a" * 121, "café.pdf", None, 5])
def test_only_plain_file_names_are_accepted(folder, name):
    put(folder, "a.pdf")
    with pytest.raises(ff.FileOperationError):
        ff.rename_noreplace(folder, "a.pdf", name)
    with pytest.raises(ff.FileOperationError):
        ff.write_exclusive(folder, name, b"x")
    assert sorted(p.name for p in folder.iterdir()) == ["a.pdf"]


def test_the_directory_must_be_an_existing_absolute_real_folder(folder, tmp_path):
    with pytest.raises(ff.FileOperationError, match="absolute"):
        ff.write_exclusive("relative_dir", "a.pdf", b"x")
    with pytest.raises(ff.FileOperationError):
        ff.write_exclusive(tmp_path / "missing_dir", "a.pdf", b"x")
    assert not (tmp_path / "missing_dir").exists()  # never created
    file_path = put(folder, "plain.txt")
    with pytest.raises(ff.FileOperationError, match="real folder"):
        ff.write_exclusive(file_path, "a.pdf", b"x")
    link_or_skip(tmp_path / "link_dir", folder)
    with pytest.raises(ff.FileOperationError, match="real folder"):
        ff.write_exclusive(tmp_path / "link_dir", "a.pdf", b"x")


def test_a_path_that_is_too_long_is_refused(tmp_path):
    deep = tmp_path
    while len(str(deep)) < 250:
        deep = deep / ("d" * 40)
    with pytest.raises(ff.FileOperationError):
        ff.write_exclusive(deep, "report.pdf", b"x")  # the folder does not exist and the path is too long
    assert not (tmp_path / ("d" * 40)).exists()


# --- exclusive creation and writing ------------------------------------------------------------------------------------------

def test_an_exclusive_write_creates_the_file(folder):
    result = ff.write_exclusive(folder, "a.json", b'{"a": 1}')
    assert result == ff.WrittenFile("a.json", 8, sha(b'{"a": 1}')) and (folder / "a.json").read_bytes() == b'{"a": 1}'
    assert ff.write_exclusive(folder, "empty.json", b"").byte_count == 0


def test_an_existing_file_is_never_overwritten_by_an_exclusive_write(folder):
    put(folder, "a.json", b"original")
    before = state(folder)
    with pytest.raises(ff.FileConflictError) as raised:
        ff.write_exclusive(folder, "a.json", b"new content")
    assert raised.value.kind == "destination_exists" and state(folder) == before
    (folder / "d").mkdir()
    with pytest.raises(ff.FileConflictError):
        ff.write_exclusive(folder, "d", b"x")


def test_bytes_are_flushed_to_disk_and_the_handle_is_closed(folder):
    ops = FaultOps()
    ff.write_exclusive(folder, "a.json", b"data", ops=ops)
    assert ops.calls == ["create_exclusive", "write", "sync", "close"]
    (folder / "a.json").unlink()  # impossible if a handle were still open on Windows


def test_non_bytes_data_is_refused_before_anything_is_created(folder):
    for bad in ("text", 5, None, ["a"]):
        with pytest.raises(ff.FileOperationError, match="bytes"):
            ff.write_exclusive(folder, "a.json", bad)
    assert list(folder.iterdir()) == []


def test_a_failure_while_creating_leaves_nothing(folder):
    with pytest.raises(ff.FileOperationError) as raised:
        ff.write_exclusive(folder, "a.json", b"x", ops=FaultOps(fail="create_exclusive"))
    assert not raised.value.left_behind and list(folder.iterdir()) == []


@pytest.mark.parametrize("fail", ["write", "write_half", "sync", "close"])
def test_a_failure_while_writing_is_reported_and_the_incomplete_file_is_left_in_place(folder, fail):
    with pytest.raises(ff.FileOperationError) as raised:
        ff.write_exclusive(folder, "a.json", b"0123456789", ops=FaultOps(fail=fail))
    assert raised.value.left_behind is True and raised.value.operation in ("write", "close")
    assert (folder / "a.json").exists()  # never deleted automatically
    assert "injected" not in str(raised.value)


def test_a_failed_write_to_a_new_name_does_not_touch_other_files(folder):
    put(folder, "other.json", b"other")
    before = state(folder)
    with pytest.raises(ff.FileOperationError):
        ff.write_exclusive(folder, "a.json", b"x", ops=FaultOps(fail="write"))
    after = state(folder)
    assert {k: v for k, v in after.items() if not k.endswith("a.json")} == before


# --- partial files and promotion ------------------------------------------------------------------------------------------------

def test_partial_names_are_unique_short_and_tied_to_the_final_name():
    first = ff.partial_name("reviewer.x.json", "0123456789ab")
    assert first == ff.partial_name("reviewer.x.json", "0123456789ab")
    assert first != ff.partial_name("reviewer.x.json", "0123456789ac")
    assert first != ff.partial_name("reviewer.y.json", "0123456789ab")
    assert re.fullmatch(r"\.[0-9a-f]{8}\.0123456789ab\.partial", first) and ff.is_partial_name(first)
    assert len(first) <= layout.LONGEST_FILENAME_CHARS["reviewer"] and len(first) == 30


def test_partial_names_can_never_be_mistaken_for_records_or_pdfs():
    name = ff.partial_name("reviewer.0198c1a2-7b3c-7d4e-8f5a-1234567890ab.0123456789abcdef.json", "ab" * 6)
    assert not re.fullmatch(r"reviewer\.[0-9a-f-]{36}\.[0-9a-f]{16}\.json", name)
    with pytest.raises(layout.OutputLayoutError):
        layout.parse_report_pdf_name(name)
    assert not ff.is_partial_name("reviewer.x.json") and not ff.is_partial_name(5)
    assert not ff.is_partial_name(".0123456789.abcdef.partial")


@pytest.mark.parametrize("nonce", ["", "abc", "0123456789ABCDEF", "0123456789abcdef0", "xyz12345", "0123456789 a",
                                   None, 12345678])
def test_invalid_nonces_are_refused(folder, nonce):
    with pytest.raises(ff.FileOperationError, match="nonce"):
        ff.partial_name("a.json", nonce)
    if nonce is not None:  # None means "generate one", which is valid
        with pytest.raises(ff.FileOperationError):
            ff.write_partial_then_promote(folder, "a.json", b"x", nonce=nonce)
        assert list(folder.iterdir()) == []


def test_generated_nonces_are_unique_and_valid():
    nonces = {ff.new_nonce() for _ in range(500)}
    assert len(nonces) == 500 and all(re.fullmatch(r"[0-9a-f]{12}", n) for n in nonces)


def test_a_partial_write_is_promoted_to_the_final_name(folder):
    result = ff.write_partial_then_promote(folder, "a.json", b'{"x": 1}', nonce="0123456789ab")
    assert result == ff.WrittenFile("a.json", 8, sha(b'{"x": 1}'))
    assert [p.name for p in folder.iterdir()] == ["a.json"] and (folder / "a.json").read_bytes() == b'{"x": 1}'


def test_the_default_nonce_gives_distinct_partials_for_each_attempt(folder):
    seen = set()

    class Record(ff.FileOps):
        def create_exclusive(self, path):
            seen.add(Path(path).name)
            return super().create_exclusive(path)

    for n in range(5):
        ff.write_partial_then_promote(folder, f"f{n}.json", b"x", ops=Record())
    assert len(seen) == 5 and all(ff.is_partial_name(name) for name in seen)


def test_an_existing_final_file_is_a_conflict_and_no_partial_is_created(folder):
    put(folder, "a.json", b"original")
    before = state(folder)
    with pytest.raises(ff.FileConflictError) as raised:
        ff.write_partial_then_promote(folder, "a.json", b"new", nonce="0123456789ab")
    assert raised.value.kind == "destination_exists" and state(folder) == before


def test_a_failure_while_writing_the_partial_leaves_the_final_name_free_and_the_partial_in_place(folder):
    with pytest.raises(ff.FileOperationError) as raised:
        ff.write_partial_then_promote(folder, "a.json", b"0123456789", nonce="0123456789ab",
                                      ops=FaultOps(fail="write_half"))
    assert raised.value.left_behind is True
    names = [p.name for p in folder.iterdir()]
    assert names == [ff.partial_name("a.json", "0123456789ab")] and not (folder / "a.json").exists()
    assert (folder / names[0]).read_bytes() == b"01234"  # the incomplete data stays, labelled as a partial


def test_a_failure_during_promotion_keeps_the_complete_partial_and_leaves_the_final_name_free(folder):
    with pytest.raises(ff.FileOperationError) as raised:
        ff.write_partial_then_promote(folder, "a.json", b"complete", nonce="0123456789ab",
                                      ops=FaultOps(fail="rename_noreplace"))
    assert raised.value.left_behind is True and not (folder / "a.json").exists()
    assert (folder / ff.partial_name("a.json", "0123456789ab")).read_bytes() == b"complete"


def test_a_final_file_that_appears_before_promotion_is_not_overwritten(folder):
    ops = FaultOps(hook=("rename_noreplace", lambda: put(folder, "a.json", b"raced in")))
    with pytest.raises(ff.FileConflictError) as raised:
        ff.write_partial_then_promote(folder, "a.json", b"mine", nonce="0123456789ab", ops=ops)
    assert raised.value.kind == "destination_exists" and raised.value.left_behind is True
    assert (folder / "a.json").read_bytes() == b"raced in"
    assert (folder / ff.partial_name("a.json", "0123456789ab")).read_bytes() == b"mine"


def test_a_stale_partial_is_neither_deleted_nor_adopted_nor_a_blocker(folder):
    stale = put(folder, ff.partial_name("a.json", "aaaaaaaaaaaa"), b"old abandoned bytes")
    os.utime(stale, (946684800, 946684800))  # the year 2000: old does not mean abandoned
    before = (stale.read_bytes(), stale.stat().st_mtime_ns)
    result = ff.write_partial_then_promote(folder, "a.json", b"fresh", nonce="bbbbbbbbbbbb")
    assert (folder / "a.json").read_bytes() == b"fresh" and result.sha256 == sha(b"fresh")
    assert (stale.read_bytes(), stale.stat().st_mtime_ns) == before


def test_reusing_a_nonce_never_overwrites_an_existing_partial(folder):
    first = put(folder, ff.partial_name("a.json", "cccccccccccc"), b"someone else's data")
    with pytest.raises(ff.FileConflictError):
        ff.write_partial_then_promote(folder, "a.json", b"mine", nonce="cccccccccccc")
    assert first.read_bytes() == b"someone else's data" and not (folder / "a.json").exists()


def test_a_failed_promotion_does_not_block_a_later_attempt(folder):
    with pytest.raises(ff.FileOperationError):
        ff.write_partial_then_promote(folder, "a.json", b"one", nonce="111111111111",
                                      ops=FaultOps(fail="rename_noreplace"))
    ff.write_partial_then_promote(folder, "a.json", b"two", nonce="222222222222")
    assert (folder / "a.json").read_bytes() == b"two"
    assert (folder / ff.partial_name("a.json", "111111111111")).read_bytes() == b"one"  # still there, untouched


# --- preserved-copy names -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("family", ff.FAMILIES)
def test_variant_names_follow_the_approved_patterns(family):
    assert ff.variant_name(family, H) == f"report.{family}.{H}.pdf"
    assert ff.variant_name(family, H, 1) == f"report.{family}.{H}.pdf"
    assert ff.variant_name(family, H, 2) == f"report.{family}.{H}.2.pdf"
    assert ff.variant_name(family, H, 99) == f"report.{family}.{H}.99.pdf"
    assert len(ff.variant_name(family, H, 99)) <= layout.LONGEST_FILENAME_CHARS["reports"] == 41


def test_the_first_two_families_are_exactly_the_layout_helpers():
    assert ff.variant_name("duplicate", H, 7) == layout.duplicate_pdf_name(H, 7)
    assert ff.variant_name("superseded", H, 7) == layout.superseded_pdf_name(H, 7)


@pytest.mark.parametrize("family", ff.FAMILIES)
@pytest.mark.parametrize("number", [0, -1, 100, True, False, 2.0, "2", None])
def test_invalid_variant_numbers_are_refused(family, number):
    with pytest.raises(ff.FileOperationError):
        ff.variant_name(family, H, number)


@pytest.mark.parametrize("family", ff.FAMILIES)
@pytest.mark.parametrize("hash16", ["", H.upper(), H[:15], H + "0", "0123456789abcdeg", None, 5])
def test_invalid_hashes_are_refused(family, hash16):
    with pytest.raises(ff.FileOperationError):
        ff.variant_name(family, hash16)


def test_an_unknown_family_is_refused():
    for family in ("backup", "final", "", None, "Duplicate"):
        with pytest.raises(ff.FileOperationError):
            ff.variant_name(family, H)


@pytest.mark.parametrize("family", ff.FAMILIES)
def test_the_parser_round_trips_every_name(family):
    for number in range(1, 100):
        assert ff.parse_variant_name(ff.variant_name(family, H, number)) == ff.VariantName(family, H, number)


@pytest.mark.parametrize("family", ff.FAMILIES)
@pytest.mark.parametrize("suffix", [f".{H.upper()}.pdf", ".0123.pdf", f".{H}.pdf.bak", f".{H}.PDF", f".{H}.1.pdf",
                                    f".{H}.0.pdf", f".{H}.100.pdf", f".{H}.02.pdf", f".{H}.copy.pdf",
                                    f".{H}.2.3.pdf", f".{H}.-2.pdf", f".{H}..pdf", f".{H}.pdf ", f".{H}.pdf\n"])
def test_the_parser_rejects_malformed_names(family, suffix):
    with pytest.raises(ff.FileOperationError):
        ff.parse_variant_name(f"report.{family}{suffix}")


@pytest.mark.parametrize("name", ["report.pdf", "Report.duplicate." + H + ".pdf", "report.other." + H + ".pdf",
                                  "sub/report.duplicate." + H + ".pdf", "report.unarchived.pdf",
                                  "report.unarchived.pdf.partial", "", None, 5])
def test_the_parser_rejects_other_names(name):
    with pytest.raises(ff.FileOperationError):
        ff.parse_variant_name(name)


@pytest.mark.parametrize("family", ff.FAMILIES)
def test_there_are_ninety_nine_distinct_candidates_in_order(family):
    names = ff.variant_candidates(family, H)
    assert len(names) == 99 == len(set(names))
    assert names[0] == f"report.{family}.{H}.pdf" and names[1].endswith(".2.pdf") and names[-1].endswith(".99.pdf")


@pytest.mark.parametrize("family", ff.FAMILIES)
def test_a_free_name_is_chosen_without_touching_anything(folder, family):
    put(folder, "report.pdf")
    before = state(folder)
    choice = ff.choose_preserved_name(folder, family, sha(PDF))
    assert choice == ff.PreservedChoice(f"report.{family}.{sha(PDF)[:16]}.pdf", 1, ())
    assert state(folder) == before


@pytest.mark.parametrize("family", ff.FAMILIES)
def test_identical_copies_are_kept_and_the_next_number_is_used(folder, family):
    digest = sha(PDF)
    put(folder, ff.variant_name(family, digest[:16], 1), PDF)
    assert ff.choose_preserved_name(folder, family, digest) == ff.PreservedChoice(
        ff.variant_name(family, digest[:16], 2), 2, (1,))
    put(folder, ff.variant_name(family, digest[:16], 2), PDF)
    choice = ff.choose_preserved_name(folder, family, digest)
    assert (choice.number, choice.identical_copies) == (3, (1, 2))
    assert (folder / ff.variant_name(family, digest[:16], 1)).read_bytes() == PDF  # never removed


@pytest.mark.parametrize("family", ff.FAMILIES)
def test_a_same_name_file_with_different_content_is_a_conflict_not_an_overwrite(folder, family):
    digest = sha(PDF)
    name = ff.variant_name(family, digest[:16], 1)
    put(folder, name, b"%PDF-1.4 someone else's report %%EOF")
    before = state(folder)
    with pytest.raises(ff.FileConflictError) as raised:
        ff.choose_preserved_name(folder, family, digest)
    assert (raised.value.kind, raised.value.family, raised.value.number) == ("name_conflict", family, 1)
    assert state(folder) == before


@pytest.mark.parametrize("family", ff.FAMILIES)
def test_a_conflict_at_a_later_number_stops_the_walk(folder, family):
    digest = sha(PDF)
    put(folder, ff.variant_name(family, digest[:16], 1), PDF)
    put(folder, ff.variant_name(family, digest[:16], 2), b"different")
    with pytest.raises(ff.FileConflictError) as raised:
        ff.choose_preserved_name(folder, family, digest)
    assert raised.value.number == 2 and raised.value.kind == "name_conflict"


def test_a_same_sized_file_with_different_content_is_still_a_conflict(folder):
    digest = sha(PDF)
    put(folder, ff.variant_name("superseded", digest[:16], 1), bytes([PDF[0] ^ 1]) + PDF[1:])
    with pytest.raises(ff.FileConflictError):
        ff.choose_preserved_name(folder, "superseded", digest)


def test_a_folder_or_link_in_the_way_is_a_conflict(folder):
    digest = sha(PDF)
    (folder / ff.variant_name("duplicate", digest[:16], 1)).mkdir()
    with pytest.raises(ff.FileConflictError):
        ff.choose_preserved_name(folder, "duplicate", digest)
    link_or_skip(folder / ff.variant_name("unarchived", digest[:16], 1), put(folder, "target.pdf", PDF))
    with pytest.raises(ff.FileConflictError):
        ff.choose_preserved_name(folder, "unarchived", digest)


@pytest.mark.parametrize("family", ff.FAMILIES)
def test_the_walk_uses_number_99_and_then_reports_exhaustion(folder, family):
    digest = sha(PDF)
    for number in range(1, 99):
        put(folder, ff.variant_name(family, digest[:16], number), PDF)
    choice = ff.choose_preserved_name(folder, family, digest)
    assert choice.number == 99 and choice.name.endswith(".99.pdf") and len(choice.identical_copies) == 98
    put(folder, choice.name, PDF)
    before = state(folder)
    with pytest.raises(ff.FileConflictError) as raised:
        ff.choose_preserved_name(folder, family, digest)
    assert raised.value.kind == "variants_exhausted" and raised.value.family == family
    assert state(folder) == before and len(list(folder.iterdir())) == 99


def test_other_hashes_and_families_do_not_interfere(folder):
    digest = sha(PDF)
    put(folder, ff.variant_name("duplicate", digest[:16], 1), b"different")
    other = sha(b"another pdf")
    assert ff.choose_preserved_name(folder, "duplicate", other).number == 1
    assert ff.choose_preserved_name(folder, "superseded", digest).number == 1
    assert ff.choose_preserved_name(folder, "unarchived", digest).number == 1


@pytest.mark.parametrize("bad", ["", "A" * 64, "a" * 63, "g" * 64, None, 5])
def test_the_source_hash_must_be_a_full_lowercase_sha256(folder, bad):
    with pytest.raises(ff.FileOperationError):
        ff.choose_preserved_name(folder, "duplicate", bad)


def test_a_chosen_name_can_be_used_with_the_no_overwrite_rename(folder):
    digest = sha(PDF)
    put(folder, "report.unarchived.pdf")
    put(folder, ff.variant_name("duplicate", digest[:16], 1), PDF)  # an identical copy already preserved
    choice = ff.choose_preserved_name(folder, "duplicate", digest)
    ff.rename_noreplace(folder, "report.unarchived.pdf", choice.name)
    assert choice.number == 2 and (folder / choice.name).read_bytes() == PDF
    assert (folder / ff.variant_name("duplicate", digest[:16], 1)).read_bytes() == PDF
    assert not (folder / "report.unarchived.pdf").exists()


# --- these names and finalization eligibility ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("family", ["duplicate", "unarchived"])
@pytest.mark.parametrize("number", [1, 2, 99])
def test_duplicate_and_unarchived_copies_are_never_recognized_as_a_finalized_pdf(folder, family, number):
    digest = sha(PDF)
    put(folder, ff.variant_name(family, digest[:16], number), PDF)
    assert ha.FinalizedOnlyReader._check_pdf(folder, digest, len(PDF)) == (False, False)


@pytest.mark.parametrize("number", [1, 2, 17, 99])
def test_a_superseded_copy_is_recognized_by_the_reader(folder, number):
    digest = sha(PDF)
    put(folder, ff.variant_name("superseded", digest[:16], number), PDF)
    assert ha.FinalizedOnlyReader._check_pdf(folder, digest, len(PDF)) == (True, True)


def test_the_standard_report_name_is_recognized_by_the_reader(folder):
    put(folder, "report.pdf")
    assert ha.FinalizedOnlyReader._check_pdf(folder, sha(PDF), len(PDF)) == (True, True)


# --- link handling without needing the privilege to create real symbolic links --------------------------------------------------

class LinkOps(ff.FileOps):
    """Reports chosen names as symbolic links (or Windows reparse points), as a real link would look to lstat."""

    def __init__(self, names, reparse=False):
        self.names, self.reparse = set(names), reparse

    def lstat(self, path):
        real = os.lstat(path)
        if Path(path).name not in self.names:
            return real
        mode = real.st_mode if self.reparse else stat.S_IFLNK | 0o777
        fields = list(real)
        fields[0] = mode
        result = os.stat_result(fields)
        if not self.reparse:
            return result
        return type("ReparseStat", (), {"st_mode": mode, "st_size": real.st_size, "st_file_attributes": 0x400})()


@pytest.mark.parametrize("reparse", [False, True])
def test_links_and_reparse_points_are_refused_everywhere(folder, reparse):
    put(folder, "link.pdf")
    put(folder, "free.pdf")
    ops = LinkOps({"link.pdf"}, reparse=reparse)
    with pytest.raises(ff.FileOperationError, match="regular file"):
        ff.sha256_file(folder / "link.pdf", ops=ops)
    assert ff.validate_draft_pdf(folder / "link.pdf", ops=ops).problems == ("symlink",)
    with pytest.raises(ff.FileOperationError, match="regular file"):
        ff.rename_noreplace(folder, "link.pdf", "moved.pdf", ops=ops)
    assert (folder / "link.pdf").exists() and not (folder / "moved.pdf").exists()
    assert ff.sha256_file(folder / "free.pdf", ops=ops).sha256 == sha(PDF)  # other files are unaffected


@pytest.mark.parametrize("reparse", [False, True])
def test_a_link_sitting_on_a_preserved_name_is_a_conflict(folder, reparse):
    digest = sha(PDF)
    name = ff.variant_name("superseded", digest[:16], 1)
    put(folder, name, PDF)  # identical content, but it is reported as a link
    with pytest.raises(ff.FileConflictError) as raised:
        ff.choose_preserved_name(folder, "superseded", digest, ops=LinkOps({name}, reparse=reparse))
    assert raised.value.kind == "name_conflict"


def test_a_link_in_the_way_of_an_exclusive_write_is_a_conflict(folder):
    put(folder, "taken.json", b"x")
    with pytest.raises(ff.FileConflictError):
        ff.write_exclusive(folder, "taken.json", b"y", ops=LinkOps({"taken.json"}))
    assert (folder / "taken.json").read_bytes() == b"x"


def test_a_directory_reported_as_a_link_is_not_a_usable_folder(folder):
    with pytest.raises(ff.FileOperationError, match="real folder"):
        ff.write_exclusive(folder, "a.json", b"x", ops=LinkOps({folder.name}))
    assert list(folder.iterdir()) == []

# --- scope and safety --------------------------------------------------------------------------------------------------------------

def test_files_outside_the_folder_are_never_touched(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    sibling = tmp_path / "reviewer"
    sibling.mkdir()
    (sibling / "reviewer.x.json").write_bytes(b"keep")
    (tmp_path / "root.txt").write_bytes(b"keep")
    outside_before = {k: v for k, v in state(tmp_path).items() if "reports" not in k}
    put(reports, "report.unarchived.pdf")
    ff.rename_noreplace(reports, "report.unarchived.pdf", "report.pdf")
    ff.write_exclusive(reports, "note.txt", b"n")
    ff.write_partial_then_promote(reports, "x.json", b"{}", nonce="0123456789ab")
    ff.choose_preserved_name(reports, "duplicate", sha(PDF))
    with pytest.raises(ff.FileOperationError):
        ff.write_exclusive(reports, "../escape.txt", b"x")
    assert {k: v for k, v in state(tmp_path).items() if "reports" not in k} == outside_before


def test_no_function_deletes_or_overwrites_in_the_module_code():
    source = (ROOT / "src" / "seo_report" / "finalize_files.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for pattern in (r"os\.replace", r"shutil", r"os\.remove", r"rmtree", r"rmdir", r"\.mkdir\(", r"makedirs",
                    r"(?<!os)\.unlink\(", r"open\([^)]*['\"]w", r"\.write_bytes", r"\.write_text", r"copyfile|copy2|copytree", r"datetime",
                    r"\btime\.", r"sqlite", r"requests", r"googleapiclient"):
        assert not re.search(pattern, code), pattern
    assert code.count("os.unlink") == 1  # only the non-Windows link-then-unlink branch of rename_noreplace


def test_errors_never_contain_paths_or_system_text(folder, tmp_path):
    put(folder, "a.pdf")
    messages = []
    for call in (lambda: ff.rename_noreplace(folder, "a.pdf", "a.pdf"),
                 lambda: ff.rename_noreplace(folder, "nope.pdf", "b.pdf"),
                 lambda: ff.write_exclusive(folder, "a.pdf", b"x"),
                 lambda: ff.sha256_file(folder / "nope"),
                 lambda: ff.write_exclusive(folder, "b.json", b"x", ops=FaultOps(fail="write")),
                 lambda: ff.choose_preserved_name(folder, "duplicate", "x")):
        with pytest.raises(ff.FinalizeFileError) as raised:
            call()
        messages.append(str(raised.value))
    for message in messages:
        assert str(tmp_path) not in message and "injected" not in message and "WinError" not in message
        assert "Errno" not in message and "\\" not in message


def test_the_error_hierarchy():
    assert issubclass(ff.FileConflictError, ff.FinalizeFileError) and issubclass(ff.FileOperationError, ff.FinalizeFileError)
    assert not issubclass(ff.FileConflictError, ff.FileOperationError)
    assert issubclass(ff.FinalizeFileError, RuntimeError)
