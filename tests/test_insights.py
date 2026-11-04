import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from types import SimpleNamespace

from seo_report import metrics as mt
from seo_report.insights import rules as rl
from seo_report.insights import wording as w
from seo_report.insights.rules import Tone
from seo_report.periods import report_periods
from seo_report.rankings import parse_ranking
from seo_report.sources import ga4 as ga
from seo_report.sources import gsc as gs

D = dt.date


# --- numbers -----------------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (0, "0"), (6, "6"), (6.0, "6"), (1862, "1,862"), (1234567, "1,234,567"), (2.5, "3"), (0.4, "0"),
])
def test_format_count(value, expected):
    assert w.format_count(value) == expected


@pytest.mark.parametrize("value, expected", [
    (0, "0"), (999, "999"), (1000, "1K"), (1960, "1.96K"), (1552, "1.55K"), (2275, "2.28K"),
    (1500, "1.5K"), (12345, "12.35K"), (999_500, "999.5K"), (999_999, "1M"),
    (1_234_567, "1.23M"), (-1960, "-1.96K"),
])
def test_format_compact_matches_search_console_style(value, expected):
    assert w.format_compact(value) == expected


@pytest.mark.parametrize("fraction, expected", [
    (0.0039, "0.39%"), (0.003, "0.3%"), (0.0, "0%"), (0.0018, "0.18%"), (0.099, "9.9%"),
    (0.311, "31.1%"), (0.547, "54.7%"), (0.6, "60%"), (1.0, "100%"),
])
def test_format_rate(fraction, expected):
    assert w.format_rate(fraction) == expected


@pytest.mark.parametrize("fraction, expected", [
    (0.4063, "40.6%"), (-0.3178, "31.8%"), (0.5, "50%"), (1.0, "100%"), (1.255, "126%"), (0.0, "0%"),
])
def test_format_percent_change_is_a_magnitude(fraction, expected):
    assert w.format_percent_change(fraction) == expected


@pytest.mark.parametrize("difference, expected", [
    (0.0021, "0.21"), (-0.0021, "0.21"), (0.236, "23.6"), (-0.0545, "5.5"), (0.01, "1"),
])
def test_format_points(difference, expected):
    assert w.format_points(difference) == expected


@pytest.mark.parametrize("value, expected", [(29.6, "29.6"), (29.0, "29.0"), (29.05, "29.1"), (1, "1.0")])
def test_format_position(value, expected):
    assert w.format_position(value) == expected


def test_rounding_is_half_up_not_bankers():
    assert w.format_count(0.5) == "1" and w.format_count(1.5) == "2" and w.format_count(2.5) == "3"
    assert w.format_position(0.25) == "0.3"


# --- dates ---------------------------------------------------------------------------------------

def test_format_date_uses_us_style():
    assert w.format_date(D(2026, 9, 9)) == "September 9, 2026"


@pytest.mark.parametrize("start, end, expected", [
    (D(2026, 9, 29), D(2026, 10, 26), "September 29–October 26, 2026"),
    (D(2026, 9, 1), D(2026, 9, 28), "September 1–28, 2026"),
    (D(2025, 12, 15), D(2026, 1, 11), "December 15, 2025–January 11, 2026"),
    (D(2026, 9, 1), D(2026, 9, 1), "September 1, 2026"),
])
def test_format_date_range(start, end, expected):
    assert w.format_date_range(start, end) == expected


def test_format_date_range_rejects_reversed_dates():
    with pytest.raises(ValueError):
        w.format_date_range(D(2026, 10, 26), D(2026, 9, 29))


# --- nouns and lists ---------------------------------------------------------------------------------

def test_count_noun_pluralizes_and_groups_digits():
    assert w.count_noun(1, "click") == "1 click"
    assert w.count_noun(0, "click") == "0 clicks"
    assert w.count_noun(6, "click") == "6 clicks"
    assert w.count_noun(1862, "impression") == "1,862 impressions"
    assert w.count_noun(2, "query", "queries") == "2 queries"
    assert w.count_noun(1960, "impression", compact=True) == "1.96K impressions"


def test_join_list_uses_the_serial_comma():
    assert w.join_list([]) == ""
    assert w.join_list(["a"]) == "a"
    assert w.join_list(["a", "b"]) == "a and b"
    assert w.join_list(["a", "b", "c"]) == "a, b, and c"


