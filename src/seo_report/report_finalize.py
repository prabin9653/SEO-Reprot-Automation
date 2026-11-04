"""The finalization orchestrator (Stage 6c-8f): from an assembled report and a draft PDF to a finalized report.

`finalize_report(...)` coordinates components that already exist and are not changed here:

  report_run          the assembled `ReportBundle` (and its `_compare_with_history`, used for the stale check)
  client_content_hash `client_content_sha256`
  finalize_files      draft validation, hashing, name choice
  report_promotion    `promote_draft`
  reviewer_record     `build_reviewer_record`
  record_writer       `write_reviewer_record`
  history_access      probe, finalized-only reader
  snapshots.store     `SnapshotStore` (write, add_event, verify_run) and `SnapshotReader`
  attempt_log         failure-only attempt records

Sequence (each step first looks at what already exists, so a retry after any interruption continues, never repeats):

  1 preflight   bundle consistent; output folders exist (`reports/` and `reviewer/` of the period; nothing is created);
                no renderer `.partial` draft; draft PDF validated (`validate_draft_pdf`); client-content hash;
                the history is re-compared and must still match the bundle (stale check, below)
  2 reconcile   read-only: the stored snapshot of this client and period, its `rendered` events, `report.pdf`, the
                reviewer record, the 20-record reader limit. Anything that would make a later step conflict
                (a different report.pdf without `replace_existing`, a draft that exists only as a superseded copy,
                a full superseded/duplicate name set, a reviewer record with another identity or invalid content, the
                reader limit) stops here, before the first change
  3 dry run     the reviewer record is built in memory with a placeholder run ID and checked by the real validator
  4 persist     `SnapshotStore.write` (identical content reuses the run and revision; changed content adds a revision
                by the store's own rule), then the run is read back and verified (`verify_run`)
  5 event       one `rendered` event with the exact full PDF hash, unless that exact event already exists
  6 promote     `promote_draft` (no overwrite; an old report is kept under its superseded name; an identical draft is
                kept as a duplicate). When the draft is gone and `report.pdf` is the PDF that this run's rendered
                event names, the PDF was already promoted and this step is skipped
  7 record      the real reviewer record is built from the stored run and written by `write_reviewer_record`
  8 verify      the finalized-only reader must list the run, and the record, the PDF and the event are checked again
  9 result      "finalized" or "already_finalized" only after step 8 succeeded

SQLite and the filesystem are not one transaction. A failure after step 4 leaves the earlier steps in place (nothing is
rolled back, restored or deleted) and the result says `incomplete=True`: run the same call again. Interruption at
any boundary therefore ends in one of three ways on retry: it completes, it reports the same stop, or it reports a
fixed conflict category.

Stale bundle check (point in time, no locking): the history comparison the bundle was assembled from is repeated with
the same code (`report_run._compare_with_history`) against the history as it is now. The assembled and the new
history state, limits, per-dimension statuses (including the baseline run, revision and creation time), skipped
candidates and unfinalized-run list must be equal. Entries that belong to this report's own period are left out
of both sides (an earlier interrupted attempt leaves such an orphan run, and a finished one is "the same period" for
the comparison). Any difference, including history becoming available or a baseline changing, stops with
`stale_bundle` before anything is written. The bundle is never rebuilt. There is no lock file or cross-process
protection: two processes finalizing at once are not supported (single operator).

Attempt records (failure only): a stop that has an approved step and status code is written with
`attempt_log.write_attempt_record` into the existing `attempts/` folder of the period, best effort. A missing folder,
a failed write or any error there is reported as `attempt_log` and never changes or hides the original result.

Render identity (R7.4, only with `render_input=`): the immutable `ClientRenderInput` that produced the PDF is checked in
step 2, before anything is written (`_check_render_identity`): it matches the bundle (`render_identity.check_matches_bundle`),
the historical statements it draws equal the ones the record lists, and its sidecar (`render_state.read_sidecar_for`)
carries exactly its hash and the PDF's SHA-256 and size. The receipt then carries `render_input_sha256` and the
format, the record is schema version 2 and the final verification compares the nine-value identity. Without
`render_input` nothing of this runs and the version 1 flow is unchanged. A retry uses the render input of the
original sidecar (its hash and generation date); nothing here renders or rebuilds one.

Limits (documented, not hidden)
  * Known and left as is (G1): after the draft has been promoted and before a v2 record exists, the sidecar is still
    "open" for `render_artifacts.produce_report_draft`; calling it again would re-render and hand off a duplicate
    draft, which a later finalization preserves as a duplicate copy. `render_state.finalized_render_hashes` closes a
    sidecar only once its record exists.
  * `superseded_previous_pdf` / `duplicate_draft_preserved_as` of the reviewer record are known only when this call
    performed the replacement or the duplicate preservation; a retry that finds the PDF already promoted records
    null for both (the preserved files themselves are untouched).
  * A draft that is already gone is accepted as "already promoted" only when `report.pdf` is a valid PDF whose
    SHA-256 is named by a `rendered` event of this run.
  * The history database folder is created by the snapshot store when the database does not exist yet, as before.

Results contain fixed categories, run IDs, file names, hashes and sizes only: no paths, exception text or record
content.
"""

