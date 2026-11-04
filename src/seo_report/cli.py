"""Command-line interface of the SEO report runner (R8.6).

    python -m seo_report --help

This module holds the command tree, argument validation, the production guard, configuration resolution, output formatting, the exit-code
mapping and the error boundary, plus the handlers of `system-info`, `doctor`, `clients` and `setup auth`; the handlers of `report`, `status`
and `recovery` live in `cli_report`. Every command has its real handler. `deferred_handler` (exit code 13, nothing read or written) is kept
for a command that might one day be parsed before it is built; no current command uses it.

Rules that hold for every command, now and later
  * **No command defaults to production.** A configuration file must be named (`--config` or `JASPER_SEO_CONFIG`; the console says where it
    came from). A command that writes the production ledger or output (`report run`, `report batch`, `recovery acknowledge|cancel`) additionally
    requires the explicit flag `--production`. `report dry-run` has no such flag and never writes to production storage or the ledger.
  * Client keys and periods are validated before anything else happens (`client_config.validate_client_key`, `contracts.parse_period_id`).
  * Output is plain text by default and a single JSON object with `--json`. Neither contains secrets; dynamic text is sanitized.
  * Failures end with a stable exit code (`cli_exit`) and a short operator message; diagnostic detail goes to the sanitized log.
  * `KeyboardInterrupt` ends the command with code 130 after the normal cleanup; `SystemExit` raised by a handler is never swallowed.
"""

import argparse
import json
import logging
import os
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from seo_report import contracts
from seo_report import runner_config as rc
from seo_report.client_config import ClientConfigError, validate_client_key
from seo_report.cli_exit import Exit, describe, exit_code_for_ledger_error

APP_NAME = "seo-report"
APP_VERSION = "8.6.0"
ENV_CONFIG = "JASPER_SEO_CONFIG"
LOGGER = logging.getLogger("seo_report.cli")

