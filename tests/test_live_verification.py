"""R8.7e.1b: the live-verification workflow, tested OFFLINE with fake Google services.

Nothing here contacts Google: sockets and the sign-in flow are forbidden (the autouse fixture of test_attestation), every service is a fake, every credential
is a synthetic object, and every attestation is written to a temporary `%LOCALAPPDATA%` (a fake protector, or the real Windows DPAPI over synthetic data in one
end-to-end test). The workflow has NEVER been run against Google; that is R8.7e.2.
"""
import dataclasses
import datetime as dt
import inspect
import json
import os
import sys
from pathlib import Path

import pytest
from google.api_core import exceptions as api_exc
from google.auth import exceptions as ge
from google.oauth2.credentials import Credentials

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import attestation as att
from seo_report import auth, cli, live_verification as lv
from seo_report import google_clients as gc
from seo_report import preflight as pf
from seo_report.cli_exit import Exit
from seo_report.periods import Period
from seo_report.sources import ga4 as ga4_source
from seo_report.sources import gsc as gsc_source
from test_attestation import ACL, IDENT, SEAMS, FakeProtector, nothing_leaves_the_machine, stored  # noqa: F401  (the autouse guard is imported on purpose)
from test_auth_noninteractive import FUTURE, PAST, SECRETS, default_paths_are_never_used, no_browser, refreshed, token, write_token  # noqa: F401
from test_client_loader import CLIENT_HEADER, KEYWORD_HEADER, client_row, keyword_row
from test_ga4 import FakeClient, metadata, response
from test_gsc import FakeService, default_handler, http_error
from test_r86b_credentials_and_clients import FakeSheets, alpha_rows
from test_r86d_report_status_recovery import PROBES_OK, home  # noqa: F401  (home is a fixture)

UTC = dt.timezone.utc
GSC_PROPERTY, GA4_PROPERTY = "https://alpha.test/", "111111111"  # what client_row("Alpha Co") carries
ISSUE = dict(protector=FakeProtector(), identity=IDENT, acl_reader=ACL, acl_applier=lambda *a: None)
WINDOW = Period(dt.date(2026, 10, 20), dt.date(2026, 10, 26))


class Ticker:
    """A clock on real time that moves one second per reading (the attestation checks file times against the real clock)."""

    def __init__(self, start=None, step=1.0):
        self.now = start or dt.datetime.now(UTC).replace(microsecond=0)
        self.step = dt.timedelta(seconds=step)

    def __call__(self):
        value, self.now = self.now, self.now + self.step
        return value


