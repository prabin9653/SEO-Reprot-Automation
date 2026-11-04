import dataclasses
import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_engine as te  # shared synthetic fixtures (helper functions only)
import test_snapshot as ts
from seo_report.periods import report_periods
from seo_report.snapshots import eligibility as el
from seo_report.sources import ga4 as ga
from seo_report.sources import gsc as gs

UNSET = ts.UNSET
GSC = el.GSC_DIMENSIONS
GA4 = el.GA4_DIMENSIONS
CURRENT = te.PERIODS


# --- shared helpers (also imported by the other history test files) ------------------------------------------------------

def periods_from(start: dt.date):
    """A 28-day reporting period starting on `start`, with its 28-day comparison period."""
    return report_periods(start, start + dt.timedelta(days=27))


def complete_data(periods=CURRENT):
    """Search Console and GA4 data with daily rows and channels, and no warnings."""
    gsc, ga4 = te.live_like_gsc(), te.live_like_ga4()
    days = periods.current.dates()
    gsc_current = dataclasses.replace(
        gsc.current, daily=[gs.DailyPoint(d, gs.Metrics(0, 70, 0.0, 30.0), True) for d in days],
        latest_data_date=periods.current.end)
    ga4_current = dataclasses.replace(
        ga4.current, daily=[ga.DailySessions(d, 3, 1, True) for d in days],
        channels=[ga.ChannelRow("Direct", te.g4(67, 65, 40, 0.6)), ga.ChannelRow("Organic Search", te.g4(19, 13, 9, 0.474))],
        landing_pages=[te.landing("/", 50), te.landing("/about", 12)])
    return dataclasses.replace(gsc, current=gsc_current), dataclasses.replace(ga4, current=ga4_current)


def relabel(gsc, ga4, periods):
    """The same data relabelled for other reporting periods (None stays None)."""
    new_gsc = None if gsc is None else gs.SearchConsoleData(
        gsc.site_url, gsc.permission_level, dataclasses.replace(gsc.current, period=periods.current),
        dataclasses.replace(gsc.previous, period=periods.previous))
    new_ga4 = None if ga4 is None else ga.Ga4Data(
        ga4.property_id, ga4.key_events_status, dataclasses.replace(ga4.current, period=periods.current),
        dataclasses.replace(ga4.previous, period=periods.previous))
    return new_gsc, new_ga4


def payload_for(periods=CURRENT, *, gsc=UNSET, ga4=UNSET, keywords=None, client=None, key="example-co"):
    default_gsc, default_ga4 = complete_data(periods)
    gsc = default_gsc if gsc is UNSET else gsc
    ga4 = default_ga4 if ga4 is UNSET else ga4
    gsc, ga4 = relabel(gsc, ga4, periods)
    return ts.build(client_key=key, gsc=gsc, ga4=ga4, keywords=keywords, client=client, periods=periods).payload


def ineligible(assessment):
    return {d for d, a in assessment.items() if not a.eligible}


# --- structure ------------------------------------------------------------------------------------------------------------

def test_there_are_twelve_independent_dimensions():
    assert len(el.DIMENSIONS) == 12 and len(set(el.DIMENSIONS)) == 12
    assert el.DIMENSIONS[0] == "rankings.keywords" and len(GSC) == 5 and len(GA4) == 6
    assert list(el.assess(payload_for())) == list(el.DIMENSIONS)


def test_complete_data_is_eligible_everywhere():
    assessment = el.assess(payload_for())
    assert ineligible(assessment) == set()
    assert all(a.membership_complete and a.reasons == () and a.caveats == () for a in assessment.values())


def test_missing_daily_data_makes_only_the_daily_dimensions_ineligible():
    payload = ts.build().payload  # the plain fixtures have no daily rows
    assessment = el.assess(payload)
    assert ineligible(assessment) == {"gsc.daily", "ga4.daily"}
    assert assessment["gsc.daily"].reasons == ("no daily Search Console data was recorded",)
    assert assessment["ga4.daily"].reasons == ("no daily GA4 data was recorded",)


# --- rankings ------------------------------------------------------------------------------------------------------------

