"""The resumable production pipeline for one client and period (R8.3): `run_client`.

A run walks the state machine of `contracts` (docs/r8/execution_contract.md), recording every transition in the ledger:

  requested -> collecting -> rendering -> rendered -> finalizing -> finalized -> auditing -> audited

and stops there: publishing, delivery and approval belong to later stages, so `audited` is the last state R8.3 reaches
(`ready_for_delivery` can never be reached without a passing audit, a publisher and a manifest).

What `run_client` does, in order
  1. validates the request against the loaded client and the explicit period of the workbook (no ledger write when the
     request is invalid beyond recording it as rejected);
  2. registers the request by idempotency key (a stored response is replayed);
  3. applies the admission rules for the existing run (resume, return the stored result, or reject);
  4. takes the runner-wide lock (unless the batch runner holds it) and the client's lock, after recovering runs that a
     dead process left in an in-progress state;
  5. runs the stages. Every stage first looks at what exists, so a resume never repeats finished work:
       * collection/assembly are read-only and are repeated when the engine needs the in-memory bundle (the bundle is not
         persisted); the data may have changed since the first attempt, in which case the engine's own identity checks
         stop the run as `finalization_conflict` instead of finalizing something different;
       * rendering reuses the open sidecar, so the original render hash and generation date survive;
       * finalization reuses the stored `finalized_at` and the render input of the sidecar;
       * the audit gate is read-only.
  6. returns a `contracts.RunResponse`.

Failure handling: expected operational failures are `StageError`s (fixed category and code) and end the run as `failed`
or `blocked` with a retry classification; `run_client` returns the response and does not raise. Unexpected programming
errors are logged with their traceback (`logging`, logger `seo_report.pipeline`) and returned as `internal_error` with the
exception class in the code. `KeyboardInterrupt` and `SystemExit` are never caught.

`dry_run_client` performs collection, assembly and rendering only, writing exclusively to an isolated scratch folder. It
never touches the ledger, the history database, the production output root or any report storage.
"""

import contextlib
import contextvars
import dataclasses
import datetime as dt
import errno
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from seo_report import audit_service
from seo_report import contracts
from seo_report import finalize_files as ff
from seo_report import finalize_service
from seo_report import ledger as ledger_module
from seo_report import output_layout as layout
from seo_report import render_input_hash as rh
from seo_report import render_service
from seo_report import render_state as rstate
from seo_report import report_service
from seo_report.contracts import ERROR_CATEGORIES, RunRequest, RunResponse, StageError
from seo_report.snapshots.baselines import HistoryConfig

LOGGER = logging.getLogger("seo_report.pipeline")

# The identifiers of the run being processed (client, period, run, request, stage). Every log record written while it is set
# carries them (see `runner.JsonFormatter`), including records from the ledger, authentication and rendering code.
LOG_CONTEXT = contextvars.ContextVar("seo_report_log_context", default=None)


