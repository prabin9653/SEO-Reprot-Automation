"""Real Chromium renders of SYNTHETIC reports into temporary folders (never the project's folders)."""
import dataclasses
import hashlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import matplotlib

from seo_report import asset_fingerprints as af
from seo_report import keyword_pages as kp
from seo_report import pdf_renderer as pr
from seo_report import pdf_validation as pv
from seo_report import render_input as ri
from seo_report import report_sections as rs
from test_render_input import sample
from test_report_document import full

FONT_DIR = Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf"
LOGO = ROOT / "assets" / "jasper-logo.png"
FONT_FILES = {"DejaVuSans": FONT_DIR / "DejaVuSans.ttf", "DejaVuSans-Bold": FONT_DIR / "DejaVuSans-Bold.ttf"}
SOURCES = (pr.FontSource("DejaVuSans", FONT_FILES["DejaVuSans"], weight="400"),
           pr.FontSource("DejaVuSans-Bold", FONT_FILES["DejaVuSans-Bold"], weight="700"))


def with_fingerprints(value):
    fp = af.measure_fingerprints(template_version="r6.0", logo_path=LOGO, font_paths=FONT_FILES)
    return dataclasses.replace(value, fingerprints=fp)


def render(value, workdir):
    return pr.render_report_pdf(with_fingerprints(value), workdir=workdir, logo_path=LOGO, fonts=SOURCES)


@pytest.fixture(scope="module")
def standard(tmp_path_factory):
    work = tmp_path_factory.mktemp("standard")
    try:
        return render(sample(), work)
    except Exception as err:  # the browser is not available here: this is not a defect of the renderer
        if type(err).__module__.startswith("playwright"):
            pytest.skip("Chromium could not be started")
        raise


@pytest.fixture(scope="module")
def long_report(tmp_path_factory):
    return render(full(), tmp_path_factory.mktemp("long"))


def hash_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- the standard report ----------------------------------------------------------------------------------------------------------

def test_the_standard_report_renders_six_a4_pages_in_order(standard):
    assert standard.pdf.page_count == 6 and len(standard.measures) == 6
    assert standard.page_kinds == ("cover", "keywords", "report", "report", "report", "report")
    assert set(standard.pdf.page_boxes) == {(594.96, 841.92)} and all(pv.is_a4(b) for b in standard.pdf.page_boxes)
    assert standard.pdf.xref_consistent and standard.pdf.ends_with_eof and standard.pdf.version == "%PDF-1.4"
    assert standard.pdf.has_image  # the logo


def test_each_page_has_its_expected_content(standard):
    texts = standard.page_texts
    assert "SEARCH ENGINE OPTIMIZATION REPORT" in texts[0] and "Sample Roofing Co" in texts[0]
    assert "Sample Sender" in texts[0] and "30th Oct, 2026" in standard.html_path.read_text(encoding="utf-8")  # the date is in the header
    assert "roof repair" in texts[1] and "Previous Rank" in texts[1]
    joined = "\n".join(texts[2:])
    for heading in ("Google Search Console", "Geographical Performance", "Google Analytics 4", "CONTACT",
                    "Recommendation and Next Month Targeted SEO growth"):
        assert joined.count(heading) == 1, heading
    order = [joined.index(h) for h in ("Google Search Console", "Geographical Performance", "Google Analytics 4",
                                       "Recommendation and Next Month", "CONTACT")]
    assert order == sorted(order)
    html = standard.html_path.read_text(encoding="utf-8")
    assert [int(n) for n in re.findall(r'data-page-number="(\d+)"', html)] == [2, 3, 4, 5, 6]


def test_the_actual_font_is_the_bundled_dejavu_sans(standard):
    assert [f[0] for f in standard.fonts_used] == ["DejaVu Sans"] and standard.fonts_used[0][2] is True  # a downloaded font
    assert standard.fonts_used[0][1] > 1000
    assert standard.pdf.fonts == ("DejaVuSans", "DejaVuSans-Bold")  # both weights embedded as subsets
    assert not any("Arial" in f or "Times" in f for f in standard.pdf.fonts)


