"""Google Analytics 4 data for one property over two periods.

GA4 sessions and users come from the site's own tag and are a different measurement
system from Search Console clicks and impressions (which count Google Search appearances
and clicks). They are deliberately named differently and never combined or compared here.
Nothing in this module touches the manually maintained keyword rankings.

Metric and dimension names are checked against the property's metadata before any report
runs; a missing required name stops the run and is never replaced by a similar one.
"""

import datetime as dt
import re
import time
from dataclasses import dataclass, field

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Filter,
    FilterExpression,
    GetMetadataRequest,
    Metric,
    OrderBy,
    RunReportRequest,
)
from google.api_core.exceptions import GoogleAPICallError

from seo_report.periods import Period, ReportPeriods

ORGANIC_CHANNEL = "Organic Search"
TOTAL_USERS_NOTE = (
    "Total users may differ from the standard Users card in GA4, which generally reflects active users."
)

REQUIRED_METRICS = ("sessions", "totalUsers", "engagementRate", "engagedSessions")
REQUIRED_DIMENSIONS = ("date", "sessionDefaultChannelGroup", "landingPage")
KEY_EVENTS_METRIC = "keyEvents"
PLACEHOLDER_PAGES = {"(not set)", "(other)"}

PAGE_SIZE = 10000
MAX_ROWS = {"channels": 100, "daily": 5000, "landing_pages": 250, "organic_landing_pages": 250}
MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
RECENT_DAYS = 2  # GA4 data can take up to about two days to finish processing


class Ga4Error(RuntimeError):
    """A GA4 request failed, was rejected, or the property lacks required fields."""

    def __init__(self, message: str, status: int | None = None, incompatible: bool = False):
        super().__init__(message)
        self.status = status
        self.incompatible = incompatible


@dataclass(frozen=True)
class Ga4Metrics:
    sessions: int
    total_users: int
    engaged_sessions: int
    engagement_rate: float | None  # fraction; None when there are no sessions
    key_events: int | None  # None when unavailable or the period has no data


EMPTY_METRICS = Ga4Metrics(0, 0, 0, None, None)


@dataclass(frozen=True)
class ChannelRow:
    channel: str
    metrics: Ga4Metrics


@dataclass(frozen=True)
class LandingPage:
    path: str
    metrics: Ga4Metrics
    is_placeholder: bool  # "(not set)" or "(other)"


@dataclass(frozen=True)
class DailySessions:
    date: dt.date
    sessions: int
    organic_sessions: int
    has_data: bool


@dataclass(frozen=True)
class Ga4Warning:
    """Limitations for report code to surface.

    Codes: empty_period, incomplete_data, truncated, thresholded, sampled, other_row,
    key_events_unavailable.
    """

    code: str
    message: str
    report: str | None = None


@dataclass
class Ga4PeriodData:
    period: Period
    whole_site: Ga4Metrics
    organic: Ga4Metrics
    is_empty: bool = False
    channels: list[ChannelRow] = field(default_factory=list)
    daily: list[DailySessions] = field(default_factory=list)
    landing_pages: list[LandingPage] = field(default_factory=list)
    organic_landing_pages: list[LandingPage] = field(default_factory=list)
    time_zone: str | None = None
    warnings: list[Ga4Warning] = field(default_factory=list)
    # Sum minus comparison figure per breakdown. Diagnostics only, never required to be 0.
    diagnostics: dict[str, int] = field(default_factory=dict)

    def top_organic_landing_pages(self, n: int = 5) -> list[LandingPage]:
        """Top Organic Search landing pages by sessions, without placeholder rows."""
        return [p for p in self.organic_landing_pages if not p.is_placeholder][:n]


@dataclass
class Ga4Data:
    property_id: str
    key_events_status: str  # "recorded" | "none_recorded" | "unavailable"
    current: Ga4PeriodData
    previous: Ga4PeriodData

    @property
    def warnings(self) -> list[Ga4Warning]:
        return self.current.warnings + self.previous.warnings

    @property
    def truncated_reports(self) -> set[str]:
        return {w.report for w in self.warnings if w.code == "truncated"}


def build_client(credentials=None):
    # Imported lazily so the parsing code and its tests need no credentials.
    if credentials is None:
        from seo_report.auth import get_credentials

        credentials = get_credentials()
    return BetaAnalyticsDataClient(credentials=credentials)


def _clean(message) -> str:
    text = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "[email]", str(message))
    text = re.sub(r"\d[\d\s().-]{8,}\d", "[number]", text)
    return text[:200]