def test_quoted_and_sentence_helpers():
    assert w.quoted("roof repair") == "“roof repair”"
    assert w.capitalize_first("roof repair") == "Roof repair"
    assert w.capitalize_first("") == ""
    assert w.ensure_sentence("  One   two ") == "One two."
    assert w.ensure_sentence("Done!") == "Done!"
    assert w.join_sentences(["First", "", "Second."]) == "First. Second."
    assert w.word_count("one two  three") == 3


# --- change phrases ---------------------------------------------------------------------------------------

def test_count_phrase_small_base_has_no_percentage():
    assert w.change_phrase(mt.compare_count(6, 4)) == "up from 4"
    assert w.change_phrase(mt.compare_count(2, 6)) == "down from 6"
    assert w.change_phrase(mt.compare_count(4, 4)) == "unchanged from 4"


def test_count_phrase_with_percentage_and_formatter():
    assert w.change_phrase(mt.compare_count(1552, 1000)) == "up 55.2% from 1,000"
    assert w.change_phrase(mt.compare_count(1552, 2275), w.format_compact) == "down 31.8% from 2.28K"
    assert w.change_phrase(mt.compare_count(105, 100)) == "essentially unchanged from 100"


def test_count_phrase_is_none_when_not_comparable():
    assert w.change_phrase(mt.compare_count(None, 4)) is None


def rate(current, previous, cv=1000, pv=1000, min_delta=0.001):
    return mt.compare_rate(current, previous, current_volume=cv, previous_volume=pv, min_volume=500, min_delta=min_delta)


def test_rate_phrases():
    assert w.rate_change_phrase(rate(0.0039, 0.0018)) == "up from 0.18%"
    assert w.rate_change_phrase(rate(0.311, 0.547, min_delta=0.05)) == "down from 54.7%"
    assert w.rate_change_phrase(rate(0.0030, 0.0030)) == "unchanged from 0.3%"
    assert w.rate_change_phrase(rate(0.0030, 0.0025)) == "essentially unchanged from 0.25%"
    assert w.rate_change_phrase(rate(0.0039, 0.0018, cv=100)) is None


def test_position_phrases_never_describe_a_worse_position_as_growth():
    assert w.position_change_phrase(mt.compare_position(24.0, 29.1)) == "improved from 29.1"
    assert w.position_change_phrase(mt.compare_position(31.5, 29.1)) == "compared with 29.1"
    assert w.position_change_phrase(mt.compare_position(29.6, 29.1)) == "essentially unchanged from 29.1"
    assert w.position_change_phrase(mt.compare_position(None, 29.1)) is None
    assert not w.find_growth_words(w.position_change_phrase(mt.compare_position(31.5, 29.1)))


# --- text checks ---------------------------------------------------------------------------------------------

def test_us_spelling_check():
    assert w.find_non_us_spelling("Further optimisation and local enquiries") == ["enquiries", "optimisation"]
    assert w.find_non_us_spelling("Optimization of inquiries and color") == []
    assert w.find_non_us_spelling("The centrepiece") == []  # whole words only


def test_superlative_check_is_case_insensitive_and_whole_word():
    assert w.find_superlatives("Strong local rankings and an Excellent result") == ["excellent", "strong"]
    assert w.find_superlatives("a stronghold of bestselling roofs") == []
    assert w.find_superlatives("6 clicks, up from 4") == []


def test_growth_word_check():
    assert w.find_growth_words("Clicks increased and growth continued, up from 4") == ["growth", "increased", "up from"]
    assert w.find_growth_words("Impressions were 1.55K, down from 2.28K") == []


def test_numbers_in_extracts_figures_and_ignores_dates_and_quoted_labels():
    text = ("Between September 29–October 26, 2026, the website earned 6 organic clicks, up from 4, with a "
            "0.39% CTR, 1.55K impressions and 1,862 US impressions. It ranks #1 for “Roof Repair 75001” "
            "and averaged position 29.6 over 28 days.")
    assert w.numbers_in(text) == ["6", "4", "0.39%", "1.55K", "1862", "1", "29.6", "28"]


def test_numbers_in_handles_text_without_numbers():
    assert w.numbers_in("No figures here.") == []


