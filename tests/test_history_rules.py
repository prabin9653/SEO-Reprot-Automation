import copy
import dataclasses
import datetime as dt
import math
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_engine as te
import test_history_eligibility as he
import test_history_integration as ti
import test_record_schema as trs
import test_snapshot_store as tss
from seo_report import record_schema
from seo_report import report_run as rr
from seo_report.insights import engine
from seo_report.insights import history_rules as hr
from seo_report.insights import wording as w
from seo_report.insights.rules import Insight, ReviewerNote, Tone
from seo_report.snapshots import eligibility as el
from seo_report.snapshots import history
from seo_report.sources import ga4 as ga
from seo_report.sources import gsc as gs

CURRENT = he.CURRENT
P_A, P_B = ti.P_A, ti.P_B
BASE_DEFAULT = dict(clicks=4, impressions=2275, target=2153, sessions=64, organic=25)
CURRENT_DEFAULT = dict(clicks=6, impressions=1552, target=1479, sessions=90, organic=30, empty_previous=True)


def data(periods, *, clicks=6, impressions=1552, target=1479, sessions=90, organic=30, empty_previous=False,
         site="https://example.test/", gsc_warnings=(), ga4_warnings=(), diagnostics=None, time_zone=None,
         with_target=True, previous_totals=None):
    """Search Console and GA4 data with the given totals (the target country is "United States")."""
    gsc, ga4 = he.complete_data(periods)
    countries = [te.row("other", 0, impressions - target, label="Elsewhere")]
    if with_target:
        countries.insert(0, te.row("usa", 1, target, label="United States"))
    current = dataclasses.replace(gsc.current, totals=te.gm(clicks, impressions, clicks / impressions, 29.6),
                                  countries=countries, warnings=list(gsc_warnings))
    if empty_previous:
        previous = gs.PeriodData(periods.previous, gs.EMPTY_METRICS, warnings=[gs.DataWarning("empty_period", "none")])
    elif previous_totals:
        c, i = previous_totals
        previous = dataclasses.replace(gsc.previous, totals=te.gm(c, i, c / i, 29.0))
    else:
        previous = gsc.previous
    gsc = gs.SearchConsoleData(site, gsc.permission_level, current, previous)
    ga4_current = dataclasses.replace(
        ga4.current, whole_site=te.g4(sessions, sessions // 2, sessions // 3, 0.31),
        organic=te.g4(organic, organic // 2, organic // 3, 0.47), warnings=list(ga4_warnings),
        diagnostics=dict(diagnostics or {}), time_zone=time_zone or ga4.current.time_zone)
    if empty_previous:
        ga4_previous = ga.Ga4PeriodData(periods.previous, ga.EMPTY_METRICS, ga.EMPTY_METRICS, is_empty=True,
                                        warnings=[ga.Ga4Warning("empty_period", "none")])
    else:
        ga4_previous = ga4.previous
    return gsc, dataclasses.replace(ga4, current=ga4_current, previous=ga4_previous)


def write_baseline(store, periods, *, gsc_none=False, ga4_none=False, **kwargs):
    gsc, ga4 = data(periods, **kwargs)
    store.write(ti.make_snapshot(periods, gsc=None if gsc_none else gsc, ga4=None if ga4_none else ga4))


def scenario(tmp_path, *, baseline=BASE_DEFAULT, current=CURRENT_DEFAULT, older=None, keywords=None):
    """A real Stage 6b comparison (against a temporary history) plus the live metrics and base insights."""
    store = tss.make_store(tmp_path / "history" / "db.sqlite")
    try:
        if older is not None:
            write_baseline(store, P_B, **older)
        if baseline is not None:
            write_baseline(store, P_A, **baseline)
        gsc, ga4 = data(CURRENT, **current)
        keywords = te.mixed_keywords() if keywords is None else keywords
        payload = he.payload_for(CURRENT, gsc=gsc, ga4=ga4, keywords=keywords)
        comparison = history.compare_with_history(payload, store)
    finally:
        store.close()
    insights = engine.generate_insights(gsc=gsc, ga4=ga4, keywords=keywords, target_country="United States")
    return SimpleNamespace(comparison=comparison, insights=insights, metrics=insights.metrics, gsc=gsc, ga4=ga4)


def evaluate(s, **overrides):
    options = dict(comparison=s.comparison, metrics=s.metrics, base_insights=s.insights, crosscheck=None,
                   target_country="United States")
    options.update(overrides)
    return hr.evaluate_history_rules(**options)


def keys(result):
    return [c.key for c in result.client_candidates]


def finding(result, kind):
    return next((f for f in result.reviewer_findings if f.kind == kind), None)


def findings_of(result, kind):
    return [f for f in result.reviewer_findings if f.kind == kind]


# --- registries and structure --------------------------------------------------------------------------------------------

def test_registries_are_finite_and_documented():
    assert hr.RULES_VERSION == "1" and hr.STATEMENT_GROUP == "since_last_report"
    assert hr.MAX_STATEMENTS_PER_SECTION == 2 and hr.GEO_MIN_IMPRESSIONS == 500 and hr.GEO_MIN_SHIFT == 0.05
    for registry in (hr.EVIDENCE_KEYS, hr.BLOCK_REASONS, hr.VALIDATION_REASONS):
        assert registry and all(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) for name in registry)
    assert all(isinstance(meaning, str) and meaning for meaning, _ in hr.EVIDENCE_KEYS.values())
    assert all(isinstance(client_ok, bool) for _, client_ok in hr.EVIDENCE_KEYS.values())
    assert all(text for text in hr.BLOCK_REASONS.values()) and all(text for text in hr.VALIDATION_REASONS.values())
    assert set(hr.VALIDATION_REASONS) == {"invalid_evidence", "untraced_number", "unsafe_wording",
                                          "missing_baseline_period", "duplicate_of_base_insight",
                                          "inconsistent_comparison", "invalid_identity"}


def test_result_keeps_the_four_parts_separate(tmp_path):
    result = evaluate(scenario(tmp_path))
    assert [f.name for f in dataclasses.fields(hr.HistoryRuleResult)] == [
        "client_candidates", "reviewer_findings", "notes", "validation_failures", "rules_version"]
    assert all(isinstance(c, hr.ClientHistoryCandidate) and c.audience == "client" for c in result.client_candidates)
    assert all(isinstance(f, hr.ReviewerHistoryFinding) and f.audience == "reviewer" for f in result.reviewer_findings)
    assert all(isinstance(n, ReviewerNote) for n in result.notes)
    client_fields = {f.name for f in dataclasses.fields(hr.ClientHistoryCandidate)}
    assert not client_fields & {"blocked_reason", "baseline_run_id", "kind", "message"}  # no reviewer detail


# --- Rule A: GSC totals ------------------------------------------------------------------------------------------------

def test_an_empty_live_previous_period_with_eligible_history_gives_gsc_statements(tmp_path):
    s = scenario(tmp_path)
    assert s.metrics.gsc.previous_empty and not s.metrics.gsc.clicks.comparable
    result = evaluate(s)
    clicks, impressions = [c for c in result.client_candidates if c.section == "search_console"]
    assert (clicks.key, impressions.key) == ("history.gsc.clicks", "history.gsc.impressions")
    assert clicks.dimension == impressions.dimension == "gsc.totals"
    assert clicks.text == ("Organic clicks from Google Search were up from 4 in the report for "
                           f"{w.format_date_range(P_A.current.start, P_A.current.end)}.")
    assert impressions.text.startswith("Search impressions were down 31.8% from 2.28K in the report for ")
    assert clicks.evidence == {"current_value": 6, "baseline_value": 4, "direction": 1, "baseline_days": 28}
    assert impressions.evidence["direction"] == -1 and round(impressions.evidence["change_percent"], 3) == -0.318
    assert clicks.tone is Tone.POSITIVE and impressions.tone is Tone.NEUTRAL
    assert (clicks.priority, impressions.priority) == (10, 20)
    assert result.validation_failures == ()


def test_the_baseline_period_is_named_accurately(tmp_path):
    result = evaluate(scenario(tmp_path))
    for candidate in result.client_candidates:
        assert candidate.baseline_period_start == P_A.current.start.isoformat()
        assert candidate.baseline_period_end == P_A.current.end.isoformat()
        assert w.format_date_range(P_A.current.start, P_A.current.end) in candidate.text
    older = scenario(tmp_path / "older", baseline=None, older=BASE_DEFAULT)
    result = evaluate(older)
    assert {c.baseline_period_start for c in result.client_candidates} == {P_B.current.start.isoformat()}
    assert all(w.format_date_range(P_B.current.start, P_B.current.end) in c.text for c in result.client_candidates)


def test_a_non_empty_live_previous_period_keeps_the_history_reviewer_only(tmp_path):
    s = scenario(tmp_path, current={**CURRENT_DEFAULT, "empty_previous": False})
    assert not s.metrics.gsc.previous_empty and not s.metrics.ga4.previous_empty
    result = evaluate(s)
    assert result.client_candidates == ()
    blocked = {f.key: f for f in result.reviewer_findings if f.kind == "gsc_historical_change"}
    assert set(blocked) == {"history.review.gsc_clicks", "history.review.gsc_impressions"}
    assert {f.blocked_reason for f in blocked.values()} == {"live_previous_not_empty"}
    assert blocked["history.review.gsc_clicks"].evidence["baseline_value"] == 4
    assert "reviewer-only" in blocked["history.review.gsc_clicks"].message


@pytest.mark.parametrize("totals", [(1, 3), (0, 1), (2, 40), (19, 19)])
def test_a_small_non_zero_previous_value_is_not_an_empty_period(tmp_path, totals):
    s = scenario(tmp_path, current={**CURRENT_DEFAULT, "empty_previous": False, "previous_totals": totals})
    assert not s.metrics.gsc.previous_empty
    result = evaluate(s)
    assert not [c for c in result.client_candidates if c.section == "search_console"]
    assert {f.blocked_reason for f in findings_of(result, "gsc_historical_change")} == {"live_previous_not_empty"}


def test_an_ineligible_historical_dimension_cannot_produce_client_wording(tmp_path):
    s = scenario(tmp_path, baseline={**BASE_DEFAULT, "gsc_none": True})
    assert not s.comparison.status["gsc.totals"].eligible
    result = evaluate(s)
    assert not [c for c in result.client_candidates if c.section in ("search_console", "geography")]
    assert [c.key for c in result.client_candidates] == ["history.ga4.sessions", "history.ga4.organic_sessions"]
    assert not any(f.dimension == "gsc.totals" for f in findings_of(result, "gsc_historical_change"))
    assert finding(result, "dimensions_unusable").evidence == {"ineligible_dimensions": 5}


def test_an_invalidating_caveat_blocks_client_wording(tmp_path):
    s = scenario(tmp_path)
    totals = s.comparison.gsc.totals
    caveat = dataclasses.replace(totals.status, caveats=("GA4 sampled this data, so it is not exact",))
    comparison = dataclasses.replace(s.comparison, gsc=dataclasses.replace(
        s.comparison.gsc, totals=dataclasses.replace(totals, status=caveat)))
    result = evaluate(s, comparison=comparison)
    assert not [c for c in result.client_candidates if c.section == "search_console"]
    assert {f.blocked_reason for f in findings_of(result, "gsc_historical_change")} == {"caveat_present"}


def test_the_informational_top_rows_caveat_does_not_block_totals(tmp_path):
    s = scenario(tmp_path)
    totals = s.comparison.gsc.totals
    note = dataclasses.replace(totals.status, caveats=("only the top rows were stored, so new or lost items "
                                                       "cannot be reported",))
    comparison = dataclasses.replace(s.comparison, gsc=dataclasses.replace(
        s.comparison.gsc, totals=dataclasses.replace(totals, status=note)))
    assert "history.gsc.clicks" in keys(evaluate(s, comparison=comparison))


def test_a_baseline_for_another_property_blocks_client_wording(tmp_path):
    s = scenario(tmp_path, baseline={**BASE_DEFAULT, "site": "https://other.test/"})
    assert not s.comparison.status["gsc.totals"].eligible and s.comparison.status["ga4.site"].eligible
    result = evaluate(s)
    assert not [c for c in result.client_candidates if c.section in ("search_console", "geography")]


def test_an_incomplete_current_period_blocks_client_wording(tmp_path):
    s = scenario(tmp_path)
    live = dataclasses.replace(s.metrics.gsc, incomplete_current=True)
    metrics = dataclasses.replace(s.metrics, gsc=live)
    result = evaluate(s, metrics=metrics)
    assert not [c for c in result.client_candidates if c.section in ("search_console", "geography")]
    assert {f.blocked_reason for f in findings_of(result, "gsc_historical_change")} == {"current_period_incomplete"}


def test_a_baseline_of_a_different_length_is_not_compared(tmp_path):
    s = scenario(tmp_path)
    ref = dataclasses.replace(s.comparison.gsc.totals.status.baseline, same_length=False)
    totals = dataclasses.replace(s.comparison.gsc.totals, status=dataclasses.replace(s.comparison.gsc.totals.status,
                                                                                      baseline=ref))
    comparison = dataclasses.replace(s.comparison, gsc=dataclasses.replace(s.comparison.gsc, totals=totals))
    result = evaluate(s, comparison=comparison)
    assert not [c for c in result.client_candidates if c.section == "search_console"]
    assert not findings_of(result, "gsc_historical_change")


def test_live_data_that_is_missing_or_empty_gives_nothing(tmp_path):
    s = scenario(tmp_path)
    nothing = dataclasses.replace(s.metrics, gsc=None, ga4=None)
    result = evaluate(s, metrics=nothing)
    assert result.client_candidates == () and not findings_of(result, "gsc_historical_change")
    empty = dataclasses.replace(s.metrics, gsc=dataclasses.replace(s.metrics.gsc, current_empty=True))
    assert not [c for c in evaluate(s, metrics=empty).client_candidates if c.section in ("search_console", "geography")]


# --- Rule B: geography --------------------------------------------------------------------------------------------------

def geo(tmp_path, current_target, baseline_target, total=1000, current_total=None, baseline_total=None, **extra):
    current_total = current_total or total
    baseline_total = baseline_total or total
    baseline = {**BASE_DEFAULT, "impressions": baseline_total, "target": baseline_target, **extra.get("baseline", {})}
    current = {**CURRENT_DEFAULT, "empty_previous": False, "impressions": current_total, "target": current_target,
               **extra.get("current", {})}
    return scenario(tmp_path, baseline=baseline, current=current)


def geo_candidate(result):
    return next((c for c in result.client_candidates if c.section == "geography"), None)


def test_geography_with_matching_totals_and_countries_baselines(tmp_path):
    s = geo(tmp_path, 800, 700)
    assert s.comparison.status["gsc.totals"].baseline.run_id == s.comparison.status["gsc.countries"].baseline.run_id
    candidate = geo_candidate(evaluate(s))
    assert candidate.key == "history.geo.target_share" and candidate.dimension == "gsc.countries"
    assert candidate.text == ("United States accounted for 80% of search impressions, up from 70% in the report for "
                              f"{w.format_date_range(P_A.current.start, P_A.current.end)}, a change of 10 "
                              "percentage points.")
    assert candidate.evidence == {
        "share_current": 0.8, "share_baseline": 0.7, "share_change": pytest.approx(0.1), "direction": 1,
        "impressions_current": 1000, "impressions_baseline": 1000, "target_impressions_current": 800,
        "target_impressions_baseline": 700, "baseline_days": 28}
    assert candidate.baseline_period_start == P_A.current.start.isoformat() and candidate.tone is Tone.NEUTRAL


def test_a_falling_share_is_described_as_down(tmp_path):
    candidate = geo_candidate(evaluate(geo(tmp_path, 600, 750)))
    assert ", down from 75% in the report for " in candidate.text and "a change of 15 percentage points" in candidate.text
    assert candidate.evidence["direction"] == -1 and candidate.evidence["share_change"] < 0


def test_different_baselines_for_totals_and_countries_block_the_statement(tmp_path):
    truncated = [gs.DataWarning("truncated", "cut", "country")]
    s = scenario(tmp_path, baseline=dict(BASE_DEFAULT, impressions=1000, target=700, gsc_warnings=truncated),
                 older=dict(BASE_DEFAULT, impressions=1000, target=600),
                 current=dict(CURRENT_DEFAULT, empty_previous=False, impressions=1000, target=800))
    totals, countries = s.comparison.status["gsc.totals"], s.comparison.status["gsc.countries"]
    assert totals.baseline.run_id != countries.baseline.run_id
    result = evaluate(s)
    assert geo_candidate(result) is None
    assert finding(result, "geography_share_change").blocked_reason == "different_baselines"
    assert finding(result, "baselines_differ").evidence == {"baseline_runs": 2}


def test_499_impressions_block_the_statement_and_500_meets_the_minimum(tmp_path):
    assert geo_candidate(evaluate(geo(tmp_path / "a", 300, 200, current_total=500, baseline_total=500))) is not None
    below_now = evaluate(geo(tmp_path / "b", 300, 200, current_total=499, baseline_total=500))
    below_then = evaluate(geo(tmp_path / "c", 300, 200, current_total=500, baseline_total=499))
    for result in (below_now, below_then):
        assert geo_candidate(result) is None
        blocked = finding(result, "geography_share_change")
        assert blocked.blocked_reason == "impressions_below_minimum" and blocked.evidence["min_impressions"] == 500


def test_a_share_change_below_5_points_is_blocked(tmp_path):
    result = evaluate(geo(tmp_path, 549, 500))  # 4.9 points
    assert geo_candidate(result) is None
    blocked = finding(result, "geography_share_change")
    assert blocked.blocked_reason == "shift_below_minimum" and blocked.evidence["min_share_change"] == 0.05


@pytest.mark.parametrize("now,then", [(550, 500), (450, 400), (600, 650), (300, 250), (950, 900)])
def test_a_change_of_exactly_5_points_qualifies(tmp_path, now, then):
    candidate = geo_candidate(evaluate(geo(tmp_path, now, then)))
    assert candidate is not None and "a change of 5 percentage points" in candidate.text


def test_a_change_above_5_points_qualifies(tmp_path):
    candidate = geo_candidate(evaluate(geo(tmp_path, 561, 500)))
    assert "a change of 6.1 percentage points" in candidate.text


def test_missing_target_country_data_blocks_the_statement(tmp_path):
    no_baseline_row = evaluate(geo(tmp_path / "a", 800, 700, baseline={"with_target": False}))
    no_current_row = evaluate(geo(tmp_path / "b", 800, 700, current={"with_target": False}))
    for result in (no_baseline_row, no_current_row):
        assert geo_candidate(result) is None and finding(result, "geography_share_change") is None
    assert geo_candidate(evaluate(geo(tmp_path / "c", 800, 700), target_country=None)) is None
    assert geo_candidate(evaluate(geo(tmp_path / "d", 800, 700), target_country="   ")) is None


def test_no_division_by_zero_when_a_total_is_zero(tmp_path):
    s = geo(tmp_path, 800, 700)
    totals = s.comparison.gsc.totals
    for current, baseline in (({**totals.current, "impressions": 0}, totals.baseline),
                              (totals.current, {**totals.baseline, "impressions": 0}),
                              (None, totals.baseline), (totals.current, None)):
        broken = dataclasses.replace(totals, current=current, baseline=baseline)
        comparison = dataclasses.replace(s.comparison, gsc=dataclasses.replace(s.comparison.gsc, totals=broken))
        assert geo_candidate(evaluate(s, comparison=comparison)) is None
    no_share = dataclasses.replace(s.comparison, gsc=dataclasses.replace(s.comparison.gsc, target_country_share=None))
    assert geo_candidate(evaluate(s, comparison=no_share)) is None


def test_the_share_comes_from_the_comparison_not_from_a_country_total(tmp_path):
    s = geo(tmp_path, 800, 700, current_total=1000, baseline_total=2000)  # 80% now, 700 of 2,000 = 35% before
    candidate = geo_candidate(evaluate(s))
    assert candidate.evidence["share_baseline"] == 0.35 and candidate.evidence["impressions_baseline"] == 2000
    assert candidate.evidence["target_impressions_baseline"] == 700 and "35%" in candidate.text
    s = geo(tmp_path / "x", 800, 1100, current_total=1000, baseline_total=2000)  # 80% now, 55% before
    candidate = geo_candidate(evaluate(s))
    assert candidate.evidence["share_baseline"] == 0.55 and "a change of 25 percentage points" in candidate.text


def test_a_caveat_or_an_incomplete_period_blocks_geography(tmp_path):
    s = geo(tmp_path, 800, 700)
    countries = s.comparison.gsc.countries
    caveated = dataclasses.replace(countries, status=dataclasses.replace(countries.status, caveats=("sampled",)))
    comparison = dataclasses.replace(s.comparison, gsc=dataclasses.replace(s.comparison.gsc, countries=caveated))
    result = evaluate(s, comparison=comparison)
    assert geo_candidate(result) is None and finding(result, "geography_share_change").blocked_reason == "caveat_present"
    metrics = dataclasses.replace(s.metrics, gsc=dataclasses.replace(s.metrics.gsc, incomplete_current=True))
    assert geo_candidate(evaluate(s, metrics=metrics)) is None


def test_geography_does_not_depend_on_an_empty_live_previous_period(tmp_path):
    empty = geo(tmp_path / "e", 800, 700, current={"empty_previous": True})
    full = geo(tmp_path / "f", 800, 700)
    assert geo_candidate(evaluate(empty)) is not None and geo_candidate(evaluate(full)) is not None


def test_ineligible_geography_dimensions_give_nothing(tmp_path):
    s = scenario(tmp_path, baseline=dict(BASE_DEFAULT, gsc_none=True))
    assert geo_candidate(evaluate(s)) is None and finding(evaluate(s), "geography_share_change") is None


# --- Rule E: GA4 ---------------------------------------------------------------------------------------------------------

def test_an_empty_live_previous_period_with_eligible_ga4_history(tmp_path):
    result = evaluate(scenario(tmp_path))
    sessions, organic = [c for c in result.client_candidates if c.section == "ga4"]
    assert (sessions.key, organic.key) == ("history.ga4.sessions", "history.ga4.organic_sessions")
    assert sessions.text.startswith("Website sessions were up 40.6% from 64 in the report for ")
    assert organic.text.startswith("Organic Search sessions were up 20% from 25 in the report for ")
    assert (sessions.dimension, organic.dimension) == ("ga4.site", "ga4.organic")
    assert sessions.evidence["current_value"] == 90 and sessions.evidence["baseline_value"] == 64


def test_a_non_empty_live_previous_ga4_period_keeps_history_reviewer_only(tmp_path):
    s = scenario(tmp_path, current=dict(CURRENT_DEFAULT, empty_previous=False))
    result = evaluate(s)
    assert not [c for c in result.client_candidates if c.section == "ga4"]
    reasons = {f.key: f.blocked_reason for f in findings_of(result, "ga4_historical_change")}
    assert reasons == {"history.review.ga4_sessions": "live_previous_not_empty",
                       "history.review.ga4_organic_sessions": "live_previous_not_empty"}


def test_an_insufficient_ga4_sample_blocks_client_wording(tmp_path):
    thin_baseline = evaluate(scenario(tmp_path / "a", baseline=dict(BASE_DEFAULT, sessions=19, organic=19)))
    assert not [c for c in thin_baseline.client_candidates if c.section == "ga4"]
    assert {f.blocked_reason for f in findings_of(thin_baseline, "ga4_historical_change")} == {"insufficient_sample"}
    thin_current = evaluate(scenario(tmp_path / "b", current=dict(CURRENT_DEFAULT, sessions=19, organic=30)))
    assert [c.key for c in thin_current.client_candidates if c.section == "ga4"] == ["history.ga4.organic_sessions"]
    at_minimum = evaluate(scenario(tmp_path / "c", baseline=dict(BASE_DEFAULT, sessions=20, organic=20)))
    assert len([c for c in at_minimum.client_candidates if c.section == "ga4"]) == 2


@pytest.mark.parametrize("code", ["thresholded", "sampled", "other_row"])
def test_ga4_thresholding_sampling_and_other_row_caveats_are_respected(tmp_path, code):
    warning = ga.Ga4Warning(code, "limitation", "totals")
    s = scenario(tmp_path, baseline=dict(BASE_DEFAULT, ga4_warnings=[warning]))
    assert s.comparison.status["ga4.site"].caveats
    result = evaluate(s)
    assert "history.ga4.sessions" not in keys(result)
    blocked = next(f for f in findings_of(result, "ga4_historical_change") if f.dimension == "ga4.site")
    assert blocked.blocked_reason == "caveat_present"
    assert finding(result, "incomplete_comparison_data").evidence == {"caveated_dimensions": 1}


def test_a_different_ga4_time_zone_blocks_ga4_client_wording(tmp_path):
    s = scenario(tmp_path, current=dict(CURRENT_DEFAULT, time_zone="UTC"), baseline=dict(BASE_DEFAULT, time_zone="Asia/Tokyo"))
    result = evaluate(s)
    assert not [c for c in result.client_candidates if c.section == "ga4"]
    assert {f.blocked_reason for f in findings_of(result, "ga4_historical_change")} == {"time_zone_differs"}


def test_the_ga4_daily_discrepancy_is_reviewer_only(tmp_path):
    s = scenario(tmp_path, current=dict(CURRENT_DEFAULT, diagnostics={"daily_sessions_diff": -2}),
                 baseline=dict(BASE_DEFAULT, diagnostics={"daily_sessions_diff": 1}))
    result = evaluate(s)
    found = {f.key: f for f in findings_of(result, "ga4_daily_discrepancy")}
    assert set(found) == {"history.review.ga4_daily_discrepancy.current", "history.review.ga4_daily_discrepancy.baseline_1"}
    assert found["history.review.ga4_daily_discrepancy.current"].evidence == {"daily_sessions_diff": -2}
    assert found["history.review.ga4_daily_discrepancy.baseline_1"].evidence == {"daily_sessions_diff": 1}
    assert found["history.review.ga4_daily_discrepancy.baseline_1"].baseline_run_id
    assert all(f.section == "ga4" and f.tone is Tone.NEUTRAL for f in found.values())
    assert all("daily_sessions_diff" not in c.evidence and "daily" not in c.text for c in result.client_candidates)
    assert hr.EVIDENCE_KEYS["daily_sessions_diff"][1] is False
    assert hr.validate_evidence({"daily_sessions_diff": 1}, audience="client")


def test_no_discrepancy_means_no_finding(tmp_path):
    assert not findings_of(evaluate(scenario(tmp_path)), "ga4_daily_discrepancy")


# --- Rules C and D: rankings ---------------------------------------------------------------------------------------------

def pending(movement, keyword="k"):
    return rr.PendingFallback(keyword, "Dallas", 3, 3 + movement, movement, "run", 1, "2026-09-01", "2026-09-28")


def crosscheck(**kwargs):
    return rr.RankingCrosscheck(**kwargs)


def test_pending_fallbacks_are_reviewer_findings_only(tmp_path):
    s = scenario(tmp_path)
    check = crosscheck(pending_fallbacks=(pending(2), pending(-1), pending(0), pending(0)))
    result = evaluate(s, crosscheck=check)
    found = finding(result, "ranking_pending_fallback")
    assert found.section == "keywords" and found.dimension == "rankings.keywords"
    assert found.evidence == {"pending_fallbacks": 4, "fallback_improved": 1, "fallback_declined": 1,
                              "fallback_unchanged": 2}
    assert "4 keywords have a blank Previous Ranking" in found.message
    assert "no marker or footnote was created" in found.message
    assert not [c for c in result.client_candidates if c.section == "keywords"]
    assert all("Previous Ranking" not in c.text and "keyword" not in c.text.lower() for c in result.client_candidates)


def test_zero_movement_is_never_described_as_improvement_or_decline(tmp_path):
    result = evaluate(scenario(tmp_path), crosscheck=crosscheck(pending_fallbacks=(pending(0),)))
    found = finding(result, "ranking_pending_fallback")
    assert found.evidence["fallback_unchanged"] == 1
    assert found.evidence["fallback_improved"] == 0 == found.evidence["fallback_declined"]
    lowered = found.message.lower()
    assert not any(word in lowered for word in ("improv", "declin", "rose", "fell", "moved up", "moved down", "gain"))


def test_new_lost_mismatched_and_not_comparable_keywords_are_reviewer_only(tmp_path):
    check = crosscheck(sheet_previous_mismatch=2, new_keywords=3, no_longer_tracked=4, not_comparable=5)
    result = evaluate(scenario(tmp_path), crosscheck=check)
    evidence = {f.kind: f.evidence for f in result.reviewer_findings if f.kind.startswith("ranking_")}
    assert evidence == {"ranking_sheet_mismatch": {"sheet_mismatches": 2}, "ranking_new_keywords": {"new_keywords": 3},
                        "ranking_lost_keywords": {"lost_keywords": 4},
                        "ranking_not_comparable": {"not_comparable_keywords": 5}}
    assert all(f.section == "keywords" for f in result.reviewer_findings if f.kind.startswith("ranking_"))
    assert not [c for c in result.client_candidates if c.section == "keywords"]
    messages = " ".join(f.message for f in result.reviewer_findings if f.kind.startswith("ranking_"))
    assert "no movement was inferred" in messages


def test_zero_and_missing_counts_produce_no_ranking_findings(tmp_path):
    for check in (None, crosscheck(), crosscheck(sheet_previous_mismatch=0, new_keywords=0, no_longer_tracked=0,
                                                 not_comparable=0)):
        result = evaluate(scenario(tmp_path), crosscheck=check)
        assert not [f for f in result.reviewer_findings if f.kind.startswith("ranking_")]


def test_ranking_findings_carry_counts_only_never_a_movement(tmp_path):
    check = crosscheck(pending_fallbacks=(pending(2),), sheet_previous_mismatch=1, new_keywords=1,
                       no_longer_tracked=1, not_comparable=3)
    result = evaluate(scenario(tmp_path), crosscheck=check)
    ranking = [f for f in result.reviewer_findings if f.kind.startswith("ranking_")]
    assert ranking
    assert not any(name in f.evidence for f in ranking for name in ("movement", "position", "current_position"))
    assert not any(name in hr.EVIDENCE_KEYS for name in ("movement", "position", "current_position"))

def test_sheet_rankings_are_not_part_of_any_output(tmp_path):
    s = scenario(tmp_path)
    keywords_before = copy.deepcopy(te.mixed_keywords())
    result = evaluate(s, crosscheck=crosscheck(pending_fallbacks=(pending(1),)))
    assert te.mixed_keywords() == keywords_before
    for candidate in result.client_candidates:
        assert not re.search(r"position|ranking|keyword", candidate.text.lower())


# --- reviewer findings: retained, never negative -----------------------------------------------------------------------------

def test_a_missing_comparison_is_empty_and_safe():
    result = hr.evaluate_history_rules(comparison=None, metrics=None, base_insights=None)
    assert result.client_candidates == () and result.reviewer_findings == () and result.validation_failures == ()
    (note,) = result.notes
    assert (note.severity, note.source, note.category) == ("info", "history", "suppressed")
    assert "No historical comparison was available" in note.message


def test_a_first_report_with_no_baselines_produces_nothing_negative(tmp_path):
    s = scenario(tmp_path, baseline=None)
    assert not any(st.eligible for st in s.comparison.status.values())
    result = evaluate(s)
    assert result.client_candidates == () and result.reviewer_findings == () and result.validation_failures == ()
    (note,) = result.notes
    assert "No dimension had an eligible baseline" in note.message and note.severity == "info"


def test_ineligible_dimensions_are_kept_as_one_reviewer_finding_when_other_dimensions_have_a_baseline(tmp_path):
    result = evaluate(scenario(tmp_path, baseline=dict(BASE_DEFAULT, ga4_none=True)))
    found = finding(result, "dimensions_unusable")
    assert found.evidence == {"ineligible_dimensions": 6} and found.section == "history"
    assert "6 dimensions" in found.message


def test_current_data_problems_are_not_reported_as_missing_baselines(tmp_path):
    s = scenario(tmp_path, current=dict(CURRENT_DEFAULT))
    assert finding(evaluate(s), "dimensions_unusable") is None
    thin = scenario(tmp_path / "t", baseline=BASE_DEFAULT)
    assert all(not f.message.lower().startswith("warning") for f in evaluate(thin).reviewer_findings)


def test_different_baselines_are_noted(tmp_path):
    s = scenario(tmp_path, baseline=dict(BASE_DEFAULT, ga4_none=True), older=dict(BASE_DEFAULT))
    assert finding(evaluate(s), "baselines_differ").evidence == {"baseline_runs": 2}


def test_a_baseline_of_a_different_length_is_reviewer_noted(tmp_path):
    s = scenario(tmp_path)
    ref = dataclasses.replace(s.comparison.status["gsc.totals"].baseline, same_length=False, period_end="2026-09-14")
    status = dict(s.comparison.status)
    status["gsc.totals"] = dataclasses.replace(status["gsc.totals"], baseline=ref)
    comparison = dataclasses.replace(s.comparison, status=status)
    found = finding(evaluate(s, comparison=comparison), "different_period_length")
    assert found.evidence["current_days"] == 28 and found.evidence["baseline_days"] == 14


def test_findings_are_neutral_and_never_make_unsupported_claims(tmp_path):
    result = evaluate(scenario(tmp_path, baseline=dict(BASE_DEFAULT, ga4_none=True), older=dict(BASE_DEFAULT),
                               current=dict(CURRENT_DEFAULT, diagnostics={"daily_sessions_diff": 3})),
                      crosscheck=crosscheck(pending_fallbacks=(pending(2),), new_keywords=1))
    assert result.reviewer_findings
    for item in result.reviewer_findings:
        assert item.tone is Tone.NEUTRAL and item.audience == "reviewer"
        assert not w.find_superlatives(item.message) and not w.find_non_us_spelling(item.message)


# --- duplicate protection ---------------------------------------------------------------------------------------------------------

def good_candidate(tmp_path):
    return evaluate(scenario(tmp_path)).client_candidates[0]


def base_insight(key, text, **evidence):
    return Insight("search_console", key, text, Tone.NEUTRAL, 10, evidence)


def test_an_exact_base_insight_duplicate_is_blocked(tmp_path):
    s = scenario(tmp_path)
    first = evaluate(s).client_candidates[0]
    base = [base_insight("gsc.other", first.text.upper().replace(".", "!"))]  # same words, other case and punctuation
    result = evaluate(s, base_insights=base)
    assert first.key not in keys(result)
    assert (first.key, "duplicate_of_base_insight") in [(f.key, f.reason) for f in result.validation_failures]
    assert any("was left out" in n.message and n.severity == "warning" for n in result.notes)


def test_the_same_fact_with_different_wording_is_blocked(tmp_path):
    s = scenario(tmp_path)
    # the base report already compares clicks with a previous value
    stated = base_insight("gsc.clicks", "Clicks climbed from four to six", current=6, previous=4)
    result = evaluate(s, base_insights=[stated])
    assert "history.gsc.clicks" not in keys(result) and "history.gsc.impressions" in keys(result)
    assert ("history.gsc.clicks", "duplicate_of_base_insight") in [(f.key, f.reason) for f in result.validation_failures]
    # the baseline figure already appears in the base statement of the same fact
    worded = base_insight("gsc.clicks", "The website earned 6 organic clicks, against 4 earlier", current=6)
    assert "history.gsc.clicks" not in keys(evaluate(s, base_insights=[worded]))


def test_a_base_insight_about_another_fact_does_not_block(tmp_path):
    s = scenario(tmp_path)
    other = base_insight("gsc.ctr", "The click-through rate was 0.39%, with 4 clicks recorded", previous=0.002)
    assert "history.gsc.clicks" in keys(evaluate(s, base_insights=[other]))
    assert "history.gsc.clicks" in keys(evaluate(s, base_insights=()))


def test_equal_figures_are_not_mistaken_for_a_duplicate(tmp_path):
    s = scenario(tmp_path, baseline=dict(BASE_DEFAULT, clicks=6))
    clicks = next(c for c in evaluate(s).client_candidates if c.key == "history.gsc.clicks")
    assert clicks.text.startswith("Organic clicks from Google Search were essentially unchanged from 6") or \
        clicks.text.startswith("Organic clicks from Google Search were unchanged from 6")
    assert clicks.evidence["direction"] == 0


def test_the_real_base_report_does_not_block_a_new_comparison(tmp_path):
    s = scenario(tmp_path)
    base_texts = {i.text for section in s.insights.sections for i in section.insights}
    result = evaluate(s)
    assert result.client_candidates and not (base_texts & {c.text for c in result.client_candidates})
    assert result.validation_failures == ()


def test_base_insights_are_not_mutated(tmp_path):
    s = scenario(tmp_path)
    before_insights, before_metrics = copy.deepcopy(s.insights), copy.deepcopy(s.metrics)
    before_comparison = copy.deepcopy(s.comparison)
    evaluate(s, crosscheck=crosscheck(pending_fallbacks=(pending(1),)))
    assert s.insights == before_insights and s.metrics == before_metrics and s.comparison == before_comparison


# --- evidence and validation ---------------------------------------------------------------------------------------------------------

def test_every_emitted_item_has_valid_registered_flat_numeric_evidence(tmp_path):
    result = evaluate(scenario(tmp_path, current=dict(CURRENT_DEFAULT, diagnostics={"daily_sessions_diff": 2}),
                               baseline=dict(BASE_DEFAULT, ga4_none=True), older=BASE_DEFAULT),
                      crosscheck=crosscheck(pending_fallbacks=(pending(1),), sheet_previous_mismatch=1))
    assert result.client_candidates and result.reviewer_findings
    for candidate in result.client_candidates:
        assert hr.validate_evidence(candidate.evidence, audience="client") == []
    for item in result.reviewer_findings:
        assert hr.validate_evidence(item.evidence, audience="reviewer") == []
    for item in (*result.client_candidates, *result.reviewer_findings):
        assert set(item.evidence) <= set(hr.EVIDENCE_KEYS)
        for value in item.evidence.values():
            assert isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@pytest.mark.parametrize("evidence", [
    {"made_up": 1}, {"current_value": "6"}, {"current_value": True}, {"current_value": None},
    {"current_value": float("nan")}, {"current_value": float("inf")}, {"current_value": [1]},
    {"current_value": {"a": 1}}, {1: 1}, {"pending_fallbacks": 1}, {"min_impressions": 500},
])
def test_invalid_evidence_is_reported_for_client_candidates(evidence):
    assert hr.validate_evidence(evidence, audience="client")


def test_evidence_must_be_a_flat_dictionary():
    for bad in (None, [], "x", 5):
        assert hr.validate_evidence(bad)
    assert hr.validate_evidence({}) == []
    assert hr.validate_evidence({"pending_fallbacks": 2}, audience="reviewer") == []


def test_candidate_evidence_is_accepted_by_the_record_schema(tmp_path):
    result = evaluate(geo(tmp_path, 800, 700, current={"empty_previous": True}), crosscheck=None)
    assert {c.section for c in result.client_candidates} == {"search_console", "geography", "ga4"}
    for candidate in result.client_candidates:
        record = trs.finalized()
        record["client_facing_history"] = [{
            "section": candidate.section, "key": candidate.key, "dimension": candidate.dimension,
            "text": candidate.text,
            "baseline_period": {"start": candidate.baseline_period_start, "end": candidate.baseline_period_end},
            "evidence": candidate.evidence, "rules_version": candidate.rules_version}]
        assert record_schema.validate_finalized_record(record) is record


def test_every_failure_reason_is_registered_and_stable(tmp_path):
    good = good_candidate(tmp_path)
    bad = {
        "invalid_evidence": dataclasses.replace(good, evidence={**good.evidence, "surprise": 1}),
        "untraced_number": dataclasses.replace(good, text=good.text.replace(" from 4", " from 4 after 7 tries")),
        "unsafe_wording": dataclasses.replace(good, text=good.text.replace("Organic", "Excellent organic")),
        "missing_baseline_period": dataclasses.replace(good, baseline_period_start="not-a-date"),
        "inconsistent_comparison": dataclasses.replace(good, evidence={**good.evidence, "direction": -1}),
        "invalid_identity": dataclasses.replace(good, section="keywords"),
    }
    for reason, candidate in bad.items():
        assert reason in hr.validate_client_candidate(candidate), reason
    duplicate = hr.validate_client_candidate(good, [base_insight("gsc.clicks", good.text)])
    assert "duplicate_of_base_insight" in duplicate
    assert hr.validate_client_candidate(good) == ()
    assert set(bad) | {"duplicate_of_base_insight"} == set(hr.VALIDATION_REASONS)


def test_growth_words_are_only_allowed_when_the_figure_went_up(tmp_path):
    good = good_candidate(tmp_path)
    assert good.evidence["direction"] == 1 and hr.validate_client_candidate(good) == ()
    fell = dataclasses.replace(good, evidence={**good.evidence, "direction": -1},
                               text=good.text.replace("up from", "down from"))
    assert hr.validate_client_candidate(fell) == ()
    wrong = dataclasses.replace(good, evidence={**good.evidence, "direction": -1})
    assert "unsafe_wording" in hr.validate_client_candidate(wrong)
    british = dataclasses.replace(good, text=good.text.replace("Organic", "Organic optimisation"))
    assert "unsafe_wording" in hr.validate_client_candidate(british)


def test_a_candidate_must_name_its_baseline_period_in_the_text(tmp_path):
    good = good_candidate(tmp_path)
    period = w.format_date_range(dt.date.fromisoformat(good.baseline_period_start),
                                 dt.date.fromisoformat(good.baseline_period_end))
    assert "missing_baseline_period" in hr.validate_client_candidate(
        dataclasses.replace(good, text=good.text.replace(period, "an earlier period")))
    assert "missing_baseline_period" in hr.validate_client_candidate(
        dataclasses.replace(good, baseline_period_end="2020-01-01"))


def test_rejected_candidates_become_validation_failures_not_output(tmp_path, monkeypatch):
    s = scenario(tmp_path)
    original = hr._count_outcome

    def broken(spec, comparison, metrics):
        candidate, found = original(spec, comparison, metrics)
        if candidate is not None and spec.name == "clicks":
            candidate = dataclasses.replace(candidate, text=candidate.text + " Excellent.")
        return candidate, found

    monkeypatch.setattr(hr, "_count_outcome", broken)
    result = evaluate(s)
    assert "history.gsc.clicks" not in keys(result) and "history.gsc.impressions" in keys(result)
    failures = [(f.key, f.reason) for f in result.validation_failures]
    assert ("history.gsc.clicks", "unsafe_wording") in failures
    assert all(reason in hr.VALIDATION_REASONS for _, reason in failures)
    assert all(isinstance(f, hr.HistoryValidationFailure) for f in result.validation_failures)


def test_validation_failures_stay_separate_from_ordinary_caveats(tmp_path):
    result = evaluate(scenario(tmp_path, current=dict(CURRENT_DEFAULT, empty_previous=False)))
    assert result.validation_failures == () and result.reviewer_findings  # blocked statements are not failures


def test_validation_failure_codes_are_accepted_by_the_reviewer_record_schema(tmp_path):
    for reason in hr.VALIDATION_REASONS:
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason)
    record = trs.finalized()
    record["validation_failures"] = [{"key": "history.gsc.clicks", "reason": "unsafe_wording"}]
    assert record_schema.validate_finalized_record(record) is record


