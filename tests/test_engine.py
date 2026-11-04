import dataclasses
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import metrics as mt
from seo_report.insights import engine as eng
from seo_report.insights import recommendations as rc
from seo_report.insights import rules as rl
from seo_report.insights import wording as w
from seo_report.insights.rules import Tone
from seo_report.periods import report_periods
from seo_report.rankings import parse_ranking
from seo_report.sources import ga4 as ga
from seo_report.sources import gsc as gs

PERIODS = report_periods(dt.date(2026, 9, 29), dt.date(2026, 10, 26))
CITY = "Dallas–Fort Worth Metroplex"


# --- synthetic fixtures ---------------------------------------------------------------------------------

def kw(name, previous="", current="", location=""):
    return SimpleNamespace(keyword=name, previous=parse_ranking(previous), current=parse_ranking(current),
                           target_location=location)


def mixed_keywords():
    return [
        kw("Concrete Patio Wiley", 3, 1, "Wiley"), kw("Fence Repair Cedar Hill", 5, 2, "Cedar Hill"),
        kw("Siding Install", 4, 3, "Glenn Heights"), kw("Gutter Repair", 9, 6, "Waxahachie"),
        kw("Interior Painting", 12, "Page 2, Rank 6", "Duncanville"),
        kw("Roof Repair Arlington", "", "Above 10", "Arlington"), kw("Roof Install", 8, "Above 10", "Fort Worth"),
    ]


def gm(clicks, impressions, ctr, position):
    return gs.Metrics(clicks, impressions, ctr, position)


def row(key, clicks, impressions, position=None, label=None):
    return gs.DimensionRow(key, label or key, gm(clicks, impressions, None, position))


def gsc_period(period, totals, *, countries=(), queries=(), pages=(), warnings=(), diagnostics=None):
    return gs.PeriodData(period, totals, countries=list(countries), queries=list(queries), pages=list(pages),
                         warnings=list(warnings), diagnostics=diagnostics or {})


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
    return gs.SearchConsoleData("https://example.test/", "siteOwner", gsc_period(PERIODS.current, **cur),
                                gsc_period(PERIODS.previous, **prev))


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


def generate(**kwargs):
    kwargs.setdefault("gsc", live_like_gsc())
    kwargs.setdefault("ga4", live_like_ga4())
    kwargs.setdefault("keywords", mixed_keywords())
    kwargs.setdefault("target_country", "United States")
    return eng.generate_insights(**kwargs)


def texts(section):
    return " ".join(section.paragraphs)


def note_messages(result, **match):
    return [n.message for n in result.notes if all(getattr(n, k) == v for k, v in match.items())]


def rec_keys(result):
    return [r.key for r in result.recommendations]


# --- the complete engine ---------------------------------------------------------------------------------------

def test_complete_output_has_the_four_sections_in_order():
    result = generate()
    assert [s.key for s in result.sections] == ["keywords", "search_console", "geography", "ga4"]
    assert [s.title for s in result.sections] == ["Keyword Rankings", "Google Search Console",
                                                  "Geographical Performance", "Google Analytics 4"]
    assert all(s.status == "ok" and s.has_strength for s in result.sections)
    assert [len(s.paragraphs) for s in result.sections] == [2, 2, 1, 2]
    assert result.validation_failures == ()
    assert result.section("ga4").title == "Google Analytics 4"


def test_keyword_section_text():
    section = generate().section("keywords")
    assert section.paragraphs == (
        "The website ranks #1 for “Concrete Patio Wiley”. 2 other tracked keywords rank in the top 3. Overall, 4 of "
        "the 7 tracked keywords rank on page 1. 4 tracked keywords have moved up since the previous ranking check, "
        "led by “Fence Repair Cedar Hill” (from position 5 to position 2). 1 tracked keyword has moved down since "
        "the previous ranking check, led by “Interior Painting” (from position 12 to position 16).",
        "2 tracked keywords are currently labelled “Above 10”, including “Roof Repair Arlington” and “Roof Install”, "
        "which leaves room to build visibility for those searches.")