def test_formatters_round_trip_through_numbers_in():
    sentence = f"{w.count_noun(1862, 'impression')} and {w.format_compact(1960)} at {w.format_rate(0.0039)}"
    assert w.numbers_in(sentence) == ["1862", "1.96K", "0.39%"]
    assert w.numbers_in(f"averaged {w.format_position(29.6)}") == ["29.6"]
    assert w.numbers_in(f"in {w.format_date_range(D(2026, 9, 29), D(2026, 10, 26))}") == []


def test_wording_module_is_pure():
    source = (ROOT / "src" / "seo_report" / "insights" / "wording.py").read_text(encoding="utf-8")
    for forbidden in ("import datetime", "import time", "import locale", "open(", "print(", "requests",
                      "googleapiclient", "seo_report.sources", "seo_report.clients", "seo_report.auth"):
        assert forbidden not in source


# ===================================================================================================
# rules
# ===================================================================================================

PERIODS = report_periods(D(2026, 9, 29), D(2026, 10, 26))


def kw(name, previous="", current=""):
    return SimpleNamespace(keyword=name, previous=parse_ranking(previous), current=parse_ranking(current))


def by_key(output):
    return {i.key: i for i in output.insights}


def assert_clean(output):
    """Every insight is traceable, free of superlatives and non-US spelling, and well formed."""
    for insight in output.insights:
        assert rl.untraced_numbers(insight) == [], insight.key
        assert w.find_superlatives(insight.text) == [], insight.key
        assert w.find_non_us_spelling(insight.text) == [], insight.key
        assert (insight.text[0].isupper() or insight.text[0].isdigit()) and insight.text.endswith("."), insight.key
        assert "  " not in insight.text and "@" not in insight.text, insight.key


def notes_text(output, **match):
    return [n.message for n in output.notes if all(getattr(n, k) == v for k, v in match.items())]


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


def mixed_keywords():
    return [
        kw("Concrete Patio Wiley", 3, 1), kw("Fence Repair Cedar Hill", 5, 2), kw("Siding Install", 4, 3),
        kw("Gutter Repair", 9, 6), kw("Interior Painting", 12, "Page 2, Rank 6"),
        kw("Roof Repair Arlington", "", "Above 10"), kw("Roof Install", 8, "Above 10"),
    ]


# --- evidence tracing ----------------------------------------------------------------------------------

def test_untraced_numbers_flags_fabricated_figures():
    insight = rl.Insight("ga4", "x", "6 clicks and 99 queries.", Tone.NEUTRAL, 1, {"clicks": 6})
    assert rl.untraced_numbers(insight) == ["99"]


def test_evidence_tokens_cover_each_formatting():
    tokens = rl.evidence_tokens({"a": 1862, "b": 0.0039, "c": 29.6, "d": True, "e": None, "f": "text"})
    assert {"1862", "1.86K", "0.39%", "29.6"} <= tokens
    assert "True" not in tokens and "1" not in tokens


# --- keyword rules -------------------------------------------------------------------------------------------

def test_keyword_insights_from_mixed_rankings():
    output = rl.keyword_rules(mt.build_report_metrics(keywords=mixed_keywords()))
    insights = by_key(output)
    assert set(insights) == {"keywords.top_positions", "keywords.moved_up", "keywords.moved_down", "keywords.above_10"}
    assert insights["keywords.top_positions"].text == (
        "The website ranks #1 for “Concrete Patio Wiley”. 2 other tracked keywords rank in the top 3. "
        "Overall, 4 of the 7 tracked keywords rank on page 1.")
    assert insights["keywords.moved_up"].text == (
        "4 tracked keywords have moved up since the previous ranking check, led by “Fence Repair Cedar Hill” "
        "(from position 5 to position 2).")
    assert insights["keywords.moved_down"].text == (
        "1 tracked keyword has moved down since the previous ranking check, led by “Interior Painting” "
        "(from position 12 to position 16).")
    assert insights["keywords.above_10"].text == (
        "2 tracked keywords are currently labelled “Above 10”, including “Roof Repair Arlington” and "
        "“Roof Install”, which leaves room to build visibility for those searches.")
    assert [insights[k].tone for k in ("keywords.top_positions", "keywords.moved_up", "keywords.moved_down",
                                       "keywords.above_10")] == [Tone.POSITIVE, Tone.POSITIVE, Tone.NEUTRAL,
                                                                  Tone.OPPORTUNITY]
    assert "above_10" in insights["keywords.above_10"].tags
    assert_clean(output)


