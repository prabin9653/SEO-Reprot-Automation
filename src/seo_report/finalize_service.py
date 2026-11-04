"""Finalizing the exact draft and render input of a run (R8.3).

Calls `report_finalize.finalize_report(..., render_input=<the render input produced for this draft>)` and maps its result
to the runner's categories. The call is idempotent in the engine: the stored `finalized_at`, `render_input` and
`replace_existing` are reused on every retry, so a retry never changes the record's identity or timestamp.

Mapping (docs/r8/production_architecture.md §5.5):

  finalized / already_finalized      ok
  stale_bundle                       `stale_bundle`        (retry from collection)
  conflict                           `finalization_conflict` (blocked: an operator must decide)
  rejected with a render_* category  `finalization_conflict`
  rejected (draft_invalid, ...)      `render_failed`       (the draft was not valid; render again)
  failed                             `finalization_failed` (retry the same call)
  anything else                      `internal_error`

This service never passes `render_input=None`: the version 1 path is for historical records only.
"""

from dataclasses import dataclass

from seo_report import report_finalize as rf
from seo_report.contracts import StageError


@dataclass(frozen=True)
class FinalizeVerdict:
    run_id: str
    revision: int
    pdf_sha256: str
    pdf_bytes: int
    record_name: str
    outcome: str  # finalized | already_finalized
    result: rf.FinalizationResult


def finalize(assembled, rendered, client_run, *, settings, finalized_at: str, replace_existing: bool, environ=None,
             ops=None, store_factory=None, allow_onedrive_output: bool = False) -> FinalizeVerdict:
    """Finalize; returns a verdict or raises `StageError`."""
    from seo_report import output_layout as layout
    start, end = client_run.periods.current.start, client_run.periods.current.end
    draft = layout.report_dir(settings.output_root, client_run.client_key, start, end) / layout.DRAFT_PDF_NAME
    options = dict(
        bundle=assembled.bundle, draft_path=draft, output_root=settings.output_root,
        history_db_path=settings.history_db_path, finalized_at=finalized_at, replace_existing=replace_existing,
        history_insights=assembled.history_insights, recommendation_annotations=assembled.annotations,
        history_config=assembled.history_config, environ=environ, allow_onedrive_output=allow_onedrive_output,
        render_input=rendered.render_input)
    if ops is not None:
        options["ops"] = ops
    if store_factory is not None:
        options["store_factory"] = store_factory
    result = rf.finalize_report(**options)
    if result.ok:
        return FinalizeVerdict(result.run_id, result.revision, result.pdf_sha256, result.pdf_bytes, result.record_name,
                               result.outcome, result)
    code = result.category or "finalization_failed"
    if result.outcome == rf.OUTCOME_STALE:
        raise StageError("stale_bundle", "stale_bundle")
    if result.outcome == rf.OUTCOME_CONFLICT or (result.outcome == rf.OUTCOME_REJECTED and code.startswith("render_")):
        raise StageError("finalization_conflict", code)
    if result.outcome == rf.OUTCOME_REJECTED:
        if code in ("draft_invalid", "draft_partial_conflict"):
            raise StageError("render_failed", code)
        raise StageError("internal_error", code)
    if result.outcome == rf.OUTCOME_FAILED:
        raise StageError("finalization_failed", code)
    raise StageError("internal_error", code)
