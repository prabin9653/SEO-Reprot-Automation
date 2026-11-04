"""The report, status and recovery commands of the CLI (R8.6d).

    report dry-run | run | batch        status        recovery inspect | acknowledge | cancel

These handlers only connect the command line to code that already exists and is already tested: `pipeline.run_client` (admission, locks, ledger, idempotency,
retry, finalization and the mandatory audit gate), `runner.run_batch` (sequential batch, client isolation, batch-level stops), the operator actions of R8.4.2
and the read-only ledger interfaces. They add no pipeline state, no error category and no exit code.

Production safety
  * `report run`, `report batch`, `recovery acknowledge` and `recovery cancel` need `--production` (parser) AND pass `require_gate`, the single function that
    calls `preflight.production_gate`. It runs BEFORE any client is built, any Google call, any collection, any ledger open or any write. The requirement
    `live_google_not_verified` is cleared only by a valid, genuine, unexpired verification attestation (R8.7c; none exists yet) and
    `credentials_not_relocated` is independent, so today these commands refuse with exit code 4 (or 3/5 when the preflight itself says so) and fixed
    blocker codes. There is no argument, option or environment variable that skips the gate; the only seams are keyword arguments of
    `cli.main` that exist for tests.
  * `report dry-run` is a PLAN: it validates the client, period and local configuration, runs the local preflight and explains what a real run would do. It
    does not call Google, sign in, collect data, render, write the ledger or the output folders, or write any workbook.
  * `status` and `recovery inspect` open the ledger read-only (`readonly=True`, never created) and never write.
  * `recovery acknowledge|cancel` go through the pipeline's operator actions, so version checks, the audit gate, the blocked-run rules, the cancellation
    restrictions and the append-only events are exactly those of R8.4.2; nothing is ever deleted.
"""

import datetime as dt
import uuid
from pathlib import Path

from seo_report import cli, contracts
from seo_report.cli import CliError, emit, load_settings
from seo_report.cli_exit import Exit, exit_code_for_batch, exit_code_for_response

_UUID_RUN = cli._UUID
_RESUMABLE = ("requested", "collecting", "rendering", "rendered", "finalizing", "finalized", "auditing")
_SETTLED = ("audited", "publishing", "ready_for_delivery", "delivered")


# --- shared helpers ---------------------------------------------------------------------------------------------------------------------

def _clock(ctx):
    return ctx.extra.get("clock") or (lambda: dt.datetime.now(dt.timezone.utc))


def _redact(ctx, path):
    from seo_report import preflight
    return preflight.redact_path(path, ctx.environ)


def require_gate(ctx, settings) -> None:
    """The one production gate. Raises `CliError` (exit code of the gate, fixed blocker codes) when anything blocks; returns only when allowed."""
    from seo_report import preflight
    gate = ctx.extra.get("gate") or preflight.production_gate
    allowed, code, blockers = gate(cli._preflight_inputs(ctx, settings, launch=True))
    if allowed:
        return
    raise _blocked(Exit(int(code)) if code != Exit.OK else Exit.PREFLIGHT_FAILED,
                   "production execution is blocked; nothing was started (no Google call, collection, rendering or ledger write)",
                   sorted({b.code for b in blockers}), "run `doctor` for details")


def _blocked(exit_code, message, codes, next_step) -> CliError:
    return CliError(exit_code, message, "production_gate_blocked", details={"blockers": list(codes), "next": next_step})


def require_attestation_coverage(ctx, settings, loaded, keys) -> None:
    """Second stage of the production gate, after the workbook has been read (the properties live in the workbook): every client about to run must be
    one the live verification covered, with the same Search Console and GA4 properties. Fails closed with fixed codes; nothing has been started yet.
    An injected (test-only) `gate` replaces the whole production gate, including this stage."""
    if ctx.extra.get("gate") is not None:
        return
    from seo_report import attestation, preflight
    verdict = preflight.attestation_verdict(cli._preflight_inputs(ctx, settings, launch=False))
    problems = set()
    for key in keys:
        run = loaded.clients.get(key)
        if run is None or run.client is None:
            continue
        code = attestation.client_coverage_code(verdict, key, run.client.gsc_property, run.client.ga4_property_id)
        if code:
            problems.add(code)
    if problems:
        raise _blocked(Exit.PREFLIGHT_FAILED, "production execution is blocked: the verification attestation does not cover every client or its properties; "
                       "nothing was started (no collection, rendering or ledger write)", sorted(problems), "verify again when authorised")