def test_search_console_section_text_and_limits():
    section = generate().section("search_console")
    assert section.paragraphs[0] == (
        "The website earned 6 organic clicks from Google Search during this 28-day period, up from 4 in the previous "
        "28 days. The click-through rate was 0.39%, up from 0.18% in the previous 28 days. 3 new search queries "
        "appeared in Google Search compared with the previous 28 days, including “new roofer” and “far away”. "
        "Search impressions totaled 1.55K during this 28-day period, down 31.8% from 2.28K in the previous 28 days. "
        "The average search position was 29.6, essentially unchanged from 29.1 in the previous 28 days.")
    assert section.paragraphs[1].startswith("3 search queries are showing in positions 4–20 of Google Search")
    assert [i.key for i in section.insights][:3] == ["gsc.clicks", "gsc.ctr", "gsc.new_queries"]  # strengths first
    assert section.omitted == ("gsc.top_page",)  # the five-sentence limit
    assert "zz@example.test" not in texts(section) and "?x=1" not in texts(section)


def test_geography_and_ga4_section_text():
    result = generate()
    assert result.section("geography").paragraphs == (
        "United States accounted for 95.3% of search impressions (1,479 impressions) and 3 clicks during this 28-day "
        "period, so search visibility is concentrated in the target market. The website also recorded clicks from "
        "several other countries, including Pakistan and Mexico, while search impressions were recorded across 4 countries.",)
    ga4_section = result.section("ga4")
    assert ga4_section.paragraphs[0].startswith(
        "The website recorded 90 sessions during this 28-day period, up 40.6% from 64 in the previous 28 days.")
    assert ga4_section.paragraphs[0].endswith(
        "The site-wide engagement rate was 31.1%, down from 54.7% in the previous 28 days.")
    assert ga4_section.paragraphs[1] == (
        "17 of the 19 Organic Search sessions entered through “/”, so organic traffic depends heavily on a single "
        "page. No key events were recorded during this 28-day period or the previous 28 days, so the report cannot "
        "yet show how many visits lead to inquiries.")


def test_strengths_come_first_and_opportunities_second():
    result = generate()
    for section in result.sections:
        by_text = {i.text: i for i in section.insights}
        first = [by_text[t] for t in _sentences(section.paragraphs[0], by_text)]
        assert first[0].tone is Tone.POSITIVE
        assert all(i.tone is not Tone.OPPORTUNITY for i in first)
        if len(section.paragraphs) == 2:
            assert all(by_text[t].tone is Tone.OPPORTUNITY for t in _sentences(section.paragraphs[1], by_text))


def _sentences(paragraph, by_text):
    """The insight texts, in order, that make up a paragraph."""
    found, rest = [], paragraph
    while rest:
        match = next(t for t in by_text if rest.startswith(t))
        found.append(match)
        rest = rest[len(match):].lstrip()
    return found


def test_balanced_wording_does_not_invent_strengths():
    declining = live_like_ga4(
        current={"whole": g4(40, 30, 10, 0.25), "organic": g4(5, 4, 2, 0.4), "pages": [landing("/", 5)]},
        previous={"whole": g4(80, 60, 50, 0.625), "organic": g4(8, 7, 5, 0.6), "pages": [landing("/", 8)]})
    result = generate(ga4=declining, gsc=None, keywords=[])
    section = result.section("ga4")
    assert section.has_strength is False
    assert not any(i.tone is Tone.POSITIVE for i in section.insights)
    assert "down 50%" in section.paragraphs[0] and w.find_growth_words(section.paragraphs[0]) == []


# --- sections independently -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, available", [
    ({"gsc": None, "ga4": None}, {"keywords"}),
    ({"ga4": None, "keywords": []}, {"search_console", "geography"}),
    ({"gsc": None, "keywords": []}, {"ga4"}),
    ({"gsc": None, "ga4": None, "keywords": []}, set()),
])
def test_each_section_depends_only_on_its_own_source(kwargs, available):
    result = generate(**kwargs)
    assert {s.key for s in result.sections if s.status == "ok"} == available
    for section in result.sections:
        if section.status == "unavailable":
            assert section.paragraphs == () and section.insights == () and not section.has_strength
            assert any(section.title in m and "no verified insights" in m for m in note_messages(result))


