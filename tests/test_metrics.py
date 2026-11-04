import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import metrics as mt
from seo_report.metrics import Direction, Thresholds
from seo_report.periods import report_periods
from seo_report.rankings import parse_ranking
from seo_report.sources import ga4 as ga
from seo_report.sources import gsc as gs

TH = Thresholds()
D = dt.date
PERIODS = report_periods(D(2026, 9, 29), D(2026, 10, 26))


# --- compare_count --------------------------------------------------------------------

def test_count_small_base_uses_absolute_change_only():
    change = mt.compare_count(6, 4)
    assert change.small_base and change.percent is None
    assert change.absolute == 2 and change.direction is Direction.UP and change.favorable is True


def test_count_base_of_exactly_20_is_not_small():
    assert mt.compare_count(30, 20).small_base is False
    assert mt.compare_count(30, 19).small_base is True


def test_count_percent_change_and_direction():
    up = mt.compare_count(1552, 1000)
    assert up.percent == pytest.approx(0.552) and up.direction is Direction.UP and up.favorable is True
    down = mt.compare_count(1552, 2275)
    assert down.percent == pytest.approx(-0.3178, abs=1e-4)
    assert down.direction is Direction.DOWN and down.favorable is False


def test_count_change_below_ten_percent_is_flat():
    flat = mt.compare_count(105, 100)
    assert flat.direction is Direction.FLAT and flat.favorable is None
    assert mt.compare_count(110, 100).direction is Direction.UP
    assert mt.compare_count(90, 100).direction is Direction.DOWN


def test_count_zero_previous_never_divides():
    change = mt.compare_count(5, 0)
    assert change.small_base and change.percent is None and change.direction is Direction.UP
    assert mt.compare_count(0, 0).direction is Direction.FLAT


def test_count_none_is_not_comparable():
    for args in ((None, 5), (5, None), (None, None)):
        change = mt.compare_count(*args)
        assert not change.comparable and change.absolute is None and change.favorable is None


def test_change_evidence_exposes_numbers():
    evidence = mt.compare_count(6, 4).evidence()
    assert evidence == {"current": 6, "previous": 4, "absolute": 2, "percent": None, "direction": "up"}


# --- compare_rate ------------------------------------------------------------------------

def rate(current, previous, cv, pv, min_volume=500, min_delta=0.001):
    return mt.compare_rate(current, previous, current_volume=cv, previous_volume=pv,
                           min_volume=min_volume, min_delta=min_delta)


def test_ctr_improvement_with_enough_impressions():
    change = rate(0.0039, 0.0018, 1552, 2275)
    assert change.direction is Direction.UP and change.favorable is True
    assert change.absolute == pytest.approx(0.0021)


def test_ctr_needs_enough_impressions_in_both_periods():
    change = rate(0.0039, 0.0018, 1552, 499)
    assert not change.comparable and change.insufficient_volume
    assert not rate(0.0039, 0.0018, None, 1000).comparable


def test_ctr_difference_under_threshold_is_flat_and_boundary_counts():
    assert rate(0.0030, 0.0025, 1000, 1000).direction is Direction.FLAT
    assert rate(0.0030, 0.0020, 1000, 1000).direction is Direction.UP  # exactly 0.1 points
    assert rate(0.0010, 0.0030, 1000, 1000).direction is Direction.DOWN


def test_rate_with_missing_value_is_not_comparable_without_volume_flag():
    change = rate(None, 0.5, 100, 100)
    assert not change.comparable and not change.insufficient_volume


# --- compare_position ----------------------------------------------------------------------

def test_position_lower_is_better():
    better = mt.compare_position(24.0, 29.1)
    assert better.direction is Direction.DOWN and better.favorable is True
    worse = mt.compare_position(31.0, 29.1)
    assert worse.direction is Direction.UP and worse.favorable is False


def test_position_within_one_place_is_flat():
    change = mt.compare_position(29.6, 29.1)
    assert change.direction is Direction.FLAT and change.favorable is None
    assert change.absolute == pytest.approx(0.5)
    assert mt.compare_position(30.1, 29.1).direction is Direction.UP  # exactly one place


