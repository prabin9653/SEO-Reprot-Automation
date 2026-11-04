import dataclasses
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_engine as te
import test_history_baselines as tb
import test_history_eligibility as he
import test_snapshot as ts
from seo_report import metrics as mt
from seo_report.snapshots import baselines as bl
from seo_report.snapshots import comparison as cmp
from seo_report.snapshots import eligibility as el
from seo_report.sources import ga4 as ga
from seo_report.sources import gsc as gs

Direction = mt.Direction
SHIFTED_WEEKDAY = ("2026-08-30", "2026-09-26")  # a 28-day period starting on a Sunday
LONG_SPAN = ("2026-08-29", "2026-09-28")  # 31 days


def run(current, *candidates, config=bl.HistoryConfig()):
    """Compare a current payload with candidates given nearest first (ranks 1, 2, ...)."""
    ranked = [dataclasses.replace(c, rank=i) for i, c in enumerate(candidates, start=1)]
    return cmp.compare(current, bl.select_baselines(current, ranked, config), config)


def base(rank=1, span=tb.A, **kwargs):
    return tb.loaded(f"run-{rank}", span, rank, **kwargs)


def messages(result, **match):
    return [n.message for n in result.notes if all(getattr(n, k) == v for k, v in match.items())]


def gsc_with(**current):
    return te.live_like_gsc(current=current)


def ga4_with(**current):
    return te.live_like_ga4(current=current)


def gsc_daily(periods, clicks, missing=()):
    gsc = te.live_like_gsc()
    points = [gs.DailyPoint(day, gs.Metrics(clicks[i], 70, 0.0, 30.0), i not in missing)
              for i, day in enumerate(periods.current.dates())]
    return dataclasses.replace(gsc, current=dataclasses.replace(gsc.current, daily=points))


def ga4_daily(periods, sessions, missing=(), diagnostics=None):
    ga4 = te.live_like_ga4()
    points = [ga.DailySessions(day, sessions[i], 1, i not in missing) for i, day in enumerate(periods.current.dates())]
    return dataclasses.replace(ga4, current=dataclasses.replace(ga4.current, daily=points, diagnostics=diagnostics or {}))


# --- structure -----------------------------------------------------------------------------------------------------------

def test_the_comparison_reports_all_twelve_dimensions_with_their_limits():
    result = run(he.payload_for(), base())
    assert list(result.status) == list(el.DIMENSIONS) and len(result.status) == 12
    assert result.client_key == "example-co" and result.current_period == ("2026-09-29", "2026-10-26")
    assert result.limits == {"lookback_periods": 3, "max_gap_days": 35, "list_limit": 10}
    assert all(s.baseline is None or s.baseline.run_id == "run-1" for s in result.status.values())


def test_dimensions_without_a_baseline_explain_why():
    result = run(he.payload_for())
    status = result.status["gsc.totals"]
    assert status.eligible is False and status.baseline is None
    assert status.reasons == ("no eligible earlier report within the lookback",)
    assert result.gsc.totals.changes == {} and result.gsc.queries.matched == ()
    assert result.rankings.counts["improved"] is None
    assert "No earlier report is available as a baseline for this client." in messages(result)


def test_ineligible_current_data_blocks_only_that_dimension():
    gsc, _ = he.complete_data()
    truncated = dataclasses.replace(gsc, current=dataclasses.replace(
        gsc.current, warnings=[gs.DataWarning("truncated", "cut", "query")]))
    result = run(he.payload_for(gsc=truncated), base())
    assert result.status["gsc.queries"].eligible is False
    assert result.status["gsc.queries"].reasons[0].startswith("the current report's data is not eligible")
    assert result.status["gsc.totals"].eligible and result.gsc.totals.changes["clicks"].comparable
    assert result.gsc.queries.matched == () and result.gsc.queries.new is None


# --- Search Console totals ---------------------------------------------------------------------------------------------------

def test_search_console_totals_use_the_existing_comparators():
    baseline = gsc_with(totals=te.gm(4, 2275, 0.0018, 29.1))
    result = run(he.payload_for(), base(gsc=baseline))
    changes = result.gsc.totals.changes
    assert (changes["clicks"].direction, changes["clicks"].absolute, changes["clicks"].small_base) == (
        Direction.UP, 2, True)
    assert changes["impressions"].direction is Direction.DOWN
    assert changes["impressions"].percent == pytest.approx(-723 / 2275)
    assert changes["ctr"].direction is Direction.UP and changes["ctr"].favorable is True
    assert changes["position"].direction is Direction.FLAT and changes["position"].absolute == pytest.approx(0.5)
    assert result.gsc.totals.current["clicks"] == 6 and result.gsc.totals.baseline["impressions"] == 2275


