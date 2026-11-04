"""Read-only helpers about the render identities that are on disk (R7.4).

Everything here only reads. Nothing is created, updated, deleted, renamed or repaired, no database is opened and no
record is changed. The validation is the existing validation: reviewer records are parsed with
`record_writer.parse_record_bytes` (strict JSON plus `record_schema`), sidecars with `render_artifacts.read_sidecar`
(strict parser). This module only decides which files to look at and what their results mean.

  scan_finalized_render_hashes / finalized_render_hashes
      The render-input hashes named by valid schema version 2 reviewer records in one reviewer folder. A file counts
      only when it has the reviewer-record file name, is a regular file, is within the size limit, parses strictly, is
      schema-valid and its own run ID and PDF prefix agree with its name. Version 1 records have no render identity and
      are only counted. Anything else is listed as ignored with a fixed reason and never contributes a hash; an
      unreadable candidate makes the scan `complete=False` (it might have been a finalized identity). A record counted
      here is schema-valid, not proven against the database or the PDF (the audit does that).

  read_sidecar_for / find_open_sidecar / open_sidecars
      The sidecar of a render-input hash (`report.render-input.<first 16 hex>.json` in the reports folder), read with
      the strict parser; it must carry exactly the requested full hash. "Open" means that no valid v2 record names the
      hash yet (the caller passes the finalized hashes).

  with_operational
      The same immutable render input with its snapshot identity restored. A render input rebuilt from a sidecar has
      empty operational metadata (it is not stored); the operational block is never hashed, so the hash is unchanged
      (this is checked).

Errors are `RenderStateError(code, group)` with fixed codes; no message holds client data or a path.
"""

import dataclasses
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from seo_report import finalize_files as ff
from seo_report import record_writer as rw
from seo_report import render_artifacts as ra
from seo_report import render_input as ri
from seo_report import render_input_hash as rh

_RECORD_NAME = re.compile(r"reviewer\.([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.([0-9a-f]{16})\.json")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class RenderStateError(ValueError):
    """A render identity cannot be established from what is on disk. `code` is fixed; `group` names the part."""

    def __init__(self, code: str, group: str = ""):
        super().__init__(f"{code}: {group}" if group else code)
        self.code = code
        self.group = group


@dataclass(frozen=True)
class FinalizedScan:
    hashes: frozenset  # render_input_sha256 of the valid version 2 records
    v1_records: int  # valid version 1 records (no render identity)
    v2_records: int
    ignored: tuple[tuple[str, str], ...]  # (file name, fixed reason), in name order
    complete: bool  # False when a candidate file could not be read: its hash, if any, is unknown


def _folder(path, ops, what: str) -> Path:
    folder = Path(path)
    if not folder.is_absolute():
        raise RenderStateError(f"{what}_invalid", what)
    try:
        info = ops.lstat(folder)
    except FileNotFoundError:
        raise RenderStateError(f"{what}_missing", what) from None
    except OSError:
        raise RenderStateError(f"{what}_unreadable", what) from None
    if ff._is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise RenderStateError(f"{what}_invalid", what)
    return folder


def scan_finalized_render_hashes(reviewer_dir, *, ops: ff.FileOps | None = None) -> FinalizedScan:
    """Scan `reviewer_dir` (absolute, existing) for finalized render identities. See the module docstring."""
    ops = ops or ff.FileOps()
    folder = _folder(reviewer_dir, ops, "reviewer_dir")
    try:
        with os.scandir(folder) as entries:
            names = sorted(e.name for e in entries if _RECORD_NAME.fullmatch(e.name))
    except OSError:
        raise RenderStateError("reviewer_dir_unreadable", "reviewer_dir") from None
    hashes, ignored, v1, v2, complete = set(), [], 0, 0, True
    for name in names:
        match = _RECORD_NAME.fullmatch(name)
        path = folder / name
        try:
            info = ops.lstat(path)
        except OSError:
            ignored.append((name, "unreadable"))
            complete = False
            continue
        if ff._is_link(info) or not stat.S_ISREG(info.st_mode):
            ignored.append((name, "not_regular_file"))
            continue
        if info.st_size > rw.MAX_RECORD_BYTES:
            ignored.append((name, "too_large"))
            continue
        try:
            with ops.open_read(path) as handle:
                data = handle.read(rw.MAX_RECORD_BYTES + 1)
        except OSError:
            ignored.append((name, "unreadable"))
            complete = False
            continue
        record = rw.parse_record_bytes(data) if len(data) <= rw.MAX_RECORD_BYTES else None
        if record is None:
            ignored.append((name, "invalid_record"))
            continue
        if record["snapshot"]["run_id"] != match.group(1) or record["finalization"]["pdf_sha256"][:16] != match.group(2):
            ignored.append((name, "name_mismatch"))
            continue
        if record["schema_version"] == 2:
            v2 += 1
            hashes.add(record["finalization"]["render_input_sha256"])
        else:
            v1 += 1
    return FinalizedScan(frozenset(hashes), v1, v2, tuple(ignored), complete)


