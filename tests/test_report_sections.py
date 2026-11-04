import dataclasses
import datetime as dt
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import keyword_pages as kp
from seo_report import render_input as ri
from seo_report import report_document as rd
from seo_report import report_sections as rs
from test_render_input import sample
from test_report_templates import ASSETS, Tags, visible_text

START = dt.date(2026, 9, 29)
USABLE = kp.usable_height_mm(ASSETS)


def days(n, start=START, step=1):
    return [start + dt.timedelta(days=i * step) for i in range(n)]


def sc_visual(status="available", n=5, clicks=120, impressions=4000, ctr=Decimal("0.03"), position=Decimal("12.5"), **kw):
    daily = tuple(ri.DailyPoint(d, i, 10 * i + 1) for i, d in enumerate(days(n)))
    return ri.SearchConsoleVisuals(status, clicks, impressions, ctr, position, daily, **kw)


def ga4_visual(status="available", n=5, **kw):
    values = dict(sessions=300, users=250, engagement_rate=Decimal("0.5"),
                  channels=(ri.ChannelRow("Organic Search", 200, 150, 120), ri.ChannelRow("Direct", 100, 100, 40)),
                  daily=tuple(ri.Ga4DailyPoint(d, 5 + i) for i, d in enumerate(days(n))))
    values.update(kw)
    return ri.Ga4Visuals(status, **values)


def countries(n, **kw):
    return tuple(ri.CountryRow(f"Country {i:03d}", 1000 - i, 5000 - i) for i in range(n))


def geo_visual(n=5, status="available", **kw):
    return ri.GeographyVisuals(status, countries(n), **kw)


def input_(**overrides):
    return sample(**overrides)


def pages(**overrides):
    return rs.plan_section_pages(input_(**overrides), ASSETS)


def html_of(**overrides):
    value = input_(**overrides)
    return "\n".join(str(rs.render_section_page_body(p, ASSETS)) for p in rs.plan_section_pages(value, ASSETS))


def sec(html, name):
    """Only the blocks of one report section (the default sample fills every section)."""
    parts = re.split(r'(?=<div class="block block--)', html)
    return "".join(b for b in parts if f'data-section="{name}"' in b[:120])


def sec_html(name, **overrides):
    return sec(html_of(**overrides), name)


def tiles(html):
    return dict(re.findall(r'<div class="tile__label">([^<]*)</div><div class="tile__value">([^<]*)</div>', html))


def count(html, record):
    return html.count(f'data-record="{record}"')


def placements(plan_, kind=None):
    return [p for page in plan_ for p in page.placements if kind is None or p.block.kind == kind]


def table_rows(plan_, name):
    return [row for p in placements(plan_, "table") if p.block.name == name for row in p.block.rows]


# --- Search Console --------------------------------------------------------------------------------------------------------------

def test_search_console_shows_all_four_metrics_with_their_units():
    html = html_of(search_console=sc_visual(clicks=1234, impressions=1234567, ctr=Decimal("0.0343"), position=Decimal("29.64")))
    t = tiles(html)
    assert (t["Clicks"], t["Impressions"], t["CTR"], t["Average position"]) == ("1,234", "1,234,567", "3.43%", "29.6")


def test_ctr_and_position_use_fixed_rounding_of_the_source_values():
    for ctr, text in ((Decimal("0.0039"), "0.39%"), (Decimal("0.00005"), "0.01%"), (Decimal("0.0000"), "0.00%"), (Decimal("1"), "100.00%")):
        assert tiles(html_of(search_console=sc_visual(ctr=ctr)))["CTR"] == text
    for pos, text in ((Decimal("29.6"), "29.6"), (Decimal("29.65"), "29.7"), (Decimal("1"), "1.0"), (Decimal("0"), "0.0")):
        assert tiles(html_of(search_console=sc_visual(position=pos)))["Average position"] == text


def test_a_missing_metric_shows_a_dash_and_the_others_stay_valid():
    html = html_of(search_console=sc_visual(position=None))
    t = tiles(html)
    assert t["Average position"] == "–" and t["Clicks"] == "120" and t["CTR"] == "3.00%" and t["Impressions"] == "4,000"
    html = html_of(search_console=sc_visual(clicks=None, ctr=None))
    t = tiles(html)
    assert (t["Clicks"], t["CTR"], t["Impressions"]) == ("–", "–", "4,000")