def test_counts_are_not_compared_across_periods_of_different_length_but_rates_are():
    baseline = gsc_with(totals=te.gm(4, 2275, 0.0018, 29.1))
    result = run(he.payload_for(), base(span=LONG_SPAN, gsc=baseline))
    changes = result.gsc.totals.changes
    assert not changes["clicks"].comparable and not changes["impressions"].comparable
    assert changes["clicks"].current == 6 and changes["clicks"].previous == 4
    assert changes["ctr"].comparable and changes["position"].comparable
    assert any("different number of days" in m for m in messages(result, severity="warning"))


def test_the_ctr_comparison_needs_enough_impressions():
    thin = gsc_with(totals=te.gm(1, 300, 0.0033, 29.1))
    changes = run(he.payload_for(), base(gsc=thin)).gsc.totals.changes
    assert changes["ctr"].insufficient_volume and not changes["ctr"].comparable
    assert changes["position"].comparable


# --- GA4 totals ------------------------------------------------------------------------------------------------------------------

def test_ga4_site_and_organic_totals():
    baseline = ga4_with(whole=te.g4(64, 58, 35, 0.547, None), organic=te.g4(17, 13, 12, 0.706))
    result = run(he.payload_for(), base(ga4=baseline))
    site, organic = result.ga4.site.changes, result.ga4.organic.changes
    assert site["sessions"].percent == pytest.approx(26 / 64) and site["sessions"].direction is Direction.UP
    assert site["total_users"].absolute == 24
    assert site["engagement_rate"].direction is Direction.DOWN and site["engagement_rate"].favorable is False
    assert not site["key_events"].comparable and site["key_events"].previous is None  # NULL is not zero
    assert organic["sessions"].small_base and organic["sessions"].direction is Direction.UP
    assert organic["engagement_rate"].insufficient_volume  # 19 and 17 sessions are below the 20-session minimum
    assert organic["key_events"].direction is Direction.FLAT


def test_organic_share_needs_site_and_organic_from_the_same_baseline():
    same = run(he.payload_for(), base(ga4=ga4_with(whole=te.g4(64, 58, 35, 0.547), organic=te.g4(17, 13, 12, 0.706))))
    assert same.ga4.organic_share.current == pytest.approx(19 / 90)
    assert same.ga4.organic_share.previous == pytest.approx(17 / 64)
    assert same.ga4.organic_share.direction is Direction.DOWN
    _, truncated_ga4 = he.complete_data()
    channels_cut = dataclasses.replace(truncated_ga4, current=dataclasses.replace(
        truncated_ga4.current, warnings=[ga.Ga4Warning("truncated", "cut", "channels")]))
    split = run(he.payload_for(), base(1, tb.A, ga4=channels_cut), base(2, tb.B))
    assert split.status["ga4.site"].baseline.run_id == "run-1" and split.status["ga4.organic"].baseline.run_id == "run-2"
    assert split.ga4.organic_share is None


def test_target_country_share_needs_totals_and_countries_from_the_same_baseline():
    baseline = gsc_with(totals=te.gm(4, 2275, 0.0018, 29.1))
    result = run(he.payload_for(), base(gsc=baseline))
    assert result.gsc.target_country_share.current == pytest.approx(1479 / 1552)
    assert result.gsc.target_country_share.previous == pytest.approx(1479 / 2275)  # the baseline's own country list
    assert result.gsc.target_country_share.direction is Direction.UP
    gsc, _ = he.complete_data()
    countries_cut = dataclasses.replace(gsc, current=dataclasses.replace(
        gsc.current, warnings=[gs.DataWarning("truncated", "cut", "country")]))
    split = run(he.payload_for(), base(1, tb.A, gsc=countries_cut), base(2, tb.B))
    assert split.status["gsc.totals"].baseline.run_id == "run-1" and split.status["gsc.countries"].baseline.run_id == "run-2"
    assert split.gsc.target_country_share is None


# --- lists -----------------------------------------------------------------------------------------------------------------------------