def test_missing_sources_are_reported_and_suppress_their_recommendations():
    result = generate(gsc=None)
    assert any("Search Console data was not provided" in m for m in note_messages(result, source="gsc"))
    assert result.section("search_console").status == "unavailable"
    assert result.section("geography").status == "unavailable"
    assert result.section("keywords").status == "ok" and result.section("ga4").status == "ok"
    assert "improve_ctr" not in rec_keys(result)
    no_ga4 = generate(ga4=None)
    assert any("GA4 data was not provided" in m for m in note_messages(no_ga4, source="ga4"))
    assert not {"broaden_landing_pages", "review_key_events"} & set(rec_keys(no_ga4))


def test_gsc_average_position_never_substitutes_for_manual_rankings():
    blank = [kw("a", "", "", "X"), kw("b", "", "", "Y")]
    result = generate(keywords=blank)
    keywords = result.section("keywords")
    assert keywords.status == "unavailable" and "29.6" not in texts(keywords)
    assert "29.6" in texts(result.section("search_console"))
    assert "Search Console" not in texts(result.section("keywords"))
    with_rankings = generate()
    assert "29.6" not in texts(with_rankings.section("keywords"))
    assert "average" not in texts(with_rankings.section("keywords")).lower()
    for section in with_rankings.sections:
        prefixes = {i.key.split(".")[0] for i in section.insights}
        assert prefixes == {eng.SECTION_PREFIX[section.key]}


def test_ga4_sessions_are_never_described_as_search_console_clicks():
    result = generate()
    assert "clicks" not in texts(result.section("ga4")) and "impressions" not in texts(result.section("ga4"))
    assert "sessions" not in texts(result.section("search_console"))


# --- blank rankings and “Above 10” ----------------------------------------------------------------------------------------

def test_blank_rankings_suppress_keyword_claims_and_add_a_note():
    result = generate(keywords=[kw("a", "", "", "Plano"), kw("b", "", "", "Frisco")], recommendation_config=
                      rc.RecommendationConfig(target_city=CITY))
    keywords = result.section("keywords")
    assert keywords.status == "unavailable" and keywords.insights == ()
    assert any("not recorded for any of the 2 tracked keywords" in m for m in note_messages(result, source="rankings"))
    assert result.metrics.rankings.all_current_missing
    assert {"local_seo", "review_declining_keywords"}.isdisjoint(rec_keys(result))
    assert result.section("search_console").status == "ok"


def test_above_10_never_creates_top_10_or_movement_claims():
    result = generate(keywords=[kw("Roof Repair", 9, "Above 10", "Arlington"), kw("Roof Install", "Above 10", 7)])
    text = texts(result.section("keywords")).lower()
    assert "labelled “above 10”" in text
    for phrase in ("top 10", "entered", "left the", "dropped", "outside", "moved up", "moved down", "fell"):
        assert phrase not in text
    assert result.metrics.rankings.comparable == 0


def test_only_above_10_keywords_give_an_opportunity_but_no_strength():
    result = generate(keywords=[kw("Roof Repair", "", "Above 10", "Arlington")])
    section = result.section("keywords")
    assert section.has_strength is False and len(section.paragraphs) == 1
    assert [i.tone for i in section.insights] == [Tone.OPPORTUNITY]


# --- local SEO recommendation and original keyword rows -----------------------------------------------------------------------

def test_original_keyword_rows_reach_the_recommendations(monkeypatch):
    captured = {}
    real = eng.generate_recommendations

    def spy(insights, metrics, config, keywords):
        captured.update(insights=list(insights), keywords=keywords, config=config)
        return real(insights, metrics, config, keywords)

    monkeypatch.setattr(eng, "generate_recommendations", spy)
    keywords = mixed_keywords()
    config = rc.RecommendationConfig(target_city=CITY)
    generate(keywords=keywords, recommendation_config=config)
    assert list(captured["keywords"]) == keywords and all(a is b for a, b in zip(captured["keywords"], keywords))
    assert captured["config"] is config


