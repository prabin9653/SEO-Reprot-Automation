import re
import sys
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
from test_report_sections import ga4_visual, geo_visual, sc_visual, statement
from test_report_templates import ASSETS, CSS, visible_text


def full(**overrides):
    values = dict(
        keywords=tuple(ri.RenderKeywordRow(f"keyword {i:03d}", f"City {i:03d}", ri.RenderRanking("position", 5),
                                           ri.RenderRanking("position", 3), 2) for i in range(45)),
        search_console=sc_visual(n=28), geography=geo_visual(40), ga4=ga4_visual(n=28),
        recommendations=tuple(ri.RenderRecommendation(f"Recommendation {i}", f"Do thing {i}.") for i in range(6)),
        historical_statements=(statement("keywords", "KW history."), statement("search_console", "SC history."),
                               statement("geography", "Geo history."), statement("ga4", "GA history.")))
    values.update(overrides)
    return sample(**values)


def pages_of(html):
    return re.findall(r'<section class="page page--[a-z]+" data-page-kind="[a-z]+">.*?</section>', html, re.S)


def numbers(html):
    return [int(n) for n in re.findall(r'data-page-number="(\d+)"', html)]


def test_the_complete_report_has_every_section_in_the_fixed_order():
    html = rd.render_report_document(full(), ASSETS)
    kinds = re.findall(r'<section class="page page--[a-z]+" data-page-kind="([a-z]+)"', html)
    assert kinds[0] == "cover" and set(kinds[1:]) == {"keywords", "report"}
    assert kinds == sorted(kinds, key={"cover": 0, "keywords": 1, "report": 2}.get)
    order = []
    for section in re.findall(r'<div class="block [^"]*" data-section="([a-z_0-9]+)"', html):
        if not order or order[-1] != section:
            order.append(section)
    assert order == ["keywords", "search_console", "geography", "ga4", "recommendations", "contact"]
    assert 'data-table="daily"' not in html and "Daily performance" not in html
    assert html.index('data-keyword-index="44"') < html.index("Keywords Ranking: history") < html.index("chart-sc-impressions")
    assert html.index("chart-sc-impressions") < html.index('data-table="countries"') < html.index('data-table="channels"')
    assert html.index('data-table="channels"') < html.index("Recommendation and Next Month") < html.index("CONTACT")


def test_page_numbers_run_from_two_without_gaps_or_repeats_after_the_cover():
    html = rd.render_report_document(full(), ASSETS)
    plan = rd.plan_report(full(), ASSETS)
    assert numbers(html) == list(range(2, plan.page_count + 1))
    assert len(pages_of(html)) == plan.page_count == 1 + len(plan.keyword_pages) + len(plan.section_pages)


def test_no_page_is_blank_and_each_page_has_a_header_and_a_footer_except_the_cover():
    html = rd.render_report_document(full(), ASSETS)
    pages = pages_of(html)
    assert len(pages) > 7  # nothing forces seven pages: the content decides
    for page in pages:
        assert page.count('<header class="page-header">') == 1
        if 'data-page-kind="cover"' in page:
            assert "<footer" not in page
        else:
            assert page.count("<footer") == 1
            assert re.search(r'<tr class="keyword-row"|<div class="block |keywords__empty|keywords__insights', page)


def test_every_source_record_is_rendered_exactly_once():
    value = full()
    html = rd.render_report_document(value, ASSETS)
    assert html.count('class="keyword-row"') == len(value.keywords) == 45
    assert html.count('data-record="daily"') == 0 and len(value.search_console.daily) == 28  # charted, not tabulated (R8.7h.4)
    assert html.count('data-record="country"') == 5 and len(value.geography.countries) == 40  # the top 5 are shown
    assert html.count('data-record="channel"') == len(value.ga4.channels) == 2
    assert html.count('data-record="recommendation"') == len(value.recommendations) == 6
    assert html.count('data-record="statement"') == len(value.historical_statements) == 4
    paragraphs = sum(len(s.paragraphs) for s in value.sections if s.key in ("search_console", "geography", "ga4"))
    assert html.count('data-record="paragraph"') == paragraphs
    assert html.count("CONTACT</h2>") == 1


def test_no_section_or_content_is_duplicated():
    html = rd.render_report_document(full(), ASSETS)
    for text in ("KW history.", "SC history.", "Geo history.", "GA history.", "Search paragraph.", "Analytics paragraph.",
                 "Keywords Ranking Insights", "Recommendation and Next Month Targeted SEO growth"):
        assert html.count(text) == 1, text
    for chart in ("chart-sc-clicks", "chart-sc-impressions", "chart-geo-clicks", "chart-ga4-sessions"):
        assert html.count(f'<title id="{chart}-title">') == 1
    for heading in ("Google Search Console", "Geographical Performance", "Google Analytics 4"):
        assert html.count(f'<h2 class="section__heading">{heading}</h2>') == 1


