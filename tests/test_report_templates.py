import dataclasses
import datetime as dt
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest
from markupsafe import Markup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import render_input as ri
from seo_report import report_templates as rt
from test_render_input import sample

LOGO = ROOT / "assets" / "jasper-logo.png"
LOGO_ASSET = rt.LogoAsset("assets/jasper-logo.png", 1178, 411)
ASSETS = rt.TemplateAssets(LOGO_ASSET)
CSS = (ROOT / "templates" / "report" / "report.css").read_text(encoding="utf-8")


class Tags(HTMLParser):
    """Collects start tags with their attributes (a plain structural view of the document)."""

    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        if data.strip():
            self.text.append(" ".join(data.split()))

    def find(self, tag, **attrs):
        return [a for t, a in self.tags if t == tag and all(a.get(k) == v for k, v in attrs.items())]

    def with_class(self, name):
        return [(t, a) for t, a in self.tags if name in (a.get("class") or "").split()]


def cover(**overrides):
    return rt.render_cover_document(sample(**overrides), ASSETS)


def visible_text(html):
    return " | ".join(Tags(html).text)


# --- inputs and assets -----------------------------------------------------------------------------------------------------------

def test_the_real_logo_has_the_expected_pixel_size_and_the_asset_matches_it():
    assert rt.read_png_size(LOGO) == (1178, 411)
    assert (LOGO_ASSET.width_px, LOGO_ASSET.height_px) == rt.read_png_size(LOGO)


def test_read_png_size_rejects_other_files(tmp_path):
    bad = tmp_path / "not.png"
    bad.write_bytes(b"GIF89a" + b"\0" * 40)
    with pytest.raises(rt.TemplateInputError):
        rt.read_png_size(bad)
    with pytest.raises(rt.TemplateInputError):
        rt.read_png_size(tmp_path / "missing.png")
    short = tmp_path / "short.png"
    short.write_bytes(b"\x89PNG")
    with pytest.raises(rt.TemplateInputError):
        rt.read_png_size(short)


@pytest.mark.parametrize("href", ["", None, "C:\\Users\\x\\logo.png", "/abs/logo.png", "\\\\server\\share\\a.png",
                                  "file:///C:/logo.png", "https://example.test/logo.png", "../logo.png",
                                  "a/../../logo.png", "logo with space.png", "a\\b.png", '"quoted".png', "~/logo.png"])
def test_asset_locations_must_not_depend_on_the_machine(href):
    with pytest.raises(rt.TemplateInputError):
        rt.LogoAsset(href, 10, 10)
    with pytest.raises(rt.TemplateInputError):
        rt.FontFace("DejaVu Sans", href)


@pytest.mark.parametrize("href", ["assets/jasper-logo.png", "logo.png", "data:image/png;base64,iVBORw0KGgo="])
def test_relative_locations_and_data_urls_are_accepted(href):
    assert rt.LogoAsset(href, 10, 4).href == href


@pytest.mark.parametrize("w,h", [(0, 10), (10, 0), (-1, 10), (1.5, 10), (True, 10), ("10", 10)])
def test_logo_dimensions_must_be_positive_whole_numbers(w, h):
    with pytest.raises(rt.TemplateInputError):
        rt.LogoAsset("logo.png", w, h)


def test_font_and_asset_inputs_are_validated():
    with pytest.raises(rt.TemplateInputError):
        rt.FontFace("", "a.ttf")
    with pytest.raises(rt.TemplateInputError):
        rt.FontFace("DejaVu Sans", "a.ttf", weight="500")
    with pytest.raises(rt.TemplateInputError):
        rt.FontFace("DejaVu Sans", "a.ttf", style="oblique")
    with pytest.raises(rt.TemplateInputError):
        rt.TemplateAssets("logo.png")
    with pytest.raises(rt.TemplateInputError):
        rt.TemplateAssets(LOGO_ASSET, fonts=("a.ttf",))