def test_above_10_never_creates_movement_or_top_10_claims():
    output = rl.keyword_rules(mt.build_report_metrics(keywords=[kw("a", 9, "Above 10"), kw("b", "Above 10", 7)]))
    assert set(by_key(output)) == {"keywords.top_positions", "keywords.above_10"}
    for insight in output.insights:
        for phrase in ("top 10", "entered", "left the", "dropped", "outside", "fell"):
            assert phrase not in insight.text.lower()
    assert_clean(output)


def test_long_name_lists_are_summarized():
    keywords = [kw(f"Keyword {i}", "", 1) for i in range(5)]
    output = rl.keyword_rules(mt.build_report_metrics(keywords=keywords))
    text = by_key(output)["keywords.top_positions"].text
    assert text.startswith("The website ranks #1 for “Keyword 0”, “Keyword 1”, “Keyword 2”, and 2 other tracked keywords")
    assert_clean(output)


def test_unchanged_rankings_are_reported_without_gain_or_loss():
    output = rl.keyword_rules(mt.build_report_metrics(keywords=[kw("a", 4, 4), kw("b", 7, 7)]))
    assert by_key(output)["keywords.steady"].text == "2 tracked keyword rankings were unchanged since the previous ranking check."
    assert "keywords.moved_up" not in by_key(output) and "keywords.moved_down" not in by_key(output)


def test_blank_rankings_suppress_claims_and_add_a_reviewer_note():
    output = rl.keyword_rules(mt.build_report_metrics(keywords=[kw("a"), kw("b")]))
    assert output.insights == ()
    (message,) = notes_text(output, source="rankings", severity="warning")
    assert "not recorded for any of the 2" in message and "Not recorded" in message


def test_partly_missing_rankings_add_info_notes_but_keep_valid_insights():
    output = rl.keyword_rules(mt.build_report_metrics(keywords=[kw("a", "", 1), kw("b", "", "")]))
    assert "keywords.top_positions" in by_key(output)
    assert any("not recorded for 1 of 2" in m for m in notes_text(output, severity="info"))


def test_no_keywords_adds_a_warning_note():
    output = rl.keyword_rules(mt.build_report_metrics(keywords=[]))
    assert output.insights == () and notes_text(output, severity="warning")


def test_keywords_all_beyond_page_one_produce_no_positive_claim():
    output = rl.keyword_rules(mt.build_report_metrics(keywords=[kw("a", "", 14), kw("b", "", 22)]))
    assert output.insights == ()


# --- Search Console rules ----------------------------------------------------------------------------------------

def test_search_console_headline_sentences():
    output = rl.search_console_rules(mt.build_report_metrics(gsc=live_like_gsc()))
    insights = by_key(output)
    assert insights["gsc.clicks"].text == (
        "The website earned 6 organic clicks from Google Search during this 28-day period, up from 4 in the "
        "previous 28 days.")
    assert insights["gsc.impressions"].text == (
        "Search impressions totaled 1.55K during this 28-day period, down 31.8% from 2.28K in the previous 28 days.")
    assert insights["gsc.ctr"].text == "The click-through rate was 0.39%, up from 0.18% in the previous 28 days."
    assert insights["gsc.position"].text == (
        "The average search position was 29.6, essentially unchanged from 29.1 in the previous 28 days.")
    assert [insights[k].tone for k in ("gsc.clicks", "gsc.impressions", "gsc.ctr", "gsc.position")] == [
        Tone.POSITIVE, Tone.NEUTRAL, Tone.POSITIVE, Tone.NEUTRAL]
    assert_clean(output)


def test_declines_never_use_growth_language():
    output = rl.run_rules(mt.build_report_metrics(gsc=live_like_gsc(), ga4=live_like_ga4()))
    for key in ("gsc.impressions", "ga4.engagement", "ga4.organic_share"):
        assert w.find_growth_words(by_key(output)[key].text) == [], key
        assert by_key(output)[key].tone is Tone.NEUTRAL


