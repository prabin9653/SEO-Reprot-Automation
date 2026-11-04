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
from test_keyword_pages import (ABOVE, MISSING, POSITION, cells, keyword_rows, keywords, only_body, pages_of, plan,
                                render, row)
from test_render_input import sample
from test_report_templates import ASSETS, CSS

CAPACITY = kp.fresh_page_capacity_mm(ASSETS)


def unbroken(lines: int) -> str:
    """A keyword with no spaces that needs exactly `lines` lines in the keyword column."""
    capacity = kp._column_capacity(kp.COLUMNS[0][1], kp.CHAR_MM)
    return "x" * (capacity * lines)


def max_lines_that_fit(extra: float = 0.0) -> int:
    lines = 1
    while kp.row_mm(kp.row_views((row(0, keyword=unbroken(lines + 1)),))[0]) + extra <= CAPACITY:
        lines += 1
    return lines


FITS = max_lines_that_fit()


def rows_of(plan_):
    return [r.index for page in plan_ for r in page.rows]


# --- capacity -------------------------------------------------------------------------------------------------------------------

def test_fresh_page_capacity_is_the_usable_body_less_heading_and_header_row():
    assert CAPACITY == pytest.approx(kp.usable_height_mm(ASSETS) - kp.HEADING_MM - kp.header_row_mm())
    assert CAPACITY == pytest.approx(214.9, abs=0.1)
    assert CAPACITY < kp.usable_height_mm(ASSETS) < kp.body_height_mm(ASSETS)


def test_capacity_follows_the_logo_height_through_the_header():
    taller = rt.TemplateAssets(rt.LogoAsset("a.png", 100, 100))
    assert kp.fresh_page_capacity_mm(taller) < CAPACITY


def test_the_boundary_row_exactly_fits_and_one_more_line_does_not():
    assert FITS == 40
    fits = kp.row_mm(kp.row_views((row(0, keyword=unbroken(FITS)),))[0])
    too_tall = kp.row_mm(kp.row_views((row(0, keyword=unbroken(FITS + 1)),))[0])
    assert fits <= CAPACITY < too_tall
    assert rows_of(plan(keywords=(row(0, keyword=unbroken(FITS)),), ranking_footnote=None)) == [0]
    with pytest.raises(kp.KeywordPaginationError):
        plan(keywords=(row(0, keyword=unbroken(FITS + 1)),), ranking_footnote=None)


# --- rejected rows ----------------------------------------------------------------------------------------------------------------

def test_an_extremely_long_keyword_is_rejected():
    with pytest.raises(kp.KeywordPaginationError) as caught:
        plan(keywords=(row(0, keyword="word " * 5000),))
    assert (caught.value.row_index, caught.value.column) == (0, "keyword")


def test_an_extremely_long_location_is_rejected():
    with pytest.raises(kp.KeywordPaginationError) as caught:
        plan(keywords=(row(0), row(1, location="y" * 20000)))
    assert (caught.value.row_index, caught.value.column) == (1, "location")


def test_a_combined_long_keyword_and_location_is_rejected_by_the_taller_cell():
    with pytest.raises(kp.KeywordPaginationError) as caught:
        plan(keywords=(row(0, keyword="a" * 15000, location="b" * 30000),))
    assert caught.value.column == "location"
    with pytest.raises(kp.KeywordPaginationError) as caught:
        plan(keywords=(row(0, keyword="a" * 30000, location="b" * 3000),))
    assert caught.value.column == "keyword"


def test_a_row_exceeding_the_fresh_page_height_reports_both_heights():
    with pytest.raises(kp.KeywordPaginationError) as caught:
        plan(keywords=(row(0, keyword=unbroken(60)),))  # with the default footnote: the row alone is the cause
    error = caught.value
    assert error.required_mm > error.available_mm == pytest.approx(CAPACITY, abs=0.001)
    assert error.reason == "row_exceeds_empty_page"


def test_the_error_identifies_the_original_row_index_among_many_rows():
    for bad_index in (0, 7, 41, 99):
        items = [row(i) for i in range(100)]
        items[bad_index] = row(bad_index, keyword=unbroken(FITS + 5))
        with pytest.raises(kp.KeywordPaginationError) as caught:
            plan(keywords=tuple(items))
        assert caught.value.row_index == bad_index


def test_the_first_of_several_oversized_rows_is_reported():
    items = [row(i) for i in range(10)]
    items[3] = row(3, keyword=unbroken(FITS + 1))
    items[6] = row(6, keyword=unbroken(FITS + 9))
    with pytest.raises(kp.KeywordPaginationError) as caught:
        plan(keywords=tuple(items))
    assert caught.value.row_index == 3