@contextlib.contextmanager
def log_context(**fields):
    token = LOG_CONTEXT.set({**(LOG_CONTEXT.get() or {}), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        LOG_CONTEXT.reset(token)  # also on KeyboardInterrupt and SystemExit


def update_log_context(**fields) -> None:
    LOG_CONTEXT.set({**(LOG_CONTEXT.get() or {}), **{k: v for k, v in fields.items() if v is not None}})


def _log(level, message, *args, **kwargs) -> None:
    """Log without ever letting a failing handler interrupt the run, its recording or its cleanup. (`Exception` only:
    KeyboardInterrupt and SystemExit still propagate.)"""
    try:
        LOGGER.log(level, message, *args, **kwargs)
    except Exception:
        pass
_SUCCESS_OUTCOME_STATES = ("audited", "publishing", "ready_for_delivery", "delivered")
_RESUMABLE_STATES = ("requested", "collecting", "rendering", "rendered", "finalizing", "finalized", "auditing")


@dataclass(frozen=True)
class Services:
    """Everything `run_client` needs besides the ledger. Collaborators are injected so tests use offline fakes."""

    settings: object  # runner_config.RunnerSettings
    registry: object  # client_config.ClientRegistry
    clients: object  # client_loader.LoadResult
    clock: Callable[[], dt.datetime]  # aware UTC
    collect: Callable  # (ClientRun, today: date) -> source_collector.SourceResult
    renderer: Callable | None = None  # replaces pdf_renderer.render_report_pdf (tests)
    environ: dict | None = None
    file_ops: object | None = None
    store_factory: Callable | None = None
    history_config: HistoryConfig | None = None
    global_lock_held: bool = False
    sleep: Callable = time.sleep
    new_id: Callable[[], str] = lambda: str(uuid.uuid4())


# --- responses --------------------------------------------------------------------------------------------------------------

def _outcome_of(state: str) -> str:
    if state in _SUCCESS_OUTCOME_STATES:
        return "succeeded"
    return {"failed": "failed", "blocked": "blocked", "cancelled": "rejected"}.get(state, "in_progress")


def build_response(request: RunRequest, run, *, mode="production", outcome=None, error=None, retry=None,
                   replayed=False, warnings=(), updated_at: str, state=None, attempt=None,
                   operator_action=None) -> RunResponse:
    identity = audit = None
    if run is not None and run.render_input_sha256:
        identity = {"render_input_sha256": run.render_input_sha256,
                    "render_input_format": {"name": run.render_format_name, "version": run.render_format_version},
                    "generation_date": run.generation_date, "pdf_sha256": run.pdf_sha256,
                    "pdf16": run.pdf_sha256[:16] if run.pdf_sha256 else None, "pdf_bytes": run.pdf_bytes,
                    "record_name": run.record_name}
    if run is not None and run.audit_outcome:
        audit = {"outcome": run.audit_outcome, "finalized_valid": run.audit_finalized_valid,
                 "verification_complete": bool(run.audit_verification_complete)}
    if error is None and run is not None and run.state in ("failed", "blocked") and run.error_category:
        error = contracts.error_info(run.error_category, run.error_code, run.failed_stage or "intake")
        retry = contracts.retry_info(run.error_category, after=run.retry_after) if run.retry_eligible else \
            contracts.retry_info(None)
    state = state or (run.state if run is not None else "requested")
    return RunResponse(
        request_id=request.request_id, idempotency_key=request.idempotency_key, mode=mode,
        run_key=request.run_key if run is None else run.run_key,
        client_key=request.client_key, period_id=request.period_id, state=state,
        outcome=outcome or _outcome_of(state), attempt=attempt or (run.attempt_count if run is not None else 1),
        timestamps={"requested_at": request.requested_at, "updated_at": updated_at,
                    "finished_at": None if run is None else run.finished_at},
        replayed=replayed, run_id=None if run is None else run.engine_run_id,
        revision=None if run is None else run.engine_revision, identity=identity, audit=audit, delivery=None,
        error=error, retry=retry or contracts.retry_info(None), warnings=tuple(warnings),
        generation=1 if run is None else run.generation, supersedes=None if run is None else run.supersedes_run_key,
        operator_action=operator_action)


def response_from_dict(data: dict, *, replayed: bool = False) -> RunResponse:
    contracts.validate_response(data)
    fields = {f.name for f in dataclasses.fields(RunResponse)}
    values = {k: v for k, v in data.items() if k in fields}
    values["replayed"] = replayed
    values["warnings"] = tuple(values.get("warnings", ()))
    return RunResponse(**values)


def _rejection(request, services, code, *, category="validation_failed", run=None, state=None) -> RunResponse:
    return build_response(request, run, outcome="rejected", error=contracts.error_info(category, code, "intake"),
                          updated_at=contracts.timestamp_of(services.clock()), state=state)


def _period_id(period) -> str:
    if isinstance(period, str):
        return period
    if isinstance(period, tuple) and len(period) == 2:
        return contracts.period_id_of(*period)
    raise TypeError("period must be a period_id text or a (start, end) pair")


def _unexpected(error: BaseException) -> tuple[str, str]:
    if isinstance(error, OSError) and getattr(error, "errno", None) == errno.ENOSPC:
        return "internal_error", "disk_full"
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(error).__name__).lower()
    return "internal_error", ("exception_" + re.sub(r"[^a-z0-9_]", "", name))[:64]


# --- the public entry points --------------------------------------------------------------------------------------------------

def run_client(client_key: str, period, *, services: Services, ledger, request: RunRequest | None = None,
               mode: str = "production", scratch_dir=None) -> RunResponse:
    """Run (or resume) one client's report for `period` (a `period_id` text or a (start, end) pair). See the module
    docstring. `mode="dry_run"` delegates to `dry_run_client` and needs `scratch_dir`."""
    period_id = _period_id(period)
    if mode == "dry_run":
        return dry_run_client(client_key, period_id, services=services, scratch_dir=scratch_dir)
    if mode != "production":
        raise ValueError("mode must be 'production' or 'dry_run'")
    if request is None:
        request_id = services.new_id()
        request = RunRequest(request_id=request_id, idempotency_key=f"manual:{client_key}/{period_id}:{request_id}",
                             client_key=client_key, period_id=period_id, action="run",
                             requested_at=contracts.timestamp_of(services.clock()), requested_by="cli:manual")
    elif (request.client_key, request.period_id) != (client_key, period_id):
        raise ValueError("the request does not match the client and period")
    with log_context(client_key=client_key, period_id=period_id, request_id=request.request_id,
                     run_key=contracts.run_key_of(client_key, period_id)):
        return _run_client_logged(request, services, ledger)


def _run_client_logged(request: RunRequest, services: Services, ledger) -> RunResponse:
    try:
        return _production(request, services, ledger)
    except (KeyboardInterrupt, SystemExit):
        raise
    except ledger_module.LedgerError as error:
        # A conflict that concerns only this client's run (stale state, a lost lock, a transient busy ledger) is a
        # structured answer for this client. A database that cannot be used, and logical ledger errors, still raise: the
        # runner decides what they mean for the batch.
        response = _ledger_conflict_response(error, request, services, ledger)
        if response is None:
            raise
        return response
    except Exception as error:
        category, code = _unexpected(error)
        _log(logging.ERROR, "unexpected error in run_client", exc_info=True,
             extra={"fields": {"error_category": category, "error_code": code}})  # the traceback stays in the log only
        return _record_unexpected(request, services, ledger, category, code, error)


def _ledger_conflict_response(error, request, services, ledger) -> RunResponse | None:
    """An unrecorded `rejected` response for a client-level ledger conflict, or None when the error must propagate.

    Nothing is written: another owner may hold or have moved the run, or the ledger was only busy. The request stays
    `accepted` without a response, so the same idempotency key is simply tried again on the next poll, and the run (left
    in whatever state it had) is picked up by recovery or the next attempt. The run is never continued after this.
    """
    if isinstance(error, (ledger_module.StaleStateError, ledger_module.LockHeldError)):
        category, code = "run_in_progress", ("stale_state" if isinstance(error, ledger_module.StaleStateError)
                                             else error.code)
    elif error.transient:
        category, code = "lock_unavailable", error.code  # ledger_busy
    else:
        return None
    run = None
    try:
        run = ledger.latest_run(request.client_key, request.period_id)
    except ledger_module.LedgerError:
        pass  # the answer does not depend on reading the run
    stage = contracts.STAGE_OF_STATE.get(run.state, "intake") if run is not None else "intake"
    return build_response(request, run, outcome="rejected", updated_at=contracts.timestamp_of(services.clock()),
                          error=contracts.error_info(category, code, stage))


def ledger_failure_response(request, services, error) -> RunResponse:
    """A `failed` response for a ledger error that is not a database outage (for example a contract violation caught by
    the ledger). Used by the runner so that one client's logical ledger error does not stop the other clients."""
    code = re.sub(r"[^a-z0-9_]", "", str(error.code).lower())[:64] or "ledger_error"
    return build_response(request, None, outcome="failed", state="failed",
                          updated_at=contracts.timestamp_of(services.clock()),
                          error=contracts.error_info("internal_error", code, "intake"))


def _record_unexpected(request, services, ledger, category, code, original=None) -> RunResponse:
    now = contracts.timestamp_of(services.clock())
    try:
        run = ledger.latest_run(request.client_key, request.period_id)
        if run is not None and run.state in contracts.IN_PROGRESS_STATES:
            run = _fail_run(ledger, services, request, run, category, code)
            return build_response(request, run, updated_at=now)
    except ledger_module.LedgerError as recording:
        _log(logging.WARNING, "the failure could not be recorded in the ledger: %s", recording.code)
        if recording.storage and not recording.transient:
            if original is not None:
                recording.add_note(f"original failure: {category}/{code}")
            raise recording from original  # the storage is unusable: stop the batch, keep the original as the cause
    return build_response(request, None, outcome="failed", state="failed", updated_at=now,
                          error=contracts.error_info(category, code, "intake"))


def _finish(ledger, request, response: RunResponse, run_key=None) -> RunResponse:
    data = response.to_dict()
    contracts.validate_response(data)
    status = "rejected" if response.outcome == "rejected" else "completed"
    try:
        ledger.finish_request(request.request_id, status=status, response=data, run_key=run_key,
                              reject_code=response.error["code"] if response.outcome == "rejected" and response.error
                              else None)
    except ledger_module.LedgerError as error:
        if error.storage and not error.transient:
            raise
        # The run itself is already recorded. The request stays `accepted`; a repeat of the same key is admitted again
        # and answered from the run (for a finished run: `already_complete`).
        _log(logging.WARNING, "the response could not be stored in the ledger: %s", error.code)
    return response


def _production(request: RunRequest, services: Services, ledger) -> RunResponse:
    settings = services.settings
    if request.action == "dry_run":
        return _rejection(request, services, "action_not_supported", category="validation_failed")
    intake = ledger.register_request(request)
    if intake.kind == "replay" and intake.status in ("completed", "rejected") and intake.response is not None:
        return response_from_dict(intake.response, replayed=True)

    # 1. validation against the loaded workbook (no run is created for an invalid request)
    client_run = services.clients.clients.get(request.client_key)
    if client_run is None:
        rejected = next((r for r in services.clients.rejected if r.client_key == request.client_key), None)
        if rejected is not None:
            response = _rejection(request, services, rejected.code, category="config_invalid")
        else:
            response = _rejection(request, services, "client_unknown", category="config_invalid")
        return _finish(ledger, request, response)
    if client_run.period_id != request.period_id:
        return _finish(ledger, request, _rejection(request, services, "period_differs_from_workbook"))
    if request.action == "validate":
        response = build_response(request, None, mode="read_only", outcome="succeeded",
                                  updated_at=contracts.timestamp_of(services.clock()))
        return _finish(ledger, request, response)
    if request.action == "status":
        run = ledger.latest_run(request.client_key, request.period_id)
        if run is None:
            return _finish(ledger, request, _rejection(request, services, "no_run"))
        return _finish(ledger, request, build_response(request, run, mode="read_only", updated_at=run.updated_at))
    if request.action == "publish":
        return _finish(ledger, request, _rejection(request, services, "publish_not_supported"))

    # 2. locks, recovery, admission
    held = []
    stack = contextlib.ExitStack()
    try:
        try:
            if not services.global_lock_held:
                ledger.acquire_lock("runner:global", ttl_seconds=settings.lock_ttl_seconds)
                held.append("runner:global")
            ledger.recover_interrupted()
            ledger.acquire_lock(f"client:{request.client_key}", ttl_seconds=settings.lock_ttl_seconds)
            held.append(f"client:{request.client_key}")
        except ledger_module.LockHeldError:
            # transient: the request stays 'accepted' without a response, so the same key is simply tried again
            return build_response(request, ledger.latest_run(request.client_key, request.period_id), outcome="rejected",
                                  updated_at=contracts.timestamp_of(services.clock()),
                                  error=contracts.error_info("run_in_progress", "lock_held", "intake"))
        # leases are renewed in the background for the whole run (a stage can outlast a lease); the runner's own lock too
        # when it holds it for the batch. The keeper is stopped before the locks are released (the `finally` below).
        owned = list(held)
        if services.global_lock_held:
            batch = ledger.get_lock("runner:global")
            if batch is not None and batch.owner_id == ledger.owner_id:
                owned.append("runner:global")
        lease = stack.enter_context(ledger.keep_alive(
            owned, ttl_seconds=settings.lock_ttl_seconds, interval_seconds=settings.heartbeat_interval_seconds))
        run = ledger.latest_run(request.client_key, request.period_id)
        if request.action in contracts.OPERATOR_ACTIONS:
            return _operator(request, services, ledger, client_run, run, intake.request_id, lease=lease)
        admission = _admit(request, run)
        if admission[0] == "reject":
            return _finish(ledger, request, _rejection(request, services, admission[1], run=run,
                                                       state=None if run is None else run.state))
        if admission[0] == "return":
            return _finish(ledger, request, build_response(
                request, run, outcome="already_complete", updated_at=run.updated_at), run_key=run.run_key)
        execution = _Execution(request, client_run, services, ledger)
        execution.lease = lease
        if admission[0] == "create":
            run = ledger.create_run(request.client_key, request.period_id, replace_existing=request.replace_existing,
                                    request_id=request.request_id)
        elif admission[0] == "retry":
            reason = _unresolved(services, run, client_run) if run.state == "blocked" else None
            if reason is not None:
                return _finish(ledger, request, _rejection(request, services, "blocking_condition_unresolved",
                                                           run=run, state=run.state), run_key=run.run_key)
            values = {"replace_existing": 1} if run.state == "blocked" and request.replace_existing else None
            if run.state == "blocked" and run.error_code == "existing_report_differs" and not (
                    run.replace_existing or request.replace_existing):
                return _finish(ledger, request, _rejection(request, services, "replace_existing_required", run=run,
                                                           state=run.state), run_key=run.run_key)
            run = ledger.transition(run.run_key, contracts.RESUME_STATE[run.failed_stage], expected_state=run.state,
                                    expected_version=run.state_version,
                                    code="retry_after_acknowledgement" if run.state == "blocked" else "retry_admitted",
                                    request_id=request.request_id, new_attempt=True, values=values)
        execution.run = run
        response = execution.execute()
        return _finish(ledger, request, response, run_key=run.run_key)
    finally:
        stack.close()  # the renewing thread first, then the locks it renewed
        for name in reversed(held):
            try:
                ledger.release_lock(name)
            except ledger_module.LedgerError:
                _log(logging.WARNING, "lock %s could not be released", name.split(":")[0])


def _admit(request: RunRequest, run):
    """(action, code): create | resume | retry | return | reject (see docs/r8/execution_contract.md §2.1)."""
    if run is None:
        return ("create", None) if request.action == "run" else ("reject", "no_run")
    state = run.state
    if state == "cancelled":
        return "reject", "cancelled"
    if state in _RESUMABLE_STATES:
        return ("resume", None) if request.action == "run" else ("reject", "not_failed")
    if state in ("audited", "publishing", "ready_for_delivery", "delivered"):
        return ("return", None) if request.action == "run" else ("reject", "not_failed")
    if state == "blocked":
        # only an operator's acknowledgement lets a retry be considered, and the retry re-checks the blocking condition
        if run.acknowledged and request.action == "retry":
            return ("retry", None) if _recoverable(run) else ("reject", "condition_not_recoverable")
        return "reject", "operator_required"
    # failed
    if not run.retry_eligible:
        return "reject", "operator_required"
    return ("retry", None) if request.action == "retry" else ("reject", "use_retry")


# --- operator actions (R8.4.2) ------------------------------------------------------------------------------------------------
#
# Acknowledge, cancel and supersede are decisions of a person. Each carries an operator label and a reason code (recorded
# in the append-only event history), runs under the client lock, is idempotent by idempotency key, and never deletes or
# edits a report, snapshot, sidecar, record or ledger row. There is deliberately no "force" action.

_BLOCKED_CATEGORIES = ("layout_issues", "finalization_conflict", "audit_failed")
# Blocking codes that a retry cannot resolve: the engine refuses the same content or a conflicting record for good.
_NOT_RECOVERABLE = frozenset({
    "render_input_already_finalized", "record_identity_differs", "existing_record_invalid", "existing_record_not_regular",
    "reader_record_limit", "superseded_only_copy", "existing_report_not_regular"})
_SETTLED_STATES = ("audited", "ready_for_delivery", "delivered")
_NOT_CANCELLABLE_STATES = ("finalized", "auditing", "audited", "publishing", "ready_for_delivery", "delivered")


def _recoverable(run) -> bool:
    return (run.error_category in _BLOCKED_CATEGORIES and run.error_code not in _NOT_RECOVERABLE
            and run.failed_stage in contracts.RESUME_STATE)


def _folders(settings, run):
    start, end = dt.date.fromisoformat(run.period_start), dt.date.fromisoformat(run.period_end)
    root = settings.output_root
    return (layout.report_dir(root, run.client_key, start, end), layout.reviewer_dir(root, run.client_key, start, end),
            layout.attempt_dir(root, run.client_key, start, end))


def _has_partial_files(settings, run) -> bool:
    """True when an unfinished (partial) file is present. Partial files are never deleted by the system; an operator
    moves them away, and until then they block recovery. A folder that cannot be listed is not shown to be clean."""
    for folder in _folders(settings, run):
        try:
            names = os.listdir(folder)
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if any(ff.is_partial_name(name) or name.endswith(".partial") for name in names):
            return True
    return False


def _audit_of(services, run):
    return audit_service.verify_finalization(
        settings=services.settings, client_key=run.client_key, period_start=run.period_start, period_end=run.period_end,
        run_id=run.engine_run_id, pdf_sha256=run.pdf_sha256, pdf_bytes=run.pdf_bytes, record_name=run.record_name,
        render_input_sha256=run.render_input_sha256, environ=services.environ, ops=services.file_ops)


def _unresolved(services, run, client_run=None) -> str | None:
    """A fixed code naming the blocking condition that still exists, or None when it is resolved."""
    if _has_partial_files(services.settings, run):
        return "partial_file_present"
    if run.error_category == "audit_failed":
        if run.engine_run_id is None:
            return "audit_not_possible"
        verdict = _audit_of(services, run)
        if not verdict.ok:
            return verdict.reasons[0][:64]
    return None


def _operator(request, services, ledger, client_run, run, intake_request_id, lease=None) -> RunResponse:
    if not request.operator or request.reason not in contracts.REASON_CODES:  # who decided, and why, is mandatory
        return _reject(request, services, ledger, "operator_details_required", None)
    handler = {"acknowledge": _acknowledge, "cancel": _cancel, "supersede": _supersede}[request.action]
    if handler is _supersede:
        return handler(request, services, ledger, client_run, run, intake_request_id, lease)
    return handler(request, services, ledger, client_run, run, intake_request_id)


def _info(request) -> dict:
    return {"action": request.action, "operator": request.operator, "reason": request.reason}


def _reject(request, services, ledger, code, run, *, event=None) -> RunResponse:
    if event is not None and run is not None:
        ledger.append_event(kind="operator", code=event, run_key=run.run_key, request_id=request.request_id,
                            detail={"operator": request.operator, "reason": request.reason, "refused": code})
    return _finish(ledger, request, _rejection(request, services, code, run=run, state=None if run is None else run.state),
                   run_key=None if run is None else run.run_key)


def _acknowledge(request, services, ledger, client_run, run, intake_request_id) -> RunResponse:
    if run is None:
        return _reject(request, services, ledger, "no_run", None)
    if run.state != "blocked":
        return _reject(request, services, ledger, "not_blocked", run, event="acknowledge_refused")
    if not run.acknowledged:
        if not _recoverable(run):
            return _reject(request, services, ledger, "condition_not_recoverable", run, event="acknowledge_refused")
        if _unresolved(services, run, client_run) is not None:
            return _reject(request, services, ledger, "blocking_condition_unresolved", run, event="acknowledge_refused")
    updated, changed = ledger.acknowledge(run.run_key, expected_state=run.state, expected_version=run.state_version,
                                          operator=request.operator, reason=request.reason,
                                          request_id=request.request_id)
    response = build_response(request, updated, outcome="succeeded" if changed else "already_complete",
                              updated_at=updated.updated_at, operator_action=_info(request))
    return _finish(ledger, request, response, run_key=updated.run_key)


def _cancel_refusal(services, run) -> str | None:
    if run.state in contracts.IN_PROGRESS_STATES:
        return "run_in_progress"
    if run.state in _NOT_CANCELLABLE_STATES or run.engine_run_id is not None:
        return "not_cancellable"
    if run.render_input_sha256:  # the ledger may lag the engine after a crash: never cancel a run that is finalized
        try:
            if run.render_input_sha256 in rstate.finalized_render_hashes(_folders(services.settings, run)[1],
                                                                          require_complete=True):
                return "not_cancellable"
        except rstate.RenderStateError:
            return "not_cancellable"
    return None


def _cancel(request, services, ledger, client_run, run, intake_request_id) -> RunResponse:
    if run is None:
        return _reject(request, services, ledger, "no_run", None)
    if run.state == "cancelled":
        ledger.append_event(kind="operator", code="cancel_repeated", run_key=run.run_key, request_id=request.request_id,
                            detail={"operator": request.operator, "reason": request.reason})
        response = build_response(request, run, outcome="already_complete", updated_at=run.updated_at,
                                  operator_action=_info(request))
        return _finish(ledger, request, response, run_key=run.run_key)
    refusal = _cancel_refusal(services, run)
    if refusal is not None:
        return _reject(request, services, ledger, refusal, run, event="cancel_refused")
    updated = ledger.transition(
        run.run_key, "cancelled", expected_state=run.state, expected_version=run.state_version, code="run_cancelled",
        request_id=request.request_id, detail={"operator": request.operator, "reason": request.reason},
        event_kind="operator", outbox=lambda row: _status_payload(request, row, services))
    response = build_response(request, updated, outcome="succeeded", updated_at=updated.updated_at,
                              operator_action=_info(request))
    return _finish(ledger, request, response, run_key=updated.run_key)


def _sha256_of(path) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _original_state(settings, run) -> dict:
    """What must stay true of an audited run while a replacement is made: its reviewer record is byte-identical and its
    PDF still exists, either as `report.pdf` or (after a safe replacement) as `report.superseded.<hash16>.pdf`."""
    reports, reviewer, _ = _folders(settings, run)
    return {"record_name": run.record_name, "record_sha256": _sha256_of(reviewer / run.record_name),
            "pdf_sha256": run.pdf_sha256}


def _original_intact(settings, original, baseline) -> bool:
    if not baseline or baseline.get("record_sha256") is None:
        return False
    reports, reviewer, _ = _folders(settings, original)
    if _sha256_of(reviewer / baseline["record_name"]) != baseline["record_sha256"]:
        return False
    try:
        names = [n for n in os.listdir(reports) if n == layout.STANDARD_PDF_NAME or n.startswith("report.superseded.")]
    except OSError:
        return False
    return any(_sha256_of(reports / name) == baseline["pdf_sha256"] for name in names)


def _supersede(request, services, ledger, client_run, run, intake_request_id, lease=None) -> RunResponse:
    if run is None:
        return _reject(request, services, ledger, "no_run", None)
    if run.generation > 1 and ledger.attempts_for(run.run_key)[0].trigger_request == intake_request_id:
        # this very request created the replacement and was interrupted: continue that replacement, never a second one
        if run.state in _SETTLED_STATES:
            response = build_response(request, run, outcome="already_complete", updated_at=run.updated_at,
                                      operator_action=_info(request))
            return _finish(ledger, request, response, run_key=run.run_key)
        if run.state not in _RESUMABLE_STATES:
            return _reject(request, services, ledger, "replacement_pending", run)
        return _execute_new(request, services, ledger, client_run, run, lease)
    if run.state in _SETTLED_STATES:
        kind, replace = "supersede", True
        if not request.replace_existing:
            return _reject(request, services, ledger, "replace_existing_required", run, event="supersede_refused")
        if not _audit_of(services, run).ok:
            return _reject(request, services, ledger, "original_changed", run, event="supersede_refused")
    elif run.state == "cancelled":
        kind, replace = "restart", request.replace_existing
    elif run.generation > 1 and run.link_kind == "supersede":
        return _reject(request, services, ledger, "replacement_pending", run, event="supersede_refused")
    else:
        return _reject(request, services, ledger, "not_audited", run, event="supersede_refused")
    baseline = _original_state(services.settings, run) if kind == "supersede" else None
    new = ledger.create_run(run.client_key, request.period_id, replace_existing=replace, request_id=request.request_id,
                            supersedes=run.run_key, link_kind=kind, operator=request.operator, reason=request.reason,
                            baseline=baseline)
    return _execute_new(request, services, ledger, client_run, new, lease)


def _execute_new(request, services, ledger, client_run, run, lease=None) -> RunResponse:
    execution = _Execution(request, client_run, services, ledger)
    execution.lease = lease
    execution.run = run
    response = dataclasses.replace(execution.execute(), operator_action=_info(request))
    return _finish(ledger, request, response, run_key=run.run_key)


def _check_original(services, ledger, run) -> None:
    """A replacement is only audited while the run it replaces is still intact."""
    if run.link_kind != "supersede":
        return
    original = ledger.get_run(run.supersedes_run_key)
    created = next((e for e in ledger.events_for(run.run_key) if e.code == "run_supersede_created"), None)
    baseline = json.loads(created.detail_json).get("baseline") if created is not None else None
    if original is None or not _original_intact(services.settings, original, baseline):
        ledger.append_event(kind="error", code="original_changed", run_key=run.run_key,
                            detail={"original": run.supersedes_run_key})
        raise StageError("audit_failed", "original_changed")


# --- the stages ---------------------------------------------------------------------------------------------------------------

def _fail_run(ledger, services, request, run, category, code):
    eligible, kind, automatic, final_state = ERROR_CATEGORIES[category]
    settings = services.settings
    after = None
    if eligible and automatic and run.attempt_count <= settings.max_automatic_attempts:
        delays = settings.retry_delays_seconds
        seconds = delays[min(run.attempt_count - 1, len(delays) - 1)]
        after = contracts.timestamp_of(services.clock() + dt.timedelta(seconds=seconds))
    values = {"failed_stage": contracts.STAGE_OF_STATE[run.state], "error_category": category, "error_code": code,
              "retry_eligible": int(eligible), "retry_kind": kind, "retry_after": after}
    _log(logging.INFO, "run failed: %s %s", category, code,
         extra={"fields": {"error_category": category, "error_code": code}})
    return ledger.transition(run.run_key, final_state, expected_state=run.state, expected_version=run.state_version,
                             code="run_" + final_state, values=values, request_id=request.request_id,
                             outbox=lambda row: _status_payload(request, row, services))


def _status_payload(request, run, services) -> dict:
    data = build_response(request, run, updated_at=run.updated_at).to_dict()
    contracts.validate_response(data)
    return data


class _Execution:
    def __init__(self, request, client_run, services, ledger):
        self.request, self.client_run, self.services, self.ledger = request, client_run, services, ledger
        self.settings = services.settings
        self.run = None
        self.lease = None  # the background lease keeper of this run (None: renewed at stage boundaries only)
        self.warnings = []
        self.started = services.clock()

    # -- helpers -------------------------------------------------------------------------------------------------------------

    def _move(self, to_state, *, code, values=None, detail=None):
        if self.lease is not None:
            self.lease.check()  # a lost lease stops the run before its next recorded step
        self.run = self.ledger.transition(
            self.run.run_key, to_state, expected_state=self.run.state, expected_version=self.run.state_version,
            code=code, values=values, detail=detail, request_id=self.request.request_id,
            outbox=lambda row: _status_payload(self.request, row, self.services))
        update_log_context(stage=to_state, run_key=self.run.run_key, run_id=self.run.engine_run_id)
        return self.run

    def _guard(self):
        """Cooperative deadline and lock renewal at every stage boundary (a blocked call cannot be interrupted here)."""
        if (self.services.clock() - self.started).total_seconds() > self.settings.client_timeout_seconds:
            raise StageError("timeout", "client_deadline")
        if self.lease is not None:
            self.lease.check()  # lost lease: stop before any further production write
        ttl = self.settings.lock_ttl_seconds
        self.ledger.heartbeat(f"client:{self.request.client_key}", ttl_seconds=ttl)
        if not self.services.global_lock_held:
            self.ledger.heartbeat("runner:global", ttl_seconds=ttl)

    def _dirs(self):
        root, key = self.settings.output_root, self.request.client_key
        start, end = self.client_run.periods.current.start, self.client_run.periods.current.end
        folders = (layout.report_dir(root, key, start, end), layout.reviewer_dir(root, key, start, end),
                   layout.attempt_dir(root, key, start, end))
        for folder in folders:
            Path(folder).mkdir(parents=True, exist_ok=True)  # created, never emptied or removed
        return folders[0]

    def _response(self):
        return build_response(self.request, self.run, updated_at=self.run.updated_at, warnings=self.warnings)

    # -- the run -------------------------------------------------------------------------------------------------------------

    def execute(self) -> RunResponse:
        try:
            return self._execute()
        except StageError as error:
            return self._failed(error.category, error.code, error)
        except (KeyboardInterrupt, SystemExit):
            raise
        except ledger_module.LedgerError:
            raise  # a lost lock, stale state or a ledger problem: `run_client` decides; the run is not continued
        except Exception as error:
            category, code = _unexpected(error)
            _log(logging.ERROR, "unexpected error in a pipeline stage", exc_info=True,
                 extra={"fields": {"error_category": category, "error_code": code}})
            return self._failed(category, code, error)

    def _failed(self, category, code, original) -> RunResponse:
        """Record the failure and answer with it. If recording fails, the ORIGINAL failure is still what is reported (or,
        when the storage is unusable, the cause of the error that stops the batch)."""
        try:
            self.run = self.ledger.get_run(self.run.run_key)
            if self.run.state in contracts.IN_PROGRESS_STATES:
                self.run = _fail_run(self.ledger, self.services, self.request, self.run, category, code)
                return self._response()
        except ledger_module.LedgerError as recording:
            _log(logging.WARNING, "the failure could not be recorded in the ledger: %s", recording.code)
            if recording.storage and not recording.transient:
                recording.add_note(f"original failure: {category}/{code}")
                raise recording from original
            stage = contracts.STAGE_OF_STATE.get(self.run.state, "intake")
            return build_response(self.request, self.run, outcome="failed", updated_at=self.run.updated_at,
                                  warnings=self.warnings, error=contracts.error_info(category, code, stage),
                                  retry=contracts.retry_info(category))
        # a failure before any stage was entered (for example the output folders): reported, state unchanged
        return build_response(self.request, self.run, outcome="failed", updated_at=self.run.updated_at,
                              warnings=self.warnings, error=contracts.error_info(category, code, "intake"),
                              retry=contracts.retry_info(category))

    def _execute(self) -> RunResponse:
        services, ledger, run = self.services, self.ledger, self.run
        self._guard()
        reports_dir = self._dirs()
        assembled = rendered = None

        if run.state == "requested":
            today = services.clock().date()
            values = {"generation_date": today.isoformat()} if run.generation_date is None else None
            self._move("collecting", code="collection_started", values=values)
            assembled = self._collect_and_assemble()
            self._guard()
            self._move("rendering", code="rendering_started")
            rendered = render_service.render(
                assembled, self.client_run, settings=self.settings,
                generation_date=dt.date.fromisoformat(self.run.generation_date), attempt=self.run.attempt_count,
                renderer=services.renderer, ops=services.file_ops)
            drafted = rendered.drafted
            self._move("rendered", code="render_completed", values={
                "render_input_sha256": drafted.render_input_sha256, "render_format_name": rh.FORMAT_NAME,
                "render_format_version": rh.FORMAT_VERSION, "pdf_sha256": drafted.pdf_sha256,
                "pdf_bytes": drafted.pdf_bytes})
            _log(logging.INFO, "rendered")

        if self.run.state == "rendered":
            self._guard()
            values = {"finalized_at": contracts.timestamp_of(services.clock())} if self.run.finalized_at is None else None
            self._move("finalizing", code="finalization_started", values=values)
            if assembled is None:  # a resume: the bundle is rebuilt in memory, the render identity comes from the sidecar
                assembled = self._collect_and_assemble()
                rendered = self._rendered_from_sidecar(reports_dir)
            verdict = finalize_service.finalize(
                assembled, rendered, self.client_run, settings=self.settings, finalized_at=self.run.finalized_at,
                replace_existing=bool(self.run.replace_existing), environ=services.environ, ops=services.file_ops,
                store_factory=services.store_factory)
            self._move("finalized", code="finalization_completed", values={
                "engine_run_id": verdict.run_id, "engine_revision": verdict.revision, "pdf_sha256": verdict.pdf_sha256,
                "pdf_bytes": verdict.pdf_bytes, "record_name": verdict.record_name})

        if self.run.state == "finalized":
            self._guard()
            self._move("auditing", code="audit_started")
            current = self.run
            verdict = audit_service.verify_finalization(
                settings=self.settings, client_key=current.client_key, period_start=current.period_start,
                period_end=current.period_end, run_id=current.engine_run_id, pdf_sha256=current.pdf_sha256,
                pdf_bytes=current.pdf_bytes, record_name=current.record_name,
                render_input_sha256=current.render_input_sha256, environ=services.environ, ops=services.file_ops)
            if not verdict.ok:
                ledger.append_event(kind="error", code=verdict.reasons[0][:64], run_key=current.run_key,
                                    detail={"reasons": list(verdict.reasons)})
                raise StageError("audit_failed", verdict.reasons[0][:64])
            _check_original(services, ledger, current)
            self._move("audited", code="audit_passed", values={
                "audit_outcome": verdict.outcome, "audit_finalized_valid": verdict.finalized_valid,
                "audit_verification_complete": int(verdict.verification_complete), "audited_at": contracts.timestamp_of(
                    services.clock())})
        return self._response()

    def _collect_and_assemble(self):
        services = self.services
        today = services.clock().date()
        sources = services.collect(self.client_run, today)
        for warning in sources.warnings:
            self.warnings.append({"code": warning.code, "source": warning.source})
        if self.warnings:
            self.ledger.append_event(kind="warning", code="source_warnings", run_key=self.run.run_key,
                                     detail={"warnings": sorted({f"{w['source']}:{w['code']}" for w in self.warnings})})
        if not sources.ok:  # a wholly unavailable source blocks finalization and delivery
            from seo_report.source_collector import worst_failure
            failure = worst_failure(sources.failures)
            self.ledger.append_event(kind="error", code=failure.code[:64], run_key=self.run.run_key,
                                     detail={"sources": sorted(sources.unavailable_sources)})
            raise StageError(failure.category, failure.code)
        return report_service.assemble(self.client_run, sources, registry=services.registry, settings=self.settings,
                                       history_config=services.history_config, environ=services.environ)

    def _rendered_from_sidecar(self, reports_dir):
        try:
            sidecar = rstate.read_sidecar_for(reports_dir, self.run.render_input_sha256, ops=self.services.file_ops)
        except rstate.RenderStateError as error:
            raise StageError("finalization_conflict", ("sidecar_" + error.code)[:64]) from None
        return render_service.RenderResult(sidecar.render_input, None, sidecar.generation_date)


# --- dry run ------------------------------------------------------------------------------------------------------------------

def dry_run_client(client_key: str, period, *, services: Services, scratch_dir) -> RunResponse:
    """Collect, assemble and render into `scratch_dir` only; never finalize, never touch the ledger or production storage.

    The production history database and output root are read (through the finalized-only reader) for the historical
    comparison and are never written. The scratch folder must be absolute and must not overlap the output root, the
    history or ledger folders, the work root or the project tree.
    """
    period_id = _period_id(period)
    settings = services.settings
    request_id = services.new_id()
    request = RunRequest(request_id=request_id, idempotency_key=f"dry_run:{client_key}/{period_id}:{request_id}",
                         client_key=client_key, period_id=period_id, action="dry_run",
                         requested_at=contracts.timestamp_of(services.clock()), requested_by="cli:dry-run")
    now = contracts.timestamp_of(services.clock())

    def response(**kw):
        kw.setdefault("updated_at", contracts.timestamp_of(services.clock()))
        return build_response(request, None, mode="dry_run", **kw)

    client_run = services.clients.clients.get(client_key)
    if client_run is None:
        return response(outcome="rejected", error=contracts.error_info("config_invalid", "client_unknown", "intake"))
    if client_run.period_id != period_id:
        return response(outcome="rejected", error=contracts.error_info(
            "validation_failed", "period_differs_from_workbook", "intake"))
    scratch = _checked_scratch(scratch_dir, settings)
    if scratch is None:
        return response(outcome="rejected", error=contracts.error_info("validation_failed", "scratch_dir_invalid",
                                                                       "intake"))
    base = scratch / f"dry-run-{client_key}-{period_id}"
    n = 1
    while (base.parent / f"{base.name}-{n}").exists():
        n += 1
    root = base.parent / f"{base.name}-{n}"
    reports, reviewer, work = root / "reports", root / "reviewer", root / "work"
    for folder in (reports, reviewer, work):
        folder.mkdir(parents=True)
    try:
        sources = services.collect(client_run, services.clock().date())
        if not sources.ok:
            from seo_report.source_collector import worst_failure
            failure = worst_failure(sources.failures)
            return response(outcome="failed", state="collecting", error=contracts.error_info(
                failure.category, failure.code, "collecting"), retry=contracts.retry_info(failure.category))
        assembled = report_service.assemble(client_run, sources, registry=services.registry, settings=settings,
                                            history_config=services.history_config, environ=services.environ)
        rendered = render_service.render(
            assembled, client_run, settings=settings, generation_date=services.clock().date(), attempt=1,
            renderer=services.renderer, ops=services.file_ops, reports_dir=reports, reviewer_dir=reviewer, workdir=work)
    except StageError as error:
        stage = "collecting" if error.category.startswith("source") or error.category == "auth_required" else "rendering"
        return response(outcome="failed", state=stage, error=contracts.error_info(error.category, error.code, stage),
                        retry=contracts.retry_info(error.category))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        category, code = _unexpected(error)
        _log(logging.ERROR, "unexpected error in dry_run_client", exc_info=True,
             extra={"fields": {"error_category": category, "error_code": code}})
        return response(outcome="failed", state="failed", error=contracts.error_info(category, code, "intake"))
    drafted = rendered.drafted
    identity = {"render_input_sha256": drafted.render_input_sha256,
                "render_input_format": {"name": rh.FORMAT_NAME, "version": rh.FORMAT_VERSION},
                "generation_date": rendered.generation_date.isoformat(), "pdf_sha256": drafted.pdf_sha256,
                "pdf16": drafted.pdf_sha256[:16], "pdf_bytes": drafted.pdf_bytes, "record_name": None}
    result = response(outcome="succeeded", state="rendered", warnings=tuple(
        {"code": w.code, "source": w.source} for w in sources.warnings))
    return dataclasses.replace(result, identity=identity)


def _checked_scratch(scratch_dir, settings):
    """The scratch folder when it is safe, else None. Safe = absolute, and its CANONICAL path (links, junctions, 8.3 names
    and case resolved through the existing ancestors) overlaps none of the production folders or the project tree.
    A path that cannot be resolved with certainty is refused. The check is repeated after the folder is created."""
    from seo_report import runner_config as rc
    if scratch_dir is None:
        return None
    path = Path(scratch_dir)
    if not path.is_absolute():
        return None
    guarded = (settings.output_root, settings.history_db_path.parent, settings.ledger_db_path.parent,
               settings.work_root, settings.history_db_path, settings.ledger_db_path, rc.PROJECT_ROOT)

    def safe() -> bool:
        try:
            key = rc.canonical_key(path)
            return not any(rc.keys_overlap(key, rc.canonical_key(protected)) for protected in guarded)
        except rc.RunnerConfigError:
            return False

    if not safe():
        return None
    path.mkdir(parents=True, exist_ok=True)
    return path if safe() else None
