"""Promoting a validated draft PDF to `report.pdf` without losing or overwriting any report (Stage 6c-8d).

`promote_draft(directory)` works in one period's reports folder (it must already exist) on three fixed names:
the draft `report.unarchived.pdf`, the current report `report.pdf`, and preserved copies
`report.superseded.<h16>[.<n>].pdf` / `report.duplicate.<h16>[.<n>].pdf`. It uses only the file primitives of
`finalize_files` (no-overwrite rename, collision walk, streaming hash) and never deletes, overwrites, creates a
folder or touches any other name, including stale partial files.

Order of work
  1. Validate the draft (`validate_draft_pdf`): regular file, no link, non-empty, size limit, `%PDF-` header, `%%EOF`
     near the end. Its full SHA-256 and byte count are the values used for the whole operation. Any problem stops
     here (`draft_rejected`) with nothing changed. The draft is re-hashed before every move; a change stops the
     operation (`draft_changed`).
  2. Look at `report.pdf` and decide:

     A  no `report.pdf`                -> rename the draft to `report.pdf` (no overwrite).        outcome promoted
     B  `report.pdf` has the same full SHA-256
                                       -> `report.pdf` stays; the draft is preserved as
                                          `report.duplicate.<h16>[.n].pdf`.                     outcome identical_existing
     C  `report.pdf` differs           -> only with `replace_existing=True` (default False). The current report is
                                          hashed, preserved as `report.superseded.<h16>[.n].pdf` (h16 from ITS full
                                          hash), the draft is re-checked, then renamed to `report.pdf`.
                                                                                                outcome replaced
                                          Without the flag: conflict `existing_report_differs`, nothing changes.
     D  as C, but the draft's own full hash already exists under a recognized superseded name
                                       -> conflict `superseded_only_copy`; nothing is restored, promoted or moved.

  3. The result of the last rename is verified by hashing the file at its new name.

Stops and states
  * A conflict (the name is taken, content differs, a variant is exhausted, a file appeared meanwhile) returns
    outcome `conflict` with a fixed category; an operating-system failure returns outcome `failed` with a fixed
    category. Neither raises, and neither deletes or repairs anything.
  * In case C, once the old report has been preserved, `report.pdf` is absent until the draft is promoted. If the
    promotion then fails, the old report stays available under its superseded name (the finalized-only reader
    finds it there), the draft stays as `report.unarchived.pdf`, and the result says `incomplete=True`. Nothing is
    restored automatically; a retry finds no `report.pdf` and simply promotes the draft (case A).
  * "Validating" the current report means: a regular file (no link) that can be read and hashed. It is preserved
    whether or not it is a well-formed PDF, because nothing is ever deleted.
  * The `report.unarchived.<h16>` preservation family is not used here: preserving a leftover draft that cannot
    take the duplicate or superseded flow belongs to the renderer and the reconciliation step, so such situations
    stop with a conflict instead.

The result never contains a path or operating-system text. The categories below are descriptions for the caller;
they are not attempt status codes (the orchestrator maps them).
"""

import stat
from dataclasses import dataclass
from pathlib import Path

from seo_report import finalize_files as ff
from seo_report import output_layout as layout

DRAFT_NAME = layout.DRAFT_PDF_NAME
FINAL_NAME = layout.STANDARD_PDF_NAME

OUTCOME_PROMOTED = "promoted"
OUTCOME_REPLACED = "replaced"
OUTCOME_IDENTICAL = "identical_existing"
OUTCOME_CONFLICT = "conflict"
OUTCOME_REJECTED = "draft_rejected"
OUTCOME_FAILED = "failed"
OUTCOMES = (OUTCOME_PROMOTED, OUTCOME_REPLACED, OUTCOME_IDENTICAL, OUTCOME_CONFLICT, OUTCOME_REJECTED, OUTCOME_FAILED)

CONFLICT_CATEGORIES = (
    "existing_report_differs",  # case C without replace_existing
    "superseded_only_copy",  # case D
    "existing_report_not_regular",  # report.pdf is a folder or link
    "destination_appeared",  # a file appeared at a name we were about to create
    "superseded_name_conflict",  # a superseded name holds different content
    "superseded_variants_exhausted",
    "duplicate_name_conflict",  # a duplicate name holds different content
    "duplicate_variants_exhausted",
)
FAILURE_CATEGORIES = (
    "directory_unusable", "current_report_unreadable", "draft_changed", "preserve_failed", "promote_failed",
    "preserved_content_mismatch", "promoted_content_mismatch", "scan_failed",
)


@dataclass(frozen=True)
class PreservedCopy:
    """A file kept under a preserved name. Names only, never paths."""

    name: str
    number: int
    sha256: str
    byte_count: int


