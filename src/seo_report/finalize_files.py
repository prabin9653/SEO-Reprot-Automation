"""Safe file primitives for report finalization (Stage 6c-8a).

These are small, separately tested building blocks. They do not decide what to do with a report; the finalization
workflow built on them (a later stage) does.

  sha256_file             full SHA-256 and byte count of a regular file, read in chunks
  validate_draft_pdf      the preliminary draft checks: regular file, non-empty, size limit, `%PDF-` header,
                          `%%EOF` in the last 1,024 bytes, plus the hash and size (NOT a proof the PDF is valid)
  rename_noreplace        move a file to a name that must not exist yet
  write_exclusive         create a new file (fails if the name exists) and write bytes to it
  write_partial_then_promote
                          write to a unique partial file, then move it to its final name without overwriting
  variant_name / parse_variant_name / choose_preserved_name
                          the `report.<family>.<h16>[.<n>].pdf` names (families: duplicate, superseded, unarchived)
                          and the collision walk that picks a free name

Rules that hold everywhere
  * Nothing is ever overwritten. A destination that exists is a `FileConflictError`; there is no fallback to an
    operation that could overwrite, and `os.replace` is never used.
  * Nothing is ever deleted. A failed write or rename leaves whatever it already created, and says so; partial
    files are never removed or adopted automatically, however old they are.
  * Every operation works on a plain file name inside a directory that the caller supplies and that must already
    exist: no separators, no `..`, no drive or stream syntax. These functions never create directories.
  * Errors name the operation, the exception class and file names only: no paths and no raw system messages.

Platform assumption: Windows (the project's target). There, `os.rename` refuses an existing destination, which is
what makes the rename a no-overwrite operation. On other systems `os.rename` would overwrite, so the code uses
`os.link` (which refuses an existing destination) followed by `os.unlink` of the source instead; that branch is
untested here and, if the unlink fails, leaves both names.

The "unarchived" family is for preserving leftover drafts only. `FinalizedOnlyReader` recognizes only `report.pdf`
and `report.superseded.*`, so unarchived and duplicate copies can never make a run finalized.
"""

import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from seo_report import output_layout as layout
from seo_report import record_schema
from seo_report.reviewer_record import PdfChecks

MAX_PDF_BYTES = record_schema.MAX_PDF_BYTES
HASH_CHUNK = 1024 * 1024
PDF_HEADER = b"%PDF-"
EOF_MARKER = b"%%EOF"
EOF_WINDOW = 1024
FAMILIES = ("duplicate", "superseded", "unarchived")
MIN_VARIANT, MAX_VARIANT = layout.MIN_VARIANT, layout.MAX_VARIANT
MAX_FILENAME_CHARS = 120
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: symbolic links, junctions and similar

_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,%d}" % MAX_FILENAME_CHARS)
_NONCE = re.compile(r"[0-9a-f]{8,16}")
_PARTIAL = re.compile(r"\.[0-9a-f]{8}\.[0-9a-f]{8,16}\.partial")
_UNARCHIVED = re.compile(r"report\.unarchived\.([0-9a-f]{16})(?:\.([0-9]{1,3}))?\.pdf")
_NUMBER = re.compile(r"[2-9]|[1-9][0-9]")

# Problem codes of a draft check.
DRAFT_PROBLEMS = ("not_found", "symlink", "not_regular_file", "empty", "too_large", "bad_header", "missing_eof",
                  "unreadable", "changed_while_reading")


# --- errors ---------------------------------------------------------------------------------------------------------------

class FinalizeFileError(RuntimeError):
    """A file operation failed. `operation` and `cause_class` are safe to log; `left_behind` is True when the
    operation had already created a file (a partial one is never removed automatically)."""

    def __init__(self, message, *, operation: str, cause_class: str | None = None, left_behind: bool = False):
        super().__init__(message)
        self.operation = operation
        self.cause_class = cause_class
        self.left_behind = left_behind


class FileOperationError(FinalizeFileError):
    """An operating-system failure (permissions, a missing folder, a full disk, a locked file...)."""


class FileConflictError(FinalizeFileError):
    """The operation would have overwritten something, or no free name exists.

    `kind` is one of: destination_exists, same_path, name_conflict (a same-named file with different content),
    variants_exhausted (all 99 numbered names are taken).
    """

    def __init__(self, message, *, kind: str, operation: str, family: str | None = None, number: int | None = None,
                 left_behind: bool = False):
        super().__init__(message, operation=operation, left_behind=left_behind)
        self.kind = kind
        self.family = family
        self.number = number


# --- file operations (replaceable in tests) ---------------------------------------------------------------------------------

