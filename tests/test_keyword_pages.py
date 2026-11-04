import dataclasses
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import keyword_pages as kp
from seo_report import render_input as ri
from seo_report import report_templates as rt
from seo_report.rankings import NOT_RECORDED
from test_render_input import sample
from test_report_templates import ASSETS, CSS, Tags, visible_text

POSITION = lambda n: ri.RenderRanking("position", n)
ABOVE = ri.RenderRanking("above_10", None, "Above 10")
MISSING = ri.RenderRanking("missing")


def row(i, **kw):
    values = dict(keyword=f"keyword {i:03d}", location=f"City {i:03d}", previous=POSITION(5), current=POSITION(3), movement=2)
    values.update(kw)
    return ri.RenderKeywordRow(**values)


def keywords(count, **kw):
    return tuple(row(i, **kw) for i in range(count))


def render(**overrides):
    return kp.render_keyword_report_document(sample(**overrides), ASSETS)


def plan(count=None, **overrides):
    if count is not None:
        overrides["keywords"] = keywords(count)
    return kp.plan_keyword_pages(sample(**overrides), ASSETS)


def keyword_rows(html):
    """(data-keyword-index, keyword text) of every rendered row, in document order."""
    return [(int(m.group(1)), m.group(2)) for m in re.finditer(
        r'<tr class="keyword-row" data-keyword-index="(\d+)">\s*<td class="keyword-table__col--left">(.*?)</td>', html, re.S)]


def pages_of(html):
    return re.findall(r'<section class="page page--keywords".*?</section>', html, re.S)


def only_body(html):
    return re.sub(r"<style>.*?</style>", "", html, flags=re.S)


# --- exact table design ----------------------------------------------------------------------------------------------------

def test_the_table_has_exactly_the_five_columns_in_order():
    html = render()
    heads = re.findall(r'<th scope="col"[^>]*>(.*?)</th>', html)
    assert heads == ["Keyword", "Location", "Previous Rank", "Current Rank", "Movement"]
    assert [c[0] for c in kp.COLUMNS] == heads
    assert sum(c[1] for c in kp.COLUMNS) == 100
    assert len(re.findall(r"<col style=", html)) == 5


def test_the_header_uses_the_verified_yellow_and_grid_colours():
    assert re.search(r"\.data-table th \{[^}]*background: var\(--table-head\)", CSS) and "--table-head: #ffff9e" in CSS
    assert "--grid: #d9d9d9" in CSS and "border: 0.2mm solid var(--grid)" in CSS
    assert re.search(r"\.keyword-table \{[^}]*table-layout: fixed", CSS)
    assert "overflow-wrap: anywhere" in re.search(r"\.keyword-table th, \.keyword-table td \{([^}]*)\}", CSS).group(1)


def test_the_css_keeps_the_verified_palette_only():
    assert set(re.findall(r"#[0-9a-fA-F]{3,8}\b", CSS)) == {"#000000", "#ffffff", "#e61001", "#d9d9d9", "#1a62ff",
                                                           "#689f38", "#ffff9e"}


def test_the_page_uses_the_r3_shell_with_header_footer_and_numbers():
    html = render(keywords=keywords(3))
    tags = Tags(html)
    assert len(tags.find("section", **{"data-page-kind": "keywords"})) == 1
    assert len(tags.find("header")) == 2 and len(tags.find("footer")) == 1  # cover header + keyword page header/footer
    assert 'data-page-number="2">Page 2<' in html and '<span class="page-footer__client">Sample Roofing Co</span>' in html
    assert 'data-page-kind="cover"' in html


# --- datasets: every keyword, original order ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("count", [1, 10, 20, 21, 30, 100, 400])
def test_every_keyword_is_rendered_once_in_the_original_order(count):
    items = keywords(count)
    html = render(keywords=items)
    rows = keyword_rows(html)
    assert len(rows) == count
    assert [i for i, _ in rows] == list(range(count))
    assert [t for _, t in rows] == [k.keyword for k in items]
    assert html.count('class="keyword-row"') == count


def test_zero_keywords_draws_one_page_with_a_message_and_no_table():
    html = render(keywords=())
    assert "<table" not in html and keyword_rows(html) == []
    assert f'<p class="keywords__empty">{kp.NO_KEYWORDS}</p>' in html
    assert len(pages_of(html)) == 1 and kp.HEADING in html