@dataclass(frozen=True)
class PromotionResult:
    outcome: str  # one of OUTCOMES
    promoted: bool  # the draft is now `report.pdf`
    final_name: str | None  # "report.pdf" when it holds the intended content (promoted, replaced, identical)
    sha256: str | None  # the validated draft's full SHA-256
    byte_count: int | None
    existing_report_sha256: str | None = None  # the report that was at `report.pdf` when the operation began
    preserved_existing: PreservedCopy | None = None  # that report, now under a superseded name (case C)
    duplicate_preserved: PreservedCopy | None = None  # the draft, kept as a duplicate copy (case B)
    conflict: str | None = None  # one of CONFLICT_CATEGORIES
    failure: str | None = None  # one of FAILURE_CATEGORIES
    draft_problems: tuple[str, ...] = ()  # codes from `finalize_files.DRAFT_PROBLEMS`
    incomplete: bool = False  # the old report was preserved but the draft was not promoted

    @property
    def ok(self) -> bool:
        return self.outcome in (OUTCOME_PROMOTED, OUTCOME_REPLACED, OUTCOME_IDENTICAL)

    @property
    def stopped_on_conflict(self) -> bool:
        return self.outcome == OUTCOME_CONFLICT


# --- helpers -----------------------------------------------------------------------------------------------------------------

def _hash(path, ops):
    try:
        return ff.sha256_file(path, ops=ops)
    except ff.FileOperationError:
        return None


def _draft_unchanged(path, ops, sha256: str, byte_count: int) -> bool:
    digest = _hash(path, ops)
    return digest is not None and (digest.sha256, digest.byte_count) == (sha256, byte_count)


def _conflict_for(err: ff.FileConflictError, family: str) -> str:
    if err.kind == "variants_exhausted":
        return f"{family}_variants_exhausted"
    if err.kind == "name_conflict":
        return f"{family}_name_conflict"
    return "destination_appeared"


def _superseded_copy_of(directory, ops, sha256: str, byte_count: int) -> bool:
    """True when a recognized superseded name already holds a file with exactly this full SHA-256."""
    for name in ff.variant_candidates("superseded", sha256[:layout.HASH16_CHARS]):
        path = Path(directory) / name
        try:
            st = ops.lstat(path)
        except OSError:  # absent (or unreadable, which cannot be the draft's copy)
            continue
        if ff._is_link(st) or not stat.S_ISREG(st.st_mode) or st.st_size != byte_count:
            continue
        digest = _hash(path, ops)
        if digest is not None and digest.sha256 == sha256:
            return True
    return False


# --- the operation --------------------------------------------------------------------------------------------------------------