def test_queries_are_matched_new_and_lost_with_counts():
    baseline = gsc_with(queries=[te.row("roof repair", 1, 200, 9.0), te.row("brand name", 1, 90, 1.1),
                                 te.row("old term", 3, 40, 5.0)])
    queries = run(he.payload_for(), base(gsc=baseline)).gsc.queries
    assert [i.label for i in queries.matched] == ["roof repair", "brand name"]
    assert [i.label for i in queries.new] == ["new roofer", "far away"] and [i.label for i in queries.lost] == ["old term"]
    assert queries.counts == {"current_total": 4, "baseline_total": 3, "matched": 2, "new": 2, "lost": 1, "ambiguous": 0}
    roof = queries.matched[0]
    assert roof.state == "matched" and roof.current["clicks"] == 2 and roof.baseline["clicks"] == 1
    assert roof.changes["clicks"].direction is Direction.UP and roof.changes["impressions"].direction is Direction.DOWN
    assert roof.changes["position"].favorable is True
    assert queries.membership_complete is True and "zz@example.test" not in repr(queries)


def test_new_and_lost_items_are_not_reported_when_a_list_was_capped():
    many = [te.row(f"query {i:04d}", i % 7, 1000 - i, 5.0) for i in range(520)]
    result = run(he.payload_for(), base(gsc=gsc_with(queries=many)))
    queries = result.gsc.queries
    assert queries.status.eligible and queries.membership_complete is False
    assert queries.new is None and queries.lost is None
    assert queries.counts["new"] is None and queries.counts["lost"] is None and queries.counts["matched"] == 0
    assert any(c.startswith("only the top rows") for c in queries.status.caveats)
    assert any("gsc.queries: only the top rows were stored" in m for m in messages(result, severity="info"))


def test_lists_return_the_top_ten_plus_total_counts():
    pages = [te.row(f"https://example.test/p{i:02d}", 20 - i, 100 + i, 5.0) for i in range(15)]
    queries = [te.row(f"query {i:02d}", 20 - i, 100 + i, 5.0) for i in range(15)]
    data = gsc_with(pages=pages, queries=queries)
    result = run(he.payload_for(gsc=data), base(gsc=data), config=bl.HistoryConfig(list_limit=10))
    listing = result.gsc.pages
    assert len(listing.matched) == 10 and listing.counts["matched"] == 15
    assert [i.key for i in listing.matched] == [f"/p{i:02d}" for i in range(10)]
    assert listing.new == () and listing.lost == () and listing.counts["new"] == 0
    assert len(result.gsc.queries.matched) == 10 and result.gsc.queries.counts["matched"] == 15
    assert len(run(he.payload_for(gsc=data), base(gsc=data), config=bl.HistoryConfig(list_limit=3)
                   ).gsc.pages.matched) == 3


def test_duplicate_keys_are_left_out_and_counted():
    baseline_pages = [te.row("https://example.test/a", 3, 30, 5.0), te.row("http://example.test/a", 2, 20, 5.0),
                      te.row("https://example.test/b", 1, 10, 5.0)]  # two URLs share the path /a
    current_pages = [te.row("https://example.test/a", 4, 40, 5.0), te.row("https://example.test/b", 2, 20, 5.0)]
    result = run(he.payload_for(gsc=gsc_with(pages=current_pages)), base(gsc=gsc_with(pages=baseline_pages)))
    listing = result.gsc.pages
    assert [i.key for i in listing.matched] == ["/b"] and listing.counts["ambiguous"] == 1
    assert listing.new == () and listing.lost == ()  # /a is neither matched, new nor lost: it is ambiguous


def test_countries_channels_and_landing_pages_are_matched_by_their_own_keys():
    _, ga4 = he.complete_data()
    baseline_ga4 = dataclasses.replace(ga4, current=dataclasses.replace(
        ga4.current, channels=[ga.ChannelRow("direct", te.g4(40, 38, 20, 0.5)), ga.ChannelRow("Referral", te.g4(3, 3, 1, 0.3))],
        landing_pages=[te.landing("/", 30), te.landing("(not set)", 2, True)]))
    result = run(he.payload_for(), base(ga4=baseline_ga4))
    assert [i.key for i in result.gsc.countries.matched] == ["usa", "pak", "mex", "can"][:len(result.gsc.countries.matched)]
    channels = result.ga4.channels
    assert [i.label for i in channels.matched] == ["Direct"] and [i.label for i in channels.new] == ["Organic Search"]
    assert [i.label for i in channels.lost] == ["Referral"]
    landing = result.ga4.landing_pages
    assert [i.key for i in landing.matched] == ["/"] and [i.key for i in landing.new] == ["/about"]
    assert [i.key for i in landing.lost] == ["(not set)"] and landing.lost[0].baseline["is_placeholder"] is True


