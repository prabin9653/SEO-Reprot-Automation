"""Historical comparison of a current snapshot with the baselines chosen for each dimension.

Pure functions over snapshot payloads (format 1). The result is a separate set of objects: nothing here
touches ReportMetrics or the insight engine. Existing comparators in `metrics.py` are reused so
thresholds and directions mean the same thing everywhere.

Rules worth knowing:
  * rankings are compared numerically only when both values are numeric positions; "Above 10" and
    missing values never produce a numeric movement, a top-10 claim or a zero;
  * new keywords and no-longer-tracked keywords are reported as such, not as ranking movements;
  * "new" and "lost" items are reported only when both stored lists are complete;
  * daily series are aligned by day index and only days with data on both sides are compared;
  * totals are never rebuilt from daily rows, and the GA4 daily-session difference is passed through
    as a diagnostic note, not reconciled;
  * counts are not compared between periods of different lengths (rates and positions still are).
"""

import datetime as dt
from dataclasses import dataclass

from seo_report import metrics as mt
from seo_report.insights.rules import ReviewerNote
from seo_report.snapshots import eligibility as el
from seo_report.snapshots.baselines import BaselineRef, BaselineSelection, HistoryConfig, SkippedRun
from seo_report.snapshots.baselines import note as _note

GSC_FIELDS = ("clicks", "impressions", "ctr", "position")
GA4_FIELDS = ("sessions", "total_users", "engagement_rate", "key_events")
DAILY_FIELDS = {"gsc.daily": ("clicks", "impressions"), "ga4.daily": ("sessions", "organic_sessions")}


# --- result types ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DimensionStatus:
    dimension: str
    eligible: bool  # both the current report and a baseline can support this comparison
    reasons: tuple[str, ...]  # why not
    caveats: tuple[str, ...]  # reduced reliability (thresholding, sampling, capped lists)
    baseline: BaselineRef | None


@dataclass(frozen=True)
class TotalsComparison:
    status: DimensionStatus
    current: dict | None
    baseline: dict | None
    changes: dict  # metric -> metrics.Change


@dataclass(frozen=True)
class ItemComparison:
    key: str
    label: str
    state: str  # "matched", "new" or "lost"
    current: dict | None
    baseline: dict | None
    changes: dict  # empty unless matched


@dataclass(frozen=True)
class ListComparison:
    status: DimensionStatus
    matched: tuple[ItemComparison, ...]  # top items present in both reports
    new: tuple[ItemComparison, ...] | None  # None when membership is incomplete
    lost: tuple[ItemComparison, ...] | None
    counts: dict  # current_total, baseline_total, matched, new, lost, ambiguous (new/lost None if unknown)
    membership_complete: bool


@dataclass(frozen=True)
class DailyPoint:
    offset: int
    current_date: str
    baseline_date: str
    values: dict  # field -> (current, baseline)


@dataclass(frozen=True)
class DailyComparison:
    status: DimensionStatus
    aligned_by: str
    points: tuple[DailyPoint, ...]
    days_compared: int
    days_with_data_current: int
    days_with_data_baseline: int
    weekday_differs: bool
    changes: dict  # field -> Change between the sums over the jointly available days only


@dataclass(frozen=True)
class KeywordComparison:
    keyword: str
    target_location: str | None
    state: str  # improved, declined, unchanged, label_unchanged, not_comparable, new_keyword, no_longer_tracked
    current: dict | None  # {"kind", "position", "label"}
    baseline: dict | None
    movement: int | None  # positions gained (positive) or lost (negative); only for two numeric rankings
    reason: str | None
    sheet_previous_matches: bool | None  # the sheet's Previous Ranking against the baseline's Current Ranking


@dataclass(frozen=True)
class RankingComparison:
    status: DimensionStatus
    moved: tuple[KeywordComparison, ...]  # improved and declined, largest change first
    not_comparable: tuple[KeywordComparison, ...]
    new_keywords: tuple[KeywordComparison, ...]
    no_longer_tracked: tuple[KeywordComparison, ...]
    counts: dict


@dataclass(frozen=True)
class GscHistory:
    totals: TotalsComparison
    daily: DailyComparison
    countries: ListComparison
    queries: ListComparison
    pages: ListComparison
    target_country_share: mt.Change | None  # needs totals and countries from the same baseline