def _to_error(err: GoogleAPICallError, what: str) -> Ga4Error:
    status = getattr(err, "code", None)
    message = getattr(err, "message", "") or ""
    if status == 401:
        return Ga4Error(f"GA4 authentication failed ({what}); sign in again", status)
    if status == 403:
        return Ga4Error(
            f"GA4 denied access ({what}); check that the signed-in account can view the property", status
        )
    if status == 404:
        return Ga4Error(f"GA4 resource not found ({what}); check the property ID", status)
    if status == 400:
        return Ga4Error(
            f"GA4 rejected the request ({what}): {_clean(message)}",
            status,
            incompatible="incompatible" in message.lower(),
        )
    return Ga4Error(f"GA4 request failed ({what}): HTTP {status}", status)


def _call(fn, what: str, sleep):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except GoogleAPICallError as err:
            if getattr(err, "code", None) in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS:
                sleep(min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2 ** (attempt - 1)))
                continue
            raise _to_error(err, what) from None


def _request(property_id, period, metrics, dimensions, *, organic_only, order_by, limit, offset):
    request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=period.start.isoformat(), end_date=period.end.isoformat())],
        metrics=[Metric(name=m) for m in metrics],
        dimensions=[Dimension(name=d) for d in dimensions],
    )
    if organic_only:
        request.dimension_filter = FilterExpression(
            filter=Filter(
                field_name="sessionDefaultChannelGroup",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.EXACT, value=ORGANIC_CHANNEL
                ),
            )
        )
    if order_by == "date":
        request.order_bys = [OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))]
    elif order_by == "sessions":
        request.order_bys = [OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)]
    if dimensions:
        request.limit = limit
        request.offset = offset
    return request


@dataclass
class _Report:
    rows: list[tuple[dict, dict]]  # (dimension values, metric values)
    truncated: bool
    time_zone: str | None
    thresholded: bool
    sampling: list[str]
    other_row: bool
    key_events_dropped: bool


def _parse_rows(response) -> list[tuple[dict, dict]]:
    dim_names = [h.name for h in response.dimension_headers]
    metric_names = [h.name for h in response.metric_headers]
    parsed = []
    for row in response.rows:
        if len(row.dimension_values) != len(dim_names) or len(row.metric_values) != len(metric_names):
            raise Ga4Error("Unexpected GA4 response: row does not match its headers")
        try:
            metrics = {n: float(v.value) for n, v in zip(metric_names, row.metric_values)}
        except ValueError:
            raise Ga4Error("Unexpected GA4 response: non-numeric metric value") from None
        parsed.append(({n: v.value for n, v in zip(dim_names, row.dimension_values)}, metrics))
    return parsed


def _paginate(client, property_id, period, metrics, dimensions, cap, *, organic_only, order_by, label, sleep):
    rows, first, row_count = [], None, 0
    while True:
        limit = min(PAGE_SIZE, cap - len(rows)) if dimensions else None
        request = _request(property_id, period, metrics, dimensions, organic_only=organic_only,
                           order_by=order_by, limit=limit, offset=len(rows))
        response = _call(lambda: client.run_report(request), f"{label} report", sleep)
        if first is None:
            first = response
        row_count = response.row_count or len(response.rows)
        batch = _parse_rows(response)
        rows.extend(batch)
        if not dimensions or not batch or len(rows) >= min(cap, row_count):
            break
    meta = first.metadata
    return _Report(
        rows=rows,
        truncated=bool(dimensions) and row_count > len(rows),
        time_zone=meta.time_zone or None,
        thresholded=bool(meta.subject_to_thresholding),
        sampling=[
            f"{s.samples_read_count}/{s.sampling_space_size}" if s.sampling_space_size else "unknown ratio"
            for s in meta.sampling_metadatas
        ],
        other_row=bool(meta.data_loss_from_other_row),
        key_events_dropped=False,
    )


def _run(client, property_id, period, metrics, dimensions, cap, *, label, sleep, organic_only=False, order_by=None):
    metrics = list(metrics)
    dropped = False
    while True:
        try:
            report = _paginate(client, property_id, period, metrics, dimensions, cap,
                               organic_only=organic_only, order_by=order_by, label=label, sleep=sleep)
            report.key_events_dropped = dropped
            return report
        except Ga4Error as err:
            if err.incompatible and KEY_EVENTS_METRIC in metrics:
                metrics.remove(KEY_EVENTS_METRIC)
                dropped = True
                continue
            raise