def test_position_opportunity_is_tagged_and_separate_from_manual_rankings():
    output = rl.run_rules(mt.build_report_metrics(gsc=live_like_gsc(), keywords=mixed_keywords()))
    insights = by_key(output)
    assert insights["gsc.position_opportunity"].text == (
        "An average position of 29.6 leaves clear room to climb toward the first page of results.")
    assert insights["gsc.position_opportunity"].tone is Tone.OPPORTUNITY
    assert "low_position" in insights["gsc.position_opportunity"].tags
    assert not any(k.startswith("keywords.") and "29.6" in i.text for k, i in insights.items())


def test_a_worse_search_position_is_not_described_as_improvement():
    gsc = live_like_gsc(current={"totals": gm(6, 1552, 0.0039, 31.5)})
    insight = by_key(rl.search_console_rules(mt.build_report_metrics(gsc=gsc)))["gsc.position"]
    assert insight.text == "The average search position was 31.5, compared with 29.1 in the previous 28 days."
    assert w.find_growth_words(insight.text) == [] and insight.tone is Tone.NEUTRAL


def test_search_console_query_and_page_insights_name_only_safe_labels():
    output = rl.search_console_rules(mt.build_report_metrics(gsc=live_like_gsc()))
    insights = by_key(output)
    assert insights["gsc.new_queries"].text == (
        "3 new search queries appeared in Google Search compared with the previous 28 days, including "
        "“new roofer” and “far away”.")
    assert insights["gsc.striking_distance"].text == (
        "3 search queries are showing in positions 4–20 of Google Search, including “roof repair” and "
        "“new roofer”, which makes them candidates for focused optimization.")
    assert insights["gsc.zero_click_queries"].text == (
        "2 search queries generated at least 50 impressions without earning a click, including “new roofer”, "
        "which points to an opportunity to improve titles and meta descriptions for those searches.")
    assert insights["gsc.zero_click_pages"].text == (
        "1 page appeared in search results at least 50 times without earning a click, including “/old”.")
    assert insights["gsc.top_page"].text == "The page “/” earned the most search clicks (5 clicks) from 900 impressions."
    assert insights["gsc.new_queries"].tone is Tone.POSITIVE
    assert insights["gsc.striking_distance"].tags == {"striking_distance"}
    assert all("zz@example.test" not in i.text and "?x=1" not in i.text for i in output.insights)
    assert_clean(output)


def test_query_summaries_spell_the_plural_queries_never_querys():
    def texts(n):
        queries = [row(f"term {i}", 0, 200 - i, 8.0 + i / 10) for i in range(n)]
        out = by_key(rl.search_console_rules(mt.build_report_metrics(gsc=live_like_gsc(current={"queries": queries}))))
        return [out[k].text for k in ("gsc.new_queries", "gsc.striking_distance", "gsc.zero_click_queries")]
    for text in texts(7):
        assert "and 4 other" in text and "querys" not in text
        assert "other search queries" in text or "other new search queries" in text
    for text in texts(4):
        assert "1 other search query" in text or "1 other new search query" in text
        assert "querys" not in text


def test_truncated_query_list_suppresses_only_query_insights():
    warning = gs.DataWarning("truncated", "cut", "query")
    output = rl.search_console_rules(mt.build_report_metrics(gsc=live_like_gsc(current={"warnings": [warning]})))
    insights = by_key(output)
    for key in ("gsc.new_queries", "gsc.striking_distance", "gsc.zero_click_queries"):
        assert key not in insights
    assert {"gsc.clicks", "gsc.zero_click_pages", "gsc.top_page"} <= set(insights)
    assert any("query list was truncated" in m for m in notes_text(output, category="suppressed"))
    assert any("GSC current period: cut" in m for m in notes_text(output, category="data_quality"))


def test_empty_previous_period_keeps_current_statements_without_comparisons():
    empty = gs.PeriodData(PERIODS.previous, gs.EMPTY_METRICS, warnings=[gs.DataWarning("empty_period", "none")])
    gsc = gs.SearchConsoleData("https://example.test/", "siteOwner",
                               live_like_gsc().current, empty)
    output = rl.search_console_rules(mt.build_report_metrics(gsc=gsc))
    insights = by_key(output)
    assert insights["gsc.clicks"].text == "The website earned 6 organic clicks from Google Search during this 28-day period."
    assert "gsc.new_queries" not in insights and "gsc.striking_distance" in insights
    assert any("previous period" in m for m in notes_text(output, category="suppressed"))
    assert_clean(output)