def test_position_none_is_not_comparable():
    assert not mt.compare_position(None, 29.1).comparable


# --- display-safe labels ---------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "roof repair mansfield", "  Concrete   Patio   Wiley ", "best roofer near 75001",
])
def test_safe_queries_pass_with_whitespace_collapsed(query):
    assert mt.safe_query_label(query) == " ".join(query.split())


@pytest.mark.parametrize("query", [
    None, "", "   ", "(not set)", "(other)", "someone@example.test roofing", "call 281 555 0100",
    "(281) 555-0100 roofer", "https://example.test/page", "www.example.test", "a" * 81,
    "tok_" + "a" * 30,
])
def test_unsafe_queries_are_not_printable(query):
    assert mt.safe_query_label(query) is None


@pytest.mark.parametrize("page, expected", [
    ("https://example.test/painting?utm_source=x&email=a@b.test#top", "/painting"),
    ("https://example.test/", "/"),
    ("https://example.test", "/"),
    ("/contact", "/contact"),
    ("/contact?ref=abc", "/contact"),
    ("/regular-roof-maintenance", "/regular-roof-maintenance"),
    ("/blog/2026-10-26-new-post", "/blog/2026-10-26-new-post"),
])
def test_safe_page_labels_keep_only_the_path(page, expected):
    assert mt.safe_page_label(page) == expected


@pytest.mark.parametrize("page", [
    None, "", "(not set)", "(other)", "/user/someone@example.test", "/user/someone%40example.test",
    "/account/1234567890123", "/r/" + "a" * 30, "/s/123e4567-e89b-12d3-a456-426614174000",
    "/" + "x" * 101, "relative-without-slash",
])
def test_unsafe_page_labels_are_not_printable(page):
    assert mt.safe_page_label(page) is None


# --- manual rankings -----------------------------------------------------------------------------

def kw(name, previous="", current=""):
    return SimpleNamespace(keyword=name, previous=parse_ranking(previous), current=parse_ranking(current))


def test_ranking_summary_counts_and_movements():
    summary = mt.summarize_rankings([
        kw("a", previous=5, current=1),
        kw("b", previous="Page 2, Rank 6", current=3),  # 16 -> 3
        kw("c", previous=4, current=8),
        kw("d", previous=7, current=7),
        kw("e", previous=9, current="Above 10"),
        kw("f", previous="Above 10", current=12),
        kw("g", previous="", current=2),
        kw("h", previous=3, current=""),
        kw("i", previous="Above 10", current="Above 10"),
    ])
    assert summary.total == 9
    assert (summary.numeric_current, summary.above_10_current, summary.missing_current) == (6, 2, 1)
    assert (summary.numeric_previous, summary.above_10_previous, summary.missing_previous) == (6, 2, 1)
    assert (summary.at_1, summary.top_3, summary.page_1) == (1, 3, 5)  # positions 1,3,8,7,2 on page 1; 12 is not
    assert summary.comparable == 4
    assert (summary.improved, summary.declined, summary.unchanged) == (2, 1, 1)
    assert [(m.keyword, m.change) for m in summary.movements] == [("b", 13), ("a", 4), ("d", 0), ("c", -4)]
    assert [(r.keyword, r.position) for r in summary.best_keywords][:3] == [("a", 1), ("g", 2), ("b", 3)]
    assert summary.above_10_keywords == ("e", "i")
    assert summary.has_current_data and not summary.all_current_missing


def test_above_10_and_missing_never_create_movement():
    summary = mt.summarize_rankings([kw("e", previous=9, current="Above 10"), kw("h", previous=3, current="")])
    assert summary.comparable == 0 and summary.movements == ()
    assert summary.improved == summary.declined == summary.unchanged == 0
    assert summary.top_3 == 0 and summary.page_1 == 0  # Above 10 is never counted as a position


def test_blank_rankings_are_missing_not_zero():
    summary = mt.summarize_rankings([kw("a"), kw("b")])
    assert summary.all_current_missing and not summary.has_current_data
    assert summary.numeric_current == 0 and summary.best_keywords == ()


