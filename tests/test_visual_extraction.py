import copy
import dataclasses
import datetime as dt
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_history_integration as ti
from seo_report import render_input as ri
from seo_report import visual_extraction as vx


@pytest.fixture
def snapshot():
    return ti.make_snapshot(ti.CURRENT)


@pytest.fixture
def payload(snapshot):
    return copy.deepcopy(snapshot.payload)


def gsc(payload):
    return payload["gsc"]["periods"]["current"]


def ga4(payload):
    return payload["ga4"]["periods"]["current"]


def mark(block, *issues):
    """Set a period's issues and the status the snapshot builder derives from them."""
    block["issues"] = list(issues)
    block["status"] = next((name for name in ("empty", "incomplete", "truncated") if name in issues), "ok")


UNAVAILABLE = (ri.SearchConsoleVisuals.unavailable(), ri.GeographyVisuals.unavailable(), ri.Ga4Visuals.unavailable())


# --- Search Console: complete, partial, incomplete, truncated, unavailable ----------------------------------------------------------------

def test_a_complete_search_console_snapshot_is_available(payload):
    visual = vx.extract_search_console(payload)
    source = gsc(payload)
    assert type(visual) is ri.SearchConsoleVisuals and visual.status == "available"
    assert (visual.clicks, visual.impressions) == (source["totals"]["clicks"], source["totals"]["impressions"])
    assert visual.ctr == Decimal("0.0039") and visual.average_position == Decimal("29.6")  # fractions, not percents
    assert len(visual.daily) == 28 and all(type(p) is ri.DailyPoint for p in visual.daily)
    first = source["daily"][0]
    assert visual.daily[0] == ri.DailyPoint(dt.date.fromisoformat(first["date"]), first["clicks"], first["impressions"])


def test_a_snapshot_object_and_its_payload_give_the_same_result(snapshot):
    assert vx.extract_visuals(snapshot) == vx.extract_visuals(snapshot.payload)


def test_daily_order_is_the_stored_ascending_order(payload):
    visual = vx.extract_search_console(payload)
    assert [p.day.isoformat() for p in visual.daily] == [d["date"] for d in gsc(payload)["daily"] if d["has_data"]]


def test_zero_values_are_data_not_missing(payload):
    source = gsc(payload)
    source["totals"].update(clicks=0, impressions=10, ctr=0.0, position=5.5)
    source["daily"][0].update(clicks=0, impressions=0)
    visual = vx.extract_search_console(payload)
    assert (visual.status, visual.clicks, visual.ctr) == ("available", 0, Decimal("0.0"))
    assert visual.daily[0].clicks == 0 and visual.daily[0].impressions == 0


@pytest.mark.parametrize("missing", ["ctr", "position", "clicks", "impressions"])
def test_a_missing_total_stays_missing_and_the_visual_stays_available(payload, missing):
    gsc(payload)["totals"][missing] = None
    visual = vx.extract_search_console(payload)
    assert visual.status == "available"
    field = {"position": "average_position"}.get(missing, missing)
    assert getattr(visual, field) is None
    others = {n: getattr(visual, n) for n in ("clicks", "impressions", "ctr", "average_position") if n != field}
    assert all(value is not None for value in others.values())  # the valid metrics are kept
    assert len(visual.daily) == 28


def test_the_example_with_a_missing_average_position(payload):
    gsc(payload)["totals"].update(clicks=120, impressions=3500, ctr=0.0343, position=None)
    visual = vx.extract_search_console(payload)
    assert (visual.status, visual.clicks, visual.impressions, visual.ctr, visual.average_position) == \
        ("available", 120, 3500, Decimal("0.0343"), None)


def test_all_totals_missing_but_daily_points_present_is_still_usable(payload):
    gsc(payload)["totals"].update(clicks=None, impressions=None, ctr=None, position=None)
    visual = vx.extract_search_console(payload)
    assert visual.status == "available" and visual.clicks is None and len(visual.daily) == 28