def test_local_seo_uses_actual_target_locations():
    result = generate(recommendation_config=rc.RecommendationConfig(target_city=CITY))
    local = next(r for r in result.recommendations if r.key == "local_seo")
    assert local.trigger == ("Selected because 2 tracked keywords labelled “Above 10” have target locations: "
                             "“Arlington” and “Fort Worth”.")
    assert local.evidence["local_keywords.location_names"] == ("Arlington", "Fort Worth")
    assert "“Dallas–Fort Worth Metroplex”" in local.action


def test_local_seo_is_suppressed_without_target_locations_or_keyword_rows():
    general = [kw("Roof Repair", "", "Above 10"), kw("Roof Install", 8, "Above 10")]
    result = generate(keywords=general)
    assert "local_seo" not in rec_keys(result)
    assert any("printable target location" in m for m in note_messages(result, source="recommendations"))
    no_rows = generate(keywords=[])
    assert "local_seo" not in rec_keys(no_rows)
    assert no_rows.section("keywords").status == "unavailable"
    missing_attribute = SimpleNamespace(keyword="Roof Repair", previous=parse_ranking(""),
                                        current=parse_ranking("Above 10"))
    assert "local_seo" not in rec_keys(generate(keywords=[missing_attribute]))


# --- incomplete, truncated and empty data ------------------------------------------------------------------------------------------

def test_incomplete_search_console_data_keeps_the_warning_and_drops_its_recommendations():
    warning = gs.DataWarning("incomplete_data", "No data on the final day")
    result = generate(gsc=live_like_gsc(current={"warnings": [warning]}))
    assert any("GSC current period: No data on the final day" in m for m in note_messages(result))
    assert any("may be incomplete" in m for m in note_messages(result, source="gsc"))
    assert {"accelerate_positions", "improve_ctr"} .isdisjoint(
        {r.key for r in result.recommendations if r.supporting and r.supporting[0].startswith("gsc.")})
    assert "improve_ctr" not in rec_keys(result)
    assert result.section("search_console").status == "ok"  # the section text itself is kept


def test_incomplete_ga4_data_keeps_the_warning_and_drops_its_recommendations():
    warning = ga.Ga4Warning("incomplete_data", "No sessions on the final day")
    result = generate(ga4=live_like_ga4(current={"warnings": [warning]}))
    assert any("GA4 current period: No sessions on the final day" in m for m in note_messages(result))
    assert not {"broaden_landing_pages", "improve_engagement", "review_key_events"} & set(rec_keys(result))
    assert "improve_ctr" in rec_keys(result)


def test_truncated_query_list_suppresses_only_query_insights():
    warning = gs.DataWarning("truncated", "cut", "query")
    result = generate(gsc=live_like_gsc(current={"warnings": [warning]}))
    keys = {i.key for i in result.section("search_console").insights}
    assert not {"gsc.new_queries", "gsc.striking_distance", "gsc.zero_click_queries"} & keys
    assert {"gsc.clicks", "gsc.position", "gsc.zero_click_pages", "gsc.top_page"} <= keys
    assert any("query list was truncated" in m for m in note_messages(result))
    assert "search quer" not in texts(result.section("search_console"))


def test_truncated_country_list_suppresses_only_the_target_market_claim():
    warning = gs.DataWarning("truncated", "cut", "country")
    result = generate(gsc=live_like_gsc(current={"warnings": [warning]}), target_country="Narnia")
    assert "geo.target_market" not in {i.key for i in result.section("geography").insights}
    assert any("truncated" in m for m in note_messages(result, source="geography"))


def test_empty_and_comparison_free_periods():
    empty = gs.PeriodData(PERIODS.previous, gs.EMPTY_METRICS, warnings=[gs.DataWarning("empty_period", "none")])
    base = live_like_gsc()
    no_previous = gs.SearchConsoleData("https://example.test/", "siteOwner", base.current, empty)
    result = generate(gsc=no_previous)
    section = result.section("search_console")
    assert section.status == "ok" and "previous" not in texts(section)
    assert any("previous period" in m for m in note_messages(result, source="gsc"))
    empty_current = gs.PeriodData(PERIODS.current, gs.EMPTY_METRICS, warnings=[gs.DataWarning("empty_period", "none")])
    none_now = gs.SearchConsoleData("https://example.test/", "siteOwner", empty_current, base.previous)
    unavailable = generate(gsc=none_now)
    assert unavailable.section("search_console").status == "unavailable"
    assert unavailable.section("geography").status == "unavailable"