def test_empty_current_period_suppresses_search_console_and_geography():
    empty = gs.PeriodData(PERIODS.current, gs.EMPTY_METRICS, warnings=[gs.DataWarning("empty_period", "none")])
    gsc = gs.SearchConsoleData("https://example.test/", "siteOwner", empty, live_like_gsc().previous)
    metrics = mt.build_report_metrics(gsc=gsc, target_country="United States")
    assert rl.search_console_rules(metrics).insights == () and rl.geography_rules(metrics).insights == ()
    assert any("current period" in m for m in notes_text(rl.search_console_rules(metrics), severity="warning"))


def test_missing_sources_add_notes_instead_of_failing():
    metrics = mt.build_report_metrics(keywords=mixed_keywords())
    output = rl.run_rules(metrics)
    assert {n.source for n in output.notes if n.category == "suppressed"} >= {"gsc", "ga4"}
    assert all(i.section == "keywords" for i in output.insights)


def test_search_console_diagnostics_become_reviewer_notes():
    diagnostics = {"query_clicks_diff": -5, "query_impressions_diff": -358, "page_impressions_diff": 108,
                   "daily_clicks_diff": 0, "country_impressions_diff": 2}
    output = rl.search_console_rules(mt.build_report_metrics(gsc=live_like_gsc(current={"diagnostics": diagnostics})))
    messages = notes_text(output, category="diagnostic")
    assert any("anonymized" in m and "5" in m for m in messages)
    assert any("page-level aggregation" in m and "108" in m for m in messages)
    assert any("country impressions differs" in m for m in notes_text(output, severity="warning", category="diagnostic"))
    assert len(messages) == 4  # the zero difference produces no note


# --- geography rules ---------------------------------------------------------------------------------------------------

def test_target_market_and_other_countries():
    metrics = mt.build_report_metrics(gsc=live_like_gsc(), target_country="United States")
    output = rl.geography_rules(metrics)
    insights = by_key(output)
    assert insights["geo.target_market"].text == (
        "United States accounted for 95.3% of search impressions (1,479 impressions) and 3 clicks during this "
        "28-day period, so search visibility is concentrated in the target market.")
    assert insights["geo.target_market"].tone is Tone.POSITIVE
    assert insights["geo.other_countries"].text == (
        "The website also recorded clicks from several other countries, including Pakistan and Mexico, "
        "while search impressions were recorded across 4 countries.")
    assert any("country level only" in m for m in notes_text(output, category="limitation"))
    assert_clean(output)


def test_other_countries_sentence_explains_its_relation_to_the_table():
    countries = [row("usa", 9, 900, label="United States")] + [
        row(f"c{i}", 1 if i < 3 else 0, 100 - i, label=n if i < 3 else f"Land {i}")
        for i, n in enumerate(("Pakistan", "Mexico", "France"))] + [row(f"z{i}", 0, 1, label=f"Zed {i}") for i in range(25)]
    metrics = mt.build_report_metrics(gsc=live_like_gsc(current={"countries": countries}), target_country="United States")
    text = by_key(rl.geography_rules(metrics))["geo.other_countries"].text
    assert text == ("The website also recorded clicks from several other countries, including Pakistan, Mexico and France, "
                    "while search impressions were recorded across 29 countries.")


def test_target_market_below_threshold_is_neutral_without_a_concentration_claim():
    countries = [row("usa", 3, 500, label="United States"), row("pak", 1, 8, label="Pakistan")]
    metrics = mt.build_report_metrics(gsc=live_like_gsc(current={"countries": countries}), target_country="United States")
    insight = by_key(rl.geography_rules(metrics))["geo.target_market"]
    assert insight.tone is Tone.NEUTRAL and "concentrated" not in insight.text and "32.2%" in insight.text