def test_measured_zero_is_shown_as_zero_not_as_missing():
    t = tiles(html_of(search_console=sc_visual(clicks=0, impressions=0, ctr=Decimal("0"), position=Decimal("0"))))
    assert (t["Clicks"], t["Impressions"], t["CTR"], t["Average position"]) == ("0", "0", "0.00%", "0.0")


def test_missing_daily_records_are_not_invented_in_the_charts():
    daily = tuple(ri.DailyPoint(START + dt.timedelta(days=d), 1, 2) for d in (0, 1, 3, 4))
    html = sec_html("search_console", search_console=ri.SearchConsoleVisuals("available", 4, 8, Decimal("0.5"), Decimal("3"), daily))
    assert html.count('class="chart__marker chart__marker--primary"') == 4
    assert html.count("<polyline") == 4  # two pieces per chart (clicks and impressions)
    assert "24 days in this period have no record and are left blank, not shown as zero." in html
    assert "Daily performance" not in html and count(html, "daily") == 0


def test_a_complete_period_has_no_gap_message():
    daily = tuple(ri.DailyPoint(d, 1, 2) for d in days(28))
    html = sec_html("search_console", search_console=ri.SearchConsoleVisuals("available", 28, 56, Decimal("0.5"), Decimal("3"), daily))
    assert "no record" not in html and "Daily performance" not in html and 'data-table="daily"' not in html


def test_the_charts_draw_every_daily_record_and_there_is_no_daily_table():
    visual = sc_visual(n=12)
    html = sec_html("search_console", search_console=visual)
    assert html.count('class="chart__marker chart__marker--primary"') == len(visual.daily) == 12
    assert html.count('chart__marker--secondary"') == 12
    assert "<table" not in html and not table_rows(pages(search_console=visual), "daily")
    assert [p for p in placements(pages(search_console=visual), "table") if p.block.name == "daily"] == []
    assert "Clicks per day" in html and "Impressions per day" in html


def test_the_daily_table_is_gone_but_the_summary_tiles_paragraphs_and_statements_stay():
    html = sec_html("search_console", search_console=sc_visual(n=9),
                    historical_statements=(statement("search_console", "SC history."),))
    t = tiles(html)
    assert (t["Clicks"], t["Impressions"], t["CTR"], t["Average position"]) == ("120", "4,000", "3.00%", "12.5")
    assert "Search paragraph." in html and "SC history." in html and 'class="chart chart--line"' in html


def test_the_daily_series_is_still_in_the_render_input_after_presentation():
    visual = sc_visual(n=12)
    value = input_(search_console=visual)
    html_of(search_console=visual)
    assert len(value.search_console.daily) == 12 and value.search_console.daily == visual.daily


def test_two_charts_clicks_and_impressions_have_separate_scales():
    html = sec_html("search_console", search_console=sc_visual(n=6))
    assert html.count('class="chart chart--line"') == 2 and "Clicks per day" in html and "Impressions per day" in html


def test_an_incomplete_search_console_section_keeps_its_figures_and_adds_a_caveat():
    html = sec_html("search_console", search_console=sc_visual(status="incomplete"))
    assert 'data-visual-status="incomplete"' in html and "Search Console data is incomplete for this period" in html
    assert tiles(html)["Clicks"] == "120" and html.count('class="chart__marker chart__marker--primary"') == 5 and "not available" not in html


def test_an_unavailable_search_console_section_has_only_the_message():
    html = sec_html("search_console", search_console=ri.SearchConsoleVisuals.unavailable())
    assert "Search Console data is not available for this report." in html
    assert "Clicks" not in tiles(html) and 'class="chart' not in html and count(html, "daily") == 0
    assert "Google Search Console" in html  # the heading stays


def test_search_console_without_daily_records_says_so_and_draws_no_chart():
    visual = ri.SearchConsoleVisuals("available", 4, 8, Decimal("0.5"), Decimal("3"))
    html = sec_html("search_console", search_console=visual)
    assert rs.NO_DAILY in html and 'class="chart chart--line"' not in html and count(html, "daily") == 0
    assert tiles(html)["Clicks"] == "4"


def test_search_console_paragraphs_come_from_its_own_section_only():
    html = html_of()
    assert sec(html, "search_console").count("Search paragraph.") == 1
    assert "Analytics paragraph." not in sec(html, "search_console") and "Search paragraph." not in sec(html, "ga4")
    assert sec(html, "ga4").count("Analytics paragraph.") == 1 and "First paragraph." not in html  # keyword text is R4's