def test_empty_keyword_list():
    summary = mt.summarize_rankings([])
    assert summary.total == 0 and not summary.all_current_missing and not summary.has_current_data


def test_movement_order_is_deterministic():
    first = mt.summarize_rankings([kw("b", 5, 3), kw("a", 5, 3), kw("c", 2, 4)])
    assert [m.keyword for m in first.movements] == ["a", "b", "c"]


# --- Search Console comparison ----------------------------------------------------------------------

def gm(clicks, impressions, ctr, position):
    return gs.Metrics(clicks, impressions, ctr, position)


def row(key, clicks, impressions, position=None, label=None):
    return gs.DimensionRow(key, label or key, gm(clicks, impressions, None, position))


def gsc_period(period, totals, *, countries=(), queries=(), pages=(), warnings=(), diagnostics=None):
    return gs.PeriodData(period, totals, countries=list(countries), queries=list(queries), pages=list(pages),
                         warnings=list(warnings), diagnostics=diagnostics or {})


def gsc_data(current, previous):
    return gs.SearchConsoleData("https://example.test/", "siteOwner", current, previous)


def live_like_gsc(**overrides):
    cur = dict(
        totals=gm(6, 1552, 0.0039, 29.6),
        countries=[row("usa", 3, 1479, label="United States"), row("pak", 1, 8, label="Pakistan"),
                   row("mex", 1, 5, label="Mexico"), row("can", 0, 9, label="Canada")],
        queries=[row("roof repair", 2, 100, 8.0), row("new roofer", 0, 80, 14.0), row("brand name", 1, 60, 1.2),
                 row("zz@example.test", 0, 90, 12.0), row("far away", 0, 30, 45.0)],
        pages=[row("https://example.test/", 5, 900), row("https://example.test/painting?x=1", 1, 300),
               row("https://example.test/old", 0, 120)],
    )
    prev = dict(
        totals=gm(4, 2275, 0.0018, 29.1),
        countries=[row("usa", 3, 2153, label="United States"), row("mar", 1, 1, label="Morocco")],
        queries=[row("roof repair", 1, 200, 9.0), row("brand name", 1, 90, 1.1)],
        pages=[row("https://example.test/", 4, 1500)],
    )
    cur.update(overrides.get("current", {}))
    prev.update(overrides.get("previous", {}))
    return gsc_data(gsc_period(PERIODS.current, **cur), gsc_period(PERIODS.previous, **prev))


def test_gsc_headline_comparisons():
    result = mt.build_report_metrics(gsc=live_like_gsc()).gsc
    assert result.clicks.small_base and result.clicks.absolute == 2 and result.clicks.favorable is True
    assert result.impressions.direction is Direction.DOWN and result.impressions.favorable is False
    assert result.ctr.direction is Direction.UP
    assert result.position.direction is Direction.FLAT  # 29.6 vs 29.1
    assert not result.current_empty and not result.previous_empty


def test_gsc_position_is_not_a_keyword_ranking():
    metrics = mt.build_report_metrics(gsc=live_like_gsc(), keywords=[kw("a", 5, 1)])
    assert metrics.gsc.position.current == 29.6
    assert metrics.rankings.at_1 == 1  # manual rankings are summarized independently


def test_gsc_target_country_share_uses_period_totals():
    result = mt.build_report_metrics(gsc=live_like_gsc(), target_country="united states").gsc
    target = result.target_country
    assert target.found and (target.clicks, target.impressions) == (3, 1479)
    assert target.impressions_share == pytest.approx(1479 / 1552)
    assert target.clicks_share == pytest.approx(0.5)
    assert [(c.name, c.clicks) for c in result.other_countries_with_clicks] == [("Pakistan", 1), ("Mexico", 1)]


def test_gsc_missing_target_country_and_no_target():
    missing = mt.build_report_metrics(gsc=live_like_gsc(), target_country="Narnia").gsc.target_country
    assert missing.found is False and missing.impressions_share is None
    assert mt.build_report_metrics(gsc=live_like_gsc()).gsc.target_country is None