import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from seo_report import attempt_log as al
from seo_report import finalize_files as ff
from seo_report import history_access as ha
from seo_report import output_layout as layout
from seo_report import record_schema as rs
from seo_report import record_writer as rw
from seo_report import render_input as ri
from seo_report import render_input_hash as rh
from seo_report import report_promotion as rp
from seo_report import report_run as rr
from seo_report.client_content_hash import ClientContentHashError, client_content_sha256
from seo_report.reviewer_record import (FinalizationReceipt, PreservedPdf, RenderInputFormat, ReviewerRecordError,
                                        build_reviewer_record)
from seo_report.snapshots import store as st
from seo_report.snapshots.baselines import HistoryConfig

OUTCOME_FINALIZED = "finalized"
OUTCOME_ALREADY = "already_finalized"
OUTCOME_STALE = "stale_bundle"
OUTCOME_CONFLICT = "conflict"
OUTCOME_REJECTED = "rejected"
OUTCOME_FAILED = "failed"
OUTCOMES = (OUTCOME_FINALIZED, OUTCOME_ALREADY, OUTCOME_STALE, OUTCOME_CONFLICT, OUTCOME_REJECTED, OUTCOME_FAILED)

ATTEMPT_WRITTEN = "written"
ATTEMPT_NOT_NEEDED = "not_needed"  # the call succeeded
ATTEMPT_NOT_RECORDED = "not_recorded"  # the stop has no approved step/status code
ATTEMPT_NO_DIRECTORY = "directory_missing"
ATTEMPT_FAILED = "failed"
ATTEMPT_OUTCOMES = (ATTEMPT_WRITTEN, ATTEMPT_NOT_NEEDED, ATTEMPT_NOT_RECORDED, ATTEMPT_NO_DIRECTORY, ATTEMPT_FAILED)

CATEGORIES = (
    "bundle_inconsistent", "output_unavailable", "draft_invalid", "draft_partial_conflict", "stale_bundle",
    "history_unreadable", "existing_report_differs", "superseded_only_copy", "existing_report_not_regular",
    "destination_appeared", "superseded_name_conflict", "superseded_variants_exhausted", "duplicate_name_conflict",
    "duplicate_variants_exhausted", "reader_record_limit", "record_identity_differs", "existing_record_invalid",
    "existing_record_not_regular", "record_preflight_failed", "snapshot_persist_failed", "snapshot_mismatch",
    "event_failed", "preserve_failed", "promote_failed", "draft_changed", "record_write_failed",
    "reader_verification_failed", "unexpected_error",
    # render identity (only when a render input is supplied); the attempt status code is `draft_invalid` or none
    "render_input_invalid", "render_input_bundle_mismatch", "render_statements_mismatch",
    "render_input_format_invalid", "render_sidecar_missing", "render_sidecar_invalid", "render_sidecar_unreadable",
    "render_identity_mismatch", "render_pdf_mismatch")

_PLACEHOLDER_RUN_ID = "00000000-0000-7000-8000-000000000000"
_STEP_CODE = {"persist": "persist_failed", "event": "event_failed", "supersede": "supersede_failed",
              "promote": "promote_failed", "record": "record_failed"}

# promote_draft conflict / failure category -> (attempt step, attempt status code, result category)
_PROMOTION_CONFLICT = {
    "existing_report_differs": ("promote", "existing_report_conflict", "existing_report_differs"),
    "superseded_only_copy": ("promote", "existing_report_conflict", "superseded_only_copy"),
    "existing_report_not_regular": ("promote", "existing_report_conflict", "existing_report_not_regular"),
    "destination_appeared": ("promote", "existing_report_conflict", "destination_appeared"),
    "superseded_name_conflict": ("supersede", "superseded_name_conflict", "superseded_name_conflict"),
    "superseded_variants_exhausted": ("supersede", "superseded_variants_exhausted", "superseded_variants_exhausted"),
    "duplicate_name_conflict": ("promote", "duplicate_draft_conflict", "duplicate_name_conflict"),
    "duplicate_variants_exhausted": ("promote", "duplicate_draft_conflict", "duplicate_variants_exhausted"),
}
_PROMOTION_FAILURE = {
    "draft_changed": ("verify_draft", "draft_invalid", "draft_changed"),
    "preserve_failed": ("supersede", "supersede_failed", "preserve_failed"),
    "preserved_content_mismatch": ("supersede", "supersede_failed", "preserve_failed"),
}
_RECORD_CONFLICT = {
    "reader_record_limit": "reader_record_limit",
    "record_identity_differs": "reviewer_record_conflict",
    "existing_record_invalid": "reviewer_record_conflict",
    "existing_record_not_regular": "reviewer_record_conflict",
}


@dataclass(frozen=True)
class FinalizationResult:
    outcome: str  # one of OUTCOMES
    finalized: bool  # True only after the finalized-only reader confirmed the final state
    step: str | None  # the approved attempt step where the call stopped
    category: str | None  # one of CATEGORIES
    status_code: str | None  # the approved attempt status code for the stop, when there is one
    run_id: str | None = None
    revision: int | None = None
    snapshot_created: bool | None = None  # this call stored a new run (False: an identical run was reused)
    event_recorded: bool | None = None  # this call recorded the rendered event (False: it already existed)
    pdf_outcome: str | None = None  # promote_draft's outcome, or "already_promoted"
    final_name: str | None = None
    pdf_sha256: str | None = None
    pdf_bytes: int | None = None
    preserved_report: str | None = None  # file name of the old report kept under its superseded name
    duplicate_draft: str | None = None  # file name of the draft kept as a duplicate copy
    record_name: str | None = None
    record_outcome: str | None = None
    attempt_log: str = ATTEMPT_NOT_NEEDED  # one of ATTEMPT_OUTCOMES
    incomplete: bool = False  # something was written and the run is not finalized yet: call again
    draft_problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome in (OUTCOME_FINALIZED, OUTCOME_ALREADY)