def test_the_renderers_check_their_argument_types():
    with pytest.raises(TypeError):
        rt.render_cover_document({"client": "x"}, ASSETS)
    with pytest.raises(TypeError):
        rt.render_cover_document(sample(), "assets")
    with pytest.raises(TypeError):
        rt.render_shell_page_document(None, ASSETS)
    with pytest.raises(rt.TemplateInputError):
        rt.render_shell_page_document(sample(), ASSETS, page_number=0)
    with pytest.raises(rt.TemplateInputError):
        rt.render_shell_page_document(sample(), ASSETS, page_number=True)


# --- A4 page structure and CSS --------------------------------------------------------------------------------------------------------

def test_the_document_is_one_a4_cover_page():
    html = cover()
    tags = Tags(html)
    assert html.startswith("<!doctype html>") and 'lang="en"' in html and '<meta charset="utf-8">' in html
    pages = tags.with_class("page")
    assert len(pages) == 1 and pages[0][0] == "section" and pages[0][1]["data-page-kind"] == "cover"
    assert len(tags.find("div", **{"class": "page__frame"})) == 1
    assert "<title>Sample Roofing Co - Search Engine Optimization Report</title>" in html


def test_the_stylesheet_fixes_an_a4_page_and_is_inlined():
    assert "@page { size: A4; margin: 0; }" in CSS
    assert "--page-width: 210mm" in CSS and "--page-height: 297mm" in CSS
    assert "width: var(--page-width);" in CSS and "height: var(--page-height);" in CSS
    assert "break-after: page" in CSS and "overflow: hidden" in CSS and "overflow-wrap: anywhere" in CSS
    html = cover()
    assert CSS.strip() in html and "@page { size: A4; margin: 0; }" in html


def test_the_stylesheet_uses_only_the_verified_brand_colours():
    colours = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", CSS))
    assert colours == {"#000000", "#ffffff", "#e61001", "#d9d9d9", "#1a62ff", "#689f38", "#ffff9e"}
    assert "--accent: #e61001" in CSS and "--grid: #d9d9d9" in CSS and "--link: #1a62ff" in CSS
    assert "--check: #689f38" in CSS and "--table-head: #ffff9e" in CSS


def test_the_font_stack_starts_with_dejavu_sans_and_no_font_is_embedded():
    assert 'font-family: "DejaVu Sans", "DejaVuSans", sans-serif;' in CSS
    assert "@font-face" not in CSS and "url(" not in CSS and "data:" not in CSS
    assert "@font-face" not in cover()  # no fonts supplied, none bundled


def test_font_faces_are_generated_only_from_supplied_locations():
    assets = rt.TemplateAssets(LOGO_ASSET, (rt.FontFace("DejaVu Sans", "fonts/DejaVuSans.ttf"),
                                            rt.FontFace("DejaVu Sans", "fonts/DejaVuSans-Bold.ttf", "700")))
    html = rt.render_cover_document(sample(), assets)
    assert html.count("@font-face") == 2 and "@font-face" not in CSS
    assert 'src: local("DejaVu Sans"), url("fonts/DejaVuSans-Bold.ttf");' in html and "font-weight: 700" in html


def test_the_dejavu_font_files_and_licence_exist_where_r0_found_them_and_are_not_copied():
    import matplotlib
    folder = Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf"
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "LICENSE_DEJAVU"):
        assert (folder / name).is_file(), name
    assert not (ROOT / "assets" / "fonts").exists()
    assert sorted(p.name for p in (ROOT / "assets").iterdir()) == ["jasper-logo.png"]


def test_nothing_in_the_css_or_templates_depends_on_the_machine_or_the_network():
    for path in (ROOT / "templates" / "report").iterdir():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"https?://|file:|[A-Za-z]:\\\\|@import|<script", text), path.name
        assert not re.search(r"\bnow\(|\btoday\b|\brandom\b|environ|\bnamespace\b", text.replace("--", "")), path.name


# --- logo -----------------------------------------------------------------------------------------------------------------------------

def test_the_logo_is_the_supplied_file_reference_with_alt_text():
    logos = Tags(cover()).find("img", **{"class": "logo"})
    assert len(logos) == 1 and logos[0]["src"] == "assets/jasper-logo.png"
    assert logos[0]["alt"] == "Jasper IT Solutions"


