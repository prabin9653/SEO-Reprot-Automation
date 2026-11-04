"""R8.3: source collection and validation with offline fakes (no Google call is made)."""
import dataclasses
import datetime as dt
import sys
from pathlib import Path

import pytest
from google.auth.exceptions import RefreshError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import source_collector as sc
from seo_report.periods import Period
from seo_report.sources import ga4 as ga4_source
from seo_report.sources import gsc as gsc_source
from test_gsc import FakeService, default_handler, http_error
from test_pipeline import make_client
from test_report_run import CURRENT, GA4, GSC

TODAY = dt.date(2026, 11, 2)
CLIENT = make_client()


def collect(fetch_gsc=None, fetch_ga4=None, **kw):
    return sc.collect_sources(
        CLIENT, CURRENT, gsc_service=kw.pop("gsc_service", None), ga4_client=None, today=TODAY, sleep=kw.pop("sleep", lambda s: None),
        fetch_gsc=fetch_gsc or (lambda service, site, periods, **k: GSC),
        fetch_ga4=fetch_ga4 or (lambda client, prop, periods, **k: GA4), **kw)


def raises(error):
    def fetch(*args, **kwargs):
        raise error
    return fetch


# --- success ----------------------------------------------------------------------------------------------------------------------

def test_both_sources_available_returns_their_data_unchanged():
    result = collect()
    assert result.ok and result.failures == () and result.unavailable_sources == ()
    assert result.gsc is GSC and result.ga4 is GA4


def test_the_requested_site_period_and_dates_are_passed_to_the_existing_fetchers():
    seen = {}

    def fetch_gsc(service, site, periods, **kw):
        seen["gsc"] = (service, site, periods, kw["today"])
        return GSC

    def fetch_ga4(client, prop, periods, **kw):
        seen["ga4"] = (client, prop, periods, kw["today"])
        return GA4
    collect(fetch_gsc=fetch_gsc, fetch_ga4=fetch_ga4, gsc_service="svc")
    assert seen["gsc"] == ("svc", "https://example.test/", CURRENT, TODAY)
    assert seen["ga4"] == (None, "123456789", CURRENT, TODAY)


# --- failures: classified, wholly unavailable, never swallowed beyond the known errors ----------------------------------------------------

@pytest.mark.parametrize("error,category,code", [
    (gsc_source.SearchConsoleError("x", 401), "auth_required", "gsc_http_401"),
    (gsc_source.SearchConsoleError("x", 403), "source_denied", "gsc_http_403"),
    (gsc_source.SearchConsoleError("x", 404), "source_denied", "gsc_http_404"),
    (gsc_source.SearchConsoleError("x", 429), "source_temporary", "gsc_http_429"),
    (gsc_source.SearchConsoleError("x", 503), "source_temporary", "gsc_http_503"),
    (gsc_source.SearchConsoleError("x", 418), "source_invalid", "gsc_request_rejected"),
    (gsc_source.SearchConsoleError("not accessible"), "source_denied", "gsc_property_not_accessible"),
    (RefreshError("revoked"), "auth_required", "auth_refresh_failed"),
    (TimeoutError("slow"), "source_temporary", "gsc_network_error"),
    (ConnectionError("down"), "source_temporary", "gsc_network_error")])
def test_known_gsc_errors_make_the_source_wholly_unavailable_with_a_fixed_code(error, category, code):
    result = collect(fetch_gsc=raises(error))
    assert not result.ok and result.gsc is None and result.ga4 is GA4
    assert result.failures == (sc.SourceFailure("gsc", category, code),) and result.unavailable_sources == ("gsc",)


@pytest.mark.parametrize("error,category,code", [
    (ga4_source.Ga4Error("x", 401), "auth_required", "ga4_http_401"),
    (ga4_source.Ga4Error("x", 403), "source_denied", "ga4_http_403"),
    (ga4_source.Ga4Error("x", 503), "source_temporary", "ga4_http_503"),
    (ga4_source.Ga4Error("x", incompatible=True), "source_incompatible", "ga4_schema_incompatible"),
    (ga4_source.Ga4Error("GA4 property ID must be numeric"), "source_invalid", "ga4_request_invalid")])
def test_known_ga4_errors_make_the_source_wholly_unavailable_with_a_fixed_code(error, category, code):
    result = collect(fetch_ga4=raises(error))
    assert not result.ok and result.ga4 is None and result.gsc is GSC
    assert result.failures == (sc.SourceFailure("ga4", category, code),)


def test_both_sources_can_fail_and_the_worst_failure_decides():
    result = collect(fetch_gsc=raises(gsc_source.SearchConsoleError("x", 403)),
                     fetch_ga4=raises(ga4_source.Ga4Error("x", 503)))
    assert set(result.unavailable_sources) == {"gsc", "ga4"}
    assert sc.worst_failure(result.failures).category == "source_temporary"
    assert sc.worst_failure(collect(fetch_gsc=raises(RefreshError("x")),
                                    fetch_ga4=raises(ga4_source.Ga4Error("x", 503))).failures).category == "auth_required"


def test_unknown_exceptions_are_not_swallowed():
    for error in (ValueError("programming error"), KeyError("k"), AttributeError("a"), RuntimeError("r")):
        with pytest.raises(type(error)):
            collect(fetch_gsc=raises(error))
        with pytest.raises(type(error)):
            collect(fetch_ga4=raises(error))
    with pytest.raises(KeyboardInterrupt):
        collect(fetch_gsc=raises(KeyboardInterrupt()))