def test_nothing_usable_is_unavailable(payload):
    gsc(payload)["totals"].update(clicks=None, impressions=None, ctr=None, position=None)
    gsc(payload)["daily"] = []
    assert vx.extract_search_console(payload) == ri.SearchConsoleVisuals.unavailable()
    changed = copy.deepcopy(payload)
    gsc(changed)["daily"] = [{"date": "2026-10-01", "has_data": False, "clicks": None, "impressions": None,
                              "ctr": None, "position": None}]
    assert vx.extract_search_console(changed) == ri.SearchConsoleVisuals.unavailable()


def test_incomplete_data_is_kept_and_marked_incomplete_not_unavailable(payload):
    mark(gsc(payload), "incomplete")
    visual = vx.extract_search_console(payload)
    assert visual.status == "incomplete" and visual.clicks == 6 and len(visual.daily) == 28


def test_incomplete_and_truncated_together_is_incomplete(payload):
    mark(gsc(payload), "incomplete", "truncated")
    assert vx.extract_search_console(payload).status == "incomplete"


def test_a_truncated_period_has_complete_search_console_data(payload):
    mark(gsc(payload), "truncated")
    gsc(payload)["source_truncated"] = ["query", "page", "country"]
    visual = vx.extract_search_console(payload)
    assert visual.status == "available" and not hasattr(visual, "truncation")  # SC totals and daily are intact


def test_an_empty_period_or_a_missing_source_is_unavailable(payload):
    changed = copy.deepcopy(payload)
    mark(gsc(changed), "empty")
    gsc(changed)["totals"].update(clicks=None, impressions=None, ctr=None, position=None)
    assert vx.extract_search_console(changed) == ri.SearchConsoleVisuals.unavailable()
    payload["gsc"] = {"provided": False}
    assert vx.extract_search_console(payload) == ri.SearchConsoleVisuals.unavailable()
    assert vx.extract_geography(payload) == ri.GeographyVisuals.unavailable()


def test_days_without_data_are_omitted_and_no_zero_is_created(payload):
    for index in (0, 3):
        gsc(payload)["daily"][index].update(has_data=False, clicks=None, impressions=None, ctr=None, position=None)
    visual = vx.extract_search_console(payload)
    days = {p.day for p in visual.daily}
    assert len(visual.daily) == 26 and dt.date(2026, 9, 29) not in days and dt.date(2026, 10, 2) not in days
    assert [p.day for p in visual.daily] == sorted(days)


def test_the_october_example_missing_the_third_is_not_filled(payload):
    gsc(payload)["daily"] = [
        {"date": "2026-10-01", "has_data": True, "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 3.0},
        {"date": "2026-10-02", "has_data": True, "clicks": 0, "impressions": 12, "ctr": 0.0, "position": 4.0},
        {"date": "2026-10-03", "has_data": False, "clicks": None, "impressions": None, "ctr": None, "position": None},
        {"date": "2026-10-04", "has_data": True, "clicks": 2, "impressions": 9, "ctr": 0.2, "position": 3.5},
        {"date": "2026-10-05", "has_data": True, "clicks": 0, "impressions": 8, "ctr": 0.0, "position": 6.0}]
    visual = vx.extract_search_console(payload)
    assert [(p.day.day, p.clicks) for p in visual.daily] == [(1, 1), (2, 0), (4, 2), (5, 0)]  # a measured 0 stays 0


def test_empty_trend_data_is_available_with_no_points(payload):
    gsc(payload)["daily"] = []
    visual = vx.extract_search_console(payload)
    assert visual.status == "available" and visual.daily == ()


@pytest.mark.parametrize("path,value", [
    (("totals", "clicks"), 1.5), (("totals", "clicks"), True), (("totals", "clicks"), "6"), (("totals", "clicks"), -1),
    (("totals", "ctr"), "0.1"), (("totals", "ctr"), float("nan")), (("totals", "ctr"), float("inf")),
    (("totals", "ctr"), -0.1), (("totals", "position"), True), (("totals", "impressions"), 2.0)])
def test_malformed_totals_are_rejected(payload, path, value):
    gsc(payload)[path[0]][path[1]] = value
    with pytest.raises(vx.VisualExtractionError) as caught:
        vx.extract_search_console(payload)
    assert "payload.gsc.periods.current.totals" in str(caught.value) and str(value) not in str(caught.value)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(date="2026-13-40"), lambda d: d.update(date=20260929), lambda d: d.update(clicks="3"),
    lambda d: d.update(impressions=-5), lambda d: d.update(clicks=None), lambda d: d.update(has_data="yes"),
    lambda d: d.pop("has_data")])
