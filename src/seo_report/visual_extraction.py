"""Pure extraction of the render-input visuals from a stored or built snapshot payload (R2, updated in R2.1).

`extract_search_console`, `extract_geography` and `extract_ga4` read a snapshot payload (`Snapshot.payload`, the
dictionary written by `snapshots.snapshot.build_snapshot`) and return the R1 types `SearchConsoleVisuals`,
`GeographyVisuals` and `Ga4Visuals`. `extract_visuals` runs all three. Everything is deterministic and pure: no clock,
environment, file, database or network access; the payload is only read, never changed; a new typed object is returned.
Only the current reporting period is used (`periods.current`); the previous period is not drawn.

Source fields used (payload path -> R1 field)
  Search Console   gsc.provided                                  availability
                   gsc.periods.current.status / .issues          completeness (see the rules below)
                   gsc.periods.current.totals.clicks             SearchConsoleVisuals.clicks
                   gsc.periods.current.totals.impressions        .impressions
                   gsc.periods.current.totals.ctr                .ctr (a fraction, as stored; 0.03 = 3%)
                   gsc.periods.current.totals.position           .average_position (Search Console's own average)
                   gsc.periods.current.daily[]                   .daily: DailyPoint(date, clicks, impressions), only
                                                                 days with has_data true, in the stored (ascending) order
  Geography        gsc.periods.current.countries[]               CountryRow(name, clicks, impressions), in the stored
                                                                 order (clicks, then impressions, descending). `name`
                                                                 is the source's own country label. The client's target
                                                                 country (client.target_country) is never used.
                   gsc.periods.current.source_truncated          "country" -> TruncationNote("countries",
                                                                 "source_limit")
                   gsc.periods.current.capped.countries          true -> TruncationNote("countries", "snapshot_cap")
  GA4              ga4.provided, ga4.periods.current.status/.issues   as for Search Console
                   ga4.periods.current.site.sessions             Ga4Visuals.sessions
                   ga4.periods.current.site.total_users          .users
                   ga4.periods.current.site.engagement_rate      .engagement_rate (a fraction)
                   ga4.periods.current.channels[]                ChannelRow(channel, sessions, total_users,
                                                                 engaged_sessions), in the stored order
                   ga4.periods.current.daily[]                   .daily: Ga4DailyPoint(date, sessions), only days with
                                                                 has_data true, in the stored (ascending) order
                   ga4.periods.current.source_truncated          "channels" / "daily" -> TruncationNote(that name,
                                                                 "source_limit")

Source fields intentionally not used: GSC queries and pages (no R1 field), GSC daily ctr/position, warnings text,
latest_data_date, GA4 key events, organic totals, organic daily sessions, landing pages, all previous-period data, site
and property identifiers (never client-visible), and `source_truncated` entries that belong to datasets a visual does
not use (a truncated query list does not affect the Search Console visual).

Availability and completeness (R2.1)
  unavailable  the source was not provided; or the period is "empty" (the snapshot stores no totals for an empty
               period, so a stored zero is never produced); or nothing usable is left (no total and no daily point;
               no country row; no total, channel row or daily point)
  incomplete   usable data, and the period's `issues` list contains "incomplete" (the source reported incomplete
               coverage, for example no data on the period's last day); the figures are kept, not discarded
  available    usable data and no "incomplete" issue
  `status` and `issues` must agree (status is the first of empty, incomplete, truncated that appears in issues, else
  "ok"); a disagreement is a malformed payload.

Partial metrics. Every total is optional: a missing one (None) stays None and the visual stays available (or
incomplete) while the other values are valid. Nothing is calculated to replace a missing value, and a missing value is
never turned into zero. Zero is data: a stored 0 stays 0.

Truncation. A truncated dataset is described by a `TruncationNote(dataset, reason, omitted_rows)`:
  source_limit  the source reported that more rows exist than it returned (the number is unknown, so None)
  snapshot_cap  the snapshot stored only its maximum rows for the dataset (number unknown, None); the snapshot sets
                this flag for countries only
  row_limit     `max_rows` / `max_channels` kept fewer rows than the snapshot holds; `omitted_rows` is the exact number
                left out (a count, not an estimate)
GA4 channels are cut at 100 rows by the snapshot without a flag; that cap cannot be detected here and is not reported.

Daily series. Days without data (has_data false) are left out; no zero or empty point is created. Within the period,
a date that is absent from the series has no record, and a date present with 0 was measured as 0. Dates must be
strictly increasing (the source's own order); a duplicate or out-of-order date is a malformed payload.

Malformed source structures (wrong types, negative or boolean counts, non-finite numbers, bad dates, missing keys,
unknown or contradictory status values) raise `VisualExtractionError`; its `code` is a fixed identifier and the message
names a payload path, never a value.
"""

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal

from seo_report import render_input as ri
from seo_report.snapshots import eligibility as el

SOURCE_STATUSES = ("ok", "truncated", "incomplete", "empty")
SOURCE_ISSUES = ("empty", "incomplete", "truncated")


class VisualExtractionError(ValueError):
    """The snapshot payload does not have the expected structure. `code` is fixed; the message names a path only."""

    def __init__(self, code: str, path: str):
        super().__init__(f"{path}: {code}")
        self.code = code
        self.path = path


@dataclass(frozen=True)
class ExtractedVisuals:
    search_console: ri.SearchConsoleVisuals
    geography: ri.GeographyVisuals
    ga4: ri.Ga4Visuals


# --- reading the payload ---------------------------------------------------------------------------------------------------

def _payload(source) -> dict:
    payload = getattr(source, "payload", source)  # a Snapshot or its payload dictionary
    if not isinstance(payload, dict):
        raise VisualExtractionError("payload_not_a_mapping", "payload")
    if payload.get("format") not in el.SUPPORTED_FORMATS:
        raise VisualExtractionError("unsupported_format", "payload.format")
    return payload


def _mapping(value, path):
    if not isinstance(value, dict):
        raise VisualExtractionError("not_a_mapping", path)
    return value


def _list(value, path):
    if not isinstance(value, list):
        raise VisualExtractionError("not_a_list", path)
    return value


def _require(mapping, key, path):
    if key not in mapping:
        raise VisualExtractionError("missing_key", f"{path}.{key}")
    return mapping[key]