def test_the_last_row_must_fit_together_with_the_footnote():
    footnote = "Footnote text. " * 20
    extra = kp.footnote_mm(footnote)
    lines = max_lines_that_fit(extra)
    assert lines < FITS
    ok = row(0, keyword=unbroken(lines))
    assert rows_of(plan(keywords=(ok,), ranking_footnote=footnote)) == [0]
    boundary = row(0, keyword=unbroken(FITS))  # fits alone, not together with the footnote
    assert rows_of(plan(keywords=(boundary,), ranking_footnote=None)) == [0]
    with pytest.raises(kp.KeywordPaginationError) as caught:
        plan(keywords=(boundary,), ranking_footnote=footnote)
    assert caught.value.reason == "row_and_footnote_exceed_empty_page" and caught.value.row_index == 0
    # the same row is fine when it is not the last one (the footnote stays with the last row only)
    assert rows_of(plan(keywords=(boundary, row(1)), ranking_footnote=footnote)) == [0, 1]


# --- the error ------------------------------------------------------------------------------------------------------------------------

SECRET = "SECRET-CLIENT-KEYWORD"


def oversized_input():
    return sample(keywords=(row(0), row(1, keyword=SECRET + " " + "z" * 30000, location="Secret City")),
                  ranking_footnote="Secret footnote", client=ri.RenderClient("Secret Client Co", "https://secret.example"))


def test_the_error_code_is_stable_and_structured():
    assert kp.ERROR_ROW_EXCEEDS_PAGE == "keyword_row_exceeds_page"
    with pytest.raises(kp.KeywordPaginationError) as caught:
        kp.plan_keyword_pages(oversized_input(), ASSETS)
    error = caught.value
    assert isinstance(error, ValueError)  # the project's convention for structured errors
    assert (error.code, error.row_index, error.reason, error.column) == ("keyword_row_exceeds_page", 1,
                                                                       "row_exceeds_empty_page", "keyword")
    assert isinstance(error.required_mm, float) and isinstance(error.available_mm, float)
    assert error.reason in (kp.REASON_ROW, kp.REASON_ROW_WITH_FOOTNOTE)


def test_the_error_exposes_no_client_data():
    with pytest.raises(kp.KeywordPaginationError) as caught:
        kp.plan_keyword_pages(oversized_input(), ASSETS)
    text = str(caught.value) + repr(caught.value) + repr(vars(caught.value))
    for secret in (SECRET, "Secret City", "Secret footnote", "Secret Client", "secret.example", "zzzz"):
        assert secret not in text
    assert "keyword row 1" in text and "keyword column" in text


def test_the_failure_is_deterministic_for_identical_input():
    outcomes = []
    for _ in range(3):
        with pytest.raises(kp.KeywordPaginationError) as caught:
            kp.plan_keyword_pages(oversized_input(), ASSETS)
        e = caught.value
        outcomes.append((str(e), e.code, e.row_index, e.reason, e.column, e.required_mm, e.available_mm))
    assert outcomes[0] == outcomes[1] == outcomes[2]


def test_a_failed_render_returns_no_document():
    result = []
    with pytest.raises(kp.KeywordPaginationError):
        result.append(kp.render_keyword_report_document(oversized_input(), ASSETS))
    assert result == []


def test_the_failure_happens_before_any_output_is_written(tmp_path):
    with pytest.raises(kp.KeywordPaginationError):
        kp.render_keyword_report_document(oversized_input(), ASSETS)
    assert list(tmp_path.iterdir()) == []
    value = oversized_input()
    before = value
    with pytest.raises(kp.KeywordPaginationError):
        kp.plan_keyword_pages(value, ASSETS)
    assert value == before and len(value.keywords) == 2  # nothing was removed or altered


def test_the_rejected_row_is_never_truncated_or_altered_in_the_source():
    long_text = "x" * 30000
    value = sample(keywords=(row(0, keyword=long_text),))
    with pytest.raises(kp.KeywordPaginationError):
        kp.plan_keyword_pages(value, ASSETS)
    assert value.keywords[0].keyword == long_text


def test_the_stylesheet_gained_no_shrinking_clipping_or_hiding_rules():
    rule = re.search(r"\.keyword-table \{([^}]*)\}", CSS).group(1)
    assert "font-size: 10pt" in rule and "line-height: 1.45" in rule
    assert "padding: 3.4mm 2mm" in re.search(r"\.keyword-table th, \.keyword-table td \{([^}]*)\}", CSS).group(1)
    assert "text-overflow" not in CSS and "line-clamp" not in CSS and "ellipsis" not in CSS


# --- rows that merely do not fit the current page ----------------------------------------------------------------------------------------

def test_a_row_that_does_not_fit_the_current_page_but_fits_a_fresh_one_moves_on():
    tall = row(15, keyword=unbroken(30))
    items = tuple(row(i) for i in range(15)) + (tall,) + tuple(row(i) for i in range(16, 20))
    pages = plan(keywords=items)
    assert rows_of(pages) == list(range(20))
    first = [r.index for r in pages[0].rows]
    assert 15 not in first and first == list(range(len(first))) and len(first) < 17
    assert pages[1].rows[0].index == 15 and pages[1].heading == kp.HEADING_CONTINUED
    assert not any(p.overflow for p in pages)


