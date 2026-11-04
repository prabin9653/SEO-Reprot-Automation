"""Durable render identity and the safe handoff of the rendered PDF to the draft slot (R7.2).

Two artifacts live in a period's existing `reports/` folder (nothing here creates a folder):

  report.render-input.<h16>.json   the SIDECAR: an immutable record of exactly what was rendered
  report.unarchived.pdf            the DRAFT PDF that finalization picks up (copied from the renderer's `report.pdf`)

Sidecar. `<h16>` is the first 16 hex characters of the render-input hash, so the name is deterministic and a different
input can never collide with it. The file is one line of canonical JSON (sorted keys, compact, UTF-8) with exactly these
members:

  schema                  "jasper.render-sidecar"
  version                 1
  render_input_sha256     the hash of the render input (64 hex)
  render_input_format     {"name", "version"} of the canonical render-input form (`render_input_hash`)
  generation_date         the report's date, ISO text (repeated from the payload for convenience; checked equal)
  template_fingerprint    the `template_version` label of the input (`render_identity`; checked equal)
  renderer                {"browser", "playwright", "python", "system"}: the Chromium build and the environment versions
  pdf                     {"sha256", "bytes"} of the PDF that this input rendered to
  render_input            the canonical payload that was hashed (`render_input_hash.build_payload`)

Reading is strict (`parse_sidecar`): invalid UTF-8 or JSON, duplicate keys, NaN/Infinity, missing or extra members, an
unsupported schema or format version, a payload whose canonical text does not hash to `render_input_sha256`, a payload
that does not rebuild into a `ClientRenderInput` with the same hash, a generation date or template fingerprint that
differs from the payload, a name that does not match the hash, and malformed hashes or sizes are all refused with a fixed
code and no client data. `render_input_from_payload` rebuilds the exact immutable object from the stored payload (the
operational metadata, which is never hashed, is not stored and comes back empty).

Retry. Rendering is deterministic (R0, R6), so a sidecar is enough to resume: `produce_report_draft` decides from what is
in the folder, never overwriting or deleting anything:

  no open sidecar, no draft     render the candidate input, write its sidecar, hand off the draft         -> "created"
  one open sidecar, no draft    rebuild the input FROM THE SIDECAR (same date, same identity), render it, require the
                                same PDF hash as the sidecar, hand off the draft                         -> "resumed"
  one open sidecar and a draft  require the draft to be the sidecar's PDF; write nothing                   -> "already_prepared"
  a draft without a sidecar     stop: an identity is never invented                                        -> conflict
  anything inconsistent         stop with a fixed code; every existing file is kept as it is               -> conflict

A sidecar is "open" until its render-input hash is in `finalized_render_hashes` (supplied by the caller from the
reviewer records in a later stage). A candidate that differs from the open sidecar never replaces it: the sidecar's
input is used and `candidate_matches` says whether the candidate agreed.

Handoff (`handoff_draft`) copies, it does not move: the source `report.pdf` of the render folder stays where it is. The
copy is written through `finalize_files.write_partial_then_promote` (unique partial file, no-overwrite rename); the PDF
hash is verified before and after; an existing draft is never touched (identical bytes are reported as already present,
anything else is a conflict); a source inside the reports folder (it could be the finalized `report.pdf`) is refused.
Partial files from an interrupted write are left in place and named in the result.

Offline: no network, database, clock or finalization module is used; the only writes are the sidecar and the draft.
"""

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import typing
import datetime as dt
import types
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from seo_report import finalize_files as ff
from seo_report import output_layout as layout
from seo_report import pdf_renderer as pr
from seo_report import render_input as ri
from seo_report import render_input_hash as rh

SIDECAR_SCHEMA = "jasper.render-sidecar"
SIDECAR_VERSION = 1
SIDECAR_PREFIX = "report.render-input."
SIDECAR_SUFFIX = ".json"
SIDECAR_NAME = re.compile(r"report\.render-input\.([0-9a-f]{16})\.json")
DRAFT_NAME = layout.DRAFT_PDF_NAME
MAX_SIDECAR_BYTES = 16 * 1024 * 1024
MAX_PDF_COPY_BYTES = 512 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MEMBERS = ("schema", "version", "render_input_sha256", "render_input_format", "generation_date", "template_fingerprint",
            "renderer", "pdf", "render_input")