def finalized_render_hashes(reviewer_dir, *, ops: ff.FileOps | None = None, require_complete: bool = False) -> frozenset:
    """The render-input hashes of the valid version 2 records in `reviewer_dir` (what `produce_report_draft` calls
    `finalized_render_hashes`). With `require_complete`, an unreadable candidate record raises `scan_incomplete`."""
    scan = scan_finalized_render_hashes(reviewer_dir, ops=ops)
    if require_complete and not scan.complete:
        raise RenderStateError("scan_incomplete", "reviewer_dir")
    return scan.hashes


def read_sidecar_for(reports_dir, render_input_sha256: str, *, ops: ff.FileOps | None = None) -> ra.Sidecar:
    """The strictly parsed sidecar of `render_input_sha256` (open or finalized). Raises `RenderStateError`:
    `sidecar_missing`, `sidecar_unreadable`, `reports_dir_*`, the parser's own codes (`sidecar_malformed`,
    `unsupported_version`, `hash_mismatch`, …) or `identity_mismatch` when the sidecar carries another full hash."""
    ops = ops or ff.FileOps()
    if not isinstance(render_input_sha256, str) or not _SHA256.fullmatch(render_input_sha256):
        raise RenderStateError("hash_invalid", "render_input_sha256")
    folder = _folder(reports_dir, ops, "reports_dir")
    name = ra.sidecar_name(render_input_sha256)
    try:
        ops.lstat(folder / name)
    except FileNotFoundError:
        raise RenderStateError("sidecar_missing", "sidecar") from None
    except OSError:
        raise RenderStateError("sidecar_unreadable", "sidecar") from None
    try:
        sidecar = ra.read_sidecar(folder, name, ops=ops)
    except ra.RenderArtifactError as err:
        code = "sidecar_unreadable" if err.code == "file_unreadable" else err.code
        raise RenderStateError(code, err.group) from None
    if sidecar.render_input_sha256 != render_input_sha256:
        raise RenderStateError("identity_mismatch", "render_input_sha256")
    return sidecar


def find_open_sidecar(reports_dir, render_input_sha256: str, *, finalized=frozenset(),
                      ops: ff.FileOps | None = None) -> ra.Sidecar:
    """`read_sidecar_for`, but a hash in `finalized` is refused (`render_input_finalized`): its sidecar is closed."""
    if render_input_sha256 in set(finalized):
        raise RenderStateError("render_input_finalized", "render_input_sha256")
    return read_sidecar_for(reports_dir, render_input_sha256, ops=ops)


def open_sidecars(reports_dir, *, finalized=frozenset(), ops: ff.FileOps | None = None) -> tuple[ra.Sidecar, ...]:
    """Every valid sidecar in `reports_dir` whose hash is not in `finalized`, in name order. An invalid sidecar file
    raises `sidecar_invalid` (group: its file name): the identity of a retry is never guessed around it."""
    ops = ops or ff.FileOps()
    folder = _folder(reports_dir, ops, "reports_dir")
    try:
        with os.scandir(folder) as entries:
            names = sorted(e.name for e in entries if ra.SIDECAR_NAME.fullmatch(e.name))
    except OSError:
        raise RenderStateError("reports_dir_unreadable", "reports_dir") from None
    closed, found = set(finalized), []
    for name in names:
        try:
            sidecar = ra.read_sidecar(folder, name, ops=ops)
        except ra.RenderArtifactError:
            raise RenderStateError("sidecar_invalid", name) from None
        if sidecar.render_input_sha256 not in closed:
            found.append(sidecar)
    return tuple(found)


def with_operational(render_input: ri.ClientRenderInput, *, client_key: str,
                     snapshot_content_hash: str) -> ri.ClientRenderInput:
    """`render_input` with its snapshot identity filled in when it has none (a sidecar-rebuilt input).

    An input that already carries a different identity raises `operational_mismatch`. The render-input hash must not
    change (`hash_changed`, a defensive check).
    """
    if not isinstance(render_input, ri.ClientRenderInput):
        raise TypeError(f"render_input must be a ClientRenderInput, got {type(render_input).__name__}")
    current = render_input.operational
    if (current.client_key, current.snapshot_content_hash) == (client_key, snapshot_content_hash):
        return render_input
    if current.client_key is not None or current.snapshot_content_hash is not None:
        raise RenderStateError("operational_mismatch", "operational")
    restored = dataclasses.replace(render_input, operational=dataclasses.replace(
        current, client_key=client_key, snapshot_content_hash=snapshot_content_hash))
    if rh.render_input_sha256(restored) != rh.render_input_sha256(render_input):
        raise RenderStateError("hash_changed", "operational")
    return restored