def test_the_heading_is_the_sections_own_title():
    sections = (ri.RenderSection("search_console", "My Search Title", "ok", ("P.",)),) + sample().sections[2:]
    html = html_of(sections=sections)
    assert '<h2 class="section__heading">My Search Title</h2>' in html


# --- geography ---------------------------------------------------------------------------------------------------------------------

def test_geography_shows_up_to_five_countries_with_a_chart_in_the_same_order():
    value = geo_visual(5)
    html = sec_html("geography", geography=value)
    rows = table_rows(pages(geography=value), "countries")
    assert rows == [(c.name, f"{c.clicks:,}", f"{c.impressions:,}") for c in value.countries]
    assert count(html, "country") == 5 and 'class="chart chart--bar"' in html
    tspans = re.findall(r"<tspan[^>]*>([^<]+)</tspan>", html)
    assert tspans == [c.name for c in value.countries]
    assert 'class="table__caption">Countries</p>' in html and "The chart shows the top" not in html


def test_the_table_has_the_five_countries_with_the_most_impressions_whatever_the_source_order():
    rows_in = tuple(ri.CountryRow(f"C{i}", 100 - i, 10 * i + 1) for i in range(12))  # impressions rise with i
    value = ri.GeographyVisuals("available", rows_in)
    html = sec_html("geography", geography=value)
    rows = table_rows(pages(geography=value), "countries")
    assert [r[0] for r in rows] == ["C11", "C10", "C9", "C8", "C7"] and count(html, "country") == 5
    assert [int(r[2].replace(",", "")) for r in rows] == sorted((int(r[2].replace(",", "")) for r in rows), reverse=True)
    assert re.findall(r"<tspan[^>]*>([^<]+)</tspan>", html) == ["C11", "C10", "C9", "C8", "C7"]  # chart and table agree
    assert 'class="table__caption">Top 5 countries by impressions (of 12)</p>' in html


def test_equal_impressions_are_ordered_by_clicks_descending_then_by_name():
    rows_in = (ri.CountryRow("Zed", 5, 100), ri.CountryRow("beta", 9, 100), ri.CountryRow("Alpha", 9, 100),
               ri.CountryRow("Mid", 7, 100), ri.CountryRow("Low", 1, 100), ri.CountryRow("Dropped", 1, 100),
               ri.CountryRow("Top", 0, 101))
    ordered = [c.name for c in rs.ranked_countries(rows_in)]
    assert ordered == ["Top", "Alpha", "beta", "Mid", "Zed"]
    assert [c.name for c in rs.ranked_countries(tuple(reversed(rows_in)))] == ordered  # the input order does not matter
    assert [c.name for c in rs.ranked_countries(rows_in, 7)][-2:] == ["Dropped", "Low"]  # name decides the last tie


def test_ranking_does_not_change_or_drop_the_source_countries():
    rows_in = countries(9)[::-1]
    value = ri.GeographyVisuals("available", rows_in)
    before = value.countries
    rs.ranked_countries(value.countries)
    html_of(geography=value)
    assert value.countries == before and len(value.countries) == 9
    assert len(input_(geography=value).geography.countries) == 9


def test_fewer_than_five_countries_are_all_shown():
    value = geo_visual(3)
    assert len(table_rows(pages(geography=value), "countries")) == 3


def test_unicode_country_names_survive_in_the_table_and_the_chart():
    names = ["Côte d’Ivoire", "Türkiye", "日本"]
    value = ri.GeographyVisuals("available", tuple(ri.CountryRow(n, 10 - i, 100) for i, n in enumerate(names)))
    html = sec_html("geography", geography=value)
    for name in names:
        assert html.count(name) >= 3  # table cell, chart label and chart tooltip


def test_long_country_names_are_kept_in_full_and_wrap():
    name = "The Federal Democratic Republic of a Very Long Country Name"
    value = ri.GeographyVisuals("available", (ri.CountryRow(name, 10, 100), ri.CountryRow("B", 5, 50)))
    html = sec_html("geography", geography=value)
    assert f">{name}</td>" in html and f"<title>{name}: 10 clicks</title>" in html
    assert " ".join(re.findall(r"<tspan[^>]*>([^<]+)</tspan>", html)[:-1]) == name


def test_the_clients_target_country_is_never_substituted():
    html = sec_html("geography", geography=ri.GeographyVisuals.unavailable())
    assert count(html, "country") == 0 and 'chart--bar' not in html and "<table" not in html
    assert "Country" not in html.replace("Geographic data", "")  # no row, label or stand-in for the target country