def _count(value, path, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise VisualExtractionError("not_a_whole_number", path)
    if value < 0:
        raise VisualExtractionError("negative_number", path)
    return value


def _fraction(value, path):
    """A float (or int) as an exact Decimal of its shortest text form; None stays None."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VisualExtractionError("not_a_number", path)
    if isinstance(value, float) and not math.isfinite(value):
        raise VisualExtractionError("not_finite", path)
    if value < 0:
        raise VisualExtractionError("negative_number", path)
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        raise VisualExtractionError("not_text", path)
    return value


def _date(value, path):
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise VisualExtractionError("not_an_iso_date", path) from None


def _limit(value, name):
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise ValueError(f"{name} must be None or a whole number of at least 1")
    return value


@dataclass(frozen=True)
class _Period:
    block: dict
    path: str
    empty: bool
    incomplete: bool


def _current_period(payload, source_key):
    """The current-period block of a provided source (with its completeness), or None when not provided."""
    block = _mapping(_require(payload, source_key, "payload"), f"payload.{source_key}")
    provided = _require(block, "provided", f"payload.{source_key}")
    if not isinstance(provided, bool):
        raise VisualExtractionError("not_a_boolean", f"payload.{source_key}.provided")
    if not provided:
        return None
    periods = _mapping(_require(block, "periods", f"payload.{source_key}"), f"payload.{source_key}.periods")
    path = f"payload.{source_key}.periods.current"
    current = _mapping(_require(periods, "current", f"payload.{source_key}.periods"), path)
    status = _require(current, "status", path)
    if status not in SOURCE_STATUSES:
        raise VisualExtractionError("unknown_status", f"{path}.status")
    issues = _list(_require(current, "issues", path), f"{path}.issues")
    if any(i not in SOURCE_ISSUES for i in issues) or len(set(issues)) != len(issues):
        raise VisualExtractionError("unknown_issue", f"{path}.issues")
    expected = next((name for name in ("empty", "incomplete", "truncated") if name in issues), "ok")
    if status != expected:
        raise VisualExtractionError("status_contradicts_issues", f"{path}.status")
    return _Period(current, path, "empty" in issues, "incomplete" in issues)


def _truncated_labels(period: _Period):
    labels = _list(_require(period.block, "source_truncated", period.path), f"{period.path}.source_truncated")
    for index, label in enumerate(labels):
        _text(label, f"{period.path}.source_truncated[{index}]")
    return set(labels)


def _daily(rows, path, build):
    """The daily rows that have data, in the stored order. Duplicate dates are malformed."""
    result, seen = [], set()
    for index, row in enumerate(_list(rows, path)):
        where = f"{path}[{index}]"
        row = _mapping(row, where)
        day = _date(_require(row, "date", where), f"{where}.date")
        if day in seen:
            raise VisualExtractionError("duplicate_date", f"{where}.date")
        seen.add(day)
        has_data = _require(row, "has_data", where)
        if not isinstance(has_data, bool):
            raise VisualExtractionError("not_a_boolean", f"{where}.has_data")
        if has_data:
            result.append(build(day, row, where))
    if [p.day for p in result] != sorted(p.day for p in result):
        raise VisualExtractionError("dates_out_of_order", path)
    return tuple(result)


def _status(period: _Period) -> str:
    return "incomplete" if period.incomplete else "available"


# --- Search Console -----------------------------------------------------------------------------------------------------

def extract_search_console(source) -> ri.SearchConsoleVisuals:
    """`SearchConsoleVisuals` from the current period of the payload's Search Console block."""
    payload = _payload(source)
    period = _current_period(payload, "gsc")
    if period is None or period.empty:
        return ri.SearchConsoleVisuals.unavailable()
    current, path = period.block, period.path
    totals = _mapping(_require(current, "totals", path), f"{path}.totals")
    clicks = _count(_require(totals, "clicks", f"{path}.totals"), f"{path}.totals.clicks", optional=True)
    impressions = _count(_require(totals, "impressions", f"{path}.totals"), f"{path}.totals.impressions",
                         optional=True)
    ctr = _fraction(_require(totals, "ctr", f"{path}.totals"), f"{path}.totals.ctr")
    position = _fraction(_require(totals, "position", f"{path}.totals"), f"{path}.totals.position")
    daily = _daily(_require(current, "daily", path), f"{path}.daily", lambda day, row, where: ri.DailyPoint(
        day, _count(row.get("clicks"), f"{where}.clicks"), _count(row.get("impressions"), f"{where}.impressions")))
    if all(value is None for value in (clicks, impressions, ctr, position)) and not daily:
        return ri.SearchConsoleVisuals.unavailable()
    return ri.SearchConsoleVisuals(_status(period), clicks, impressions, ctr, position, daily)


# --- geography --------------------------------------------------------------------------------------------------------------

def extract_geography(source, *, max_rows: int | None = None) -> ri.GeographyVisuals:
    """`GeographyVisuals` from the measured Search Console country rows (never the client's target country).

    `max_rows` keeps only the first rows of the stored order (clicks, then impressions, descending); None keeps all.
    Rows left out are reported as a `row_limit` truncation note with their exact number. A renderer should pass the
    number of rows it actually displays, so the hash covers exactly what is drawn.
    """
    _limit(max_rows, "max_rows")
    payload = _payload(source)
    period = _current_period(payload, "gsc")
    if period is None or period.empty:
        return ri.GeographyVisuals.unavailable()
    current, path = period.block, period.path
    rows = []
    for index, row in enumerate(_list(_require(current, "countries", path), f"{path}.countries")):
        where = f"{path}.countries[{index}]"
        row = _mapping(row, where)
        rows.append(ri.CountryRow(_text(row.get("name"), f"{where}.name"),
                                  _count(row.get("clicks"), f"{where}.clicks"),
                                  _count(row.get("impressions"), f"{where}.impressions")))
    notes = []
    if "country" in _truncated_labels(period):
        notes.append(ri.TruncationNote("countries", "source_limit"))
    capped = _mapping(_require(current, "capped", path), f"{path}.capped")
    flag = _require(capped, "countries", f"{path}.capped")
    if not isinstance(flag, bool):
        raise VisualExtractionError("not_a_boolean", f"{path}.capped.countries")
    if flag:
        notes.append(ri.TruncationNote("countries", "snapshot_cap"))
    if max_rows is not None and len(rows) > max_rows:
        notes.append(ri.TruncationNote("countries", "row_limit", len(rows) - max_rows))
        rows = rows[:max_rows]
    if not rows:
        return ri.GeographyVisuals.unavailable()
    return ri.GeographyVisuals(_status(period), tuple(rows), tuple(notes))


# --- GA4 --------------------------------------------------------------------------------------------------------------------

def extract_ga4(source, *, max_channels: int | None = None) -> ri.Ga4Visuals:
    """`Ga4Visuals` from the current period of the payload's GA4 block (whole-site totals, channels, daily sessions).

    `max_channels` keeps only the first channel rows of the stored order (sessions descending); None keeps all. Rows
    left out are reported as a `row_limit` truncation note with their exact number.
    """
    _limit(max_channels, "max_channels")
    payload = _payload(source)
    period = _current_period(payload, "ga4")
    if period is None or period.empty:
        return ri.Ga4Visuals.unavailable()
    current, path = period.block, period.path
    site = _mapping(_require(current, "site", path), f"{path}.site")
    sessions = _count(_require(site, "sessions", f"{path}.site"), f"{path}.site.sessions", optional=True)
    users = _count(_require(site, "total_users", f"{path}.site"), f"{path}.site.total_users", optional=True)
    rate = _fraction(_require(site, "engagement_rate", f"{path}.site"), f"{path}.site.engagement_rate")
    channels = []
    for index, row in enumerate(_list(_require(current, "channels", path), f"{path}.channels")):
        where = f"{path}.channels[{index}]"
        row = _mapping(row, where)
        channels.append(ri.ChannelRow(_text(row.get("channel"), f"{where}.channel"),
                                      _count(row.get("sessions"), f"{where}.sessions"),
                                      _count(row.get("total_users"), f"{where}.total_users"),
                                      _count(row.get("engaged_sessions"), f"{where}.engaged_sessions")))
    daily = _daily(_require(current, "daily", path), f"{path}.daily", lambda day, row, where: ri.Ga4DailyPoint(
        day, _count(row.get("sessions"), f"{where}.sessions")))
    notes = [ri.TruncationNote(label, "source_limit") for label in ("channels", "daily")
             if label in _truncated_labels(period)]
    if max_channels is not None and len(channels) > max_channels:
        notes.append(ri.TruncationNote("channels", "row_limit", len(channels) - max_channels))
        channels = channels[:max_channels]
    if all(value is None for value in (sessions, users, rate)) and not channels and not daily:
        return ri.Ga4Visuals.unavailable()
    return ri.Ga4Visuals(_status(period), sessions, users, rate, tuple(channels), daily, tuple(notes))


def extract_visuals(source, *, max_countries: int | None = None, max_channels: int | None = None) -> ExtractedVisuals:
    """All three visuals from one payload."""
    return ExtractedVisuals(extract_search_console(source), extract_geography(source, max_rows=max_countries),
                            extract_ga4(source, max_channels=max_channels))
