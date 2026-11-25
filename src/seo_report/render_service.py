"""Building the render input and producing the sidecar and draft PDF for one run (R8.3).

Connects `render_identity` (fingerprints, `build_render_input`), `pdf_renderer` and `render_artifacts.produce_report_draft`.
The identity rules of R7.2/R7.4 are used as they are:

  * The render input finalization receives is **exactly** `DraftOutcome.render_input` returned by `produce_report_draft`
    (on a resume that is the original sidecar's input: its hash and generation date survive).
  * The caller supplies the run's `generation_date`, recorded once in the ledger; it is only a candidate when no open
    sidecar exists. A later retry that passes a different date still gets the original sidecar's input.
  * Sidecars and drafts are created exclusively and never overwritten or deleted (engine behaviour).
  * Finalized render hashes come from `render_state` (valid version 2 reviewer records) and must be complete.
  * Every call uses a fresh, empty work folder under the configured work root (outside the project, as the renderer requires).

Failures are `contracts.StageError` with a category from `contracts.ERROR_CATEGORIES`.
"""

import datetime as dt
import functools
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from seo_report import output_layout as layout
from seo_report import pdf_renderer as pr
from seo_report import render_artifacts as ra
from seo_report import render_identity as idn
from seo_report import render_state as rstate
from seo_report.clients import regional_logo_path
from seo_report.contracts import StageError

LOGGER = logging.getLogger("seo_report.render_service")

_CONFLICT_CODES = {"draft_without_sidecar", "sidecar_invalid", "multiple_open_sidecars", "render_identity_mismatch",
                   "rendered_pdf_mismatch", "render_not_reproducible", "draft_pdf_mismatch",
                   "render_input_already_finalized", "sidecar_conflict", "draft_exists_differs", "draft_not_regular",
                   "source_in_reports_dir"}


@dataclass(frozen=True)
class RenderResult:
    render_input: object  # the immutable ClientRenderInput that was rendered (DraftOutcome.render_input)
    drafted: ra.DraftOutcome
    generation_date: dt.date


def font_sources(settings) -> tuple:
    if not settings.fonts:
        raise StageError("config_invalid", "fonts_not_configured")
    return tuple(pr.FontSource(f.label, f.path, weight=f.weight) for f in settings.fonts)


def new_workdir(settings, run_key: str, attempt: int) -> Path:
    """A fresh, empty folder `<work_root>/<run_key with '/' as '__'>/a<attempt>-<n>`; existing folders are never reused."""
    base = Path(settings.work_root) / run_key.replace("/", "__")
    for n in range(1, 1000):
        candidate = base / f"a{attempt}-{n}"
        if not candidate.exists():
            candidate.mkdir(parents=True)
            return candidate
    raise StageError("render_failed", "workdir_exhausted")


def render(assembled, client_run, *, settings, generation_date: dt.date, attempt: int, renderer=None, ops=None,
           reports_dir: Path | None = None, reviewer_dir: Path | None = None, workdir: Path | None = None) -> RenderResult:
    """Produce the sidecar and draft for `assembled`. `reports_dir`/`reviewer_dir`/`workdir` default to the production
    locations of the run; a dry run passes scratch folders instead."""
    root, key = settings.output_root, client_run.client_key
    start, end = client_run.periods.current.start, client_run.periods.current.end
    reports = reports_dir or layout.report_dir(root, key, start, end)
    reviewer = reviewer_dir or layout.reviewer_dir(root, key, start, end)
    try:
        finalized = rstate.finalized_render_hashes(reviewer, require_complete=True)
    except rstate.RenderStateError as error:
        raise StageError("render_failed", "reviewer_scan_" + error.code[:40]) from None
    font_paths = {f.label: f.path for f in settings.fonts}
    sources = font_sources(settings)
    logo_path = regional_logo_path(client_run.client.sender) or settings.logo_path  # a regional profile may name its own logo (inside assets/)
    try:
        fingerprints = idn.measure_render_fingerprints(logo_path=logo_path, font_paths=font_paths)
        candidate = idn.build_render_input(assembled.bundle, sender=client_run.client.sender,
                                           generation_date=generation_date,
                                           history_insights=assembled.history_insights, fingerprints=fingerprints)
    except (ValueError, OSError):
        LOGGER.warning("render input could not be built", exc_info=True)
        raise StageError("render_failed", "render_input_invalid") from None
    work = workdir or new_workdir(settings, client_run.run_key, attempt)
    if renderer is None:  # the real browser, under the configured hard limit (`execution.stage_timeouts.rendering`)
        renderer = functools.partial(pr.render_report_pdf, timeout_seconds=settings.stage_timeouts.rendering)
    try:
        drafted = ra.produce_report_draft(reports, candidate, workdir=work, logo_path=logo_path, fonts=sources,
                                          finalized_render_hashes=finalized, renderer=renderer, ops=ops)
    except pr.RenderTimeoutError as error:
        # raised before any PDF, sidecar or draft exists for this attempt: nothing was promoted, a retry renders again
        LOGGER.warning("render timed out in the %s phase", error.phase)
        raise StageError("render_failed", f"render_timeout_{error.phase}") from None
    except Exception as error:
        if type(error).__module__.startswith("playwright"):  # the browser, not our code
            LOGGER.warning("browser error while rendering", exc_info=True)
            raise StageError("render_failed", "browser_error") from None
        raise
    if not drafted.ok:
        code = drafted.code or "render_failed"
        if code == "render_has_layout_issues":
            raise StageError("layout_issues", code)
        if code in _CONFLICT_CODES:
            raise StageError("finalization_conflict", code)
        raise StageError("render_failed", code)
    return RenderResult(drafted.render_input, drafted, drafted.generation_date)