class _Stop(Exception):
    def __init__(self, outcome, step, code, category, *, exception=None, draft_problems=()):
        super().__init__(category)
        self.outcome, self.step, self.code, self.category = outcome, step, code, category
        self.exception, self.draft_problems = exception, tuple(draft_problems)


class _Context:
    """What the call has learned so far. Never leaves this module."""

    def __init__(self):
        self.step = "preflight"
        self.root = None
        self.client_key = None
        self.period = None
        self.attempted_at = None
        self.run_id = self.revision = None
        self.pdf = None  # (sha256, byte_count)
        self.snapshot_created = self.event_recorded = None
        self.pdf_outcome = self.final_name = None
        self.preserved = self.duplicate = None  # rp.PreservedCopy
        self.record_name = self.record_outcome = None
        self.mutated = False
        self.old_report_preserved = False


# --- small helpers ------------------------------------------------------------------------------------------------------------

def _is_real_dir(path, ops) -> bool:
    try:
        info = ops.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not ff._is_link(info)


def _exists(path, ops) -> bool:
    try:
        ops.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # unknown: treat as present so that nothing is overwritten
    return True


def _own_period(item, period) -> bool:
    return (item.period_start, item.period_end) == period


def _rendered_detail(pdf_sha256: str) -> str:
    return f"Report rendered (PDF {pdf_sha256})"


def _identity(run_id, revision, snapshot, pdf, client_hash, render=None) -> tuple:
    """The six-value record identity; with a render identity the nine-value one (`record_writer.record_identity`)."""
    identity = (run_id, revision, snapshot.content_hash, pdf[0], pdf[1], client_hash)
    return identity if render is None else identity + tuple(render)


# --- render identity (only when a render input is supplied) -----------------------------------------------------------

def _render_stop(category, *, outcome=OUTCOME_REJECTED, code="draft_invalid") -> _Stop:
    # existing attempt status code only (the set is closed): a draft that does not match its render identity is invalid
    return _Stop(outcome, "verify_draft", code, category)


def _check_render_identity(render_input, bundle, history_insights, reports, ctx, ops):
    """Everything that must hold before the first change when a render input is supplied. Read-only.

    Returns (render input with its snapshot identity restored, (render_input_sha256, format name, format version)).
    The hash is of the supplied immutable object; the sidecar must carry exactly that hash and the PDF being finalized.
    """
    from seo_report import render_identity as rid
    from seo_report import render_state as rstate

    snapshot = bundle.snapshot
    try:
        render_input = rstate.with_operational(render_input, client_key=snapshot.client_key,
                                               snapshot_content_hash=snapshot.content_hash)
    except rstate.RenderStateError:
        raise _render_stop("render_input_bundle_mismatch") from None
    try:
        digest = rh.render_input_sha256(render_input)
    except Exception:
        raise _render_stop("render_input_invalid") from None
    try:
        rid.check_matches_bundle(render_input, bundle)
    except rid.RenderIdentityError:
        raise _render_stop("render_input_bundle_mismatch") from None
    # the historical statements that were drawn are the ones the record will list
    recorded = () if history_insights is None else tuple(history_insights.client_statements)
    wanted = tuple((s.section, s.key, s.text, s.group, s.baseline_period_start, s.baseline_period_end)
                   for s in recorded)
    drawn = tuple((s.section, s.key, s.text, s.group, s.baseline_start.isoformat(), s.baseline_end.isoformat())
                  for s in render_input.historical_statements)
    if drawn != wanted:
        raise _render_stop("render_statements_mismatch")
    if rh.FORMAT_NAME != rs.RENDER_INPUT_FORMAT_NAME or rh.FORMAT_VERSION not in rs.SUPPORTED_RENDER_INPUT_VERSIONS:
        raise _render_stop("render_input_format_invalid")
    try:
        sidecar = rstate.read_sidecar_for(reports, digest, ops=ops)
    except rstate.RenderStateError as err:
        if err.code == "sidecar_missing":
            raise _render_stop("render_sidecar_missing") from None
        if err.code in ("sidecar_unreadable", "reports_dir_unreadable"):
            raise _render_stop("render_sidecar_unreadable", outcome=OUTCOME_FAILED, code=None) from None
        if err.code == "unsupported_version" and err.group == "render_input_format":
            raise _render_stop("render_input_format_invalid") from None
        if err.code in ("hash_mismatch", "identity_mismatch"):
            raise _render_stop("render_identity_mismatch") from None
        raise _render_stop("render_sidecar_invalid") from None
    if (sidecar.pdf_sha256, sidecar.pdf_bytes) != tuple(ctx.pdf):
        raise _render_stop("render_pdf_mismatch")
    return render_input, (digest, rh.FORMAT_NAME, rh.FORMAT_VERSION)


