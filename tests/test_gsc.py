import datetime as dt
import sys
from pathlib import Path

import httplib2
import pytest
from googleapiclient.errors import HttpError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report.countries import country_name
from seo_report.periods import Period, report_periods
from seo_report.sources import gsc

D = dt.date
SITE = "https://example.test/"
CURRENT = Period(D(2026, 9, 29), D(2026, 10, 26))
PREVIOUS = Period(D(2026, 9, 1), D(2026, 9, 28))
TODAY = D(2026, 10, 29)


def http_error(status):
    resp = httplib2.Response({"status": str(status)})
    return HttpError(resp, b'{"error": {"message": "synthetic"}}')


class FakeRequest:
    def __init__(self, run):
        self.run = run

    def execute(self):
        return self.run()


class FakeService:
    def __init__(self, handler, sites=None):
        self.handler = handler
        self.calls = []
        self.list_calls = 0
        self.sites_response = (
            {"siteEntry": [{"siteUrl": SITE, "permissionLevel": "siteOwner"}]} if sites is None else sites
        )

    def searchanalytics(self):
        return self

    def query(self, siteUrl, body):
        def run():
            self.calls.append((siteUrl, body))
            return self.handler(body)

        return FakeRequest(run)

    def sites(self):
        return self

    def list(self):
        def run():
            self.list_calls += 1
            return self.sites_response

        return FakeRequest(run)


def days_between(body):
    start, end = D.fromisoformat(body["startDate"]), D.fromisoformat(body["endDate"])
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def default_handler(body):
    dims = body.get("dimensions")
    if not dims:
        return {"rows": [{"clicks": 6.0, "impressions": 1960.0, "ctr": 0.003, "position": 29.2}]}
    if dims == ["date"]:
        return {"rows": [{"keys": [d.isoformat()], "clicks": 0.0, "impressions": 70.0, "ctr": 0.0, "position": 30.0}
                         for d in days_between(body)]}
    if dims == ["country"]:
        return {"rows": [
            {"keys": ["pak"], "clicks": 1.0, "impressions": 13.0, "ctr": 0.07, "position": 12.0},
            {"keys": ["usa"], "clicks": 4.0, "impressions": 1862.0, "ctr": 0.002, "position": 29.0},
        ]}
    if dims == ["query"]:
        return {"rows": [{"keys": ["widget repair"], "clicks": 2.0, "impressions": 100.0, "ctr": 0.02, "position": 8.0}]}
    return {"rows": [{"keys": [SITE], "clicks": 6.0, "impressions": 2000.0, "ctr": 0.003, "position": 29.0}]}


def sliced(rows, body):
    start = body["startRow"]
    return {"rows": rows[start:start + body["rowLimit"]]}


def query_rows(n):
    return [{"keys": [f"query {i:03d}"], "clicks": float(100 - i), "impressions": 200.0, "ctr": 0.1, "position": 5.0}
            for i in range(n)]


def fetch(handler=default_handler, period=CURRENT, **kwargs):
    service = FakeService(handler)
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("sleep", lambda s: None)
    return service, gsc.fetch_period(service, SITE, period, **kwargs)


def test_requests_use_exact_property_web_type_final_data_and_inclusive_dates():
    service = FakeService(default_handler)
    gsc.fetch_search_console(service, SITE, report_periods(D(2026, 9, 29), D(2026, 10, 26)),
                             today=TODAY, sleep=lambda s: None)
    assert service.list_calls == 1
    assert len(service.calls) == 10  # 5 queries x 2 periods, no completeness probes
    for site, body in service.calls:
        assert site == SITE
        assert body["type"] == "web"
        assert body["dataState"] == "final"
    assert [b.get("dimensions") for _, b in service.calls[:5]] == [None, ["date"], ["country"], ["query"], ["page"]]
    assert {(b["startDate"], b["endDate"]) for _, b in service.calls[:5]} == {("2026-09-29", "2026-10-26")}
    assert {(b["startDate"], b["endDate"]) for _, b in service.calls[5:]} == {("2026-09-01", "2026-09-28")}


def test_both_periods_include_every_breakdown():
    data = gsc.fetch_search_console(FakeService(default_handler), SITE, report_periods(D(2026, 9, 29), D(2026, 10, 26)),
                                    today=TODAY, sleep=lambda s: None)
    for period_data in (data.current, data.previous):
        assert period_data.countries and period_data.queries and period_data.pages
        assert len(period_data.daily) == 28
    assert data.permission_level == "siteOwner"
    assert data.warnings == []


def test_totals_are_parsed_as_ints_and_fractions():
    _, data = fetch()
    assert data.totals == gsc.Metrics(clicks=6, impressions=1960, ctr=0.003, position=29.2)
    assert isinstance(data.totals.clicks, int)


def test_countries_are_sorted_and_named():
    _, data = fetch()
    assert [(r.key, r.label) for r in data.countries] == [("usa", "United States"), ("pak", "Pakistan")]


