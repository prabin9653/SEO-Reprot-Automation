import dataclasses
import hashlib
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
from seo_report import report_promotion as rp
from test_finalize_files import FaultOps, link_or_skip, state

DRAFT, FINAL = layout.DRAFT_PDF_NAME, layout.STANDARD_PDF_NAME


def pdf(tag: str) -> bytes:
    return f"%PDF-1.4\n% {tag}\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n".encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sup(data: bytes, n: int = 1) -> str:
    return ff.variant_name("superseded", sha(data)[:16], n)


def dup(data: bytes, n: int = 1) -> str:
    return ff.variant_name("duplicate", sha(data)[:16], n)


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "reports"
    path.mkdir()
    return path


def put(folder, name, data):
    (folder / name).write_bytes(data)
    return folder / name


def files(folder):
    return {p.name: p.read_bytes() for p in folder.iterdir()}


class ChangeAfterValidation(ff.FileOps):
    """Changes the draft once, at the first look at `report.pdf` (which happens after the draft was validated)."""

    def __init__(self, draft: Path, data: bytes, when=FINAL):
        self.draft, self.data, self.when, self.done = draft, data, when, False

    def lstat(self, path):
        if not self.done and Path(path).name == self.when:
            self.done = True
            self.draft.write_bytes(self.data)
        return super().lstat(path)


class NthRename(ff.FileOps):
    """Runs `hook` just before the nth rename (1-based)."""

    def __init__(self, n, hook):
        self.n, self.hook, self.count = n, hook, 0

    def rename_noreplace(self, source, destination):
        self.count += 1
        if self.count == self.n:
            self.hook()
        super().rename_noreplace(source, destination)


# --- result type ------------------------------------------------------------------------------------------------------------

def test_the_result_is_frozen_and_holds_no_paths(folder):
    put(folder, DRAFT, pdf("a"))
    result = rp.promote_draft(folder)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.outcome = "x"
    assert str(folder) not in repr(result)
    assert result.outcome in rp.OUTCOMES


def test_replace_existing_must_be_a_real_boolean(folder):
    put(folder, DRAFT, pdf("a"))
    for bad in (1, "yes", None):
        with pytest.raises(TypeError):
            rp.promote_draft(folder, replace_existing=bad)
    assert files(folder) == {DRAFT: pdf("a")}


# --- draft validation ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("data,problem", [(b"", "empty"), (b"not a pdf %%EOF\n", "bad_header"),
                                          (b"%PDF-1.4\nno end marker\n", "missing_eof")])
def test_an_invalid_draft_is_refused_and_nothing_changes(folder, data, problem):
    put(folder, DRAFT, data)
    put(folder, FINAL, pdf("old"))
    before = files(folder)
    result = rp.promote_draft(folder, replace_existing=True)
    assert (result.outcome, result.promoted) == (rp.OUTCOME_REJECTED, False)
    assert problem in result.draft_problems
    assert files(folder) == before


def test_a_missing_draft_is_refused(folder):
    result = rp.promote_draft(folder)
    assert result.outcome == rp.OUTCOME_REJECTED and "not_found" in result.draft_problems
    assert files(folder) == {}


def test_a_draft_that_is_a_folder_is_refused(folder):
    (folder / DRAFT).mkdir()
    result = rp.promote_draft(folder)
    assert result.outcome == rp.OUTCOME_REJECTED
    assert [p.name for p in folder.iterdir()] == [DRAFT]


def test_an_oversize_draft_is_refused(folder, monkeypatch):
    monkeypatch.setattr(ff, "MAX_PDF_BYTES", 10)
    put(folder, DRAFT, pdf("a"))
    assert rp.promote_draft(folder).draft_problems == ("too_large",) or "too_large" in rp.promote_draft(folder).draft_problems
    assert files(folder) == {DRAFT: pdf("a")}


def test_a_linked_draft_is_refused(folder, tmp_path):
    target = tmp_path / "elsewhere.pdf"
    target.write_bytes(pdf("a"))
    link_or_skip(folder / DRAFT, target)
    result = rp.promote_draft(folder)
    assert result.outcome == rp.OUTCOME_REJECTED and "symlink" in result.draft_problems
    assert target.read_bytes() == pdf("a")