def open_ledger(ctx, settings, mode: str):
    """`read` (read-only; None when no ledger exists), `write` (create if needed) or `existing` (read-write, must exist). Never creates in the other modes."""
    from seo_report import ledger as lm
    clock = _clock(ctx)
    try:
        if mode == "read":
            return lm.Ledger(settings.ledger_db_path, clock=clock, readonly=True)
        return lm.Ledger(settings.ledger_db_path, clock=clock, create=(mode == "write"))
    except lm.LedgerError as error:
        if error.code == "ledger_missing":
            if mode == "read":
                return None
            raise CliError(Exit.CONFIG_INVALID, "no ledger exists yet, so there is nothing to change", "ledger_missing") from None
        raise


def _require_registered(ctx, settings, client_key):
    registry, sender, sheet = cli._local_inputs(ctx, settings)
    if client_key not in {entry.client_key for entry in registry.entries}:
        raise CliError(Exit.CONFIG_INVALID, f"client {client_key} is not in the client registry", "client_unknown")
    return registry


def _services(ctx, settings, loaded, registry, factory):
    from seo_report import pipeline
    from seo_report.google_clients import make_collect
    options = dict(ctx.extra.get("services_options") or {})
    collect = options.pop("collect", None) or make_collect(factory)
    return pipeline.Services(settings=settings, registry=registry, clients=loaded, clock=_clock(ctx), collect=collect, environ=ctx.environ, **options)


def _request(ctx, ledger, client_key, period_id):
    """The request for `report run`: `run` normally, `retry` for a failed/blocked latest run (the pipeline's own admission rules still decide)."""
    now = _clock(ctx)()
    base = dict(request_id=str(uuid.uuid4()), client_key=client_key, period_id=period_id, requested_at=contracts.timestamp_of(now),
                requested_by="cli:operator")
    latest = ledger.latest_run(client_key, period_id)
    if latest is not None and latest.state in ("failed", "blocked"):
        key = f"retry:{client_key}/{period_id}:g{latest.generation}:a{latest.attempt_count}"
        return contracts.RunRequest(idempotency_key=key, action="retry", **base)
    key = f"manual:{period_id}:{client_key}:{now.strftime('%Y%m%dT%H%M%SZ')}"
    return contracts.RunRequest(idempotency_key=key, action="run", **base)


def summarize(response) -> dict:
    data = response.to_dict()
    error = data.get("error")
    identity = data.get("identity") or {}
    return {"client_key": data["client_key"], "period_id": data["period_id"], "run_key": data["run_key"], "generation": data.get("generation", 1),
            "supersedes": data.get("supersedes"), "state": data["state"], "outcome": data["outcome"], "attempt": data["attempt"],
            "error": error, "retry_eligible": data["retry"]["eligible"], "audit": (data.get("audit") or {}).get("outcome"),
            "pdf16": identity.get("pdf16"), "replayed": data["replayed"], "exit_code": int(exit_code_for_response(response))}


def _summary_line(item: dict) -> str:
    text = f"{item['run_key']}: {item['state']} ({item['outcome']}, attempt {item['attempt']})"
    if item["error"]:
        text += f" — {item['error']['category']}/{item['error']['code']} at {item['error']['stage']}"
    if item["audit"]:
        text += f"; audit {item['audit']}"
    return text


# --- report dry-run ---------------------------------------------------------------------------------------------------------------------

_STEPS = [
    "check that the request matches the reporting workbook's client and its explicit 28-day period",
    "take the runner and client locks; recover any interrupted run",
    "collect Search Console and Analytics data (read-only Google access); an unavailable required source stops the run before finalization",
    "assemble the report with the history, insights and recommendations",
    "render the PDF with Chromium into a fresh work folder (hard time limit)",
    "finalize: write the reviewer record and promote the PDF safely (nothing is overwritten or deleted)",
    "run the mandatory audit gate; the run is `audited` only if it passes",
    "stop. Delivery (Drive, approval, e-mail) is not implemented",
]