# --- preflight -----------------------------------------------------------------------------------------------------------------------

def _check_bundle(bundle) -> None:
    snapshot, reviewer, content = bundle.snapshot, bundle.reviewer_report, bundle.client_content
    current = content.periods.current
    consistent = (
        snapshot.client_key == reviewer.client_key
        and (reviewer.snapshot.client_key, reviewer.snapshot.period_start, reviewer.snapshot.period_end,
             reviewer.snapshot.content_hash) == (snapshot.client_key, snapshot.period_start, snapshot.period_end,
                                                 snapshot.content_hash)
        and (current.start.isoformat(), current.end.isoformat()) == (snapshot.period_start, snapshot.period_end))
    if not consistent:
        raise _Stop(OUTCOME_REJECTED, "preflight", None, "bundle_inconsistent")


def _check_stale(bundle, ctx, *, history_db_path, output_root, environ, allow_onedrive_output, history_config) -> None:
    """Repeat the assembly's history comparison and require the same result (see the module docstring)."""
    snapshot, reviewer = bundle.snapshot, bundle.reviewer_report
    try:
        comparison, status, excluded, _ = rr._compare_with_history(
            snapshot, ctx.client_key, (), history_db_path, output_root, environ, allow_onedrive_output, history_config)
    except Exception as err:  # an unexpected failure while reading: nothing can be said about the history
        raise _Stop(OUTCOME_FAILED, "preflight", None, "history_unreadable", exception=err) from None

    def outside(items):
        return tuple(item for item in items if not _own_period(item, ctx.period))

    same = reviewer.history_status.state == status.state
    old, new = bundle.historical_comparison, comparison
    if same and (old is None) != (new is None):
        same = False
    if same and old is not None:
        same = (old.status == new.status and old.limits == new.limits
                and outside(old.skipped) == outside(new.skipped))
    if same:
        same = outside(reviewer.excluded_unfinalized) == outside(excluded)
    if not same:
        raise _Stop(OUTCOME_STALE, "preflight", "stale_bundle", "stale_bundle")


# --- reconciliation (read-only) -------------------------------------------------------------------------------------------------------

def _read_history_state(bundle, ctx, history_db_path, environ):
    """(latest stored run of this client and period or None, its events). Read-only; a missing database is empty."""
    probe = ha.probe_history(history_db_path, environ=environ)
    if probe.status == ha.STATUS_MISSING:
        return None, ()
    if not probe.available:
        raise _Stop(OUTCOME_FAILED, "preflight", None, "history_unreadable")
    reader = None
    try:
        reader = st.SnapshotReader(history_db_path, environ=environ)
        latest = reader.latest(ctx.client_key, *ctx.period)
        if latest is None or latest.content_hash != bundle.snapshot.content_hash:
            return None, ()
        return latest, reader.events(latest.run_id)
    except (st.SnapshotStoreError, sqlite3.Error, ValueError):
        raise _Stop(OUTCOME_FAILED, "preflight", None, "history_unreadable") from None
    finally:
        if reader is not None:
            reader.close()


def _validate_pdf_source(ctx, reports, draft, latest, events, ops):
    """The PDF this call finalizes: ('draft', check) or ('promoted', check). Raises _Stop when there is none."""
    check = ff.validate_draft_pdf(draft, ops=ops)
    if check.ok:
        return "draft", check
    if check.problems == ("not_found",) and latest is not None:
        final = reports / layout.STANDARD_PDF_NAME
        promoted = ff.validate_draft_pdf(final, ops=ops)
        if promoted.ok and any(e.kind == "rendered" and e.detail == _rendered_detail(promoted.sha256) for e in events):
            return "promoted", promoted
    raise _Stop(OUTCOME_REJECTED, "verify_draft", "draft_invalid", "draft_invalid", draft_problems=check.problems)


def _preview_promotion(source, reports, pdf, replace_existing, ops) -> None:
    """The conflicts `promote_draft` would report, found before the first change. Read-only."""
    if source != "draft":
        return
    final = reports / layout.STANDARD_PDF_NAME
    try:
        info = ops.lstat(final)
    except FileNotFoundError:
        return
    except OSError:
        raise _Stop(OUTCOME_CONFLICT, "promote", "existing_report_conflict", "existing_report_not_regular") from None
    if ff._is_link(info) or not stat.S_ISREG(info.st_mode):
        raise _Stop(OUTCOME_CONFLICT, "promote", "existing_report_conflict", "existing_report_not_regular")
    try:
        current = ff.sha256_file(final, ops=ops)
    except ff.FileOperationError:
        raise _Stop(OUTCOME_FAILED, "promote", "promote_failed", "promote_failed") from None
    try:
        if current.sha256 == pdf[0]:
            ff.choose_preserved_name(reports, "duplicate", pdf[0], ops=ops)
            return
        if rp._superseded_copy_of(reports, ops, pdf[0], pdf[1]):
            raise _Stop(OUTCOME_CONFLICT, "promote", "existing_report_conflict", "superseded_only_copy")
        if not replace_existing:
            raise _Stop(OUTCOME_CONFLICT, "promote", "existing_report_conflict", "existing_report_differs")
        ff.choose_preserved_name(reports, "superseded", current.sha256, ops=ops)
    except ff.FileConflictError as err:
        family = "duplicate" if current.sha256 == pdf[0] else "superseded"
        if err.kind == "variants_exhausted":
            category = f"{family}_variants_exhausted"
        else:
            category = f"{family}_name_conflict"
        step, code, category = _PROMOTION_CONFLICT[category]
        raise _Stop(OUTCOME_CONFLICT, step, code, category) from None
    except ff.FileOperationError:
        raise _Stop(OUTCOME_FAILED, "promote", "promote_failed", "promote_failed") from None