def test_gsc_query_analyses_and_printable_labels():
    result = mt.build_report_metrics(gsc=live_like_gsc()).gsc
    # striking distance: position 4-20, at least 20 impressions; includes the unsafe query in the count only
    assert result.striking_queries_count == 3
    assert [q.label for q in result.striking_queries] == ["roof repair", "new roofer"]
    # zero clicks and at least 50 impressions: "new roofer" and the unsafe query ("far away" has only 30)
    assert result.zero_click_queries_count == 2
    assert [q.label for q in result.zero_click_queries] == ["new roofer"]
    assert result.new_queries_count == 3
    assert [q.label for q in result.new_queries] == ["new roofer", "far away"]
    assert result.zero_click_pages_count == 1
    assert [p.label for p in result.zero_click_pages] == ["/old"]
    assert [p.label for p in result.top_pages] == ["/", "/painting", "/old"]


def test_gsc_truncated_query_list_suppresses_dependent_claims_only():
    warning = gs.DataWarning("truncated", "cut", "query")
    result = mt.build_report_metrics(gsc=live_like_gsc(current={"warnings": [warning]})).gsc
    assert result.striking_queries is None and result.new_queries is None and result.zero_click_queries is None
    assert result.query_count.comparable is False
    assert result.zero_click_pages_count == 1  # unrelated page analysis still works
    assert result.clicks.comparable and result.country_count.comparable
    assert result.truncated_current == {"query"}
    assert any(f.code == "truncated" and f.detail == "query" and f.period == "current" for f in result.flags)


def test_gsc_truncated_previous_queries_only_blocks_new_query_detection():
    warning = gs.DataWarning("truncated", "cut", "query")
    result = mt.build_report_metrics(gsc=live_like_gsc(previous={"warnings": [warning]})).gsc
    assert result.new_queries is None and result.new_queries_count is None
    assert result.striking_queries_count == 3


def test_gsc_truncated_country_list_leaves_unlisted_target_undetermined():
    warning = gs.DataWarning("truncated", "cut", "country")
    result = mt.build_report_metrics(gsc=live_like_gsc(current={"warnings": [warning]}), target_country="Narnia").gsc
    assert result.target_country.found is None


def test_gsc_empty_current_period_suppresses_comparisons_and_lists():
    empty = gs.PeriodData(PERIODS.current, gs.EMPTY_METRICS,
                          warnings=[gs.DataWarning("empty_period", "none")])
    previous = gsc_period(PERIODS.previous, gm(4, 2275, 0.0018, 29.1), queries=[row("a", 1, 100, 5.0)])
    result = mt.build_report_metrics(gsc=gsc_data(empty, previous), target_country="United States").gsc
    assert result.current_empty and not result.previous_empty
    assert not result.clicks.comparable and not result.ctr.comparable and not result.position.comparable
    assert result.target_country is None and result.striking_queries is None and result.top_pages == ()


def test_gsc_empty_previous_period_blocks_comparisons_and_new_queries_but_not_current_lists():
    empty = gs.PeriodData(PERIODS.previous, gs.EMPTY_METRICS, warnings=[gs.DataWarning("empty_period", "none")])
    current = gsc_period(PERIODS.current, gm(6, 1552, 0.0039, 29.6), queries=[row("a", 0, 80, 9.0)])
    result = mt.build_report_metrics(gsc=gsc_data(current, empty)).gsc
    assert result.previous_empty and not result.clicks.comparable
    assert result.new_queries is None  # every query would otherwise look new
    assert result.striking_queries_count == 1


def test_gsc_ctr_comparison_needs_volume_and_missing_ctr_is_not_comparable():
    low = live_like_gsc(previous={"totals": gm(1, 300, 0.0033, 29.1)})
    assert mt.build_report_metrics(gsc=low).gsc.ctr.insufficient_volume
    none_ctr = live_like_gsc(current={"totals": gm(0, 0, None, None)})
    assert not mt.build_report_metrics(gsc=none_ctr).gsc.ctr.comparable