# --- recommendations: limits and ordering ----------------------------------------------------------------------------------------------

def test_recommendations_are_capped_and_ordered():
    result = generate(recommendation_config=rc.RecommendationConfig(target_city=CITY, standing=("backlinks", "technical")))
    assert rec_keys(result) == ["accelerate_positions", "improve_ctr", "review_declining_keywords", "local_seo",
                                "broaden_landing_pages"]
    assert result.omitted_recommendations == ("improve_engagement", "review_key_events", "standing.backlinks",
                                              "standing.technical")
    assert len(result.recommendations) <= rc.MAX_RECOMMENDATIONS
    assert [r.priority for r in result.recommendations] == sorted(r.priority for r in result.recommendations)


def test_standing_recommendations_are_disabled_by_default_and_come_last_when_enabled():
    assert not any(k.startswith("standing.") for k in rec_keys(generate(gsc=None, keywords=[])))
    config = rc.RecommendationConfig(standing=("technical", "backlinks"))
    result = generate(gsc=None, keywords=[], recommendation_config=config)
    assert rec_keys(result) == ["broaden_landing_pages", "improve_engagement", "review_key_events",
                                "standing.backlinks", "standing.technical"]
    assert [r.kind for r in result.recommendations] == ["evidence"] * 3 + ["standing"] * 2


def test_key_event_recommendation_follows_the_none_recorded_status():
    assert "review_key_events" in rec_keys(generate(gsc=None, keywords=[]))
    recorded = live_like_ga4(status="recorded", current={"whole": g4(90, 82, 28, 0.311, 0)},
                             previous={"whole": g4(64, 58, 35, 0.547, 4)})
    result = generate(ga4=recorded, recommendation_config=rc.RecommendationConfig(max_items=5))
    assert "review_key_events" not in [r.key for r in result.recommendations] + list(result.omitted_recommendations)


# --- diagnostics -----------------------------------------------------------------------------------------------------------------------------

def test_ga4_daily_session_discrepancy_is_preserved_as_a_diagnostic_only():
    ga4_data = live_like_ga4(current={"diagnostics": {"daily_sessions_diff": 1, "channel_sessions_diff": 0}})
    result = generate(ga4=ga4_data)
    assert result.diagnostics["ga4"]["current"] == {"daily_sessions_diff": 1, "channel_sessions_diff": 0}
    assert result.metrics.ga4.sessions.current == 90  # the validated total is untouched
    (message,) = note_messages(result, category="diagnostic", source="ga4")
    assert "+1" in message and "not adjusted" in message
    client = " ".join(result.client_texts)
    assert "91" not in client and "daily" not in client.lower() and "differ" not in client.lower()
    assert not any(name.endswith("_diff") for i in result.insights for name in i.evidence)
    assert "90 sessions" in texts(result.section("ga4"))


def test_search_console_diagnostics_are_preserved_and_stay_out_of_client_text():
    diagnostics = {"query_clicks_diff": -5, "query_impressions_diff": -358, "page_impressions_diff": 108}
    result = generate(gsc=live_like_gsc(current={"diagnostics": diagnostics}))
    assert result.diagnostics["gsc"]["current"] == diagnostics
    assert any("anonymized" in m for m in note_messages(result, category="diagnostic"))
    assert "358" not in " ".join(result.client_texts) and "108" not in " ".join(result.client_texts)


def test_diagnostic_evidence_is_rejected_by_validation():
    result = generate()
    insight = next(i for i in result.insights if i.key == "ga4.sessions")
    tampered = dataclasses.replace(insight, evidence={**insight.evidence, "daily_sessions_diff": 1})
    assert "diagnostic differences" in eng.validate_insight(tampered, result.metrics)


# --- evidence traceability and validation -----------------------------------------------------------------------------------------------------