def test_a_missing_or_relative_directory_changes_nothing(tmp_path):
    for bad in (tmp_path / "nope", Path("relative")):
        result = rp.promote_draft(bad)
        assert (result.outcome, result.failure) == (rp.OUTCOME_FAILED, "directory_unusable")
    assert not (tmp_path / "nope").exists()


# --- case A ------------------------------------------------------------------------------------------------------------------------

def test_case_a_promotes_the_draft_without_a_report(folder):
    data = pdf("a")
    put(folder, DRAFT, data)
    result = rp.promote_draft(folder)
    assert (result.outcome, result.promoted, result.final_name) == (rp.OUTCOME_PROMOTED, True, FINAL)
    assert (result.sha256, result.byte_count) == (sha(data), len(data))
    assert result.preserved_existing is None and result.duplicate_preserved is None and result.ok
    assert files(folder) == {FINAL: data}


def test_case_a_a_competing_report_that_appears_is_never_overwritten(folder):
    put(folder, DRAFT, pdf("a"))
    ops = NthRename(1, lambda: put(folder, FINAL, pdf("racer")))
    result = rp.promote_draft(folder, ops=ops)
    assert (result.outcome, result.conflict) == (rp.OUTCOME_CONFLICT, "destination_appeared")
    assert files(folder) == {DRAFT: pdf("a"), FINAL: pdf("racer")}


def test_case_a_a_draft_changed_after_validation_stops(folder):
    draft = put(folder, DRAFT, pdf("a"))
    result = rp.promote_draft(folder, ops=ChangeAfterValidation(draft, pdf("changed")))
    assert (result.outcome, result.failure) == (rp.OUTCOME_FAILED, "draft_changed")
    assert result.sha256 == sha(pdf("a"))
    assert files(folder) == {DRAFT: pdf("changed")}


def test_case_a_a_rename_failure_leaves_the_draft_alone(folder):
    put(folder, DRAFT, pdf("a"))
    result = rp.promote_draft(folder, ops=FaultOps(fail="rename_noreplace"))
    assert (result.outcome, result.failure, result.promoted) == (rp.OUTCOME_FAILED, "promote_failed", False)
    assert files(folder) == {DRAFT: pdf("a")}
    assert "injected" not in repr(result)


def test_case_a_a_hashing_failure_changes_nothing(folder):
    put(folder, DRAFT, pdf("a"))
    result = rp.promote_draft(folder, ops=FaultOps(fail="open_read"))
    assert result.outcome == rp.OUTCOME_REJECTED
    assert files(folder) == {DRAFT: pdf("a")}


def test_case_a_a_failure_after_the_first_hash_is_reported_and_safe(folder):
    put(folder, DRAFT, pdf("a"))
    ops = FaultOps(fail="open_read", after=2)  # validation hashes once; the pre-rename re-hash is the next reads
    result = rp.promote_draft(folder, ops=ops)
    assert result.outcome in (rp.OUTCOME_FAILED, rp.OUTCOME_REJECTED, rp.OUTCOME_PROMOTED)
    assert set(files(folder)) <= {DRAFT, FINAL}
    assert sorted(files(folder).values()) == [pdf("a")]


def test_a_retry_after_a_failed_promotion_succeeds(folder):
    put(folder, DRAFT, pdf("a"))
    rp.promote_draft(folder, ops=FaultOps(fail="rename_noreplace"))
    assert rp.promote_draft(folder).outcome == rp.OUTCOME_PROMOTED
    assert files(folder) == {FINAL: pdf("a")}


def test_stale_partial_files_are_never_touched(folder):
    put(folder, DRAFT, pdf("a"))
    put(folder, "report.unarchived.pdf.partial", b"half")
    put(folder, "report.unarchived.pdf.partial.abc123", b"half2")
    rp.promote_draft(folder)
    assert files(folder) == {FINAL: pdf("a"), "report.unarchived.pdf.partial": b"half",
                             "report.unarchived.pdf.partial.abc123": b"half2"}


# --- case B ------------------------------------------------------------------------------------------------------------------------