def test_unavailable_geography_has_the_message_and_no_table():
    html = sec_html("geography", geography=ri.GeographyVisuals.unavailable())
    assert "Geographic data is not available for this report." in html and count(html, "country") == 0
    assert "<table" not in html.split("Geographical Performance")[1].split("Google Analytics 4")[0]


def test_incomplete_geography_keeps_its_rows_and_adds_a_caveat():
    html = sec_html("geography", geography=geo_visual(4, status="incomplete"))
    assert "Geographic data is incomplete for this period" in html and count(html, "country") == 4


def test_geography_truncation_is_stated_without_an_invented_count():
    notes = (ri.TruncationNote("countries", "source_limit"), ri.TruncationNote("countries", "snapshot_cap"))
    html = sec_html("geography", geography=geo_visual(4, truncation=notes))
    assert "The countries list is cut off at the data source" in html and "capped when the report data was stored" in html
    assert 'class="table__caption">Countries (not a complete list)</p>' in html
    assert not re.search(r"\d+ more", html)


def test_an_exact_row_limit_count_is_stated_and_the_table_says_it_is_incomplete():
    notes = (ri.TruncationNote("countries", "row_limit", 7),)
    html = sec_html("geography", geography=geo_visual(4, truncation=notes))
    assert "Not every entry of the countries list is shown: 7 more rows left out." in html
    assert "(not a complete list)" in html


def test_a_complete_country_list_is_not_labelled_incomplete():
    assert 'class="table__caption">Countries</p>' in sec_html("geography", geography=geo_visual(4))


def test_one_country_or_no_clicks_means_a_message_instead_of_a_chart():
    one = sec_html("geography", geography=geo_visual(1))
    assert rs.NO_CHART_ONE_COUNTRY in one and 'chart--bar' not in one and count(one, "country") == 1
    zero = ri.GeographyVisuals("available", (ri.CountryRow("A", 0, 10), ri.CountryRow("B", 0, 20)))
    html = sec_html("geography", geography=zero)
    assert rs.NO_CHART_NO_CLICKS in html and 'chart--bar' not in html and count(html, "country") == 2


def test_only_the_top_five_countries_are_charted_and_listed():
    html = sec_html("geography", geography=geo_visual(25))
    assert len(re.findall(r'class="chart__bar"', html)) == 5 and count(html, "country") == 5
    assert "The chart shows the top 5 of 25 countries by impressions, as in the table." in html


def test_one_country_and_zero_click_messages_look_at_the_countries_shown():
    top_zero = tuple(ri.CountryRow(f"Z{i}", 0, 1000 - i) for i in range(5)) + (ri.CountryRow("Busy", 50, 10),)
    html = sec_html("geography", geography=ri.GeographyVisuals("available", top_zero))
    assert rs.NO_CHART_NO_CLICKS in html and "chart--bar" not in html and count(html, "country") == 5


# --- GA4 ------------------------------------------------------------------------------------------------------------------------------

def test_ga4_shows_sessions_users_and_engagement_rate():
    html = html_of(ga4=ga4_visual(sessions=1234, users=999, engagement_rate=Decimal("0.311")))
    assert tiles(html)["Sessions"] == "1,234" and tiles(html)["Users"] == "999" and tiles(html)["Engagement rate"] == "31.10%"


def test_ga4_missing_metrics_are_dashes_and_zero_is_zero():
    t = tiles(html_of(ga4=ga4_visual(users=None)))
    assert t["Users"] == "–" and t["Sessions"] == "300"
    t = tiles(html_of(ga4=ga4_visual(sessions=0, users=0, engagement_rate=Decimal("0"))))
    assert (t["Sessions"], t["Users"], t["Engagement rate"]) == ("0", "0", "0.00%")


def test_ga4_daily_gaps_are_left_blank():
    daily = tuple(ri.Ga4DailyPoint(START + dt.timedelta(days=d), 3) for d in (0, 1, 5))
    html = sec_html("ga4", ga4=ga4_visual(daily=daily))
    assert html.count("chart__marker--primary") == 3 and "25 days in this period have no record" in html
    assert html.count("<polyline") == 1


