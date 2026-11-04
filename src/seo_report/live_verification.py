"""Live Google verification (R8.7e.1b): the one workflow that may issue the verification attestation.

    seo-report verify live --client CLIENT_KEY --live --config <runner.yaml>

**This module contacts Google (read-only) when it is run.** It was written and tested offline; it has not been run against Google yet (R8.7e.2).

The sequence is fixed, and every step stops the run at its first failure:

  1. configuration  the registry, sender, workbook id, the client, and BOTH credential locations (outside the project and cloud-synchronised folders,
                    a cached sign-in present). No network.
  2. credentials    the cached sign-in is loaded; the outcome is `not_needed` (it was valid, nothing was refreshed or written) or `ok` (a refresh was
                    required and succeeded; the refreshed token is written back to the token file by `auth`). A failed refresh stops the run.
  3. workbook       the reporting workbook is read with the existing loader (`load_workbook_clients`: read-only Sheets access, structure validation,
                    per-client rejection codes). The requested client must be eligible; its exact Search Console and GA4 identifiers come from here.
  4. search_console `gsc.verify_access`: the exact property is accessible, and one totals query is answered.
  5. ga4            `ga4.verify_access`: the exact property's metadata exposes the report's fields, and one one-metric report is answered.
  6. evidence       `VerificationEvidence.from_live_checks(...)`, built ONLY from the results above, with timestamps taken from the clock when each step
                    finished. 7. attestation `attestation.issue_attestation(...)`, exactly once, only if every step succeeded.

Provenance rules (the security boundary of this module)
  * The evidence is created inside `run_live_verification` from this run's own results. There is no parameter, command-line option, environment
    variable or file through which evidence, a timestamp or a success flag can be supplied.
  * Nothing is written, and the attestation file is neither created nor replaced, unless every step succeeded and `issue_attestation` accepted the evidence
    (which re-validates it: required checks, freshness within 30 minutes, workbook, scopes, clients).
  * A failed run leaves any existing attestation untouched.
The keyword arguments `factory`, `clock`, `sleep`, `today` and `issue_options` exist for offline tests only; the command line reaches none of them except
through the normal credential-location settings.
"""

import dataclasses
import datetime as dt
import logging
import time

from seo_report import attestation, auth, client_loader, preflight
from seo_report import clients as cl
from seo_report.cli_exit import BY_CATEGORY, Exit
from seo_report.client_config import ClientConfigError, load_client_registry
from seo_report.google_clients import GoogleClientFactory, load_workbook_clients
from seo_report.periods import Period
from seo_report.sources import ga4 as ga4_source
from seo_report.sources import gsc as gsc_source

LOGGER = logging.getLogger("seo_report.live_verification")

STEPS = ("configuration", "credentials", "workbook", "search_console", "ga4", "evidence", "attestation")
VERIFICATION_DAYS = 7  # a short window: the checks prove access, they are not a report
DATA_LAG_DAYS = 3  # Search Console "final" data and GA4 processing lag; the window ends this many days before today


class VerificationFailed(Exception):
    """A verification step failed. `stage` is one of `STEPS`, `code` a fixed identifier, `exit_code` the CLI exit code. Nothing was attested."""

    def __init__(self, stage: str, code: str, exit_code: Exit, message: str):
        super().__init__(message)
        self.stage, self.code, self.exit_code, self.message = stage, code, exit_code, message


@dataclasses.dataclass(frozen=True)
class VerificationResult:
    """What a successful run reports. No token, identifier or property value."""

    client_key: str
    checks: tuple  # ((service, outcome), ...) in the order they ran
    refresh_outcome: str
    search_console_permission: str
    ga4_key_events_supported: bool
    issued_at: str
    expires_at: str

    def public(self) -> dict:
        return {"client_key": self.client_key, "checks": [{"service": s, "outcome": o} for s, o in self.checks], "oauth_refresh": self.refresh_outcome,
                "search_console_permission": self.search_console_permission, "ga4_key_events_supported": self.ga4_key_events_supported,
                "issued_at": self.issued_at, "expires_at": self.expires_at}


def _fail(stage: str, code: str, exit_code: Exit, message: str, cause: BaseException | None = None):
    LOGGER.warning("live verification stopped at %s (%s)", stage, code)
    raise VerificationFailed(stage, code, exit_code, message) from cause


def _classify_failure(stage: str, source: str, error: BaseException):
    """The existing error taxonomy (`source_collector.classify_error`) applied to a Google-facing failure; anything unrecognised stays an internal error."""
    from seo_report import source_collector
    found = source_collector.classify_error(source, error)
    if found is None:
        _fail(stage, "verification_unexpected_error", Exit.INTERNAL_ERROR, f"an unexpected error stopped the verification at {stage} ({type(error).__name__})", error)
    category, code = found
    hint = " Run `setup auth` at an interactive console." if category == "auth_required" else ""
    _fail(stage, code, BY_CATEGORY.get(category, Exit.INTERNAL_ERROR), f"{stage} failed: {category}: {code}.{hint}", error)