def test_the_logo_keeps_its_aspect_ratio():
    logo = Tags(cover()).find("img", **{"class": "logo"})[0]
    assert (logo["width"], logo["height"]) == ("1178", "411")
    assert abs(int(logo["width"]) / int(logo["height"]) - 1178 / 411) < 1e-9
    assert logo["style"] == "aspect-ratio: 1178 / 411;"
    rule = re.search(r"\.logo \{([^}]*)\}", CSS).group(1)
    assert "height: auto" in rule and "width: 38mm" in rule  # one fixed dimension, the other follows the ratio


def test_the_logo_is_never_replaced_by_text_or_a_drawing():
    html = cover()
    assert "<svg" not in html and "<canvas" not in html and html.count("<img") == 1
    assert not re.search(r">\s*JASPER\s*<", html)


def test_a_different_logo_location_and_size_are_used_as_given():
    assets = rt.TemplateAssets(rt.LogoAsset("logo.png", 600, 200))
    logo = Tags(rt.render_cover_document(sample(), assets)).find("img", **{"class": "logo"})[0]
    assert (logo["src"], logo["width"], logo["height"], logo["style"]) == ("logo.png", "600", "200",
                                                                          "aspect-ratio: 600 / 200;")


# --- cover content ---------------------------------------------------------------------------------------------------------------------

def test_the_generation_date_is_the_supplied_date_in_the_reference_form():
    assert "30th Oct, 2026" in visible_text(cover())
    cases = {dt.date(2026, 10, 1): "1st Oct, 2026", dt.date(2026, 10, 2): "2nd Oct, 2026", dt.date(2026, 10, 3): "3rd Oct, 2026",
             dt.date(2026, 10, 9): "9th Oct, 2026", dt.date(2026, 10, 11): "11th Oct, 2026",
             dt.date(2026, 10, 12): "12th Oct, 2026", dt.date(2026, 10, 13): "13th Oct, 2026",
             dt.date(2026, 10, 21): "21st Oct, 2026", dt.date(2026, 10, 22): "22nd Oct, 2026",
             dt.date(2026, 10, 23): "23rd Oct, 2026", dt.date(2026, 1, 31): "31st Jan, 2026",
             dt.date(2026, 12, 25): "25th Dec, 2026"}
    for day, text in cases.items():
        assert rt.format_report_date(day) == text
        assert text in visible_text(cover(generation_date=day))


def test_the_date_is_not_read_from_the_clock(monkeypatch):
    import datetime
    first = cover(generation_date=dt.date(2020, 2, 29))
    assert "29th Feb, 2020" in first and first == cover(generation_date=dt.date(2020, 2, 29))
    with pytest.raises(rt.TemplateInputError):
        rt.format_report_date("2026-10-30")
    with pytest.raises(rt.TemplateInputError):
        rt.format_report_date(dt.datetime(2026, 10, 30, 1, 2))


def test_the_report_title_is_exact():
    html = cover()
    assert rt.REPORT_TITLE == "SEARCH ENGINE OPTIMIZATION REPORT"
    assert '<h1 class="cover__title">SEARCH ENGINE OPTIMIZATION REPORT</h1>' in html and html.count("<h1") == 1


def test_the_client_name_and_website_are_displayed():
    html = cover()
    assert '<p class="cover__client">Sample Roofing Co</p>' in html
    assert '<p class="cover__website"><a href="https://sample.example">https://sample.example</a></p>' in html


def test_a_client_website_that_is_not_http_is_text_not_a_link():
    for bad in ("javascript:alert(1)", "ftp://x.example", "not a url", "data:text/html,x"):
        html = cover(client=ri.RenderClient("Sample Roofing Co", bad))
        assert not [a for t, a in Tags(html).tags if t == "a" and a.get("href", "").startswith(("javascript", "ftp", "data"))]
        assert '<p class="cover__website">' in html and "<a href=\"" + bad not in html


def test_the_sender_block_comes_from_the_render_input_in_order():
    text = visible_text(cover())
    expected = ["Sample Sender", "Project Manager | Sample Co", "1 Sample Way", "Sample City, TX 00000",
                "Cell: (000) 000-0000", "E-mail: sender@example.test", "Website: Sample Co"]
    positions = [text.index(item) for item in expected]
    assert positions == sorted(positions)
    assert "Jimmy" not in text and "jasperit" not in text and "Pine Brook" not in text  # nothing is hardcoded