def test_gsc_flags_and_diagnostics_are_carried_through():
    incomplete = gs.DataWarning("incomplete_data", "late")
    result = mt.build_report_metrics(
        gsc=live_like_gsc(current={"warnings": [incomplete], "diagnostics": {"query_clicks_diff": -5}})).gsc
    assert result.incomplete_current and not result.incomplete_previous
    assert result.diagnostics_current == {"query_clicks_diff": -5}
    assert [(f.source, f.period, f.code) for f in result.flags] == [("gsc", "current", "incomplete_data")]


# --- GA4 comparison ----------------------------------------------------------------------------------

def g4(sessions, users, engaged, rate_, key_events=0):
    return ga.Ga4Metrics(sessions, users, engaged, rate_, key_events)


def landing(path, sessions, placeholder=False):
    return ga.LandingPage(path, g4(sessions, sessions, 0, None, None), placeholder)


def ga_period(period, whole, organic, *, pages=(), is_empty=False, warnings=(), diagnostics=None):
    return ga.Ga4PeriodData(period, whole, organic, is_empty=is_empty, organic_landing_pages=list(pages),
                            warnings=list(warnings), diagnostics=diagnostics or {})


def live_like_ga4(status="none_recorded", **overrides):
    cur = dict(whole=g4(90, 82, 28, 0.311), organic=g4(19, 13, 9, 0.474),
               pages=[landing("/", 17), landing("/painting", 1), landing("(not set)", 1, True)])
    prev = dict(whole=g4(64, 58, 35, 0.547), organic=g4(17, 13, 12, 0.706),
                pages=[landing("/", 14), landing("/contact", 1)])
    cur.update(overrides.get("current", {}))
    prev.update(overrides.get("previous", {}))
    return ga.Ga4Data("123456789", status, ga_period(PERIODS.current, **cur), ga_period(PERIODS.previous, **prev))


def test_ga4_headline_comparisons_with_small_base_and_volume_rules():
    result = mt.build_report_metrics(ga4=live_like_ga4()).ga4
    assert result.sessions.percent == pytest.approx(26 / 64) and result.sessions.direction is Direction.UP
    assert result.total_users.absolute == 24
    assert result.engagement_rate.direction is Direction.DOWN and result.engagement_rate.favorable is False
    assert result.organic_sessions.small_base and result.organic_sessions.percent is None  # previous 17 < 20
    assert result.organic_total_users.direction is Direction.FLAT
    # organic sessions (19 and 17) are below the 20-session minimum, so organic engagement is not compared
    assert not result.organic_engagement_rate.comparable and result.organic_engagement_rate.insufficient_volume


def test_ga4_organic_share_change():
    result = mt.build_report_metrics(ga4=live_like_ga4()).ga4
    assert result.organic_share.current == pytest.approx(19 / 90)
    assert result.organic_share.previous == pytest.approx(17 / 64)
    assert result.organic_share.direction is Direction.DOWN and result.organic_share.favorable is False


def test_ga4_engagement_gap_requires_enough_organic_sessions():
    low = mt.build_report_metrics(ga4=live_like_ga4()).ga4.engagement_gap
    assert low.insufficient_volume and not low.comparable and low.organic_higher is None
    enough = live_like_ga4(current={"organic": g4(25, 20, 12, 0.48)})
    gap = mt.build_report_metrics(ga4=enough).ga4.engagement_gap
    assert gap.comparable and gap.organic_higher is True
    assert gap.difference == pytest.approx(0.48 - 0.311)
    similar = live_like_ga4(current={"organic": g4(25, 20, 8, 0.33)})
    assert mt.build_report_metrics(ga4=similar).ga4.engagement_gap.organic_higher is None


def test_ga4_organic_concentration_ignores_placeholders():
    result = mt.build_report_metrics(ga4=live_like_ga4()).ga4
    concentration = result.organic_concentration
    assert concentration.top_label == "/" and concentration.top_sessions == 17
    assert concentration.share == pytest.approx(17 / 19) and concentration.concentrated
    assert concentration.distinct_pages == 2 and concentration.placeholder_sessions == 1
    assert [(p.label, p.sessions) for p in result.top_organic_pages] == [("/", 17), ("/painting", 1)]
    assert result.top_organic_pages[0].share_of_organic == pytest.approx(17 / 19)