def _preview_record(reviewer_dir, latest, identity_fields, ops) -> None:
    """For an already stored run: the target record must be absent, matching, or leave room under the limit."""
    if latest is None:
        return
    run_id = latest.run_id
    name = layout.reviewer_record_name(run_id, identity_fields["pdf_sha256"])
    path = reviewer_dir / name
    try:
        info = ops.lstat(path)
    except FileNotFoundError:
        try:
            count = rw.count_run_records(reviewer_dir, run_id)
        except OSError:
            raise _Stop(OUTCOME_FAILED, "record", "record_failed", "record_preflight_failed") from None
        if count >= rw.MAX_RECORDS_PER_RUN:
            raise _Stop(OUTCOME_CONFLICT, "preflight", "reader_record_limit", "reader_record_limit")
        return
    except OSError:
        raise _Stop(OUTCOME_FAILED, "record", "record_failed", "record_preflight_failed") from None
    kind, existing = rw._read_existing(path, info, ops)
    if kind == "not_regular":
        raise _Stop(OUTCOME_CONFLICT, "record", "reviewer_record_conflict", "existing_record_not_regular")
    if kind == "unreadable":
        raise _Stop(OUTCOME_FAILED, "record", "record_failed", "record_preflight_failed")
    if kind == "invalid":
        raise _Stop(OUTCOME_CONFLICT, "record", "reviewer_record_conflict", "existing_record_invalid")
    wanted = (run_id, latest.revision, latest.content_hash, identity_fields["pdf_sha256"],
              identity_fields["pdf_bytes"], identity_fields["client_content_sha256"])
    if identity_fields.get("render") is not None:  # a version 2 record is being finalized: nine values
        wanted += tuple(identity_fields["render"])
    if rw.record_identity(existing) != wanted:
        raise _Stop(OUTCOME_CONFLICT, "record", "reviewer_record_conflict", "record_identity_differs")


# --- persistence --------------------------------------------------------------------------------------------------------------------

def _persist(bundle, ctx, pdf, history_db_path, environ, allow_cloud_sync, store_factory) -> None:
    factory = store_factory or st.SnapshotStore
    ctx.step = "persist"
    try:
        store = factory(history_db_path, allow_cloud_sync=allow_cloud_sync, environ=environ)
    except Exception as err:
        raise _Stop(OUTCOME_FAILED, "persist", "persist_failed", "snapshot_persist_failed", exception=err) from None
    try:
        try:
            result = store.write(bundle.snapshot)
            ctx.mutated = True
            ctx.run_id, ctx.revision, ctx.snapshot_created = result.run_id, result.revision, result.created
            stored = store.get(result.run_id)
            if (stored.content_hash, stored.client_key, stored.period_start, stored.period_end, stored.revision,
                    stored.voided) != (bundle.snapshot.content_hash, ctx.client_key, ctx.period[0], ctx.period[1],
                                       result.revision, False) or not store.verify_run(result.run_id).ok:
                raise _Stop(OUTCOME_FAILED, "persist", "persist_failed", "snapshot_mismatch")
        except _Stop:
            raise
        except Exception as err:
            raise _Stop(OUTCOME_FAILED, "persist", "persist_failed", "snapshot_persist_failed", exception=err) from None

        ctx.step = "event"
        detail = _rendered_detail(pdf[0])
        wanted = st.mask_event_detail(detail)
        try:
            if any(e.kind == "rendered" and e.detail == wanted for e in store.events(ctx.run_id)):
                ctx.event_recorded = False
            else:
                store.add_event(ctx.run_id, "rendered", detail)
                ctx.event_recorded = True
            if not any(e.kind == "rendered" and e.detail == wanted for e in store.events(ctx.run_id)):
                raise _Stop(OUTCOME_FAILED, "event", "event_failed", "event_failed")
        except _Stop:
            raise
        except Exception as err:
            raise _Stop(OUTCOME_FAILED, "event", "event_failed", "event_failed", exception=err) from None
    finally:
        try:
            store.close()
        except Exception:
            pass


def _promote(ctx, source, reports, replace_existing, ops) -> None:
    ctx.step = "promote"
    if source == "promoted":
        ctx.pdf_outcome, ctx.final_name = "already_promoted", layout.STANDARD_PDF_NAME
        return
    result = rp.promote_draft(reports, replace_existing=replace_existing, ops=ops)
    if result.preserved_existing is not None:
        ctx.preserved, ctx.old_report_preserved = result.preserved_existing, True
    ctx.pdf_outcome = result.outcome
    if result.ok:
        ctx.final_name, ctx.duplicate = result.final_name, result.duplicate_preserved
        return
    if result.outcome == rp.OUTCOME_REJECTED:
        raise _Stop(OUTCOME_REJECTED, "verify_draft", "draft_invalid", "draft_invalid",
                    draft_problems=result.draft_problems)
    if result.outcome == rp.OUTCOME_CONFLICT:
        step, code, category = _PROMOTION_CONFLICT[result.conflict]
        raise _Stop(OUTCOME_CONFLICT, step, code, category)
    step, code, category = _PROMOTION_FAILURE.get(result.failure, ("promote", "promote_failed", "promote_failed"))
    raise _Stop(OUTCOME_FAILED, step, code, category)