# --- determinism, purity and metadata ---------------------------------------------------------------------------------------------

def test_repeated_evaluation_is_equivalent_and_ordered(tmp_path):
    s = scenario(tmp_path, baseline=dict(BASE_DEFAULT, ga4_none=True), older=BASE_DEFAULT)
    check = crosscheck(pending_fallbacks=(pending(1),), new_keywords=2)
    first, second = evaluate(s, crosscheck=check), evaluate(s, crosscheck=check)
    assert first == second
    order = {section: index for index, section in enumerate(("keywords", "search_console", "geography", "ga4"))}
    sort_keys = [(order[c.section], c.priority, c.key) for c in first.client_candidates]
    assert sort_keys == sorted(sort_keys)
    review = [(f.priority, f.key) for f in first.reviewer_findings]
    assert review == sorted(review) and len({f.key for f in first.reviewer_findings}) == len(review)


def test_client_candidates_carry_the_metadata_stage_6c_5_needs(tmp_path):
    result = evaluate(geo(tmp_path, 800, 700, current={"empty_previous": True}))
    assert len(result.client_candidates) == 5
    for candidate in result.client_candidates:
        assert candidate.group == "since_last_report" and candidate.section in ("search_console", "geography", "ga4")
        assert isinstance(candidate.priority, int) and candidate.key.startswith("history.")
        assert candidate.rules_version == "1" and candidate.dimension in el.DIMENSIONS
    per_section = {}
    for candidate in result.client_candidates:
        per_section.setdefault(candidate.section, []).append(candidate)
    assert max(len(items) for items in per_section.values()) <= hr.MAX_STATEMENTS_PER_SECTION
    assert len({c.key for c in result.client_candidates}) == len(result.client_candidates)