def test_every_numeric_claim_is_traceable_to_structured_evidence():
    result = generate(recommendation_config=rc.RecommendationConfig(target_city=CITY))
    for section in result.sections:
        allowed = set()
        for insight in section.insights:
            assert rl.untraced_numbers(insight) == [], insight.key
            allowed |= rl.evidence_tokens(insight.evidence)
        for paragraph in section.paragraphs:
            assert [n for n in w.numbers_in(paragraph) if n not in allowed] == []
    for rec in result.recommendations:
        assert rc.untraced_recommendation_numbers(rec) == [], rec.key


def test_all_real_insights_validate_against_the_metrics():
    result = generate()
    for insight in rl.run_rules(result.metrics).insights:
        assert eng.validate_insight(insight, result.metrics) is None, insight.key
    assert set(eng.AUTHORITY) >= {i.key for i in rl.run_rules(result.metrics).insights}


@pytest.fixture
def base():
    result = generate()
    return result.metrics, {i.key: i for i in rl.run_rules(result.metrics).insights}


def test_validation_rejects_wrong_direction_wording(base):
    metrics, insights = base
    clicks = insights["gsc.clicks"]
    wrong = dataclasses.replace(clicks, text=clicks.text.replace("up from 4", "down from 4"))
    assert "does not match the metric's direction" in eng.validate_insight(wrong, metrics)
    flat = dataclasses.replace(clicks, text=clicks.text.replace("up from 4", "unchanged from 4"))
    assert "direction" in eng.validate_insight(flat, metrics)
    position = insights["gsc.position"]
    improved = dataclasses.replace(position, text=position.text.replace("essentially unchanged from", "improved from"))
    assert "direction" in eng.validate_insight(improved, metrics)


def test_validation_rejects_a_comparison_the_metric_cannot_support(base):
    metrics, insights = base
    thin = mt.build_report_metrics(gsc=live_like_gsc(previous={"totals": gm(1, 300, 0.0033, 29.1)}))
    ctr = rl.run_rules(thin).insights
    real = next(i for i in ctr if i.key == "gsc.ctr")
    assert "previous" not in real.text  # the rules do not claim a comparison here
    claimed = dataclasses.replace(real, text=real.text.rstrip(".") + ", up from 0.18% in the previous 28 days.")
    assert "not comparable" in eng.validate_insight(claimed, thin)


def test_validation_rejects_the_wrong_comparison_period(base):
    metrics, insights = base
    sessions = insights["ga4.sessions"]
    wrong = dataclasses.replace(sessions, text=sessions.text.replace("previous 28 days", "previous 14 days"))
    assert "other than the approved 28 days" in eng.validate_insight(wrong, metrics)
    bad_evidence = dataclasses.replace(sessions, evidence={**sessions.evidence, "period_days": 14})
    assert eng.validate_insight(bad_evidence, metrics) is not None


def test_validation_rejects_evidence_from_the_wrong_metric_or_period(base):
    metrics, insights = base
    sessions, users = insights["ga4.sessions"], insights["ga4.users"]
    wrong_metric = dataclasses.replace(sessions, evidence=dict(users.evidence))
    assert "does not match the metrics" in eng.validate_insight(wrong_metric, metrics)
    swapped = dataclasses.replace(sessions, evidence={**sessions.evidence, "current": sessions.evidence["previous"],
                                                      "previous": sessions.evidence["current"]})
    assert "does not match the metrics" in eng.validate_insight(swapped, metrics)
    stale = dataclasses.replace(insights["keywords.top_positions"],
                                evidence={**insights["keywords.top_positions"].evidence, "at_1": 5})
    assert "'at_1'" in eng.validate_insight(stale, metrics)


def test_validation_rejects_text_that_mixes_sources(base):
    metrics, insights = base
    clicks = dataclasses.replace(insights["gsc.clicks"], text="The website earned 6 sessions in this 28-day period.")
    assert "another source" in eng.validate_insight(clicks, metrics)
    keyword = dataclasses.replace(insights["keywords.above_10"], text=insights["keywords.above_10"].text.replace(
        "labelled", "labelled in Search Console as"))
    assert "another source" in eng.validate_insight(keyword, metrics)
    quoted_name = dataclasses.replace(insights["keywords.above_10"], text=insights["keywords.above_10"].text.replace(
        "“Roof Install”", "“Impressions Roof”"))
    assert eng.validate_insight(quoted_name, metrics) is None  # quoted keyword names are labels, not claims