def test_a_standard_twenty_keyword_report_has_two_keyword_pages():
    pages = plan(20)
    assert len(pages) == 2 and sum(len(p.rows) for p in pages) == 20
    assert len(pages_of(render(keywords=keywords(20)))) == 2


def test_twenty_one_and_thirty_keywords_continue_onto_further_pages_without_a_fixed_cap():
    for count in (21, 30):
        pages = plan(count)
        assert sum(len(p.rows) for p in pages) == count and len(pages) >= 2
    assert len(plan(60)) > len(plan(30)) > 1
    assert len(plan(400)) > 10  # no hardcoded maximum: a large dataset simply needs more pages


def test_one_and_ten_keywords_fit_on_one_page_with_the_insights():
    for count in (1, 10):
        pages = plan(count)
        assert len(pages) == 1 and len(pages[0].rows) == count and pages[0].paragraphs


def test_row_capacity_comes_from_the_geometry_not_a_constant():
    usable = kp.usable_height_mm(ASSETS)
    assert 240 < usable < 260  # A4 body minus header, footer and the safety reserve
    one_line = kp.row_mm(kp.row_views(keywords(1))[0])
    assert 11.5 < one_line < 13
    for page in plan(120):
        if not page.overflow:
            assert page.estimated_mm <= usable
    assert kp.body_height_mm(rt.TemplateAssets(rt.LogoAsset("a.png", 100, 100))) < kp.body_height_mm(ASSETS)  # a taller logo leaves less room


# --- continuation pages --------------------------------------------------------------------------------------------------------------

def test_every_page_with_rows_repeats_the_complete_header():
    html = render(keywords=keywords(60))
    pages = pages_of(html)
    assert len(pages) >= 3
    for page in pages:
        if "keyword-row" in page:
            assert page.count("<thead>") == 1
            assert re.findall(r'<th scope="col"[^>]*>(.*?)</th>', page) == [c[0] for c in kp.COLUMNS]
    assert html.count("<thead>") == sum("keyword-row" in p for p in pages)


def test_headings_continued_and_the_website_only_on_the_first_page():
    pages = plan(60)
    assert pages[0].heading == kp.HEADING and pages[0].website == "https://sample.example"
    assert all(p.heading == kp.HEADING_CONTINUED for p in pages[1:] if p.rows)
    assert all(p.website is None for p in pages[1:])
    html = render(keywords=keywords(60))
    assert html.count('class="keywords__website"') == 1


def test_no_row_is_split_a_row_lives_in_exactly_one_table_cell_row():
    html = render(keywords=keywords(60))
    for page in pages_of(html):
        assert page.count("<tr") == page.count("</tr>") and page.count("<tbody>") == page.count("</tbody>")
        assert page.count("<tr") == page.count('<tr class="keyword-row"') + (1 if "<thead>" in page else 0)
    assert re.search(r"\.data-table tr \{[^}]*break-inside: avoid", CSS)  # and the stylesheet forbids a split too


def test_pages_are_numbered_consecutively_after_the_cover():
    html = render(keywords=keywords(60))
    numbers = [int(n) for n in re.findall(r'data-page-number="(\d+)"', html)]
    assert numbers == list(range(2, 2 + len(numbers))) and len(numbers) == len(plan(60))


def test_long_rows_get_fewer_rows_per_page_than_short_rows():
    short = plan(60)
    long_text = "word " * 30
    long_pages = kp.plan_keyword_pages(sample(keywords=tuple(row(i, keyword=long_text, location=long_text) for i in range(60))), ASSETS)
    assert max(len(p.rows) for p in long_pages) < max(len(p.rows) for p in short)
    assert sum(len(p.rows) for p in long_pages) == 60


def test_a_row_taller_than_a_page_is_an_explicit_error_not_a_clipped_row():
    giant = row(0, keyword="x" * 20000)
    with pytest.raises(kp.KeywordPaginationError) as caught:
        kp.plan_keyword_pages(sample(keywords=(giant, row(1))), ASSETS)
    assert caught.value.code == "keyword_row_exceeds_page" and caught.value.row_index == 0
    with pytest.raises(kp.KeywordPaginationError):
        render(keywords=(row(1), giant))