def test_all_four_svg_charts_are_present_once():
    html = rd.render_report_document(full(), ASSETS)
    assert html.count("<svg") == 4 and html.count('class="chart chart--line"') == 3 and html.count('class="chart chart--bar"') == 1


def test_every_table_in_the_whole_report_has_its_header_and_the_country_table_is_short():
    html = rd.render_report_document(full(geography=geo_visual(150)), ASSETS)
    heads = re.findall(r"<thead>\s*<tr>(.*?)</tr>", html, re.S)
    assert len(heads) == html.count("<thead>")
    kinds = [re.findall(r">([^<]+)</th>", h) for h in heads]
    assert kinds.count(["Country", "Clicks", "Impressions"]) == 1
    assert ["Date", "Clicks", "Impressions"] not in kinds
    assert html.count('class="keyword-row"') == 45 and html.count('data-record="country"') == 5


def test_the_other_sections_are_unaffected_by_the_layout_refinements():
    html = rd.render_report_document(full(), ASSETS)
    assert html.count('data-record="channel"') == 2 and html.count('data-record="recommendation"') == 6
    assert html.count('class="keyword-row"') == 45 and html.count("<svg") == 4 and html.count("CONTACT</h2>") == 1


def test_the_document_is_deterministic():
    assert rd.render_report_document(full(), ASSETS) == rd.render_report_document(full(), ASSETS)
    assert rd.plan_report(full(), ASSETS) == rd.plan_report(full(), ASSETS)


def test_no_none_null_or_empty_placeholders_in_the_complete_report():
    unavailable = full(search_console=ri.SearchConsoleVisuals.unavailable(), geography=ri.GeographyVisuals.unavailable(),
                       ga4=ri.Ga4Visuals.unavailable(), recommendations=(), keywords=(), ranking_footnote=None)
    partial = full(search_console=sc_visual(position=None, status="incomplete"), ga4=ga4_visual(users=None, status="incomplete"))
    for value in (full(), sample(), unavailable, partial):
        html = rd.render_report_document(value, ASSETS)
        text = visible_text(html)
        assert not re.search(r"\bNone\b|\bnull\b|\bNaN\b|\bundefined\b", text), text[:200]
        assert not re.search(r"<p>\s*</p>|<td[^>]*>\s*</td>|<th[^>]*>\s*</th>|<h2[^>]*>\s*</h2>", html)
        assert "{{" not in html and "{%" not in html and "&amp;amp;" not in html


def test_a_report_with_everything_unavailable_keeps_every_heading_and_the_contact_block():
    html = rd.render_report_document(full(search_console=ri.SearchConsoleVisuals.unavailable(), geography=ri.GeographyVisuals.unavailable(),
                                          ga4=ri.Ga4Visuals.unavailable(), recommendations=()), ASSETS)
    for heading in ("Google Search Console", "Geographical Performance", "Google Analytics 4"):
        assert heading in html
    assert html.count("data is not available for this report.") == 3 and "CONTACT" in html and "<svg" not in html


def test_an_oversized_element_anywhere_stops_the_whole_document():
    bad_inputs = [
        full(keywords=(ri.RenderKeywordRow("x" * 30000, "L", ri.RenderRanking("missing"), ri.RenderRanking("missing"), None),)),
        full(geography=ri.GeographyVisuals("available", (ri.CountryRow("y" * 30000, 1, 1),))),
        full(recommendations=(ri.RenderRecommendation("T", "z " * 20000),))]
    for value in bad_inputs:
        with pytest.raises((kp.KeywordPaginationError, rs.ContentPaginationError)):
            rd.render_report_document(value, ASSETS)
        with pytest.raises((kp.KeywordPaginationError, rs.ContentPaginationError)):
            rd.plan_report(value, ASSETS)


def test_the_complete_report_does_not_change_its_inputs():
    from seo_report import render_input_hash as rh
    value = full()
    before = (value, rh.render_input_sha256(value))
    rd.render_report_document(value, ASSETS)
    assert (value, rh.render_input_sha256(value)) == before


def test_the_stylesheet_still_uses_only_the_verified_palette_after_r5():
    assert set(re.findall(r"#[0-9a-fA-F]{3,8}\b", CSS)) == {"#000000", "#ffffff", "#e61001", "#d9d9d9", "#1a62ff",
                                                           "#689f38", "#ffff9e"}
    assert "@font-face" not in CSS and "url(" not in CSS and "text-overflow" not in CSS and "line-clamp" not in CSS


def test_the_rendering_modules_use_no_browser_clock_or_network():
    for name in ("report_document", "report_sections", "report_charts"):
        source = (ROOT / "src" / "seo_report" / f"{name}.py").read_text(encoding="utf-8")
        code = re.sub(r'""".*?"""', "", source, flags=re.S)
        for word in ("playwright", "chromium", "subprocess", "requests", "urllib", "socket", "datetime.now", "date.today",
                     "os.environ", ".write(", ".pdf"):
            assert word not in code, (name, word)