def test_geography_notes_for_missing_target_country():
    none = rl.geography_rules(mt.build_report_metrics(gsc=live_like_gsc()))
    assert "geo.target_market" not in by_key(none) and notes_text(none, severity="info", category="suppressed")
    missing = rl.geography_rules(mt.build_report_metrics(gsc=live_like_gsc(), target_country="Narnia"))
    assert "geo.target_market" not in by_key(missing) and notes_text(missing, severity="warning")
    warning = gs.DataWarning("truncated", "cut", "country")
    truncated = rl.geography_rules(mt.build_report_metrics(
        gsc=live_like_gsc(current={"warnings": [warning]}), target_country="Narnia"))
    assert any("truncated" in m for m in notes_text(truncated, severity="warning"))


# --- GA4 rules -------------------------------------------------------------------------------------------------------------

def test_ga4_headline_sentences_and_tone():
    output = rl.ga4_rules(mt.build_report_metrics(ga4=live_like_ga4()))
    insights = by_key(output)
    assert set(insights) == {"ga4.sessions", "ga4.users", "ga4.organic", "ga4.organic_share", "ga4.engagement",
                             "ga4.concentration", "ga4.key_events"}
    assert insights["ga4.sessions"].text == (
        "The website recorded 90 sessions during this 28-day period, up 40.6% from 64 in the previous 28 days.")
    assert insights["ga4.users"].text == (
        "The site had 82 total users during this 28-day period, up 41.4% from 58 in the previous 28 days.")
    assert insights["ga4.organic"].text == (
        "Organic Search contributed 19 sessions during this 28-day period, up from 17 in the previous 28 days.")
    assert insights["ga4.organic_share"].text == (
        "Organic Search accounted for 21.1% of all sessions, down from 26.6% in the previous 28 days.")
    assert insights["ga4.engagement"].text == (
        "The site-wide engagement rate was 31.1%, down from 54.7% in the previous 28 days.")
    assert "engagement_down" in insights["ga4.engagement"].tags
    assert insights["ga4.concentration"].text == (
        "17 of the 19 Organic Search sessions entered through “/”, so organic traffic depends heavily on a single page.")
    assert insights["ga4.key_events"].text == (
        "No key events were recorded during this 28-day period or the previous 28 days, so the report cannot yet "
        "show how many visits lead to inquiries.")
    assert insights["ga4.key_events"].tone is Tone.OPPORTUNITY and "no_key_events" in insights["ga4.key_events"].tags
    assert_clean(output)


def test_ga4_volume_suppression_is_explained_not_hidden():
    output = rl.ga4_rules(mt.build_report_metrics(ga4=live_like_ga4()))
    insights = by_key(output)
    assert "ga4.organic_engagement" not in insights and "ga4.engagement_gap" not in insights
    assert any("at least 20 sessions" in m for m in notes_text(output, category="suppressed"))


def test_ga4_engagement_gap_and_organic_engagement_with_enough_sessions():
    higher = live_like_ga4(current={"organic": g4(25, 20, 12, 0.48)}, previous={"organic": g4(25, 20, 12, 0.45)})
    output = rl.ga4_rules(mt.build_report_metrics(ga4=higher))
    insights = by_key(output)
    assert insights["ga4.engagement_gap"].tone is Tone.POSITIVE
    assert insights["ga4.engagement_gap"].text == (
        "Organic Search visitors were more engaged than the site average, with an engagement rate of 48% versus 31.1%.")
    assert "ga4.organic_engagement" in insights
    lower = live_like_ga4(current={"organic": g4(25, 20, 5, 0.2)})
    low_insight = by_key(rl.ga4_rules(mt.build_report_metrics(ga4=lower)))["ga4.engagement_gap"]
    assert low_insight.tone is Tone.OPPORTUNITY and "organic_engagement_lower" in low_insight.tags
    assert_clean(output)


def test_ga4_concentration_without_a_printable_label_and_non_concentrated_pages():
    unsafe = live_like_ga4(current={"pages": [landing("/r/" + "a" * 30, 17), landing("/painting", 1)]})
    text = by_key(rl.ga4_rules(mt.build_report_metrics(ga4=unsafe)))["ga4.concentration"].text
    assert text.startswith("17 of the 19 Organic Search sessions entered through the same landing page")
    spread = live_like_ga4(current={"pages": [landing("/", 6), landing("/painting", 5), landing("/contact", 4),
                                              landing("(not set)", 4, True)]})
    output = rl.ga4_rules(mt.build_report_metrics(ga4=spread))
    insights = by_key(output)
    assert "ga4.concentration" not in insights
    assert insights["ga4.top_pages"].text == (
        "The top Organic Search landing pages were “/” (6 sessions), “/painting” (5 sessions), and "
        "“/contact” (4 sessions).")
    assert_clean(output)