# --- validation of successful responses ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    None, "not data", dataclasses.replace(GSC, site_url="https://other.test/"),
    dataclasses.replace(GSC, current=dataclasses.replace(GSC.current, period=Period(dt.date(2026, 1, 1), dt.date(2026, 1, 28)))),
    dataclasses.replace(GSC, previous=dataclasses.replace(GSC.previous, period=CURRENT.current)),
    dataclasses.replace(GSC, current=dataclasses.replace(GSC.current, totals=gsc_source.Metrics(-1, 5, None, None))),
    dataclasses.replace(GSC, current=dataclasses.replace(GSC.current, totals=gsc_source.Metrics(1.5, 5, None, None))),
    dataclasses.replace(GSC, current=dataclasses.replace(GSC.current, totals=gsc_source.Metrics(True, 5, None, None)))])
def test_an_invalid_search_console_response_is_refused_before_assembly(bad):
    result = collect(fetch_gsc=lambda *a, **k: bad)
    assert result.gsc is None and result.failures == (sc.SourceFailure("gsc", "source_invalid", "gsc_response_invalid"),)


@pytest.mark.parametrize("bad", [
    None, [], dataclasses.replace(GA4, property_id="999"),
    dataclasses.replace(GA4, current=dataclasses.replace(GA4.current, period=Period(dt.date(2026, 1, 1), dt.date(2026, 1, 28)))),
    dataclasses.replace(GA4, current=dataclasses.replace(
        GA4.current, whole_site=dataclasses.replace(GA4.current.whole_site, sessions=-3)))])
def test_an_invalid_ga4_response_is_refused_before_assembly(bad):
    result = collect(fetch_ga4=lambda *a, **k: bad)
    assert result.ga4 is None and result.failures == (sc.SourceFailure("ga4", "source_invalid", "ga4_response_invalid"),)


# --- valid zero results, incomplete and truncated data ----------------------------------------------------------------------------------

def test_a_valid_zero_result_response_is_available_data_with_a_notice():
    result = collect(gsc_service=FakeService(lambda body: {}), fetch_gsc=gsc_source.fetch_search_console)
    assert result.ok and result.gsc is not None
    assert result.gsc.current.totals == gsc_source.EMPTY_METRICS and result.gsc.previous.totals == gsc_source.EMPTY_METRICS
    assert sc.SourceWarning("gsc", "source_empty") in result.warnings
    assert result.unavailable_sources == ()


def test_the_real_search_console_fetch_runs_unchanged_against_a_fake_service():
    service = FakeService(default_handler)
    result = collect(gsc_service=service, fetch_gsc=gsc_source.fetch_search_console)
    assert result.ok and result.gsc.site_url == "https://example.test/" and service.calls
    assert result.gsc.current.totals.clicks == 6 and result.gsc.current.period == CURRENT.current


def test_the_existing_retry_behaviour_is_preserved():
    attempts, sleeps = [], []

    def handler(body):
        attempts.append(1)
        if len(attempts) == 1:
            raise http_error(503)
        return default_handler(body)
    result = collect(gsc_service=FakeService(handler), fetch_gsc=gsc_source.fetch_search_console, sleep=sleeps.append)
    assert result.ok and sleeps == [1.0]  # one backoff of the module's own policy, then success


def test_a_persistent_server_error_becomes_a_temporary_failure_after_the_modules_own_attempts():
    calls = []

    def handler(body):
        calls.append(1)
        raise http_error(503)
    result = collect(gsc_service=FakeService(handler), fetch_gsc=gsc_source.fetch_search_console)
    assert result.failures == (sc.SourceFailure("gsc", "source_temporary", "gsc_http_503"),)
    assert len(calls) == gsc_source.MAX_ATTEMPTS


def test_permission_errors_are_not_retried_and_are_denials():
    calls = []

    def handler(body):
        calls.append(1)
        raise http_error(403)
    result = collect(gsc_service=FakeService(handler), fetch_gsc=gsc_source.fetch_search_console)
    assert result.failures == (sc.SourceFailure("gsc", "source_denied", "gsc_http_403"),) and len(calls) == 1


def test_an_inaccessible_property_is_a_denial():
    result = collect(gsc_service=FakeService(default_handler, sites={"siteEntry": []}),
                     fetch_gsc=gsc_source.fetch_search_console)
    assert result.failures == (sc.SourceFailure("gsc", "source_denied", "gsc_property_not_accessible"),)


def test_incomplete_and_truncated_data_stay_available_with_notices():
    current = dataclasses.replace(GSC.current, warnings=[gsc_source.DataWarning("incomplete_data", "m"),
                                                         gsc_source.DataWarning("truncated", "m", "query"),
                                                         gsc_source.DataWarning("incomplete_data", "again")])
    ga4_current = dataclasses.replace(GA4.current, warnings=[ga4_source.Ga4Warning("thresholded", "m"),
                                                             ga4_source.Ga4Warning("empty_period", "m")])
    result = collect(fetch_gsc=lambda *a, **k: dataclasses.replace(GSC, current=current),
                     fetch_ga4=lambda *a, **k: dataclasses.replace(GA4, current=ga4_current))
    assert result.ok
    assert result.warnings == (sc.SourceWarning("gsc", "source_incomplete"), sc.SourceWarning("gsc", "source_truncated"),
                               sc.SourceWarning("ga4", "source_empty"), sc.SourceWarning("ga4", "source_thresholded"))


def test_failures_and_warnings_hold_fixed_codes_only():
    result = collect(fetch_gsc=raises(gsc_source.SearchConsoleError("Property https://private.example/ denied", 403)))
    text = repr(result)
    assert "private.example" not in text and "denied" not in text.replace("source_denied", "")