def handle_dry_run(ctx) -> int:
    from seo_report import preflight
    settings = load_settings(ctx)
    client_key, period_id = ctx.args.client, ctx.args.period
    _require_registered(ctx, settings, client_key)
    inp = cli._preflight_inputs(ctx, settings, launch=False)
    findings = preflight.run_preflight(inp)
    blocked = sorted(f.code for f in findings if f.status == "blocked")
    blockers = blocked + preflight.uncleared_requirements(inp, findings)
    plan = {
        "mode": "plan_only", "client_key": client_key, "period_id": period_id,
        "client_configuration": {"in_registry": True, "workbook_row": "not_checked",
                                 "note": "the client's row, property identifiers, keywords and period dates live in the reporting workbook; reading it needs Google"},
        "sources": [{"service": "Google Sheets (reporting workbook)", "access": "read-only"}, {"service": "Google Search Console", "access": "read-only"},
                    {"service": "Google Analytics 4", "access": "read-only"}],
        "steps": _STEPS,
        "would_write": {"ledger": _redact(ctx, settings.ledger_db_path), "history": _redact(ctx, settings.history_db_path),
                        "output": _redact(ctx, settings.output_root), "work": _redact(ctx, settings.work_root)},
        "preflight": {"overall": preflight.overall_readiness(findings), "blocked": blocked, "browser_launch": "not_tested"},
        "production_blockers": blockers,
        "performed": ["validated the arguments", "validated the local configuration and the client registry", "ran the local read-only preflight"],
        "not_performed": ["no Google call or sign-in", "no source collection", "no rendering or PDF", "no ledger, history or output write",
                          "no workbook write"]}
    lines = [f"Dry run (plan only) for {client_key}, period {period_id}", "", "Would use:"]
    lines += [f"  - {s['service']} ({s['access']})" for s in plan["sources"]]
    lines += ["", "A real run would:"] + [f"  {n}. {step}" for n, step in enumerate(_STEPS, start=1)]
    lines += ["", "It would write (not written now):"] + [f"  {name:<8} {value}" for name, value in plan["would_write"].items()]
    lines += ["", f"Client configuration: registered; the workbook row is not checked ({plan['client_configuration']['note']})",
              f"Local preflight: {plan['preflight']['overall']}" + (f" — blocked: {', '.join(blocked)}" if blocked else ""),
              "Production blockers: " + (", ".join(blockers) if blockers else "none"),
              "Performed: " + "; ".join(plan["performed"]), "Not performed: " + "; ".join(plan["not_performed"])]
    emit(ctx, plan, lines)
    return int(preflight.exit_code(findings))


# --- report preview ------------------------------------------------------------------------------------------------------------------------