def test_ga4_channels_keep_their_order_and_values():
    channels = (ri.ChannelRow("Zeta", 5, 4, 3), ri.ChannelRow("Alpha", 90, 80, 70), ri.ChannelRow("Mid", 0, 0, 0))
    plan_ = pages(ga4=ga4_visual(channels=channels))
    assert table_rows(plan_, "channels") == [("Zeta", "5", "4", "3"), ("Alpha", "90", "80", "70"), ("Mid", "0", "0", "0")]


def test_ga4_incomplete_and_unavailable():
    html = sec_html("ga4", ga4=ga4_visual(status="incomplete"))
    assert "Google Analytics 4 data is incomplete for this period" in html and tiles(html)["Sessions"] == "300"
    html = sec_html("ga4", ga4=ri.Ga4Visuals.unavailable())
    assert "Google Analytics 4 data is not available for this report." in html and count(html, "channel") == 0
    assert "Sessions" not in tiles(html)


def test_ga4_truncated_channels_are_not_presented_as_all_channels():
    notes = (ri.TruncationNote("channels", "source_limit"), ri.TruncationNote("daily", "source_limit"))
    html = sec_html("ga4", ga4=ga4_visual(truncation=notes))
    assert "Channel performance (not a complete list of channels)" in html
    assert "The channels list is cut off at the data source" in html
    assert "The daily series is cut off at the data source and may not cover every day." in html


def test_ga4_without_daily_or_channels_omits_those_parts_cleanly():
    html = sec_html("ga4", ga4=ga4_visual(daily=(), channels=()))
    assert rs.NO_DAILY in html and count(html, "channel") == 0 and "Channel performance" not in html
    assert tiles(html)["Sessions"] == "300"


# --- recommendations and contact ---------------------------------------------------------------------------------------------------------

def test_recommendations_keep_their_order_title_and_action():
    recs = tuple(ri.RenderRecommendation(f"Title {c}", f"Action {c}.") for c in "ZAM")
    html = html_of(recommendations=recs)
    assert re.findall(r'recommendation__title">([^<]+)<', html) == ["Title Z", "Title A", "Title M"]
    assert re.findall(r'recommendation__action">([^<]+)<', html) == ["Action Z.", "Action A.", "Action M."]
    assert count(html, "recommendation") == 3 and html.count("&#10003;") == 3
    assert "Recommendation and Next Month Targeted SEO growth" in html


def test_a_long_recommendation_action_is_kept_in_full():
    action = "Improve the page. " * 90
    html = html_of(recommendations=(ri.RenderRecommendation("T", action),))
    assert action.strip() in html


def test_an_empty_recommendation_list_shows_no_recommendations_and_no_placeholder():
    html = html_of(recommendations=())
    assert count(html, "recommendation") == 0 and "Recommendation and Next Month" not in html and "&#10003;" not in html
    assert 'class="contact__heading">CONTACT</h2>' in html  # the contact block is still there


def test_the_contact_block_uses_the_sender_and_the_logo():
    html = html_of()
    block = html.split('class="contact"')[1]
    for text in ("Sample Sender", "Project Manager | Sample Co", "1 Sample Way", "Sample City, TX 00000",
                 "Cell: (000) 000-0000", "E-mail: sender@example.test", "Website: Sample Co"):
        assert text in block
    logo = Tags(block).find("img", **{"class": "logo"})[0]
    assert (logo["width"], logo["height"], logo["style"]) == ("1178", "411", "aspect-ratio: 1178 / 411;")
    assert "Jimmy" not in html and "jasperit" not in html


def _manager_sender(country="United States", **cells):
    """The sender the loader builds from a client row's Project Manager, Manager Email and Manager Phone cells and the
    regional profile of its Target Country (the shipped configuration)."""
    from seo_report import clients as cl
    values = dict(manager="Pat Manager", email="pat.manager@client.test", phone="555-010-0147")
    values.update(cells)
    s = cl.resolve_sender(cl.load_default_sender(), country, values["manager"], values["email"], values["phone"])
    return ri.RenderSender(s.name, s.title, tuple(s.address_lines), s.phone, s.email, s.website_label, s.website_url)


def _lines(html, marker='class="contact__lines">'):
    return re.findall(r"<p>(?:<a [^>]*>)?([^<]*)(?:</a>)?</p>", html.split(marker)[1].split("</div>")[0])


US_LINES = ["Pat Manager", "Project Manager | Jasper IT Inc.", "3402 E Pine Brook Way", "Houston, TX 77059",
            "Cell: 555-010-0147", "E-mail: pat.manager@client.test", "Website: Jasper IT Inc"]