def test_ga4_organic_with_no_sessions_is_an_opportunity():
    output = rl.ga4_rules(mt.build_report_metrics(ga4=live_like_ga4(current={"organic": g4(0, 0, 0, None)})))
    insight = by_key(output)["ga4.organic"]
    assert insight.tone is Tone.OPPORTUNITY and "no_organic" in insight.tags
    assert insight.text == "No sessions were attributed to Organic Search during this 28-day period."


def test_ga4_key_events_variants():
    recorded = live_like_ga4(status="recorded", current={"whole": g4(90, 82, 28, 0.311, 5)},
                             previous={"whole": g4(64, 58, 35, 0.547, 2)})
    insight = by_key(rl.ga4_rules(mt.build_report_metrics(ga4=recorded)))["ga4.key_events"]
    assert insight.text == "The site recorded 5 key events during this 28-day period, up from 2 in the previous 28 days."
    unavailable = rl.ga4_rules(mt.build_report_metrics(ga4=live_like_ga4(status="unavailable")))
    assert "ga4.key_events" not in by_key(unavailable)
    assert any("Key events are unavailable" in m for m in notes_text(unavailable))


def test_ga4_discrepancy_is_a_diagnostic_note_and_totals_are_unchanged():
    ga4_data = live_like_ga4(current={"diagnostics": {"daily_sessions_diff": 1, "channel_sessions_diff": 0}})
    output = rl.ga4_rules(mt.build_report_metrics(ga4=ga4_data))
    (message,) = notes_text(output, category="diagnostic")
    assert "daily sessions" in message and "+1" in message and "not adjusted" in message
    assert by_key(output)["ga4.sessions"].evidence["current"] == 90


def test_ga4_flags_and_empty_periods():
    warnings = [ga.Ga4Warning("sampled", "sampled report", "daily")]
    output = rl.ga4_rules(mt.build_report_metrics(ga4=live_like_ga4(current={"warnings": warnings})))
    assert any("GA4 current period: sampled report" in m for m in notes_text(output, category="data_quality"))
    empty = ga.Ga4PeriodData(PERIODS.current, ga.EMPTY_METRICS, ga.EMPTY_METRICS, is_empty=True,
                             warnings=[ga.Ga4Warning("empty_period", "none")])
    data = ga.Ga4Data("1", "unavailable", empty, live_like_ga4().previous)
    result = rl.ga4_rules(mt.build_report_metrics(ga4=data))
    assert result.insights == () and notes_text(result, severity="warning", category="suppressed")


# --- all rules together --------------------------------------------------------------------------------------------------------

def full_metrics():
    return mt.build_report_metrics(gsc=live_like_gsc(), ga4=live_like_ga4(), keywords=mixed_keywords(),
                                   target_country="United States")


def test_run_rules_orders_sections_and_is_deterministic():
    first, second = rl.run_rules(full_metrics()), rl.run_rules(full_metrics())
    assert first == second
    order = {name: i for i, name in enumerate(rl.SECTIONS)}
    positions = [order[i.section] for i in first.insights]
    assert positions == sorted(positions)
    assert {i.section for i in first.insights} == set(rl.SECTIONS)
    keys = [i.key for i in first.insights]
    assert len(keys) == len(set(keys))
    assert_clean(first)


def test_tones_are_not_mixed_up_with_notes():
    output = rl.run_rules(full_metrics())
    assert all(isinstance(i.tone, Tone) for i in output.insights)
    assert all(n.severity in {"info", "warning"} for n in output.notes)
    assert all(n.message not in {i.text for i in output.insights} for n in output.notes)


def test_rules_module_is_pure_and_does_not_import_sources():
    source = (ROOT / "src" / "seo_report" / "insights" / "rules.py").read_text(encoding="utf-8")
    for forbidden in ("import datetime", "import time", "import locale", "open(", "print(", "requests",
                      "googleapiclient", "seo_report.sources", "seo_report.clients", "seo_report.auth"):
        assert forbidden not in source
