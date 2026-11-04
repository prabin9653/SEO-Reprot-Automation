"""Search Console (Search Analytics) data for one property over two periods.

`average_position` here is Search Console's impression-weighted figure across all
queries. It is unrelated to the manually maintained keyword rankings: nothing in this
module reads, fills or compares those, and a missing position is always None, never 0.
"""

import datetime as dt
import time
from dataclasses import dataclass, field

from googleapiclient.errors import HttpError

from seo_report.countries import country_name
from seo_report.periods import Period, ReportPeriods

SEARCH_TYPE = "web"
DATA_STATE = "final"
PAGE_SIZE = 1000
MAX_ROWS = {"date": 1000, "country": 300, "query": 1000, "page": 500}
MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
PROBE_DAYS = 7


class SearchConsoleError(RuntimeError):
    """A Search Console request failed or returned an unexpected response."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Metrics:
    clicks: int
    impressions: int
    ctr: float | None  # fraction (0.003 = 0.3%); None when there are no impressions
    position: float | None  # Search Console average position; None when unavailable


EMPTY_METRICS = Metrics(0, 0, None, None)


@dataclass(frozen=True)
class DailyPoint:
    date: dt.date
    metrics: Metrics
    has_data: bool


@dataclass(frozen=True)
class DimensionRow:
    key: str
    label: str  # country name for countries, otherwise the key
    metrics: Metrics


@dataclass(frozen=True)
class DataWarning:
    """A limitation that report code should surface (codes: empty_period, truncated, incomplete_data)."""

    code: str
    message: str
    dimension: str | None = None


@dataclass
class PeriodData:
    period: Period
    totals: Metrics
    daily: list[DailyPoint] = field(default_factory=list)
    countries: list[DimensionRow] = field(default_factory=list)
    queries: list[DimensionRow] = field(default_factory=list)
    pages: list[DimensionRow] = field(default_factory=list)
    latest_data_date: dt.date | None = None
    warnings: list[DataWarning] = field(default_factory=list)
    # Sum minus total for each breakdown. Diagnostics only: anonymized queries make query
    # sums smaller, page-level aggregation can make page sums larger, and truncation
    # shrinks any of them.
    diagnostics: dict[str, int] = field(default_factory=dict)


@dataclass
class SearchConsoleData:
    site_url: str
    permission_level: str
    current: PeriodData
    previous: PeriodData

    @property
    def warnings(self) -> list[DataWarning]:
        return self.current.warnings + self.previous.warnings

    @property
    def truncated_dimensions(self) -> set[str]:
        return {w.dimension for w in self.warnings if w.code == "truncated"}


def build_service(credentials=None):
    # Imported lazily so the parsing code and its tests need no credentials.
    from googleapiclient.discovery import build

    if credentials is None:
        from seo_report.auth import get_credentials

        credentials = get_credentials()
    return build("searchconsole", "v1", credentials=credentials, cache_discovery=False)


def _to_error(err: HttpError, what: str) -> SearchConsoleError:
    status = int(err.resp.status)
    if status == 401:
        return SearchConsoleError(f"Search Console authentication failed ({what}); sign in again", status)
    if status == 403:
        return SearchConsoleError(
            f"Search Console denied access ({what}); check that the signed-in account has access to the property",
            status,
        )
    if status == 404:
        return SearchConsoleError(f"Search Console resource not found ({what}); check the property URL", status)
    return SearchConsoleError(f"Search Console request failed ({what}): HTTP {status} {err.reason or ''}".strip(), status)


def _execute(request, what: str, sleep):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return request.execute()
        except HttpError as err:
            if int(err.resp.status) in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS:
                sleep(min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2 ** (attempt - 1)))
                continue
            raise _to_error(err, what) from None


def _body(period: Period, dimensions=(), row_limit=None, start_row=0) -> dict:
    body = {
        "startDate": period.start.isoformat(),
        "endDate": period.end.isoformat(),
        "type": SEARCH_TYPE,
        "dataState": DATA_STATE,
    }
    if dimensions:
        body["dimensions"] = list(dimensions)
        body["rowLimit"] = row_limit
        body["startRow"] = start_row
    return body


def _query(service, site_url: str, body: dict, what: str, sleep) -> dict:
    request = service.searchanalytics().query(siteUrl=site_url, body=body)
    return _execute(request, what, sleep)


def _fetch_rows(service, site_url, period, dimension, sleep):
    """Page through one dimension. Returns (rows, truncated)."""
    max_rows = MAX_ROWS[dimension]
    rows: list[dict] = []
    while True:
        limit = min(PAGE_SIZE, max_rows - len(rows))
        body = _body(period, [dimension], limit, len(rows))
        batch = _query(service, site_url, body, f"{dimension} rows", sleep).get("rows", [])
        rows.extend(batch)
        if len(batch) < limit:
            return rows, False
        if len(rows) >= max_rows:
            probe = _body(period, [dimension], 1, max_rows)
            more = _query(service, site_url, probe, f"{dimension} rows", sleep).get("rows", [])
            return rows, bool(more)


def _metrics(row: dict) -> Metrics:
    clicks = int(round(row.get("clicks") or 0))
    impressions = int(round(row.get("impressions") or 0))
    if impressions <= 0:
        return Metrics(clicks, 0, None, None)
    ctr = row.get("ctr")
    position = row.get("position")
    return Metrics(
        clicks,
        impressions,
        float(ctr) if ctr is not None else clicks / impressions,
        float(position) if position is not None else None,
    )


def _dimension_rows(raw: list[dict], dimension: str) -> list[DimensionRow]:
    parsed = []
    for row in raw:
        keys = row.get("keys")
        if not keys:
            raise SearchConsoleError(f"Unexpected Search Console response: {dimension} row without keys")
        key = str(keys[0])
        parsed.append(DimensionRow(key, country_name(key) if dimension == "country" else key, _metrics(row)))
    parsed.sort(key=lambda r: (-r.metrics.clicks, -r.metrics.impressions, r.key))
    return parsed


def _daily_points(raw: list[dict], period: Period) -> list[DailyPoint]:
    by_date = {}
    for row in raw:
        keys = row.get("keys")
        try:
            by_date[dt.date.fromisoformat(keys[0])] = _metrics(row)
        except (TypeError, IndexError, ValueError):
            raise SearchConsoleError("Unexpected Search Console response: invalid date row") from None
    return [
        DailyPoint(day, by_date[day], True) if day in by_date else DailyPoint(day, EMPTY_METRICS, False)
        for day in period.dates()
    ]


def _check_completeness(service, site_url, period, daily, today, sleep):
    """Warn when the period's last day has no data and nothing later suggests it is just a quiet day."""
    if daily[-1].has_data:
        return []
    latest = max((p.date for p in daily if p.has_data), default=None)
    note = f"No data was returned for {period.end}; latest date with data: {latest or 'none'}"
    today = today or dt.date.today()
    if today <= period.end:
        return [DataWarning("incomplete_data", f"{note}. The period has not finished yet.")]
    probe_period = Period(period.end + dt.timedelta(days=1), min(period.end + dt.timedelta(days=PROBE_DAYS), today))
    rows = _query(service, site_url, _body(probe_period, ["date"], PROBE_DAYS), "completeness probe", sleep).get("rows", [])
    if rows:
        return []
    return [DataWarning("incomplete_data", f"{note}. Data may not be finalized yet, or there were no impressions.")]