def test_no_request_left_the_machine(standard):
    assert standard.blocked_requests == 0
    html = standard.html_path.read_text(encoding="utf-8")
    assert not re.search(r'(?:src|srcset)="https?://|url\(["\']?https?://|<link|<script', html)


def test_the_standard_report_has_zero_layout_issues(standard):
    assert standard.issues == () and standard.ok
    for m in standard.measures:  # the clipped final date label of the line charts (R5.1) is gone
        assert all(s["textOutside"] == 0 for s in m["svgs"])
        assert m["contentRight"] <= m["frameRight"] + 0.75


def test_measured_layout_matches_the_planned_estimates(standard):
    assert len(standard.estimates) == 5  # every page except the cover
    for item in standard.estimates:
        assert abs(item.delta_mm) < 8.0 and item.measured_mm <= item.usable_mm
    assert standard.capacity_warnings == ()


def test_every_page_has_a_header_and_every_page_but_the_cover_a_footer(standard):
    for index, m in enumerate(standard.measures):
        assert m["hasHeader"] and m["logoLoaded"] and m["textLength"] > 0
        assert m["hasFooter"] == (index > 0)
        assert abs(m["pageWidth"] - 793.7) < 0.8 and abs(m["pageHeight"] - 1122.5) < 0.8
        assert m["contentBottom"] <= m["bodyBottom"] + 0.75 and not m["clippedRows"] and not m["overlaps"]


def test_the_charts_are_drawn_and_none_is_empty(standard):
    svgs = [s for m in standard.measures for s in m["svgs"]]
    assert len(svgs) == 4 and all(s["width"] > 600 and s["height"] > 40 and s["shapes"] > 5 for s in svgs)


def test_the_work_folder_holds_only_the_expected_files(standard):
    names = sorted(p.relative_to(standard.html_path.parent).as_posix() for p in standard.html_path.parent.rglob("*") if p.is_file())
    assert names == ["assets/jasper-logo.png", "fonts/DejaVuSans-Bold.ttf", "fonts/DejaVuSans.ttf", "report.html",
                     "report.pdf", "report.raw.pdf"]
    assert ROOT not in standard.pdf_path.resolve().parents


# --- a longer report ------------------------------------------------------------------------------------------------------------------

def test_the_long_report_has_continuation_pages_in_the_right_order(long_report):
    assert long_report.pdf.page_count == len(long_report.measures) == 8
    kinds = long_report.page_kinds
    assert kinds[0] == "cover" and kinds.count("keywords") == 3 and kinds.count("report") == 4
    assert list(kinds) == sorted(kinds, key={"cover": 0, "keywords": 1, "report": 2}.get)
    assert set(long_report.pdf.page_boxes) == {(594.96, 841.92)}


def test_every_keyword_appears_once_in_order_across_the_continuation_pages(long_report):
    text = "\n".join(long_report.page_texts[1:4])
    found = re.findall(r"keyword (\d{3})", text)
    assert found == [f"{i:03d}" for i in range(45)]
    for page in long_report.page_texts[1:4]:
        assert page.count("Previous Rank") == 1 and page.count("Movement") == 1  # the header repeats on each page


def test_the_long_report_tables_keep_every_row(long_report):
    text = "\n".join(long_report.page_texts[4:])
    assert len(re.findall(r"Country \d{3}", text)) == 5 + 5  # the top-5 table rows plus the five chart labels (the other 35 countries are not drawn)
    assert text.count("Recommendation ") >= 6 and "CONTACT" in text
    assert [m["tableRows"] for m in long_report.measures[1:4]] and sum(m["tableRows"] for m in long_report.measures[1:4]) == 45


