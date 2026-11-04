import copy
import datetime as dt
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import pdf_validation as pv

PX = pv.PX_PER_MM


def tiny_pdf(pages=2, box="0 0 594.96 841.92", fonts=("AAAAAA+DejaVuSans", "BAAAAA+DejaVuSans-Bold"),
             dates=("20261030124948+00'00'", "20261030124948+00'00'"), image=False):
    """A small, valid PDF with a correct cross-reference table, built here so the tests need no renderer."""
    objects = []
    objects.append(b"<</Type /Catalog /Pages 2 0 R>>")
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objects.append(f"<</Type /Pages /Count {pages} /Kids [{kids}]>>".encode())
    for _ in range(pages):
        objects.append(f"<</Type /Page /Parent 2 0 R /MediaBox [{box}]>>".encode())
    for font in fonts:
        objects.append(f"<</Type /Font /Subtype /TrueType /BaseFont /{font}>>".encode())
    if image:
        objects.append(b"<</Type /XObject /Subtype /Image /Width 1 /Height 1>>")
    info = f"<</Producer (Test) /CreationDate (D:{dates[0]}) /ModDate (D:{dates[1]})>>".encode()
    objects.append(info)
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<</Size {len(objects) + 1} /Root 1 0 R>>\nstartxref\n{start}\n%%EOF\n".encode()
    return out


# --- PDF inspection --------------------------------------------------------------------------------------------------------

def test_a_pdf_is_inspected():
    info = pv.inspect_pdf(tiny_pdf(pages=3, image=True))
    assert (info.version, info.page_count, info.has_image) == ("%PDF-1.4", 3, True)
    assert info.page_boxes == ((594.96, 841.92),) * 3 and info.fonts == ("DejaVuSans", "DejaVuSans-Bold")
    assert info.xref_consistent and info.ends_with_eof


def test_pages_are_counted_without_counting_the_page_tree():
    assert pv.inspect_pdf(tiny_pdf(pages=1)).page_count == 1 and pv.inspect_pdf(tiny_pdf(pages=9)).page_count == 9


def test_a4_boxes_are_recognised_with_a_small_tolerance():
    assert pv.is_a4((594.96, 841.92)) and pv.is_a4((595.28, 841.89)) and pv.is_a4((595.5, 842.25))
    assert not pv.is_a4((612, 792)) and not pv.is_a4((841.92, 594.96)) and not pv.is_a4((600, 842))
    assert pv.inspect_pdf(tiny_pdf(box="0 0 612 792")).page_boxes[0] == (612.0, 792.0)


def test_a_damaged_cross_reference_table_is_detected():
    good = tiny_pdf()
    assert pv.inspect_pdf(good).xref_consistent
    offset = re.search(rb"xref\n0 \d+\n0000000000 65535 f \n(\d{10})", good).group(1)
    broken = good.replace(offset, b"%010d" % (int(offset) + 3), 1)
    assert not pv.inspect_pdf(broken).xref_consistent
    assert not pv.inspect_pdf(good.replace(b"startxref", b"startxrex")).xref_consistent
    assert not pv.inspect_pdf(good + b"junk").ends_with_eof


@pytest.mark.parametrize("bad", [b"", b"not a pdf", "text", None, 5])
def test_non_pdf_data_is_refused(bad):
    with pytest.raises(pv.PdfValidationError):
        pv.inspect_pdf(bad)


# --- date normalisation (the R0 approach) -----------------------------------------------------------------------------------------------

def test_dates_are_replaced_in_place_with_the_same_length():
    data = tiny_pdf()
    fixed = pv.normalize_pdf_dates(data, dt.date(2026, 10, 30))
    assert len(fixed) == len(data) and fixed != data
    assert fixed.count(b"(D:20261030000000+00'00')") == 2
    assert pv.inspect_pdf(fixed).xref_consistent  # every offset still points at its object
    diffs = [i for i, (a, b) in enumerate(zip(data, fixed)) if a != b]
    assert max(diffs) - min(diffs) < 120 and len(diffs) <= 12  # only the two date strings differ


def test_renders_with_different_clock_times_normalise_to_identical_bytes():
    one = tiny_pdf(dates=("20261030124948+00'00'", "20261030124948+00'00'"))
    two = tiny_pdf(dates=("20261030125100+00'00'", "20261030125100+00'00'"))
    assert one != two
    assert pv.normalize_pdf_dates(one, dt.date(2026, 10, 30)) == pv.normalize_pdf_dates(two, dt.date(2026, 10, 30))
    assert pv.normalize_pdf_dates(one, dt.date(2026, 10, 30)) != pv.normalize_pdf_dates(one, dt.date(2026, 10, 31))


def test_normalisation_refuses_unexpected_input():
    with pytest.raises(pv.PdfValidationError):
        pv.normalize_pdf_dates(tiny_pdf().replace(b"CreationDate", b"CreatedOn").replace(b"ModDate", b"ChangedOn"),
                               dt.date(2026, 10, 30))
    with pytest.raises(pv.PdfValidationError):
        pv.normalize_pdf_dates(tiny_pdf(dates=("2026", "2026")), dt.date(2026, 10, 30))
    for bad in (dt.datetime(2026, 10, 30, 1), "2026-10-30", None):
        with pytest.raises(pv.PdfValidationError):
            pv.normalize_pdf_dates(tiny_pdf(), bad)