def test_client_candidates_hold_no_run_ids_hashes_or_paths(tmp_path):
    s = scenario(tmp_path)
    result = evaluate(s)
    run_ids = {ref.run_id for ref in (st.baseline for st in s.comparison.status.values()) if ref}
    dump = repr(result.client_candidates)
    assert run_ids and not any(run_id in dump for run_id in run_ids)
    assert str(tmp_path) not in dump and not re.search(r"[0-9a-f]{64}", dump)


def test_the_module_is_pure():
    source = (ROOT / "src" / "seo_report" / "insights" / "history_rules.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for pattern in (r"\.now\(", r"today\(", r"\btime\.", r"\bopen\(", r"write_text", r"write_bytes", r"\.mkdir\(",
                    r"sqlite", r"requests", r"googleapiclient", r"random", r"\bos\.", r"environ"):
        assert not re.search(pattern, code), pattern


def test_outputs_contain_no_timestamps(tmp_path):
    result = evaluate(scenario(tmp_path))
    dump = repr(result)
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", dump)


def test_unexpected_programming_errors_are_not_swallowed(tmp_path):
    s = scenario(tmp_path)
    with pytest.raises(AttributeError):
        hr.evaluate_history_rules(comparison=object(), metrics=s.metrics, base_insights=s.insights)
    with pytest.raises(AttributeError):
        hr.evaluate_history_rules(comparison=s.comparison, metrics=object(), base_insights=s.insights)


# --- safe with partial inputs -------------------------------------------------------------------------------------------------------

def test_no_eligible_dimensions_gives_only_a_note(tmp_path):
    s = scenario(tmp_path, baseline=None)
    result = evaluate(s, crosscheck=crosscheck())
    assert result.client_candidates == () and result.reviewer_findings == ()


def test_some_eligible_and_some_ineligible_dimensions(tmp_path):
    result = evaluate(scenario(tmp_path, baseline=dict(BASE_DEFAULT, ga4_none=True)))
    assert {c.section for c in result.client_candidates} == {"search_console"}
    assert finding(result, "dimensions_unusable") is not None


def test_a_comparison_without_the_gsc_or_ga4_block_is_safe(tmp_path):
    s = scenario(tmp_path, baseline=dict(BASE_DEFAULT, gsc_none=True, ga4_none=True))
    result = evaluate(s)
    assert result.client_candidates == ()


def test_every_ranking_finding_kind_is_produced_from_a_full_cross_check(tmp_path):
    check = rr.RankingCrosscheck(fallback_used=0, sheet_previous_mismatch=1, new_keywords=1, no_longer_tracked=1,
                                 not_comparable=1, pending_fallbacks=(pending(3, "Roof Repair"), pending(0, "Same")))
    result = evaluate(scenario(tmp_path), crosscheck=check)
    assert {f.kind for f in result.reviewer_findings} >= {
        "ranking_pending_fallback", "ranking_sheet_mismatch", "ranking_new_keywords", "ranking_lost_keywords",
        "ranking_not_comparable"}