def test_the_rendered_pdf_has_no_daily_table_and_a_top_five_country_table(long_report, standard):
    for outcome in (long_report, standard):
        text = "\n".join(outcome.page_texts)
        assert "Daily performance" not in text and "Clicks per day" in text and "Impressions per day" in text
        assert outcome.issues == () and outcome.ok
        assert all(len(page.strip()) > 80 for page in outcome.page_texts)  # no blank or near-empty page
    assert "Top 5 countries by impressions (of 40)" in "\n".join(long_report.page_texts)
    assert sum(m["tableRows"] for m in long_report.measures[4:]) == 5 + 2  # five countries and two channels


def test_the_long_report_has_zero_layout_issues(long_report):
    assert long_report.issues == () and long_report.ok
    for m in long_report.measures:
        assert all(s["textOutside"] == 0 for s in m["svgs"])
        assert m["contentRight"] <= m["frameRight"] + 0.75 and not m["clippedRows"] and not m["overlaps"]


def test_measured_heights_stay_within_the_physical_page_body(long_report):
    for m in long_report.measures:
        assert m["scrollHeight"] - m["clientHeight"] <= 0.75
    deltas = [e.delta_mm for e in long_report.estimates]
    assert max(abs(d) for d in deltas) < 8.0
    assert all(e.measured_mm < long_report.measures[e.page - 1]["clientHeight"] / pv.PX_PER_MM for e in long_report.estimates)


# --- determinism -------------------------------------------------------------------------------------------------------------------------

def test_the_same_input_renders_to_identical_bytes_in_a_new_browser(long_report, tmp_path):
    again = render(full(), tmp_path)
    assert again.pdf_sha256 == long_report.pdf_sha256 and again.byte_count == long_report.byte_count
    assert hash_of(again.pdf_path) == hash_of(long_report.pdf_path)


def test_dates_are_fixed_to_the_generation_date_and_nothing_else_is_altered(standard):
    data = standard.pdf_path.read_bytes()
    raw = (standard.pdf_path.parent / "report.raw.pdf").read_bytes()
    assert len(data) == len(raw) and b"(D:20261030000000+00'00')" in data
    assert pv.inspect_pdf(raw).page_count == pv.inspect_pdf(data).page_count
    diffs = [i for i, (a, b) in enumerate(zip(raw, data)) if a != b]
    assert diffs and max(diffs) - min(diffs) < 200  # only the two date strings differ


def test_a_different_generation_date_gives_different_bytes(standard, tmp_path):
    other = render(sample(generation_date=sample().generation_date.replace(day=29)), tmp_path)
    assert other.pdf_sha256 != standard.pdf_sha256 and b"D:20261029000000" in other.pdf_path.read_bytes()


# --- safety --------------------------------------------------------------------------------------------------------------------------------

def test_the_original_assets_are_never_modified(standard):
    before = {p.name: hash_of(p) for p in (LOGO, *FONT_FILES.values())}
    again = {p.name: hash_of(p) for p in (LOGO, *FONT_FILES.values())}
    assert before == again
    assert hash_of(standard.html_path.parent / "assets" / "jasper-logo.png") == hash_of(LOGO)


def test_the_copied_fonts_match_the_fingerprints_in_the_render_input(standard):
    fp = af.measure_fingerprints(template_version="r6.0", logo_path=LOGO, font_paths=FONT_FILES)
    folder = standard.html_path.parent
    assert af.sha256_of_file(folder / "assets" / "jasper-logo.png") == fp.logo_sha256
    assert {f.name: f.sha256 for f in fp.fonts} == {n: af.sha256_of_file(folder / "fonts" / f"{n}.ttf") for n in FONT_FILES}


def test_a_work_folder_inside_the_project_is_refused(tmp_path):
    inside = ROOT / "templates"  # never written to: the check happens first
    with pytest.raises(pr.PdfRenderError) as caught:
        pr.render_report_pdf(with_fingerprints(sample()), workdir=inside, logo_path=LOGO, fonts=SOURCES)
    assert caught.value.code == "workdir_inside_project"
    with pytest.raises(pr.PdfRenderError) as caught:
        pr.render_report_pdf(with_fingerprints(sample()), workdir=ROOT, logo_path=LOGO, fonts=SOURCES)
    assert caught.value.code == "workdir_inside_project"