def test_malformed_daily_rows_are_rejected(payload, mutate):
    mutate(gsc(payload)["daily"][1])
    with pytest.raises(vx.VisualExtractionError):
        vx.extract_search_console(payload)


def test_duplicate_and_out_of_order_daily_dates_are_rejected(payload):
    gsc(payload)["daily"][2]["date"] = gsc(payload)["daily"][1]["date"]
    with pytest.raises(vx.VisualExtractionError) as caught:
        vx.extract_search_console(payload)
    assert caught.value.code == "duplicate_date"
    changed = copy.deepcopy(payload)
    gsc(changed)["daily"] = [{"date": "2026-10-02", "has_data": True, "clicks": 1, "impressions": 1},
                             {"date": "2026-10-01", "has_data": True, "clicks": 1, "impressions": 1}]
    with pytest.raises(vx.VisualExtractionError) as caught:
        vx.extract_search_console(changed)
    assert caught.value.code == "dates_out_of_order"


@pytest.mark.parametrize("break_it", [
    lambda p: p.pop("gsc"), lambda p: p.update(gsc=None), lambda p: p["gsc"].pop("provided"),
    lambda p: p["gsc"].update(provided="yes"), lambda p: p["gsc"].pop("periods"),
    lambda p: gsc(p).pop("status"), lambda p: gsc(p).update(status="weird"), lambda p: gsc(p).pop("totals"),
    lambda p: gsc(p).update(daily="x"), lambda p: gsc(p).pop("daily"), lambda p: p.update(format=999),
    lambda p: gsc(p)["totals"].pop("ctr"), lambda p: gsc(p).pop("issues"), lambda p: gsc(p).update(issues="empty"),
    lambda p: gsc(p).update(issues=["bogus"]), lambda p: gsc(p).update(issues=["incomplete", "incomplete"]),
    lambda p: gsc(p).update(status="empty"),  # status says empty, issues do not
    lambda p: gsc(p).update(issues=["incomplete"])])  # issues say incomplete, status says ok
def test_malformed_structures_are_rejected_with_a_fixed_code(payload, break_it):
    break_it(payload)
    with pytest.raises(vx.VisualExtractionError) as caught:
        vx.extract_search_console(payload)
    assert re.fullmatch(r"[a-z_]+", caught.value.code) and caught.value.path.startswith("payload")


@pytest.mark.parametrize("bad", [None, [], "payload", 5])
def test_a_payload_that_is_not_a_mapping_is_rejected(bad):
    with pytest.raises(vx.VisualExtractionError):
        vx.extract_search_console(bad)


# --- geography ---------------------------------------------------------------------------------------------------------------

def test_a_complete_geography_snapshot_keeps_labels_values_and_order(payload):
    visual = vx.extract_geography(payload)
    source = gsc(payload)["countries"]
    assert type(visual) is ri.GeographyVisuals and visual.status == "available" and visual.truncation == ()
    assert [(c.name, c.clicks, c.impressions) for c in visual.countries] == \
        [(c["name"], c["clicks"], c["impressions"]) for c in source]
    assert visual.countries[0].name == "United States" and all(type(c) is ri.CountryRow for c in visual.countries)