AU_LINES = ["Pat Manager", "Project Manager | Jasper IT Inc.", "Ingleburn NSW 2565",
            "Cell: 555-010-0147", "E-mail: pat.manager@client.test", "Website: https://jasperitsolutions.com.au/"]


def test_the_united_states_block_has_the_reference_lines_in_order_with_the_us_address_and_link():
    html = html_of(sender=_manager_sender("United States"))
    assert _lines(html) == US_LINES
    assert '<a href="https://jasperitinc.com/">Website: Jasper IT Inc</a>' in html.split('class="contact"')[1]
    block = html.split('class="contact"')[1]
    for leftover in ("Ingleburn", "jasperitsolutions", "None", "null", "Jimmy", "281"):
        assert leftover not in block


def test_the_australian_block_has_the_australian_address_and_website_and_nothing_american():
    html = html_of(sender=_manager_sender("Australia"))
    assert _lines(html) == AU_LINES
    block = html.split('class="contact"')[1]
    assert '<a href="https://jasperitsolutions.com.au/">Website: https://jasperitsolutions.com.au/</a>' in block
    for leftover in ("Pine Brook", "Houston", "TX 77059", "jasperitinc.com", "Website: Jasper IT Inc<", "None", "null"):
        assert leftover not in block


def test_the_country_of_the_client_row_selects_the_profile_and_an_unsupported_one_is_rejected():
    from seo_report import clients as cl
    assert _manager_sender("  australia ").address_lines == ("Ingleburn NSW 2565",)
    assert _manager_sender("UNITED STATES").address_lines[0] == "3402 E Pine Brook Way"
    for country in ("USA", "New Zealand", ""):
        with pytest.raises(cl.SheetDataError):
            _manager_sender(country)


def test_the_same_manager_block_is_on_the_cover_and_on_the_contact_page():
    value = sample(sender=_manager_sender())
    html = rd.render_report_document(value, ASSETS)
    assert _lines(html, 'class="cover__sender">') == _lines(html) == US_LINES
    assert html.count("pat.manager@client.test") == 2 and html.count("555-010-0147") == 2
    au = rd.render_report_document(sample(sender=_manager_sender("Australia")), ASSETS)
    assert _lines(au, 'class="cover__sender">') == _lines(au) == AU_LINES and "Houston" not in au


def test_a_blank_email_or_phone_is_left_out_without_a_label_or_gap():
    assert _lines(html_of(sender=_manager_sender(email=""))) == [l for l in US_LINES if not l.startswith("E-mail")]
    assert _lines(html_of(sender=_manager_sender(phone="  "))) == [l for l in US_LINES if not l.startswith("Cell")]
    neither = _lines(html_of(sender=_manager_sender(email="", phone="")))
    assert neither == [l for l in US_LINES if not l.startswith(("Cell", "E-mail"))]
    assert not any(l in " ".join(neither) for l in ("None", "null", "Cell:", "E-mail:"))


def test_no_jasper_or_manager_details_are_written_into_the_source_templates():
    root = Path(__file__).resolve().parents[1]
    for path in list((root / "templates" / "report").glob("*")) + [root / "src" / "seo_report" / "report_templates.py",
                                                                    root / "src" / "seo_report" / "report_sections.py"]:
        text = path.read_text(encoding="utf-8")
        for needle in ("Jimmy", "jasperit", "281", "Pine Brook", "Houston", "Ingleburn"):
            assert needle not in text, (path.name, needle)


def test_missing_optional_contact_fields_leave_no_labels_or_blank_lines():
    html = html_of(sender=ri.RenderSender("Only Name"))
    block = html.split('class="contact"')[1]
    assert "Only Name" in block and "Cell:" not in block and "E-mail:" not in block and "Website:" not in block
    assert "<p></p>" not in block and "None" not in block and block.count("<p>") == 1


def test_a_safe_sender_url_is_a_link_and_an_unsafe_one_is_not():
    html = html_of(sender=dataclasses.replace(sample().sender, website_url="https://jasper.example"))
    assert '<a href="https://jasper.example">Website: Sample Co</a>' in html
    assert "javascript" not in html_of(sender=dataclasses.replace(sample().sender, website_url="javascript:alert(1)"))


HOSTILE = '<script>alert("x")</script> & "q" <img src=x onerror=alert(1)>'


