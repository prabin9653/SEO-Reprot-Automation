"""Report preview (R8.7h.2): a real-data render of one client's report that can never become a production report.

    seo-report report preview --client CLIENT_KEY --period YYYY-MM-DD_YYYY-MM-DD --config <runner.yaml>

**Running it contacts Google (read-only) and starts Chromium.** It was written and tested offline; it has not been run against Google yet (R8.7h.3).

How it works: it reuses `pipeline.dry_run_client` (collect with the production collector, assemble with the production assembly, render with the production
renderer into a scratch folder) and adds three things: a preview folder of its own, the visible label `PREVIEW — NOT FINAL` on every page (added by
`pdf_renderer.render_report_pdf(..., preview_label=...)`, measured on every page, and absent from production renders), and a manifest.

What it never does (each is a property of the code it calls, and each is asserted by a test):
  * no ledger is opened: `dry_run_client` takes no ledger, so there is no request, run, attempt, event, outbox row or lock;
  * the history database and the production output root are only READ, through the read-only, finalized-only reader (`mode=ro`); no snapshot is stored;
  * nothing is finalized, audited, promoted or recorded: no reviewer record, no `report.pdf` in the production reports folder, no production folder is created;
  * nothing is sent or uploaded: there is no email, Drive, Make.com or webhook code anywhere.

Preview folder (outside the project and OneDrive; never reused, never overwritten, never deleted):

    %LOCALAPPDATA%\\JasperSEOReporting\\previews\\<client_key>\\<period>\\preview-<UTC time>-<id>\\
        preview-manifest.json   status: "incomplete" while it runs; "preview_only" on success; "failed" or "interrupted" otherwise
        preview.pdf             the labelled PDF (only after every check passed)
        render\\                 the scratch folder of the dry run: its sidecar, draft PDF and the Chromium work folder
"""

import dataclasses
import datetime as dt
import functools
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

from seo_report import pdf_renderer as pr
from seo_report import pdf_validation as pv
from seo_report import pipeline
from seo_report import runner_config as rc
from seo_report.cli_exit import Exit, exit_code_for_response

LABEL = pr.PREVIEW_LABEL
NOTICE = "PREVIEW ONLY — NO PRODUCTION REPORT CREATED"
SCHEMA = "jasper.report-preview"
VERSION = 1
MANIFEST_NAME = "preview-manifest.json"
PDF_NAME = "preview.pdf"
FOLDER_NAME = "previews"


class PreviewFailed(Exception):
    """A preview step failed. `stage` names the step, `code` is a fixed identifier, `exit_code` the CLI exit code. No production report was created."""

    def __init__(self, stage: str, code: str, exit_code: Exit, message: str, preview_dir=None):
        super().__init__(message)
        self.stage, self.code, self.exit_code, self.message, self.preview_dir = stage, code, exit_code, message, preview_dir


@dataclasses.dataclass(frozen=True)
class PreviewResult:
    preview_id: str
    client_key: str
    period_id: str
    folder: Path
    pdf_path: Path
    pdf_sha256: str
    pdf_bytes: int
    page_count: int
    watermark_pages: int
    validation: str
    warnings: tuple

    def public(self, environ=None) -> dict:
        from seo_report.preflight import redact_path
        return {"status": "preview_only", "notice": NOTICE, "preview_id": self.preview_id, "client_key": self.client_key, "period_id": self.period_id,
                "preview_pdf": redact_path(self.pdf_path, environ), "preview_folder": redact_path(self.folder, environ), "pages": self.page_count,
                "label_verified_on_pages": self.watermark_pages, "validation": self.validation, "pdf_sha256": self.pdf_sha256, "pdf_bytes": self.pdf_bytes,
                "warnings": list(self.warnings)}


class PreviewRenderer:
    """The renderer handed to `dry_run_client`: the production renderer with the preview label (or an injected one, for offline tests). It remembers the last
    outcome so that the manifest reports what was actually measured."""

    def __init__(self, settings, inner=None):
        self.inner = inner or functools.partial(pr.render_report_pdf, timeout_seconds=settings.stage_timeouts.rendering, preview_label=LABEL)
        self.last = None

    def __call__(self, render_input, **kwargs):
        self.last = self.inner(render_input, **kwargs)
        return self.last


def previews_root(environ) -> Path:
    """`%LOCALAPPDATA%\\JasperSEOReporting\\previews`, after the location rules: no fallback, outside the project, OneDrive and every production folder."""
    environ = os.environ if environ is None else environ
    base = environ.get("LOCALAPPDATA")
    if not base:
        raise PreviewFailed("storage", "preview_location_unsafe", Exit.LEDGER_UNAVAILABLE, "LOCALAPPDATA is not set, so no preview folder can be chosen")
    path = rc.default_local_base(environ) / FOLDER_NAME
    try:
        canonical = rc.canonical_path(path)
        if os.path.normcase(str(canonical)) != os.path.normcase(os.path.abspath(path)):
            raise ValueError("aliased")
        key = rc.canonical_key(path)
        if rc.keys_overlap(key, rc.canonical_key(rc.PROJECT_ROOT)) or rc.in_cloud_sync_folder(canonical, environ):
            raise ValueError("unsafe")
    except Exception:
        raise PreviewFailed("storage", "preview_location_unsafe", Exit.LEDGER_UNAVAILABLE,
                            "the preview location must be a local folder outside the project and OneDrive") from None
    return path


def _not_production(root: Path, settings) -> None:
    key = rc.canonical_key(root)
    for protected in (settings.output_root, settings.work_root, settings.history_db_path.parent, settings.ledger_db_path.parent):
        if rc.keys_overlap(key, rc.canonical_key(protected)):
            raise PreviewFailed("storage", "preview_location_unsafe", Exit.LEDGER_UNAVAILABLE, "the preview folder may not overlap a production folder")