def handle_preview(ctx) -> int:
    """A real-data preview in its own folder (R8.7h.2). It reads Google (read-only) and runs Chromium; it opens no ledger, takes no lock, writes no history
    entry and creates no production folder, record or report. It passes the same read-only gate as a production run (local preflight with the Chromium
    launch, a valid attestation) and the same coverage check of the client and its properties, and needs the exact period of the workbook."""
    import dataclasses
    from seo_report import preview
    settings = load_settings(ctx)
    require_gate(ctx, settings)  # read-only: the preflight, the attestation; nothing is created or locked
    client_key, period_id = ctx.args.client, ctx.args.period
    registry = _require_registered(ctx, settings, client_key)
    _, loaded = cli._read_workbook(ctx, settings, only=client_key)
    run = loaded.clients.get(client_key)
    if run is None or run.client is None:
        reason = next((r.code for r in loaded.rejected if r.client_key == client_key), None) or dict(loaded.skipped).get(client_key) or "client_not_in_workbook"
        raise CliError(Exit.CONFIG_INVALID, f"client {client_key} is not eligible in the reporting workbook ({reason})", "client_not_eligible")
    if run.period_id != period_id:
        raise CliError(Exit.CONFIG_INVALID, "the requested period differs from the dates in the reporting workbook; use the workbook's exact period "
                       "(`clients list` shows it)", "period_differs_from_workbook")
    require_attestation_coverage(ctx, settings, loaded, [client_key])
    base = _services(ctx, settings, loaded, registry, cli._client_factory(ctx, settings))
    renderer = preview.PreviewRenderer(settings, inner=base.renderer)
    services = dataclasses.replace(base, renderer=renderer)
    try:
        result = preview.run_preview(settings=settings, environ=ctx.environ, services=services, client_key=client_key, period_id=period_id,
                                     renderer=renderer, clock=ctx.extra.get("clock"))
    except preview.PreviewFailed as error:
        details = {"stage": error.stage, "production_report_created": False}
        if error.preview_dir is not None:
            details["preview_folder"] = _redact(ctx, error.preview_dir)
        raise CliError(error.exit_code, error.message, error.code, details=details) from error
    data = result.public(ctx.environ)
    lines = [preview.NOTICE, f"Client {result.client_key}, period {result.period_id}", f"Preview ID: {result.preview_id}",
             f"Preview PDF: {data['preview_pdf']}", f"Pages: {result.page_count}; preview label verified on {result.watermark_pages} page(s)",
             f"Validation: {result.validation}", f"SHA-256: {result.pdf_sha256}"]
    if result.warnings:
        lines.append("Data warnings: " + ", ".join(result.warnings))
    lines.append(preview.NOTICE)
    emit(ctx, data, lines)
    return int(Exit.OK)


# --- report run -------------------------------------------------------------------------------------------------------------------------

def handle_run(ctx) -> int:
    from seo_report import pipeline
    settings = load_settings(ctx)
    require_gate(ctx, settings)  # before anything else: no client, no Google, no ledger
    client_key, period_id = ctx.args.client, ctx.args.period
    registry = _require_registered(ctx, settings, client_key)
    _, loaded = cli._read_workbook(ctx, settings, only=client_key)
    require_attestation_coverage(ctx, settings, loaded, [client_key])
    factory = cli._client_factory(ctx, settings)
    services = _services(ctx, settings, loaded, registry, factory)
    ledger = open_ledger(ctx, settings, "write")
    try:
        request = _request(ctx, ledger, client_key, period_id)
        response = pipeline.run_client(client_key, period_id, services=services, ledger=ledger, request=request)
    finally:
        ledger.close()
    item = summarize(response)
    code = Exit(item["exit_code"])
    lines = [_summary_line(item)]
    if code != Exit.OK:
        lines.append(f"exit code {int(code)}: " + cli.describe(code))
    emit(ctx, item, lines, ok=code == Exit.OK)
    return int(code)


# --- report batch -----------------------------------------------------------------------------------------------------------------------

