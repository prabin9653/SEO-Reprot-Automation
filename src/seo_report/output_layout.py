"""Where generated reports and reviewer records live, and what their files are called.

Layout (the root is chosen below; nothing here creates it):

    <root>/reports/<client_key>/<period_start>_<period_end>/report.pdf
    <root>/reviewer/<client_key>/<period_start>_<period_end>/...
    <root>/attempts/<client_key>/<period_start>_<period_end>/...

Root precedence: explicit path, then $JASPER_SEO_OUTPUT_ROOT, then %LOCALAPPDATA%\\JasperSEOReporting\\output.
The root is separate from the history database ($JASPER_SEO_HISTORY_DB) and must not overlap it in either
direction. It must be absolute, must not be a drive root, and must not sit in a OneDrive folder unless the
caller passes allow_onedrive_output=True or $JASPER_SEO_ALLOW_ONEDRIVE_OUTPUT is exactly "1".

A PDF is never renamed, overwritten or deleted. The standard file is `report.pdf`; preserved copies of a
replaced or repeated report are named `report.superseded.<h16>.pdf` / `report.duplicate.<h16>.pdf`, then
`...<h16>.<n>.pdf` for n = 2..99, where <h16> is the first 16 lowercase hex characters of the PDF's SHA-256.

Other file names (Revision 4, sections 3 and 5):

    reports/...   report.pdf, report.superseded.<h16>[.<n>].pdf, report.duplicate.<h16>[.<n>].pdf,
                  report.unarchived.pdf, report.unarchived.pdf.partial
    reviewer/...  reviewer.<run_id>.<pdf16>.json
    attempts/...  <timestamp>.<pdf16|nopdf>.<step>.attempt.json

Path length: the full path of every file this module can name is checked against the Windows limit using the
actual client-key folder, period folder and file name. A folder path is accepted only if the longest file name
that may live in it still fits, so a folder path that was accepted can hold any of its files. The root-length
limit in `resolve_output_root` is only a necessary condition (it assumes the shortest possible client key);
the guarantee comes from the folder and file path functions.

Everything here is pure: no directory is created and no file is read, written or listed. The only file
system call is the path resolution that the cloud-sync check performs.
"""

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path

from seo_report.client_config import ClientConfigError, validate_client_key
from seo_report.snapshots.store import in_cloud_sync_folder, resolve_db_path

ENV_OUTPUT_ROOT = "JASPER_SEO_OUTPUT_ROOT"
ENV_ALLOW_ONEDRIVE = "JASPER_SEO_ALLOW_ONEDRIVE_OUTPUT"

REPORTS_DIR = "reports"
REVIEWER_DIR = "reviewer"
ATTEMPTS_DIR = "attempts"
SUBFOLDERS = (REPORTS_DIR, REVIEWER_DIR, ATTEMPTS_DIR)

STANDARD_PDF_NAME = "report.pdf"
VARIANT_KINDS = ("superseded", "duplicate")
MIN_VARIANT = 1
MAX_VARIANT = 99
HASH16_CHARS = 16

DRAFT_PDF_NAME = "report.unarchived.pdf"
DRAFT_PARTIAL_NAME = "report.unarchived.pdf.partial"

# The approved attempt steps (Revision 4, section 5).
ATTEMPT_STEPS = ("preflight", "verify_draft", "persist", "event", "supersede", "promote", "record")
# Assumed (Revision 4 says only "a numeric suffix on collision"): `.<n>` before `.json`, n = 2..999.
MAX_ATTEMPT_COLLISION = 999