def _stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_manifest(folder: Path, data: dict, *, exclusive: bool = False) -> None:
    """The manifest, written atomically (a temporary file in the same folder, then a replace). The first write is exclusive."""
    path = folder / MANIFEST_NAME
    payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if exclusive:
        with open(path, "xb") as handle:
            handle.write(payload)
        return
    handle, temporary = tempfile.mkstemp(dir=str(folder), prefix=MANIFEST_NAME + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _manifest(status, preview_id, created_at, client_key, period_id, **extra) -> dict:
    data = {"schema": SCHEMA, "version": VERSION, "status": status, "notice": NOTICE, "preview_id": preview_id, "created_at": created_at,
            "client_key": client_key, "period_id": period_id, "label": LABEL}
    data.update(extra)
    return data


def _best_effort_manifest(folder, data):
    try:
        _write_manifest(folder, data)
    except OSError:
        pass  # the incomplete manifest written at the start remains and says so


def run_preview(*, settings, environ, services, client_key: str, period_id: str, renderer: PreviewRenderer, clock=None) -> PreviewResult:
    """Collect, assemble and render one preview. `services` is a `pipeline.Services` whose renderer is `renderer`; no ledger is involved. Raises `PreviewFailed`."""
    clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    root = previews_root(environ)
    _not_production(root, settings)
    created = clock()
    preview_id = f"{_stamp(created)}-{uuid.uuid4().hex[:8]}"
    folder = root / client_key / period_id / f"preview-{preview_id}"
    try:
        folder.mkdir(parents=True, exist_ok=False)  # a preview folder is never reused or overwritten
    except FileExistsError:
        raise PreviewFailed("storage", "preview_id_collision", Exit.LEDGER_UNAVAILABLE, "a preview folder with this identifier already exists; nothing was changed") from None
    except OSError:
        raise PreviewFailed("storage", "preview_storage_failed", Exit.LEDGER_UNAVAILABLE, "the preview folder could not be created") from None
    stamp = created.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    try:
        _write_manifest(folder, _manifest("incomplete", preview_id, stamp, client_key, period_id), exclusive=True)
    except OSError:
        raise PreviewFailed("storage", "preview_storage_failed", Exit.LEDGER_UNAVAILABLE, "the preview manifest could not be written", folder) from None

    def failed(stage, code, exit_code, message):
        _best_effort_manifest(folder, _manifest("failed", preview_id, stamp, client_key, period_id, failure={"stage": stage, "code": code}))
        return PreviewFailed(stage, code, exit_code, message, folder)

    try:
        response = pipeline.dry_run_client(client_key, period_id, services=services, scratch_dir=folder / "render")
    except (KeyboardInterrupt, SystemExit):
        _best_effort_manifest(folder, _manifest("interrupted", preview_id, stamp, client_key, period_id))
        raise
    except Exception as error:
        raise failed("rendering", "preview_unexpected_error", Exit.INTERNAL_ERROR, f"the preview stopped unexpectedly ({type(error).__name__})") from error
    if response.outcome != "succeeded":
        info = response.error or {}
        exit_code = exit_code_for_response(response)
        stage = "collection" if info.get("stage") in ("collecting",) else "rendering" if info.get("stage") == "rendering" else "request"
        raise failed(stage, info.get("code", "preview_failed"), exit_code,
                     f"the preview stopped at {stage}: {info.get('category', 'failure')}: {info.get('code', 'preview_failed')}")
    identity = response.identity or {}
    drafts = sorted((folder / "render").glob("*/reports/report.unarchived.pdf"))
    if len(drafts) != 1:
        raise failed("rendering", "preview_draft_missing", Exit.REPORT_FAILED, "the rendered preview PDF could not be found")
    try:
        data = drafts[0].read_bytes()
    except OSError:
        raise failed("storage", "preview_storage_failed", Exit.LEDGER_UNAVAILABLE, "the rendered preview PDF could not be read") from None
    digest = hashlib.sha256(data).hexdigest()
    if digest != identity.get("pdf_sha256"):
        raise failed("rendering", "preview_pdf_mismatch", Exit.REPORT_FAILED, "the rendered PDF does not match its recorded fingerprint")
    last = renderer.last
    if last is None or not getattr(last, "ok", False):
        raise failed("rendering", "preview_not_validated", Exit.REPORT_FAILED, "the renderer did not report a validated preview")
    try:
        info_pdf = pv.inspect_pdf(data)
        pages = info_pdf.page_count
    except pv.PdfValidationError:
        pages = 0
    watermark_pages = int(getattr(last, "watermark_pages", 0))
    target = folder / PDF_NAME
    try:
        with open(target, "xb") as handle:  # exclusive: never overwrites
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise failed("storage", "preview_storage_failed", Exit.LEDGER_UNAVAILABLE, "the preview PDF could not be saved") from None
    warnings = tuple(sorted({w["code"] for w in response.warnings}))
    final = _manifest("preview_only", preview_id, stamp, client_key, period_id,
                      pdf={"name": PDF_NAME, "sha256": digest, "bytes": len(data), "pdf16": digest[:16], "pages": pages},
                      validation={"result": "passed", "layout_issues": 0, "label_verified_on_pages": watermark_pages,
                                  "browser_version": getattr(last, "browser_version", None)},
                      warnings=list(warnings))
    try:
        _write_manifest(folder, final)
    except OSError:
        raise PreviewFailed("storage", "preview_storage_failed", Exit.LEDGER_UNAVAILABLE,
                            "the preview manifest could not be completed; the PDF is saved but the manifest still says 'incomplete'", folder) from None
    return PreviewResult(preview_id, client_key, period_id, folder, target, digest, len(data), pages, watermark_pages, "passed", warnings)