_OPERATOR = re.compile(r"[A-Za-z0-9:._-]{1,64}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# (command, subcommand) -> sub-stage that delivers the real handler. Empty since R8.6d: every command has its handler
# (`report`, `status` and `recovery` live in `cli_report`). The mechanism stays for any future command that is parsed before it is built.
DEFERRED: dict = {}
# commands that change the production ledger or production output: --production is mandatory
PRODUCTION_COMMANDS = frozenset({("report", "run"), ("report", "batch"), ("recovery", "acknowledge"), ("recovery", "cancel")})


class CliError(Exception):
    """A failure the operator should read: a stable exit code, a short message with no secrets, and a fixed code."""

    def __init__(self, exit_code: Exit, message: str, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.exit_code, self.message, self.code = exit_code, message, code
        self.details = details or {}  # extra fixed-code facts shown with the error (for example the production-gate blockers)


# --- argument types (each raises ArgumentTypeError, which argparse turns into a usage error, exit code 2) ---------------------------

def client_key_arg(text: str) -> str:
    try:
        return validate_client_key(text, where="--client")
    except ClientConfigError:
        raise argparse.ArgumentTypeError("not a valid client key (lower-case letters, digits, '-' and '_'; up to 63 characters)") from None


def period_arg(text: str) -> str:
    try:
        contracts.parse_period_id(text)
    except contracts.ContractError as error:
        raise argparse.ArgumentTypeError(
            f"not a valid reporting period ({error.code}); use YYYY-MM-DD_YYYY-MM-DD covering exactly 28 days") from None
    return text


def run_arg(text: str) -> str:
    """A run identifier: the ledger run key `<client>/<period>[~N]`, or the engine's run UUID."""
    if _UUID.fullmatch(text):
        return text
    try:
        contracts.parse_run_key_full(text)
    except contracts.ContractError:
        raise argparse.ArgumentTypeError("not a run identifier (expected <client>/<period>, optionally ~N, or a run UUID)") from None
    return text


def run_key_arg(text: str) -> str:
    try:
        contracts.parse_run_key_full(text)
    except contracts.ContractError:
        raise argparse.ArgumentTypeError("not a run key (expected <client>/<period>, optionally ~N)") from None
    return text


def operator_arg(text: str) -> str:
    if not _OPERATOR.fullmatch(text):
        raise argparse.ArgumentTypeError("operator must be 1-64 characters from letters, digits and : . _ - (no e-mail address)")
    return text


def positive_int_arg(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if not 1 <= value <= 1000:
        raise argparse.ArgumentTypeError("must be between 1 and 1000")
    return value


# --- the parser ---------------------------------------------------------------------------------------------------------------------

def _epilog() -> str:
    lines = ["exit codes:"]
    for code in Exit:
        # exit code 13 survives only as a reserved value (the "not built yet" mechanism); the shared description in cli_exit.py is left untouched
        lines.append(f"  {int(code):>3}  " + ("reserved: a command that is parsed but not built yet (none at present)" if code == Exit.NOT_AVAILABLE else describe(code)))
    lines += ["", "No command uses production data unless you name the configuration file; commands that change production state also need --production.",
              "Production execution is additionally refused by a built-in readiness gate until live Google access has been verified and the credentials "
              "are relocated (see `doctor`)."]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m seo_report", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Jasper IT Solutions SEO report runner (local, sequential, ledger-backed).", epilog=_epilog())
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    parser.add_argument("--config", metavar="PATH", help=f"runner settings file (or set {ENV_CONFIG}); never defaulted")
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    # --config and --json are accepted before OR after the command (SUPPRESS keeps the value given first)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    def leaf(group, name, **kwargs):
        return group.add_parser(name, parents=[common], **kwargs)

    leaf(commands, "system-info", help="show version, configuration and storage locations")
    doctor = leaf(commands, "doctor", help="run the local, read-only deployment preflight: runtime, dependencies, browser, assets, storage, "
                                           "authentication configuration, client configuration, logging")
    doctor.add_argument("--skip-browser-launch", action="store_true", help="do not start Chromium (the launch check is then 'unverified')")

    clients = commands.add_parser("clients", help="list and validate clients").add_subparsers(
        dest="subcommand", required=True, metavar="ACTION")
    leaf(clients, "list", help="list stable client keys and status; invalid rows are reported, never hide valid clients")
    validate = leaf(clients, "validate", help="validate every client's configuration")
    validate.add_argument("--client", type=client_key_arg, metavar="CLIENT_KEY", help="only this client")
    validate.add_argument("--online", action="store_true",
                          help="ALSO read the reporting workbook (read-only Google Sheets access) and check each client's rows; "
                               "without it nothing contacts Google")

    report = commands.add_parser("report", help="plan, run or batch-run reports").add_subparsers(
        dest="subcommand", required=True, metavar="ACTION")
    dry = leaf(report, "dry-run", help="plan only: show what a real run would read, do and write, and what blocks production; renders nothing and "
                                       "never touches Google, production storage or the ledger")
    dry.add_argument("--client", type=client_key_arg, required=True, metavar="CLIENT_KEY")
    dry.add_argument("--period", type=period_arg, required=True, metavar="YYYY-MM-DD_YYYY-MM-DD")
    # Not supported (dry-run renders nothing, so it has no scratch folder). Parsed only so that it is refused with an explanation instead of
    # an "unrecognized argument" message or, worse, being accepted and ignored; hidden from the help.
    dry.add_argument("--scratch-dir", metavar="PATH", default=None, help=argparse.SUPPRESS)
    preview = leaf(report, "preview", help="PREVIEW (contacts Google, read-only; starts Chromium): render a real-data report marked \"PREVIEW \u2014 NOT FINAL\" into "
                                           "its own preview folder; creates no production report, run, record or history entry")
    preview.add_argument("--client", type=client_key_arg, required=True, metavar="CLIENT_KEY")
    preview.add_argument("--period", type=period_arg, required=True, metavar="YYYY-MM-DD_YYYY-MM-DD", help="must equal the client's dates in the workbook")
    run = leaf(report, "run", help="run one client's report through the ledger (needs --production)")
    run.add_argument("--client", type=client_key_arg, required=True, metavar="CLIENT_KEY")
    run.add_argument("--period", type=period_arg, required=True, metavar="YYYY-MM-DD_YYYY-MM-DD")
    run.add_argument("--production", action="store_true", help="confirm that this writes the production ledger and output")
    batch = leaf(report, "batch", help="run clients one after another (needs --production)")
    batch.add_argument("--period", type=period_arg, required=True, metavar="YYYY-MM-DD_YYYY-MM-DD")
    batch.add_argument("--client", type=client_key_arg, action="append", metavar="CLIENT_KEY", help="restrict to these clients (repeatable)")
    batch.add_argument("--max-clients", type=positive_int_arg, metavar="N", help="stop after N clients")
    batch.add_argument("--production", action="store_true", help="confirm that this writes the production ledger and output")

    status = leaf(commands, "status", help="show run states from the ledger (read-only)")
    target = status.add_mutually_exclusive_group()
    target.add_argument("--client", type=client_key_arg, metavar="CLIENT_KEY")
    target.add_argument("--run", type=run_arg, metavar="RUN_ID", help="run key <client>/<period>[~N] or the engine run UUID")

    recovery = commands.add_parser("recovery", help="inspect and resolve blocked or failed runs").add_subparsers(
        dest="subcommand", required=True, metavar="ACTION")
    inspect = leaf(recovery, "inspect", help="list blocked and failed runs with what is needed next (read-only)")
    inspect.add_argument("--client", type=client_key_arg, metavar="CLIENT_KEY")
    for name, text in (("acknowledge", "record that an operator reviewed a blocked run (never moves it; a retry follows)"),
                       ("cancel", "cancel a run that has no finalized report (never deletes anything)")):
        sub = leaf(recovery, name, help=text + " (needs --production)")
        sub.add_argument("--run", type=run_key_arg, required=True, metavar="RUN_KEY", help="<client>/<period>[~N]")
        sub.add_argument("--operator", type=operator_arg, required=True, metavar="LABEL", help="who is deciding (recorded in the ledger)")
        sub.add_argument("--reason", choices=contracts.REASON_CODES, required=True, metavar="CODE",
                         help="one of: " + ", ".join(contracts.REASON_CODES))
        sub.add_argument("--production", action="store_true", help="confirm that this changes the production ledger")

    verify = commands.add_parser("verify", help="live verification of the Google integration (contacts Google)").add_subparsers(
        dest="subcommand", required=True, metavar="ACTION")
    live = leaf(verify, "live", help="LIVE: contact Google (read-only: OAuth refresh if needed, the reporting workbook, Search Console, GA4) and, only if every "
                                     "check succeeds, store the verification attestation that clears `live_google_not_verified` (needs --live)")
    live.add_argument("--client", type=client_key_arg, required=True, metavar="CLIENT_KEY", help="the client whose workbook row and properties are verified")
    live.add_argument("--live", action="store_true", help="confirm that this contacts Google, may rewrite the cached sign-in and writes the attestation")
    setup = commands.add_parser("setup", help="attended one-time set-up").add_subparsers(
        dest="subcommand", required=True, metavar="WHAT")
    leaf(setup, "auth", help="sign in to Google in a browser (only at an attended console); never part of normal reporting")
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Rules that argparse cannot express. Exits with the usage code (2)."""
    key = (args.command, getattr(args, "subcommand", None))
    if key == ("report", "dry-run") and getattr(args, "scratch_dir", None) is not None:
        parser.error("--scratch-dir is not supported: `report dry-run` only plans and renders nothing, so it has no scratch folder "
                     "(remove the option; nothing was read or written)")
    if key == ("verify", "live") and not getattr(args, "live", False):
        parser.error("this command contacts Google (read-only), may rewrite the cached sign-in and stores the verification attestation; add --live to confirm")
    if key in PRODUCTION_COMMANDS and not getattr(args, "production", False):
        parser.error("this command changes production state; add --production to confirm "
                     "(use `report dry-run` to try a report without touching production)")


# --- context, configuration, output -------------------------------------------------------------------------------------------------

@dataclass
class Context:
    args: argparse.Namespace
    environ: dict
    handlers: dict
    config_source: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple:
        return self.args.command, getattr(self.args, "subcommand", None)


def resolve_config_path(ctx: Context) -> Path:
    """The settings file named by `--config` or `JASPER_SEO_CONFIG`; there is deliberately no default location."""
    explicit, from_env = ctx.args.config, ctx.environ.get(ENV_CONFIG)
    if explicit:
        ctx.config_source = "--config"
        text = explicit
    elif from_env:
        ctx.config_source = ENV_CONFIG
        text = from_env
    else:
        raise CliError(Exit.CONFIG_INVALID, f"no configuration file was given: use --config PATH or set {ENV_CONFIG}", "config_not_given")
    path = Path(text)
    if not path.is_absolute():
        path = path.resolve()
    if not path.is_file():
        raise CliError(Exit.CONFIG_INVALID, "the configuration file does not exist or is not a file", "config_not_found")
    return path


def load_settings(ctx: Context):
    """Validated `RunnerSettings` for the named configuration. Never creates anything."""
    path = resolve_config_path(ctx)
    ctx.extra["config_path"] = path
    try:
        return rc.load_settings(path, environ=ctx.environ)
    except rc.RunnerConfigError as error:
        where = f" ({error.field})" if error.field else ""
        raise CliError(Exit.CONFIG_INVALID, f"the configuration is invalid: {error.code}{where}", error.code) from error


def emit(ctx: Context, data: dict, lines: list[str], ok: bool = True) -> None:
    """Print a result: one sanitized JSON object (`--json`) or the human-readable lines."""
    if ctx.args.json:
        from seo_report.runner import safe_value
        print(json.dumps({"ok": ok, **safe_value(data)}, sort_keys=True))
    else:
        for line in lines:
            print(line)


def fail(ctx: Context, error: CliError) -> int:
    if ctx.args.json:
        body = {"ok": False, "exit_code": int(error.exit_code), "error": {"code": error.code, "message": error.message}}
        if error.details:
            body["details"] = error.details
        print(json.dumps(body, sort_keys=True))
    else:
        print(f"error: {error.message}", file=sys.stderr)
        for key, value in error.details.items():
            print(f"  {key}: {', '.join(map(str, value)) if isinstance(value, (list, tuple)) else value}", file=sys.stderr)
        print(f"exit code {int(error.exit_code)}: {describe(error.exit_code)}", file=sys.stderr)
    return int(error.exit_code)


# --- handlers ----------------------------------------------------------------------------------------------------------------------

def handle_system_info(ctx: Context) -> int:
    """Informational summary (R8.6c). Needs a valid settings file (exit 3 otherwise). Local metadata only: no network, no sign-in, no browser start,
    no write; paths are shown redacted."""
    from seo_report import auth, preflight
    settings = load_settings(ctx)
    path = ctx.extra["config_path"]
    env = ctx.environ
    redact = lambda p: preflight.redact_path(p, env)  # noqa: E731
    inp = _preflight_inputs(ctx, settings, launch=False)
    findings = preflight.run_preflight(inp)
    by = lambda section: {f.component: f for f in findings if f.section == section}  # noqa: E731
    try:
        from seo_report import runner
        log_folder = redact(runner.check_log_dir(environ=env))
    except Exception:
        log_folder = None
    logging_state = ctx.extra.get("logging") or {"active": False, "code": "logging_state_unknown"}
    credentials = auth.describe_paths(auth.paths_from_settings(settings, env))
    verdict = preflight.attestation_verdict(inp)
    attestation = {"status": verdict.status, "code": verdict.code, "expires_at": verdict.expires_at, "age_hours": None if verdict.age_seconds is None else
                   round(verdict.age_seconds / 3600, 1)}
    deps = by("dependencies")
    storage = {"output_root": redact(settings.output_root), "work_root": redact(settings.work_root),
               "history_db": redact(settings.history_db_path), "ledger_db": redact(settings.ledger_db_path), "log_dir": log_folder}
    data = {"application": APP_NAME, "version": APP_VERSION, "python": platform.python_version(),
            "operating_system": f"{platform.system()} {platform.release()}", "architecture": platform.machine(),
            "config": {"path": redact(path), "source": ctx.config_source}, "storage": storage,
            "ledger": {"database": "present" if Path(settings.ledger_db_path).exists() else "not_created_yet"},
            "dependencies": {f.component: {"status": f.status, "code": f.code} for f in deps.values()},
            "playwright": {"version": preflight.installed_version("playwright"), "chromium": by("browser").get("chromium", None) and
                           by("browser")["chromium"].code},
            "assets": {f.component: {"status": f.status, "code": f.code} for f in findings if f.section == "assets"},
            "oauth_files": {"configuration": credentials["configuration"], "client_file": credentials["client_file"],
                            "cached_sign_in": credentials["token_file"], "outside_project": credentials["outside_project"]},
            "logging": {"active": bool(logging_state.get("active")), "code": logging_state.get("code"), "format": "json-lines",
                        "rotation": "5 MB x 5 backups"},
            "runner_readiness": {"local": preflight.overall_readiness(findings), "production_ready": False,
                                 "blocked": sorted(f.code for f in findings if f.status == "blocked"),
                                 "outstanding": preflight.outstanding_for(findings, settings)},
            "verification_attestation": attestation, "live_google_verified": attestation["status"] == "valid",
            "lock_ttl_seconds": settings.lock_ttl_seconds}
    lines = [f"{APP_NAME} {APP_VERSION}", f"Python {data['python']} on {data['operating_system']} ({data['architecture']})",
             f"Configuration: {data['config']['path']} (from {ctx.config_source})", "Storage:"]
    lines += [f"  {name:<12} {value}" for name, value in storage.items()]
    lines += [f"Ledger database: {data['ledger']['database'].replace('_', ' ')}",
              "Dependencies: " + "; ".join(f"{f.component} {f.status}" for f in deps.values()),
              f"Playwright: {data['playwright']['version'] or 'not installed'}; Chromium: {data['playwright']['chromium'] or 'not found'}",
              "Assets: " + "; ".join(f"{name} {item['status']}" for name, item in data["assets"].items()),
              f"Credentials: configuration {credentials['configuration']}, client file {credentials['client_file']}, cached sign-in "
              f"{credentials['token_file']}, outside project: {'yes' if credentials['outside_project'] else 'NO'}",
              f"Logging: {'active' if data['logging']['active'] else 'NOT active (' + str(data['logging']['code']) + ')'}, JSON lines, {data['logging']['rotation']}",
              f"Runner readiness (local checks): {data['runner_readiness']['local']}; production-ready: NO",
              "Live Google integration verified: " + (f"YES (local attestation, expires {attestation['expires_at']}; not re-checked against Google)"
                                                      if verdict.valid else f"NO ({verdict.code})"), "Run `doctor` for the full preflight."]
    emit(ctx, data, lines)
    return int(Exit.OK)


def _preflight_inputs(ctx: Context, settings, *, launch: bool):
    """The inputs of the preflight for this command (the settings may be missing; the reason is carried as a fixed code)."""
    from seo_report import preflight
    return preflight.Inputs(settings=settings, config_code=ctx.extra.get("config_code"), config_source=ctx.config_source, environ=ctx.environ,
                            logging_status=ctx.extra.get("logging"), launch_browser=launch, app_version=APP_VERSION,
                            probes=ctx.extra.get("preflight_probes", {}), clock=ctx.extra.get("clock"))


def handle_doctor(ctx: Context) -> int:
    """The local, read-only deployment preflight (R8.6c). Works even when the settings are missing or invalid (that is then a finding)."""
    from seo_report import preflight
    try:
        settings = load_settings(ctx)
    except CliError as error:
        settings, ctx.extra["config_code"] = None, error.code
    inp = _preflight_inputs(ctx, settings, launch=not ctx.args.skip_browser_launch)
    findings = preflight.run_preflight(inp)
    clock = ctx.extra.get("clock")
    report = preflight.build_report(findings, app_version=APP_VERSION, now=clock() if clock else None, settings=settings)
    if ctx.args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        for line in preflight.format_text(report):
            print(line)
    return int(report["exit_code"])


# --- clients and setup (R8.6b) ---------------------------------------------------------------------------------------------------

def _client_factory(ctx: Context, settings):
    """The Google client factory for this command: the injected one (tests), else one over the configured credential locations."""
    injected = ctx.extra.get("client_factory")
    if injected is not None:
        return injected
    from seo_report import auth
    from seo_report.google_clients import GoogleClientFactory
    return GoogleClientFactory(paths=auth.paths_from_settings(settings, ctx.environ))


def _local_inputs(ctx: Context, settings):
    """(registry, default sender, spreadsheet id) from local files and settings; configuration errors become exit code 3."""
    from seo_report import clients as cl
    from seo_report.client_config import load_client_registry
    try:
        registry = load_client_registry(settings.clients_config_path)
    except (ClientConfigError, OSError):
        raise CliError(Exit.CONFIG_INVALID, "the client registry (clients_config) is missing or invalid", "client_registry_invalid") from None
    try:
        sender = cl.load_default_sender(settings.sender_config_path)
    except (cl.SheetDataError, OSError, ValueError):
        raise CliError(Exit.CONFIG_INVALID, "the sender configuration (sender_config) is missing or invalid", "sender_config_invalid") from None
    if not settings.reporting_spreadsheet_id:
        raise CliError(Exit.CONFIG_INVALID, "workbooks.reporting_spreadsheet_id is not configured", "reporting_workbook_not_configured")
    return registry, sender, settings.reporting_spreadsheet_id


def _read_workbook(ctx: Context, settings, only=None):
    """Read the reporting workbook through the existing loader (read-only). Google problems become the matching exit codes."""
    from seo_report import client_loader
    from seo_report.cli_exit import BY_CATEGORY
    from seo_report.google_clients import load_workbook_clients
    registry, sender, sheet_id = _local_inputs(ctx, settings)
    factory = _client_factory(ctx, settings)
    try:
        return registry, load_workbook_clients(factory, spreadsheet_id=sheet_id, registry=registry, default_sender=sender, only=only)
    except client_loader.WorkbookUnavailable as error:
        hint = " Run `setup auth` at an interactive console." if error.category == "auth_required" else ""
        raise CliError(BY_CATEGORY.get(error.category, Exit.INTERNAL_ERROR),
                       f"the reporting workbook could not be read: {error.category}: {error.code}.{hint}", error.code) from error
    except client_loader.WorkbookError as error:
        raise CliError(Exit.CONFIG_INVALID, f"the reporting workbook cannot be used: {error.code}", error.code) from error


def _client_row(run) -> dict:
    client = run.client
    return {"client_key": run.client_key, "name": client.name, "status": "active", "period_id": run.period_id,
            "gsc_property_configured": bool(client.gsc_property), "ga4_property_configured": bool(client.ga4_property_id),
            "keywords": len(client.keywords), "sheet_row": client.sheet_row}


def handle_clients_list(ctx: Context) -> int:
    settings = load_settings(ctx)
    _, loaded = _read_workbook(ctx, settings)
    clients = [_client_row(run) for _, run in sorted(loaded.clients.items())]
    rejected = [{"client_key": r.client_key, "sheet_row": r.sheet_row, "code": r.code} for r in loaded.rejected]
    skipped = [{"client_key": key, "code": code} for key, code in loaded.skipped]
    lines = [f"{len(clients)} client(s) ready, {len(rejected)} rejected, {len(skipped)} skipped"]
    for row in clients:
        lines.append(f"  {row['client_key']:<24} active  {row['period_id']}  gsc={'yes' if row['gsc_property_configured'] else 'NO'}  "
                     f"ga4={'yes' if row['ga4_property_configured'] else 'NO'}  keywords={row['keywords']}  (row {row['sheet_row']})")
    for row in skipped:
        lines.append(f"  {row['client_key']:<24} skipped: {row['code']}")
    for row in rejected:
        lines.append(f"  REJECTED row {row['sheet_row']} ({row['client_key'] or 'unknown client'}): {row['code']}")
    emit(ctx, {"clients": clients, "rejected": rejected, "skipped": skipped}, lines)
    return int(Exit.OK)


def handle_clients_validate(ctx: Context) -> int:
    from seo_report import auth
    settings = load_settings(ctx)
    wanted = ctx.args.client
    registry, _sender, _sheet = _local_inputs(ctx, settings)
    checks = [{"check": "client_registry", "status": "ok", "detail": f"{len(registry)} client(s) configured"},
              {"check": "sender_config", "status": "ok", "detail": "valid"},
              {"check": "reporting_workbook_id", "status": "ok", "detail": "configured"}]
    if wanted is not None:
        if wanted not in {entry.client_key for entry in registry.entries}:
            raise CliError(Exit.CONFIG_INVALID, f"client {wanted} is not in the client registry", "client_unknown")
        checks.append({"check": "client_in_registry", "status": "ok", "detail": wanted})
    description = auth.describe_paths(auth.paths_from_settings(settings, ctx.environ))
    for name in ("client_file", "token_file"):
        state = description[name]
        checks.append({"check": f"oauth_{name}", "status": "ok" if state == "present" else "warning", "detail": state})
    checks.append({"check": "oauth_locations", "status": "ok" if description["outside_project"] else "warning",
                   "detail": f"{description['configuration']}; " + ("outside the project" if description["outside_project"]
                                                                   else "inside the project (migration-compatible fallback)")})
    problems = []
    if ctx.args.online:
        _, loaded = _read_workbook(ctx, settings, only=wanted)
        problems = [{"client_key": r.client_key, "sheet_row": r.sheet_row, "code": r.code} for r in loaded.rejected]
        checks.append({"check": "workbook_rows", "status": "error" if problems else "ok",
                       "detail": f"{len(loaded.clients)} ready, {len(problems)} rejected, {len(loaded.skipped)} skipped"})
    else:
        checks.append({"check": "workbook_rows", "status": "not_checked", "detail": "use --online to read the workbook (read-only)"})
    lines = [f"  [{c['status']:<11}] {c['check']}: {c['detail']}" for c in checks]
    lines += [f"  REJECTED row {p['sheet_row']} ({p['client_key'] or 'unknown client'}): {p['code']}" for p in problems]
    emit(ctx, {"checks": checks, "rejected": problems}, ["Client configuration check (no Google contact)" if not ctx.args.online
                                                          else "Client configuration check (workbook read, read-only)"] + lines)
    return int(Exit.CONFIG_INVALID if problems else Exit.OK)


def handle_verify_live(ctx: Context) -> int:
    """The live verification (R8.7e.1b): contacts Google, read-only, and issues the attestation only after every check succeeded. It is run only on an
    explicit operator command; no other command calls it. Nothing about the evidence can be supplied from here."""
    from seo_report import live_verification
    settings = load_settings(ctx)
    try:
        result = live_verification.run_live_verification(settings, ctx.environ, ctx.args.client, factory=_client_factory(ctx, settings), clock=ctx.extra.get("clock"))
    except live_verification.VerificationFailed as error:
        raise CliError(error.exit_code, error.message, error.code, details={"stage": error.stage, "attestation_issued": False}) from error
    data = result.public()
    lines = [f"Live verification for {result.client_key}: all checks succeeded; the verification attestation was stored (valid until {result.expires_at}).",
             "  " + "; ".join(f"{service} {outcome}" for service, outcome in result.checks)]
    emit(ctx, {"attestation_issued": True, **data}, lines)
    return int(Exit.OK)


def handle_setup_auth(ctx: Context) -> int:
    """Attended sign-in with the configured credential locations (the same ones normal reporting uses). Never run by another command."""
    from seo_report import auth
    settings = load_settings(ctx)
    paths = auth.paths_from_settings(settings, ctx.environ)
    runner = ctx.extra.get("setup_runner", auth.run_setup)
    if auth.path_status(paths.client_file) != "present" and "setup_runner" not in ctx.extra:
        raise CliError(Exit.AUTH_REQUIRED, "the OAuth client file is not available at the configured location; setup cannot start "
                       "(nothing was created or changed)", "oauth_client_file_missing")
    try:
        runner(paths=paths)
    except auth.AuthRequiredError as error:
        raise CliError(Exit.AUTH_REQUIRED, f"setup did not complete ({error.code}); it needs an interactive console of the Windows account "
                       "that runs the reports", error.code) from error
    emit(ctx, {"setup": "completed", "configuration": paths.source},
         [f"Sign-in cached at the configured token location (configuration: {paths.source}). No file contents were shown."])
    return int(Exit.OK)


def deferred_handler(stage: str):
    def handle(ctx: Context) -> int:
        what = " ".join(part for part in ctx.key if part)
        raise CliError(Exit.NOT_AVAILABLE, f"`{what}` is not available yet; it is delivered in {stage}. Nothing was read or changed.",
                       "not_available")
    return handle


def default_handlers() -> dict:
    handlers = {("system-info", None): handle_system_info, ("doctor", None): handle_doctor, ("clients", "list"): handle_clients_list,
                ("clients", "validate"): handle_clients_validate, ("setup", "auth"): handle_setup_auth,
                ("verify", "live"): handle_verify_live}
    from seo_report import cli_report  # imported here: it imports this module
    handlers.update(cli_report.HANDLERS)
    for key, stage in DEFERRED.items():
        handlers[key] = deferred_handler(stage)
    return handlers


# --- entry point --------------------------------------------------------------------------------------------------------------------

def main(argv=None, *, handlers: dict | None = None, environ: dict | None = None, client_factory=None, setup_runner=None,
         preflight_probes: dict | None = None, clock=None, gate=None, services_options: dict | None = None) -> int:
    """Run the CLI and return the exit code. `handlers`, `environ`, `client_factory` (a `GoogleClientFactory` or a fake), `setup_runner`
    (replaces `auth.run_setup`), `preflight_probes` (replacements for the environment probes of the preflight) and `clock` (a UTC `datetime`
    source for the report timestamp) exist for tests and for later sub-stages. `gate` (replaces `preflight.production_gate`) and `services_options`
    (extra `pipeline.Services` fields such as a stub renderer, and an optional `collect`) are **test seams only**: no command-line argument or
    environment variable can reach them, so the production gate cannot be bypassed from outside Python."""
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
        validate_args(parser, args)
    except SystemExit as stop:  # argparse: --help/--version exit 0, usage errors exit 2
        return int(stop.code) if isinstance(stop.code, int) else int(Exit.USAGE)
    ctx = Context(args, dict(os.environ if environ is None else environ), {**default_handlers(), **(handlers or {})})
    if client_factory is not None:
        ctx.extra["client_factory"] = client_factory
    if setup_runner is not None:
        ctx.extra["setup_runner"] = setup_runner
    if preflight_probes is not None:
        ctx.extra["preflight_probes"] = preflight_probes
    if clock is not None:
        ctx.extra["clock"] = clock
    if gate is not None:
        ctx.extra["gate"] = gate
    if services_options is not None:
        ctx.extra["services_options"] = services_options
    handler = ctx.handlers.get(ctx.key)
    if handler is None:  # defensive: every parsed command has a handler
        return fail(ctx, CliError(Exit.INTERNAL_ERROR, "this command has no handler", "no_handler"))
    # Structured logging starts here, once per invocation, after the arguments were accepted: `--help`, `--version` and usage errors returned above and
    # never touch the log folder. It is stopped (handler detached) in the `finally`, whatever happens, including Ctrl+C and SystemExit.
    started = activate_logging(ctx)
    try:
        command = " ".join(part for part in ctx.key if part)
        from contextlib import nullcontext
        scope = nullcontext()
        if started:
            from seo_report import pipeline
            scope = pipeline.log_context(client_key=getattr(args, "client", None) if isinstance(getattr(args, "client", None), str) else None,
                                         period_id=getattr(args, "period", None))
        with scope:
            LOGGER.info("command started", extra={"fields": {"command": command}})
            code = _dispatch(ctx, handler)
            LOGGER.info("command finished", extra={"fields": {"command": command, "exit_code": code}})
            return code
    finally:
        if started:
            from seo_report import runner
            runner.shutdown_logging()


def activate_logging(ctx: Context) -> bool:
    """Start the sanitized JSON-lines log for this command (`runner.configure_logging`, default folder `%LOCALAPPDATA%\\JasperSEOReporting\\logs`).

    Returns True when active. If the folder is unacceptable or unavailable, a safe one-line warning (a fixed code, no path) goes to the console, the
    command continues WITHOUT a log file, and the state is recorded for `doctor` / `system-info` (which report it as blocked / not active)."""
    try:
        from seo_report import runner
        runner.configure_logging(environ=ctx.environ)
    except Exception as error:  # never prevents the command from starting; KeyboardInterrupt/SystemExit are not caught
        code = getattr(error, "code", None)
        code = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code) else "logging_unavailable"
        ctx.extra["logging"] = {"active": False, "code": code}
        print(f"warning: logging is unavailable ({code}); this command continues without a log file", file=sys.stderr)
        return False
    ctx.extra["logging"] = {"active": True, "code": None}
    return True


def _dispatch(ctx: Context, handler) -> int:
    try:
        return int(handler(ctx))
    except CliError as error:
        return fail(ctx, error)
    except KeyboardInterrupt:
        return fail(ctx, CliError(Exit.INTERRUPTED, "interrupted; locks were released and nothing was left half-done", "interrupted"))
    except rc.RunnerConfigError as error:
        return fail(ctx, CliError(Exit.CONFIG_INVALID, f"the configuration is invalid: {error.code}", error.code))
    except contracts.StageError as error:
        from seo_report.cli_exit import BY_CATEGORY
        return fail(ctx, CliError(BY_CATEGORY.get(error.category, Exit.INTERNAL_ERROR), f"{error.category}: {error.code}", error.code))
    except Exception as error:
        from seo_report import ledger as ledger_module
        if isinstance(error, ledger_module.LedgerError):
            return fail(ctx, CliError(exit_code_for_ledger_error(error), f"the ledger reported: {error.code}", error.code))
        LOGGER.exception("unexpected error in the command line interface")
        return fail(ctx, CliError(Exit.INTERNAL_ERROR, f"unexpected internal error ({type(error).__name__}); details are in the log",
                                  "internal_error"))