def _metrics(values: dict) -> Ga4Metrics:
    sessions = int(round(values.get("sessions", 0)))
    engaged = int(round(values.get("engagedSessions", 0)))
    rate = None
    if sessions > 0:
        rate = values["engagementRate"] if "engagementRate" in values else engaged / sessions
    key_events = int(round(values[KEY_EVENTS_METRIC])) if KEY_EVENTS_METRIC in values else None
    return Ga4Metrics(sessions, int(round(values.get("totalUsers", 0))), engaged, rate, key_events)


def _check_schema(client, property_id, sleep) -> bool:
    """Verify required names exist; return whether the keyEvents metric is available."""
    metadata = _call(
        lambda: client.get_metadata(request=GetMetadataRequest(name=f"properties/{property_id}/metadata")),
        "property metadata",
        sleep,
    )
    metric_names = {m.api_name for m in metadata.metrics}
    dimension_names = {d.api_name for d in metadata.dimensions}
    missing = [m for m in REQUIRED_METRICS if m not in metric_names]
    missing += [d for d in REQUIRED_DIMENSIONS if d not in dimension_names]
    if missing:
        raise Ga4Error(
            f"GA4 property does not expose required fields: {missing}. No substitute fields were used"
        )
    return KEY_EVENTS_METRIC in metric_names


def verify_access(client, property_id, period: Period, *, sleep=time.sleep) -> dict:
    """Minimal read-only proof of access to ONE property (R8.7e.1b): its metadata (which must expose the fields the report needs) and one small report
    (a single metric, no dimensions) over `period`. Writes nothing and changes no GA4 setting. Returns safe metadata; raises `Ga4Error` (same
    classification as the full extraction) on any failure."""
    pid = str(property_id).strip()
    if not re.fullmatch(r"[0-9]{1,20}", pid):
        raise Ga4Error("The GA4 property ID must be numeric")
    key_events_supported = _check_schema(client, pid, sleep)
    request = _request(pid, period, ("sessions",), (), organic_only=False, order_by=None, limit=None, offset=0)
    response = _call(lambda: client.run_report(request), "verification report", sleep)
    rows = _parse_rows(response)  # validates the response against its own headers
    return {"key_events_supported": key_events_supported, "period_days": period.days, "rows": len(rows)}


class _Collector:
    def __init__(self):
        self.warnings: list[Ga4Warning] = []
        self.time_zone: str | None = None
        self._seen = set()

    def add(self, code, message, report=None):
        if (code, report) not in self._seen:
            self._seen.add((code, report))
            self.warnings.append(Ga4Warning(code, message, report))

    def note(self, report: _Report, label: str, cap: int):
        self.time_zone = self.time_zone or report.time_zone
        if report.truncated:
            self.add("truncated", f"Only the top {cap} {label} rows were fetched; more exist", label)
        if report.thresholded:
            self.add("thresholded", f"The {label} report is subject to GA4 data thresholding; some rows may be withheld", label)
        if report.sampling:
            self.add("sampled", f"The {label} report is sampled (samples read / space: {', '.join(report.sampling)})", label)
        if report.other_row:
            self.add("other_row", f"The {label} report folded some rows into '(other)'", label)
        if report.key_events_dropped:
            self.add("key_events_unavailable", f"Key events could not be combined with the {label} report", label)


def _landing_pages(report: _Report) -> list[LandingPage]:
    pages = [
        LandingPage(dims["landingPage"], _metrics(values), dims["landingPage"] in PLACEHOLDER_PAGES)
        for dims, values in report.rows
    ]
    pages.sort(key=lambda p: (-p.metrics.sessions, p.path))
    return pages


def _daily_sessions(report: _Report, period: Period) -> list[DailySessions]:
    total: dict[dt.date, int] = {}
    organic: dict[dt.date, int] = {}
    for dims, values in report.rows:
        try:
            day = dt.datetime.strptime(dims["date"], "%Y%m%d").date()
        except ValueError:
            raise Ga4Error("Unexpected GA4 response: invalid date value") from None
        sessions = int(round(values["sessions"]))
        total[day] = total.get(day, 0) + sessions
        if dims.get("sessionDefaultChannelGroup") == ORGANIC_CHANNEL:
            organic[day] = organic.get(day, 0) + sessions
    return [DailySessions(d, total.get(d, 0), organic.get(d, 0), d in total) for d in period.dates()]


