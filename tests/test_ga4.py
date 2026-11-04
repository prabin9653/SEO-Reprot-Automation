import datetime as dt
import sys
from pathlib import Path

import pytest
from google.analytics.data_v1beta.types import (
    DimensionHeader,
    DimensionMetadata,
    DimensionValue,
    Metadata,
    MetricHeader,
    MetricMetadata,
    MetricValue,
    ResponseMetaData,
    Row,
    RunReportResponse,
    SamplingMetadata,
)
from google.api_core import exceptions as api_exceptions

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report.periods import Period, report_periods
from seo_report.sources import ga4

D = dt.date
PROPERTY = "123456789"
CURRENT = Period(D(2026, 9, 29), D(2026, 10, 26))
PREVIOUS = Period(D(2026, 9, 1), D(2026, 9, 28))
PERIODS = report_periods(D(2026, 9, 29), D(2026, 10, 26))
TODAY = D(2026, 11, 20)

ALL_METRICS = ["sessions", "totalUsers", "engagementRate", "engagedSessions", "keyEvents"]
ALL_DIMENSIONS = ["date", "sessionDefaultChannelGroup", "landingPage"]


def metadata(metrics=ALL_METRICS, dimensions=ALL_DIMENSIONS):
    return Metadata(
        metrics=[MetricMetadata(api_name=m) for m in metrics],
        dimensions=[DimensionMetadata(api_name=d) for d in dimensions],
    )


def response(dim_names, metric_names, rows, *, row_count=None, **meta):
    return RunReportResponse(
        dimension_headers=[DimensionHeader(name=n) for n in dim_names],
        metric_headers=[MetricHeader(name=n) for n in metric_names],
        rows=[
            Row(
                dimension_values=[DimensionValue(value=v) for v in dims],
                metric_values=[MetricValue(value=str(v)) for v in values],
            )
            for dims, values in rows
        ],
        row_count=len(rows) if row_count is None else row_count,
        metadata=ResponseMetaData(**meta),
    )


class FakeClient:
    def __init__(self, handler, meta=None):
        self.handler = handler
        self.requests = []
        self.metadata_calls = 0
        self.meta = meta or metadata()

    def get_metadata(self, request=None, **kwargs):
        self.metadata_calls += 1
        return self.meta

    def run_report(self, request):
        self.requests.append(request)
        return self.handler(request)


def names(request):
    return [m.name for m in request.metrics], [d.name for d in request.dimensions]


def has_filter(request):
    return bool(request.dimension_filter.filter.field_name)


def values_for(metric_names, base):
    table = {"sessions": base[0], "totalUsers": base[1], "engagementRate": base[2],
             "engagedSessions": base[3], "keyEvents": base[4]}
    return [table[m] for m in metric_names]