def _receipt(ctx, bundle, check, client_hash, finalized_at, run_id, revision, render=None) -> FinalizationReceipt:
    def kept(copy):
        return None if copy is None else PreservedPdf(copy.sha256[:layout.HASH16_CHARS], copy.number)

    sha, size = ctx.pdf
    # the preserved old report is named by its own hash; the duplicate draft by the promoted PDF's hash
    superseded = None if ctx.preserved is None else PreservedPdf(ctx.preserved.sha256[:layout.HASH16_CHARS],
                                                                 ctx.preserved.number)
    duplicate = kept(ctx.duplicate)
    return FinalizationReceipt(
        run_id=run_id, revision=revision, finalized_at=finalized_at, pdf_sha256=sha, pdf_bytes=size,
        pdf_checks=check.checks, client_content_sha256=client_hash, rendered_event_detail=_rendered_detail(sha),
        superseded_previous_pdf=superseded, duplicate_draft_preserved_as=duplicate,
        render_input_sha256=None if render is None else render[0],
        render_input_format=None if render is None else RenderInputFormat(render[1], render[2]))


def _write_record(ctx, record, reviewer_dir, ops) -> None:
    ctx.step = "record"
    result = rw.write_reviewer_record(reviewer_dir, record, ops=ops)
    ctx.record_name, ctx.record_outcome = result.name, result.outcome
    if result.ok:
        return
    if result.outcome == rw.OUTCOME_CONFLICT:
        raise _Stop(OUTCOME_CONFLICT, "record", _RECORD_CONFLICT[result.conflict], result.conflict)
    raise _Stop(OUTCOME_FAILED, "record", "record_failed", "record_write_failed")


def _verify_final_state(ctx, bundle, identity, reports, reviewer_dir, history_db_path, output_root, environ,
                        allow_onedrive_output, ops) -> None:
    """The finalized-only reader must list the run; the record, the PDF and the event are then checked directly."""
    ctx.step = "record"
    fail = _Stop(OUTCOME_FAILED, "record", "record_failed", "reader_verification_failed")
    sha, size = ctx.pdf
    try:
        access = ha.open_history(history_db_path, output_root, environ=environ,
                                 allow_onedrive_output=allow_onedrive_output)
    except Exception:
        raise fail from None
    try:
        if access.reader is None:
            raise fail
        try:
            listed = {info.run_id: info for info in access.reader.list_runs(ctx.client_key)}
            if ctx.run_id not in listed:
                raise fail
            events = access.reader.events(ctx.run_id)
        except _Stop:
            raise
        except Exception:
            raise fail from None
        if not any(e.kind == "rendered" and e.detail == st.mask_event_detail(_rendered_detail(sha)) for e in events):
            raise fail
        found, matched = ha.FinalizedOnlyReader._check_pdf(reports, sha, size)
        if not (found and matched):
            raise fail
        path = reviewer_dir / layout.reviewer_record_name(ctx.run_id, sha)
        try:
            info = ops.lstat(path)
        except OSError:
            raise fail from None
        kind, record = rw._read_existing(path, info, ops)
        if kind != "ok" or rw.record_identity(record) != identity:
            raise fail
    finally:
        access.close()


# --- the call -------------------------------------------------------------------------------------------------------------------------

