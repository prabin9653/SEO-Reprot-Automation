import dataclasses
import datetime as dt
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_engine as te  # shared synthetic fixtures (only helper functions are used)
from seo_report.insights import engine as eng
from seo_report.insights import recommendations as rc
from seo_report.periods import Period
from seo_report.snapshots import snapshot as snap
from seo_report.sources import ga4 as ga
from seo_report.sources import gsc as gs

UNSET = object()

# Values that must never appear in a snapshot or in the database.
FORBIDDEN_STRINGS = (
    "sender@example.test", "(555) 010-0199", "Sender Person", "Manager Person", "manager@example.test",
    "555-010-0123", "1SpreadsheetIdSecretValueXYZ", "topsecret-client-secret", "ya29.fake-access-token",
    "1//fake-refresh-token", "client_secret", "access_token", "refresh_token", "zz@example.test", "?x=1",
)
# Typical phone layouts such as (281) 555-0100 or +1 281 555 0100 (long decimals are not phone numbers).
_PHONE_LIKE = re.compile(r"(?:\+?\d{1,2}[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}")
_EMAIL_LIKE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def make_client(**overrides):
    values = dict(
        name="Example Co", website="https://example.test/", gsc_property="https://example.test/",
        ga4_property_id="123456789", status="Testing", target_country="United States", target_city="Dallas",
        # attributes a careless copy could leak: none of them may reach a snapshot
        sender=SimpleNamespace(name="Sender Person", email="sender@example.test", phone="(555) 010-0199"),
        project_manager="Manager Person", manager_email="manager@example.test", manager_phone="555-010-0123",
        spreadsheet_id="1SpreadsheetIdSecretValueXYZ", client_secret="topsecret-client-secret",
        access_token="ya29.fake-access-token", refresh_token="1//fake-refresh-token",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def build(client_key="example-co", gsc=UNSET, ga4=UNSET, keywords=None, client=None, config=None, periods=None):
    gsc = te.live_like_gsc() if gsc is UNSET else gsc
    ga4 = te.live_like_ga4() if ga4 is UNSET else ga4
    keywords = te.mixed_keywords() if keywords is None else keywords
    insights = eng.generate_insights(gsc=gsc, ga4=ga4, keywords=keywords, target_country="United States",
                                     recommendation_config=config or rc.RecommendationConfig())
    return snap.build_snapshot(client_key=client_key, client=client or make_client(), periods=periods or te.PERIODS,
                               gsc=gsc, ga4=ga4, keywords=keywords, insights=insights, recommendation_config=config)


def rich_data():
    """GSC and GA4 data that fills every table: daily rows, channels, warnings and diagnostics."""
    gsc = te.live_like_gsc()
    daily = [gs.DailyPoint(day, gs.Metrics(0, 70, 0.0, 30.0), True) for day in te.PERIODS.current.dates()]
    gsc_current = dataclasses.replace(
        gsc.current, daily=daily, latest_data_date=dt.date(2026, 10, 26), diagnostics={"query_clicks_diff": -5},
        warnings=[gs.DataWarning("incomplete_data", "Late data on the final day")])
    gsc = dataclasses.replace(gsc, current=gsc_current)
    ga4 = te.live_like_ga4()
    sessions = [ga.DailySessions(day, 3 + (1 if i < 7 else 0), 1, True) for i, day in enumerate(te.PERIODS.current.dates())]
    channels = [ga.ChannelRow("Direct", te.g4(67, 65, 40, 0.6)), ga.ChannelRow("Organic Search", te.g4(19, 13, 9, 0.474))]
    ga4_current = dataclasses.replace(
        ga4.current, daily=sessions, channels=channels, landing_pages=[te.landing("/", 50), te.landing("/about", 12)],
        time_zone="America/Los_Angeles", diagnostics={"daily_sessions_diff": 1},
        warnings=[ga.Ga4Warning("sampled", "Sampled report", "daily")])
    return gsc, dataclasses.replace(ga4, current=ga4_current)


def shifted(gsc, ga4, periods):
    """The same data relabelled for other reporting periods."""
    new_gsc = gs.SearchConsoleData(gsc.site_url, gsc.permission_level,
                                   dataclasses.replace(gsc.current, period=periods.current),
                                   dataclasses.replace(gsc.previous, period=periods.previous))
    new_ga4 = ga.Ga4Data(ga4.property_id, ga4.key_events_status,
                         dataclasses.replace(ga4.current, period=periods.current),
                         dataclasses.replace(ga4.previous, period=periods.previous))
    return new_gsc, new_ga4


def phone_like(text):
    return _PHONE_LIKE.findall(text)


# --- canonical form and hashing -----------------------------------------------------------------------------

def test_canonical_json_is_sorted_compact_and_rejects_nan():
    assert snap.canonical_json({"b": 1, "a": [1, (2, 3)], "c": "é"}) == '{"a":[1,[2,3]],"b":1,"c":"é"}'
    with pytest.raises(ValueError):
        snap.canonical_json({"x": float("nan")})


def test_hashes_are_sha256_of_the_canonical_text():
    assert snap.sha256_hex("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    payload = {"b": 1, "a": 2}
    assert snap.compute_content_hash(payload) == snap.sha256_hex('{"a":2,"b":1}')
    assert snap.compute_content_hash({"a": 2, "b": 1}) == snap.compute_content_hash(payload)


def test_chain_hash_depends_on_every_field():
    base = dict(seq=2, run_id="r2", client_key="c", period_start="2026-09-29", period_end="2026-10-26",
                revision=1, content_hash="a" * 64, created_at="2026-10-30T00:00:00+00:00")
    reference = snap.chain_hash("prev", **base)
    assert reference == snap.chain_hash("prev", **base) and len(reference) == 64
    assert snap.chain_hash("other", **base) != reference
    for name, value in (("seq", 3), ("run_id", "r3"), ("client_key", "d"), ("period_start", "2026-09-30"),
                        ("period_end", "2026-10-27"), ("revision", 2), ("content_hash", "b" * 64),
                        ("created_at", "2026-10-31T00:00:00+00:00")):
        assert snap.chain_hash("prev", **{**base, name: value}) != reference, name


def test_building_is_deterministic():
    first, second = build(), build()
    assert first == second and first.payload_json == second.payload_json
    assert re.fullmatch(r"[0-9a-f]{64}", first.content_hash)
    assert first.content_hash == snap.compute_content_hash(first.payload)


def test_content_hash_changes_when_the_data_changes():
    base = build()
    other_keywords = te.mixed_keywords() + [te.kw("Extra Keyword", "", 4, "Plano")]
    assert build(keywords=other_keywords).content_hash != base.content_hash
    assert build(client=make_client(name="Renamed Co")).content_hash != base.content_hash
    assert build(gsc=te.live_like_gsc(current={"totals": te.gm(7, 1552, 0.0045, 29.6)})).content_hash != base.content_hash


# --- whitelist and structure -----------------------------------------------------------------------------------

def test_payload_has_exactly_the_expected_top_level_blocks():
    payload = build().payload
    assert set(payload) == {"format", "settings", "client", "periods", "rankings", "gsc", "ga4", "output",
                            "diagnostics", "quality", "eligibility", "privacy"}
    assert payload["format"] == snap.FORMAT_VERSION and payload["settings"]["engine_version"] == snap.ENGINE_VERSION


def test_nested_blocks_contain_only_whitelisted_fields():
    payload = build().payload
    assert set(payload["client"]) == {"client_key", "name", "website", "gsc_property", "ga4_property_id",
                                      "status", "target_country", "target_city"}
    assert set(payload["rankings"]["keywords"][0]) == {"ordinal", "keyword", "target_location", "previous", "current"}
    assert set(payload["rankings"]["keywords"][0]["current"]) == {"kind", "position", "label"}
    gsc_period = payload["gsc"]["periods"]["current"]
    assert set(gsc_period) == {"status", "issues", "totals", "daily", "countries", "queries", "pages",
                               "latest_data_date", "warnings", "source_truncated", "dropped", "capped"}
    assert set(gsc_period["totals"]) == {"clicks", "impressions", "ctr", "position"}
    ga4_period = payload["ga4"]["periods"]["current"]
    assert set(ga4_period) == {"status", "issues", "time_zone", "site", "organic", "channels", "daily",
                               "landing_pages", "organic_landing_pages", "warnings", "source_truncated",
                               "dropped", "capped"}
    assert set(ga4_period["site"]) == set(snap.METRIC_FIELDS)
    section = payload["output"]["sections"][0]
    assert set(section) == {"key", "title", "status", "has_strength", "paragraphs", "omitted", "insights"}
    assert set(section["insights"][0]) == {"key", "tone", "priority", "text", "evidence", "tags"}
    assert set(payload["output"]["recommendations"][0]) == {"key", "kind", "priority", "title", "action", "trigger",
                                                            "evidence", "supporting"}
    assert set(payload["output"]["notes"][0]) == {"severity", "source", "category", "message"}
    assert set(payload["output"]) == {"sections", "recommendations", "omitted_recommendations", "notes",
                                      "validation_failures"}


def test_projection_rows_match_the_declared_columns():
    rows = snap.projection_rows(build().payload)
    assert set(rows) == set(snap.PROJECTION_COLUMNS)
    for table, table_rows in rows.items():
        assert all(len(row) == len(snap.PROJECTION_COLUMNS[table]) for row in table_rows), table
    assert len(rows["run_keywords"]) == 7 and len(rows["run_sections"]) == 4
    gsc, ga4 = rich_data()
    rich = snap.projection_rows(build(gsc=gsc, ga4=ga4).payload)
    assert all(rich[table] for table in snap.PROJECTION_COLUMNS)  # the rich data fills every table


def test_row_order_of_keywords_is_preserved():
    names = [k["keyword"] for k in build().payload["rankings"]["keywords"]]
    assert names == [k.keyword for k in te.mixed_keywords()]
    assert [k["ordinal"] for k in build().payload["rankings"]["keywords"]] == list(range(7))


# --- client key --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["example-client", "a", "client_1", "0abc", "x" * 63])
def test_valid_client_keys(key):
    assert build(client_key=key).client_key == key


@pytest.mark.parametrize("key", ["", "Example-Client", "-lead", "has space", "a/b", "é", "x" * 64, None, 5])
def test_invalid_client_keys_are_rejected(key):
    with pytest.raises(ValueError, match="client_key"):
        build(client_key=key)


def test_the_client_key_is_independent_of_the_display_name():
    renamed = build(client=make_client(name="A Completely Different Name"))
    original = build()
    assert renamed.client_key == original.client_key == renamed.payload["client"]["client_key"]
    assert renamed.payload["client"]["name"] == "A Completely Different Name"


# --- privacy -------------------------------------------------------------------------------------------------------

def test_no_secret_contact_or_unsafe_value_reaches_the_payload():
    text = build().payload_json
    for forbidden in FORBIDDEN_STRINGS:
        assert forbidden not in text, forbidden
    assert _EMAIL_LIKE.findall(text) == [] and phone_like(text) == []
    assert "sender" not in build().payload["client"] and "notes" not in build().payload["rankings"]["keywords"][0]


def test_free_text_is_masked_and_counted():
    tokenlike = "x" * 40
    keywords = [te.kw("Call jane@example.test or +1 281 555 0100 " + tokenlike, "", 3, "Plano")]
    payload = build(keywords=keywords).payload
    stored = payload["rankings"]["keywords"][0]["keyword"]
    assert "[email]" in stored and "[phone]" in stored and "[redacted]" in stored and "@" not in stored
    assert payload["privacy"]["masked_fields"] >= 1
    assert snap.mask_text("a@b.test 281 555 0100 " + tokenlike) == "[email] [phone] [redacted]"
    assert snap.mask_text(None) is None and snap.mask_text("plain text") == "plain text"


def test_unsafe_queries_and_pages_are_dropped_without_a_trace():
    payload = build().payload["gsc"]["periods"]["current"]
    labels = [q["label"] for q in payload["queries"]]
    assert "zz@example.test" not in labels and len(labels) == 4
    assert payload["dropped"] == {"queries": 1, "pages": 0}
    assert [p["path"] for p in payload["pages"]] == ["/", "/painting", "/old"]  # query string removed
    assert "zz@example.test" not in build().payload_json and "?x=1" not in build().payload_json


def test_unsafe_landing_pages_are_dropped_and_placeholders_kept():
    pages = [te.landing("/", 17), te.landing("/r/" + "a" * 30, 3), te.landing("(not set)", 2, True)]
    ga4 = te.live_like_ga4(current={"pages": pages})
    period = build(ga4=ga4).payload["ga4"]["periods"]["current"]
    assert [(p["path"], p["is_placeholder"]) for p in period["organic_landing_pages"]] == [("/", False),
                                                                                           ("(not set)", True)]
    assert period["dropped"]["organic_landing_pages"] == 1


def test_stored_lists_are_capped_and_sorted_deterministically():
    queries = [te.row(f"query {i:04d}", i % 7, 1000 - i, 5.0) for i in range(520)] + [te.row("bad@example.test", 99, 1, 5.0)]
    pages = [te.row(f"https://example.test/p{i:03d}", i % 5, i, 5.0) for i in range(210)]
    period = build(gsc=te.live_like_gsc(current={"queries": queries, "pages": pages})).payload["gsc"]["periods"]["current"]
    assert len(period["queries"]) == snap.MAX_STORED["queries"] and period["capped"]["queries"] is True
    assert len(period["pages"]) == snap.MAX_STORED["pages"] and period["capped"]["pages"] is True
    assert period["dropped"]["queries"] == 1 and period["capped"]["countries"] is False
    keys = [(-q["clicks"], -q["impressions"], q["label"]) for q in period["queries"]]
    assert keys == sorted(keys)
    assert build(gsc=te.live_like_gsc(current={"queries": queries, "pages": pages})) == \
        build(gsc=te.live_like_gsc(current={"queries": list(reversed(queries)), "pages": list(reversed(pages))}))


def test_unsupported_evidence_values_are_refused():
    sanitizer = snap._Sanitizer()
    with pytest.raises(TypeError):
        sanitizer.jsonable(object())
    with pytest.raises(ValueError):
        sanitizer.jsonable(float("inf"))
    assert sanitizer.jsonable({"a": (1, 2.5, None, True, dt.date(2026, 1, 2))}) == \
        {"a": [1, 2.5, None, True, "2026-01-02"]}


# --- missing values stay missing -------------------------------------------------------------------------------------------

def test_blank_rankings_are_missing_not_zero():
    payload = build(keywords=[te.kw("a", "", "", "Plano"), te.kw("b", "", "", "Frisco")]).payload
    assert payload["rankings"]["status"] == "missing"
    for entry in payload["rankings"]["keywords"]:
        assert entry["previous"] == {"kind": "missing", "position": None, "label": None}
        assert entry["current"] == {"kind": "missing", "position": None, "label": None}


def test_above_10_is_kept_verbatim_and_numeric_positions_are_derived():
    payload = build(keywords=[te.kw("a", "Page 2, Rank 6", "above 10"), te.kw("b", "ABOVE 10", 3)]).payload
    first, second = payload["rankings"]["keywords"]
    assert first["current"] == {"kind": "above_10", "position": None, "label": "above 10"}
    assert first["previous"] == {"kind": "position", "position": 16, "label": None}
    assert second["previous"] == {"kind": "above_10", "position": None, "label": "ABOVE 10"}


def test_empty_search_console_period_is_stored_as_no_data_not_zeros():
    empty = gs.PeriodData(te.PERIODS.current, gs.EMPTY_METRICS,
                          daily=[gs.DailyPoint(d, gs.EMPTY_METRICS, False) for d in te.PERIODS.current.dates()],
                          warnings=[gs.DataWarning("empty_period", "No data")])
    gsc = gs.SearchConsoleData("https://example.test/", "siteOwner", empty, te.live_like_gsc().previous)
    period = build(gsc=gsc).payload["gsc"]["periods"]["current"]
    assert period["status"] == "empty" and period["totals"] == {"clicks": None, "impressions": None, "ctr": None,
                                                                 "position": None}
    assert len(period["daily"]) == 28 and all(d["has_data"] is False and d["clicks"] is None for d in period["daily"])
    assert period["countries"] == period["queries"] == period["pages"] == []


def test_empty_ga4_period_and_unavailable_key_events_stay_null():
    empty = ga.Ga4PeriodData(te.PERIODS.current, ga.EMPTY_METRICS, ga.EMPTY_METRICS, is_empty=True,
                             daily=[ga.DailySessions(d, 0, 0, False) for d in te.PERIODS.current.dates()],
                             warnings=[ga.Ga4Warning("empty_period", "No data")])
    ga4 = ga.Ga4Data("123456789", "unavailable", empty, te.live_like_ga4().previous)
    period = build(ga4=ga4).payload["ga4"]["periods"]["current"]
    assert period["status"] == "empty" and all(v is None for v in period["site"].values())
    assert all(v is None for v in period["organic"].values())
    assert all(d["sessions"] is None and d["organic_sessions"] is None for d in period["daily"])
    unavailable = te.live_like_ga4(status="unavailable", current={"whole": te.g4(90, 82, 28, 0.311, None)})
    assert build(ga4=unavailable).payload["ga4"]["periods"]["current"]["site"]["key_events"] is None


# --- quality and baseline eligibility ------------------------------------------------------------------------------------------

def scenario(name):
    incomplete_gsc = te.live_like_gsc(current={"warnings": [gs.DataWarning("incomplete_data", "late")]})
    truncated_gsc = te.live_like_gsc(current={"warnings": [gs.DataWarning("truncated", "cut", "query")]})
    prev_empty_gsc = te.live_like_gsc()
    prev_empty_gsc = gs.SearchConsoleData(
        prev_empty_gsc.site_url, prev_empty_gsc.permission_level, prev_empty_gsc.current,
        gs.PeriodData(te.PERIODS.previous, gs.EMPTY_METRICS, warnings=[gs.DataWarning("empty_period", "none")]))
    return {
        "complete": {},
        "gsc_incomplete": {"gsc": incomplete_gsc},
        "gsc_truncated": {"gsc": truncated_gsc},
        "gsc_previous_empty": {"gsc": prev_empty_gsc},
        "ga4_incomplete": {"ga4": te.live_like_ga4(current={"warnings": [ga.Ga4Warning("incomplete_data", "late")]})},
        "ga4_truncated": {"ga4": te.live_like_ga4(current={"warnings": [ga.Ga4Warning("truncated", "cut", "landing_pages")]})},
        "gsc_missing": {"gsc": None},
        "ga4_missing": {"ga4": None},
        "rankings_blank": {"keywords": [te.kw("a"), te.kw("b")]},
        "rankings_partial": {"keywords": [te.kw("a", "", 2, "X"), te.kw("b", "", "", "Y")]},
    }[name]


@pytest.mark.parametrize("name, quality, eligibility", [
    ("complete", "complete", {"rankings": True, "gsc": True, "ga4": True}),
    ("gsc_incomplete", "partial", {"rankings": True, "gsc": False, "ga4": True}),
    ("gsc_truncated", "partial", {"rankings": True, "gsc": False, "ga4": True}),
    ("gsc_previous_empty", "partial", {"rankings": True, "gsc": True, "ga4": True}),
    ("ga4_incomplete", "partial", {"rankings": True, "gsc": True, "ga4": False}),
    ("ga4_truncated", "partial", {"rankings": True, "gsc": True, "ga4": False}),
    ("gsc_missing", "incomplete", {"rankings": True, "gsc": False, "ga4": True}),
    ("ga4_missing", "incomplete", {"rankings": True, "gsc": True, "ga4": False}),
    ("rankings_blank", "partial", {"rankings": False, "gsc": True, "ga4": True}),
    ("rankings_partial", "partial", {"rankings": True, "gsc": True, "ga4": True}),
])
def test_quality_and_eligibility(name, quality, eligibility):
    snapshot = build(**scenario(name))
    assert snapshot.quality == snapshot.payload["quality"]["level"] == quality
    assert snapshot.payload["eligibility"] == eligibility
    assert (quality == "complete") == (snapshot.payload["quality"]["reasons"] == [])


def test_empty_current_period_makes_a_run_incomplete_and_ineligible():
    empty = gs.PeriodData(te.PERIODS.current, gs.EMPTY_METRICS, warnings=[gs.DataWarning("empty_period", "none")])
    gsc = gs.SearchConsoleData("https://example.test/", "siteOwner", empty, te.live_like_gsc().previous)
    snapshot = build(gsc=gsc)
    assert snapshot.quality == "incomplete" and snapshot.payload["eligibility"]["gsc"] is False
    assert any("no data for the current period" in r for r in snapshot.payload["quality"]["reasons"])


def test_period_statuses_and_truncated_sources_are_recorded():
    truncated = build(**scenario("gsc_truncated")).payload["gsc"]["periods"]["current"]
    assert truncated["status"] == "truncated" and truncated["source_truncated"] == ["query"]
    assert truncated["issues"] == ["truncated"]
    ga4_truncated = build(**scenario("ga4_truncated")).payload["ga4"]["periods"]["current"]
    assert ga4_truncated["status"] == "truncated" and ga4_truncated["source_truncated"] == ["landing_pages"]


# --- diagnostics ---------------------------------------------------------------------------------------------------------------------

def test_the_ga4_daily_session_discrepancy_is_preserved_not_reconciled():
    gsc, ga4 = rich_data()
    payload = build(gsc=gsc, ga4=ga4).payload
    ga4_current = payload["ga4"]["periods"]["current"]
    assert ga4_current["site"]["sessions"] == 90  # the validated total
    assert sum(d["sessions"] for d in ga4_current["daily"]) == 91  # the daily series as fetched
    assert payload["diagnostics"]["ga4"]["current"] == {"daily_sessions_diff": 1}
    assert payload["diagnostics"]["gsc"]["current"] == {"query_clicks_diff": -5}
    evidence_names = {name for s in payload["output"]["sections"] for i in s["insights"] for name in i["evidence"]}
    assert not any(name.endswith("_diff") for name in evidence_names)
    assert any("+1" in n["message"] and "not adjusted" in n["message"] for n in payload["output"]["notes"])


# --- input checks ------------------------------------------------------------------------------------------------------------------------

def test_data_for_other_periods_is_rejected():
    other = te.live_like_gsc()
    shifted_gsc = gs.SearchConsoleData(other.site_url, other.permission_level,
                                       dataclasses.replace(other.current, period=Period(dt.date(2026, 9, 30), dt.date(2026, 10, 27))),
                                       other.previous)
    insights = eng.generate_insights(gsc=other, keywords=te.mixed_keywords())
    with pytest.raises(ValueError, match="does not cover the reporting periods"):
        snap.build_snapshot(client_key="example-co", client=make_client(), periods=te.PERIODS, gsc=shifted_gsc,
                            ga4=None, keywords=te.mixed_keywords(), insights=insights)


def test_recommendation_settings_and_thresholds_are_recorded():
    config = rc.RecommendationConfig(standing=("technical", "backlinks"), target_city="Dallas")
    settings = build(config=config).payload["settings"]
    assert settings["recommendations"] == {"standing": ["backlinks", "technical"], "target_city": "Dallas",
                                           "max_items": 5}
    assert settings["thresholds"]["small_base_count"] == 20
    assert build().payload["settings"]["recommendations"] is None


def test_snapshot_module_is_pure():
    source = (ROOT / "src" / "seo_report" / "snapshots" / "snapshot.py").read_text(encoding="utf-8")
    for forbidden in ("import sqlite3", "import datetime", "import time", "import uuid", "import random", "import os",
                      "open(", "print(", "googleapiclient", "seo_report.sources", "seo_report.clients",
                      "seo_report.auth", "import requests"):
        assert forbidden not in source