def _diagnostics(totals, daily, countries, queries, pages) -> dict[str, int]:
    result = {}
    for name, rows in (("daily", [p.metrics for p in daily]), ("country", [r.metrics for r in countries]),
                       ("query", [r.metrics for r in queries]), ("page", [r.metrics for r in pages])):
        result[f"{name}_clicks_diff"] = sum(m.clicks for m in rows) - totals.clicks
        result[f"{name}_impressions_diff"] = sum(m.impressions for m in rows) - totals.impressions
    return result


def fetch_period(service, site_url: str, period: Period, *, today=None, sleep=time.sleep) -> PeriodData:
    totals_rows = _query(service, site_url, _body(period), "totals", sleep).get("rows", [])
    totals = _metrics(totals_rows[0]) if totals_rows else EMPTY_METRICS
    if totals.impressions == 0:
        warning = DataWarning("empty_period", f"No Search Console data for {period.start} to {period.end}")
        return PeriodData(period, EMPTY_METRICS, daily=_daily_points([], period), warnings=[warning])

    raw_daily, _ = _fetch_rows(service, site_url, period, "date", sleep)
    daily = _daily_points(raw_daily, period)
    warnings = _check_completeness(service, site_url, period, daily, today, sleep)

    breakdowns = {}
    for dimension in ("country", "query", "page"):
        raw, truncated = _fetch_rows(service, site_url, period, dimension, sleep)
        breakdowns[dimension] = _dimension_rows(raw, dimension)
        if truncated:
            warnings.append(
                DataWarning(
                    "truncated",
                    f"Only the top {MAX_ROWS[dimension]} {dimension} rows were fetched; more exist",
                    dimension,
                )
            )

    return PeriodData(
        period=period,
        totals=totals,
        daily=daily,
        countries=breakdowns["country"],
        queries=breakdowns["query"],
        pages=breakdowns["page"],
        latest_data_date=max((p.date for p in daily if p.has_data), default=None),
        warnings=warnings,
        diagnostics=_diagnostics(totals, daily, breakdowns["country"], breakdowns["query"], breakdowns["page"]),
    )