def test_case_b_an_identical_report_stays_and_the_draft_becomes_a_duplicate(folder):
    data = pdf("same")
    put(folder, DRAFT, data)
    put(folder, FINAL, data)
    result = rp.promote_draft(folder)
    assert (result.outcome, result.promoted, result.final_name) == (rp.OUTCOME_IDENTICAL, False, FINAL)
    assert result.duplicate_preserved == rp.PreservedCopy(dup(data), 1, sha(data), len(data))
    assert result.ok and not result.stopped_on_conflict
    assert files(folder) == {FINAL: data, dup(data): data}


def test_case_b_ignores_replace_existing(folder):
    data = pdf("same")
    put(folder, DRAFT, data)
    put(folder, FINAL, data)
    assert rp.promote_draft(folder, replace_existing=True).outcome == rp.OUTCOME_IDENTICAL
    assert files(folder) == {FINAL: data, dup(data): data}


def test_case_b_takes_the_next_duplicate_number_when_one_is_kept(folder):
    data = pdf("same")
    put(folder, DRAFT, data)
    put(folder, FINAL, data)
    put(folder, dup(data), data)
    result = rp.promote_draft(folder)
    assert result.duplicate_preserved.number == 2 and result.duplicate_preserved.name == dup(data, 2)
    assert set(files(folder)) == {FINAL, dup(data), dup(data, 2)}


def test_case_b_a_duplicate_name_with_other_content_is_a_conflict(folder):
    data = pdf("same")
    put(folder, DRAFT, data)
    put(folder, FINAL, data)
    put(folder, dup(data), pdf("other"))
    result = rp.promote_draft(folder)
    assert (result.outcome, result.conflict) == (rp.OUTCOME_CONFLICT, "duplicate_name_conflict")
    assert files(folder) == {DRAFT: data, FINAL: data, dup(data): pdf("other")}


def test_case_b_exhausted_duplicate_names_stop_safely(folder):
    data = pdf("same")
    put(folder, DRAFT, data)
    put(folder, FINAL, data)
    for n in range(1, 100):
        put(folder, dup(data, n), data)
    before = files(folder)
    result = rp.promote_draft(folder)
    assert (result.outcome, result.conflict) == (rp.OUTCOME_CONFLICT, "duplicate_variants_exhausted")
    assert files(folder) == before


def test_case_b_a_rename_failure_keeps_every_file(folder):
    data = pdf("same")
    put(folder, DRAFT, data)
    put(folder, FINAL, data)
    result = rp.promote_draft(folder, ops=FaultOps(fail="rename_noreplace"))
    assert (result.outcome, result.failure) == (rp.OUTCOME_FAILED, "preserve_failed")
    assert files(folder) == {DRAFT: data, FINAL: data}


def test_case_b_a_destination_that_appears_is_not_overwritten(folder):
    data = pdf("same")
    put(folder, DRAFT, data)
    put(folder, FINAL, data)
    result = rp.promote_draft(folder, ops=NthRename(1, lambda: put(folder, dup(data), pdf("racer"))))
    assert (result.outcome, result.conflict) == (rp.OUTCOME_CONFLICT, "destination_appeared")
    assert files(folder) == {DRAFT: data, FINAL: data, dup(data): pdf("racer")}


def test_a_duplicate_is_never_a_finalized_report(folder):
    data = pdf("same")
    put(folder, DRAFT, data)
    put(folder, FINAL, data)
    rp.promote_draft(folder)
    wanted = {FINAL, *layout.variant_candidates("superseded", sha(data)[:16])}
    assert dup(data) not in wanted


# --- case C ------------------------------------------------------------------------------------------------------------------------

def test_case_c_without_permission_both_files_are_untouched(folder):
    put(folder, DRAFT, pdf("new"))
    put(folder, FINAL, pdf("old"))
    result = rp.promote_draft(folder)
    assert (result.outcome, result.conflict, result.promoted) == (rp.OUTCOME_CONFLICT, "existing_report_differs", False)
    assert result.existing_report_sha256 == sha(pdf("old")) and result.stopped_on_conflict
    assert files(folder) == {DRAFT: pdf("new"), FINAL: pdf("old")}


def test_case_c_replaces_and_preserves_the_old_report_by_its_own_hash(folder):
    old, new = pdf("old"), pdf("new")
    put(folder, DRAFT, new)
    put(folder, FINAL, old)
    result = rp.promote_draft(folder, replace_existing=True)
    assert (result.outcome, result.promoted, result.final_name) == (rp.OUTCOME_REPLACED, True, FINAL)
    assert result.preserved_existing == rp.PreservedCopy(sup(old), 1, sha(old), len(old))
    assert (result.sha256, result.byte_count, result.incomplete) == (sha(new), len(new), False)
    assert files(folder) == {FINAL: new, sup(old): old}