# Windows MAX_PATH is 260 including the terminating NUL.
MAX_PATH_CHARS = 259
PERIOD_DIR_CHARS = 21  # YYYY-MM-DD_YYYY-MM-DD
MAX_CLIENT_KEY_CHARS = 63

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIMESTAMP_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\+00:00", re.ASCII)
_HASH16_RE = re.compile(r"[0-9a-f]{16}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VARIANT_RE = re.compile(r"report\.(superseded|duplicate)\.([0-9a-f]{16})(?:\.([0-9]{1,3}))?\.pdf")
_NUMBER_RE = re.compile(r"[2-9]|[1-9][0-9]")
_INVALID_PATH_CHARS = set('<>:"|?*')


class OutputLayoutError(ValueError):
    """An output location or file name is missing, malformed or unsafe."""


# --- output root ------------------------------------------------------------------------------------------------

def _shortest_relative_chars() -> int:
    """Characters after the root in the longest file path of the shortest possible client (1-character key)."""
    return max(1 + len(folder) + 1 + 1 + 1 + PERIOD_DIR_CHARS + 1 + LONGEST_FILENAME_CHARS[folder]
               for folder in SUBFOLDERS)


def _max_root_chars() -> int:
    """A root longer than this cannot hold even the shortest client's longest file (necessary, not sufficient)."""
    return MAX_PATH_CHARS - _shortest_relative_chars()


def _as_path(value, where: str) -> Path:
    if isinstance(value, bool) or not isinstance(value, (str, os.PathLike)):
        raise OutputLayoutError(f"{where} must be a path, got {type(value).__name__}")
    text = os.fspath(value)
    if not isinstance(text, str):
        raise OutputLayoutError(f"{where} must be a text path")
    if "\x00" in text:
        raise OutputLayoutError(f"{where} must not contain a NUL character")
    return Path(text)


def _absolute(path: Path, where: str) -> Path:
    if not path.is_absolute():
        raise OutputLayoutError(f"{where} must be an absolute path, got {str(path)!r}")
    if ".." in path.parts:
        raise OutputLayoutError(f"{where} must not contain '..' components: {str(path)!r}")
    # normpath/abspath only tidy the text; they do not touch the file system.
    clean = Path(os.path.abspath(path))
    if clean == Path(clean.anchor):
        raise OutputLayoutError(f"{where} must not be a drive root: {str(clean)!r}")
    for part in clean.parts[1:]:
        if _INVALID_PATH_CHARS & set(part):
            raise OutputLayoutError(f"{where} contains a character that Windows does not allow in names: "
                                    f"{part!r}")
        if part != part.rstrip(" .") or not part:
            raise OutputLayoutError(f"{where} has a folder name ending in a space or dot: {part!r}")
    return clean


def _key(path: Path) -> tuple:
    return tuple(os.path.normcase(part) for part in path.parts)


def _overlaps(left: Path, right: Path) -> bool:
    a, b = _key(left), _key(right)
    short = min(len(a), len(b))
    return a[:short] == b[:short]


def resolve_output_root(path=None, *, environ=None, db_path=None, allow_onedrive_output: bool = False) -> Path:
    """Return the validated output root. Nothing is created.

    `db_path` is the history database location to keep clear of; by default it is resolved the same way the
    snapshot store resolves it (including $JASPER_SEO_HISTORY_DB).
    """
    environ = os.environ if environ is None else environ
    if not isinstance(allow_onedrive_output, bool):
        raise OutputLayoutError("allow_onedrive_output must be True or False")

    if path:
        root, where = _as_path(path, "output root"), "output root"
    elif environ.get(ENV_OUTPUT_ROOT):
        root, where = _as_path(environ[ENV_OUTPUT_ROOT], f"${ENV_OUTPUT_ROOT}"), f"${ENV_OUTPUT_ROOT}"
    else:
        base = environ.get("LOCALAPPDATA")
        base_path = Path(base) if base else Path.home() / ".local" / "share"
        root, where = base_path / "JasperSEOReporting" / "output", "default output root"
    root = _absolute(root, where)

    if len(str(root)) > MAX_ROOT_CHARS:
        raise OutputLayoutError(
            f"{where} is {len(str(root))} characters long; at most {MAX_ROOT_CHARS} are allowed so that report "
            f"paths stay within the Windows {MAX_PATH_CHARS}-character limit")

    database = Path(os.path.abspath(resolve_db_path(db_path, environ=environ)))
    if _overlaps(root.resolve(), database.resolve()):
        raise OutputLayoutError(
            f"the output root {str(root)!r} overlaps the history database path {str(database)!r}; keep "
            "reports and the history database in separate locations")

    allow = allow_onedrive_output or environ.get(ENV_ALLOW_ONEDRIVE) == "1"
    if not allow and in_cloud_sync_folder(root, environ):
        raise OutputLayoutError(
            f"the output root {str(root)!r} is inside a OneDrive folder, where cloud sync can touch report "
            f"files; choose a local folder, or pass allow_onedrive_output=True / set {ENV_ALLOW_ONEDRIVE}=1 "
            "to allow it")
    return root


# --- period folders -----------------------------------------------------------------------------------------------

def validate_period_date(value, *, where: str = "date") -> str:
    """Return a calendar date as 'YYYY-MM-DD'. Accepts that exact text or a datetime.date (not a datetime)."""
    if isinstance(value, dt.datetime):
        raise OutputLayoutError(f"{where} must be a date, not a datetime")
    if isinstance(value, dt.date):
        return value.isoformat()
    if not isinstance(value, str):
        raise OutputLayoutError(f"{where} must be a YYYY-MM-DD date, got {type(value).__name__}")
    if not _DATE_RE.fullmatch(value):
        raise OutputLayoutError(f"{where} {value!r} must be written as YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        raise OutputLayoutError(f"{where} {value!r} is not a real calendar date") from None
    return parsed.isoformat()


def period_folder_name(period_start, period_end) -> str:
    start = validate_period_date(period_start, where="period start")
    end = validate_period_date(period_end, where="period end")
    if start > end:
        raise OutputLayoutError(f"period start {start} is after period end {end}")
    return f"{start}_{end}"


def _check_root(root) -> Path:
    path = _as_path(root, "output root")
    if not path.is_absolute():
        raise OutputLayoutError(f"output root must be an absolute path, got {str(path)!r}")
    return path


def _period_dir(subfolder: str, root, client_key, period_start, period_end) -> Path:
    base = _check_root(root)
    try:
        key = validate_client_key(client_key)
    except ClientConfigError as err:
        raise OutputLayoutError(str(err)) from None
    folder = base / subfolder / key / period_folder_name(period_start, period_end)
    longest = LONGEST_FILENAME_CHARS[subfolder]
    if len(str(folder)) + 1 + longest > MAX_PATH_CHARS:
        raise OutputLayoutError(
            f"the folder {str(folder)!r} is too long: its longest possible file name ({longest} characters) "
            f"would exceed the Windows {MAX_PATH_CHARS}-character path limit")
    return folder


def _file_path(folder: Path, filename: str) -> Path:
    path = folder / filename
    if len(str(path)) > MAX_PATH_CHARS:  # cannot happen after _period_dir, kept as an explicit guarantee
        raise OutputLayoutError(f"the path {str(path)!r} exceeds the Windows {MAX_PATH_CHARS}-character limit")
    return path


def report_dir(root, client_key, period_start, period_end) -> Path:
    return _period_dir(REPORTS_DIR, root, client_key, period_start, period_end)


def reviewer_dir(root, client_key, period_start, period_end) -> Path:
    return _period_dir(REVIEWER_DIR, root, client_key, period_start, period_end)


def attempt_dir(root, client_key, period_start, period_end) -> Path:
    return _period_dir(ATTEMPTS_DIR, root, client_key, period_start, period_end)


# --- PDF file names -----------------------------------------------------------------------------------------------

def validate_sha256(value, *, where: str = "SHA-256") -> str:
    """Return a SHA-256 written as 64 lowercase hexadecimal characters."""
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise OutputLayoutError(f"{where} must be 64 lowercase hexadecimal characters")
    return value


def validate_hash16(value, *, where: str = "short hash") -> str:
    """Return exactly 16 lowercase hexadecimal characters."""
    if not isinstance(value, str) or not _HASH16_RE.fullmatch(value):
        raise OutputLayoutError(f"{where} must be exactly {HASH16_CHARS} lowercase hexadecimal characters")
    return value


def hash16_of(sha256_hex) -> str:
    """The first 16 characters of a full SHA-256 digest."""
    return validate_sha256(sha256_hex)[:HASH16_CHARS]


def _validate_kind(kind) -> str:
    if kind not in VARIANT_KINDS:
        raise OutputLayoutError(f"variant kind must be one of {list(VARIANT_KINDS)}, got {kind!r}")
    return kind


def _validate_number(number) -> int:
    if isinstance(number, bool) or not isinstance(number, int):
        raise OutputLayoutError(f"variant number must be a whole number, got {type(number).__name__}")
    if not MIN_VARIANT <= number <= MAX_VARIANT:
        raise OutputLayoutError(f"variant number must be between {MIN_VARIANT} and {MAX_VARIANT}, got {number}")
    return number


def variant_pdf_name(kind, hash16, number: int = 1) -> str:
    """`report.<kind>.<h16>.pdf` for variant 1, `report.<kind>.<h16>.<n>.pdf` for n = 2..99."""
    _validate_kind(kind)
    validate_hash16(hash16)
    number = _validate_number(number)
    if number == 1:
        return f"report.{kind}.{hash16}.pdf"
    return f"report.{kind}.{hash16}.{number}.pdf"


def superseded_pdf_name(hash16, number: int = 1) -> str:
    return variant_pdf_name("superseded", hash16, number)


def duplicate_pdf_name(hash16, number: int = 1) -> str:
    return variant_pdf_name("duplicate", hash16, number)


@dataclass(frozen=True)
class VariantName:
    kind: str
    hash16: str
    number: int


def parse_variant_pdf_name(name) -> VariantName:
    """Parse a superseded/duplicate PDF file name strictly; anything else raises OutputLayoutError."""
    if not isinstance(name, str):
        raise OutputLayoutError(f"file name must be text, got {type(name).__name__}")
    match = _VARIANT_RE.fullmatch(name)
    if match is None:
        raise OutputLayoutError(
            f"{name!r} is not a preserved-report file name (report.superseded.<16 hex>[.<n>].pdf or "
            "report.duplicate.<16 hex>[.<n>].pdf, lowercase, no other text)")
    kind, hash16, number_text = match.groups()
    if number_text is None:
        number = 1
    elif _NUMBER_RE.fullmatch(number_text):
        number = int(number_text)
    else:
        raise OutputLayoutError(f"{name!r}: the variant number must be 2 to {MAX_VARIANT} without leading "
                                "zeros (variant 1 has no number)")
    return VariantName(kind, hash16, number)


def parse_report_pdf_name(name) -> VariantName | None:
    """None for the standard `report.pdf`; a VariantName for a preserved copy; otherwise an error."""
    if name == STANDARD_PDF_NAME:
        return None
    return parse_variant_pdf_name(name)


def variant_candidates(kind, hash16) -> tuple[str, ...]:
    """Every possible file name for a kind and hash, in the order they are tried (variants 1 to 99)."""
    return tuple(variant_pdf_name(kind, hash16, n) for n in range(MIN_VARIANT, MAX_VARIANT + 1))


def first_free_variant(kind, hash16, taken_names) -> str:
    """The first candidate name not in `taken_names` (compared case-insensitively, as Windows does).

    Pure: the caller supplies the names already present. Raises OutputLayoutError when all 99 are taken.
    """
    if isinstance(taken_names, (str, bytes)):
        raise OutputLayoutError("taken_names must be a collection of file names, not a single string")
    taken = set()
    for name in taken_names:
        if not isinstance(name, str):
            raise OutputLayoutError(f"taken file names must be text, got {type(name).__name__}")
        taken.add(name.casefold())
    for candidate in variant_candidates(kind, hash16):
        if candidate.casefold() not in taken:
            return candidate
    raise OutputLayoutError(f"all {MAX_VARIANT} {kind} file names for hash {hash16} are already in use")


MAX_PDF_FILENAME_CHARS = max(len(variant_pdf_name(kind, "0" * HASH16_CHARS, MAX_VARIANT))
                             for kind in VARIANT_KINDS)


# --- reviewer and attempt record names ----------------------------------------------------------------------------

def validate_run_id(value, *, where: str = "run_id") -> str:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise OutputLayoutError(f"{where} must be a lowercase hyphenated UUID")
    return value


def reviewer_record_name(run_id, pdf_sha256) -> str:
    """`reviewer.<run_id>.<pdf16>.json`, where <pdf16> is the first 16 hex characters of the PDF's SHA-256."""
    return f"reviewer.{validate_run_id(run_id)}.{hash16_of(pdf_sha256)}.json"


def compact_timestamp(attempted_at) -> str:
    """`2026-02-03T09:30:00+00:00` -> `20260203T093000Z` (assumed file-name form of the attempt timestamp)."""
    match = _TIMESTAMP_RE.fullmatch(attempted_at) if isinstance(attempted_at, str) else None
    if match is None:
        raise OutputLayoutError("attempted_at must be a UTC timestamp like 2026-02-03T09:30:00+00:00")
    try:
        dt.datetime.fromisoformat(attempted_at)
    except ValueError:
        raise OutputLayoutError(f"attempted_at {attempted_at!r} is not a real date and time") from None
    year, month, day, hour, minute, second = match.groups()
    return f"{year}{month}{day}T{hour}{minute}{second}Z"


def attempt_record_name(attempted_at, pdf_sha256, step, collision: int = 1) -> str:
    """`<timestamp>.<pdf16|nopdf>.<step>.attempt.json`; a repeat gets `.<n>` (n = 2..999) before `.json`.

    `pdf_sha256` is None when the attempt has no PDF ('nopdf').
    """
    if step not in ATTEMPT_STEPS:
        raise OutputLayoutError(f"step must be one of {list(ATTEMPT_STEPS)}, got {step!r}")
    if isinstance(collision, bool) or not isinstance(collision, int) or not 1 <= collision <= MAX_ATTEMPT_COLLISION:
        raise OutputLayoutError(f"collision number must be a whole number from 1 to {MAX_ATTEMPT_COLLISION}")
    pdf_part = "nopdf" if pdf_sha256 is None else hash16_of(pdf_sha256)
    suffix = "" if collision == 1 else f".{collision}"
    return f"{compact_timestamp(attempted_at)}.{pdf_part}.{step}.attempt{suffix}.json"


_ZERO_SHA = "0" * 64
_ZERO_UUID = "0" * 8 + "-" + "0" * 4 + "-" + "0" * 4 + "-" + "0" * 4 + "-" + "0" * 12
LONGEST_FILENAME_CHARS = {
    REPORTS_DIR: max(MAX_PDF_FILENAME_CHARS, len(STANDARD_PDF_NAME), len(DRAFT_PDF_NAME),
                     len(DRAFT_PARTIAL_NAME)),
    REVIEWER_DIR: len(reviewer_record_name(_ZERO_UUID, _ZERO_SHA)),
    ATTEMPTS_DIR: max(len(attempt_record_name("2026-01-01T00:00:00+00:00", _ZERO_SHA, step,
                                              MAX_ATTEMPT_COLLISION)) for step in ATTEMPT_STEPS),
}
MAX_ROOT_CHARS = _max_root_chars()


# --- full file paths ----------------------------------------------------------------------------------------------

def report_file_path(root, client_key, period_start, period_end, filename) -> Path:
    """Path of a file in the reports folder: report.pdf, a preserved copy, or a draft name."""
    if filename not in (DRAFT_PDF_NAME, DRAFT_PARTIAL_NAME):
        parse_report_pdf_name(filename)
    return _file_path(report_dir(root, client_key, period_start, period_end), filename)


def reviewer_record_path(root, client_key, period_start, period_end, run_id, pdf_sha256) -> Path:
    return _file_path(reviewer_dir(root, client_key, period_start, period_end),
                      reviewer_record_name(run_id, pdf_sha256))


def attempt_record_path(root, client_key, period_start, period_end, attempted_at, pdf_sha256, step,
                        collision: int = 1) -> Path:
    return _file_path(attempt_dir(root, client_key, period_start, period_end),
                      attempt_record_name(attempted_at, pdf_sha256, step, collision))