def test_the_target_country_is_never_used(payload):
    payload["client"]["target_country"] = "Atlantis"
    gsc(payload)["countries"] = [{"code": "usa", "name": "United States", "clicks": 3, "impressions": 100,
                                  "ctr": 0.03, "position": 2.0}]
    assert [c.name for c in vx.extract_geography(payload).countries] == ["United States"]
    gsc(payload)["countries"] = []
    assert vx.extract_geography(payload) == ri.GeographyVisuals.unavailable()  # no target-country stand-in row


def test_unicode_country_labels_are_preserved_exactly(payload):
    gsc(payload)["countries"] = [
        {"code": "civ", "name": "Côte d’Ivoire", "clicks": 2, "impressions": 20},
        {"code": "tur", "name": "Türkiye", "clicks": 1, "impressions": 10},
        {"code": "jpn", "name": "日本", "clicks": 0, "impressions": 3}]
    assert [c.name for c in vx.extract_geography(payload).countries] == ["Côte d’Ivoire", "Türkiye",
                                                                           "日本"]


def test_a_country_with_zero_clicks_is_kept(payload):
    assert {c.name: c.clicks for c in vx.extract_geography(payload).countries}["Canada"] == 0


def test_an_empty_country_dataset_is_unavailable(payload):
    gsc(payload)["countries"] = []
    visual = vx.extract_geography(payload)
    assert visual == ri.GeographyVisuals.unavailable() and visual.countries == () and visual.truncation == ()


def test_incomplete_geography_keeps_its_rows(payload):
    mark(gsc(payload), "incomplete")
    visual = vx.extract_geography(payload)
    assert visual.status == "incomplete" and len(visual.countries) == 4


def test_empty_period_geography_is_unavailable(payload):
    mark(gsc(payload), "empty")
    assert vx.extract_geography(payload) == ri.GeographyVisuals.unavailable()


def test_source_truncation_is_retained_without_an_invented_count(payload):
    mark(gsc(payload), "truncated")
    gsc(payload)["source_truncated"] = ["country", "query"]
    visual = vx.extract_geography(payload)
    assert visual.status == "available"
    assert visual.truncation == (ri.TruncationNote("countries", "source_limit", None),)


def test_a_truncated_query_list_does_not_mark_geography_truncated(payload):
    mark(gsc(payload), "truncated")
    gsc(payload)["source_truncated"] = ["query", "page"]
    assert vx.extract_geography(payload).truncation == ()


def test_the_snapshot_cap_is_retained(payload):
    gsc(payload)["capped"]["countries"] = True
    assert vx.extract_geography(payload).truncation == (ri.TruncationNote("countries", "snapshot_cap", None),)


def test_source_limit_and_snapshot_cap_are_both_kept(payload):
    mark(gsc(payload), "truncated")
    gsc(payload)["source_truncated"] = ["country"]
    gsc(payload)["capped"]["countries"] = True
    reasons = [n.reason for n in vx.extract_geography(payload).truncation]
    assert reasons == ["snapshot_cap", "source_limit"]


def test_max_rows_reports_the_exact_number_left_out(payload):
    visual = vx.extract_geography(payload, max_rows=2)
    assert [c.name for c in visual.countries] == ["United States", "Pakistan"]
    assert visual.truncation == (ri.TruncationNote("countries", "row_limit", 2),)
    assert vx.extract_geography(payload, max_rows=4).truncation == ()  # nothing left out, nothing reported
    assert vx.extract_geography(payload, max_rows=99).truncation == ()
    for bad in (0, -1, True, 1.5, "2"):
        with pytest.raises(ValueError):
            vx.extract_geography(payload, max_rows=bad)