def promote_draft(directory, *, replace_existing: bool = False, ops: ff.FileOps | None = None) -> PromotionResult:
    """Promote `report.unarchived.pdf` to `report.pdf` in `directory`, following the rules in the module docstring.

    `directory` is the period's reports folder (absolute, existing). `replace_existing` must be True to replace a
    `report.pdf` whose content differs from the draft; the default refuses. Returns a PromotionResult; a bad
    argument type raises TypeError.
    """
    if not isinstance(replace_existing, bool):
        raise TypeError("replace_existing must be True or False")
    ops = ops or ff.FileOps()

    def failed(category, **fields):
        return PromotionResult(OUTCOME_FAILED, False, None, fields.pop("sha256", None), fields.pop("byte_count", None),
                               failure=category, **fields)

    try:
        draft_path = ff._location(directory, DRAFT_NAME, ops)
        final_path = ff._location(directory, FINAL_NAME, ops)
    except ff.FileOperationError:
        return failed("directory_unusable")

    # 1. the draft
    check = ff.validate_draft_pdf(draft_path, ops=ops)
    if not check.ok:
        return PromotionResult(OUTCOME_REJECTED, False, None, check.sha256, check.byte_count,
                               draft_problems=check.problems)
    sha, size = check.sha256, check.byte_count

    def stopped(category, **fields):
        return PromotionResult(OUTCOME_CONFLICT, False, None, sha, size, conflict=category, **fields)

    def broken(category, **fields):
        return PromotionResult(OUTCOME_FAILED, False, None, sha, size, failure=category, **fields)

    # 2. the current report
    try:
        current = ops.lstat(final_path)
    except FileNotFoundError:
        current = None
    except OSError:
        return broken("current_report_unreadable")
    current_sha = current_size = None
    if current is not None:
        if ff._is_link(current) or not stat.S_ISREG(current.st_mode):
            return stopped("existing_report_not_regular")
        digest = _hash(final_path, ops)
        if digest is None:
            return broken("current_report_unreadable")
        current_sha, current_size = digest.sha256, digest.byte_count

    # case A
    if current is None:
        return _promote(directory, draft_path, final_path, ops, sha, size, OUTCOME_PROMOTED, stopped, broken)

    # case B
    if current_sha == sha:
        try:
            choice = ff.choose_preserved_name(directory, "duplicate", sha, ops=ops)
        except ff.FileConflictError as err:
            return stopped(_conflict_for(err, "duplicate"), existing_report_sha256=current_sha)
        except ff.FileOperationError:
            return broken("scan_failed", existing_report_sha256=current_sha)
        if not _draft_unchanged(draft_path, ops, sha, size):
            return broken("draft_changed", existing_report_sha256=current_sha)
        try:
            ff.rename_noreplace(directory, DRAFT_NAME, choice.name, ops=ops)
        except ff.FileConflictError:
            return stopped("destination_appeared", existing_report_sha256=current_sha)
        except ff.FileOperationError:
            return broken("preserve_failed", existing_report_sha256=current_sha)
        copy = PreservedCopy(choice.name, choice.number, sha, size)
        return PromotionResult(OUTCOME_IDENTICAL, False, FINAL_NAME, sha, size, existing_report_sha256=current_sha,
                               duplicate_preserved=copy)

    # case D (checked before C so that it is reported even with replace_existing=True)
    if _superseded_copy_of(directory, ops, sha, size):
        return stopped("superseded_only_copy", existing_report_sha256=current_sha)

    # case C
    if not replace_existing:
        return stopped("existing_report_differs", existing_report_sha256=current_sha)
    try:
        choice = ff.choose_preserved_name(directory, "superseded", current_sha, ops=ops)
    except ff.FileConflictError as err:
        return stopped(_conflict_for(err, "superseded"), existing_report_sha256=current_sha)
    except ff.FileOperationError:
        return broken("scan_failed", existing_report_sha256=current_sha)
    if not _draft_unchanged(draft_path, ops, sha, size):  # before the old report is moved
        return broken("draft_changed", existing_report_sha256=current_sha)
    try:
        ff.rename_noreplace(directory, FINAL_NAME, choice.name, ops=ops)
    except ff.FileConflictError:
        return stopped("destination_appeared", existing_report_sha256=current_sha)
    except ff.FileOperationError:
        return broken("preserve_failed", existing_report_sha256=current_sha)
    preserved = PreservedCopy(choice.name, choice.number, current_sha, current_size)
    after = _hash(Path(directory) / choice.name, ops)
    if after is None or after.sha256 != current_sha:
        return broken("preserved_content_mismatch", existing_report_sha256=current_sha, preserved_existing=preserved,
                      incomplete=True)
    if not _draft_unchanged(draft_path, ops, sha, size):  # again, now that the old report has been preserved
        return broken("draft_changed", existing_report_sha256=current_sha, preserved_existing=preserved,
                      incomplete=True)
    try:
        ff.rename_noreplace(directory, DRAFT_NAME, FINAL_NAME, ops=ops)
    except ff.FileConflictError:
        return stopped("destination_appeared", existing_report_sha256=current_sha, preserved_existing=preserved,
                       incomplete=True)
    except ff.FileOperationError:
        return broken("promote_failed", existing_report_sha256=current_sha, preserved_existing=preserved,
                      incomplete=True)
    done = _hash(final_path, ops)
    if done is None or (done.sha256, done.byte_count) != (sha, size):
        return PromotionResult(OUTCOME_FAILED, True, None, sha, size, existing_report_sha256=current_sha,
                               preserved_existing=preserved, failure="promoted_content_mismatch")
    return PromotionResult(OUTCOME_REPLACED, True, FINAL_NAME, sha, size, existing_report_sha256=current_sha,
                           preserved_existing=preserved)


def _promote(directory, draft_path, final_path, ops, sha, size, outcome, stopped, broken) -> PromotionResult:
    """Case A: rename the draft to `report.pdf`, then verify the content at its new name."""
    if not _draft_unchanged(draft_path, ops, sha, size):
        return broken("draft_changed")
    try:
        ff.rename_noreplace(directory, DRAFT_NAME, FINAL_NAME, ops=ops)
    except ff.FileConflictError:
        return stopped("destination_appeared")
    except ff.FileOperationError:
        return broken("promote_failed")
    done = _hash(final_path, ops)
    if done is None or (done.sha256, done.byte_count) != (sha, size):
        return PromotionResult(OUTCOME_FAILED, True, None, sha, size, failure="promoted_content_mismatch")
    return PromotionResult(outcome, True, FINAL_NAME, sha, size)