def check_access(service, site_url: str, sleep=time.sleep) -> str:
    """Return the account's permission level for the exact property, or raise."""
    response = _execute(service.sites().list(), "list accessible properties", sleep)
    for entry in response.get("siteEntry", []):
        if entry.get("siteUrl") == site_url:
            level = entry.get("permissionLevel", "")
            if level == "siteUnverifiedUser":
                raise SearchConsoleError(f"The account is an unverified user of {site_url!r} and cannot query it")
            return level
    raise SearchConsoleError(f"Property {site_url!r} is not accessible to the signed-in account")


def is_valid_property(site_url) -> bool:
    """A Search Console property as the API names it: a URL-prefix property (`https://example.com/`) or a domain property (`sc-domain:example.com`)."""
    import re
    return isinstance(site_url, str) and re.fullmatch(r"(?:sc-domain:[A-Za-z0-9.-]+|https?://\S+)", site_url) is not None


def verify_access(service, site_url: str, period: Period, *, sleep=time.sleep) -> dict:
    """Minimal read-only proof of access to ONE property (R8.7e.1b): the property is in the account's accessible list (not as an unverified user) and one
    totals query over `period` (no dimensions, no breakdowns) is answered. Writes nothing. Returns safe metadata; raises `SearchConsoleError` (same
    classification as the full extraction) on any failure. A valid answer with no rows (a quiet property) is a successful verification."""
    if not is_valid_property(site_url):
        raise SearchConsoleError("The Search Console property identifier is not a URL-prefix or sc-domain property")
    permission = check_access(service, site_url, sleep)
    response = _query(service, site_url, _body(period), "verification totals", sleep)
    if not isinstance(response, dict):
        raise SearchConsoleError("Unexpected Search Console response to the verification query")
    return {"permission": permission, "period_days": period.days, "rows": len(response.get("rows", []))}


def fetch_search_console(service, site_url: str, periods: ReportPeriods, *, today=None, sleep=time.sleep) -> SearchConsoleData:
    permission = check_access(service, site_url, sleep)
    return SearchConsoleData(
        site_url=site_url,
        permission_level=permission,
        current=fetch_period(service, site_url, periods.current, today=today, sleep=sleep),
        previous=fetch_period(service, site_url, periods.previous, today=today, sleep=sleep),
    )