def test_a_sender_website_url_becomes_a_link_when_it_is_safe():
    sender = dataclasses.replace(sample().sender, website_url="https://jasper.example")
    html = cover(sender=sender)
    assert '<a href="https://jasper.example">Website: Sample Co</a>' in html
    unsafe = cover(sender=dataclasses.replace(sample().sender, website_url="javascript:alert(1)"))
    assert "javascript" not in unsafe and "Website: Sample Co" in unsafe


def test_a_sender_with_only_a_url_shows_the_url():
    sender = ri.RenderSender("Only Name", website_url="https://jasper.example")
    html = cover(sender=sender)
    assert '<a href="https://jasper.example">Website: https://jasper.example</a>' in html


def test_optional_sender_fields_are_omitted_cleanly():
    html = cover(sender=ri.RenderSender("Only Name"))
    text = visible_text(html)
    assert text.count("Only Name") == 1
    for label in ("Cell:", "E-mail:", "Website:"):
        assert label not in text
    sender_block = re.search(r'<div class="cover__sender">(.*?)</div>', html, re.S).group(1)
    assert sender_block.count("<p>") == 1 and "<p></p>" not in html and "<p> </p>" not in html


@pytest.mark.parametrize("field,label", [("title", None), ("phone", "Cell:"), ("email", "E-mail:"),
                                         ("website_label", "Website:"), ("address_lines", None)])
def test_each_optional_sender_field_can_be_missing_on_its_own(field, label):
    blank = () if field == "address_lines" else None
    sender = dataclasses.replace(sample().sender, **{field: blank}, website_url=None)
    html = cover(sender=sender)
    if label:
        assert label not in html
    assert "None" not in html and "<p></p>" not in html
    for value in sender.address_lines:
        assert value in html


def test_an_empty_website_url_string_adds_no_link_or_label():
    html = cover(sender=dataclasses.replace(sample().sender, website_label=None, website_url=""))
    assert "Website:" not in html


# --- header and footer -----------------------------------------------------------------------------------------------------------------

def test_the_cover_header_has_the_logo_a_rule_and_the_date_only():
    tags = Tags(cover())
    assert len(tags.find("header", **{"class": "page-header"})) == 1
    assert len(tags.find("hr", **{"class": "page-header__rule"})) == 1
    assert tags.find("footer") == []  # the reference cover has no footer
    meta = [t for t in tags.text if t == "30th Oct, 2026"]
    assert meta == ["30th Oct, 2026"]


def shell(**kw):
    return rt.render_shell_page_document(sample(), ASSETS, **kw)


def test_an_interior_page_has_header_body_and_footer_in_order():
    html = shell(page_number=2, body=Markup("<p>Body text.</p>"))
    positions = [html.index(marker) for marker in ('<header class="page-header">', 'class="page__body">',
                                                  "<p>Body text.</p>", '<footer class="page-footer">')]
    assert positions == sorted(positions)
    tags = Tags(html)
    assert len(tags.find("header")) == 1 and len(tags.find("footer")) == 1
    assert tags.find("section", **{"data-page-kind": "content"})


def test_the_interior_header_names_the_client_and_the_footer_numbers_the_page():
    html = shell(page_number=3)
    assert '<div class="page-header__meta">Sample Roofing Co</div>' in html
    assert '<span class="page-footer__client">Sample Roofing Co</span>' in html
    assert '<span class="page-footer__number" data-page-number="3">Page 3</span>' in html


def test_without_a_page_number_none_is_drawn_or_invented():
    html = shell()
    body = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
    assert "page-footer__number" not in body and "Page " not in visible_text(body) and "data-page-number" not in body


def test_the_page_number_is_exactly_what_was_supplied():
    for number in (1, 7, 12):
        assert f'data-page-number="{number}">Page {number}<' in shell(page_number=number)