class FileOps:
    """The real file operations. Tests pass a subclass that fails at a chosen call to prove nothing is overwritten
    or lost when a step fails."""

    def lstat(self, path):
        return os.lstat(path)

    def open_read(self, path):
        return open(path, "rb")

    def create_exclusive(self, path):
        return open(path, "xb")  # FileExistsError when the name exists

    def write(self, handle, data: bytes) -> None:
        handle.write(data)

    def sync(self, handle) -> None:
        handle.flush()
        os.fsync(handle.fileno())

    def close(self, handle) -> None:
        handle.close()

    def rename_noreplace(self, source, destination) -> None:
        if os.name == "nt":
            os.rename(source, destination)  # on Windows an existing destination raises FileExistsError
        else:
            os.link(source, destination)  # refuses an existing destination
            os.unlink(source)


_DEFAULT_OPS = FileOps()


def _is_link(st) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _fail(operation: str, err: OSError, *, left_behind: bool = False) -> FileOperationError:
    return FileOperationError(f"file operation '{operation}' failed ({type(err).__name__})", operation=operation,
                              cause_class=type(err).__name__, left_behind=left_behind)


# --- names and locations ------------------------------------------------------------------------------------------------------

def _safe_name(name, where: str = "file name") -> str:
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name) or name in (".", "..") \
            or name.endswith("."):
        raise FileOperationError(f"{where} is not a plain file name", operation="validate_name")
    return name


def _location(directory, name, ops) -> Path:
    """`directory / name`, after checking the directory is a real, absolute, existing folder and the path fits."""
    folder = Path(directory)
    if not folder.is_absolute():
        raise FileOperationError("the directory must be an absolute path", operation="validate_directory")
    _safe_name(name)
    try:
        st = ops.lstat(folder)
    except OSError as err:
        raise _fail("validate_directory", err) from None
    if not stat.S_ISDIR(st.st_mode) or _is_link(st):
        raise FileOperationError("the directory is not a real folder", operation="validate_directory")
    target = folder / name
    if target.parent != folder or len(str(target)) > layout.MAX_PATH_CHARS:
        raise FileOperationError("the path is outside the folder or too long", operation="validate_name")
    return target


def _exists(ops, path) -> bool:
    try:
        ops.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as err:
        raise _fail("lstat", err) from None
    return True


# --- hashing ----------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FileDigest:
    sha256: str  # 64 lowercase hexadecimal characters
    byte_count: int


def _digest(handle) -> FileDigest:
    sha, count = hashlib.sha256(), 0
    for chunk in iter(lambda: handle.read(HASH_CHUNK), b""):
        sha.update(chunk)
        count += len(chunk)
    return FileDigest(sha.hexdigest(), count)


def sha256_file(path, *, ops: FileOps | None = None) -> FileDigest:
    """Full SHA-256 and byte count of a regular file, streamed in `HASH_CHUNK` pieces.

    Missing files, directories, symbolic links and other non-regular files raise FileOperationError.
    """
    ops = ops or _DEFAULT_OPS
    try:
        st = ops.lstat(path)
    except OSError as err:
        raise _fail("hash", err) from None
    if _is_link(st) or not stat.S_ISREG(st.st_mode):
        raise FileOperationError("only a regular file (not a link or folder) can be hashed", operation="hash")
    try:
        with ops.open_read(path) as handle:
            result = _digest(handle)
    except OSError as err:
        raise _fail("hash", err) from None
    if result.byte_count != st.st_size:
        raise FileOperationError("the file changed while it was being read", operation="hash")
    return result


# --- draft PDF checks ---------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DraftCheck:
    """The outcome of the preliminary draft checks. `checks` is the `PdfChecks` the reviewer record stores; all
    three must be true (and `problems` empty) for a draft to be finalized. These checks do not prove that the PDF
    is complete, valid or readable."""

    ok: bool
    problems: tuple[str, ...]
    checks: PdfChecks
    sha256: str | None = None
    byte_count: int | None = None


def _rejected(problem: str, **checks) -> DraftCheck:
    return DraftCheck(False, (problem,), PdfChecks(**{"non_empty": False, "header": False, "eof_marker": False,
                                                      **checks}))


def validate_draft_pdf(path, *, ops: FileOps | None = None) -> DraftCheck:
    """Check a draft PDF: regular file (no link), non-empty, at most MAX_PDF_BYTES, `%PDF-` first, `%%EOF` within
    the last 1,024 bytes; and hash it in one streaming pass. Problems are reported, never raised."""
    ops = ops or _DEFAULT_OPS
    try:
        st = ops.lstat(path)
    except FileNotFoundError:
        return _rejected("not_found")
    except OSError:
        return _rejected("unreadable")
    if _is_link(st):
        return _rejected("symlink")
    if not stat.S_ISREG(st.st_mode):
        return _rejected("not_regular_file")
    if st.st_size == 0:
        return _rejected("empty")
    if st.st_size > MAX_PDF_BYTES:
        return _rejected("too_large", non_empty=True)
    try:
        with ops.open_read(path) as handle:
            header_ok = handle.read(len(PDF_HEADER)) == PDF_HEADER
            handle.seek(max(0, st.st_size - EOF_WINDOW))
            eof_ok = EOF_MARKER in handle.read(EOF_WINDOW)
            handle.seek(0)
            digest = _digest(handle)
    except OSError:
        return _rejected("unreadable", non_empty=True)
    if digest.byte_count != st.st_size:
        return _rejected("changed_while_reading", non_empty=True)
    problems = tuple(p for p, bad in (("bad_header", not header_ok), ("missing_eof", not eof_ok)) if bad)
    return DraftCheck(not problems, problems, PdfChecks(True, header_ok, eof_ok, True), digest.sha256,
                      digest.byte_count)