def handle_batch(ctx) -> int:
    from seo_report import pipeline, runner
    settings = load_settings(ctx)
    require_gate(ctx, settings)
    registry = cli._local_inputs(ctx, settings)[0]
    _, loaded = cli._read_workbook(ctx, settings)
    wanted = set(ctx.args.client or [])
    due, other_period, not_eligible = [], [], []
    for key, run in sorted(loaded.clients.items()):
        if wanted and key not in wanted:
            continue
        (due if run.period_id == ctx.args.period else other_period).append(key)
    require_attestation_coverage(ctx, settings, loaded, due)
    not_eligible = sorted(wanted - set(loaded.clients))
    rejected = [{"client_key": r.client_key, "sheet_row": r.sheet_row, "code": r.code} for r in loaded.rejected
                if not wanted or r.client_key in wanted or r.client_key is None]
    skipped = [{"client_key": k, "code": c} for k, c in loaded.skipped if not wanted or k in wanted]
    results, batch_code = [], Exit.OK
    if due:
        factory = cli._client_factory(ctx, settings)
        services = _services(ctx, settings, loaded, registry, factory)
        ledger = open_ledger(ctx, settings, "write")
        try:
            requests = [_request(ctx, ledger, key, ctx.args.period) for key in due]
            outcome = runner.run_batch(requests, services=services, ledger=ledger, max_clients=ctx.args.max_clients)
        finally:
            ledger.close()
        results = [summarize(r) for r in outcome.results]
        batch_code = exit_code_for_batch(outcome)
        aborted = outcome.abort_reason if outcome.aborted else None
        not_started = len(outcome.not_started)
    else:
        aborted, not_started = None, 0
    code = batch_code
    if code == Exit.OK and (rejected or not_eligible or (wanted and not due)):
        code = Exit.CONFIG_INVALID  # misconfigured or unknown clients must be noticed by a scheduler, even though valid clients were processed
    data = {"period_id": ctx.args.period, "processed": len(results), "results": results, "not_due_for_this_period": other_period,
            "rejected": rejected, "skipped": skipped, "not_eligible": not_eligible, "aborted": aborted, "not_started": not_started,
            "exit_code": int(code)}
    lines = [f"Batch for period {ctx.args.period}: {len(results)} client(s) processed"]
    lines += [f"  {_summary_line(item)}" for item in results]
    if other_period:
        lines.append(f"  not run (their reporting period is a different one): {', '.join(other_period)}")
    lines += [f"  REJECTED row {r['sheet_row']} ({r['client_key'] or 'unknown client'}): {r['code']}" for r in rejected]
    lines += [f"  skipped {s['client_key']}: {s['code']}" for s in skipped]
    lines += [f"  NOT ELIGIBLE: {key} (unknown, rejected or inactive)" for key in not_eligible]
    if aborted:
        lines.append(f"  BATCH STOPPED ({aborted}); {not_started} request(s) were not started")
    if code != Exit.OK:
        lines.append(f"exit code {int(code)}: " + cli.describe(code))
    emit(ctx, data, lines, ok=code == Exit.OK)
    return int(code)


# --- status and recovery inspection (read-only) --------------------------------------------------------------------------------------------

def run_view(ledger, run) -> dict:
    """A safe description of one run: ids, states, fixed codes, hashes shortened to 16 hex. No paths, recipients or credentials."""
    successors = [r.run_key for r in ledger.runs_for_period(run.client_key, f"{run.period_start}_{run.period_end}") if r.supersedes_run_key == run.run_key]
    finalized = bool(run.finalized_at and run.engine_run_id and run.record_name)
    audited = run.audit_outcome == "consistent" and bool(run.audit_verification_complete)
    return {"run_key": run.run_key, "client_key": run.client_key, "period_id": f"{run.period_start}_{run.period_end}", "generation": run.generation,
            "supersedes": run.supersedes_run_key, "link_kind": run.link_kind, "superseded_by": successors[0] if successors else None,
            "state": run.state, "stage": run.failed_stage or (run.state if run.state in contracts.IN_PROGRESS_STATES else None),
            "attempt": run.attempt_count, "error_category": run.error_category, "error_code": run.error_code, "retry_eligible": bool(run.retry_eligible),
            "acknowledged": bool(run.acknowledged), "finalized": finalized, "audited": audited, "audit_outcome": run.audit_outcome,
            "pdf16": run.pdf_sha256[:16] if run.pdf_sha256 else None, "engine_run_id": run.engine_run_id,
            "generation_date": run.generation_date, "created_at": run.created_at, "updated_at": run.updated_at, "finished_at": run.finished_at,
            "delivery": "not_tracked"}  # finalization and audit say nothing about delivery, which is not implemented


def _select(ledger, args):
    if getattr(args, "run", None):
        if _UUID_RUN.fullmatch(args.run):
            return [r for r in ledger.list_runs() if r.engine_run_id == args.run]
        found = ledger.get_run(args.run)
        return [found] if found is not None else []
    runs = ledger.list_runs()
    client = getattr(args, "client", None)
    return [r for r in runs if client is None or r.client_key == client]


def _view_line(view: dict) -> str:
    flags = [name for name, on in (("finalized", view["finalized"]), ("audited", view["audited"]), ("acknowledged", view["acknowledged"])) if on]
    text = f"{view['run_key']}: {view['state']}" + (f" at {view['stage']}" if view["stage"] and view["state"] in ("failed", "blocked") else "")
    text += f", attempt {view['attempt']}"
    if view["error_code"]:
        text += f", {view['error_category']}/{view['error_code']}"
    if flags:
        text += " [" + ", ".join(flags) + "]"
    if view["supersedes"]:
        text += f", {view['link_kind']} of {view['supersedes']}"
    if view["superseded_by"]:
        text += f", replaced by {view['superseded_by']}"
    return text