def test_caveats_become_warnings_that_forbid_calling_results_complete_or_exact():
    _, ga4 = he.complete_data()
    warned = dataclasses.replace(ga4, current=dataclasses.replace(
        ga4.current, warnings=[ga.Ga4Warning("thresholded", "reported", "channels")]))
    result = run(he.payload_for(), base(ga4=warned))
    assert result.status["ga4.channels"].eligible and result.ga4.channels.status.caveats
    warnings = messages(result, severity="warning")
    assert any("ga4.channels:" in m and "Do not describe this comparison as complete or exact." in m for m in warnings)
    assert any("ga4.organic:" in m and "not complete or exact" in m for m in warnings)


# --- daily series -----------------------------------------------------------------------------------------------------------------------

def test_daily_series_are_aligned_by_index_and_only_joint_days_are_compared():
    current_clicks = [i % 3 for i in range(28)]
    baseline_clicks = [(i % 3) + 1 for i in range(28)]
    current_gsc = gsc_daily(te.PERIODS, current_clicks, missing={7})
    baseline_gsc = gsc_daily(tb.periods_for(tb.A), baseline_clicks, missing={2, 5})
    result = run(he.payload_for(gsc=current_gsc), base(gsc=baseline_gsc))
    daily = result.gsc.daily
    joint = [i for i in range(28) if i not in {2, 5, 7}]
    assert daily.aligned_by == "day_index" and daily.days_compared == 25
    assert (daily.days_with_data_current, daily.days_with_data_baseline) == (27, 26)
    assert [p.offset for p in daily.points] == joint
    assert daily.points[0].current_date == "2026-09-29" and daily.points[0].baseline_date == "2026-09-01"
    assert daily.points[1].values["clicks"] == (current_clicks[1], baseline_clicks[1])
    expected = mt.compare_count(sum(current_clicks[i] for i in joint), sum(baseline_clicks[i] for i in joint))
    assert daily.changes["clicks"] == expected and daily.changes["impressions"].current == 70 * 25
    assert daily.weekday_differs is False


def test_different_start_weekdays_are_noted():
    current = he.payload_for(gsc=gsc_daily(te.PERIODS, [1] * 28))
    shifted = base(span=SHIFTED_WEEKDAY, gsc=gsc_daily(tb.periods_for(SHIFTED_WEEKDAY), [2] * 28))
    result = run(current, shifted)
    assert result.gsc.daily.weekday_differs is True and result.gsc.daily.days_compared == 28
    assert any("gsc.daily: the two periods start on different weekdays" in m for m in messages(result))
    same = run(current, base(gsc=gsc_daily(tb.periods_for(tb.A), [2] * 28)))
    assert not any("different weekdays" in m for m in messages(same))


def test_the_ga4_daily_session_discrepancy_is_carried_through_without_reconciliation():
    sessions = [3 + (1 if i < 7 else 0) for i in range(28)]  # sums to 91 while the site total is 90
    current = he.payload_for(ga4=ga4_daily(te.PERIODS, sessions, diagnostics={"daily_sessions_diff": 1}))
    earlier = base(ga4=ga4_daily(tb.periods_for(tb.A), [3] * 28, diagnostics={"daily_sessions_diff": -2}))
    result = run(current, earlier)
    assert sum(sessions) == 91 and result.ga4.site.current["sessions"] == 90
    assert result.ga4.site.changes["sessions"].current == 90  # totals are compared, never daily sums
    assert result.diagnostics["current"]["ga4"]["current"] == {"daily_sessions_diff": 1}
    assert result.diagnostics["baselines"]["run-1"]["ga4"]["current"] == {"daily_sessions_diff": -2}
    diagnostic = messages(result, category="diagnostic")
    assert any("+1 in the current report" in m and "not reconciled" in m for m in diagnostic)
    assert any("-2 in baseline run run-1" in m for m in diagnostic)
    assert result.ga4.daily.changes["sessions"].current == sum(sessions)  # a daily comparison of daily rows only


# --- keyword rankings ---------------------------------------------------------------------------------------------------------------------