def fetch_period(client, property_id: str, period: Period, *, key_events_supported: bool,
                 today=None, sleep=time.sleep) -> Ga4PeriodData:
    base = list(REQUIRED_METRICS) + ([KEY_EVENTS_METRIC] if key_events_supported else [])
    collector = _Collector()

    totals = _run(client, property_id, period, base, [], 1, label="totals", sleep=sleep)
    collector.note(totals, "totals", 1)
    whole = _metrics(totals.rows[0][1]) if totals.rows else EMPTY_METRICS
    if whole.sessions == 0 and whole.total_users == 0:
        collector.add("empty_period", f"No GA4 data for {period.start} to {period.end}")
        return Ga4PeriodData(
            period, EMPTY_METRICS, EMPTY_METRICS, is_empty=True,
            daily=_daily_sessions(_Report([], False, None, False, [], False, False), period),
            time_zone=collector.time_zone, warnings=collector.warnings,
        )

    channel_report = _run(client, property_id, period, base, ["sessionDefaultChannelGroup"],
                          MAX_ROWS["channels"], label="channels", sleep=sleep, order_by="sessions")
    collector.note(channel_report, "channels", MAX_ROWS["channels"])
    channels = [ChannelRow(dims["sessionDefaultChannelGroup"], _metrics(values)) for dims, values in channel_report.rows]
    channels.sort(key=lambda c: (-c.metrics.sessions, c.channel))
    organic = next((c.metrics for c in channels if c.channel == ORGANIC_CHANNEL), None)
    if organic is None:  # no row means no Organic Search sessions in the period
        organic = Ga4Metrics(0, 0, 0, None, 0 if key_events_supported and whole.key_events is not None else None)

    daily_report = _run(client, property_id, period, ["sessions"], ["date", "sessionDefaultChannelGroup"],
                        MAX_ROWS["daily"], label="daily", sleep=sleep, order_by="date")
    collector.note(daily_report, "daily", MAX_ROWS["daily"])
    daily = _daily_sessions(daily_report, period)

    pages_report = _run(client, property_id, period, base, ["landingPage"], MAX_ROWS["landing_pages"],
                        label="landing_pages", sleep=sleep, order_by="sessions")
    collector.note(pages_report, "landing_pages", MAX_ROWS["landing_pages"])
    organic_report = _run(client, property_id, period, base, ["landingPage"], MAX_ROWS["organic_landing_pages"],
                          label="organic_landing_pages", sleep=sleep, organic_only=True, order_by="sessions")
    collector.note(organic_report, "organic_landing_pages", MAX_ROWS["organic_landing_pages"])
    landing, organic_landing = _landing_pages(pages_report), _landing_pages(organic_report)

    today = today or dt.date.today()
    if not daily[-1].has_data and (today - period.end).days <= RECENT_DAYS:
        collector.add("incomplete_data", f"No GA4 sessions on {period.end}; recent GA4 data may still be processing")

    diagnostics = {
        "channel_sessions_diff": sum(c.metrics.sessions for c in channels) - whole.sessions,
        "daily_sessions_diff": sum(d.sessions for d in daily) - whole.sessions,
        "landing_sessions_diff": sum(p.metrics.sessions for p in landing) - whole.sessions,
        "organic_landing_sessions_diff": sum(p.metrics.sessions for p in organic_landing) - organic.sessions,
    }
    return Ga4PeriodData(
        period, whole, organic, channels=channels, daily=daily,
        landing_pages=landing, organic_landing_pages=organic_landing,
        time_zone=collector.time_zone, warnings=collector.warnings, diagnostics=diagnostics,
    )


def _key_events_status(supported: bool, current: Ga4PeriodData, previous: Ga4PeriodData) -> str:
    if not supported:
        return "unavailable"
    values = [p.whole_site.key_events for p in (current, previous)]
    if any(v is not None and v > 0 for v in values):
        return "recorded"
    if any(v == 0 for v in values) or (current.is_empty and previous.is_empty):
        return "none_recorded"
    return "unavailable"


def fetch_ga4(client, property_id: str, periods: ReportPeriods, *, today=None, sleep=time.sleep) -> Ga4Data:
    property_id = str(property_id).strip()
    if not re.fullmatch(r"\d+", property_id, re.ASCII):
        raise Ga4Error("GA4 property ID must be numeric")
    supported = _check_schema(client, property_id, sleep)
    current = fetch_period(client, property_id, periods.current, key_events_supported=supported, today=today, sleep=sleep)
    previous = fetch_period(client, property_id, periods.previous, key_events_supported=supported, today=today, sleep=sleep)
    return Ga4Data(property_id, _key_events_status(supported, current, previous), current, previous)