def handle_status(ctx) -> int:
    settings = load_settings(ctx)
    ledger = open_ledger(ctx, settings, "read")
    if ledger is None:
        if getattr(ctx.args, "run", None):
            raise CliError(Exit.CONFIG_INVALID, "no ledger exists yet, so that run does not exist", "run_not_found")
        emit(ctx, {"ledger": "not_created_yet", "runs": []}, ["No ledger exists yet: no runs have been recorded."])
        return int(Exit.OK)
    try:
        runs = _select(ledger, ctx.args)
        if getattr(ctx.args, "run", None) and not runs:
            raise CliError(Exit.CONFIG_INVALID, "no run with that identifier exists in the ledger", "run_not_found")
        views = [run_view(ledger, r) for r in runs]
    finally:
        ledger.close()
    lines = [f"{len(views)} run(s)"] + [f"  {_view_line(v)}" for v in views] if views else ["No runs recorded for this selection."]
    if views:
        lines.append("Delivery is not tracked by the runner yet: `audited` means finalized and audited, nothing more.")
    emit(ctx, {"ledger": "present", "runs": views}, lines)
    return int(Exit.OK)


def _lock_view(ledger, run) -> dict:
    from seo_report import ledger as lm
    lock = ledger.get_lock(f"client:{run.client_key}")
    if lock is None:
        return {"held": False}
    expired = lock.expires_at < ledger.now()
    holder = "other_host" if lock.host != ledger.host else lm.probe_pid(lock.pid)
    return {"held": True, "lease_expired": expired, "holder": holder}


def handle_recovery_inspect(ctx) -> int:
    from types import SimpleNamespace
    from seo_report import pipeline
    settings = load_settings(ctx)
    ledger = open_ledger(ctx, settings, "read")
    if ledger is None:
        emit(ctx, {"ledger": "not_created_yet", "runs": []}, ["No ledger exists yet: nothing needs recovery."])
        return int(Exit.OK)
    items = []
    try:
        wanted = ("failed", "blocked", *contracts.IN_PROGRESS_STATES)
        for run in ledger.list_runs(wanted):
            if ctx.args.client and run.client_key != ctx.args.client:
                continue
            view = run_view(ledger, run)
            view["lock"] = _lock_view(ledger, run)
            view["partial_artifacts"] = pipeline._has_partial_files(settings, run)
            cancel_refusal = pipeline._cancel_refusal(SimpleNamespace(settings=settings), run) if run.state in ("failed", "blocked") else None
            actions, needs = [], None
            if run.state == "blocked":
                if run.acknowledged:
                    actions.append("report_run_retries_after_acknowledgement")
                elif pipeline._recoverable(run):
                    actions.append("acknowledge")
                    needs = "operator_acknowledgement"
                else:
                    needs = "condition_not_recoverable_by_retry"
            elif run.state == "failed":
                actions.append("report_run_retries" if run.retry_eligible else "see_error_code")
            else:
                actions.append("recovered_automatically_by_the_next_run")
            if cancel_refusal is None and run.state in ("failed", "blocked"):
                actions.append("cancel")
            view.update(actions=actions, needs=needs, cancel_refusal=cancel_refusal,
                        reconciliation_conflict=run.error_category == "finalization_conflict")
            items.append(view)
    finally:
        ledger.close()
    lines = [f"{len(items)} run(s) need attention or are in progress"] if items else ["No blocked, failed or interrupted runs."]
    for v in items:
        lines.append(f"  {_view_line(v)}")
        lines.append(f"      actions: {', '.join(v['actions'])}" + (f"; needs: {v['needs']}" if v["needs"] else "")
                     + ("; partial artifacts present" if v["partial_artifacts"] else "") + ("; reconciliation conflict" if v["reconciliation_conflict"] else "")
                     + (f"; lock: {v['lock']['holder']}" if v["lock"].get("held") else ""))
    emit(ctx, {"ledger": "present", "runs": items}, lines)
    return int(Exit.OK)


# --- recovery acknowledge / cancel ----------------------------------------------------------------------------------------------------------