def test_rankings_eligibility():
    assert el.assess(payload_for(keywords=[te.kw("a", "", 2, "X"), te.kw("b", "", "", "Y")]))[el.RANKINGS].eligible
    blank = el.assess(payload_for(keywords=[te.kw("a"), te.kw("b")]))[el.RANKINGS]
    assert blank.eligible is False and blank.reasons == ("no current keyword rankings were recorded",)
    none = el.assess(payload_for(keywords=[]))[el.RANKINGS]
    assert none.eligible is False and none.reasons == ("no keywords were tracked",)
    only_above_10 = el.assess(payload_for(keywords=[te.kw("a", "", "Above 10", "X")]))[el.RANKINGS]
    assert only_above_10.eligible  # a recorded label is a recorded ranking


def test_rankings_problems_do_not_touch_other_dimensions():
    assessment = el.assess(payload_for(keywords=[te.kw("a"), te.kw("b")]))
    assert ineligible(assessment) == {el.RANKINGS}


# --- Search Console --------------------------------------------------------------------------------------------------------

def test_search_console_not_provided_affects_only_its_dimensions():
    assessment = el.assess(payload_for(gsc=None))
    assert ineligible(assessment) == set(GSC)
    assert assessment["gsc.totals"].reasons == ("Search Console data was not provided",)


@pytest.mark.parametrize("status_warning, reason", [
    (gs.DataWarning("incomplete_data", "late"), "incomplete"),
    (gs.DataWarning("empty_period", "none"), "no data"),
])
def test_empty_or_incomplete_search_console_periods_make_every_gsc_dimension_ineligible(status_warning, reason):
    assessment = el.assess(payload_for(gsc=te.live_like_gsc(current={"warnings": [status_warning]})))
    assert ineligible(assessment) == set(GSC)
    assert all(reason in a.reasons[0] for d, a in assessment.items() if d in GSC)


@pytest.mark.parametrize("truncated, expected", [
    ("query", {"gsc.queries"}), ("page", {"gsc.pages"}), ("country", {"gsc.countries"}), ("date", {"gsc.daily"}),
])
def test_a_truncated_dimension_is_ineligible_and_nothing_else_is(truncated, expected):
    gsc, _ = complete_data()
    warned = dataclasses.replace(gsc, current=dataclasses.replace(
        gsc.current, warnings=[gs.DataWarning("truncated", "cut", truncated)]))
    assessment = el.assess(payload_for(gsc=warned))
    assert ineligible(assessment) == expected
    assert assessment["gsc.totals"].eligible  # truncated query data never invalidates complete headline metrics
    assert "truncated" in assessment[next(iter(expected))].reasons[0]


def test_a_capped_list_keeps_its_values_but_loses_membership():
    queries = [te.row(f"query {i:04d}", i % 7, 1000 - i, 5.0) for i in range(520)]
    assessment = el.assess(payload_for(gsc=te.live_like_gsc(current={"queries": queries})))
    queries_status = assessment["gsc.queries"]
    assert queries_status.eligible and queries_status.membership_complete is False
    assert queries_status.caveats == ("only the top rows were stored, so new or lost items cannot be reported",)
    assert assessment["gsc.pages"].membership_complete and assessment["gsc.totals"].membership_complete


# --- GA4 -----------------------------------------------------------------------------------------------------------------------

def test_ga4_not_provided_affects_only_its_dimensions():
    assessment = el.assess(payload_for(ga4=None))
    assert ineligible(assessment) == set(GA4)


@pytest.mark.parametrize("warning, reason", [
    (ga.Ga4Warning("incomplete_data", "late"), "incomplete"),
    (ga.Ga4Warning("empty_period", "none"), "no data"),
])
def test_empty_or_incomplete_ga4_periods_make_every_ga4_dimension_ineligible(warning, reason):
    assessment = el.assess(payload_for(ga4=te.live_like_ga4(current={"warnings": [warning]})))
    assert ineligible(assessment) == set(GA4)
    assert all(reason in a.reasons[0] for d, a in assessment.items() if d in GA4)