def test_every_client_provided_value_in_the_sections_is_escaped():
    value = input_(
        recommendations=(ri.RenderRecommendation(HOSTILE, HOSTILE),),
        sender=ri.RenderSender(HOSTILE, HOSTILE, (HOSTILE,), HOSTILE, HOSTILE, HOSTILE, "https://x.example/?q=\"><script>"),
        search_console=sc_visual(), geography=ri.GeographyVisuals("available", (ri.CountryRow(HOSTILE, 5, 9), ri.CountryRow("B", 1, 1))),
        ga4=ga4_visual(channels=(ri.ChannelRow(HOSTILE, 1, 1, 1),)),
        sections=(ri.RenderSection("search_console", HOSTILE, "ok", (HOSTILE,)),) + sample().sections[2:],
        historical_statements=(ri.RenderStatement("ga4", "ga4.k", HOSTILE, "g", dt.date(2026, 9, 1), dt.date(2026, 9, 28)),))
    html = "\n".join(str(rs.render_section_page_body(p, ASSETS)) for p in rs.plan_section_pages(value, ASSETS))
    assert "<script" not in html and "<img src=x" not in html and html.count("&lt;script&gt;") >= 8


# --- historical statements -----------------------------------------------------------------------------------------------------------------

def statement(section, text="Clicks rose.", key=None):
    return ri.RenderStatement(section, key or f"{section}.k", text, "g", dt.date(2026, 9, 1), dt.date(2026, 9, 28))


def test_statements_appear_in_their_own_section_with_their_baseline():
    html = html_of(historical_statements=(statement("search_console", "SC text."), statement("geography", "Geo text."),
                                          statement("ga4", "GA text.")))
    for text, section in (("SC text.", "search_console"), ("Geo text.", "geography"), ("GA text.", "ga4")):
        assert html.count(text) == 1
        assert re.search(rf'data-section="{section}">\s*<p class="statement"[^>]*><span class="statement__text">{text}', html)
    assert "Compared with 1st Sep, 2026 to 28th Sep, 2026" in html


def test_keyword_statements_open_the_search_console_pages_because_the_keyword_pages_have_no_place_for_them():
    html = html_of(historical_statements=(statement("keywords", "Rankings improved."),))
    assert html.count("Rankings improved.") == 1
    assert html.index("Rankings improved.") < html.index("Google Search Console")


def test_a_statement_for_an_unknown_section_is_an_error_not_a_hidden_hash_input():
    with pytest.raises(rs.ContentPaginationError) as caught:
        pages(historical_statements=(statement("keywords"), statement("recommendations")))
    assert caught.value.code == "report_statement_unsupported" and caught.value.index == 1


# --- pagination and safety ---------------------------------------------------------------------------------------------------------------------

def test_a_long_country_list_gives_one_short_table_with_its_header_and_fits_the_pages():
    value = geo_visual(120)
    html = html_of(geography=value)
    plan_ = pages(geography=value)
    slices = [p for p in placements(plan_, "table") if p.block.name == "countries"]
    assert len(slices) == 1 and slices[0].first_slice and len(slices[0].block.rows) == 5
    assert [r for s in slices for r in s.block.rows] == [(c.name, f"{c.clicks:,}", f"{c.impressions:,}") for c in value.countries[:5]]
    assert count(html, "country") == 5 and html.count('data-table="countries"') == 1
    assert html.count('class="table__caption">Top 5 countries by impressions (of 120)</p>') == 1
    for body in re.findall(r'data-table="countries".*?</table>', html, re.S):
        assert re.findall(r'<th scope="col"[^>]*>([^<]+)</th>', body) == ["Country", "Clicks", "Impressions"]
    for page in plan_:
        assert page.estimated_mm <= USABLE


def test_a_tall_country_row_among_the_top_five_is_kept_in_full_and_fits_the_pages():
    tall = ri.CountryRow("Tall " + " ".join(f"word{i}" for i in range(60)), 1, 10 ** 6)
    value = ri.GeographyVisuals("available", countries(9) + (tall,))
    plan_ = pages(geography=value)
    rows = table_rows(plan_, "countries")
    assert rows[0][0] == tall.name and len(rows) == 5
    assert all(page.estimated_mm <= USABLE for page in plan_)


def test_a_table_row_that_cannot_fit_an_empty_page_is_a_structured_error():
    value = ri.GeographyVisuals("available", (ri.CountryRow("Secret-Country " + "x" * 20000, 1, 2),))
    with pytest.raises(rs.ContentPaginationError) as caught:
        pages(geography=value)
    e = caught.value
    assert (e.code, e.section, e.element, e.index, e.reason) == ("report_content_exceeds_page", "geography",
                                                                  "table_row:countries", 0, "row_exceeds_empty_page")
    assert e.required_mm > e.available_mm and "Secret-Country" not in str(e) + repr(vars(e))