# --- no-overwrite operations -----------------------------------------------------------------------------------------------------------

def rename_noreplace(directory, source_name: str, destination_name: str, *, ops: FileOps | None = None) -> None:
    """Move `source_name` to `destination_name` in the same folder. The destination must not exist.

    Raises FileConflictError when it does (or when both names are the same file), FileOperationError for any other
    failure. In every failure case the source and any existing destination are left exactly as they were.
    """
    ops = ops or _DEFAULT_OPS
    source = _location(directory, source_name, ops)
    destination = _location(directory, destination_name, ops)
    if os.path.normcase(source_name) == os.path.normcase(destination_name):
        raise FileConflictError("the source and destination names are the same", kind="same_path",
                                operation="rename")
    try:
        st = ops.lstat(source)
    except OSError as err:
        raise _fail("rename", err) from None
    if _is_link(st) or not stat.S_ISREG(st.st_mode):
        raise FileOperationError("only a regular file can be renamed", operation="rename")
    if _exists(ops, destination):
        raise FileConflictError(f"{destination_name} already exists", kind="destination_exists", operation="rename")
    try:
        ops.rename_noreplace(source, destination)
    except FileExistsError:
        raise FileConflictError(f"{destination_name} already exists", kind="destination_exists",
                                operation="rename") from None
    except OSError as err:
        raise _fail("rename", err) from None


@dataclass(frozen=True)
class WrittenFile:
    name: str
    byte_count: int
    sha256: str


def _write_new(ops, path, name, data: bytes) -> None:
    if _exists(ops, path):  # any existing entry (file, folder, link) counts: the name is taken
        raise FileConflictError(f"{name} already exists", kind="destination_exists", operation="create")
    try:
        handle = ops.create_exclusive(path)
    except FileExistsError:
        raise FileConflictError(f"{name} already exists", kind="destination_exists", operation="create") from None
    except OSError as err:
        raise _fail("create", err) from None
    try:
        ops.write(handle, data)
        ops.sync(handle)
    except OSError as err:
        try:
            ops.close(handle)
        except OSError:
            pass
        raise _fail("write", err, left_behind=True) from None
    try:
        ops.close(handle)
    except OSError as err:
        raise _fail("close", err, left_behind=True) from None


def write_exclusive(directory, name: str, data: bytes, *, ops: FileOps | None = None) -> WrittenFile:
    """Create `name` (it must not exist), write `data`, flush to disk and close.

    If the write fails after the file was created, the incomplete file is left in place and the error says so
    (`left_behind`); nothing is deleted.
    """
    ops = ops or _DEFAULT_OPS
    if not isinstance(data, (bytes, bytearray)):
        raise FileOperationError("data must be bytes", operation="write")
    path = _location(directory, name, ops)
    _write_new(ops, path, name, bytes(data))
    return WrittenFile(name, len(data), hashlib.sha256(data).hexdigest())


def new_nonce() -> str:
    """12 random lowercase hex characters, for a partial file name."""
    return secrets.token_hex(6)


def partial_name(final_name: str, nonce: str) -> str:
    """`.<8 hex of the final name's hash>.<nonce>.partial`: tied to its final name, unique per nonce, short enough
    for the reserved file-name length, and never matched by the reader's record or PDF name patterns."""
    _safe_name(final_name)
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        raise FileOperationError("the nonce must be 8 to 16 lowercase hex characters", operation="validate_name")
    return f".{hashlib.sha256(final_name.encode('ascii')).hexdigest()[:8]}.{nonce}.partial"


def is_partial_name(name) -> bool:
    return isinstance(name, str) and _PARTIAL.fullmatch(name) is not None