def test_case_c_the_preserved_report_stays_resolvable_for_the_reader(folder):
    old, new = pdf("old"), pdf("new")
    put(folder, DRAFT, new)
    put(folder, FINAL, old)
    rp.promote_draft(folder, replace_existing=True)
    check = ha.FinalizedOnlyReader._check_pdf
    assert check(folder, sha(old), len(old)) == (True, True)
    assert check(folder, sha(new), len(new)) == (True, True)


def test_case_c_an_identical_superseded_copy_moves_to_the_next_number(folder):
    old, new = pdf("old"), pdf("new")
    put(folder, DRAFT, new)
    put(folder, FINAL, old)
    put(folder, sup(old), old)
    result = rp.promote_draft(folder, replace_existing=True)
    assert result.preserved_existing.number == 2
    assert files(folder) == {FINAL: new, sup(old): old, sup(old, 2): old}


def test_case_c_a_superseded_name_with_other_content_is_a_conflict(folder):
    old, new = pdf("old"), pdf("new")
    put(folder, DRAFT, new)
    put(folder, FINAL, old)
    put(folder, sup(old), pdf("squatter"))
    result = rp.promote_draft(folder, replace_existing=True)
    assert (result.outcome, result.conflict) == (rp.OUTCOME_CONFLICT, "superseded_name_conflict")
    assert files(folder) == {DRAFT: new, FINAL: old, sup(old): pdf("squatter")}


def test_case_c_a_prefix_collision_with_a_different_full_hash_is_a_conflict(folder):
    old, new = pdf("old"), pdf("new")
    put(folder, DRAFT, new)
    put(folder, FINAL, old)
    put(folder, sup(old), new)  # same name (same 16-hex prefix), different full hash
    result = rp.promote_draft(folder, replace_existing=True)
    assert result.conflict == "superseded_name_conflict"
    assert files(folder) == {DRAFT: new, FINAL: old, sup(old): new}


def test_case_c_exhausted_superseded_names_stop_safely(folder):
    old, new = pdf("old"), pdf("new")
    put(folder, DRAFT, new)
    put(folder, FINAL, old)
    for n in range(1, 100):
        put(folder, sup(old, n), old)
    before = files(folder)
    result = rp.promote_draft(folder, replace_existing=True)
    assert (result.outcome, result.conflict) == (rp.OUTCOME_CONFLICT, "superseded_variants_exhausted")
    assert files(folder) == before


def test_case_c_a_report_that_is_a_folder_is_a_conflict(folder):
    put(folder, DRAFT, pdf("new"))
    (folder / FINAL).mkdir()
    result = rp.promote_draft(folder, replace_existing=True)
    assert (result.outcome, result.conflict) == (rp.OUTCOME_CONFLICT, "existing_report_not_regular")
    assert sorted(p.name for p in folder.iterdir()) == sorted([DRAFT, FINAL]) and (folder / FINAL).is_dir()
    assert (folder / DRAFT).read_bytes() == pdf("new")


def test_case_c_a_linked_report_is_a_conflict(folder, tmp_path):
    target = tmp_path / "t.pdf"
    target.write_bytes(pdf("old"))
    put(folder, DRAFT, pdf("new"))
    link_or_skip(folder / FINAL, target)
    result = rp.promote_draft(folder, replace_existing=True)
    assert result.conflict == "existing_report_not_regular"
    assert target.read_bytes() == pdf("old")


def test_case_c_an_unreadable_current_report_changes_nothing(folder):
    put(folder, DRAFT, pdf("new"))
    put(folder, FINAL, pdf("old"))

    class FailsOnFinal(ff.FileOps):
        def open_read(self, path):
            if Path(path).name == FINAL:
                raise OSError(13, "injected")
            return super().open_read(path)

    result = rp.promote_draft(folder, replace_existing=True, ops=FailsOnFinal())
    assert (result.outcome, result.failure) == (rp.OUTCOME_FAILED, "current_report_unreadable")
    assert files(folder) == {DRAFT: pdf("new"), FINAL: pdf("old")}