def test_an_oversized_channel_row_reports_its_index():
    channels = (ri.ChannelRow("ok", 1, 1, 1), ri.ChannelRow("a" * 30000, 1, 1, 1))
    with pytest.raises(rs.ContentPaginationError) as caught:
        pages(ga4=ga4_visual(channels=channels))
    assert (caught.value.element, caught.value.index) == ("table_row:channels", 1)


def test_oversized_non_table_content_is_an_error_never_clipped():
    cases = [
        ("paragraph", dict(sections=(ri.RenderSection("search_console", "T", "ok", ("word " * 15000,)),) + sample().sections[2:])),
        ("recommendation", dict(recommendations=(ri.RenderRecommendation("T", "action " * 9000),))),
        ("contact", dict(sender=ri.RenderSender("N", address_lines=tuple(f"Line {i}" for i in range(400))))),
        ("statement", dict(historical_statements=(statement("ga4", "text " * 15000),))),
    ]
    for element, overrides in cases:
        with pytest.raises(rs.ContentPaginationError) as caught:
            pages(**overrides)
        e = caught.value
        assert (e.code, e.element, e.reason) == ("report_content_exceeds_page", element, "element_exceeds_empty_page")
        assert e.required_mm > e.available_mm


def test_the_error_is_deterministic_and_no_document_is_returned():
    value = input_(geography=ri.GeographyVisuals("available", (ri.CountryRow("x" * 20000, 1, 2),)))
    outcomes = []
    for _ in range(3):
        with pytest.raises(rs.ContentPaginationError) as caught:
            rd.render_report_document(value, ASSETS)
        e = caught.value
        outcomes.append((str(e), e.code, e.section, e.element, e.index, e.reason, e.required_mm, e.available_mm))
    assert outcomes[0] == outcomes[1] == outcomes[2]
    result = []
    with pytest.raises(rs.ContentPaginationError):
        result.append(rd.render_report_document(value, ASSETS))
    assert result == []


def test_a_heading_always_stays_with_what_follows_it():
    for n in range(0, 90, 3):
        for page in pages(geography=geo_visual(n) if n else ri.GeographyVisuals.unavailable(), search_console=sc_visual(n=min(n, 28) or 1)):
            kinds = [p.block.kind for p in page.placements]
            if kinds and kinds[-1] == "heading":
                pytest.fail("a heading ends a page")


def test_every_page_has_content_and_fits_its_estimate():
    for n in (1, 5, 30, 90):
        for page in pages(geography=geo_visual(max(n, 2)), search_console=sc_visual(n=min(n, 28)), ga4=ga4_visual(n=min(n, 28))):
            assert page.placements and page.estimated_mm <= USABLE


def test_the_section_pages_are_numbered_consecutively_from_one():
    plan_ = pages(geography=geo_visual(80))
    assert [p.number for p in plan_] == list(range(1, len(plan_) + 1))


def test_sections_start_on_their_own_pages_in_the_fixed_order():
    plan_ = pages()
    order = []
    for page in plan_:
        first_sections = [p.block.section for p in page.placements]
        for s in first_sections:
            if not order or order[-1] != s:
                order.append(s)
    assert order == ["keywords", "search_console", "geography", "ga4", "recommendations", "contact"]
    for page in plan_:
        sections = {p.block.section for p in page.placements} - {"keywords"}
        assert len(sections & {"search_console", "geography", "ga4"}) <= 1


def test_the_planner_checks_arguments_and_leaves_inputs_unchanged():
    with pytest.raises(TypeError):
        rs.plan_section_pages({"x": 1}, ASSETS)
    with pytest.raises(TypeError):
        rs.plan_section_pages(sample(), "assets")
    value = sample()
    before = dataclasses.replace(value)
    rs.plan_section_pages(value, ASSETS)
    assert value == before


def test_the_module_does_not_use_a_clock_a_browser_or_the_network():
    source = (ROOT / "src" / "seo_report" / "report_sections.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for word in ("datetime.now", "date.today", "time.", "random", "uuid", "open(", "requests", "socket", "playwright",
                 "subprocess", "os.environ", "sqlite"):
        assert word not in code, word
