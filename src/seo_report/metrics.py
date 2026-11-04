"""Period-over-period comparisons and summaries for the report.

Pure functions over data that has already been fetched: no I/O, no API calls, no clock.
Search Console figures, GA4 figures and the manually maintained keyword rankings are kept
in separate structures and are never combined or reconciled with each other.

Search Console and GA4 inputs are duck-typed (they only need the attributes read below),
so this module does not import the source modules.
"""

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import unquote, urlsplit

from seo_report.rankings import RankingKind

PLACEHOLDER_LABELS = {"(not set)", "(other)", "(not provided)", "(none)"}
MAX_QUERY_LABEL_CHARS = 80
MAX_PAGE_LABEL_CHARS = 100

_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_PHONE_LIKE_RE = re.compile(r"\d(?:[\s().+-]*\d){6,}")
_LONG_DIGITS_RE = re.compile(r"\d{9,}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{24,}")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@dataclass(frozen=True)
class Thresholds:
    small_base_count: int = 20  # previous counts below this get absolute wording only
    meaningful_percent: float = 0.10  # smaller relative changes count as flat
    position_min_delta: float = 1.0  # Search Console average position
    ctr_min_delta: float = 0.001  # 0.1 percentage points
    ctr_min_impressions: int = 500
    engagement_min_delta: float = 0.05  # 5 percentage points (also used for organic share)
    ga4_min_sessions: int = 20
    concentration_share: float = 0.60
    concentration_min_sessions: int = 10
    striking_min_position: float = 4.0
    striking_max_position: float = 20.0
    striking_min_impressions: int = 20
    zero_click_min_impressions: int = 50
    list_limit: int = 10


class Direction(Enum):
    UP = "up"  # the number went up (for position, that is worse)
    DOWN = "down"
    FLAT = "flat"  # no meaningful change
    NOT_COMPARABLE = "not_comparable"


@dataclass(frozen=True)
class Change:
    """A comparison of one figure between the current and the previous period.

    `percent` is a fraction (0.25 = +25%) and is only set for counts whose previous value
    is not a small base. `favorable` is None unless the change is meaningful.
    """

    current: float | None
    previous: float | None
    absolute: float | None
    percent: float | None
    direction: Direction
    favorable: bool | None
    small_base: bool = False
    insufficient_volume: bool = False

    @property
    def comparable(self) -> bool:
        return self.direction is not Direction.NOT_COMPARABLE

    def evidence(self) -> dict:
        return {
            "current": self.current,
            "previous": self.previous,
            "absolute": self.absolute,
            "percent": self.percent,
            "direction": self.direction.value,
        }


def _na(current=None, previous=None, *, insufficient_volume=False) -> Change:
    return Change(current, previous, None, None, Direction.NOT_COMPARABLE, None, False, insufficient_volume)


def _direction(absolute: float, flat: bool) -> Direction:
    if flat or absolute == 0:
        return Direction.FLAT
    return Direction.UP if absolute > 0 else Direction.DOWN


def compare_count(current, previous, th: Thresholds = Thresholds()) -> Change:
    """Compare a count where more is better (clicks, impressions, sessions, users)."""
    if current is None or previous is None:
        return _na(current, previous)
    absolute = current - previous
    small = previous < th.small_base_count
    if small:
        percent, flat = None, False
    else:
        percent = absolute / previous
        flat = abs(percent) < th.meaningful_percent
    direction = _direction(absolute, flat)
    favorable = None if direction is Direction.FLAT else direction is Direction.UP
    return Change(current, previous, absolute, percent, direction, favorable, small_base=small)


def compare_rate(current, previous, *, current_volume, previous_volume, min_volume, min_delta) -> Change:
    """Compare a rate (CTR, engagement rate, share) where higher is better.

    Needs at least `min_volume` underlying events in both periods; the change must be at
    least `min_delta` (an absolute difference in the rate) to count as meaningful.
    """
    if current is None or previous is None:
        return _na(current, previous)
    if (current_volume is None or previous_volume is None
            or current_volume < min_volume or previous_volume < min_volume):
        return _na(current, previous, insufficient_volume=True)
    absolute = current - previous
    direction = _direction(absolute, round(abs(absolute), 10) < min_delta)
    favorable = None if direction is Direction.FLAT else direction is Direction.UP
    return Change(current, previous, absolute, None, direction, favorable)