def test_validation_rejects_unsupported_positive_tones_and_unknown_insights(base):
    metrics, insights = base
    assert "positive statement" in eng.validate_insight(
        dataclasses.replace(insights["gsc.impressions"], tone=Tone.POSITIVE), metrics)
    assert "positive statement" in eng.validate_insight(
        dataclasses.replace(insights["ga4.engagement"], tone=Tone.POSITIVE), metrics)
    unknown = dataclasses.replace(insights["gsc.clicks"], key="gsc.invented")
    assert "no authoritative evidence" in eng.validate_insight(unknown, metrics)
    misfiled = dataclasses.replace(insights["gsc.clicks"], section="ga4")
    assert "does not belong" in eng.validate_insight(misfiled, metrics)
    untraced = dataclasses.replace(insights["gsc.clicks"], text=insights["gsc.clicks"].text.rstrip(".") + " from 99 pages.")
    assert "not traceable" in eng.validate_insight(untraced, metrics)


def test_validation_requires_the_underlying_data():
    insights = rl.run_rules(generate().metrics).insights
    no_sources = mt.build_report_metrics(keywords=mixed_keywords())
    clicks = next(i for i in insights if i.key == "gsc.clicks")
    assert eng.validate_insight(clicks, no_sources) == "the underlying data is not available"


def test_the_engine_leaves_out_insights_that_fail_validation(monkeypatch):
    real = rl.run_rules

    def tampered(metrics):
        output = real(metrics)
        changed = []
        for insight in output.insights:
            if insight.key == "gsc.position_opportunity":
                insight = dataclasses.replace(insight, evidence={"position": 12.3})
            changed.append(insight)
        return rl.RuleOutput(tuple(changed), output.notes)

    monkeypatch.setattr(eng.rl, "run_rules", tampered)
    result = generate()
    assert ("gsc.position_opportunity", "evidence 'position' does not match the metrics") in result.validation_failures
    assert "gsc.position_opportunity" not in {i.key for s in result.sections for i in s.insights}
    assert "29.6 leaves clear room" not in texts(result.section("search_console"))
    assert any("Insight gsc.position_opportunity was left out" in m for m in note_messages(result, source="engine"))
    accelerate = next(r for r in result.recommendations if r.key == "accelerate_positions")
    assert "gsc.position_opportunity" not in accelerate.supporting  # an invalid insight cannot trigger anything


def test_the_engine_leaves_out_recommendations_with_untraceable_numbers(monkeypatch):
    real = eng.generate_recommendations

    def tampered(insights, metrics, config, keywords):
        result = real(insights, metrics, config, keywords)
        first = dataclasses.replace(result.recommendations[0], action="Fix 99 pages.")
        return rc.RecommendationResult((first,) + result.recommendations[1:], result.omitted, result.notes)

    monkeypatch.setattr(eng, "generate_recommendations", tampered)
    result = generate()
    assert result.recommendations[0].key != "accelerate_positions"
    assert ("accelerate_positions", "numbers are not traceable to its evidence") in result.validation_failures


# --- paragraph limits -------------------------------------------------------------------------------------------------------------------------------

def synthetic(key, priority, tone=Tone.NEUTRAL, words=12):
    text = " ".join(["word"] * (words - 1) + ["end."])
    return rl.Insight("ga4", key, text, tone, priority, {})


def test_paragraph_limits_and_order_are_enforced():
    insights = [synthetic(f"ga4.n{i}", i) for i in range(8)] + [synthetic(f"ga4.o{i}", 10 + i, Tone.OPPORTUNITY)
                                                                  for i in range(8)]
    section = eng._compose("ga4", insights)
    assert len(section.paragraphs) <= eng.MAX_PARAGRAPHS
    for paragraph in section.paragraphs:
        assert paragraph.count("end.") <= eng.MAX_SENTENCES and w.word_count(paragraph) <= eng.MAX_WORDS
    assert [i.key for i in section.insights] == [f"ga4.n{i}" for i in range(5)] + [f"ga4.o{i}" for i in range(5)]
    assert section.omitted == tuple(f"ga4.n{i}" for i in range(5, 8)) + tuple(f"ga4.o{i}" for i in range(5, 8))