class OrderedSheets(FakeSheets):
    def __init__(self, order, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order = order

    def execute(self):
        self.order.append("sheets_read")
        return super().execute()


class OrderedGsc(FakeService):
    def __init__(self, order, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order = order

    def list(self):
        self.order.append("gsc_sites")
        return super().list()


class OrderedGa4(FakeClient):
    def __init__(self, order, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order = order

    def get_metadata(self, request=None, **kwargs):
        self.order.append("ga4_metadata")
        return super().get_metadata(request, **kwargs)

    def run_report(self, request):
        self.order.append("ga4_report")
        return super().run_report(request)


def gsc_handler(order):
    def handler(body):
        order.append("gsc_query")
        return default_handler(body)
    return handler


def ga4_handler(order=None):
    def handler(request):
        return response([], ["sessions"], [([], [5])])
    return handler


def fakes(order, *, rows=None, sites=None, status="not_needed", cred_error=None):
    sheets = OrderedSheets(order, *(rows or alpha_rows()))
    gsc = OrderedGsc(order, gsc_handler(order), sites=sites if sites is not None else {"siteEntry": [{"siteUrl": GSC_PROPERTY, "permissionLevel": "siteOwner"}]})
    ga4 = OrderedGa4(order, ga4_handler())

    def provider():
        order.append("credentials")
        if cred_error is not None:
            raise cred_error
        return object(), status
    factory = gc.GoogleClientFactory(credentials_status_provider=provider, sheets_builder=lambda c: order.append("sheets_client") or sheets,
                                     gsc_builder=lambda c: order.append("gsc_client") or gsc, ga4_builder=lambda c: order.append("ga4_client") or ga4)
    return factory, sheets, gsc, ga4


def run(home, factory, *, clock=None, **kw):
    return lv.run_live_verification(home.settings, home.env, "alpha-co", factory=factory, clock=clock or Ticker(), sleep=lambda s: None,
                                    issue_options=ISSUE, **kw)


@pytest.fixture
def issued(monkeypatch):
    """Counts the calls of `attestation.issue_attestation` and keeps the evidence it was given (the real function still runs)."""
    calls = []
    real = att.issue_attestation

    def counting(evidence, settings, **kw):
        calls.append(evidence)
        return real(evidence, settings, **kw)
    monkeypatch.setattr(att, "issue_attestation", counting)
    return calls


def failed(home, factory, **kw):
    with pytest.raises(lv.VerificationFailed) as caught:
        run(home, factory, **kw)
    return caught.value


# === A. authentication: the refresh outcome =======================================================================================

def test_valid_cached_credentials_report_not_needed_without_a_refresh_or_a_write(token, no_browser, monkeypatch):
    write_token(token, expiry=FUTURE)
    before = token.read_bytes()
    monkeypatch.setattr(Credentials, "refresh", lambda *a: (_ for _ in ()).throw(AssertionError("no refresh expected")))
    creds, outcome = auth.get_credentials_with_status(token_path=token)
    assert creds.valid and outcome == "not_needed" == auth.REFRESH_NOT_NEEDED and token.read_bytes() == before and not no_browser


def test_expired_credentials_with_a_successful_refresh_report_ok_and_are_cached(token, no_browser, monkeypatch):
    write_token(token, expiry=PAST)
    monkeypatch.setattr(Credentials, "refresh", refreshed)
    creds, outcome = auth.get_credentials_with_status(token_path=token)
    assert creds.valid and outcome == "ok" == auth.REFRESH_OK
    assert json.loads(token.read_text(encoding="utf-8"))["token"] == "SYNTHETIC-NEW-TOKEN" and not no_browser  # the documented token-file write


@pytest.mark.parametrize("raised,code", [(ge.RefreshError("x", {"error": "invalid_grant"}), "auth_credentials_revoked"), (ge.RefreshError("x"), "auth_refresh_failed")])
def test_a_failed_refresh_is_an_error_never_an_outcome(token, no_browser, monkeypatch, raised, code):
    write_token(token, expiry=PAST)
    monkeypatch.setattr(Credentials, "refresh", lambda self, request: (_ for _ in ()).throw(raised))
    with pytest.raises(auth.AuthRequiredError) as caught:
        auth.get_credentials_with_status(token_path=token)
    assert caught.value.code == code and not any(secret in str(caught.value) for secret in SECRETS) and not no_browser


def test_a_refresh_that_leaves_the_credentials_invalid_is_refused_with_status(token, no_browser, monkeypatch):
    write_token(token, expiry=PAST)
    monkeypatch.setattr(Credentials, "refresh", lambda self, request: None)
    with pytest.raises(auth.AuthRequiredError) as caught:
        auth.get_credentials_with_status(token_path=token)
    assert caught.value.code == "auth_refresh_failed"


def test_existing_callers_keep_their_behaviour_and_signature(token, no_browser):
    assert list(inspect.signature(auth.get_credentials).parameters) == ["token_path", "refresh_request", "paths"]
    assert list(inspect.signature(auth.get_credentials_with_status).parameters) == ["token_path", "refresh_request", "paths"]
    write_token(token, expiry=FUTURE)
    assert isinstance(auth.get_credentials(token_path=token), Credentials)  # still only credentials


def test_the_factory_reports_the_outcome_only_when_asked_and_credentials_loaded_first_have_no_known_outcome(monkeypatch):
    seen = []
    monkeypatch.setattr(auth, "get_credentials", lambda **kw: seen.append("plain") or "CREDS")
    monkeypatch.setattr(auth, "get_credentials_with_status", lambda **kw: seen.append("status") or ("CREDS", "ok"))
    first = gc.GoogleClientFactory(paths=object(), sheets_builder=lambda c: c)
    assert first.sheets() == "CREDS" and seen == ["plain"]  # every other caller still uses get_credentials
    assert first.credentials_with_status() == ("CREDS", None)  # loaded before the question was asked: unknown, never guessed
    second = gc.GoogleClientFactory(paths=object())
    assert second.credentials_with_status() == ("CREDS", "ok") and seen == ["plain", "status"]


# === B. Search Console: the minimal read-only verification ==========================================================================

def gsc_service(sites=None, handler=default_handler):
    return FakeService(handler, sites={"siteEntry": [{"siteUrl": GSC_PROPERTY, "permissionLevel": "siteOwner"}]} if sites is None else sites)


def test_search_console_accessible_property_and_one_totals_query():
    service = gsc_service()
    result = gsc_source.verify_access(service, GSC_PROPERTY, WINDOW, sleep=lambda s: None)
    assert result == {"permission": "siteOwner", "period_days": 7, "rows": 1}
    assert service.list_calls == 1 and len(service.calls) == 1
    site, body = service.calls[0]
    assert site == GSC_PROPERTY and "dimensions" not in body and body["startDate"] == "2026-10-20" and body["endDate"] == "2026-10-26"  # totals only


def test_search_console_domain_property_and_a_quiet_property_are_valid():
    domain = "sc-domain:alpha.test"
    service = gsc_service({"siteEntry": [{"siteUrl": domain, "permissionLevel": "siteFullUser"}]}, lambda body: {})
    assert gsc_source.verify_access(service, domain, WINDOW, sleep=lambda s: None) == {"permission": "siteFullUser", "period_days": 7, "rows": 0}


@pytest.mark.parametrize("sites", [{"siteEntry": [{"siteUrl": "https://other.test/", "permissionLevel": "siteOwner"}]}, {},
                                   {"siteEntry": [{"siteUrl": GSC_PROPERTY, "permissionLevel": "siteUnverifiedUser"}]}])
def test_search_console_property_not_accessible(sites):
    service = gsc_service(sites)
    with pytest.raises(gsc_source.SearchConsoleError):
        gsc_source.verify_access(service, GSC_PROPERTY, WINDOW, sleep=lambda s: None)
    assert service.calls == []  # no query was made for a property the account cannot use


@pytest.mark.parametrize("prop", ["", None, "alpha.test", "ftp://alpha.test/", "sc-domain:", "https://", 123, "has space"])
def test_search_console_invalid_or_missing_property(prop):
    service = gsc_service()
    with pytest.raises(gsc_source.SearchConsoleError):
        gsc_source.verify_access(service, prop, WINDOW, sleep=lambda s: None)
    assert service.list_calls == 0 and service.calls == []


def test_search_console_query_failure_keeps_the_existing_error_handling():
    def broken(body):
        raise http_error(403)
    with pytest.raises(gsc_source.SearchConsoleError) as caught:
        gsc_source.verify_access(gsc_service(handler=broken), GSC_PROPERTY, WINDOW, sleep=lambda s: None)
    assert caught.value.status == 403 and "denied access" in str(caught.value)
    attempts = []

    def flaky(body):
        attempts.append(1)
        if len(attempts) < 3:
            raise http_error(503)
        return {"rows": []}
    assert gsc_source.verify_access(gsc_service(handler=flaky), GSC_PROPERTY, WINDOW, sleep=lambda s: None)["rows"] == 0 and len(attempts) == 3  # retried like the report


def test_search_console_verification_has_no_write_operation():
    source = inspect.getsource(gsc_source.verify_access)
    assert "searchanalytics().query" not in source and all(word not in source for word in (".insert(", ".delete(", ".add(", ".patch(", ".update("))
    service = gsc_service()
    gsc_source.verify_access(service, GSC_PROPERTY, WINDOW, sleep=lambda s: None)
    assert not hasattr(service, "insert") and len(service.calls) == 1


# === C. GA4: the minimal read-only verification =====================================================================================

def ga4_client(handler=None, meta=None):
    return FakeClient(handler or (lambda request: response([], ["sessions"], [([], [5])])), meta)


def test_ga4_metadata_and_one_small_report():
    client = ga4_client()
    result = ga4_source.verify_access(client, GA4_PROPERTY, WINDOW, sleep=lambda s: None)
    assert result == {"key_events_supported": True, "period_days": 7, "rows": 1} and client.metadata_calls == 1 and len(client.requests) == 1
    request = client.requests[0]
    assert request.property == f"properties/{GA4_PROPERTY}" and [m.name for m in request.metrics] == ["sessions"] and list(request.dimensions) == []
    assert request.date_ranges[0].start_date == "2026-10-20" and request.date_ranges[0].end_date == "2026-10-26"


def test_ga4_property_without_the_key_events_metric_is_still_verified():
    meta = metadata(metrics=["sessions", "totalUsers", "engagementRate", "engagedSessions"])
    assert ga4_source.verify_access(ga4_client(meta=meta), GA4_PROPERTY, WINDOW, sleep=lambda s: None)["key_events_supported"] is False


@pytest.mark.parametrize("prop", ["", None, "abc", "12 34", "properties/1", "-5", "1" * 21])
def test_ga4_invalid_or_missing_property(prop):
    client = ga4_client()
    with pytest.raises(ga4_source.Ga4Error):
        ga4_source.verify_access(client, prop, WINDOW, sleep=lambda s: None)
    assert client.metadata_calls == 0 and client.requests == []


def test_ga4_metadata_that_lacks_required_fields_or_fails():
    with pytest.raises(ga4_source.Ga4Error) as caught:
        ga4_source.verify_access(ga4_client(meta=metadata(metrics=["sessions"])), GA4_PROPERTY, WINDOW, sleep=lambda s: None)
    assert "required fields" in str(caught.value)

    class Denied(FakeClient):
        def get_metadata(self, request=None, **kwargs):
            raise api_exc.PermissionDenied("denied")
    client = Denied(lambda r: response([], ["sessions"], []))
    with pytest.raises(ga4_source.Ga4Error) as caught:
        ga4_source.verify_access(client, GA4_PROPERTY, WINDOW, sleep=lambda s: None)
    assert caught.value.status == 403 and client.requests == []  # no report after a failed metadata call


def test_ga4_report_failure_keeps_the_existing_error_handling():
    def broken(request):
        raise api_exc.NotFound("missing")
    with pytest.raises(ga4_source.Ga4Error) as caught:
        ga4_source.verify_access(ga4_client(broken), GA4_PROPERTY, WINDOW, sleep=lambda s: None)
    assert caught.value.status == 404


def test_ga4_verification_has_no_write_operation():
    source = inspect.getsource(ga4_source.verify_access)
    assert all(word not in source for word in ("create_", "update_", "delete_", "archive", "provision"))
    client = ga4_client()
    ga4_source.verify_access(client, GA4_PROPERTY, WINDOW, sleep=lambda s: None)
    assert sorted(name for name in dir(client) if not name.startswith("_") and name != "handler" and callable(getattr(client, name))) == ["get_metadata", "run_report"]


# === D. the orchestration ==========================================================================================================

EXPECTED_ORDER = ["credentials", "sheets_client", "sheets_read", "sheets_read", "gsc_client", "gsc_sites", "gsc_query", "ga4_client", "ga4_metadata", "ga4_report"]


def test_all_checks_succeed_in_the_required_order_and_the_attestation_is_issued_once(home, issued):
    order = []
    factory, sheets, gsc, ga4 = fakes(order)
    result = run(home, factory)
    assert order == EXPECTED_ORDER  # credentials, then workbook (both tabs), then Search Console, then GA4
    assert [c for c, _ in result.checks] == ["oauth_refresh", "sheets_read", "search_console_read", "ga4_read"] and all(o in ("ok", "not_needed") for _, o in result.checks)
    assert result.client_key == "alpha-co" and result.refresh_outcome == "not_needed" and result.search_console_permission == "siteOwner"
    assert len(issued) == 1  # exactly one issuing call
    assert att.evaluate(home.settings, home.env, now=Ticker()(), **SEAMS).valid
    assert sheets.calls == 2 and len(gsc.calls) == 1 and len(ga4.requests) == 1  # two tab reads, one totals query, one report: nothing more


def test_the_evidence_is_built_from_the_actual_results_with_internal_timestamps(home, issued):
    clock = Ticker()
    start = clock.now
    order = []
    run(home, fakes(order)[0], clock=clock)
    evidence = issued[0]
    assert evidence.origin == "live" and evidence.workbook_id == home.settings.reporting_spreadsheet_id
    assert [(c.service, c.outcome) for c in evidence.checks] == [("oauth_refresh", "not_needed"), ("sheets_read", "ok"), ("search_console_read", "ok"), ("ga4_read", "ok")]
    times = [c.completed_at for c in evidence.checks]
    assert times == sorted(times) and all(start <= t < clock.now and t.tzinfo is not None for t in times)  # taken from the clock, in order, none in the future
    assert max(times) - min(times) < att.EVIDENCE_WINDOW
    assert evidence.clients == (att.ClientEvidence("alpha-co", GSC_PROPERTY, GA4_PROPERTY),)  # the exact strings the loader parsed
    assert tuple(sorted(evidence.scopes)) == tuple(sorted(auth.SCOPES))


def test_a_refresh_outcome_of_ok_is_recorded_as_ok(home, issued):
    result = run(home, fakes([], status="ok")[0])
    assert dict(result.checks)["oauth_refresh"] == "ok" and result.refresh_outcome == "ok"


def test_the_exact_identifiers_from_the_workbook_reach_the_right_verification(home):
    order = []
    factory, sheets, gsc, ga4 = fakes(order)
    run(home, factory)
    assert gsc.calls[0][0] == GSC_PROPERTY and ga4.requests[0].property == f"properties/{GA4_PROPERTY}"


@pytest.mark.parametrize("status", [None, "failed", "", "OK", True])
def test_an_unknown_or_unsuccessful_oauth_outcome_stops_everything(home, issued, status):
    order = []
    error = failed(home, fakes(order, status=status)[0])
    assert (error.stage, error.code) == ("credentials", "oauth_refresh_outcome_unknown") and order == ["credentials"] and issued == [] and not stored(home).exists()


def test_oauth_failure_prevents_every_later_check(home, issued):
    order = []
    error = failed(home, fakes(order, cred_error=auth.AuthRequiredError("auth_credentials_revoked"))[0])
    assert (error.stage, error.code, error.exit_code) == ("credentials", "auth_credentials_revoked", Exit.AUTH_REQUIRED)
    assert order == ["credentials"] and issued == [] and not stored(home).exists()
    assert "Run `setup auth`" in error.message and not any(secret in error.message for secret in SECRETS)


def test_a_network_failure_during_the_refresh_is_a_temporary_source_failure_not_a_pass(home, issued):
    error = failed(home, fakes([], cred_error=auth.AuthTemporaryError())[0])
    assert (error.stage, error.code, error.exit_code) == ("credentials", "auth_network_error", Exit.SOURCE_UNAVAILABLE) and issued == []


def test_workbook_failures_stop_before_search_console_ga4_and_the_attestation(home, issued):
    cases = {
        "missing tab (the API rejects the range)": dict(rows=None, error=http_error(400)),
        "no access": dict(rows=None, error=http_error(403)),
        "invalid structure (a required column is gone)": dict(rows=([[h for h in CLIENT_HEADER if h != "GSC Property"], client_row("Alpha Co")], [KEYWORD_HEADER, keyword_row("Alpha Co")])),
        "no eligible client": dict(rows=([CLIENT_HEADER, client_row("Beta Co")], [KEYWORD_HEADER, keyword_row("Beta Co")])),
        "client paused": dict(rows=([CLIENT_HEADER, client_row("Alpha Co", Status="Paused")], [KEYWORD_HEADER, keyword_row("Alpha Co")])),
        "client rejected": dict(rows=([CLIENT_HEADER, client_row("Alpha Co", **{"GA4 Property ID": ""})], [KEYWORD_HEADER, keyword_row("Alpha Co")]))}
    for label, spec in cases.items():
        order = []
        factory, sheets, gsc, ga4 = fakes(order, rows=spec.get("rows") or alpha_rows())
        if "error" in spec:
            sheets.error = spec["error"]
        error = failed(home, factory)
        assert error.stage == "workbook", label
        assert "gsc_client" not in order and "ga4_client" not in order and issued == [] and not stored(home).exists(), label


def test_the_requested_client_is_identified_in_the_workbook(home, issued):
    rows = ([CLIENT_HEADER, client_row("Beta Co", **{"GSC Property": "https://beta.test/"}), client_row("Alpha Co")],
            [KEYWORD_HEADER, keyword_row("Beta Co"), keyword_row("Alpha Co")])
    order = []
    result = run(home, fakes(order, rows=rows)[0])
    assert result.client_key == "alpha-co" and issued[0].clients[0].gsc_property == GSC_PROPERTY  # not the other client's property


def test_a_client_that_is_not_in_the_registry_or_a_missing_workbook_id_stops_before_google(home, issued):
    order = []
    with pytest.raises(lv.VerificationFailed) as caught:
        lv.run_live_verification(home.settings, home.env, "zeta-co", factory=fakes(order)[0], clock=Ticker(), issue_options=ISSUE)
    assert (caught.value.stage, caught.value.code) == ("configuration", "client_unknown") and order == []
    no_workbook = dataclasses.replace(home.settings, reporting_spreadsheet_id=None)
    with pytest.raises(lv.VerificationFailed) as caught:
        lv.run_live_verification(no_workbook, home.env, "alpha-co", factory=fakes(order)[0], clock=Ticker(), issue_options=ISSUE)
    assert caught.value.code == "reporting_workbook_not_configured" and order == [] and issued == []


def test_credentials_that_are_not_relocated_or_missing_stop_before_google(home, issued, monkeypatch):
    order = []
    monkeypatch.setattr(auth, "TOKEN_PATH", ROOT / "zz_unused_project_folder" / "t.json")  # the fallback locations lie inside the project (never created or opened)
    monkeypatch.setattr(auth, "CLIENT_SECRET_PATH", ROOT / "zz_unused_project_folder" / "c.json")
    inside = dataclasses.replace(home.settings, oauth_client_path=None, oauth_cache_path=None)  # the in-project fallback
    with pytest.raises(lv.VerificationFailed) as caught:
        lv.run_live_verification(inside, home.env, "alpha-co", factory=fakes(order)[0], clock=Ticker(), issue_options=ISSUE)
    assert (caught.value.stage, caught.value.code, caught.value.exit_code) == ("configuration", "credentials_not_relocated", Exit.PREFLIGHT_FAILED) and order == []
    (home.creds / "login.json").unlink()
    with pytest.raises(lv.VerificationFailed) as caught:
        lv.run_live_verification(home.settings, home.env, "alpha-co", factory=fakes(order)[0], clock=Ticker(), issue_options=ISSUE)
    assert (caught.value.code, caught.value.exit_code) == ("auth_token_missing", Exit.AUTH_REQUIRED) and order == [] and issued == []


def test_search_console_failure_prevents_ga4_and_the_attestation(home, issued):
    for label, kwargs in {"property not accessible": dict(sites={"siteEntry": []}), "invalid property": dict(rows=(
            [CLIENT_HEADER, client_row("Alpha Co", **{"GSC Property": "not a property"})], [KEYWORD_HEADER, keyword_row("Alpha Co")]))}.items():
        order = []
        error = failed(home, fakes(order, **kwargs)[0])
        assert error.stage == "search_console" and "ga4_client" not in order and issued == [] and not stored(home).exists(), label
    assert error.code == "gsc_property_invalid" and error.exit_code == Exit.CONFIG_INVALID
    order = []
    factory, _, gsc, _ = fakes(order, sites={"siteEntry": []})
    error = failed(home, factory)
    assert (error.code, error.exit_code) == ("gsc_property_not_accessible", Exit.SOURCE_UNAVAILABLE) and gsc.calls == []


def test_ga4_failure_prevents_the_attestation(home, issued):
    for label, build in {"denied": lambda f: setattr(f[3], "handler", lambda r: (_ for _ in ()).throw(api_exc.PermissionDenied("denied"))),
                         "incomplete metadata": lambda f: setattr(f[3], "meta", metadata(metrics=["sessions"]))}.items():
        order = []
        built = fakes(order)
        build(built)
        error = failed(home, built[0])
        assert error.stage == "ga4" and issued == [] and not stored(home).exists(), label
        assert order[-1] in ("ga4_report", "ga4_metadata")


def test_an_evidence_validation_failure_prevents_the_attestation(home, issued, monkeypatch):
    real = att.VerificationEvidence.from_live_checks
    cases = {"another workbook": lambda **kw: real(**{**kw, "workbook_id": "1ZzYyXxWwVvUuTtSsRrQqPpOoNnMmLlKkJjIiHh"}),
             "no clients": lambda **kw: real(**{**kw, "clients": []}),
             "a missing check": lambda **kw: real(**{**kw, "checks": kw["checks"][:3]}),
             "a failed check": lambda **kw: real(**{**kw, "checks": [dataclasses.replace(c, outcome="failed") if c.service == "ga4_read" else c for c in kw["checks"]]}),
             "wrong scopes": lambda **kw: real(**{**kw, "scopes": ["https://www.googleapis.com/auth/drive"]})}
    for label, bad in cases.items():
        monkeypatch.setattr(att.VerificationEvidence, "from_live_checks", classmethod(lambda cls, **kw: bad(**kw)))
        error = failed(home, fakes([])[0])
        assert error.stage == "attestation" and not stored(home).exists(), label
        assert error.code in ("attestation_incomplete_evidence", "attestation_workbook_changed", "attestation_scopes_changed"), label


def test_a_changed_client_or_property_in_the_evidence_never_matches_what_the_gate_later_expects(home, issued, monkeypatch):
    order = []
    run(home, fakes(order)[0])
    verdict = att.evaluate(home.settings, home.env, now=Ticker()(), **SEAMS)
    assert att.client_coverage_code(verdict, "alpha-co", GSC_PROPERTY, GA4_PROPERTY) is None
    assert att.client_coverage_code(verdict, "alpha-co", "https://changed.test/", GA4_PROPERTY) == "attestation_property_changed"
    assert att.client_coverage_code(verdict, "alpha-co", GSC_PROPERTY, "222222222") == "attestation_property_changed"
    assert att.client_coverage_code(verdict, "beta-co", GSC_PROPERTY, GA4_PROPERTY) == "attestation_client_not_covered"


def test_future_or_out_of_order_timestamps_are_never_attested(home, issued):
    class Sequence:
        def __init__(self, offsets):
            self.base, self.offsets, self.n = dt.datetime.now(UTC).replace(microsecond=0), list(offsets), 0

        def __call__(self):
            value = self.base + dt.timedelta(seconds=self.offsets[min(self.n, len(self.offsets) - 1)])
            self.n += 1
            return value
    # the checks are timed at +0..+4 s but the last reading (the moment of issuing) is an hour EARLIER: the evidence would be "from the future"
    error = failed(home, fakes([])[0], clock=Sequence([0, 1, 2, 3, 4, -3600]))
    assert (error.stage, error.code) == ("attestation", "attestation_incomplete_evidence") and not stored(home).exists()
    assert len(issued) == 1  # issue_attestation was called, refused the evidence before writing anything
    error = failed(home, fakes([])[0], clock=Sequence([100, 90, 95, 99, 101, 102]))  # time runs backwards between the first two checks
    assert (error.stage, error.code) == ("evidence", "verification_order_invalid") and not stored(home).exists()


def test_checks_outside_the_thirty_minute_window_are_not_attested(home, issued):
    class Slow:
        def __init__(self):
            self.base, self.n = dt.datetime.now(UTC).replace(microsecond=0), 0

        def __call__(self):
            self.n += 1
            return self.base + dt.timedelta(minutes=20 * self.n)  # every reading is twenty minutes later: the whole run takes hours
    error = failed(home, fakes([])[0], clock=Slow())
    assert (error.stage, error.code) == ("attestation", "attestation_incomplete_evidence") and not stored(home).exists()


def test_a_failed_run_never_creates_overwrites_or_touches_an_existing_attestation(home, issued):
    run(home, fakes([])[0])
    before, stamp = stored(home).read_bytes(), os.stat(stored(home)).st_mtime_ns
    for kwargs in (dict(cred_error=auth.AuthRequiredError("auth_credentials_revoked")), dict(sites={"siteEntry": []}), dict(rows=([CLIENT_HEADER], [KEYWORD_HEADER]))):
        with pytest.raises(lv.VerificationFailed):
            run(home, fakes([], **kwargs)[0])
        assert stored(home).read_bytes() == before and os.stat(stored(home)).st_mtime_ns == stamp
        assert [p.name for p in stored(home).parent.iterdir()] == [att.FILE_NAME]
    assert len(issued) == 1  # only the first, successful run ever reached issue_attestation


def test_issue_attestation_is_never_called_on_any_failure_path(home, issued):
    for kwargs in (dict(cred_error=auth.AuthRequiredError("auth_token_missing")), dict(status=None), dict(sites={"siteEntry": []}),
                   dict(rows=([CLIENT_HEADER, client_row("Beta Co")], [KEYWORD_HEADER, keyword_row("Beta Co")]))):
        with pytest.raises(lv.VerificationFailed):
            run(home, fakes([], **kwargs)[0])
    assert issued == [] and not stored(home).exists()


def test_the_workflow_accepts_no_externally_supplied_evidence_or_success_flag(home, issued, monkeypatch):
    allowed = {"settings", "environ", "client_key", "factory", "clock", "sleep", "today", "issue_options"}
    assert set(inspect.signature(lv.run_live_verification).parameters) == allowed  # nothing named evidence, checks, timestamp, outcome, verified ...
    env = {**home.env, "JASPER_EVIDENCE": '{"origin": "live"}', "JASPER_LIVE_VERIFIED": "1", "JASPER_SKIP_VERIFICATION": "1"}
    with pytest.raises(lv.VerificationFailed):
        lv.run_live_verification(home.settings, env, "alpha-co", factory=fakes([], sites={"siteEntry": []})[0], clock=Ticker(), issue_options=ISSUE)
    assert issued == [] and not stored(home).exists()  # variables that look like evidence or a bypass change nothing
    source = (ROOT / "src" / "seo_report" / "live_verification.py").read_text(encoding="utf-8")
    assert "environ.get" not in source and "os.environ" not in source and "argparse" not in source and "json.load" not in source


# === E. the command line ============================================================================================================

def cli_run(home, argv, **kw):
    return cli.main(["--json"] + argv + ["--config", str(home.path)], environ=home.env, preflight_probes={**PROBES_OK}, clock=lambda: dt.datetime.now(UTC), **kw)


def test_the_command_is_registered_and_its_help_is_offline_and_explicit(monkeypatch, capsys):
    monkeypatch.setattr(lv, "run_live_verification", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the live verification must not run for help")))
    assert ("verify", "live") in cli.default_handlers()
    text = ""
    for argv in (["--help"], ["verify", "--help"], ["verify", "live", "--help"]):
        assert cli.main(argv, environ={}) == 0
        text += capsys.readouterr().out
    assert "verify" in text and "LIVE" in text and "contact Google" in text and "--live" in text and "attestation" in text and "--client" in text


def test_the_command_needs_the_explicit_live_flag_a_client_and_a_configuration(home, monkeypatch, capsys):
    monkeypatch.setattr(lv, "run_live_verification", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    assert cli.main(["verify", "live", "--client", "alpha-co", "--config", str(home.path)], environ=home.env) == Exit.USAGE
    assert "add --live" in capsys.readouterr().err
    assert cli.main(["verify", "live", "--live", "--config", str(home.path)], environ=home.env) == Exit.USAGE  # no client
    assert cli.main(["verify", "live", "--client", "alpha-co", "--live"], environ={}) == Exit.CONFIG_INVALID  # no configuration file: exit before any Google
    for extra in (["--evidence", "x.json"], ["--verified"], ["--force"], ["--skip-checks"], ["--synthetic"]):
        assert cli.main(["verify", "live", "--client", "alpha-co", "--live", *extra, "--config", str(home.path)], environ=home.env) == Exit.USAGE


def test_the_command_reports_success_and_failure_with_the_existing_exit_codes(home, monkeypatch, capsys):
    result = lv.VerificationResult("alpha-co", (("oauth_refresh", "ok"), ("sheets_read", "ok"), ("search_console_read", "ok"), ("ga4_read", "ok")), "ok", "siteOwner", True,
                                   "2026-11-02T10:00:00+00:00", "2026-11-09T10:00:00+00:00")
    monkeypatch.setattr(lv, "run_live_verification", lambda *a, **k: result)
    assert cli_run(home, ["verify", "live", "--client", "alpha-co", "--live"]) == Exit.OK
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["attestation_issued"] is True and data["expires_at"] == result.expires_at and "property" not in json.dumps(data).lower().replace("search_console_permission", "")

    def refuse(*a, **k):
        raise lv.VerificationFailed("search_console", "gsc_property_not_accessible", Exit.SOURCE_UNAVAILABLE, "search_console failed: source_denied: gsc_property_not_accessible.")
    monkeypatch.setattr(lv, "run_live_verification", refuse)
    assert cli_run(home, ["verify", "live", "--client", "alpha-co", "--live"]) == Exit.SOURCE_UNAVAILABLE
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and data["details"] == {"stage": "search_console", "attestation_issued": False} and data["error"]["code"] == "gsc_property_not_accessible"


def test_no_other_command_runs_the_live_verification(home, monkeypatch, capsys):
    monkeypatch.setattr(lv, "run_live_verification", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the live verification must only run for `verify live`")))
    for argv in (["doctor", "--skip-browser-launch"], ["system-info"], ["status"], ["recovery", "inspect"], ["report", "dry-run", "--client", "alpha-co", "--period", "2026-09-29_2026-10-26"]):
        assert cli_run(home, argv) in (Exit.OK, Exit.PREFLIGHT_FAILED, Exit.AUTH_REQUIRED), argv
    assert cli_run(home, ["report", "run", "--client", "alpha-co", "--period", "2026-09-29_2026-10-26", "--production"]) == Exit.PREFLIGHT_FAILED
    sources = {path.name for path in (ROOT / "src").rglob("*.py") if "run_live_verification" in path.read_text(encoding="utf-8")}
    assert sources == {"live_verification.py", "cli.py"}  # defined here, called by the one explicit command


def test_end_to_end_through_the_command_with_fake_google_and_the_real_windows_protection(home, capsys):
    """The whole command on synthetic data: fake Sheets / Search Console / GA4 and credentials, but the real DPAPI, identity and ACL code, in a temporary folder."""
    if os.name != "nt":
        pytest.skip("Windows DPAPI")
    order = []
    factory = fakes(order)[0]
    code = cli.main(["--json", "verify", "live", "--client", "alpha-co", "--live", "--config", str(home.path)], environ=home.env, client_factory=factory,
                    preflight_probes={**PROBES_OK}, clock=lambda: dt.datetime.now(UTC))
    out = json.loads(capsys.readouterr().out)
    assert code == Exit.OK and out["attestation_issued"] is True and order == EXPECTED_ORDER
    assert att.evaluate(home.settings, home.env).valid  # real DPAPI round trip
    report_code = cli.main(["--json", "doctor", "--skip-browser-launch", "--config", str(home.path)], environ=home.env, preflight_probes={**PROBES_OK},
                           clock=lambda: dt.datetime.now(UTC))
    report = json.loads(capsys.readouterr().out)
    assert report_code == 0 and report["outstanding"] == ["make_not_connected", "drive_not_connected", "email_not_connected"] and report["production_ready"] is False
    text = json.dumps(out)
    assert GSC_PROPERTY not in text and GA4_PROPERTY not in text and home.settings.reporting_spreadsheet_id not in text


def test_a_failed_command_leaves_no_attestation(home, capsys):
    order = []
    factory = fakes(order, sites={"siteEntry": []})[0]
    code = cli.main(["--json", "verify", "live", "--client", "alpha-co", "--live", "--config", str(home.path)], environ=home.env, client_factory=factory,
                    preflight_probes={**PROBES_OK}, clock=lambda: dt.datetime.now(UTC))
    out = json.loads(capsys.readouterr().out)
    assert code == Exit.SOURCE_UNAVAILABLE and out["details"]["attestation_issued"] is False and not stored(home).exists() and "ga4_client" not in order
