import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import metrics as mt
from seo_report.insights import recommendations as rc
from seo_report.insights import rules as rl
from seo_report.insights import wording as w
from seo_report.periods import report_periods
from seo_report.rankings import parse_ranking
from seo_report.sources import ga4 as ga
from seo_report.sources import gsc as gs

PERIODS = report_periods(__import__("datetime").date(2026, 9, 29), __import__("datetime").date(2026, 10, 26))
CITY = "Dallas–Fort Worth Metroplex"


# --- fixtures -------------------------------------------------------------------------------------------

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


def gsc_period(period, totals, *, countries=(), queries=(), pages=(), warnings=()):
    return gs.PeriodData(period, totals, countries=list(countries), queries=list(queries), pages=list(pages),
                         warnings=list(warnings))


def live_like_gsc(**overrides):
    cur = dict(
        totals=gm(6, 1552, 0.0039, 29.6),
        countries=[row("usa", 3, 1479, label="United States"), row("pak", 1, 8, label="Pakistan")],
        queries=[row("roof repair", 2, 100, 8.0), row("new roofer", 0, 80, 14.0), row("brand name", 1, 60, 1.2)],
        pages=[row("https://example.test/", 5, 900), row("https://example.test/old", 0, 120)],
    )
    prev = dict(
        totals=gm(4, 2275, 0.0018, 29.1),
        countries=[row("usa", 3, 2153, label="United States")],
        queries=[row("roof repair", 1, 200, 9.0)],
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


def ga_period(period, whole, organic, *, pages=(), warnings=()):
    return ga.Ga4PeriodData(period, whole, organic, organic_landing_pages=list(pages), warnings=list(warnings))


def live_like_ga4(status="none_recorded", **overrides):
    cur = dict(whole=g4(90, 82, 28, 0.311), organic=g4(19, 13, 9, 0.474),
               pages=[landing("/", 17), landing("/painting", 1)])
    prev = dict(whole=g4(64, 58, 35, 0.547), organic=g4(17, 13, 12, 0.706), pages=[landing("/", 14)])
    cur.update(overrides.get("current", {}))
    prev.update(overrides.get("previous", {}))
    return ga.Ga4Data("123456789", status, ga_period(PERIODS.current, **cur), ga_period(PERIODS.previous, **prev))


def full_metrics(**kwargs):
    kwargs.setdefault("gsc", live_like_gsc())
    kwargs.setdefault("ga4", live_like_ga4())
    kwargs.setdefault("keywords", mixed_keywords())
    kwargs.setdefault("target_country", "United States")
    return mt.build_report_metrics(**kwargs)


def pipeline(metrics=None, config=None, keywords=None):
    metrics = metrics or full_metrics()
    output = rl.run_rules(metrics)
    return rc.generate_recommendations(output.insights, metrics, config or rc.RecommendationConfig(),
                                       mixed_keywords() if keywords is None else keywords)


def keys(result):
    return [r.key for r in result.recommendations]


SECTIONS = {"keywords": "keywords", "gsc": "search_console", "geo": "geography", "ga4": "ga4"}


def ins(key, evidence=None):
    return rl.Insight(SECTIONS[key.split(".")[0]], key, "Text.", rl.Tone.NEUTRAL, 1, evidence or {})


POSITION = ins("gsc.position_opportunity", {"position": 29.6})
STRIKING = ins("gsc.striking_distance", {"striking_queries": 3, "position_min": 4.0, "position_max": 20.0})
ABOVE_10 = ins("keywords.above_10", {"above_10": 2, "total": 7})
ZERO_QUERIES = ins("gsc.zero_click_queries", {"zero_click_queries": 2, "min_impressions": 50})
ZERO_PAGES = ins("gsc.zero_click_pages", {"zero_click_pages": 1, "min_impressions": 50})
CTR_DOWN = ins("gsc.ctr", {"current": 0.002, "previous": 0.0039, "direction": "down", "period_days": 28})
CTR_UP = ins("gsc.ctr", {"current": 0.0039, "previous": 0.0018, "direction": "up", "period_days": 28})
MOVED_DOWN = ins("keywords.moved_down", {"declined": 1, "previous": 12, "current": 16, "change": -4})
CONCENTRATION = ins("ga4.concentration", {"top_sessions": 17, "organic_sessions": 19})
ENGAGEMENT_DOWN = ins("ga4.engagement", {"current": 0.311, "previous": 0.547, "direction": "down", "period_days": 28})
ENGAGEMENT_UP = ins("ga4.engagement", {"current": 0.6, "previous": 0.5, "direction": "up", "period_days": 28})
GAP_LOWER = ins("ga4.engagement_gap", {"organic_rate": 0.2, "site_rate": 0.311})
GAP_HIGHER = ins("ga4.engagement_gap", {"organic_rate": 0.48, "site_rate": 0.311})
KEY_EVENTS = ins("ga4.key_events", {"period_days": 28})


def recommend(insights, metrics=None, config=None, keywords=None):
    return rc.generate_recommendations(insights, metrics or full_metrics(), config or rc.RecommendationConfig(),
                                       mixed_keywords() if keywords is None else keywords)


def only(result):
    (rec,) = result.recommendations
    return rec


# --- each trigger and its non-trigger ----------------------------------------------------------------------

@pytest.mark.parametrize("insight, trigger", [
    (POSITION, "Selected because the Search Console average position is 29.6."),
    (STRIKING, "Selected because 3 search queries show in positions 4–20."),
    (ABOVE_10, "Selected because 2 tracked keywords are labelled “Above 10”."),
])
def test_accelerate_positions_triggers_individually(insight, trigger):
    rec = only(recommend([insight], keywords=()))
    assert rec.key == "accelerate_positions" and rec.trigger == trigger
    assert rec.supporting == (insight.key,) and rec.kind == "evidence"


def test_accelerate_positions_combines_its_triggers():
    rec = only(recommend([ABOVE_10, POSITION, STRIKING], keywords=()))
    assert rec.trigger == ("Selected because the Search Console average position is 29.6, 3 search queries show in "
                           "positions 4–20, and 2 tracked keywords are labelled “Above 10”.")
    assert rec.supporting == ("gsc.position_opportunity", "gsc.striking_distance", "keywords.above_10")


@pytest.mark.parametrize("insight, trigger", [
    (ZERO_QUERIES, "Selected because 2 search queries generated at least 50 impressions without a click."),
    (ZERO_PAGES, "Selected because 1 page appeared at least 50 times without a click."),
    (CTR_DOWN, "Selected because the click-through rate fell from 0.39% to 0.2%."),
])
def test_improve_ctr_triggers_individually(insight, trigger):
    rec = only(recommend([insight]))
    assert rec.key == "improve_ctr" and rec.trigger == trigger and rec.title == "Refresh titles and meta descriptions"


def test_improve_ctr_is_not_triggered_by_an_improving_or_flat_ctr():
    assert recommend([CTR_UP]).recommendations == ()
    flat = ins("gsc.ctr", {"current": 0.003, "previous": 0.003, "direction": "flat"})
    assert recommend([flat]).recommendations == ()


def test_review_declining_keywords_trigger():
    rec = only(recommend([MOVED_DOWN]))
    assert rec.key == "review_declining_keywords"
    assert rec.trigger == ("Selected because 1 tracked keyword has moved down since the previous ranking check, "
                           "led by a move from position 12 to position 16.")


def located(name, location, current="Above 10"):
    return kw(name, "", current, location)


def local_notes(result):
    return [n.message for n in result.notes if "local SEO" in n.message]


def test_configured_city_alone_never_triggers_local_seo():
    config = rc.RecommendationConfig(target_city=f"  {CITY} ")
    assert config.target_city == CITY
    general = [kw("Roofing services", "", "Above 10"), kw("Roof repair", "", "Above 10")]  # no target location
    result = recommend([ABOVE_10], config=config, keywords=general)
    assert keys(result) == ["accelerate_positions"]
    assert local_notes(result) == ["The local SEO recommendation was not made: none of the keywords labelled "
                                   "“Above 10” has a printable target location."]
    assert keys(recommend([POSITION], config=config)) == ["accelerate_positions"]  # no “Above 10” evidence at all
    assert keys(recommend([], config=config)) == []


def test_location_specific_above_10_keywords_trigger_local_seo():
    keywords = [located("Roof Repair Arlington", "Arlington"), located("Roof Install Fort Worth", "Fort Worth")]
    result = recommend([ABOVE_10], keywords=keywords)
    assert keys(result) == ["accelerate_positions", "local_seo"]  # the same insight can support both
    rec = result.recommendations[1]
    assert rec.trigger == ("Selected because 2 tracked keywords labelled “Above 10” have target locations: "
                           "“Arlington” and “Fort Worth”.")
    assert rec.action == ("Strengthen local relevance for “Arlington” and “Fort Worth”: keep business listings "
                          "consistent, add location-specific content for each area, and encourage customer reviews.")
    assert rec.supporting == ("keywords.above_10",)
    assert rec.evidence["local_keywords.count"] == 2 and rec.evidence["local_keywords.locations"] == 2
    assert rec.evidence["local_keywords.keywords"] == ("Roof Repair Arlington", "Roof Install Fort Worth")
    assert rec.evidence["local_keywords.location_names"] == ("Arlington", "Fort Worth")
    assert rec.evidence["keywords.above_10.above_10"] == 2  # the insight's own evidence is kept too
    assert local_notes(result) == [] and rc.untraced_recommendation_numbers(rec) == []


def test_local_seo_does_not_need_a_configured_city_and_uses_one_only_as_a_label():
    keywords = [located("Roof Repair Arlington", "Arlington"), kw("General keyword", "", "Above 10")]
    plain = recommend([ABOVE_10], keywords=keywords).recommendations[1]
    assert plain.trigger == ("Selected because 1 tracked keyword labelled “Above 10” has a target location: "
                             "“Arlington”.")
    assert "Metroplex" not in plain.action
    labelled = recommend([ABOVE_10], config=rc.RecommendationConfig(target_city=CITY), keywords=keywords)
    action = labelled.recommendations[1].action
    assert action.startswith("Strengthen local relevance for “Dallas–Fort Worth Metroplex”, starting with “Arlington”:")
    assert labelled.recommendations[1].trigger == plain.trigger  # the city never changes the trigger or evidence
    assert labelled.recommendations[1].evidence == plain.evidence


def test_general_or_differently_ranked_keywords_do_not_qualify():
    insight = ins("keywords.above_10", {"above_10": 1, "total": 3})
    keywords = [kw("Roofing", "", "Above 10"),  # labelled Above 10 but no target location
                kw("Roof Repair Arlington", "", 5, "Arlington"),  # has a location but ranks at a position
                kw("Siding Plano", 4, "", "Plano")]  # has a location but no current ranking
    result = recommend([insight], keywords=keywords)
    assert keys(result) == ["accelerate_positions"]
    assert len(local_notes(result)) == 1


@pytest.mark.parametrize("location", [None, "", "   ", "someone@example.test", "call 281 555 0100", "x" * 81])
def test_missing_or_unusable_target_locations_suppress_local_seo(location):
    result = recommend([ABOVE_10], keywords=[located("Roof Repair", location), located("Roof Install", location)])
    assert keys(result) == ["accelerate_positions"]
    assert "printable target location" in local_notes(result)[0]


def test_keyword_rows_without_a_target_location_attribute_suppress_local_seo():
    row_without_location = SimpleNamespace(keyword="Roof Repair", previous=parse_ranking(""),
                                           current=parse_ranking("Above 10"))
    result = recommend([ABOVE_10], keywords=[row_without_location])
    assert keys(result) == ["accelerate_positions"] and len(local_notes(result)) == 1


def test_local_seo_is_suppressed_when_keyword_rows_are_not_provided():
    result = recommend([ABOVE_10], keywords=())
    assert keys(result) == ["accelerate_positions"]
    assert local_notes(result) == ["The local SEO recommendation was not made: the keyword rows with target "
                                   "locations were not provided."]
    assert local_notes(recommend([POSITION], keywords=())) == []  # nothing to report without “Above 10” evidence


def test_local_seo_is_suppressed_when_keyword_rows_contradict_the_rankings_summary():
    insight = ins("keywords.above_10", {"above_10": 1, "total": 3})
    keywords = [located("Roof Repair Arlington", "Arlington"), located("Roof Install Fort Worth", "Fort Worth")]
    result = recommend([insight], keywords=keywords)
    assert "local_seo" not in keys(result)
    assert "do not match the rankings summary" in local_notes(result)[0]


def test_local_seo_summarizes_many_locations_and_deduplicates_repeats():
    insight = ins("keywords.above_10", {"above_10": 5, "total": 9})
    five = [located(f"Keyword {i}", name) for i, name in enumerate(["Plano", "Frisco", "Allen", "Wylie", "Rowlett"])]
    rec = recommend([insight], keywords=five).recommendations[1]
    assert rec.trigger == ("Selected because 5 tracked keywords labelled “Above 10” have target locations: "
                           "“Plano”, “Frisco”, “Allen”, and 2 other locations.")
    assert rec.evidence["local_keywords.extra_locations"] == 2 and rec.evidence["local_keywords.locations"] == 5
    assert rc.untraced_recommendation_numbers(rec) == []
    twice = recommend([ABOVE_10], keywords=[located("a", "Arlington"), located("b", "arlington")]).recommendations[1]
    assert twice.trigger == ("Selected because 2 tracked keywords labelled “Above 10” have target locations: "
                             "“Arlington”.")
    assert twice.evidence["local_keywords.locations"] == 1


def test_digits_in_city_and_location_labels_do_not_break_evidence_tracing():
    config = rc.RecommendationConfig(target_city="Zone 5 Metro")
    keywords = [located("Roof Repair 24 Hour", "Route 66"), located("Roof Install", "Exit 9 Area")]
    rec = recommend([ABOVE_10], config=config, keywords=keywords).recommendations[1]
    assert "“Zone 5 Metro”" in rec.action and "“Route 66”" in rec.trigger
    assert rc.untraced_recommendation_numbers(rec) == []
    # the safeguard is intact: an unquoted or fabricated number is still caught
    fake = rc.Recommendation("x", "Title", "Fix 7 pages near “Route 66”.", "Selected because 6 of 99.",
                             {"a.n": 6}, ("a",), "evidence", 1)
    assert rc.untraced_recommendation_numbers(fake) == ["7", "99"]


def test_local_seo_results_are_deterministic():
    keywords = [located("Roof Repair Arlington", "Arlington"), located("Roof Install Fort Worth", "Fort Worth")]
    config = rc.RecommendationConfig(target_city=CITY)
    assert recommend([ABOVE_10], config=config, keywords=keywords) == recommend([ABOVE_10], config=config,
                                                                                 keywords=keywords)
    assert pipeline(config=config) == pipeline(config=config)


def test_broaden_landing_pages_trigger():
    rec = only(recommend([CONCENTRATION]))
    assert rec.key == "broaden_landing_pages"
    assert rec.trigger == "Selected because 17 of 19 Organic Search sessions entered through a single landing page."


def test_improve_engagement_triggers_and_non_triggers():
    assert only(recommend([ENGAGEMENT_DOWN])).trigger == (
        "Selected because the site-wide engagement rate fell from 54.7% to 31.1%.")
    assert only(recommend([GAP_LOWER])).trigger == (
        "Selected because the Organic Search engagement rate (20%) is below the site-wide rate (31.1%).")
    both = only(recommend([ENGAGEMENT_DOWN, GAP_LOWER]))
    assert both.trigger.count(" and ") == 1 and both.supporting == ("ga4.engagement", "ga4.engagement_gap")
    assert recommend([ENGAGEMENT_UP, GAP_HIGHER]).recommendations == ()


def test_key_event_review_only_when_none_recorded_applies():
    rec = only(recommend([KEY_EVENTS]))
    assert rec.key == "review_key_events" and rec.title == "Review key event tracking"
    assert rec.trigger == "Selected because no key events were recorded in either 28-day period."
    recorded = full_metrics(ga4=live_like_ga4(status="recorded"))
    assert recommend([KEY_EVENTS], metrics=recorded).recommendations == ()
    unavailable = full_metrics(ga4=live_like_ga4(status="unavailable"))
    assert recommend([KEY_EVENTS], metrics=unavailable).recommendations == ()


def test_zero_key_events_in_one_period_does_not_trigger_the_review():
    current_zero = live_like_ga4(status="recorded", current={"whole": g4(90, 82, 28, 0.311, 0)},
                                 previous={"whole": g4(64, 58, 35, 0.547, 4)})
    metrics = full_metrics(ga4=current_zero)
    assert "review_key_events" not in keys(pipeline(metrics))


def test_key_event_wording_never_claims_tracking_is_unconfigured():
    rec = only(recommend([KEY_EVENTS]))
    text = f"{rec.title} {rec.action} {rec.trigger}".lower()
    for phrase in ("unconfigured", "not configured", "not set up", "isn't set up", "no tracking", "missing tracking"):
        assert phrase not in text


def test_irrelevant_insights_trigger_nothing():
    unrelated = [ins("ga4.sessions", {"current": 90}), ins("gsc.clicks", {"current": 6}), ins("geo.target_market", {})]
    assert recommend(unrelated).recommendations == ()
    assert recommend([]).recommendations == ()


# --- ordering and the limit ------------------------------------------------------------------------------------------

def test_full_pipeline_orders_by_priority_and_caps_at_five():
    result = pipeline(config=rc.RecommendationConfig(target_city=CITY))
    assert keys(result) == ["accelerate_positions", "improve_ctr", "review_declining_keywords", "local_seo",
                            "broaden_landing_pages"]
    assert [r.priority for r in result.recommendations] == sorted(r.priority for r in result.recommendations)
    assert result.omitted == ("improve_engagement", "review_key_events")
    (note,) = [n for n in result.notes if "Omitted" in n.message]
    assert note.message == ("Omitted by the 5-item limit: Improve landing-page engagement and Review key event "
                            "tracking.")
    assert len(result.recommendations) <= rc.MAX_RECOMMENDATIONS


def test_max_items_can_lower_but_never_raise_the_limit():
    result = pipeline(config=rc.RecommendationConfig(max_items=2))
    assert keys(result) == ["accelerate_positions", "improve_ctr"]
    assert result.omitted == ("review_declining_keywords", "local_seo", "broaden_landing_pages",
                              "improve_engagement", "review_key_events")  # seven trigger; no city is needed
    for bad in (0, 6, -1):
        with pytest.raises(ValueError, match="max_items"):
            rc.RecommendationConfig(max_items=bad)


def test_output_order_does_not_depend_on_input_order():
    metrics = full_metrics()
    insights = list(rl.run_rules(metrics).insights)
    forward = rc.generate_recommendations(insights, metrics)
    backward = rc.generate_recommendations(list(reversed(insights)), metrics)
    assert forward == backward


# --- standing recommendations ------------------------------------------------------------------------------------------

def test_standing_recommendations_are_disabled_by_default():
    assert rc.RecommendationConfig().standing == ()
    assert not any(k.startswith("standing.") for k in keys(pipeline()))
    assert recommend([]).recommendations == ()


def test_enabled_standing_recommendations_follow_evidence_driven_ones():
    config = rc.RecommendationConfig(standing=("technical", "backlinks", "technical"))
    assert config.standing == ("backlinks", "technical")  # de-duplicated, fixed order
    result = recommend([POSITION, ZERO_QUERIES], config=config)
    assert keys(result) == ["accelerate_positions", "improve_ctr", "standing.backlinks", "standing.technical"]
    assert [r.kind for r in result.recommendations] == ["evidence", "evidence", "standing", "standing"]
    standing = result.recommendations[2]
    assert standing.evidence == {} and standing.supporting == ()
    assert "not derived from this period's data" in standing.trigger
    assert standing.title == "Strengthen backlink profile"


def test_standing_recommendations_work_with_no_evidence_when_enabled():
    result = recommend([], config=rc.RecommendationConfig(standing=("backlinks",)))
    assert keys(result) == ["standing.backlinks"]


def test_standing_recommendations_only_fill_remaining_places():
    config = rc.RecommendationConfig(standing=("backlinks", "technical"), target_city=CITY)
    full = pipeline(config=config)
    assert not any(k.startswith("standing.") for k in keys(full))
    assert full.omitted == ("improve_engagement", "review_key_events", "standing.backlinks", "standing.technical")
    four = recommend([POSITION, ZERO_QUERIES, MOVED_DOWN, CONCENTRATION], config=config)
    assert keys(four) == ["accelerate_positions", "improve_ctr", "review_declining_keywords",
                          "broaden_landing_pages", "standing.backlinks"]
    assert four.omitted == ("standing.technical",)


def test_unknown_standing_recommendations_are_rejected():
    with pytest.raises(ValueError, match="unknown standing"):
        rc.RecommendationConfig(standing=("backlinks", "social"))


# --- missing, incomplete and truncated evidence -------------------------------------------------------------------------

ALL_SYNTHETIC = [POSITION, STRIKING, ZERO_QUERIES, CTR_DOWN, ABOVE_10, MOVED_DOWN, CONCENTRATION,
                 ENGAGEMENT_DOWN, KEY_EVENTS]


def test_missing_sources_produce_no_recommendations_from_their_insights():
    result = recommend(ALL_SYNTHETIC, metrics=mt.build_report_metrics(keywords=mixed_keywords()))
    assert keys(result) == ["accelerate_positions", "review_declining_keywords", "local_seo"]  # keyword evidence only
    assert result.recommendations[0].supporting == ("keywords.above_10",)
    assert {(n.source, n.severity) for n in result.notes} == {("gsc", "warning"), ("ga4", "warning")}
    assert all("data was not provided" in n.message for n in result.notes)


def test_incomplete_gsc_data_suppresses_search_console_recommendations_only():
    incomplete = full_metrics(gsc=live_like_gsc(current={"warnings": [gs.DataWarning("incomplete_data", "late")]}))
    result = recommend(ALL_SYNTHETIC, metrics=incomplete)
    assert "improve_ctr" not in keys(result)
    assert result.recommendations[0].supporting == ("keywords.above_10",)  # position and query evidence dropped
    assert "broaden_landing_pages" in keys(result)
    (note,) = [n for n in result.notes if n.source == "gsc"]  # the other note is the five-item limit
    assert "may be incomplete" in note.message


def test_incomplete_ga4_data_suppresses_ga4_recommendations_only():
    incomplete = full_metrics(ga4=live_like_ga4(current={"warnings": [ga.Ga4Warning("incomplete_data", "late")]}))
    result = recommend(ALL_SYNTHETIC, metrics=incomplete)
    assert not {"broaden_landing_pages", "improve_engagement", "review_key_events"} & set(keys(result))
    assert {"accelerate_positions", "improve_ctr"} <= set(keys(result))
    assert [n.source for n in result.notes] == ["ga4"]


def test_empty_current_periods_suppress_recommendations():
    empty_gsc = gs.PeriodData(PERIODS.current, gs.EMPTY_METRICS, warnings=[gs.DataWarning("empty_period", "none")])
    gsc = gs.SearchConsoleData("https://example.test/", "siteOwner", empty_gsc, live_like_gsc().previous)
    result = recommend([POSITION, ZERO_QUERIES], metrics=full_metrics(gsc=gsc))
    assert result.recommendations == () and "current period has no data" in result.notes[0].message


def test_truncated_query_list_drops_query_evidence_but_keeps_other_triggers():
    warning = gs.DataWarning("truncated", "cut", "query")
    metrics = full_metrics(gsc=live_like_gsc(current={"warnings": [warning]}))
    result = pipeline(metrics)
    accelerate = next(r for r in result.recommendations if r.key == "accelerate_positions")
    assert "search quer" not in accelerate.trigger and "gsc.striking_distance" not in accelerate.supporting
    assert "gsc.position_opportunity" in accelerate.supporting
    ctr = next(r for r in result.recommendations if r.key == "improve_ctr")
    assert "search quer" not in ctr.trigger  # only the page evidence remains


def test_blank_rankings_produce_no_keyword_based_recommendations():
    metrics = full_metrics(keywords=[kw("a"), kw("b")])
    result = pipeline(metrics, rc.RecommendationConfig(target_city=CITY), keywords=[kw("a"), kw("b")])
    assert "local_seo" not in keys(result) and "review_declining_keywords" not in keys(result)
    accelerate = next(r for r in result.recommendations if r.key == "accelerate_positions")
    assert "keywords.above_10" not in accelerate.supporting


# --- evidence traceability, determinism, wording --------------------------------------------------------------------------

def test_every_recommendation_is_traceable_to_its_evidence():
    result = pipeline(config=rc.RecommendationConfig(target_city=CITY, standing=("backlinks", "technical"), max_items=5))
    insights = {i.key: i for i in rl.run_rules(full_metrics()).insights}
    for rec in result.recommendations:
        assert rc.untraced_recommendation_numbers(rec) == [], rec.key
        prefixes = rec.supporting + (("local_keywords",) if rec.key == "local_seo" else ())
        for name in rec.evidence:
            assert any(name.startswith(f"{prefix}.") for prefix in prefixes), (rec.key, name)
        for key in rec.supporting:
            assert key in insights
            for field, value in insights[key].evidence.items():
                assert rec.evidence[f"{key}.{field}"] == value


def test_untraced_recommendation_numbers_flags_fabricated_figures():
    rec = rc.Recommendation("x", "Title", "Do 5 things.", "Selected because 6 of 99.", {"a.n": 6}, ("a",), "evidence", 1)
    assert rc.untraced_recommendation_numbers(rec) == ["5", "99"]


def test_recommendation_text_is_practical_balanced_and_clean():
    config = rc.RecommendationConfig(standing=("backlinks", "technical"), target_city=CITY, max_items=5)
    all_recs = []
    for insights in ([POSITION], [ZERO_QUERIES], [MOVED_DOWN], [ABOVE_10], [CONCENTRATION], [ENGAGEMENT_DOWN],
                     [KEY_EVENTS], []):
        all_recs += recommend(insights, config=config).recommendations
    assert len({r.key for r in all_recs}) == 9
    for rec in all_recs:
        text = f"{rec.title} {rec.action} {rec.trigger}"
        assert w.find_superlatives(text) == [] and w.find_non_us_spelling(text) == [], rec.key
        assert rec.trigger.endswith(".") and rec.action.endswith(".") and rec.title[0].isupper()
        for promise in ("guarantee", "will increase", "will improve", "ensure"):
            assert promise not in text.lower(), rec.key


def test_results_are_deterministic():
    config = rc.RecommendationConfig(target_city=CITY, standing=("backlinks",))
    assert pipeline(config=config) == pipeline(config=config)


def test_recommendation_module_is_pure():
    source = (ROOT / "src" / "seo_report" / "insights" / "recommendations.py").read_text(encoding="utf-8")
    for forbidden in ("import datetime", "import time", "import random", "open(", "print(", "import requests",
                      "googleapiclient", "seo_report.sources", "seo_report.clients", "seo_report.auth"):
        assert forbidden not in source