def _operator_action(ctx, action: str) -> int:
    from seo_report import client_loader, pipeline
    from seo_report.client_config import load_client_registry, ClientConfigError
    from seo_report.periods import report_periods
    settings = load_settings(ctx)
    require_gate(ctx, settings)
    client_key, period_id, generation = contracts.parse_run_key_full(ctx.args.run)
    try:
        registry = load_client_registry(settings.clients_config_path)
    except (ClientConfigError, OSError):
        raise CliError(Exit.CONFIG_INVALID, "the client registry (clients_config) is missing or invalid", "client_registry_invalid") from None
    if client_key not in {e.client_key for e in registry.entries}:
        raise CliError(Exit.CONFIG_INVALID, f"client {client_key} is not in the client registry", "client_unknown")
    ledger = open_ledger(ctx, settings, "existing")
    try:
        run = ledger.get_run(ctx.args.run)
        if run is None:
            raise CliError(Exit.CONFIG_INVALID, "no run with that identifier exists in the ledger", "run_not_found")
        latest = ledger.latest_run(client_key, period_id)
        if latest.run_key != run.run_key:
            raise CliError(Exit.OPERATOR_ACTION_REQUIRED, f"that is not the latest generation of the period (latest: {latest.run_key})", "not_latest_generation")
        start, end = contracts.parse_period_id(period_id)
        # Operator actions need the run, not the workbook: a placeholder client run (real period, no client data) satisfies the pipeline's validation.
        placeholder = client_loader.ClientRun(client_key, None, report_periods(start, end))
        loaded = client_loader.LoadResult({client_key: placeholder}, (), ())

        def no_collection(client_run, today):
            raise contracts.StageError("internal_error", "collection_not_used_by_recovery")
        options = dict(ctx.extra.get("services_options") or {})
        options.pop("collect", None)
        from seo_report import pipeline as pl
        services = pl.Services(settings=settings, registry=registry, clients=loaded, clock=_clock(ctx), collect=no_collection, environ=ctx.environ, **options)
        now = _clock(ctx)()
        key = (f"acknowledge:{client_key}/{period_id}:g{generation}:v{run.state_version}" if action == "acknowledge"
               else f"cancel:{client_key}/{period_id}:g{generation}")
        request = contracts.RunRequest(
            request_id=str(uuid.uuid4()), idempotency_key=key, client_key=client_key, period_id=period_id, action=action,
            requested_at=contracts.timestamp_of(now), requested_by="cli:operator", operator=ctx.args.operator, reason=ctx.args.reason)
        response = pl.run_client(client_key, period_id, services=services, ledger=ledger, request=request)
        view = run_view(ledger, ledger.get_run(run.run_key))
    finally:
        ledger.close()
    item = summarize(response)
    item["operator_action"] = {"action": action, "operator": ctx.args.operator, "reason": ctx.args.reason}
    item["run"] = view
    # The operator action itself succeeded even though the run it recorded a decision about is still blocked: judge the action, not the run.
    code = Exit.OK if response.outcome in ("succeeded", "already_complete") else Exit(item["exit_code"])
    item["exit_code"] = int(code)
    item["ok"] = code == Exit.OK
    lines = [_summary_line(item)]
    if code == Exit.OK:
        lines.append({"acknowledge": "Acknowledged. The run is still blocked: use `report run` to retry; the blocking condition is checked again then.",
                      "cancel": "Cancelled. Nothing was deleted."}[action])
    else:
        lines.append(f"exit code {int(code)}: " + cli.describe(code))
    emit(ctx, item, lines, ok=code == Exit.OK)
    return int(code)


def handle_acknowledge(ctx) -> int:
    return _operator_action(ctx, "acknowledge")


def handle_cancel(ctx) -> int:
    return _operator_action(ctx, "cancel")


HANDLERS = {("report", "preview"): handle_preview, ("report", "dry-run"): handle_dry_run, ("report", "run"): handle_run, ("report", "batch"): handle_batch, ("status", None): handle_status,
            ("recovery", "inspect"): handle_recovery_inspect, ("recovery", "acknowledge"): handle_acknowledge, ("recovery", "cancel"): handle_cancel}