_RENDERER_KEYS = ("browser", "playwright", "python", "system")


class RenderArtifactError(ValueError):
    """An artifact is invalid or cannot be written. `code` is fixed; `group` names the member; no client data."""

    def __init__(self, code: str, group: str = ""):
        super().__init__(f"{code}: {group}" if group else code)
        self.code = code
        self.group = group


# --- names and the renderer's identity -------------------------------------------------------------------------------------------

def sidecar_name(render_input_sha256: str) -> str:
    if not isinstance(render_input_sha256, str) or not _SHA256.fullmatch(render_input_sha256):
        raise RenderArtifactError("hash_invalid", "render_input_sha256")
    return f"{SIDECAR_PREFIX}{render_input_sha256[:layout.HASH16_CHARS]}{SIDECAR_SUFFIX}"


def renderer_identity(browser_version: str) -> dict:
    """The rendering environment recorded in a sidecar: Chromium build, Playwright, Python and operating system names."""
    if not isinstance(browser_version, str) or not browser_version.strip():
        raise RenderArtifactError("renderer_invalid", "browser")
    return {"browser": browser_version, "playwright": importlib.metadata.version("playwright"),
            "python": platform.python_version(), "system": platform.system()}


# --- strict JSON ----------------------------------------------------------------------------------------------------------------------

class _Duplicate(ValueError):
    pass


class _Constant(ValueError):
    pass


def _object(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise _Duplicate()
        seen[key] = value
    return seen


def _refuse(_name):
    raise _Constant()


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


# --- rebuilding the render input from its canonical payload ------------------------------------------------------------------------------

def _load(value, hint, where: str):
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        arms = [a for a in typing.get_args(hint) if a is not type(None)]
        if value is None:
            if type(None) in typing.get_args(hint):
                return None
            raise RenderArtifactError("payload_invalid", where)
        return _load(value, arms[0], where)
    if origin is tuple:
        if not isinstance(value, list):
            raise RenderArtifactError("payload_invalid", where)
        item = typing.get_args(hint)[0]
        return tuple(_load(v, item, f"{where}[{i}]") for i, v in enumerate(value))
    if hint is str:
        if not isinstance(value, str):
            raise RenderArtifactError("payload_invalid", where)
        return value
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise RenderArtifactError("payload_invalid", where)
        return value
    if hint is dt.date:
        try:
            day = dt.date.fromisoformat(value)
        except (TypeError, ValueError):
            raise RenderArtifactError("payload_invalid", where) from None
        if day.isoformat() != value:
            raise RenderArtifactError("payload_invalid", where)
        return day
    if hint is Decimal:
        try:
            number = Decimal(value) if isinstance(value, str) else None
        except InvalidOperation:
            number = None
        if number is None or not number.is_finite():
            raise RenderArtifactError("payload_invalid", where)
        return number
    if isinstance(hint, type) and hasattr(hint, "__dataclass_fields__"):
        return _load_model(value, hint, where)
    raise RenderArtifactError("payload_invalid", where)


def _load_model(value, cls, where: str):
    if not isinstance(value, dict):
        raise RenderArtifactError("payload_invalid", where)
    included = [name for name, kind in rh.FIELD_CLASSES[cls.__name__].items() if kind == rh.INCLUDED]
    if set(value) != set(included):
        raise RenderArtifactError("payload_invalid", where)
    hints = typing.get_type_hints(cls)
    try:
        return cls(**{name: _load(value[name], hints[name], f"{where}.{name}") for name in included})
    except (ri.RenderInputError, TypeError, ValueError):
        raise RenderArtifactError("payload_invalid", where) from None


def render_input_from_payload(payload) -> ri.ClientRenderInput:
    """The immutable `ClientRenderInput` that a canonical payload (`render_input_hash.build_payload`) describes. Strict:
    unknown or missing members, wrong types and values the model refuses raise `RenderArtifactError`."""
    if not isinstance(payload, dict) or set(payload) != {"format", "version", "input"}:
        raise RenderArtifactError("payload_invalid", "payload")
    if payload["format"] != rh.FORMAT_NAME or payload["version"] != rh.FORMAT_VERSION:
        raise RenderArtifactError("unsupported_version", "render_input_format")
    return _load_model(payload["input"], ri.ClientRenderInput, "input")


# --- the sidecar ---------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Sidecar:
    render_input_sha256: str
    generation_date: dt.date
    template_fingerprint: str
    renderer: tuple[tuple[str, str], ...]
    pdf_sha256: str
    pdf_bytes: int
    render_input: ri.ClientRenderInput = field(repr=False)

    @property
    def name(self) -> str:
        return sidecar_name(self.render_input_sha256)


def build_sidecar_bytes(render_input: ri.ClientRenderInput, *, renderer: dict, pdf_sha256: str, pdf_bytes: int) -> bytes:
    """The sidecar file content for `render_input` and the PDF it rendered to (one line of canonical JSON + newline)."""
    payload = rh.build_payload(render_input)
    digest = rh.render_input_sha256(render_input)
    if not isinstance(pdf_sha256, str) or not _SHA256.fullmatch(pdf_sha256) or isinstance(pdf_bytes, bool) \
            or not isinstance(pdf_bytes, int) or pdf_bytes < 1:
        raise RenderArtifactError("pdf_identity_invalid", "pdf")
    if not isinstance(renderer, dict) or set(renderer) != set(_RENDERER_KEYS) \
            or any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in renderer.values()):
        raise RenderArtifactError("renderer_invalid", "renderer")
    document = {
        "schema": SIDECAR_SCHEMA, "version": SIDECAR_VERSION, "render_input_sha256": digest,
        "render_input_format": {"name": payload["format"], "version": payload["version"]},
        "generation_date": render_input.generation_date.isoformat(),
        "template_fingerprint": render_input.fingerprints.template_version, "renderer": dict(renderer),
        "pdf": {"sha256": pdf_sha256, "bytes": pdf_bytes}, "render_input": payload}
    return (_canonical(document) + "\n").encode("utf-8")