def test_the_word_limit_skips_long_sentences_but_never_the_first():
    long = synthetic("ga4.long", 1, words=200)
    short = synthetic("ga4.short", 2, words=10)
    section = eng._compose("ga4", [long, short])
    assert [i.key for i in section.insights] == ["ga4.long"]  # first sentence kept even though it exceeds the limit
    assert section.omitted == ("ga4.short",)
    skipped = eng._compose("ga4", [synthetic("ga4.a", 1, words=100), synthetic("ga4.b", 2, words=60),
                                   synthetic("ga4.c", 3, words=20)])
    assert [i.key for i in skipped.insights] == ["ga4.a", "ga4.c"] and skipped.omitted == ("ga4.b",)


def test_ordering_is_deterministic_for_equal_priorities():
    a, b = synthetic("ga4.b", 1), synthetic("ga4.a", 1)
    assert [i.key for i in eng._compose("ga4", [a, b]).insights] == ["ga4.a", "ga4.b"]
    assert eng._compose("ga4", [a, b]) == eng._compose("ga4", [b, a])


def test_omissions_by_the_limits_are_reported_to_the_reviewer():
    result = generate()
    assert any("left out by the paragraph limits: gsc.top_page" in m for m in note_messages(result, source="engine"))


# --- reviewer notes stay separate, determinism, configuration, purity --------------------------------------------------------------------------------

def test_reviewer_notes_never_appear_in_client_facing_text():
    result = generate(ga4=live_like_ga4(current={"diagnostics": {"daily_sessions_diff": 1}}),
                      recommendation_config=rc.RecommendationConfig(target_city=CITY))
    assert result.notes
    client = result.client_texts
    for note in result.notes:
        assert all(note.message not in text for text in client)
        assert note.message not in " ".join(client)
    for rec in result.recommendations:
        assert rec.trigger not in client and "Selected because" not in " ".join(client)
    joined = " ".join(client).lower()
    for word in ("reviewer", "diagnostic", "validation", "suppressed", "omitted"):
        assert word not in joined
    paragraphs = [p for s in result.sections for p in s.paragraphs]
    assert not any(m in p for m in (n.message for n in result.notes) for p in paragraphs)


def test_client_texts_are_paragraphs_plus_recommendation_titles_and_actions():
    result = generate()
    expected = [p for s in result.sections for p in s.paragraphs]
    for rec in result.recommendations:
        expected += [rec.title, rec.action]
    assert list(result.client_texts) == expected


def test_all_client_text_is_clean():
    result = generate(recommendation_config=rc.RecommendationConfig(
        target_city=CITY, standing=("backlinks", "technical"), max_items=5))
    for text in result.client_texts:
        assert w.find_superlatives(text) == [] and w.find_non_us_spelling(text) == []
        assert "@" not in text and "  " not in text


def test_output_is_deterministic():
    config = rc.RecommendationConfig(target_city=CITY)
    first = generate(recommendation_config=config)
    second = generate(recommendation_config=config)
    assert first == second
    assert [n.message for n in first.notes] == [n.message for n in second.notes]
    assert len(first.notes) == len({n for n in first.notes})  # no duplicate notes


def test_thresholds_and_target_country_are_passed_through():
    relaxed = generate(thresholds=mt.Thresholds(small_base_count=3))
    assert "up 50% from 4" in texts(relaxed.section("search_console"))
    assert relaxed.validation_failures == ()
    no_country = generate(target_country=None)
    assert "geo.target_market" not in {i.key for i in no_country.section("geography").insights}
    assert any("No target country" in m for m in note_messages(no_country, source="geography"))


def test_engine_module_is_pure_and_uses_only_internal_interfaces():
    source = (ROOT / "src" / "seo_report" / "insights" / "engine.py").read_text(encoding="utf-8")
    for forbidden in ("import datetime", "import time", "import random", "open(", "print(", "import requests",
                      "googleapiclient", "seo_report.sources", "seo_report.clients", "seo_report.auth"):
        assert forbidden not in source