# --- display rules -----------------------------------------------------------------------------------------------------------------------

def cells(html, index):
    block = re.search(rf'<tr class="keyword-row" data-keyword-index="{index}">(.*?)</tr>', html, re.S).group(1)
    return [re.sub(r"\s+", " ", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", block, re.S)]


def test_numeric_rankings_display_as_numbers():
    html = render(keywords=(row(0, previous=POSITION(12), current=POSITION(1)),))
    assert cells(html, 0)[2:4] == ["12", "1"]


def test_above_10_is_displayed_consistently_and_keeps_the_supplied_wording():
    html = render(keywords=(row(0, previous=ABOVE, current=ABOVE),
                            row(1, previous=ri.RenderRanking("above_10", None, "above 10+"), current=ri.RenderRanking("above_10"))))
    assert cells(html, 0)[2:4] == ["Above 10", "Above 10"]
    assert cells(html, 1)[2:4] == ["above 10+", "Above 10"]  # the sheet's text when supplied, the standard text otherwise


def test_missing_rankings_are_not_recorded_never_zero():
    html = render(keywords=(row(0, previous=MISSING, current=MISSING, movement=None),))
    assert cells(html, 0)[2:4] == [NOT_RECORDED, NOT_RECORDED] == ["Not recorded", "Not recorded"]
    assert ">0<" not in html.split("<tbody>")[1]


def test_blank_locations_use_one_neutral_placeholder_and_no_place_is_invented():
    html = render(keywords=(row(0, location=""), row(1, location="   "), row(2, location="Sample City")))
    assert [cells(html, i)[1] for i in range(3)] == ["–", "–", "Sample City"]


def test_movement_distinguishes_improvement_decline_no_change_and_missing():
    items = (row(0, movement=3), row(1, movement=-2), row(2, movement=0), row(3, movement=None),
             row(4, movement=1), row(5, movement=-1))
    html = render(keywords=items)
    assert [cells(html, i)[4] for i in range(6)] == ["▲ 3", "▼ 2", "0", "–", "▲ 1", "▼ 1"]
    assert 'keyword-table__move--up" aria-label="Improved by 3 positions"' in html
    assert 'keyword-table__move--down" aria-label="Declined by 2 positions"' in html
    assert 'keyword-table__move--none" aria-label="No change"' in html
    assert 'keyword-table__move--missing" aria-label="No movement available"' in html
    assert 'aria-label="Improved by 1 position"' in html and 'aria-label="Declined by 1 position"' in html


def test_movement_is_taken_from_the_model_and_never_calculated():
    inconsistent = row(0, previous=POSITION(10), current=POSITION(2), movement=None)  # numeric rankings, no movement given
    assert cells(render(keywords=(inconsistent,)), 0)[4] == "–"
    contradicting = row(1, previous=POSITION(2), current=POSITION(10), movement=5)  # the value given is shown as given
    assert cells(render(keywords=(contradicting,)), 0)[4] == "▲ 5"
    non_numeric = row(2, previous=ABOVE, current=POSITION(4), movement=None)
    assert cells(render(keywords=(non_numeric,)), 0)[4] == "–"


def test_source_values_and_the_hash_inputs_are_not_altered_by_rendering():
    from seo_report import render_input_hash as rh
    value = sample(keywords=keywords(25))
    before = (value, rh.render_input_sha256(value))
    kp.render_keyword_report_document(value, ASSETS)
    assert (value, rh.render_input_sha256(value)) == before


def test_movement_and_placeholder_views():
    assert kp.movement_view(None) == kp.MovementView("missing", "–", "No movement available")
    assert kp.movement_view(4).kind == "up" and kp.movement_view(-4).kind == "down" and kp.movement_view(0).kind == "none"
    assert kp.rank_text(ri.RenderRanking("above_10")) == "Above 10"
    assert kp.rank_text(ri.RenderRanking("missing")) == "Not recorded" and kp.rank_text(POSITION(7)) == "7"


def test_long_keywords_and_locations_wrap_and_stay_in_the_cell():
    long_word = "supercalifragilistic" * 8
    location = "Very Long Location Name " * 5
    html = render(keywords=(row(0, keyword=long_word, location=location),))
    assert cells(html, 0)[0] == long_word and cells(html, 0)[1] == location.strip()
    assert "overflow-wrap: anywhere" in CSS and "table-layout: fixed" in CSS
    taller = kp.row_mm(kp.row_views((row(0, keyword=long_word),))[0])
    assert taller > kp.row_mm(kp.row_views(keywords(1))[0])


def test_wrapped_line_estimate():
    assert kp.wrapped_lines("", 10) == 1 and kp.wrapped_lines("short", 10) == 1
    assert kp.wrapped_lines("aaaa bbbb", 8) == 2 and kp.wrapped_lines("aaaa bbbb", 9) == 1
    assert kp.wrapped_lines("x" * 25, 10) == 3 and kp.wrapped_lines("ab " + "x" * 25, 10) == 4
    assert kp.wrapped_lines("a\nb", 10) == 2 and kp.wrapped_lines("abc", 0) == 3


# --- footnote and insights ------------------------------------------------------------------------------------------------------------

def test_the_footnote_is_shown_when_supplied_and_omitted_cleanly_otherwise():
    html = render(keywords=keywords(3))
    assert '<p class="keywords__footnote">Rankings are positions in Google results.</p>' in html
    none = render(keywords=keywords(3), ranking_footnote=None)
    blank = render(keywords=keywords(3), ranking_footnote="  ")
    for html_ in (none, blank):
        assert "keywords__footnote" not in only_body(html_) and "<p></p>" not in html_


def test_the_footnote_follows_the_last_row_and_precedes_the_insights():
    html = render(keywords=keywords(45))
    body = only_body(html)
    assert body.count('class="keywords__footnote"') == 1
    assert body.rindex('data-keyword-index="44"') < body.index('class="keywords__footnote"') < body.index("keywords__insights")


def test_the_footnote_stays_on_the_page_of_the_last_row():
    for count in range(1, 90):
        pages = plan(count)
        assert [bool(p.footnote) for p in pages if p.rows][-1] is True
        assert sum(bool(p.footnote) for p in pages) == 1
        assert pages[-1].footnote or not pages[-1].rows or pages[-1].paragraphs


def test_insights_appear_once_after_the_final_keyword_page():
    html = render(keywords=keywords(60))
    pages = pages_of(html)
    body = only_body(html)
    assert body.count("Keywords Ranking Insights") == 1
    holders = [i for i, p in enumerate(pages) if "keywords__insights" in p]
    assert holders == [len(pages) - 1]
    assert max(i for i, p in enumerate(pages) if "keyword-row" in p) <= holders[0]
    assert body.rindex('data-keyword-index="59"') < body.index("Keywords Ranking Insights")
    for paragraph in ("First paragraph.", "Second paragraph."):
        assert body.count(paragraph) == 1


def test_insight_paragraphs_keep_their_order_and_text_exactly():
    section = ri.RenderSection("keywords", "Keyword Rankings", "ok", ("Zebra paragraph first.", "Alpha paragraph second.", "Third."))
    sections = (section,) + sample().sections[1:]
    html = render(keywords=keywords(5), sections=sections)
    positions = [html.index(t) for t in ("Zebra paragraph first.", "Alpha paragraph second.", "Third.")]
    assert positions == sorted(positions)
    assert [p for page in plan(5, sections=sections) for p in page.paragraphs] == list(section.paragraphs)


def test_the_insights_heading_stays_with_its_first_paragraph():
    html = render(keywords=keywords(5))
    assert re.search(r'<div class="keep-together">\s*<h2 class="keywords__insights-heading">Keywords Ranking Insights</h2>\s*<p>First paragraph\.</p>\s*</div>', html)
    assert re.search(r"\.keep-together \{[^}]*break-inside: avoid", CSS)


def test_insights_that_do_not_fit_move_to_a_following_page_together_with_their_heading():
    big = tuple(" ".join(f"word{i}" for i in range(130)) + f" end{k}." for k in range(2))
    sections = (ri.RenderSection("keywords", "Keyword Rankings", "ok", big),) + sample().sections[1:]
    moved = continued = False
    for count in range(1, 120):
        pages = plan(count, sections=sections)
        last_rows = max(i for i, p in enumerate(pages) if p.rows)
        heading_pages = [i for i, p in enumerate(pages) if p.insights_heading]
        assert heading_pages and len(heading_pages) == 1 and heading_pages[0] >= last_rows
        holder = pages[heading_pages[0]]
        assert holder.paragraphs[0] == big[0]  # the heading is always on the page of the first paragraph
        assert all(p.insights_heading is None for i, p in enumerate(pages) if i != heading_pages[0])
        assert [x for p in pages for x in p.paragraphs] == list(big)
        if heading_pages[0] > last_rows:
            moved = True
            assert not holder.rows and holder.heading is None and pages[last_rows].paragraphs == ()
        if any(p.paragraphs and not p.insights_heading for p in pages):
            continued = True
    assert moved and continued  # both situations occur for some table length


def test_a_page_never_holds_more_than_it_can_hold_when_insights_follow_the_rows():
    usable = kp.usable_height_mm(ASSETS)
    for count in range(0, 130):
        for page in plan(count):
            assert page.overflow or page.estimated_mm <= usable, (count, page.number)


def test_insights_are_omitted_cleanly_when_there_is_no_keywords_section_or_it_is_unavailable():
    others = sample().sections[1:]
    for sections in (others, (ri.RenderSection("keywords", "Keyword Rankings", "unavailable", ()),) + others,
                     (ri.RenderSection("keywords", "Keyword Rankings", "ok", ()),) + others):
        html = render(keywords=keywords(5), sections=sections)
        assert "Keywords Ranking Insights" not in html and "keywords__insights" not in only_body(html)
        assert "<h2 class=\"keywords__insights-heading\"></h2>" not in html


def test_other_sections_paragraphs_are_not_used():
    html = render(keywords=keywords(5))
    for text in ("Search paragraph.", "Analytics paragraph."):
        assert text not in html


# --- escaping, determinism, placeholders -----------------------------------------------------------------------------------------------------

HOSTILE = '<script>alert("x")</script> & "q" \'s\' <img src=x onerror=alert(1)>'


def test_every_client_provided_value_is_escaped():
    items = (row(0, keyword=HOSTILE, location=HOSTILE, previous=ri.RenderRanking("above_10", None, HOSTILE)),)
    section = ri.RenderSection("keywords", "Keyword Rankings", "ok", (HOSTILE,))
    html = render(keywords=items, sections=(section,) + sample().sections[1:], ranking_footnote=HOSTILE,
                  client=ri.RenderClient(HOSTILE, "https://sample.example/?a=1&b=<2>"))
    assert "<script" not in html and "<img src=x" not in html
    assert html.count("&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;") >= 5
    assert Tags(html).find("table")


def test_the_same_input_always_gives_identical_html():
    for count in (0, 1, 20, 21, 45):
        assert render(keywords=keywords(count)) == render(keywords=keywords(count))
    assert plan(45) == plan(45)


def test_no_none_null_or_empty_placeholders_in_any_dataset():
    for count in (0, 1, 10, 20, 21, 30, 90):
        html = render(keywords=keywords(count))
        text = visible_text(html)
        assert not re.search(r"\bNone\b|\bnull\b|\bNaN\b|\bundefined\b", text)
        assert not re.search(r"<p>\s*</p>|<td[^>]*>\s*</td>|<th[^>]*>\s*</th>|<h2[^>]*>\s*</h2>|<div class=\"keywords__insights\">\s*</div>", html)
        assert "{{" not in html and "{%" not in html
    sparse = render(keywords=(row(0, location="", previous=MISSING, current=MISSING, movement=None),),
                    ranking_footnote=None, sections=sample().sections[1:])
    assert not re.search(r"\bNone\b|\bnull\b", visible_text(sparse))


def test_the_planner_checks_its_arguments_and_does_not_mutate_inputs():
    with pytest.raises(TypeError):
        kp.plan_keyword_pages({"keywords": []}, ASSETS)
    with pytest.raises(TypeError):
        kp.plan_keyword_pages(sample(), "assets")
    value = sample(keywords=keywords(30))
    before = dataclasses.replace(value)
    kp.plan_keyword_pages(value, ASSETS)
    assert value == before


def test_the_module_is_pure_html_generation():
    source = (ROOT / "src" / "seo_report" / "keyword_pages.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for word in ("datetime", "time.", "random", "uuid", "os.environ", "open(", "requests", "socket", "sqlite",
                 "playwright", "subprocess", ".write(", "chromium", ".pdf"):
        assert word not in code, word