BASELINE_KEYWORDS = [
    te.kw("Concrete Patio", "", 3, "Wiley"), te.kw("Fence Repair", "", 5, "Cedar Hill"),
    te.kw("Siding", "", 4, "Glenn Heights"), te.kw("Gutter", "", 9, "Waxahachie"),
    te.kw("Paint", "", "Above 10", "Duncanville"), te.kw("Roof Repair", "", "Above 10", "Arlington"),
    te.kw("Roof Install", "", "", "Fort Worth"), te.kw("Old Keyword", "", 7, "Plano"),
    te.kw("Case Test", "", 2, "Dallas"),
]
CURRENT_KEYWORDS = [
    te.kw("concrete  PATIO", 3, 1, "wiley"), te.kw("Fence Repair", 5, 8, "Cedar Hill"),
    te.kw("Siding", 4, 4, "Glenn Heights"), te.kw("Gutter", 8, "Above 10", "Waxahachie"),
    te.kw("Paint", "", 6, "Duncanville"), te.kw("Roof Repair", "", "Above 10", "Arlington"),
    te.kw("Roof Install", "", 4, "Fort Worth"), te.kw("New Keyword", "", 3, "Frisco"),
    te.kw("Case Test", 2, "", "  dallas "), te.kw("Dup", "", 5, "X"), te.kw("Dup", "", 6, "X"),
]


@pytest.fixture(scope="module")
def ranking_result():
    return run(he.payload_for(keywords=CURRENT_KEYWORDS), base(keywords=BASELINE_KEYWORDS))


def states(result):
    items = (result.rankings.moved + result.rankings.not_comparable + result.rankings.new_keywords
             + result.rankings.no_longer_tracked)
    return {(i.keyword, i.target_location): i for i in items}


def test_numeric_rankings_move_only_when_both_values_are_numeric(ranking_result):
    moved = ranking_result.rankings.moved
    # stored keyword text has its whitespace normalized, so "concrete  PATIO" is kept as "concrete PATIO"
    assert [(m.keyword, m.state, m.movement) for m in moved] == [("Fence Repair", "declined", -3),
                                                                  ("concrete PATIO", "improved", 2)]
    assert moved[0].baseline == {"kind": "position", "position": 5, "label": None}
    assert moved[0].current == {"kind": "position", "position": 8, "label": None}


def test_above_10_and_missing_values_never_create_movement(ranking_result):
    items = {i.keyword: i for i in ranking_result.rankings.not_comparable}
    assert set(items) == {"Gutter", "Paint", "Roof Install", "Case Test", "Dup"}  # both duplicate rows share a name
    for name in ("Gutter", "Paint"):
        assert items[name].movement is None and "label, not a numeric position" in items[name].reason
    assert items["Roof Install"].movement is None and "not recorded in the baseline report" in items["Roof Install"].reason
    case = items["Case Test"]
    assert case.movement is None and case.current == {"kind": "missing", "position": None, "label": None}
    assert "not recorded in the current report" in case.reason
    assert items["Dup"].reason == "the keyword and location appear more than once"
    assert all(i.movement is None for i in ranking_result.rankings.not_comparable)


def test_unchanged_labels_and_unchanged_positions_are_not_movements(ranking_result):
    counts = ranking_result.rankings.counts
    assert counts["unchanged"] == 1 and counts["label_unchanged"] == 1
    assert counts["improved"] == 1 and counts["declined"] == 1


def test_new_and_no_longer_tracked_keywords_are_not_ranking_movements(ranking_result):
    new = ranking_result.rankings.new_keywords
    lost = ranking_result.rankings.no_longer_tracked
    assert [(k.keyword, k.state, k.movement, k.baseline) for k in new] == [("New Keyword", "new_keyword", None, None)]
    assert [(k.keyword, k.state, k.movement, k.current) for k in lost] == [("Old Keyword", "no_longer_tracked", None, None)]
    assert lost[0].baseline == {"kind": "position", "position": 7, "label": None}


def test_ranking_counts(ranking_result):
    assert ranking_result.rankings.counts == {
        "current_total": 11, "baseline_total": 9, "matched": 8, "improved": 1, "declined": 1, "unchanged": 1,
        "label_unchanged": 1, "not_comparable": 6, "new_keyword": 1, "no_longer_tracked": 1,
        "sheet_previous_mismatch": 1}