def test_row_limit_adds_to_source_truncation(payload):
    mark(gsc(payload), "truncated")
    gsc(payload)["source_truncated"] = ["country"]
    notes = vx.extract_geography(payload, max_rows=1).truncation
    assert [(n.reason, n.omitted_rows) for n in notes] == [("row_limit", 3), ("source_limit", None)]


@pytest.mark.parametrize("row", [{"name": "", "clicks": 1, "impressions": 1}, {"name": None, "clicks": 1, "impressions": 1},
                                 {"name": "X", "clicks": None, "impressions": 1}, {"name": "X", "clicks": 1},
                                 {"name": "X", "clicks": 1.5, "impressions": 1}, {"name": 5, "clicks": 1, "impressions": 1},
                                 "text", None])
def test_malformed_country_rows_are_rejected(payload, row):
    gsc(payload)["countries"].append(row)
    with pytest.raises(vx.VisualExtractionError):
        vx.extract_geography(payload)


@pytest.mark.parametrize("break_it", [lambda p: gsc(p).pop("capped"), lambda p: gsc(p)["capped"].pop("countries"),
                                      lambda p: gsc(p)["capped"].update(countries="yes"),
                                      lambda p: gsc(p).pop("source_truncated"),
                                      lambda p: gsc(p).update(source_truncated="country"),
                                      lambda p: gsc(p).update(source_truncated=[5])])
def test_malformed_truncation_metadata_is_rejected(payload, break_it):
    break_it(payload)
    with pytest.raises(vx.VisualExtractionError):
        vx.extract_geography(payload)


# --- GA4 ------------------------------------------------------------------------------------------------------------------------

def test_a_complete_ga4_snapshot_is_available(payload):
    visual = vx.extract_ga4(payload)
    source = ga4(payload)
    assert type(visual) is ri.Ga4Visuals and visual.status == "available" and visual.truncation == ()
    assert (visual.sessions, visual.users, visual.engagement_rate) == (90, 82, Decimal("0.311"))
    assert [(c.channel, c.sessions, c.users, c.engaged_sessions) for c in visual.channels] == \
        [(c["channel"], c["sessions"], c["total_users"], c["engaged_sessions"]) for c in source["channels"]]
    assert visual.channels[0].channel == "Direct" and len(visual.daily) == 28
    assert visual.daily[0] == ri.Ga4DailyPoint(dt.date.fromisoformat(source["daily"][0]["date"]), 3)


def test_ga4_organic_figures_and_landing_pages_are_not_substituted(payload):
    ga4(payload)["organic"]["sessions"] = 12345
    assert vx.extract_ga4(payload).sessions == 90


def test_ga4_zero_values_are_data(payload):
    ga4(payload)["site"].update(sessions=0, total_users=0, engagement_rate=0.0)
    ga4(payload)["channels"][0].update(sessions=0)
    ga4(payload)["daily"][0].update(sessions=0)
    visual = vx.extract_ga4(payload)
    assert (visual.status, visual.sessions, visual.users, visual.engagement_rate) == ("available", 0, 0, Decimal("0.0"))
    assert visual.channels[0].sessions == 0 and visual.daily[0].sessions == 0


@pytest.mark.parametrize("missing", ["sessions", "total_users", "engagement_rate"])
def test_a_missing_ga4_total_stays_missing_and_the_visual_stays_available(payload, missing):
    ga4(payload)["site"][missing] = None
    visual = vx.extract_ga4(payload)
    field = {"total_users": "users"}.get(missing, missing)
    assert visual.status == "available" and getattr(visual, field) is None
    assert len(visual.channels) == 2 and len(visual.daily) == 28


def test_ga4_with_no_totals_but_channels_is_usable_and_with_nothing_is_unavailable(payload):
    ga4(payload)["site"].update(sessions=None, total_users=None, engagement_rate=None)
    visual = vx.extract_ga4(payload)
    assert visual.status == "available" and visual.sessions is None and visual.channels
    ga4(payload)["channels"] = []
    ga4(payload)["daily"] = []
    assert vx.extract_ga4(payload) == ri.Ga4Visuals.unavailable()