@dataclass(frozen=True)
class Ga4History:
    site: TotalsComparison
    organic: TotalsComparison
    channels: ListComparison
    daily: DailyComparison
    landing_pages: ListComparison
    organic_landing_pages: ListComparison
    organic_share: mt.Change | None  # needs site and organic totals from the same baseline


@dataclass(frozen=True)
class HistoricalComparison:
    client_key: str
    current_period: tuple[str, str]
    limits: dict
    status: dict  # dimension -> DimensionStatus, all 12 dimensions
    rankings: RankingComparison
    gsc: GscHistory
    ga4: Ga4History
    skipped: tuple[SkippedRun, ...]
    diagnostics: dict  # preserved as stored, never reconciled
    notes: tuple[ReviewerNote, ...]


# --- helpers ---------------------------------------------------------------------------------------------------

def _norm(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def _count(current, baseline, counts_ok: bool, th) -> mt.Change:
    if counts_ok:
        return mt.compare_count(current, baseline, th)
    return mt.Change(current, baseline, None, None, mt.Direction.NOT_COMPARABLE, None)


def _gsc_changes(cur: dict, base: dict, counts_ok: bool, th) -> dict:
    return {
        "clicks": _count(cur["clicks"], base["clicks"], counts_ok, th),
        "impressions": _count(cur["impressions"], base["impressions"], counts_ok, th),
        "ctr": mt.compare_rate(cur["ctr"], base["ctr"], current_volume=cur["impressions"],
                               previous_volume=base["impressions"], min_volume=th.ctr_min_impressions,
                               min_delta=th.ctr_min_delta),
        "position": mt.compare_position(cur["position"], base["position"], th),
    }


def _ga4_changes(cur: dict, base: dict, counts_ok: bool, th) -> dict:
    return {
        "sessions": _count(cur["sessions"], base["sessions"], counts_ok, th),
        "total_users": _count(cur["total_users"], base["total_users"], counts_ok, th),
        "engagement_rate": mt.compare_rate(cur["engagement_rate"], base["engagement_rate"],
                                           current_volume=cur["sessions"], previous_volume=base["sessions"],
                                           min_volume=th.ga4_min_sessions, min_delta=th.engagement_min_delta),
        "key_events": _count(cur["key_events"], base["key_events"], counts_ok, th),
    }


def _status(dimension, current_assessment, selection, refs) -> DimensionStatus:
    ref = refs[dimension]
    cur = current_assessment[dimension]
    reasons, caveats = [], list(cur.caveats)
    if not cur.eligible:
        reasons.append("the current report's data is not eligible: " + "; ".join(cur.reasons))
    elif ref is None:
        reasons.append("no eligible earlier report within the lookback")
        reasons += [f"run {run_id}: {why}" for run_id, why in selection.rejections.get(dimension, ())]
    else:
        caveats += list(selection.assessments[ref.run_id][dimension].caveats)
    eligible = not reasons
    return DimensionStatus(dimension, eligible, tuple(reasons), tuple(dict.fromkeys(caveats)),
                           ref if eligible else None)


def _membership(dimension, current_assessment, selection, status) -> bool:
    return bool(status.eligible and current_assessment[dimension].membership_complete
                and selection.assessments[status.baseline.run_id][dimension].membership_complete)


def _payload(selection, status):
    return selection.payloads[status.baseline.run_id] if status.eligible else None


# --- lists --------------------------------------------------------------------------------------------------------------

def _compare_list(status, current_rows, baseline_rows, key_fn, label_fn, changes_fn, sort_fn, membership, limit,
                  counts_ok, th) -> ListComparison:
    if not status.eligible:
        return ListComparison(status, (), None, None, {"current_total": None, "baseline_total": None,
                                                        "matched": None, "new": None, "lost": None,
                                                        "ambiguous": None}, False)

    def index(rows):
        seen, ambiguous = {}, set()
        for row in rows:
            key = key_fn(row)
            if key in seen:
                ambiguous.add(key)
            seen.setdefault(key, row)
        return seen, ambiguous

    cur, cur_amb = index(current_rows)
    base, base_amb = index(baseline_rows)
    ambiguous = cur_amb | base_amb
    matched = sorted((k for k in cur if k in base and k not in ambiguous), key=lambda k: sort_fn(cur[k], k))
    new = sorted((k for k in cur if k not in base and k not in ambiguous), key=lambda k: sort_fn(cur[k], k))
    lost = sorted((k for k in base if k not in cur and k not in ambiguous), key=lambda k: sort_fn(base[k], k))
    matched_items = tuple(ItemComparison(k, label_fn(cur[k]), "matched", cur[k], base[k],
                                         changes_fn(cur[k], base[k], counts_ok, th)) for k in matched[:limit])
    counts = {"current_total": len(current_rows), "baseline_total": len(baseline_rows), "matched": len(matched),
              "new": len(new) if membership else None, "lost": len(lost) if membership else None,
              "ambiguous": len(ambiguous)}
    new_items = tuple(ItemComparison(k, label_fn(cur[k]), "new", cur[k], None, {}) for k in new[:limit]) \
        if membership else None
    lost_items = tuple(ItemComparison(k, label_fn(base[k]), "lost", None, base[k], {}) for k in lost[:limit]) \
        if membership else None
    return ListComparison(status, matched_items, new_items, lost_items, counts, membership)


def _gsc_sort(row, key):
    return (-(row["clicks"] or 0), -(row["impressions"] or 0), key)


def _ga4_sort(row, key):
    return (-(row["sessions"] or 0), -(row["total_users"] or 0), key)


# --- daily ----------------------------------------------------------------------------------------------------------------

def _daily(status, current_rows, baseline_rows, dimension, counts_ok, th) -> DailyComparison:
    fields = DAILY_FIELDS[dimension]
    if not status.eligible:
        return DailyComparison(status, "day_index", (), 0, 0, 0, False, {})
    points, sums = [], {f: [0, 0] for f in fields}
    for i in range(min(len(current_rows), len(baseline_rows))):
        c, b = current_rows[i], baseline_rows[i]
        if not (c["has_data"] and b["has_data"]) or any(c[f] is None or b[f] is None for f in fields):
            continue
        points.append(DailyPoint(i, c["date"], b["date"], {f: (c[f], b[f]) for f in fields}))
        for f in fields:
            sums[f][0] += c[f]
            sums[f][1] += b[f]
    weekday_differs = bool(current_rows and baseline_rows
                           and dt.date.fromisoformat(current_rows[0]["date"]).weekday()
                           != dt.date.fromisoformat(baseline_rows[0]["date"]).weekday())
    changes = {f: _count(sums[f][0], sums[f][1], counts_ok, th) for f in fields} if points else {}
    return DailyComparison(status, "day_index", tuple(points), len(points),
                           sum(1 for r in current_rows if r["has_data"]),
                           sum(1 for r in baseline_rows if r["has_data"]), weekday_differs, changes)


# --- keyword rankings ---------------------------------------------------------------------------------------------------

def _ranking_view(ranking: dict) -> dict:
    return {"kind": ranking["kind"], "position": ranking["position"], "label": ranking["label"]}


def _sheet_previous_matches(previous: dict, baseline_current: dict) -> bool | None:
    if previous["kind"] == "missing" or baseline_current["kind"] == "missing":
        return None
    if previous["kind"] != baseline_current["kind"]:
        return False
    return previous["position"] == baseline_current["position"]


def _compare_rankings(status, current_rows, baseline_rows, limit) -> RankingComparison:
    empty_counts = {k: None for k in ("current_total", "baseline_total", "matched", "improved", "declined",
                                      "unchanged", "label_unchanged", "not_comparable", "new_keyword",
                                      "no_longer_tracked", "sheet_previous_mismatch")}
    if not status.eligible:
        return RankingComparison(status, (), (), (), (), empty_counts)

    def index(rows):
        seen, ambiguous = {}, set()
        for row in rows:
            key = (_norm(row["keyword"]), _norm(row["target_location"]))
            if key in seen:
                ambiguous.add(key)
            seen.setdefault(key, row)
        return seen, ambiguous

    base, base_amb = index(baseline_rows)
    cur, cur_amb = index(current_rows)
    results = []
    for row in current_rows:
        key = (_norm(row["keyword"]), _norm(row["target_location"]))
        cur_view = _ranking_view(row["current"])
        if key in cur_amb or key in base_amb:
            results.append(KeywordComparison(row["keyword"], row["target_location"], "not_comparable", cur_view,
                                             None, None, "the keyword and location appear more than once", None))
            continue
        if key not in base:
            results.append(KeywordComparison(row["keyword"], row["target_location"], "new_keyword", cur_view, None,
                                             None, "tracking began after the baseline report", None))
            continue
        base_row = base[key]
        base_view = _ranking_view(base_row["current"])
        matches = _sheet_previous_matches(row["previous"], base_row["current"])
        c, b = cur_view, base_view
        movement, state, reason = None, "not_comparable", None
        if c["kind"] == "position" and b["kind"] == "position":
            movement = b["position"] - c["position"]
            state = "improved" if movement > 0 else "declined" if movement < 0 else "unchanged"
        elif "missing" in (c["kind"], b["kind"]):
            reason = "a ranking was not recorded in " + ("the current report" if c["kind"] == "missing"
                                                         else "the baseline report")
        elif c["kind"] == "above_10" and b["kind"] == "above_10":
            state = "label_unchanged"
        else:
            reason = "'Above 10' is a label, not a numeric position"
        results.append(KeywordComparison(row["keyword"], row["target_location"], state, c, b, movement, reason,
                                         matches))
    current_keys = {(_norm(r["keyword"]), _norm(r["target_location"])) for r in current_rows}
    lost = [KeywordComparison(r["keyword"], r["target_location"], "no_longer_tracked", None,
                              _ranking_view(r["current"]), None, "no longer in the keyword list", None)
            for r in baseline_rows
            if (_norm(r["keyword"]), _norm(r["target_location"])) not in current_keys
            and (_norm(r["keyword"]), _norm(r["target_location"])) not in base_amb]

    def state_of(name):
        return [r for r in results if r.state == name]

    moved = sorted(state_of("improved") + state_of("declined"), key=lambda r: (-abs(r.movement), _norm(r.keyword)))
    counts = {"current_total": len(current_rows), "baseline_total": len(baseline_rows),
              "matched": sum(1 for r in results if r.state not in ("new_keyword",) and r.baseline is not None),
              "improved": len(state_of("improved")), "declined": len(state_of("declined")),
              "unchanged": len(state_of("unchanged")), "label_unchanged": len(state_of("label_unchanged")),
              "not_comparable": len(state_of("not_comparable")), "new_keyword": len(state_of("new_keyword")),
              "no_longer_tracked": len(lost),
              "sheet_previous_mismatch": sum(1 for r in results if r.sheet_previous_matches is False)}
    return RankingComparison(
        status, tuple(moved[:limit]), tuple(state_of("not_comparable")[:limit]),
        tuple(state_of("new_keyword")[:limit]), tuple(sorted(lost, key=lambda r: _norm(r.keyword))[:limit]), counts)


# --- the comparison ----------------------------------------------------------------------------------------------------------

def compare(current: dict, selection: BaselineSelection, config: HistoryConfig = HistoryConfig()) -> HistoricalComparison:
    """Compare the current payload with the baselines in `selection`, dimension by dimension."""
    th, limit = config.thresholds, config.list_limit
    cur_assessment = el.assess(current)
    refs = selection.baselines
    status = {d: _status(d, cur_assessment, selection, refs) for d in el.DIMENSIONS}
    notes = list(selection.notes)

    def counts_ok(dim):
        return bool(status[dim].eligible and status[dim].baseline.same_length)

    def base(dim):
        return _payload(selection, status[dim])

    def member(dim):
        return _membership(dim, cur_assessment, selection, status[dim])

    cur_gsc = current["gsc"]["periods"]["current"] if current["gsc"]["provided"] else None
    cur_ga4 = current["ga4"]["periods"]["current"] if current["ga4"]["provided"] else None

    def gsc_base(dim):
        payload = base(dim)
        return payload["gsc"]["periods"]["current"] if payload else None

    def ga4_base(dim):
        payload = base(dim)
        return payload["ga4"]["periods"]["current"] if payload else None

    # rankings
    rank_status = status[el.RANKINGS]
    rankings = _compare_rankings(rank_status, current["rankings"]["keywords"],
                                 base(el.RANKINGS)["rankings"]["keywords"] if rank_status.eligible else [], limit)
    if rankings.counts["sheet_previous_mismatch"]:
        notes.append(_note("warning", "data_quality",
                           f"For {rankings.counts['sheet_previous_mismatch']} keyword(s), the sheet's Previous Ranking "
                           "differs from the Current Ranking stored in the baseline report."))
    if rankings.counts["not_comparable"]:
        notes.append(_note("info", "data_quality", f"{rankings.counts['not_comparable']} keyword(s) could not be "
                           "compared (missing rankings, 'Above 10' labels or duplicate rows); no movement was inferred."))

    # Search Console
    if cur_gsc and status["gsc.totals"].eligible:
        cur_m, base_m = cur_gsc["totals"], gsc_base("gsc.totals")["totals"]
        g_totals = TotalsComparison(status["gsc.totals"], cur_m, base_m,
                                    _gsc_changes(cur_m, base_m, counts_ok("gsc.totals"), th))
    else:
        g_totals = TotalsComparison(status["gsc.totals"], None, None, {})
    g_daily = _daily(status["gsc.daily"], cur_gsc["daily"] if cur_gsc else [],
                     gsc_base("gsc.daily")["daily"] if status["gsc.daily"].eligible else [], "gsc.daily",
                     counts_ok("gsc.daily"), th)

    def gsc_list(dim, name, key_fn, label_fn):
        return _compare_list(status[dim], cur_gsc[name] if cur_gsc else [],
                             gsc_base(dim)[name] if status[dim].eligible else [], key_fn, label_fn, _changes_gsc_item,
                             _gsc_sort, member(dim), limit, counts_ok(dim), th)

    g_countries = gsc_list("gsc.countries", "countries", lambda r: r["code"], lambda r: r["name"])
    g_queries = gsc_list("gsc.queries", "queries", lambda r: _norm(r["label"]), lambda r: r["label"])
    g_pages = gsc_list("gsc.pages", "pages", lambda r: r["path"], lambda r: r["path"])

    target_share = None
    ref_t, ref_c = refs.get("gsc.totals"), refs.get("gsc.countries")
    if status["gsc.totals"].eligible and status["gsc.countries"].eligible and ref_t.run_id == ref_c.run_id:
        name = _norm(current["client"]["target_country"])
        find = lambda rows: next((r for r in rows if _norm(r["name"]) == name), None)
        cur_row, base_row = find(cur_gsc["countries"]), find(gsc_base("gsc.countries")["countries"])
        if name and cur_row and base_row and cur_gsc["totals"]["impressions"] and \
                gsc_base("gsc.totals")["totals"]["impressions"]:
            c_total, b_total = cur_gsc["totals"]["impressions"], gsc_base("gsc.totals")["totals"]["impressions"]
            target_share = mt.compare_rate(cur_row["impressions"] / c_total, base_row["impressions"] / b_total,
                                           current_volume=c_total, previous_volume=b_total,
                                           min_volume=th.ctr_min_impressions, min_delta=th.engagement_min_delta)
    gsc = GscHistory(g_totals, g_daily, g_countries, g_queries, g_pages, target_share)

    # GA4
    def ga4_totals(dim, scope):
        st = status[dim]
        if not (cur_ga4 and st.eligible):
            return TotalsComparison(st, None, None, {})
        cur_m, base_m = cur_ga4[scope], ga4_base(dim)[scope]
        return TotalsComparison(st, cur_m, base_m, _ga4_changes(cur_m, base_m, counts_ok(dim), th))

    def ga4_list(dim, name, key_fn, label_fn):
        return _compare_list(status[dim], cur_ga4[name] if cur_ga4 else [],
                             ga4_base(dim)[name] if status[dim].eligible else [], key_fn, label_fn,
                             _changes_ga4_item, _ga4_sort, member(dim), limit, counts_ok(dim), th)

    a_site, a_organic = ga4_totals("ga4.site", "site"), ga4_totals("ga4.organic", "organic")
    organic_share = None
    ref_s, ref_o = refs.get("ga4.site"), refs.get("ga4.organic")
    if a_site.status.eligible and a_organic.status.eligible and ref_s.run_id == ref_o.run_id:
        cs, co, bs, bo = a_site.current, a_organic.current, a_site.baseline, a_organic.baseline
        if cs["sessions"] and bs["sessions"] and co["sessions"] is not None and bo["sessions"] is not None:
            organic_share = mt.compare_rate(co["sessions"] / cs["sessions"], bo["sessions"] / bs["sessions"],
                                            current_volume=cs["sessions"], previous_volume=bs["sessions"],
                                            min_volume=th.ga4_min_sessions, min_delta=th.engagement_min_delta)
    ga4 = Ga4History(
        a_site, a_organic,
        ga4_list("ga4.channels", "channels", lambda r: _norm(r["channel"]), lambda r: r["channel"]),
        _daily(status["ga4.daily"], cur_ga4["daily"] if cur_ga4 else [],
               ga4_base("ga4.daily")["daily"] if status["ga4.daily"].eligible else [], "ga4.daily",
               counts_ok("ga4.daily"), th),
        ga4_list("ga4.landing_pages", "landing_pages", lambda r: r["path"], lambda r: r["path"]),
        ga4_list("ga4.organic_landing_pages", "organic_landing_pages", lambda r: r["path"], lambda r: r["path"]),
        organic_share)

    # notes: caveats, ineligibility, daily alignment, differences between the two runs
    for dimension in el.DIMENSIONS:
        st = status[dimension]
        for caveat in st.caveats:
            if caveat.startswith("only the top rows"):
                notes.append(_note("info", "data_quality", f"{dimension}: {caveat}."))
            else:
                notes.append(_note("warning", "data_quality", f"{dimension}: {caveat}. Do not describe this "
                                   "comparison as complete or exact."))
        if not st.eligible and refs[dimension] is None and cur_assessment[dimension].eligible is False:
            notes.append(_note("info", "suppressed", f"{dimension}: not compared because the current report's data "
                                                    "is not eligible."))
    for name, daily in (("gsc.daily", gsc.daily), ("ga4.daily", ga4.daily)):
        if daily.status.eligible and daily.weekday_differs:
            notes.append(_note("info", "data_quality", f"{name}: the two periods start on different weekdays, so "
                                                       "days were aligned by position only."))
    used = list(dict.fromkeys(r.run_id for r in refs.values() if r is not None))
    diagnostics = {"current": current["diagnostics"], "baselines": {rid: selection.payloads[rid]["diagnostics"]
                                                                    for rid in used}}
    for label, diag in [("the current report", current["diagnostics"])] + [
            (f"baseline run {rid}", selection.payloads[rid]["diagnostics"]) for rid in used]:
        diff = diag.get("ga4", {}).get("current", {}).get("daily_sessions_diff")
        if diff:
            notes.append(_note("info", "diagnostic", f"GA4 daily sessions differ from the total by {diff:+d} in "
                                                     f"{label}. This was preserved and not reconciled; totals were "
                                                     "compared, not daily sums."))
    cur_country = _norm(current["client"]["target_country"])
    for rid in used:
        other = selection.payloads[rid]
        if _norm(other["client"]["target_country"]) != cur_country:
            notes.append(_note("info", "data_quality", f"The target country differs in baseline run {rid}."))
        if current["ga4"]["provided"] and other["ga4"]["provided"] and \
                current["ga4"]["periods"]["current"]["time_zone"] != other["ga4"]["periods"]["current"]["time_zone"]:
            notes.append(_note("info", "data_quality", f"The GA4 reporting time zone differs in baseline run {rid}."))
    for skipped in selection.skipped:
        if skipped.reason.startswith("failed verification"):
            notes.append(_note("warning", "data_quality", f"Run {skipped.run_id} was not used: {skipped.reason}."))

    return HistoricalComparison(
        client_key=current["client"]["client_key"],
        current_period=(current["periods"]["current"]["start"], current["periods"]["current"]["end"]),
        limits={"lookback_periods": config.lookback_periods, "max_gap_days": config.max_gap_days,
                "list_limit": config.list_limit},
        status=status, rankings=rankings, gsc=gsc, ga4=ga4, skipped=selection.skipped, diagnostics=diagnostics,
        notes=tuple(dict.fromkeys(notes)))


def _changes_gsc_item(cur, base, counts_ok, th):
    return _gsc_changes(cur, base, counts_ok, th)


def _changes_ga4_item(cur, base, counts_ok, th):
    return _ga4_changes(cur, base, counts_ok, th)