def test_a_missing_relative_or_non_empty_work_folder_is_refused_and_nothing_is_overwritten(tmp_path):
    for bad, code in ((tmp_path / "missing", "workdir_invalid"), (Path("relative"), "workdir_invalid")):
        with pytest.raises(pr.PdfRenderError) as caught:
            pr.render_report_pdf(with_fingerprints(sample()), workdir=bad, logo_path=LOGO, fonts=SOURCES)
        assert caught.value.code == code
    (tmp_path / "keep.txt").write_text("existing")
    with pytest.raises(pr.PdfRenderError) as caught:
        pr.render_report_pdf(with_fingerprints(sample()), workdir=tmp_path, logo_path=LOGO, fonts=SOURCES)
    assert caught.value.code == "workdir_not_empty" and (tmp_path / "keep.txt").read_text() == "existing"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt"]


def test_a_logo_or_font_that_does_not_match_its_fingerprint_stops_before_any_html(tmp_path):
    other_logo = tmp_path / "other.png"
    other_logo.write_bytes(LOGO.read_bytes() + b"x")
    work = tmp_path / "w1"
    work.mkdir()
    with pytest.raises(pr.PdfRenderError) as caught:
        pr.render_report_pdf(with_fingerprints(sample()), workdir=work, logo_path=other_logo, fonts=SOURCES)
    assert caught.value.code == "fingerprint_mismatch" and not (work / "report.html").exists()
    work2 = tmp_path / "w2"
    work2.mkdir()
    good = with_fingerprints(sample())
    wrong = dataclasses.replace(good, fingerprints=dataclasses.replace(
        good.fingerprints, fonts=(ri.FontFingerprint("DejaVuSans", "a" * 64), ri.FontFingerprint("DejaVuSans-Bold", "b" * 64))))
    with pytest.raises(pr.PdfRenderError) as caught:
        pr.render_report_pdf(wrong, workdir=work2, logo_path=LOGO, fonts=SOURCES)
    assert caught.value.code == "fingerprint_mismatch" and not (work2 / "report.html").exists()
    assert LOGO.read_bytes() == other_logo.read_bytes()[:-1]  # the original logo was only read


def test_content_that_cannot_be_placed_produces_no_html_and_no_pdf(tmp_path):
    bad = full(geography=ri.GeographyVisuals("available", (ri.CountryRow("y" * 30000, 1, 1),)))
    with pytest.raises(rs.ContentPaginationError):
        render(bad, tmp_path)
    assert not (tmp_path / "report.html").exists() and not list(tmp_path.glob("*.pdf"))
    bad = full(keywords=(ri.RenderKeywordRow("x" * 30000, "L", ri.RenderRanking("missing"), ri.RenderRanking("missing"), None),))
    work = tmp_path / "second"
    work.mkdir()
    with pytest.raises(kp.KeywordPaginationError):
        render(bad, work)
    assert not (work / "report.html").exists()


def test_arguments_are_checked(tmp_path):
    with pytest.raises(TypeError):
        pr.render_report_pdf({"x": 1}, workdir=tmp_path, logo_path=LOGO, fonts=SOURCES)
    with pytest.raises(pr.PdfRenderError) as caught:
        pr.render_report_pdf(with_fingerprints(sample()), workdir=tmp_path, logo_path=LOGO, fonts=())
    assert caught.value.code == "fonts_invalid"


def test_the_renderer_source_uses_no_network_clock_or_finalization():
    source = (ROOT / "src" / "seo_report" / "pdf_renderer.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for word in ("import requests", "urllib", "socket", "datetime.now", "date.today", "os.environ", "report_finalize",
                 "record_writer", "sqlite", "SnapshotStore", "google"):
        assert word not in code, word