def test_incomplete_ga4_data_is_kept_and_marked(payload):
    mark(ga4(payload), "incomplete")
    visual = vx.extract_ga4(payload)
    assert visual.status == "incomplete" and visual.sessions == 90 and len(visual.daily) == 28


def test_ga4_empty_and_not_provided_are_unavailable(payload):
    changed = copy.deepcopy(payload)
    mark(ga4(changed), "empty")
    assert vx.extract_ga4(changed) == ri.Ga4Visuals.unavailable()
    payload["ga4"] = {"provided": False}
    assert vx.extract_ga4(payload) == ri.Ga4Visuals.unavailable()


def test_ga4_source_truncation_of_channels_and_daily_is_retained(payload):
    mark(ga4(payload), "truncated")
    ga4(payload)["source_truncated"] = ["landing_pages", "channels", "daily"]
    visual = vx.extract_ga4(payload)
    assert visual.status == "available"
    assert visual.truncation == (ri.TruncationNote("channels", "source_limit"), ri.TruncationNote("daily", "source_limit"))


def test_ga4_truncated_landing_pages_do_not_mark_the_visual_truncated(payload):
    mark(ga4(payload), "truncated")
    ga4(payload)["source_truncated"] = ["landing_pages"]
    assert vx.extract_ga4(payload).truncation == ()


def test_ga4_max_channels_reports_the_exact_number_left_out(payload):
    visual = vx.extract_ga4(payload, max_channels=1)
    assert [c.channel for c in visual.channels] == ["Direct"]
    assert visual.truncation == (ri.TruncationNote("channels", "row_limit", 1),)
    assert vx.extract_ga4(payload, max_channels=2).truncation == ()
    for bad in (0, True, "1"):
        with pytest.raises(ValueError):
            vx.extract_ga4(payload, max_channels=bad)


def test_ga4_empty_channels_and_trend_are_available_when_totals_exist(payload):
    ga4(payload)["channels"] = []
    ga4(payload)["daily"] = []
    visual = vx.extract_ga4(payload)
    assert visual.status == "available" and visual.channels == () and visual.daily == ()


def test_ga4_missing_days_are_omitted(payload):
    ga4(payload)["daily"] = [{"date": "2026-10-01", "has_data": False, "sessions": None, "organic_sessions": None},
                             {"date": "2026-10-02", "has_data": True, "sessions": 0, "organic_sessions": 0},
                             {"date": "2026-10-04", "has_data": True, "sessions": 4, "organic_sessions": 1}]
    assert [(p.day.day, p.sessions) for p in vx.extract_ga4(payload).daily] == [(2, 0), (4, 4)]


def test_ga4_unicode_channel_labels_are_preserved(payload):
    ga4(payload)["channels"] = [{"channel": "Référal — 日本", "sessions": 3, "total_users": 2,
                                 "engaged_sessions": 1}]
    assert vx.extract_ga4(payload).channels[0].channel == "Référal — 日本"


@pytest.mark.parametrize("path,value", [
    (("site", "sessions"), 1.5), (("site", "total_users"), True), (("site", "engagement_rate"), "0.3"),
    (("site", "engagement_rate"), float("nan")), (("site", "sessions"), -3)])
def test_malformed_ga4_totals_are_rejected(payload, path, value):
    ga4(payload)[path[0]][path[1]] = value
    with pytest.raises(vx.VisualExtractionError):
        vx.extract_ga4(payload)


@pytest.mark.parametrize("row", [{"channel": "", "sessions": 1, "total_users": 1, "engaged_sessions": 1},
                                 {"channel": "X", "sessions": None, "total_users": 1, "engaged_sessions": 1},
                                 {"channel": "X", "sessions": 1, "total_users": 1},
                                 {"channel": "X", "sessions": "1", "total_users": 1, "engaged_sessions": 1}])