def test_the_interior_body_is_escaped_unless_it_is_marked_safe():
    assert "&lt;b&gt;x&lt;/b&gt;" in shell(body="<b>x</b>") and "<b>x</b>" not in shell(body="<b>x</b>")
    assert "<b>x</b>" in shell(body=Markup("<b>x</b>"))


# --- visual status messages -----------------------------------------------------------------------------------------------------------

SC_FULL = sample().search_console


def test_an_available_visual_without_truncation_draws_nothing():
    notice = rt.build_visual_notice(SC_FULL, "Search Console")
    assert (notice.status, notice.message, notice.truncation) == ("available", None, ())
    assert rt.render_visual_notice(SC_FULL, "Search Console") == ""


def test_an_unavailable_visual_gets_a_concise_message():
    html = rt.render_visual_notice(ri.Ga4Visuals.unavailable(), "Google Analytics 4")
    assert "Google Analytics 4 data is not available for this report." in html
    assert 'data-visual-status="unavailable"' in html and "notice--unavailable" in html
    assert "Incomplete" not in html and "truncat" not in html.lower()
    for visual, label in ((ri.SearchConsoleVisuals.unavailable(), "Search Console"),
                          (ri.GeographyVisuals.unavailable(), "Geographic")):
        assert f"{label} data is not available for this report." in rt.render_visual_notice(visual, label)


def test_an_incomplete_visual_gets_a_caveat_and_keeps_its_figures():
    incomplete = dataclasses.replace(SC_FULL, status="incomplete")
    notice = rt.build_visual_notice(incomplete, "Search Console")
    assert notice.status == "incomplete" and "incomplete for this period" in notice.message and "may change" in notice.message
    html = rt.render_visual_notice(incomplete, "Search Console")
    assert "Incomplete data" in html and "notice--incomplete" in html and 'data-visual-status="incomplete"' in html
    assert "not available" not in html  # the figures are not hidden or declared unavailable
    assert incomplete.clicks == 120 and incomplete.daily == SC_FULL.daily  # the visual itself is unchanged


def test_truncation_with_an_unknown_count_states_no_count():
    visual = dataclasses.replace(sample().geography, truncation=(ri.TruncationNote("countries", "source_limit"),
                                                                 ri.TruncationNote("countries", "snapshot_cap")))
    notice = rt.build_visual_notice(visual, "Geographic")
    assert notice.truncation == (
        "The countries list was capped when the report data was stored, so more countries may exist than are shown.",
        "The countries list is cut off at the data source, so more countries exist than are shown.")
    html = rt.render_visual_notice(visual, "Geographic")
    assert "notice--truncated" in html and not re.search(r"\d", html.split("</aside>")[0].replace("notice", ""))


def test_truncation_with_a_known_count_states_exactly_that_count():
    visual = dataclasses.replace(sample().ga4, truncation=(ri.TruncationNote("channels", "row_limit", 3),
                                                           ri.TruncationNote("daily", "row_limit", 1)))
    lines = rt.build_visual_notice(visual, "Google Analytics 4").truncation
    assert lines == ("Not every entry of the channels list is shown: 3 more rows left out.",
                     "Not every entry of the daily records list is shown: 1 more row left out.")


def test_known_and_unknown_counts_are_never_mixed_up():
    visual = dataclasses.replace(sample().geography, truncation=(ri.TruncationNote("countries", "row_limit", 7),
                                                                 ri.TruncationNote("countries", "source_limit")))
    lines = rt.build_visual_notice(visual, "Geographic").truncation
    assert sum(bool(re.search(r"\b7\b", line)) for line in lines) == 1 and len(lines) == 2


def test_an_incomplete_and_truncated_visual_shows_both_messages():
    visual = dataclasses.replace(sample().geography, status="incomplete")
    html = rt.render_visual_notice(visual, "Geographic")
    assert "notice--incomplete" in html and "notice--truncated" in html
    assert html.index("notice--incomplete") < html.index("notice--truncated")


def test_search_console_visuals_have_no_truncation_notes():
    assert rt.build_visual_notice(dataclasses.replace(SC_FULL, status="incomplete"), "Search Console").truncation == ()