def write_partial_then_promote(directory, final_name: str, data: bytes, *, nonce: str | None = None,
                               ops: FileOps | None = None) -> WrittenFile:
    """Write `data` to a unique partial file and then move it to `final_name`, which must not exist.

    The final name never holds incomplete data. A failure leaves the partial file in place (reported through
    `left_behind`) and the final name untouched; stale partials are never removed here.
    """
    ops = ops or _DEFAULT_OPS
    if not isinstance(data, (bytes, bytearray)):
        raise FileOperationError("data must be bytes", operation="write")
    final = _location(directory, final_name, ops)
    partial = partial_name(final_name, nonce if nonce is not None else new_nonce())
    if _exists(ops, final):
        raise FileConflictError(f"{final_name} already exists", kind="destination_exists", operation="create")
    path = _location(directory, partial, ops)
    _write_new(ops, path, partial, bytes(data))
    try:
        rename_noreplace(directory, partial, final_name, ops=ops)
    except FinalizeFileError as err:
        err.left_behind = True  # the partial file is still there
        raise
    return WrittenFile(final_name, len(data), hashlib.sha256(data).hexdigest())


# --- preserved-copy names -----------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class VariantName:
    family: str
    hash16: str
    number: int


def variant_name(family: str, hash16: str, number: int = 1) -> str:
    """`report.<family>.<h16>.pdf` for variant 1 and `report.<family>.<h16>.<n>.pdf` for n = 2..99."""
    if family not in FAMILIES:
        raise FileOperationError(f"the family must be one of {list(FAMILIES)}", operation="validate_name")
    try:
        if family == "unarchived":
            layout.validate_hash16(hash16)
            if isinstance(number, bool) or not isinstance(number, int) or not MIN_VARIANT <= number <= MAX_VARIANT:
                raise layout.OutputLayoutError("bad variant number")
            return f"report.unarchived.{hash16}.pdf" if number == 1 else f"report.unarchived.{hash16}.{number}.pdf"
        return layout.variant_pdf_name(family, hash16, number)
    except layout.OutputLayoutError as err:
        raise FileOperationError(str(err), operation="validate_name") from None


def parse_variant_name(name) -> VariantName:
    """Strict parser for the three families. Anything else (case, extra suffixes, 0/1/100, leading zeros,
    separators, other extensions) raises FileOperationError."""
    if isinstance(name, str):
        match = _UNARCHIVED.fullmatch(name)
        if match:
            hash16, text = match.groups()
            if text is None:
                return VariantName("unarchived", hash16, 1)
            if _NUMBER.fullmatch(text):
                return VariantName("unarchived", hash16, int(text))
        else:
            try:
                parsed = layout.parse_variant_pdf_name(name)
            except layout.OutputLayoutError:
                parsed = None
            if parsed is not None:
                return VariantName(parsed.kind, parsed.hash16, parsed.number)
    raise FileOperationError("not a preserved-copy file name", operation="validate_name")


def variant_candidates(family: str, hash16: str) -> tuple[str, ...]:
    """All 99 names of a family for a hash, in the order they are tried."""
    return tuple(variant_name(family, hash16, n) for n in range(MIN_VARIANT, MAX_VARIANT + 1))


@dataclass(frozen=True)
class PreservedChoice:
    name: str  # the free name to use
    number: int
    identical_copies: tuple[int, ...]  # variant numbers that already hold an identical copy (kept, never removed)


def choose_preserved_name(directory, family: str, source_sha256: str, *, ops: FileOps | None = None) -> PreservedChoice:
    """Pick the name under which a file with this full SHA-256 can be preserved, without touching anything.

    Walks n = 1, 2, 3... for `report.<family>.<h16>[.<n>].pdf`:
      * name free                                    -> use it
      * name exists with the same full SHA-256       -> keep it, try the next number
      * name exists with different content (or is not a regular file)
                                                     -> FileConflictError(kind="name_conflict"), nothing changes
      * all 99 names taken by identical copies       -> FileConflictError(kind="variants_exhausted")
    Only the full hash decides sameness: the 16-character prefix alone never does.
    """
    ops = ops or _DEFAULT_OPS
    try:
        layout.validate_sha256(source_sha256)
    except layout.OutputLayoutError as err:
        raise FileOperationError(str(err), operation="validate_name") from None
    hash16 = source_sha256[:layout.HASH16_CHARS]
    identical = []
    for number in range(MIN_VARIANT, MAX_VARIANT + 1):
        name = variant_name(family, hash16, number)
        path = _location(directory, name, ops)
        try:
            st = ops.lstat(path)
        except FileNotFoundError:
            return PreservedChoice(name, number, tuple(identical))
        except OSError as err:
            raise _fail("lstat", err) from None
        if _is_link(st) or not stat.S_ISREG(st.st_mode) or sha256_file(path, ops=ops).sha256 != source_sha256:
            raise FileConflictError(f"{name} exists with different content", kind="name_conflict",
                                    operation="choose_name", family=family, number=number)
        identical.append(number)
    raise FileConflictError(f"all {MAX_VARIANT} {family} names for this hash are taken", kind="variants_exhausted",
                            operation="choose_name", family=family)