def test_missing_days_are_zero_filled_without_inventing_position():
    def handler(body):
        if body.get("dimensions") == ["date"]:
            return {"rows": [
                {"keys": ["2026-10-01"], "clicks": 1.0, "impressions": 50.0, "ctr": 0.02, "position": 11.0},
                {"keys": ["2026-10-26"], "clicks": 0.0, "impressions": 20.0, "ctr": 0.0, "position": 40.0},
            ]}
        return default_handler(body)

    _, data = fetch(handler)
    assert len(data.daily) == 28
    gap = next(p for p in data.daily if p.date == D(2026, 10, 2))
    assert gap.has_data is False
    assert (gap.metrics.clicks, gap.metrics.impressions) == (0, 0)
    assert gap.metrics.position is None and gap.metrics.ctr is None
    assert data.latest_data_date == D(2026, 10, 26)
    assert data.warnings == []


def test_empty_response_gives_empty_period_without_fabricated_values():
    service, data = fetch(lambda body: {})
    assert len(service.calls) == 1  # breakdown queries are skipped
    assert data.totals == gsc.EMPTY_METRICS
    assert data.totals.position is None and data.totals.ctr is None
    assert [w.code for w in data.warnings] == ["empty_period"]
    assert len(data.daily) == 28 and not any(p.has_data for p in data.daily)
    assert data.countries == data.queries == data.pages == []


def test_zero_impression_row_has_no_ctr_or_position():
    metrics = gsc._metrics({"clicks": 0.0, "impressions": 0.0, "ctr": 0.0, "position": 0.0})
    assert metrics == gsc.Metrics(0, 0, None, None)


def test_row_without_position_keeps_position_none():
    metrics = gsc._metrics({"clicks": 1.0, "impressions": 10.0})
    assert metrics.position is None
    assert metrics.ctr == pytest.approx(0.1)


def test_pagination_collects_all_pages(monkeypatch):
    monkeypatch.setattr(gsc, "PAGE_SIZE", 3)
    monkeypatch.setitem(gsc.MAX_ROWS, "query", 10)
    rows = query_rows(7)

    def handler(body):
        return sliced(rows, body) if body.get("dimensions") == ["query"] else default_handler(body)

    service, data = fetch(handler)
    query_calls = [b for _, b in service.calls if b.get("dimensions") == ["query"]]
    assert [(b["startRow"], b["rowLimit"]) for b in query_calls] == [(0, 3), (3, 3), (6, 3)]
    assert len(data.queries) == 7
    assert not any(w.code == "truncated" for w in data.warnings)


def test_cap_reached_with_more_rows_adds_truncation_warning(monkeypatch):
    monkeypatch.setattr(gsc, "PAGE_SIZE", 3)
    monkeypatch.setitem(gsc.MAX_ROWS, "query", 5)
    rows = query_rows(12)

    def handler(body):
        return sliced(rows, body) if body.get("dimensions") == ["query"] else default_handler(body)

    service, data = fetch(handler)
    query_calls = [b for _, b in service.calls if b.get("dimensions") == ["query"]]
    assert [(b["startRow"], b["rowLimit"]) for b in query_calls] == [(0, 3), (3, 2), (5, 1)]
    assert len(data.queries) == 5
    (warning,) = [w for w in data.warnings if w.code == "truncated"]
    assert warning.dimension == "query"


def test_exactly_cap_rows_is_not_truncated(monkeypatch):
    monkeypatch.setattr(gsc, "PAGE_SIZE", 3)
    monkeypatch.setitem(gsc.MAX_ROWS, "query", 5)
    rows = query_rows(5)

    def handler(body):
        return sliced(rows, body) if body.get("dimensions") == ["query"] else default_handler(body)

    _, data = fetch(handler)
    assert len(data.queries) == 5
    assert not any(w.code == "truncated" for w in data.warnings)


def test_truncated_dimensions_are_exposed_on_the_result(monkeypatch):
    monkeypatch.setitem(gsc.MAX_ROWS, "page", 1)

    def handler(body):
        if body.get("dimensions") == ["page"]:
            return sliced([{"keys": [f"{SITE}{i}"], "clicks": 1.0, "impressions": 5.0} for i in range(3)], body)
        return default_handler(body)

    data = gsc.fetch_search_console(FakeService(handler), SITE, report_periods(D(2026, 9, 29), D(2026, 10, 26)),
                                    today=TODAY, sleep=lambda s: None)
    assert data.truncated_dimensions == {"page"}


def test_transient_errors_are_retried_with_backoff():
    attempts = {"n": 0}

    def handler(body):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise http_error(429)
        return default_handler(body)

    sleeps = []
    service, data = fetch(handler, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]
    assert data.totals.clicks == 6


def test_persistent_server_error_stops_after_bounded_attempts():
    sleeps = []

    def handler(body):
        raise http_error(503)

    service = FakeService(handler)
    with pytest.raises(gsc.SearchConsoleError) as error:
        gsc.fetch_period(service, SITE, CURRENT, today=TODAY, sleep=sleeps.append)
    assert len(service.calls) == gsc.MAX_ATTEMPTS
    assert sleeps == [1.0, 2.0, 4.0]
    assert error.value.status == 503