def default_handler(request):
    metric_names, dim_names = names(request)
    if dim_names == []:
        return response([], metric_names, [([], values_for(metric_names, (100, 80, 0.6, 60, 3)))],
                        time_zone="America/Chicago")
    if dim_names == ["sessionDefaultChannelGroup"]:
        return response(dim_names, metric_names, [
            (["Direct"], values_for(metric_names, (60, 50, 0.7, 40, 2))),
            (["Organic Search"], values_for(metric_names, (40, 35, 0.5, 20, 1))),
        ])
    if dim_names == ["date", "sessionDefaultChannelGroup"]:
        start = D.fromisoformat(request.date_ranges[0].start_date)
        end = D.fromisoformat(request.date_ranges[0].end_date)
        days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        rows = []
        for day in days:
            rows.append(([day.strftime("%Y%m%d"), "Direct"], [2]))
            rows.append(([day.strftime("%Y%m%d"), "Organic Search"], [1]))
        return response(dim_names, metric_names, rows)
    if has_filter(request):
        paths = [("/", 20), ("/services", 15), ("/blog", 5), ("/a", 4), ("/b", 3), ("/c", 2), ("(not set)", 30)]
    else:
        paths = [("/", 50), ("/services", 30), ("(not set)", 5)]
    return response(dim_names, metric_names, [
        ([p], values_for(metric_names, (s, s, 0.5, s // 2, 1))) for p, s in paths
    ])


def fetch_period(handler=default_handler, period=CURRENT, supported=True, **kwargs):
    client = FakeClient(handler)
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("sleep", lambda s: None)
    return client, ga4.fetch_period(client, PROPERTY, period, key_events_supported=supported, **kwargs)


def fetch_all(handler=default_handler, meta=None, **kwargs):
    client = FakeClient(handler, meta)
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("sleep", lambda s: None)
    return client, ga4.fetch_ga4(client, PROPERTY, PERIODS, **kwargs)


def test_requests_target_the_property_with_inclusive_dates_and_expected_fields():
    client, data = fetch_all()
    assert client.metadata_calls == 1
    assert len(client.requests) == 10  # 5 reports x 2 periods
    first = client.requests[:5]
    assert all(r.property == f"properties/{PROPERTY}" for r in client.requests)
    assert {(r.date_ranges[0].start_date, r.date_ranges[0].end_date) for r in first} == {("2026-09-29", "2026-10-26")}
    assert {(r.date_ranges[0].start_date, r.date_ranges[0].end_date) for r in client.requests[5:]} == {("2026-09-01", "2026-09-28")}
    assert names(first[0]) == (["sessions", "totalUsers", "engagementRate", "engagedSessions", "keyEvents"], [])
    assert names(first[1])[1] == ["sessionDefaultChannelGroup"]
    assert names(first[2]) == (["sessions"], ["date", "sessionDefaultChannelGroup"])
    assert names(first[3])[1] == ["landingPage"] and not has_filter(first[3])
    assert names(first[4])[1] == ["landingPage"] and has_filter(first[4])
    organic_filter = first[4].dimension_filter.filter
    assert organic_filter.field_name == "sessionDefaultChannelGroup"
    assert organic_filter.string_filter.value == "Organic Search"
    assert not any(has_filter(r) for r in first[:4])


def test_totals_and_organic_metrics_are_parsed():
    _, data = fetch_period()
    assert data.whole_site == ga4.Ga4Metrics(sessions=100, total_users=80, engaged_sessions=60, engagement_rate=0.6, key_events=3)
    assert data.organic == ga4.Ga4Metrics(40, 35, 20, 0.5, 1)
    assert [c.channel for c in data.channels] == ["Direct", "Organic Search"]
    assert data.time_zone == "America/Chicago"
    assert data.warnings == []


def test_missing_organic_row_means_zero_sessions_without_engagement_rate():
    def handler(request):
        metric_names, dim_names = names(request)
        if dim_names == ["sessionDefaultChannelGroup"]:
            return response(dim_names, metric_names, [(["Direct"], values_for(metric_names, (60, 50, 0.7, 40, 2)))])
        return default_handler(request)

    _, data = fetch_period(handler)
    assert (data.organic.sessions, data.organic.total_users) == (0, 0)
    assert data.organic.engagement_rate is None


def test_empty_period_makes_no_further_requests_and_invents_nothing():
    client, data = fetch_period(lambda r: response([], names(r)[0], []))
    assert len(client.requests) == 1
    assert data.is_empty and data.whole_site == ga4.EMPTY_METRICS
    assert data.whole_site.engagement_rate is None and data.whole_site.key_events is None
    assert [w.code for w in data.warnings] == ["empty_period"]
    assert len(data.daily) == 28 and not any(d.has_data for d in data.daily)


def test_zero_sessions_row_has_no_engagement_rate():
    metrics = ga4._metrics({"sessions": 0.0, "totalUsers": 0.0, "engagementRate": 0.0, "engagedSessions": 0.0})
    assert metrics.engagement_rate is None
    assert metrics.key_events is None


def test_daily_series_is_zero_filled_and_has_organic_series():
    def handler(request):
        _, dim_names = names(request)
        if dim_names == ["date", "sessionDefaultChannelGroup"]:
            return response(dim_names, ["sessions"], [
                (["20261001", "Direct"], [2]), (["20261001", "Organic Search"], [1]),
                (["20261026", "Organic Search"], [3]),
            ])
        return default_handler(request)

    _, data = fetch_period(handler)
    assert len(data.daily) == 28
    first = next(d for d in data.daily if d.date == D(2026, 10, 1))
    assert (first.sessions, first.organic_sessions, first.has_data) == (3, 1, True)
    gap = next(d for d in data.daily if d.date == D(2026, 10, 2))
    assert (gap.sessions, gap.organic_sessions, gap.has_data) == (0, 0, False)
    last = data.daily[-1]
    assert (last.date, last.sessions, last.organic_sessions) == (D(2026, 10, 26), 3, 3)
    assert not any(w.code == "incomplete_data" for w in data.warnings)


def test_invalid_daily_date_is_rejected():
    def handler(request):
        _, dim_names = names(request)
        if dim_names == ["date", "sessionDefaultChannelGroup"]:
            return response(dim_names, ["sessions"], [(["not-a-date", "Direct"], [1])])
        return default_handler(request)

    with pytest.raises(ga4.Ga4Error, match="invalid date"):
        fetch_period(handler)


def test_recent_period_with_missing_last_day_is_flagged():
    def handler(request):
        _, dim_names = names(request)
        if dim_names == ["date", "sessionDefaultChannelGroup"]:
            return response(dim_names, ["sessions"], [(["20261001", "Direct"], [2])])
        return default_handler(request)

    _, recent = fetch_period(handler, today=D(2026, 10, 27))
    assert any(w.code == "incomplete_data" for w in recent.warnings)
    _, old = fetch_period(handler, today=D(2026, 11, 20))
    assert not any(w.code == "incomplete_data" for w in old.warnings)


def test_placeholder_landing_pages_are_kept_but_excluded_from_top_list():
    _, data = fetch_period()
    organic = data.organic_landing_pages
    assert organic[0].path == "(not set)" and organic[0].is_placeholder  # kept in the data
    top = data.top_organic_landing_pages(5)
    assert [p.path for p in top] == ["/", "/services", "/blog", "/a", "/b"]
    assert not any(p.is_placeholder for p in top)
    assert len(data.landing_pages) == 3


def test_key_events_status_recorded():
    _, data = fetch_all()
    assert data.key_events_status == "recorded"


def test_key_events_none_recorded_when_both_periods_are_zero():
    def handler(request):
        metric_names, dim_names = names(request)
        if dim_names == []:
            return response([], metric_names, [([], values_for(metric_names, (100, 80, 0.6, 60, 0)))])
        return default_handler(request)

    _, data = fetch_all(handler)
    assert data.key_events_status == "none_recorded"
    assert data.current.whole_site.key_events == 0


def test_a_real_zero_is_kept_when_the_other_period_has_key_events():
    def handler(request):
        metric_names, dim_names = names(request)
        if dim_names == []:
            events = 0 if request.date_ranges[0].start_date == "2026-09-29" else 4
            return response([], metric_names, [([], values_for(metric_names, (100, 80, 0.6, 60, events)))])
        return default_handler(request)

    _, data = fetch_all(handler)
    assert data.key_events_status == "recorded"
    assert (data.current.whole_site.key_events, data.previous.whole_site.key_events) == (0, 4)


def test_key_events_unavailable_when_metadata_lacks_the_metric():
    meta = metadata(metrics=[m for m in ALL_METRICS if m != "keyEvents"])
    client, data = fetch_all(meta=meta)
    assert data.key_events_status == "unavailable"
    assert data.current.whole_site.key_events is None
    assert all("keyEvents" not in names(r)[0] for r in client.requests)


def test_incompatible_key_events_are_dropped_for_that_report_only():
    def handler(request):
        metric_names, dim_names = names(request)
        if dim_names == ["landingPage"] and "keyEvents" in metric_names:
            raise api_exceptions.InvalidArgument("Dimensions and metrics are incompatible: keyEvents")
        return default_handler(request)

    client, data = fetch_all(handler)
    assert data.key_events_status == "recorded"
    assert data.current.whole_site.key_events == 3
    assert all(p.metrics.key_events is None for p in data.current.landing_pages + data.current.organic_landing_pages)
    reports = {w.report for w in data.current.warnings if w.code == "key_events_unavailable"}
    assert reports == {"landing_pages", "organic_landing_pages"}


def test_other_invalid_requests_are_not_retried_or_altered():
    def handler(request):
        raise api_exceptions.InvalidArgument("Field foo is not a valid dimension")

    client = FakeClient(handler)
    with pytest.raises(ga4.Ga4Error, match="rejected") as error:
        ga4.fetch_period(client, PROPERTY, CURRENT, key_events_supported=True, today=TODAY, sleep=lambda s: None)
    assert len(client.requests) == 1 and error.value.status == 400


def test_missing_required_fields_stop_the_run_without_substitution():
    meta = metadata(dimensions=["date", "sessionDefaultChannelGroup", "landingPagePlusQueryString"])
    client = FakeClient(default_handler, meta)
    with pytest.raises(ga4.Ga4Error) as error:
        ga4.fetch_ga4(client, PROPERTY, PERIODS, today=TODAY, sleep=lambda s: None)
    assert "landingPage" in str(error.value) and "No substitute" in str(error.value)
    assert client.requests == []


def test_pagination_collects_all_pages(monkeypatch):
    monkeypatch.setattr(ga4, "PAGE_SIZE", 2)
    monkeypatch.setitem(ga4.MAX_ROWS, "landing_pages", 10)
    all_rows = [f"/p{i}" for i in range(5)]

    def handler(request):
        metric_names, dim_names = names(request)
        if dim_names == ["landingPage"] and not has_filter(request):
            chunk = all_rows[request.offset:request.offset + request.limit]
            return response(dim_names, metric_names,
                            [([p], values_for(metric_names, (10, 10, 0.5, 5, 1))) for p in chunk], row_count=5)
        return default_handler(request)

    client, data = fetch_period(handler)
    calls = [(r.offset, r.limit) for r in client.requests if names(r)[1] == ["landingPage"] and not has_filter(r)]
    assert calls == [(0, 2), (2, 2), (4, 2)]
    assert len(data.landing_pages) == 5
    assert not any(w.code == "truncated" for w in data.warnings)


def test_cap_reached_adds_truncation_warning(monkeypatch):
    monkeypatch.setitem(ga4.MAX_ROWS, "landing_pages", 2)

    def handler(request):
        metric_names, dim_names = names(request)
        if dim_names == ["landingPage"] and not has_filter(request):
            rows = [([f"/p{i}"], values_for(metric_names, (10 - i, 5, 0.5, 2, 1))) for i in range(request.limit)]
            return response(dim_names, metric_names, rows, row_count=9)
        return default_handler(request)

    client, data = fetch_all(handler)
    assert len(data.current.landing_pages) == 2
    assert data.truncated_reports == {"landing_pages"}


def test_thresholding_sampling_and_other_row_warnings():
    def handler(request):
        metric_names, dim_names = names(request)
        if dim_names == ["landingPage"] and not has_filter(request):
            return response(
                dim_names, metric_names, [(["/"], values_for(metric_names, (10, 10, 0.5, 5, 1)))],
                subject_to_thresholding=True, data_loss_from_other_row=True,
                sampling_metadatas=[SamplingMetadata(samples_read_count=500, sampling_space_size=1000)],
            )
        return default_handler(request)

    _, data = fetch_period(handler)
    codes = {w.code: w for w in data.warnings}
    assert {"thresholded", "sampled", "other_row"} <= set(codes)
    assert "500/1000" in codes["sampled"].message
    assert all(w.report == "landing_pages" for w in codes.values())


def test_diagnostics_do_not_require_equality():
    _, data = fetch_period()
    assert data.diagnostics["channel_sessions_diff"] == 0
    assert data.diagnostics["daily_sessions_diff"] == 84 - 100
    assert data.diagnostics["landing_sessions_diff"] == 85 - 100
    assert data.diagnostics["organic_landing_sessions_diff"] == 79 - 40


def test_transient_errors_are_retried_with_backoff():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise api_exceptions.TooManyRequests("slow down")
        return default_handler(request)

    sleeps = []
    _, data = fetch_period(handler, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]
    assert data.whole_site.sessions == 100


def test_persistent_server_error_stops_after_bounded_attempts():
    sleeps = []
    client = FakeClient(lambda r: (_ for _ in ()).throw(api_exceptions.ServiceUnavailable("down")))
    with pytest.raises(ga4.Ga4Error) as error:
        ga4.fetch_period(client, PROPERTY, CURRENT, key_events_supported=True, today=TODAY, sleep=sleeps.append)
    assert len(client.requests) == ga4.MAX_ATTEMPTS
    assert sleeps == [1.0, 2.0, 4.0] and error.value.status == 503


def test_permission_errors_are_not_retried():
    client = FakeClient(lambda r: (_ for _ in ()).throw(api_exceptions.PermissionDenied("no access")))
    sleeps = []
    with pytest.raises(ga4.Ga4Error, match="denied access") as error:
        ga4.fetch_period(client, PROPERTY, CURRENT, key_events_supported=True, today=TODAY, sleep=sleeps.append)
    assert len(client.requests) == 1 and sleeps == [] and error.value.status == 403


def test_error_messages_redact_contact_details():
    err = api_exceptions.InvalidArgument("bad value for someone@example.test or +1 281 555 0100 ok")
    message = str(ga4._to_error(err, "test"))
    assert "someone@example.test" not in message and "555 0100" not in message


def test_property_id_must_be_numeric():
    with pytest.raises(ga4.Ga4Error, match="numeric"):
        ga4.fetch_ga4(FakeClient(default_handler), "G-ABC123", PERIODS)


def test_total_users_note_mentions_active_users():
    assert "active users" in ga4.TOTAL_USERS_NOTE


def test_ga4_module_is_independent_of_other_sources_and_rankings():
    source = (ROOT / "src" / "seo_report" / "sources" / "ga4.py").read_text(encoding="utf-8")
    for forbidden in ("seo_report.rankings", "seo_report.clients", "seo_report.sources.gsc"):
        assert forbidden not in source