def finalize_report(*, bundle, draft_path, output_root, history_db_path, finalized_at, replace_existing: bool = False,
                    history_insights=None, recommendation_annotations=None,
                    history_config: HistoryConfig | None = None, environ=None, allow_onedrive_output: bool = False,
                    allow_cloud_sync: bool = False, attempted_at: str | None = None,
                    require_attempt_directory: bool = False, ops: ff.FileOps | None = None,
                    attempt_ops: ff.FileOps | None = None, store_factory=None,
                    render_input: ri.ClientRenderInput | None = None) -> FinalizationResult:
    """Finalize one report. See the module docstring for the sequence and the recovery rules.

    bundle                    the assembled `report_run.ReportBundle` (not changed, not rebuilt)
    draft_path                must be `report.unarchived.pdf` in the period's `reports/` folder
    output_root               the approved output root; the period folders `reports/`, `reviewer/` (and `attempts/`,
                              for attempt records) must already exist
    history_db_path           the history database (created by the snapshot store if it does not exist yet)
    finalized_at              the `finalized_at` of the reviewer record (UTC ISO timestamp, supplied by the caller)
    replace_existing          True to replace a `report.pdf` with different content (the old one is kept)
    history_insights /
    recommendation_annotations  the composed history objects (6c-5 / 6c-6), passed to the reviewer-record builder
    history_config            the `HistoryConfig` used at assembly (default `HistoryConfig()`)
    attempted_at              timestamp for attempt records (default: `finalized_at`)
    require_attempt_directory stop in preflight when the period's `attempts/` folder is missing
    ops / attempt_ops         file-operation replacements for fault injection
    store_factory             replaces `SnapshotStore` (called as factory(path, allow_cloud_sync=..., environ=...)); a
                              test hook, e.g. to fix the clock and run IDs or to inject faults
    render_input              None (default): the version 1 path, unchanged (six-value identity, no sidecar needed).
                              A `ClientRenderInput`: the version 2 path. Before the first change the call checks
                              that the input matches the bundle, that the historical statements it draws are the
                              ones the record will list, and that its sidecar `report.render-input.<h16>.json` is
                              valid, carries exactly this input's hash and names the PDF being finalized. Any
                              failure stops with a `render_*` category and changes nothing; it never falls back to
                              version 1. The record is then a schema version 2 record and its nine-value identity is
                              verified at the end. An input rebuilt from a sidecar (no operational metadata) is
                              accepted: its snapshot identity is restored from the bundle without changing its hash.

    Returns a FinalizationResult for every outcome. TypeError for arguments of the wrong type.
    """
    if not isinstance(bundle, rr.ReportBundle):
        raise TypeError(f"bundle must be a ReportBundle, got {type(bundle).__name__}")
    if not isinstance(replace_existing, bool):
        raise TypeError("replace_existing must be True or False")
    if render_input is not None and not isinstance(render_input, ri.ClientRenderInput):
        raise TypeError(f"render_input must be a ClientRenderInput or None, got {type(render_input).__name__}")
    ops = ops or ff.FileOps()
    ctx = _Context()
    ctx.client_key = bundle.snapshot.client_key
    ctx.period = (bundle.snapshot.period_start, bundle.snapshot.period_end)
    ctx.attempted_at = attempted_at or finalized_at
    ctx.root = output_root
    try:
        return _run(bundle, ctx, draft_path, output_root, history_db_path, finalized_at, replace_existing,
                    history_insights, recommendation_annotations, history_config or HistoryConfig(), environ,
                    allow_onedrive_output, allow_cloud_sync, require_attempt_directory, ops, store_factory,
                    attempt_ops or ops, render_input)
    except _Stop as stop:
        return _failure(ctx, stop, attempt_ops or ops)
    except Exception as err:  # nothing may escape with a message, path or traceback
        code = _STEP_CODE.get(ctx.step)
        return _failure(ctx, _Stop(OUTCOME_FAILED, ctx.step if code else "preflight", code, "unexpected_error",
                                   exception=err), attempt_ops or ops)


def _run(bundle, ctx, draft_path, output_root, history_db_path, finalized_at, replace_existing, history_insights,
         annotations, history_config, environ, allow_onedrive_output, allow_cloud_sync, require_attempt_directory,
         ops, store_factory, attempt_ops, render_input=None) -> FinalizationResult:
    # 1. preflight ------------------------------------------------------------------------------------------------
    _check_bundle(bundle)
    try:
        root = layout.resolve_output_root(output_root, environ=environ, db_path=history_db_path,
                                          allow_onedrive_output=allow_onedrive_output)
        reports = layout.report_dir(root, ctx.client_key, *ctx.period)
        reviewer_dir = layout.reviewer_dir(root, ctx.client_key, *ctx.period)
        attempts = layout.attempt_dir(root, ctx.client_key, *ctx.period)
    except layout.OutputLayoutError:
        raise _Stop(OUTCOME_REJECTED, "preflight", "output_unavailable", "output_unavailable") from None
    ctx.root = root
    draft = reports / layout.DRAFT_PDF_NAME
    if os.path.normcase(os.path.abspath(os.fspath(draft_path))) != os.path.normcase(os.path.abspath(draft)):
        raise _Stop(OUTCOME_REJECTED, "verify_draft", "draft_invalid", "draft_invalid")
    if not (_is_real_dir(reports, ops) and _is_real_dir(reviewer_dir, ops)):
        raise _Stop(OUTCOME_REJECTED, "preflight", "output_unavailable", "output_unavailable")
    if require_attempt_directory and not _is_real_dir(attempts, ops):
        raise _Stop(OUTCOME_REJECTED, "preflight", "output_unavailable", "output_unavailable")
    if _exists(reports / layout.DRAFT_PARTIAL_NAME, ops):  # the renderer did not finish: never adopted or removed
        raise _Stop(OUTCOME_CONFLICT, "preflight", "draft_partial_conflict", "draft_partial_conflict")

    _check_stale(bundle, ctx, history_db_path=history_db_path, output_root=output_root, environ=environ,
                 allow_onedrive_output=allow_onedrive_output, history_config=history_config)

    # 2. reconciliation (read-only) ------------------------------------------------------------------------------
    latest, events = _read_history_state(bundle, ctx, history_db_path, environ)
    source, check = _validate_pdf_source(ctx, reports, draft, latest, events, ops)
    ctx.pdf = (check.sha256, check.byte_count)
    try:
        statements = None
        if history_insights is not None and not bundle.client_content.historical_statements:
            statements = tuple(history_insights.client_statements)
        client_hash = client_content_sha256(bundle.client_content, historical_statements=statements)
    except (ClientContentHashError, AttributeError, TypeError):
        raise _Stop(OUTCOME_REJECTED, "preflight", None, "bundle_inconsistent") from None
    render = None
    if render_input is not None:  # version 2: every render identity check completes before the first change
        render_input, render = _check_render_identity(render_input, bundle, history_insights, reports, ctx, ops)
    _preview_promotion(source, reports, ctx.pdf, replace_existing, ops)
    _preview_record(reviewer_dir, latest,
                    {"pdf_sha256": ctx.pdf[0], "pdf_bytes": ctx.pdf[1], "client_content_sha256": client_hash,
                     "render": render}, ops)

    # 3. dry run of the reviewer record --------------------------------------------------------------------------
    try:
        build_reviewer_record(
            bundle=bundle,
            receipt=_receipt(ctx, bundle, check, client_hash, finalized_at, _PLACEHOLDER_RUN_ID, 1, render),
            history_insights=history_insights, recommendation_annotations=annotations, history_config=history_config)
    except (ReviewerRecordError, rs.RecordSchemaError, TypeError, ValueError):
        raise _Stop(OUTCOME_REJECTED, "preflight", "record_failed", "record_preflight_failed") from None

    # 4 and 5. snapshot and rendered event -----------------------------------------------------------------------
    _persist(bundle, ctx, ctx.pdf, history_db_path, environ, allow_cloud_sync, store_factory)

    # 6. the PDF --------------------------------------------------------------------------------------------------
    _promote(ctx, source, reports, replace_existing, ops)

    # 7. the reviewer record -------------------------------------------------------------------------------------
    try:
        record = build_reviewer_record(
            bundle=bundle,
            receipt=_receipt(ctx, bundle, check, client_hash, finalized_at, ctx.run_id, ctx.revision, render),
            history_insights=history_insights, recommendation_annotations=annotations, history_config=history_config)
    except (ReviewerRecordError, rs.RecordSchemaError, TypeError, ValueError):
        raise _Stop(OUTCOME_FAILED, "record", "record_failed", "record_write_failed") from None
    _write_record(ctx, record, reviewer_dir, ops)

    # 8. the finalized-only reader must agree --------------------------------------------------------------------
    identity = _identity(ctx.run_id, ctx.revision, bundle.snapshot, ctx.pdf, client_hash, render)
    _verify_final_state(ctx, bundle, identity, reports, reviewer_dir, history_db_path, output_root, environ,
                        allow_onedrive_output, ops)

    # 9. success -------------------------------------------------------------------------------------------------
    nothing_new = (ctx.snapshot_created is False and ctx.event_recorded is False
                   and ctx.pdf_outcome in ("already_promoted", rp.OUTCOME_IDENTICAL)
                   and ctx.record_outcome == rw.OUTCOME_ALREADY)
    return FinalizationResult(
        outcome=OUTCOME_ALREADY if nothing_new else OUTCOME_FINALIZED, finalized=True, step=None, category=None,
        status_code=None, **_facts(ctx))