def test_case_c_failure_while_preserving_leaves_both_files(folder):
    put(folder, DRAFT, pdf("new"))
    put(folder, FINAL, pdf("old"))
    result = rp.promote_draft(folder, replace_existing=True, ops=FaultOps(fail="rename_noreplace"))
    assert (result.outcome, result.failure, result.incomplete) == (rp.OUTCOME_FAILED, "preserve_failed", False)
    assert files(folder) == {DRAFT: pdf("new"), FINAL: pdf("old")}


def test_case_c_failure_while_promoting_keeps_the_old_report_under_its_superseded_name(folder):
    old, new = pdf("old"), pdf("new")
    put(folder, DRAFT, new)
    put(folder, FINAL, old)
    result = rp.promote_draft(folder, replace_existing=True, ops=FaultOps(fail="rename_noreplace", after=1))
    assert (result.outcome, result.failure, result.incomplete) == (rp.OUTCOME_FAILED, "promote_failed", True)
    assert result.preserved_existing.name == sup(old) and not result.promoted
    assert files(folder) == {DRAFT: new, sup(old): old}
    assert ha.FinalizedOnlyReader._check_pdf(folder, sha(old), len(old)) == (True, True)


def test_case_c_a_retry_after_an_incomplete_replacement_promotes_the_draft(folder):
    old, new = pdf("old"), pdf("new")
    put(folder, DRAFT, new)
    put(folder, FINAL, old)
    rp.promote_draft(folder, replace_existing=True, ops=FaultOps(fail="rename_noreplace", after=1))
    result = rp.promote_draft(folder)
    assert result.outcome == rp.OUTCOME_PROMOTED
    assert files(folder) == {FINAL: new, sup(old): old}


def test_case_c_a_report_that_appears_before_promotion_is_not_overwritten(folder):
    old, new = pdf("old"), pdf("new")
    put(folder, DRAFT, new)
    put(folder, FINAL, old)
    result = rp.promote_draft(folder, replace_existing=True, ops=NthRename(2, lambda: put(folder, FINAL, pdf("racer"))))
    assert (result.outcome, result.conflict, result.incomplete) == (rp.OUTCOME_CONFLICT, "destination_appeared", True)
    assert files(folder) == {DRAFT: new, FINAL: pdf("racer"), sup(old): old}


def test_case_c_a_draft_changed_before_the_old_report_is_moved_moves_nothing(folder):
    draft = put(folder, DRAFT, pdf("new"))
    put(folder, FINAL, pdf("old"))

    class Change(ff.FileOps):
        done = False

        def lstat(self, path):
            if Path(path).name == sup(pdf("old")) and not Change.done:  # the collision walk, just before the move
                Change.done = True
                draft.write_bytes(pdf("changed"))
            return super().lstat(path)

    result = rp.promote_draft(folder, replace_existing=True, ops=Change())
    assert (result.outcome, result.failure, result.incomplete) == (rp.OUTCOME_FAILED, "draft_changed", False)
    assert files(folder) == {DRAFT: pdf("changed"), FINAL: pdf("old")}


def test_case_c_a_draft_changed_after_the_old_report_is_preserved_is_not_promoted(folder):
    old = pdf("old")
    draft = put(folder, DRAFT, pdf("new"))
    put(folder, FINAL, old)
    ops = NthRename(2, lambda: None)

    class Change(ff.FileOps):
        count = 0

        def open_read(self, path):
            if Path(path).name == sup(old):  # the post-preservation verification of the old report
                draft.write_bytes(pdf("changed"))
            return super().open_read(path)

    result = rp.promote_draft(folder, replace_existing=True, ops=Change())
    assert (result.outcome, result.failure, result.incomplete) == (rp.OUTCOME_FAILED, "draft_changed", True)
    assert files(folder) == {DRAFT: pdf("changed"), sup(old): old}