def test_the_notice_builder_checks_its_arguments():
    for bad in (None, "available", sample().client):
        with pytest.raises(TypeError):
            rt.build_visual_notice(bad, "Search Console")
    for label in ("", "  ", None, 5):
        with pytest.raises(rt.TemplateInputError):
            rt.build_visual_notice(SC_FULL, label)


def test_notice_text_is_escaped():
    html = rt.render_visual_notice(ri.GeographyVisuals.unavailable(), "<b>Geo</b>")
    assert "&lt;b&gt;Geo&lt;/b&gt; data is not available" in html and "<b>" not in html


# --- escaping, determinism, no placeholders ---------------------------------------------------------------------------------------------

HOSTILE = '<script>alert("x")</script> & "quotes" \'single\' <img src=x onerror=alert(1)>'


def test_client_provided_text_is_escaped_everywhere_on_the_cover():
    value = sample(client=ri.RenderClient(HOSTILE, "https://sample.example/?a=1&b=<2>"),
                   sender=ri.RenderSender(HOSTILE, HOSTILE, (HOSTILE,), HOSTILE, HOSTILE, HOSTILE,
                                          "https://x.example/?q=\"><script>"))
    html = rt.render_cover_document(value, ASSETS)
    assert "<script" not in html and "<img src=x" not in html and "onerror=alert(1)>" not in html.replace("&lt;", "")
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in html
    assert html.count("<script") == 0 and "<title>&lt;script&gt;" in html
    assert Tags(html).find("img", **{"class": "logo"})  # the document is still well formed
    shell_html = rt.render_shell_page_document(value, ASSETS, page_number=2)
    assert "<script" not in shell_html


def test_the_same_input_always_gives_identical_html():
    one, two = cover(), cover()
    assert one == two and one == rt.render_cover_document(sample(), ASSETS)
    assert shell(page_number=2) == shell(page_number=2)
    assert rt.render_visual_notice(ri.Ga4Visuals.unavailable(), "GA4") == rt.render_visual_notice(ri.Ga4Visuals.unavailable(), "GA4")


def test_the_html_contains_no_timestamps_environment_values_or_absolute_paths():
    html = cover() + shell(page_number=2)
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", html)
    assert not re.search(r"[A-Za-z]:\\|/Users/|AppData|OneDrive|file:", html)
    assert str(ROOT) not in html


def test_no_none_null_or_empty_placeholders_in_any_rendered_output():
    pages = [cover(), cover(sender=ri.RenderSender("Only Name")), shell(page_number=2), shell(),
             rt.render_visual_notice(ri.GeographyVisuals.unavailable(), "Geographic"),
             rt.render_visual_notice(dataclasses.replace(sample().ga4, status="incomplete"), "GA4")]
    for html in pages:
        text = visible_text(html)
        assert not re.search(r"\bNone\b|\bnull\b|\bNaN\b|\bundefined\b", text), text
        assert not re.search(r"<p>\s*</p>|<span[^>]*>\s*</span>|<div class=\"page-header__meta\">\s*</div>", html)
        assert "{{" not in html and "{%" not in html and "&amp;amp;" not in html


def test_a_misspelt_template_variable_is_an_error_not_an_empty_string():
    import jinja2
    env = rt._environment()
    with pytest.raises(jinja2.UndefinedError):
        env.from_string("{{ input.client.nmae }}").render(input=sample())


def test_rendering_changes_nothing_and_writes_no_file(tmp_path):
    value = sample()
    before = sorted(p.name for p in ROOT.joinpath("templates", "report").iterdir())
    cover_one = rt.render_cover_document(value, ASSETS)
    assert value == sample() and cover_one
    assert before == sorted(p.name for p in ROOT.joinpath("templates", "report").iterdir())
    assert list(tmp_path.iterdir()) == []


def test_the_module_does_not_use_the_clock_the_network_or_the_environment():
    source = (ROOT / "src" / "seo_report" / "report_templates.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for word in ("datetime.now", "date.today", "time.", "random", "uuid", "os.environ", "requests", "urllib.request",
                 "socket", "sqlite", "playwright", "subprocess", "write_", ".write("):
        assert word not in code, word
    assert code.count("open(") == 1  # only the PNG header read in read_png_size