def compare_position(current, previous, th: Thresholds = Thresholds()) -> Change:
    """Compare Search Console average position, where a lower number is better."""
    if current is None or previous is None:
        return _na(current, previous)
    absolute = current - previous
    direction = _direction(absolute, round(abs(absolute), 10) < th.position_min_delta)
    favorable = None if direction is Direction.FLAT else direction is Direction.DOWN
    return Change(current, previous, absolute, None, direction, favorable)


# --- display-safe labels -------------------------------------------------------------

def _collapse(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def safe_query_label(query) -> str | None:
    """Return the query text if it is safe to print, otherwise None."""
    text = _collapse(query)
    if not text or len(text) > MAX_QUERY_LABEL_CHARS or text.casefold() in PLACEHOLDER_LABELS:
        return None
    if "@" in text or _URL_RE.search(text) or _PHONE_LIKE_RE.search(text):
        return None
    if any(_TOKEN_RE.fullmatch(word) for word in text.split(" ")):
        return None
    return text


def safe_page_label(page) -> str | None:
    """Return the page path (no host, query string or fragment) if it is safe to print."""
    text = _collapse(page)
    if not text or text.casefold() in PLACEHOLDER_LABELS:
        return None
    path = urlsplit(text).path or "/"
    if not path.startswith("/") or len(path) > MAX_PAGE_LABEL_CHARS:
        return None
    decoded = unquote(path)
    if "@" in decoded or _LONG_DIGITS_RE.search(decoded) or _UUID_RE.search(decoded):
        return None
    if any(_TOKEN_RE.fullmatch(segment) for segment in decoded.split("/")):
        return None
    return path


# --- manual keyword rankings ---------------------------------------------------------

@dataclass(frozen=True)
class KeywordMovement:
    keyword: str
    previous: int
    current: int
    change: int  # positions gained (positive) or lost (negative)


@dataclass(frozen=True)
class RankedKeyword:
    keyword: str
    position: int


@dataclass(frozen=True)
class RankingSummary:
    total: int
    numeric_current: int
    above_10_current: int
    missing_current: int
    numeric_previous: int
    above_10_previous: int
    missing_previous: int
    at_1: int
    top_3: int
    page_1: int  # numeric current positions 1-10
    comparable: int  # keywords numeric in both Previous and Current
    improved: int
    declined: int
    unchanged: int
    movements: tuple[KeywordMovement, ...]  # comparable keywords, biggest gain first
    best_keywords: tuple[RankedKeyword, ...]  # numeric current positions, best first
    above_10_keywords: tuple[str, ...]  # current value is the label "Above 10"

    @property
    def has_current_data(self) -> bool:
        return self.numeric_current + self.above_10_current > 0

    @property
    def all_current_missing(self) -> bool:
        return self.total > 0 and self.missing_current == self.total


def summarize_rankings(keywords) -> RankingSummary:
    """Summarize manual rankings. Each item needs `.keyword`, `.previous` and `.current`."""
    keywords = list(keywords)
    numeric_current = above_current = missing_current = 0
    numeric_previous = above_previous = missing_previous = 0
    ranked, movements, above_keywords = [], [], []
    for item in keywords:
        previous, current = item.previous, item.current
        if previous.kind is RankingKind.POSITION:
            numeric_previous += 1
        elif previous.kind is RankingKind.ABOVE_10:
            above_previous += 1
        else:
            missing_previous += 1
        if current.kind is RankingKind.POSITION:
            numeric_current += 1
            ranked.append(RankedKeyword(item.keyword, current.position))
        elif current.kind is RankingKind.ABOVE_10:
            above_current += 1
            above_keywords.append(item.keyword)
        else:
            missing_current += 1
        if previous.is_numeric and current.is_numeric:
            movements.append(KeywordMovement(
                item.keyword, previous.position, current.position, previous.position - current.position))

    movements.sort(key=lambda m: (-m.change, m.keyword.casefold()))
    ranked.sort(key=lambda r: (r.position, r.keyword.casefold()))
    positions = [r.position for r in ranked]
    return RankingSummary(
        total=len(keywords),
        numeric_current=numeric_current,
        above_10_current=above_current,
        missing_current=missing_current,
        numeric_previous=numeric_previous,
        above_10_previous=above_previous,
        missing_previous=missing_previous,
        at_1=sum(1 for p in positions if p == 1),
        top_3=sum(1 for p in positions if p <= 3),
        page_1=sum(1 for p in positions if p <= 10),
        comparable=len(movements),
        improved=sum(1 for m in movements if m.change > 0),
        declined=sum(1 for m in movements if m.change < 0),
        unchanged=sum(1 for m in movements if m.change == 0),
        movements=tuple(movements),
        best_keywords=tuple(ranked),
        above_10_keywords=tuple(above_keywords),
    )


# --- shared ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityFlag:
    """A source warning carried through for reviewer notes."""

    source: str  # "gsc" or "ga4"
    period: str  # "current" or "previous"
    code: str
    message: str
    detail: str | None  # dimension (GSC) or report (GA4)


def _flags(source: str, period: str, warnings) -> tuple[QualityFlag, ...]:
    return tuple(
        QualityFlag(source, period, w.code, w.message, getattr(w, "dimension", None) or getattr(w, "report", None))
        for w in warnings
    )


def _truncated(warnings) -> frozenset[str]:
    return frozenset(
        getattr(w, "dimension", None) or getattr(w, "report", None) for w in warnings if w.code == "truncated"
    )


def _normalize(text) -> str:
    return _collapse(text).casefold()


# --- Search Console ---------------------------------------------------------------------

@dataclass(frozen=True)
class QueryItem:
    label: str | None  # None when the query text is not safe to print
    clicks: int
    impressions: int
    position: float | None


@dataclass(frozen=True)
class PageItem:
    label: str | None  # path only; None when not safe to print
    clicks: int
    impressions: int


@dataclass(frozen=True)
class CountryItem:
    name: str
    clicks: int
    impressions: int


@dataclass(frozen=True)
class TargetCountry:
    name: str
    found: bool | None  # None when the country list was truncated and the country is not in it
    clicks: int
    impressions: int
    impressions_share: float | None  # share of the period's total impressions
    clicks_share: float | None


@dataclass(frozen=True)
class GscComparison:
    clicks: Change
    impressions: Change
    ctr: Change
    position: Change  # Search Console average position, not a keyword ranking
    current_empty: bool
    previous_empty: bool
    country_count: Change
    query_count: Change
    page_count: Change
    target_country: TargetCountry | None
    other_countries_with_clicks: tuple[CountryItem, ...]
    top_pages: tuple[PageItem, ...]
    new_queries_count: int | None  # None when it cannot be determined reliably
    new_queries: tuple[QueryItem, ...] | None  # printable ones only, by impressions
    striking_queries_count: int | None
    striking_queries: tuple[QueryItem, ...] | None
    zero_click_queries_count: int | None
    zero_click_queries: tuple[QueryItem, ...] | None
    zero_click_pages_count: int | None
    zero_click_pages: tuple[PageItem, ...] | None
    truncated_current: frozenset[str]
    truncated_previous: frozenset[str]
    incomplete_current: bool
    incomplete_previous: bool
    diagnostics_current: dict
    diagnostics_previous: dict
    flags: tuple[QualityFlag, ...]


def _query_item(row) -> QueryItem:
    m = row.metrics
    return QueryItem(safe_query_label(row.key), m.clicks, m.impressions, m.position)


def _page_item(row) -> PageItem:
    return PageItem(safe_page_label(row.key), row.metrics.clicks, row.metrics.impressions)


def _printable(items, limit):
    return tuple([i for i in items if i.label is not None][:limit])


def _by_impressions(item):
    return (-item.impressions, -item.clicks, item.label or "")


def _gsc_comparison(gsc, target_country, th: Thresholds) -> GscComparison:
    cur, prev = gsc.current, gsc.previous
    cur_empty = cur.totals.impressions == 0 and cur.totals.clicks == 0
    prev_empty = prev.totals.impressions == 0 and prev.totals.clicks == 0
    trunc_cur, trunc_prev = _truncated(cur.warnings), _truncated(prev.warnings)

    def side(period, empty, name):
        return None if empty else getattr(period.totals, name)

    clicks = compare_count(side(cur, cur_empty, "clicks"), side(prev, prev_empty, "clicks"), th)
    impressions = compare_count(side(cur, cur_empty, "impressions"), side(prev, prev_empty, "impressions"), th)
    ctr = compare_rate(
        side(cur, cur_empty, "ctr"), side(prev, prev_empty, "ctr"),
        current_volume=side(cur, cur_empty, "impressions"), previous_volume=side(prev, prev_empty, "impressions"),
        min_volume=th.ctr_min_impressions, min_delta=th.ctr_min_delta,
    )
    position = compare_position(side(cur, cur_empty, "position"), side(prev, prev_empty, "position"), th)

    def count_change(dimension, rows_cur, rows_prev):
        if cur_empty or prev_empty or dimension in trunc_cur or dimension in trunc_prev:
            return _na()
        return compare_count(len(rows_cur), len(rows_prev), th)

    target = None
    if target_country and _normalize(target_country) and not cur_empty:
        row = next((r for r in cur.countries if _normalize(r.label) == _normalize(target_country)), None)
        total_i, total_c = cur.totals.impressions, cur.totals.clicks
        if row is not None:
            m = row.metrics
            target = TargetCountry(_collapse(target_country), True, m.clicks, m.impressions,
                                   m.impressions / total_i if total_i else None,
                                   m.clicks / total_c if total_c else None)
        else:
            target = TargetCountry(_collapse(target_country), None if "country" in trunc_cur else False, 0, 0, None, None)

    target_key = _normalize(target_country) if target_country else None
    others = () if cur_empty else tuple(
        CountryItem(r.label, r.metrics.clicks, r.metrics.impressions)
        for r in cur.countries if r.metrics.clicks > 0 and _normalize(r.label) != target_key
    )[:th.list_limit]

    top_pages = () if cur_empty else tuple(_page_item(r) for r in cur.pages)[:th.list_limit]

    # Query and page analyses need the complete list: a truncated list is cut by clicks,
    # so it can hide high-impression rows.
    queries_ok = not cur_empty and "query" not in trunc_cur
    pages_ok = not cur_empty and "page" not in trunc_cur
    new_count = new_items = striking_count = striking_items = None
    zero_q_count = zero_q_items = zero_p_count = zero_p_items = None
    if queries_ok:
        items = [_query_item(r) for r in cur.queries]
        striking = sorted((i for i in items if i.position is not None
                           and th.striking_min_position <= i.position <= th.striking_max_position
                           and i.impressions >= th.striking_min_impressions), key=_by_impressions)
        striking_count, striking_items = len(striking), _printable(striking, th.list_limit)
        zero = sorted((i for i in items if i.clicks == 0 and i.impressions >= th.zero_click_min_impressions),
                      key=_by_impressions)
        zero_q_count, zero_q_items = len(zero), _printable(zero, th.list_limit)
        if not prev_empty and "query" not in trunc_prev:
            previous_keys = {r.key for r in prev.queries}
            fresh = sorted((_query_item(r) for r in cur.queries if r.key not in previous_keys), key=_by_impressions)
            new_count, new_items = len(fresh), _printable(fresh, th.list_limit)
    if pages_ok:
        zero_pages = sorted((_page_item(r) for r in cur.pages
                             if r.metrics.clicks == 0 and r.metrics.impressions >= th.zero_click_min_impressions),
                            key=_by_impressions)
        zero_p_count, zero_p_items = len(zero_pages), _printable(zero_pages, th.list_limit)

    return GscComparison(
        clicks=clicks, impressions=impressions, ctr=ctr, position=position,
        current_empty=cur_empty, previous_empty=prev_empty,
        country_count=count_change("country", cur.countries, prev.countries),
        query_count=count_change("query", cur.queries, prev.queries),
        page_count=count_change("page", cur.pages, prev.pages),
        target_country=target, other_countries_with_clicks=others, top_pages=top_pages,
        new_queries_count=new_count, new_queries=new_items,
        striking_queries_count=striking_count, striking_queries=striking_items,
        zero_click_queries_count=zero_q_count, zero_click_queries=zero_q_items,
        zero_click_pages_count=zero_p_count, zero_click_pages=zero_p_items,
        truncated_current=trunc_cur, truncated_previous=trunc_prev,
        incomplete_current=any(w.code == "incomplete_data" for w in cur.warnings),
        incomplete_previous=any(w.code == "incomplete_data" for w in prev.warnings),
        diagnostics_current=dict(cur.diagnostics), diagnostics_previous=dict(prev.diagnostics),
        flags=_flags("gsc", "current", cur.warnings) + _flags("gsc", "previous", prev.warnings),
    )


# --- GA4 ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class LandingItem:
    label: str | None  # path only; None when not safe to print
    sessions: int
    share_of_organic: float | None


@dataclass(frozen=True)
class OrganicConcentration:
    top_label: str | None
    top_sessions: int
    organic_sessions: int
    share: float
    concentrated: bool
    distinct_pages: int  # landing pages other than "(not set)" / "(other)"
    placeholder_sessions: int


@dataclass(frozen=True)
class EngagementGap:
    """Organic Search engagement rate against the whole site, within the current period."""

    organic_rate: float | None
    site_rate: float | None
    difference: float | None
    comparable: bool
    insufficient_volume: bool
    organic_higher: bool | None  # None unless the difference is at least the threshold


@dataclass(frozen=True)
class KeyEventsComparison:
    status: str  # "recorded" | "none_recorded" | "unavailable"
    current: int | None
    previous: int | None


@dataclass(frozen=True)
class Ga4Comparison:
    sessions: Change
    total_users: Change
    engagement_rate: Change
    organic_sessions: Change
    organic_total_users: Change
    organic_engagement_rate: Change
    organic_share: Change  # Organic Search sessions as a share of all sessions
    engagement_gap: EngagementGap
    key_events: KeyEventsComparison
    top_organic_pages: tuple[LandingItem, ...]
    organic_concentration: OrganicConcentration | None
    current_empty: bool
    previous_empty: bool
    truncated_current: frozenset[str]
    truncated_previous: frozenset[str]
    incomplete_current: bool
    incomplete_previous: bool
    diagnostics_current: dict  # sum-minus-total differences; never required to be zero
    diagnostics_previous: dict
    flags: tuple[QualityFlag, ...]


def _share(part, whole):
    return part / whole if whole else None


def _ga4_comparison(ga4, th: Thresholds) -> Ga4Comparison:
    cur, prev = ga4.current, ga4.previous
    cur_empty, prev_empty = bool(cur.is_empty), bool(prev.is_empty)

    def side(period, empty, group, name):
        return None if empty else getattr(getattr(period, group), name)

    def count(group, name):
        return compare_count(side(cur, cur_empty, group, name), side(prev, prev_empty, group, name), th)

    def rate(group, name):
        return compare_rate(
            side(cur, cur_empty, group, name), side(prev, prev_empty, group, name),
            current_volume=side(cur, cur_empty, group, "sessions"),
            previous_volume=side(prev, prev_empty, group, "sessions"),
            min_volume=th.ga4_min_sessions, min_delta=th.engagement_min_delta,
        )

    share_cur = None if cur_empty else _share(cur.organic.sessions, cur.whole_site.sessions)
    share_prev = None if prev_empty else _share(prev.organic.sessions, prev.whole_site.sessions)
    organic_share = compare_rate(
        share_cur, share_prev,
        current_volume=side(cur, cur_empty, "whole_site", "sessions"),
        previous_volume=side(prev, prev_empty, "whole_site", "sessions"),
        min_volume=th.ga4_min_sessions, min_delta=th.engagement_min_delta,
    )

    gap = EngagementGap(None, None, None, False, False, None)
    if not cur_empty:
        o, s = cur.organic, cur.whole_site
        if o.engagement_rate is not None and s.engagement_rate is not None:
            if o.sessions < th.ga4_min_sessions or s.sessions < th.ga4_min_sessions:
                gap = EngagementGap(o.engagement_rate, s.engagement_rate, None, False, True, None)
            else:
                diff = o.engagement_rate - s.engagement_rate
                meaningful = round(abs(diff), 10) >= th.engagement_min_delta
                gap = EngagementGap(o.engagement_rate, s.engagement_rate, diff, True, False,
                                    (diff > 0) if meaningful else None)

    organic_sessions = 0 if cur_empty else cur.organic.sessions
    pages = [] if cur_empty else sorted(
        (p for p in cur.organic_landing_pages if not p.is_placeholder),
        key=lambda p: (-p.metrics.sessions, p.path),
    )
    top_pages = tuple(
        LandingItem(safe_page_label(p.path), p.metrics.sessions, _share(p.metrics.sessions, organic_sessions))
        for p in pages[:th.list_limit]
    )
    concentration = None
    if pages and organic_sessions > 0:
        top = pages[0]
        share = top.metrics.sessions / organic_sessions
        concentration = OrganicConcentration(
            top_label=safe_page_label(top.path), top_sessions=top.metrics.sessions,
            organic_sessions=organic_sessions, share=share,
            concentrated=organic_sessions >= th.concentration_min_sessions and share >= th.concentration_share,
            distinct_pages=len(pages),
            placeholder_sessions=sum(p.metrics.sessions for p in cur.organic_landing_pages if p.is_placeholder),
        )

    return Ga4Comparison(
        sessions=count("whole_site", "sessions"),
        total_users=count("whole_site", "total_users"),
        engagement_rate=rate("whole_site", "engagement_rate"),
        organic_sessions=count("organic", "sessions"),
        organic_total_users=count("organic", "total_users"),
        organic_engagement_rate=rate("organic", "engagement_rate"),
        organic_share=organic_share,
        engagement_gap=gap,
        key_events=KeyEventsComparison(ga4.key_events_status, cur.whole_site.key_events, prev.whole_site.key_events),
        top_organic_pages=top_pages,
        organic_concentration=concentration,
        current_empty=cur_empty, previous_empty=prev_empty,
        truncated_current=_truncated(cur.warnings), truncated_previous=_truncated(prev.warnings),
        incomplete_current=any(w.code == "incomplete_data" for w in cur.warnings),
        incomplete_previous=any(w.code == "incomplete_data" for w in prev.warnings),
        diagnostics_current=dict(cur.diagnostics), diagnostics_previous=dict(prev.diagnostics),
        flags=_flags("ga4", "current", cur.warnings) + _flags("ga4", "previous", prev.warnings),
    )


# --- assembly ----------------------------------------------------------------------------

@dataclass(frozen=True)
class ReportMetrics:
    thresholds: Thresholds
    rankings: RankingSummary  # manual keyword rankings
    gsc: GscComparison | None  # None when Search Console data is not available
    ga4: Ga4Comparison | None  # None when GA4 data is not available


def build_report_metrics(*, gsc=None, ga4=None, keywords=(), target_country=None,
                         thresholds: Thresholds = Thresholds()) -> ReportMetrics:
    """Assemble comparisons from fetched Search Console data, GA4 data and ranking rows.

    Any source may be omitted. `target_country` is the English country name from the
    client's configuration, matched against Search Console country names.
    """
    return ReportMetrics(
        thresholds=thresholds,
        rankings=summarize_rankings(keywords),
        gsc=_gsc_comparison(gsc, target_country, thresholds) if gsc is not None else None,
        ga4=_ga4_comparison(ga4, thresholds) if ga4 is not None else None,
    )