def _facts(ctx) -> dict:
    return dict(
        run_id=ctx.run_id, revision=ctx.revision, snapshot_created=ctx.snapshot_created,
        event_recorded=ctx.event_recorded, pdf_outcome=ctx.pdf_outcome, final_name=ctx.final_name,
        pdf_sha256=None if ctx.pdf is None else ctx.pdf[0], pdf_bytes=None if ctx.pdf is None else ctx.pdf[1],
        preserved_report=None if ctx.preserved is None else ctx.preserved.name,
        duplicate_draft=None if ctx.duplicate is None else ctx.duplicate.name,
        record_name=ctx.record_name, record_outcome=ctx.record_outcome)


# --- failures and attempt records ---------------------------------------------------------------------------------------------------

def _failure(ctx, stop: _Stop, attempt_ops) -> FinalizationResult:
    attempt = _log_attempt(ctx, stop, attempt_ops)
    incomplete = ctx.mutated and stop.outcome != OUTCOME_STALE
    return FinalizationResult(
        outcome=stop.outcome, finalized=False, step=stop.step, category=stop.category, status_code=stop.code,
        attempt_log=attempt, incomplete=incomplete, draft_problems=stop.draft_problems, **_facts(ctx))


def _log_attempt(ctx, stop: _Stop, attempt_ops) -> str:
    """Best effort: any problem here is reported as `attempt_log` and never replaces the original result."""
    if stop.code is None:
        return ATTEMPT_NOT_RECORDED
    try:
        if ctx.root is None or not _is_real_dir(layout.attempt_dir(ctx.root, ctx.client_key, *ctx.period),
                                                 attempt_ops):
            return ATTEMPT_NO_DIRECTORY
        preserved = None
        if ctx.preserved is not None:
            preserved = {"sha256_16": ctx.preserved.sha256[:layout.HASH16_CHARS], "variant_index": ctx.preserved.number}
        record = al.build_attempt_record(
            client_key=ctx.client_key, report_period=ctx.period, attempted_at=ctx.attempted_at, step=stop.step,
            status_code=stop.code,
            exception_class=None if stop.exception is None else al.safe_exception_class(stop.exception),
            run_id=ctx.run_id, pdf_sha256=None if ctx.pdf is None else ctx.pdf[0],
            pdf_bytes=None if ctx.pdf is None else ctx.pdf[1],
            previous_report_restored=False if ctx.old_report_preserved else None,
            previous_report_preserved_as=preserved)
        al.write_attempt_record(record, output_root=ctx.root, ops=attempt_ops)
    except Exception:
        return ATTEMPT_FAILED
    return ATTEMPT_WRITTEN