def run_live_verification(settings, environ, client_key, *, factory=None, clock=None, sleep=time.sleep, today=None, issue_options=None) -> VerificationResult:
    """Run the sequence above for `client_key` and issue the attestation on complete success. Raises `VerificationFailed` otherwise."""
    clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    # 1. configuration ---------------------------------------------------------------------------------------------------------------------------
    try:
        registry = load_client_registry(settings.clients_config_path)
    except (ClientConfigError, OSError) as error:
        _fail("configuration", "client_registry_invalid", Exit.CONFIG_INVALID, "the client registry is missing or invalid", error)
    if client_key not in {entry.client_key for entry in registry.entries}:
        _fail("configuration", "client_unknown", Exit.CONFIG_INVALID, f"client {client_key} is not in the client registry")
    try:
        sender = cl.load_default_sender(settings.sender_config_path)
    except (cl.SheetDataError, OSError, ValueError) as error:
        _fail("configuration", "sender_config_invalid", Exit.CONFIG_INVALID, "the sender configuration is missing or invalid", error)
    if not settings.reporting_spreadsheet_id:
        _fail("configuration", "reporting_workbook_not_configured", Exit.CONFIG_INVALID, "workbooks.reporting_spreadsheet_id is not configured")
    findings = preflight.check_authentication(preflight.Inputs(settings=settings, environ=environ))
    if not preflight.credentials_relocated(settings, findings):
        _fail("configuration", "credentials_not_relocated", Exit.PREFLIGHT_FAILED,
              "both OAuth locations must be configured outside the project and any cloud-synchronised folder before verifying")
    if "cached_sign_in_present" not in {f.code for f in findings}:
        _fail("configuration", "auth_token_missing", Exit.AUTH_REQUIRED, "no cached Google sign-in exists at the configured location. Run `setup auth` at an interactive console.")
    factory = factory or GoogleClientFactory(paths=auth.paths_from_settings(settings, environ))

    # 2. credentials -----------------------------------------------------------------------------------------------------------------------------
    try:
        _credentials, refresh_outcome = factory.credentials_with_status()
    except Exception as error:  # classified; nothing is swallowed
        _classify_failure("credentials", "oauth", error)
    if refresh_outcome not in (auth.REFRESH_NOT_NEEDED, auth.REFRESH_OK):  # unknown is not "not needed"
        _fail("credentials", "oauth_refresh_outcome_unknown", Exit.INTERNAL_ERROR, "the sign-in refresh outcome could not be established")
    oauth_done = clock()

    # 3. workbook (the existing loader; read-only) -----------------------------------------------------------------------------------------------
    try:
        loaded = load_workbook_clients(factory, spreadsheet_id=settings.reporting_spreadsheet_id, registry=registry, default_sender=sender, only=client_key)
    except client_loader.WorkbookUnavailable as error:
        _fail("workbook", error.code, BY_CATEGORY.get(error.category, Exit.INTERNAL_ERROR), f"the reporting workbook could not be read: {error.category}: {error.code}", error)
    except client_loader.WorkbookError as error:
        _fail("workbook", error.code, Exit.CONFIG_INVALID, f"the reporting workbook cannot be used: {error.code}", error)
    except Exception as error:
        _classify_failure("workbook", "sheets", error)
    run = loaded.clients.get(client_key)
    if run is None or run.client is None:
        reason = next((r.code for r in loaded.rejected if r.client_key == client_key), None) or dict(loaded.skipped).get(client_key) or "client_not_in_workbook"
        _fail("workbook", reason, Exit.CONFIG_INVALID, f"client {client_key} is not eligible in the reporting workbook ({reason})")
    gsc_property, ga4_property = run.client.gsc_property, run.client.ga4_property_id  # the exact values the gate will compare later
    workbook_done = clock()

    # 4. Search Console ----------------------------------------------------------------------------------------------------------------------------
    end = (today or clock().date()) - dt.timedelta(days=DATA_LAG_DAYS)
    window = Period(end - dt.timedelta(days=VERIFICATION_DAYS - 1), end)
    if not gsc_source.is_valid_property(gsc_property):
        _fail("search_console", "gsc_property_invalid", Exit.CONFIG_INVALID, "the Search Console property in the workbook is not a URL-prefix or sc-domain property")
    try:
        gsc_result = gsc_source.verify_access(factory.search_console(), gsc_property, window, sleep=sleep)
    except Exception as error:
        _classify_failure("search_console", "gsc", error)
    gsc_done = clock()

    # 5. GA4 -------------------------------------------------------------------------------------------------------------------------------------
    try:
        ga4_result = ga4_source.verify_access(factory.analytics(), ga4_property, window, sleep=sleep)
    except Exception as error:
        _classify_failure("ga4", "ga4", error)
    ga4_done = clock()

    # 6. evidence: only from the results above ------------------------------------------------------------------------------------------------
    if not (oauth_done <= workbook_done <= gsc_done <= ga4_done):
        _fail("evidence", "verification_order_invalid", Exit.INTERNAL_ERROR, "the checks did not complete in the required order")
    checks = [attestation.CheckResult("oauth_refresh", refresh_outcome, oauth_done), attestation.CheckResult("sheets_read", "ok", workbook_done),
              attestation.CheckResult("search_console_read", "ok", gsc_done), attestation.CheckResult("ga4_read", "ok", ga4_done)]
    evidence = attestation.VerificationEvidence.from_live_checks(
        checks=checks, workbook_id=settings.reporting_spreadsheet_id, clients=[attestation.ClientEvidence(client_key, gsc_property, ga4_property)])

    # 7. attestation: exactly once, only now ------------------------------------------------------------------------------------------------------
    try:
        verdict = attestation.issue_attestation(evidence, settings, environ=environ, now=clock(), **(issue_options or {}))
    except attestation.AttestationError as error:
        _fail("attestation", error.code, Exit.PREFLIGHT_FAILED, f"the verification attestation was not issued: {error.code}", error)
    LOGGER.info("live verification succeeded; the attestation was issued")
    return VerificationResult(client_key, tuple((c.service, c.outcome) for c in checks), refresh_outcome, gsc_result["permission"],
                              bool(ga4_result["key_events_supported"]), verdict.issued_at, verdict.expires_at)