def test_ga4_concentration_needs_enough_organic_sessions():
    tiny = live_like_ga4(current={"organic": g4(5, 4, 2, 0.4), "pages": [landing("/", 5)]})
    assert mt.build_report_metrics(ga4=tiny).ga4.organic_concentration.concentrated is False


def test_ga4_unsafe_landing_paths_lose_their_label_but_keep_the_numbers():
    pages = [landing("/r/" + "a" * 30, 9), landing("/ok", 3)]
    result = mt.build_report_metrics(ga4=live_like_ga4(current={"pages": pages})).ga4
    assert [p.label for p in result.top_organic_pages] == [None, "/ok"]
    assert result.organic_concentration.top_label is None and result.organic_concentration.top_sessions == 9


def test_ga4_key_events_pass_through_without_inventing_values():
    none = mt.build_report_metrics(ga4=live_like_ga4()).ga4.key_events
    assert (none.status, none.current, none.previous) == ("none_recorded", 0, 0)
    unavailable = live_like_ga4(
        status="unavailable",
        current={"whole": g4(90, 82, 28, 0.311, None)}, previous={"whole": g4(64, 58, 35, 0.547, None)})
    result = mt.build_report_metrics(ga4=unavailable).ga4.key_events
    assert (result.status, result.current, result.previous) == ("unavailable", None, None)


def test_ga4_empty_period_suppresses_comparisons():
    empty = ga.Ga4PeriodData(PERIODS.current, ga.EMPTY_METRICS, ga.EMPTY_METRICS, is_empty=True,
                             warnings=[ga.Ga4Warning("empty_period", "none")])
    previous = ga_period(PERIODS.previous, g4(64, 58, 35, 0.547), g4(17, 13, 12, 0.706), pages=[landing("/", 14)])
    result = mt.build_report_metrics(ga4=ga.Ga4Data("1", "unavailable", empty, previous)).ga4
    assert result.current_empty and not result.sessions.comparable and not result.organic_share.comparable
    assert result.organic_concentration is None and result.top_organic_pages == ()
    assert not result.engagement_gap.comparable


def test_ga4_diagnostic_discrepancy_is_preserved_not_reconciled():
    result = mt.build_report_metrics(
        ga4=live_like_ga4(current={"diagnostics": {"daily_sessions_diff": 1}})).ga4
    assert result.diagnostics_current == {"daily_sessions_diff": 1}
    assert result.sessions.current == 90  # the validated total is untouched


def test_ga4_flags_carry_report_names():
    warnings = [ga.Ga4Warning("truncated", "cut", "landing_pages"), ga.Ga4Warning("sampled", "s", "daily")]
    result = mt.build_report_metrics(ga4=live_like_ga4(current={"warnings": warnings})).ga4
    assert result.truncated_current == {"landing_pages"}
    assert {(f.code, f.detail) for f in result.flags} == {("truncated", "landing_pages"), ("sampled", "daily")}


# --- assembly and purity -----------------------------------------------------------------------------------

def test_sources_are_independent_and_optional():
    only_rankings = mt.build_report_metrics(keywords=[kw("a", 2, 1)])
    assert only_rankings.gsc is None and only_rankings.ga4 is None and only_rankings.rankings.at_1 == 1
    both = mt.build_report_metrics(gsc=live_like_gsc(), ga4=live_like_ga4())
    assert both.gsc is not None and both.ga4 is not None and both.rankings.total == 0


def test_custom_thresholds_are_applied():
    strict = Thresholds(small_base_count=5, meaningful_percent=0.5)
    assert mt.compare_count(6, 4, strict).small_base is True
    assert mt.compare_count(12, 10, strict).direction is Direction.FLAT
    assert mt.build_report_metrics(thresholds=strict).thresholds is strict


def test_metrics_module_is_pure_and_independent():
    source = (ROOT / "src" / "seo_report" / "metrics.py").read_text(encoding="utf-8")
    for forbidden in ("import datetime", "import time", "open(", "print(", "requests", "googleapiclient",
                      "seo_report.sources", "seo_report.clients", "seo_report.auth"):
        assert forbidden not in source