def test_a_tall_but_fitting_row_alone_fills_a_page_and_the_next_rows_follow():
    items = (row(0), row(1, keyword=unbroken(FITS)), row(2), row(3, keyword=unbroken(FITS)), row(4))
    pages = plan(keywords=items)
    assert rows_of(pages) == [0, 1, 2, 3, 4]
    for page in pages:
        assert page.estimated_mm <= kp.usable_height_mm(ASSETS) and not page.overflow
    html = render(keywords=items)
    assert [i for i, _ in keyword_rows(html)] == [0, 1, 2, 3, 4]


def test_a_first_row_too_tall_for_the_first_page_but_not_for_a_fresh_one_starts_the_next_page():
    first_page_room = kp.usable_height_mm(ASSETS) - kp.HEADING_MM - kp.website_mm("https://sample.example") - kp.header_row_mm()
    lines = FITS
    while kp.row_mm(kp.row_views((row(0, keyword=unbroken(lines)),))[0]) > first_page_room:
        lines -= 1
    assert lines < FITS  # such rows exist: they fit a fresh page but not the first page
    wide = row(0, keyword=unbroken(lines + 1))
    assert kp.row_mm(kp.row_views((wide,))[0]) <= CAPACITY
    pages = plan(keywords=(wide, row(1)))
    assert rows_of(pages) == [0, 1] and pages[0].rows == () and pages[0].heading == kp.HEADING
    assert pages[1].rows[0].index == 0 and not any(p.overflow for p in pages)
    html = render(keywords=(wide, row(1)))
    assert [i for i, _ in keyword_rows(html)] == [0, 1]
    assert html.count("<thead>") == sum(bool(p.rows) for p in pages)  # a header on every page that has rows


# --- normal pagination regression -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("count", [0, 1, 20, 21, 100, 400])
def test_normal_datasets_paginate_exactly_as_before(count):
    pages = plan(count)
    assert rows_of(pages) == list(range(count))
    html = render(keywords=keywords(count))
    assert [i for i, _ in keyword_rows(html)] == list(range(count))
    assert html.count("class=\"keyword-row\"") == count
    assert not any(p.overflow for p in pages)
    for page in pages_of(html):
        if "keyword-row" in page:
            assert page.count("<thead>") == 1


def test_the_page_splits_of_standard_datasets_are_unchanged():
    assert [len(p.rows) for p in plan(20)] == [17, 3]
    assert [len(p.rows) for p in plan(21)] == [17, 4]
    assert [len(p.rows) for p in plan(30)] == [17, 13]
    assert [len(p.rows) for p in plan(100)] == [17, 17, 17, 17, 17, 15, 0]


def test_footnote_and_insights_placement_is_unchanged():
    html = render(keywords=keywords(45))
    body = only_body(html)
    assert body.rindex('data-keyword-index="44"') < body.index('class="keywords__footnote"') < body.index("keywords__insights")
    assert body.count("Keywords Ranking Insights") == 1 and body.count('class="keywords__footnote"') == 1
    pages = plan(45)
    assert sum(bool(p.footnote) for p in pages) == 1 and sum(bool(p.insights_heading) for p in pages) == 1


def test_ranking_and_movement_display_rules_are_unchanged():
    items = (row(0, previous=POSITION(12), current=POSITION(1), movement=11), row(1, previous=ABOVE, current=ABOVE, movement=None),
             row(2, previous=MISSING, current=MISSING, movement=None), row(3, location="", movement=-2),
             row(4, movement=0))
    html = render(keywords=items)
    assert cells(html, 0)[2:] == ["12", "1", "▲ 11"] and cells(html, 1)[2:] == ["Above 10", "Above 10", "–"]
    assert cells(html, 2)[2:] == ["Not recorded", "Not recorded", "–"]
    assert cells(html, 3)[1] == "–" and cells(html, 3)[4] == "▼ 2" and cells(html, 4)[4] == "0"


def test_page_numbers_stay_consecutive_after_the_cover():
    html = render(keywords=keywords(60))
    numbers = [int(n) for n in re.findall(r'data-page-number="(\d+)"', html)]
    assert numbers == list(range(2, 2 + len(numbers)))


def test_a_check_of_the_whole_dataset_passes_for_normal_rows_and_is_a_pure_function():
    views = kp.row_views(keywords(50))
    assert kp.ensure_rows_fit(views, ASSETS, 5.0) is None
    with pytest.raises(kp.KeywordPaginationError):
        kp.ensure_rows_fit(kp.row_views((row(0, keyword=unbroken(FITS + 1)),)), ASSETS)