def test_permission_errors_are_not_retried():
    def handler(body):
        raise http_error(403)

    service = FakeService(handler)
    sleeps = []
    with pytest.raises(gsc.SearchConsoleError, match="denied access") as error:
        gsc.fetch_period(service, SITE, CURRENT, today=TODAY, sleep=sleeps.append)
    assert len(service.calls) == 1 and sleeps == []
    assert error.value.status == 403


def test_incomplete_data_is_flagged_when_nothing_follows_the_last_day():
    def handler(body):
        if body.get("dimensions") == ["date"] and body["startDate"] == "2026-09-29":
            rows = default_handler(body)["rows"][:-3]  # last 3 days missing
            return {"rows": rows}
        if body.get("dimensions") == ["date"]:
            return {}  # probe beyond the period finds nothing
        return default_handler(body)

    service, data = fetch(handler)
    (warning,) = [w for w in data.warnings if w.code == "incomplete_data"]
    assert "2026-10-26" in warning.message
    assert data.latest_data_date == D(2026, 10, 23)
    probe = next(b for _, b in service.calls if b["startDate"] == "2026-10-27")
    assert probe["endDate"] == "2026-10-29"  # capped at today


def test_quiet_last_day_is_not_flagged_when_later_data_exists():
    def handler(body):
        if body.get("dimensions") == ["date"] and body["startDate"] == "2026-09-29":
            return {"rows": default_handler(body)["rows"][:-1]}
        if body.get("dimensions") == ["date"]:
            return {"rows": [{"keys": ["2026-10-27"], "clicks": 0.0, "impressions": 5.0}]}
        return default_handler(body)

    _, data = fetch(handler)
    assert not any(w.code == "incomplete_data" for w in data.warnings)


def test_unfinished_period_is_flagged_without_probing():
    def handler(body):
        if body.get("dimensions") == ["date"]:
            return {"rows": default_handler(body)["rows"][:-1]}
        return default_handler(body)

    service, data = fetch(handler, today=D(2026, 10, 26))
    assert any(w.code == "incomplete_data" and "not finished" in w.message for w in data.warnings)
    assert all(b["startDate"] != "2026-10-27" for _, b in service.calls)


def test_diagnostics_report_differences_without_failing():
    _, data = fetch()
    # Synthetic data: daily 28 x 70 = 1960 matches the totals; countries (1875) and queries
    # (100) undercount; pages (2000) overcount. None of these may raise.
    assert data.diagnostics["daily_impressions_diff"] == 0
    assert data.diagnostics["country_impressions_diff"] == 1875 - 1960
    assert data.diagnostics["query_impressions_diff"] == 100 - 1960
    assert data.diagnostics["page_impressions_diff"] == 2000 - 1960


def test_dimension_row_without_keys_is_rejected():
    def handler(body):
        if body.get("dimensions") == ["country"]:
            return {"rows": [{"clicks": 1.0, "impressions": 2.0}]}
        return default_handler(body)

    with pytest.raises(gsc.SearchConsoleError, match="without keys"):
        fetch(handler)


def test_invalid_daily_date_is_rejected():
    def handler(body):
        if body.get("dimensions") == ["date"]:
            return {"rows": [{"keys": ["not-a-date"], "clicks": 1.0, "impressions": 2.0}]}
        return default_handler(body)

    with pytest.raises(gsc.SearchConsoleError, match="invalid date"):
        fetch(handler)


def test_access_check_requires_the_exact_property():
    service = FakeService(default_handler, sites={"siteEntry": [{"siteUrl": "https://example.test", "permissionLevel": "siteOwner"}]})
    with pytest.raises(gsc.SearchConsoleError, match="not accessible"):
        gsc.check_access(service, SITE)
    assert service.calls == []


def test_access_check_rejects_unverified_users_and_returns_level():
    unverified = FakeService(default_handler, sites={"siteEntry": [{"siteUrl": SITE, "permissionLevel": "siteUnverifiedUser"}]})
    with pytest.raises(gsc.SearchConsoleError, match="unverified"):
        gsc.check_access(unverified, SITE)
    restricted = FakeService(default_handler, sites={"siteEntry": [{"siteUrl": SITE, "permissionLevel": "siteRestrictedUser"}]})
    assert gsc.check_access(restricted, SITE) == "siteRestrictedUser"


def test_country_names_and_fallbacks():
    assert country_name("usa") == "United States"
    assert country_name("USA") == "United States"
    assert country_name("pak") == "Pakistan"
    assert country_name("mar") == "Morocco"
    assert country_name("xyz") == "XYZ"
    assert country_name("") == "Unknown" and country_name(None) == "Unknown"


def test_gsc_module_is_independent_of_manual_rankings():
    source = (ROOT / "src" / "seo_report" / "sources" / "gsc.py").read_text(encoding="utf-8")
    assert "seo_report.rankings" not in source
    assert "seo_report.clients" not in source