def parse_sidecar(data: bytes, *, expected_name: str | None = None) -> Sidecar:
    """Validate sidecar bytes strictly and return the `Sidecar`. Raises `RenderArtifactError` (fixed code)."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_SIDECAR_BYTES:
        raise RenderArtifactError("sidecar_unreadable", "file")
    try:
        document = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_object, parse_constant=_refuse)
    except UnicodeDecodeError:
        raise RenderArtifactError("sidecar_malformed", "encoding") from None
    except _Duplicate:
        raise RenderArtifactError("sidecar_malformed", "duplicate_key") from None
    except _Constant:
        raise RenderArtifactError("sidecar_malformed", "constant") from None
    except (ValueError, RecursionError):
        raise RenderArtifactError("sidecar_malformed", "json") from None
    if not isinstance(document, dict) or set(document) != set(_MEMBERS):
        raise RenderArtifactError("sidecar_malformed", "members")
    if document["schema"] != SIDECAR_SCHEMA or document["version"] != SIDECAR_VERSION \
            or isinstance(document["version"], bool):
        raise RenderArtifactError("unsupported_version", "schema")
    digest = document["render_input_sha256"]
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise RenderArtifactError("sidecar_malformed", "render_input_sha256")
    if expected_name is not None and expected_name != sidecar_name(digest):
        raise RenderArtifactError("identity_mismatch", "name")
    payload = document["render_input"]
    if document["render_input_format"] != {"name": rh.FORMAT_NAME, "version": rh.FORMAT_VERSION} \
            or not isinstance(payload, dict) or payload.get("format") != rh.FORMAT_NAME \
            or payload.get("version") != rh.FORMAT_VERSION:
        raise RenderArtifactError("unsupported_version", "render_input_format")
    if hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest() != digest:
        raise RenderArtifactError("hash_mismatch", "render_input")
    render_input = render_input_from_payload(payload)
    if rh.render_input_sha256(render_input) != digest:
        raise RenderArtifactError("hash_mismatch", "render_input")
    if document["generation_date"] != render_input.generation_date.isoformat():
        raise RenderArtifactError("identity_mismatch", "generation_date")
    if document["template_fingerprint"] != render_input.fingerprints.template_version:
        raise RenderArtifactError("identity_mismatch", "template_fingerprint")
    renderer = document["renderer"]
    if not isinstance(renderer, dict) or set(renderer) != set(_RENDERER_KEYS) \
            or any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in renderer.values()):
        raise RenderArtifactError("sidecar_malformed", "renderer")
    pdf = document["pdf"]
    if not isinstance(pdf, dict) or set(pdf) != {"sha256", "bytes"} or not isinstance(pdf["sha256"], str) \
            or not _SHA256.fullmatch(pdf["sha256"]) or isinstance(pdf["bytes"], bool) \
            or not isinstance(pdf["bytes"], int) or pdf["bytes"] < 1:
        raise RenderArtifactError("sidecar_malformed", "pdf")
    return Sidecar(digest, render_input.generation_date, document["template_fingerprint"],
                   tuple(sorted(renderer.items())), pdf["sha256"], pdf["bytes"], render_input)


# --- the reports folder -------------------------------------------------------------------------------------------------------------------------

def _reports_dir(reports_dir, ops: ff.FileOps) -> Path:
    path = Path(reports_dir)
    if not path.is_absolute():
        raise RenderArtifactError("reports_dir_invalid", "reports_dir")
    try:
        info = ops.lstat(path)
    except OSError:
        raise RenderArtifactError("reports_dir_missing", "reports_dir") from None  # never created here
    if ff._is_link(info) or not __import__("stat").S_ISDIR(info.st_mode):
        raise RenderArtifactError("reports_dir_invalid", "reports_dir")
    return path


def _partials(path: Path) -> tuple[str, ...]:
    try:
        with os.scandir(path) as entries:
            return tuple(sorted(e.name for e in entries if ff.is_partial_name(e.name)))
    except OSError:
        return ()


def _read(path: Path, ops: ff.FileOps, limit: int) -> bytes:
    try:
        info = ops.lstat(path)
        if ff._is_link(info) or not __import__("stat").S_ISREG(info.st_mode) or info.st_size > limit:
            raise RenderArtifactError("file_unreadable", path.name)
        with ops.open_read(path) as handle:
            data = handle.read(limit + 1)
    except OSError:
        raise RenderArtifactError("file_unreadable", path.name) from None
    if len(data) > limit:
        raise RenderArtifactError("file_unreadable", path.name)
    return data


@dataclass(frozen=True)
class WriteResult:
    name: str
    outcome: str  # "written" or "already_present"


def write_sidecar(reports_dir, data: bytes, *, ops: ff.FileOps | None = None) -> WriteResult:
    """Create the sidecar for `data` exclusively. The bytes are validated first and again after writing.

    An existing file of that name is never touched: identical bytes are "already_present"; anything else (including a
    different PDF hash or Chromium build for the same input) raises `RenderArtifactError("sidecar_exists_differs")`.
    A failed write leaves its partial file in place (named by `partial_names` of the raised error) and raises
    `sidecar_write_incomplete`.
    """
    ops = ops or ff.FileOps()
    folder = _reports_dir(reports_dir, ops)
    sidecar = parse_sidecar(data)
    name = sidecar.name
    target = folder / name
    before = set(_partials(folder))
    try:
        ff.write_partial_then_promote(folder, name, data, ops=ops)
    except ff.FileConflictError as err:
        if err.kind != "destination_exists":
            raise RenderArtifactError("sidecar_write_incomplete", "conflict") from None
        existing = _read(target, ops, MAX_SIDECAR_BYTES)
        if existing == data:
            return WriteResult(name, "already_present")
        raise RenderArtifactError("sidecar_exists_differs", name) from None
    except ff.FileOperationError:
        error = RenderArtifactError("sidecar_write_incomplete", "write")
        error.partial_names = tuple(sorted(set(_partials(folder)) - before))
        raise error from None
    stored = _read(target, ops, MAX_SIDECAR_BYTES)
    if stored != data:
        raise RenderArtifactError("sidecar_invalid_after_write", name)
    parse_sidecar(stored, expected_name=name)
    return WriteResult(name, "written")


def read_sidecar(reports_dir, name: str, *, ops: ff.FileOps | None = None) -> Sidecar:
    """Read and strictly validate one sidecar by file name."""
    ops = ops or ff.FileOps()
    folder = _reports_dir(reports_dir, ops)
    if not isinstance(name, str) or not SIDECAR_NAME.fullmatch(name):
        raise RenderArtifactError("sidecar_name_invalid", "name")
    return parse_sidecar(_read(folder / name, ops, MAX_SIDECAR_BYTES), expected_name=name)


# --- draft handoff -----------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class HandoffResult:
    outcome: str  # "copied" or "already_present"
    pdf_sha256: str
    pdf_bytes: int


def handoff_draft(source_pdf, reports_dir, *, ops: ff.FileOps | None = None) -> HandoffResult:
    """Copy the rendered PDF to `report.unarchived.pdf` in `reports_dir`, never overwriting anything.

    The source stays where it is. Raises `RenderArtifactError` with a fixed code: `source_invalid` (not a valid PDF),
    `source_in_reports_dir`, `draft_exists_differs`, `draft_not_regular`, `handoff_incomplete` (with `partial_names`) or
    `handoff_verification_failed`.
    """
    ops = ops or ff.FileOps()
    folder = _reports_dir(reports_dir, ops)
    source = Path(source_pdf)
    if not source.is_absolute() or source.parent == folder:
        raise RenderArtifactError("source_in_reports_dir" if source.parent == folder else "source_invalid", "source")
    check = ff.validate_draft_pdf(source, ops=ops)
    if not check.ok:
        raise RenderArtifactError("source_invalid", "source")
    sha, size = check.sha256, check.byte_count
    target = folder / DRAFT_NAME
    try:
        info = ops.lstat(target)
    except FileNotFoundError:
        info = None
    except OSError:
        raise RenderArtifactError("draft_not_regular", DRAFT_NAME) from None
    if info is not None:
        return _existing_draft(target, ops, sha, size)
    if size > MAX_PDF_COPY_BYTES:
        raise RenderArtifactError("source_invalid", "source")
    data = _read(source, ops, MAX_PDF_COPY_BYTES)
    if hashlib.sha256(data).hexdigest() != sha:
        raise RenderArtifactError("source_invalid", "source")
    before = set(_partials(folder))
    try:
        ff.write_partial_then_promote(folder, DRAFT_NAME, data, ops=ops)
    except ff.FileConflictError as err:
        if err.kind == "destination_exists":  # a draft appeared meanwhile: judge it, never replace it
            return _existing_draft(target, ops, sha, size)
        error = RenderArtifactError("handoff_incomplete", "conflict")
        error.partial_names = tuple(sorted(set(_partials(folder)) - before))
        raise error from None
    except ff.FileOperationError:
        error = RenderArtifactError("handoff_incomplete", "write")
        error.partial_names = tuple(sorted(set(_partials(folder)) - before))
        raise error from None
    try:
        copied = ff.sha256_file(target, ops=ops)
        again = ff.sha256_file(source, ops=ops)
    except ff.FileOperationError:
        raise RenderArtifactError("handoff_verification_failed", DRAFT_NAME) from None  # the copy is kept
    if (copied.sha256, copied.byte_count) != (sha, size) or (again.sha256, again.byte_count) != (sha, size):
        raise RenderArtifactError("handoff_verification_failed", DRAFT_NAME)
    return HandoffResult("copied", sha, size)


def _existing_draft(target: Path, ops: ff.FileOps, sha: str, size: int) -> HandoffResult:
    try:
        digest = ff.sha256_file(target, ops=ops)
    except ff.FileOperationError:
        raise RenderArtifactError("draft_not_regular", DRAFT_NAME) from None
    if (digest.sha256, digest.byte_count) == (sha, size):
        return HandoffResult("already_present", sha, size)
    raise RenderArtifactError("draft_exists_differs", DRAFT_NAME)


# --- retry and crash recovery ----------------------------------------------------------------------------------------------------------------------

OUTCOME_CREATED, OUTCOME_RESUMED, OUTCOME_PREPARED = "created", "resumed", "already_prepared"
OUTCOME_CONFLICT, OUTCOME_FAILED = "conflict", "failed"


@dataclass(frozen=True)
class DraftOutcome:
    outcome: str  # created, resumed, already_prepared, conflict or failed
    code: str | None  # a fixed code for conflict and failed
    render_input: ri.ClientRenderInput | None  # the input that is (or would have been) rendered: the sidecar's on a retry
    render_input_sha256: str | None
    sidecar_name: str | None
    generation_date: dt.date | None
    pdf_sha256: str | None
    pdf_bytes: int | None
    created_sidecar: bool = False
    handed_off: bool = False
    candidate_matches: bool | None = None  # whether the caller's candidate had the same hash as the sidecar's input
    partial_names: tuple[str, ...] = ()  # partial files left by an interrupted write (never deleted)

    @property
    def ok(self) -> bool:
        return self.outcome in (OUTCOME_CREATED, OUTCOME_RESUMED, OUTCOME_PREPARED)


def _scan(folder: Path):
    sidecars, draft_present = [], False
    try:
        with os.scandir(folder) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                if SIDECAR_NAME.fullmatch(entry.name):
                    sidecars.append(entry.name)
                elif entry.name == DRAFT_NAME:
                    draft_present = True
    except OSError:
        raise RenderArtifactError("reports_dir_unreadable", "reports_dir") from None
    return sidecars, draft_present


def produce_report_draft(reports_dir, candidate: ri.ClientRenderInput, *, workdir, logo_path, fonts,
                         finalized_render_hashes=(), renderer=None, ops: ff.FileOps | None = None) -> DraftOutcome:
    """Bring `reports_dir` to the state "sidecar and draft present and consistent", from whatever state it is in.

    `candidate` is the caller's freshly built `ClientRenderInput` (used only when no open sidecar exists);
    `workdir`, `logo_path` and `fonts` go to the renderer (`pdf_renderer.render_report_pdf`, replaceable through
    `renderer` for tests); `finalized_render_hashes` are the render-input hashes whose sidecars are closed. See the
    module docstring for every state. Nothing existing is ever overwritten or deleted.
    """
    if not isinstance(candidate, ri.ClientRenderInput):
        raise TypeError(f"candidate must be a ClientRenderInput, got {type(candidate).__name__}")
    ops = ops or ff.FileOps()
    renderer = renderer or pr.render_report_pdf
    folder = _reports_dir(reports_dir, ops)
    closed = set(finalized_render_hashes)
    candidate_hash = rh.render_input_sha256(candidate)

    def stop(outcome, code, **fields):
        base = dict(render_input=None, render_input_sha256=None, sidecar_name=None, generation_date=None,
                    pdf_sha256=None, pdf_bytes=None)
        base.update(fields)
        return DraftOutcome(outcome, code, **base)

    names, draft_present = _scan(folder)
    sidecars = []
    for name in names:
        try:
            sidecars.append(parse_sidecar(_read(folder / name, ops, MAX_SIDECAR_BYTES), expected_name=name))
        except RenderArtifactError:
            return stop(OUTCOME_CONFLICT, "sidecar_invalid", sidecar_name=name)  # kept as it is; the identity is unknown
    open_ones = [s for s in sidecars if s.render_input_sha256 not in closed]
    if len(open_ones) > 1:
        return stop(OUTCOME_CONFLICT, "multiple_open_sidecars")
    if draft_present:
        try:
            if not __import__("stat").S_ISREG(ops.lstat(folder / DRAFT_NAME).st_mode):
                return stop(OUTCOME_CONFLICT, "draft_not_regular")
        except OSError:
            return stop(OUTCOME_CONFLICT, "draft_not_regular")

    if not open_ones:
        if draft_present:
            return stop(OUTCOME_CONFLICT, "draft_without_sidecar")  # an identity is never invented for an existing draft
        if sidecar_name(candidate_hash) in names:
            return stop(OUTCOME_CONFLICT, "render_input_already_finalized", render_input_sha256=candidate_hash,
                        sidecar_name=sidecar_name(candidate_hash))
        state, render_input, expected = OUTCOME_CREATED, candidate, None
    else:
        known = open_ones[0]
        expected, render_input = known, known.render_input
        if draft_present:
            try:
                digest = ff.sha256_file(folder / DRAFT_NAME, ops=ops)
            except ff.FileOperationError:
                return stop(OUTCOME_CONFLICT, "draft_not_regular", sidecar_name=known.name)
            fields = dict(render_input=render_input, render_input_sha256=known.render_input_sha256,
                          sidecar_name=known.name, generation_date=known.generation_date, pdf_sha256=known.pdf_sha256,
                          pdf_bytes=known.pdf_bytes, candidate_matches=known.render_input_sha256 == candidate_hash)
            if (digest.sha256, digest.byte_count) != (known.pdf_sha256, known.pdf_bytes):
                return stop(OUTCOME_CONFLICT, "draft_pdf_mismatch", **fields)
            return DraftOutcome(OUTCOME_PREPARED, None, **fields)
        state = OUTCOME_RESUMED

    identity = dict(render_input=render_input, render_input_sha256=rh.render_input_sha256(render_input),
                    sidecar_name=sidecar_name(rh.render_input_sha256(render_input)),
                    generation_date=render_input.generation_date,
                    candidate_matches=None if expected is None else expected.render_input_sha256 == candidate_hash)
    try:
        outcome = renderer(render_input, workdir=workdir, logo_path=logo_path, fonts=fonts)
    except pr.PdfRenderError as err:
        return stop(OUTCOME_FAILED, "render_failed", **identity)
    if not outcome.ok:
        return stop(OUTCOME_CONFLICT, "render_has_layout_issues", **identity)
    if outcome.render_input_sha256 != identity["render_input_sha256"]:
        return stop(OUTCOME_CONFLICT, "render_identity_mismatch", **identity)
    try:
        measured = ff.sha256_file(outcome.pdf_path, ops=ops)
    except ff.FileOperationError:
        return stop(OUTCOME_FAILED, "rendered_pdf_unreadable", **identity)
    if (measured.sha256, measured.byte_count) != (outcome.pdf_sha256, outcome.byte_count):
        return stop(OUTCOME_CONFLICT, "rendered_pdf_mismatch", **identity)
    identity.update(pdf_sha256=measured.sha256, pdf_bytes=measured.byte_count)
    if expected is not None and (measured.sha256, measured.byte_count) != (expected.pdf_sha256, expected.pdf_bytes):
        return stop(OUTCOME_CONFLICT, "render_not_reproducible", **identity)  # the environment changed since the sidecar

    created = False
    try:
        data = build_sidecar_bytes(render_input, renderer=renderer_identity(outcome.browser_version),
                                   pdf_sha256=measured.sha256, pdf_bytes=measured.byte_count)
        if expected is None:
            created = write_sidecar(folder, data, ops=ops).outcome == "written"
    except RenderArtifactError as err:
        code = "sidecar_conflict" if err.code == "sidecar_exists_differs" else err.code
        return stop(OUTCOME_CONFLICT if err.code == "sidecar_exists_differs" else OUTCOME_FAILED, code,
                    partial_names=getattr(err, "partial_names", ()), **identity)
    try:
        handoff = handoff_draft(outcome.pdf_path, folder, ops=ops)
    except RenderArtifactError as err:
        outcome_kind = OUTCOME_CONFLICT if err.code in ("draft_exists_differs", "draft_not_regular",
                                                          "source_in_reports_dir") else OUTCOME_FAILED
        return stop(outcome_kind, err.code, created_sidecar=created, partial_names=getattr(err, "partial_names", ()),
                    **identity)
    return DraftOutcome(state, None, created_sidecar=created, handed_off=handoff.outcome == "copied", **identity)