def test_malformed_ga4_channel_rows_are_rejected(payload, row):
    ga4(payload)["channels"].append(row)
    with pytest.raises(vx.VisualExtractionError):
        vx.extract_ga4(payload)


def test_ga4_status_must_agree_with_issues(payload):
    ga4(payload)["issues"] = ["incomplete"]
    with pytest.raises(vx.VisualExtractionError) as caught:
        vx.extract_ga4(payload)
    assert caught.value.code == "status_contradicts_issues"


# --- purity, determinism, types ------------------------------------------------------------------------------------------------------

def test_extraction_does_not_mutate_the_input(payload):
    mark(gsc(payload), "incomplete", "truncated")
    gsc(payload)["source_truncated"] = ["country"]
    before = copy.deepcopy(payload)
    vx.extract_visuals(payload, max_countries=2, max_channels=1)
    assert payload == before


def test_extraction_is_deterministic(snapshot):
    results = [vx.extract_visuals(copy.deepcopy(snapshot.payload)) for _ in range(3)]
    assert results[0] == results[1] == results[2]
    assert json.dumps(dataclasses.asdict(results[0]), default=str) == json.dumps(dataclasses.asdict(results[2]), default=str)


def test_results_are_the_exact_r1_types_and_feed_a_render_input(snapshot):
    visuals = vx.extract_visuals(snapshot, max_countries=3)
    assert isinstance(visuals, vx.ExtractedVisuals)
    assert (type(visuals.search_console), type(visuals.geography), type(visuals.ga4)) == \
        (ri.SearchConsoleVisuals, ri.GeographyVisuals, ri.Ga4Visuals)
    from test_render_input import sample
    from seo_report import render_input_hash as rh
    value = dataclasses.replace(sample(), search_console=visuals.search_console, geography=visuals.geography,
                                ga4=visuals.ga4)
    assert re.fullmatch(r"[0-9a-f]{64}", rh.render_input_sha256(value))


def test_extracted_completeness_and_truncation_change_the_render_hash(snapshot):
    from test_render_input import sample
    from seo_report import render_input_hash as rh

    def digest(source, **options):
        v = vx.extract_visuals(source, **options)
        return rh.render_input_sha256(dataclasses.replace(sample(), search_console=v.search_console,
                                                          geography=v.geography, ga4=v.ga4))

    base = digest(snapshot.payload)
    incomplete = copy.deepcopy(snapshot.payload)
    mark(gsc(incomplete), "incomplete")
    partial = copy.deepcopy(snapshot.payload)
    gsc(partial)["totals"]["position"] = None
    capped = copy.deepcopy(snapshot.payload)
    gsc(capped)["capped"]["countries"] = True
    assert len({base, digest(incomplete), digest(partial), digest(capped), digest(snapshot.payload, max_countries=1)}) == 5


def test_extracted_visuals_are_immutable(snapshot):
    visuals = vx.extract_visuals(snapshot)
    with pytest.raises(dataclasses.FrozenInstanceError):
        visuals.ga4 = None
    with pytest.raises(dataclasses.FrozenInstanceError):
        visuals.search_console.clicks = 0
    assert type(visuals.search_console.daily) is tuple and type(visuals.geography.truncation) is tuple


def test_a_real_snapshot_without_sources_is_unavailable_for_all_three(snapshot):
    payload = copy.deepcopy(snapshot.payload)
    payload["gsc"] = {"provided": False}
    payload["ga4"] = {"provided": False}
    assert vx.extract_visuals(payload) == vx.ExtractedVisuals(*UNAVAILABLE)


def test_the_module_is_pure():
    source = (ROOT / "src" / "seo_report" / "visual_extraction.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for word in ("datetime.now", "date.today", "time.", "random", "uuid", "secrets", "os.environ", "open(", "Path(",
                 "requests", "urllib", "socket", "sqlite", "google", "write", "subprocess"):
        assert word not in code, word