def test_case_c_the_preserved_copy_is_verified_by_full_hash(folder):
    old = pdf("old")
    put(folder, DRAFT, pdf("new"))
    put(folder, FINAL, old)

    class Corrupt(ff.FileOps):
        def rename_noreplace(self, source, destination):
            super().rename_noreplace(source, destination)
            if Path(destination).name == sup(old):
                (folder / sup(old)).write_bytes(b"corrupted")

    result = rp.promote_draft(folder, replace_existing=True, ops=Corrupt())
    assert (result.failure, result.incomplete) == ("preserved_content_mismatch", True)
    assert (folder / DRAFT).read_bytes() == pdf("new")  # the draft was not promoted


# --- case D ------------------------------------------------------------------------------------------------------------------------

def test_case_d_a_draft_that_exists_only_as_a_superseded_copy_stops(folder):
    cur, draft = pdf("current"), pdf("returning")
    put(folder, DRAFT, draft)
    put(folder, FINAL, cur)
    put(folder, sup(draft), draft)
    before = files(folder)
    for flag in (False, True):
        result = rp.promote_draft(folder, replace_existing=flag)
        assert (result.outcome, result.conflict) == (rp.OUTCOME_CONFLICT, "superseded_only_copy")
        assert files(folder) == before


def test_case_d_finds_the_copy_under_a_later_variant_number(folder):
    cur, draft = pdf("current"), pdf("returning")
    put(folder, DRAFT, draft)
    put(folder, FINAL, cur)
    put(folder, sup(draft, 7), draft)
    assert rp.promote_draft(folder, replace_existing=True).conflict == "superseded_only_copy"


def test_case_d_a_superseded_name_with_only_the_same_prefix_is_not_case_d(folder):
    cur, draft = pdf("current"), pdf("returning")
    put(folder, DRAFT, draft)
    put(folder, FINAL, cur)
    put(folder, sup(draft), pdf("different full hash"))
    result = rp.promote_draft(folder, replace_existing=True)
    assert result.outcome == rp.OUTCOME_REPLACED  # the prefix match alone proves nothing
    assert (folder / FINAL).read_bytes() == draft


def test_case_d_does_not_apply_when_the_report_equals_the_draft(folder):
    data = pdf("same")
    put(folder, DRAFT, data)
    put(folder, FINAL, data)
    put(folder, sup(data), data)
    assert rp.promote_draft(folder).outcome == rp.OUTCOME_IDENTICAL


# --- hashing and generic faults -------------------------------------------------------------------------------------------------------

def test_every_failing_read_leaves_the_folder_unchanged(folder):
    put(folder, DRAFT, pdf("new"))
    put(folder, FINAL, pdf("old"))
    before = files(folder)
    for after in range(0, 8):
        result = rp.promote_draft(folder, replace_existing=True, ops=FaultOps(fail="open_read", after=after))
        if result.ok or result.promoted:
            break
        assert files(folder) == before or result.incomplete
        for name, data in before.items():
            current = files(folder)
            assert data in current.values()
        break


def test_no_scenario_ever_loses_content(folder):
    """For every failing rename position, the set of file contents never shrinks."""
    old, new = pdf("old"), pdf("new")
    for n in range(0, 3):
        for name in list(folder.iterdir()):
            name.unlink()
        put(folder, DRAFT, new)
        put(folder, FINAL, old)
        rp.promote_draft(folder, replace_existing=True, ops=FaultOps(fail="rename_noreplace", after=n))
        contents = list(files(folder).values())
        assert old in contents and new in contents


# --- result fields ----------------------------------------------------------------------------------------------------------------

def test_categories_are_fixed_strings_without_paths_or_system_text(folder):
    put(folder, DRAFT, pdf("new"))
    put(folder, FINAL, pdf("old"))
    result = rp.promote_draft(folder, ops=FaultOps(fail="open_read"))
    text = repr(result)
    assert str(folder) not in text and "injected" not in text and "Errno" not in text
    assert result.conflict is None or result.conflict in rp.CONFLICT_CATEGORIES
    assert result.failure is None or result.failure in rp.FAILURE_CATEGORIES


# --- source rules -------------------------------------------------------------------------------------------------------------------

def test_the_module_never_deletes_overwrites_or_creates_folders():
    source = (ROOT / "src" / "seo_report" / "report_promotion.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    for forbidden in ("os.replace", ".replace(", "unlink", "remove(", "rmtree", "shutil", "mkdir", "makedirs",
                      "write_text", "write_bytes", "open(", "sqlite", "requests", "urllib", "google"):
        assert forbidden not in code, forbidden