@pytest.mark.parametrize("report, expected", [
    ("totals", {"ga4.site"}),
    ("channels", {"ga4.channels", "ga4.organic"}),  # organic totals come from the channel report
    ("daily", {"ga4.daily"}),
    ("landing_pages", {"ga4.landing_pages"}),
    ("organic_landing_pages", {"ga4.organic_landing_pages"}),
])
def test_a_truncated_ga4_report_affects_only_the_dimensions_built_from_it(report, expected):
    _, ga4 = complete_data()
    warned = dataclasses.replace(ga4, current=dataclasses.replace(
        ga4.current, warnings=[ga.Ga4Warning("truncated", "cut", report)]))
    assessment = el.assess(payload_for(ga4=warned))
    assert ineligible(assessment) == expected
    assert all(assessment[d].eligible for d in GSC)  # Search Console is untouched


@pytest.mark.parametrize("code, fragment, membership", [
    ("thresholded", "not complete or exact", True),
    ("sampled", "not exact", True),
    ("other_row", "not complete", False),
])
def test_thresholded_sampled_and_folded_ga4_data_stays_eligible_with_caveats(code, fragment, membership):
    _, ga4 = complete_data()
    warned = dataclasses.replace(ga4, current=dataclasses.replace(
        ga4.current, warnings=[ga.Ga4Warning(code, "reported", "channels")]))
    assessment = el.assess(payload_for(ga4=warned))
    for dimension in ("ga4.channels", "ga4.organic"):
        status = assessment[dimension]
        assert status.eligible and len(status.caveats) == 1 and fragment in status.caveats[0]
        assert status.membership_complete is membership
    assert assessment["ga4.site"].caveats == () and ineligible(assessment) == set()


def test_ga4_capped_landing_pages_lose_membership_only():
    pages = [te.landing(f"/p{i:03d}", 300 - i) for i in range(260)]
    assessment = el.assess(payload_for(ga4=te.live_like_ga4(current={"pages": pages})))
    assert assessment["ga4.organic_landing_pages"].eligible
    assert assessment["ga4.organic_landing_pages"].membership_complete is False
    assert assessment["ga4.landing_pages"].membership_complete  # the whole-site list was not capped here


def test_dimensions_are_independent_of_each_other():
    """Every single cause below removes exactly its own dimensions."""
    base = payload_for()
    assert ineligible(el.assess(base)) == set()
    causes = {
        "no gsc": (payload_for(gsc=None), set(GSC)),
        "no ga4": (payload_for(ga4=None), set(GA4)),
        "blank rankings": (payload_for(keywords=[te.kw("a")]), {el.RANKINGS}),
    }
    for name, (payload, expected) in causes.items():
        assert ineligible(el.assess(payload)) == expected, name


# --- compatibility -------------------------------------------------------------------------------------------------------------

def test_compatible_runs_have_no_reasons():
    assert all(v == () for v in el.compatibility(payload_for(), payload_for(periods_from(dt.date(2026, 9, 1)))).values())


def test_a_different_search_console_property_blocks_only_search_console():
    other_gsc = dataclasses.replace(complete_data()[0], site_url="https://other.test/")
    reasons = el.compatibility(payload_for(), payload_for(gsc=other_gsc))
    assert {d for d, r in reasons.items() if r} == set(GSC)
    assert "Search Console property differs" in reasons["gsc.totals"][0]


def test_a_different_ga4_property_blocks_only_ga4():
    other_ga4 = dataclasses.replace(complete_data()[1], property_id="999999999")
    reasons = el.compatibility(payload_for(), payload_for(ga4=other_ga4))
    assert {d for d, r in reasons.items() if r} == set(GA4)
    assert "GA4 property differs" in reasons["ga4.site"][0]


def test_unsupported_formats_are_never_compatible():
    old = dict(payload_for(), format=99)
    assert all(r for r in el.compatibility(payload_for(), old).values())
    assert all(r for r in el.compatibility(old, payload_for()).values())


def test_sources_missing_on_either_side_do_not_raise_property_errors():
    assert all(v == () for v in el.compatibility(payload_for(gsc=None, ga4=None), payload_for()).values())


def test_eligibility_module_is_pure():
    source = (ROOT / "src" / "seo_report" / "snapshots" / "eligibility.py").read_text(encoding="utf-8")
    for forbidden in ("import sqlite3", "import datetime", "import time", "import uuid", "import random", "import os",
                      "open(", "print(", "googleapiclient", "seo_report.sources", "seo_report.auth"):
        assert forbidden not in source