# --- layout analysis ----------------------------------------------------------------------------------------------------------------------

def good(kind="keywords", **changes):
    m = {"kind": kind, "pageWidth": 210 * PX, "pageHeight": 297 * PX, "bodyTop": 100.0, "bodyBottom": 1000.0,
         "clientHeight": 900.0, "scrollHeight": 900.0, "frameRight": 750.0, "contentBottom": 900.0, "contentRight": 700.0,
         "contentExtent": 700.0, "hasHeader": True, "hasFooter": True, "logoLoaded": True, "textLength": 50, "text": "x",
         "tableRows": 3, "svgCount": 0, "svgs": [], "overlaps": [], "clippedRows": [], "clippedText": [],
         "fonts": [{"family": "DejaVu Sans", "glyphs": 100, "custom": True}]}
    m.update(changes)
    return m


def codes(measures, **kw):
    return [i.code for i in pv.analyse_layout(measures, **kw)]


def test_a_clean_page_has_no_issues():
    assert pv.analyse_layout([good("cover", hasFooter=False), good(), good("report")]) == ()


def test_vertical_overflow_is_reported_with_the_measured_distance():
    issues = pv.analyse_layout([good(contentBottom=1060.0)])
    assert [i.code for i in issues] == ["vertical_overflow"] and "mm below the page body" in issues[0].detail
    assert codes([good(scrollHeight=990.0)]) == ["vertical_overflow"]
    assert codes([good(contentBottom=1000.5)]) == []  # sub-pixel rounding is tolerated


def test_horizontal_overflow_cut_rows_overlaps_and_clipped_text_are_reported():
    assert codes([good(contentRight=760.0)]) == ["horizontal_overflow"]
    assert codes([good(clippedRows=[{"table": "countries", "index": 3, "over": 40.0}])]) == ["table_row_cut"]
    assert codes([good(overlaps=[{"first": 1, "second": 2, "by": 12.0}])]) == ["overlap"]
    assert codes([good(clippedText=[{"tag": "td", "by": 9.0}])]) == ["text_clipped"]


def test_charts_blank_pages_images_headers_and_footers_are_checked():
    assert codes([good(svgs=[{"id": "c", "width": 0, "height": 10, "shapes": 5, "textOutside": 0}], svgCount=1)]) == ["broken_chart"]
    assert codes([good(svgs=[{"id": "c", "width": 600, "height": 200, "shapes": 0, "textOutside": 0}], svgCount=1)]) == ["broken_chart"]
    assert codes([good(svgs=[{"id": "c", "width": 600, "height": 200, "shapes": 9, "textOutside": 2}], svgCount=1)]) == ["chart_text_outside"]
    assert codes([good(textLength=0, tableRows=0, svgCount=0)]) == ["blank_page"]
    assert codes([good(logoLoaded=False)]) == ["logo_not_loaded"]
    assert codes([good(hasHeader=False)]) == ["missing_header"] and codes([good(hasFooter=False)]) == ["missing_footer"]
    assert codes([good("cover", hasFooter=False)]) == []


def test_page_size_fonts_blocked_requests_and_an_empty_document():
    assert codes([good(pageWidth=612 * 96 / 72)]) == ["page_not_a4"]
    assert codes([good(fonts=[{"family": "Arial", "glyphs": 4, "custom": False}])]) == ["unexpected_font"]
    assert codes([good()], blocked_requests=["x"]) == ["external_request"]
    assert codes([]) == ["no_pages"]


def test_issues_carry_the_page_number_and_are_deterministic():
    pages = [good(), good(contentRight=770.0), good(hasHeader=False)]
    first = pv.analyse_layout(pages)
    assert [(i.page, i.code) for i in first] == [(2, "horizontal_overflow"), (3, "missing_header")]
    assert first == pv.analyse_layout(copy.deepcopy(pages))


def test_analysis_does_not_change_its_input():
    pages = [good(contentBottom=1100.0)]
    before = copy.deepcopy(pages)
    pv.analyse_layout(pages)
    assert pages == before


def test_estimates_are_compared_page_by_page_and_the_cover_is_skipped():
    measures = [good("cover", contentExtent=0.0), good(contentExtent=100 * PX), good("report", contentExtent=250 * PX)]
    result = pv.compare_estimates(measures, [("cover", None), ("keywords", 98.0), ("report", 240.0)], 246.4)
    assert [(c.page, c.kind, c.estimated_mm, c.measured_mm, c.delta_mm) for c in result] == [
        (2, "keywords", 98.0, 100.0, 2.0), (3, "report", 240.0, 250.0, 10.0)]
    assert result[0].within_usable and not result[1].within_usable
    with pytest.raises(pv.PdfValidationError):
        pv.compare_estimates(measures, [("cover", None)], 246.4)


def test_the_validation_module_is_pure():
    source = (ROOT / "src" / "seo_report" / "pdf_validation.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for word in ("playwright", "open(", "subprocess", "import requests", "urllib", "socket", "os.environ", "datetime.now",
                 "date.today"):
        assert word not in code, word