def test_keywords_match_on_normalized_text_and_location(ranking_result):
    concrete = next(m for m in ranking_result.rankings.moved if m.keyword.startswith("concrete"))
    assert concrete.baseline["position"] == 3 and concrete.current["position"] == 1
    assert next(i for i in ranking_result.rankings.not_comparable if i.keyword == "Case Test").baseline["position"] == 2


def test_the_sheets_previous_ranking_is_checked_against_the_baseline(ranking_result):
    moved = {m.keyword: m for m in ranking_result.rankings.moved}
    assert moved["Fence Repair"].sheet_previous_matches is True and moved["concrete PATIO"].sheet_previous_matches is True
    gutter = next(i for i in ranking_result.rankings.not_comparable if i.keyword == "Gutter")
    assert gutter.sheet_previous_matches is False  # the sheet says 8, the baseline's Current Ranking was 9
    assert next(i for i in ranking_result.rankings.not_comparable if i.keyword == "Case Test").sheet_previous_matches is True
    assert any("1 keyword(s)" in m and "Previous Ranking differs" in m for m in messages(ranking_result, severity="warning"))


def test_no_ranking_text_claims_a_top_10_entry_or_exit(ranking_result):
    text = " ".join(m for m in messages(ranking_result)).lower()
    text += " ".join(str(i.reason) for i in ranking_result.rankings.not_comparable).lower()
    for phrase in ("top 10", "entered", "left the", "dropped out", "fell out"):
        assert phrase not in text
    assert any("no movement was inferred" in m for m in messages(ranking_result))


def test_blank_baseline_rankings_fall_back_to_an_older_report():
    blank = [te.kw("a"), te.kw("b")]
    result = run(he.payload_for(keywords=[te.kw("a", "", 2, "X")]), base(1, tb.A, keywords=blank),
                 base(2, tb.B, keywords=[te.kw("a", "", 5, "X")]))
    assert result.status["rankings.keywords"].baseline.run_id == "run-2"
    assert result.rankings.moved[0].movement == 3


def test_moved_keywords_are_ordered_by_size_and_limited():
    current = [te.kw(f"k{i:02d}", "", 1, "L") for i in range(14)]
    baseline = [te.kw(f"k{i:02d}", "", 1 + i, "L") for i in range(14)]
    result = run(he.payload_for(keywords=current), base(keywords=baseline))
    assert [m.movement for m in result.rankings.moved] == [13, 12, 11, 10, 9, 8, 7, 6, 5, 4]
    assert result.rankings.counts["improved"] == 13 and result.rankings.counts["unchanged"] == 1


# --- determinism, privacy and structure ----------------------------------------------------------------------------------------------------

def test_results_are_deterministic():
    current = he.payload_for(keywords=CURRENT_KEYWORDS)
    candidates = (base(keywords=BASELINE_KEYWORDS), base(2, tb.B))
    assert run(current, *candidates) == run(current, *candidates)


def test_results_contain_no_secrets_contact_details_or_unsafe_values():
    result = run(he.payload_for(keywords=CURRENT_KEYWORDS), base(keywords=BASELINE_KEYWORDS))
    text = repr(result)
    for forbidden in ts.FORBIDDEN_STRINGS:
        assert forbidden not in text, forbidden
    assert ts._EMAIL_LIKE.findall(text) == [] and ts.phone_like(text) == []


def test_notes_are_review_notes_from_the_history_source():
    result = run(he.payload_for(keywords=CURRENT_KEYWORDS), base(keywords=BASELINE_KEYWORDS))
    assert result.notes and all(n.source == "history" and n.severity in {"info", "warning"} for n in result.notes)
    assert len(result.notes) == len({n for n in result.notes})


def test_comparison_modules_are_pure_and_not_integrated_with_metrics_or_the_engine():
    for name in ("comparison", "baselines", "eligibility"):
        source = (ROOT / "src" / "seo_report" / "snapshots" / f"{name}.py").read_text(encoding="utf-8")
        for forbidden in ("import sqlite3", "import time", "import uuid", "import random", "import os", "open(",
                          "print(", "googleapiclient", "build_report_metrics", "generate_insights", "import ReportMetrics",
                          "insights import engine", "insights.engine import", "seo_report.auth"):
            assert forbidden not in source, (name, forbidden)
    for existing in ("metrics.py", "insights/engine.py", "insights/rules.py", "insights/recommendations.py"):
        assert "snapshots" not in (ROOT / "src" / "seo_report" / existing).read_text(encoding="utf-8"), existing
