import datetime as dt
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import report_charts as rc

START, END = dt.date(2026, 9, 29), dt.date(2026, 10, 26)


def pts(*pairs):
    return [(START + dt.timedelta(days=d), v) for d, v in pairs]


def line(points, **kw):
    options = dict(chart_id="c1", title="Clicks per day", unit="clicks", points=points, period_start=START, period_end=END)
    options.update(kw)
    return rc.line_chart(**options)


def bar(rows, **kw):
    options = dict(chart_id="b1", title="Clicks by country", unit="clicks", rows=rows)
    options.update(kw)
    return rc.bar_chart(**options)


def coordinates(svg, cls):
    return [tuple(float(x) for x in re.search(rf'class="{cls}[^"]*" cx="([\d.]+)" cy="([\d.]+)"', m).groups())
            for m in re.findall(rf'<circle class="{cls}[^>]*>', svg)]


# --- axis helpers -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(0, (1, 1)), (1, (1, 1)), (4, (4, 1)), (5, (5, 1)), (6, (6, 2)), (9, (10, 2)),
                                            (11, (12, 5)), (50, (50, 10)), (51, (60, 20)), (1552, (2000, 500)),
                                            (100000, (100000, 20000))])
def test_the_axis_maximum_is_a_round_number_with_at_most_five_steps(value, expected):
    maximum, step = rc.nice_maximum(value)
    assert (maximum, step) == expected if value in (0, 1, 4, 5) else maximum >= value and maximum // step <= 5
    assert maximum >= value and maximum % step == 0 and step in {m * 10 ** p for m in (1, 2, 5) for p in range(18)}


def test_wrapping_never_shortens_a_label():
    for text in ("short", "two words here", "x" * 100, "ab " + "y" * 60 + " cd", "日本" * 30, ""):
        lines = rc.wrap_label(text, 12)
        assert all(len(l) <= 12 for l in lines) and "".join("".join(l.split()) for l in lines) == "".join(text.split())
    assert rc.wrap_label("aaa bbb", 7) == ["aaa bbb"] and rc.wrap_label("aaa bbb", 6) == ["aaa", "bbb"]


# --- line chart ----------------------------------------------------------------------------------------------------------------

def test_a_line_chart_is_deterministic_inline_svg_without_colours_or_remote_assets():
    first = line(pts((0, 3), (1, 5), (2, 4)))
    assert first.svg == line(pts((0, 3), (1, 5), (2, 4))).svg
    svg = str(first.svg)
    assert svg.startswith("<svg ") and svg.endswith("</svg>") and 'role="img"' in svg
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|https?://(?!www\.w3\.org)|<image|<script|href=", svg)
    assert "fill=" not in svg and "stroke=" not in svg  # colours come from the stylesheet's classes
    assert isinstance(first.height_mm, float) and 55 < first.height_mm < 65


def test_the_y_axis_starts_at_zero_and_labels_the_scale():
    svg = str(line(pts((0, 1552), (1, 900))).svg)
    labels = re.findall(r'chart__label--y"[^>]*>([\d,]+)<', svg)
    assert labels[0] == "0" and labels[-1] == "2,000" and len(labels) == 5


def test_zero_values_sit_on_the_baseline_as_measured_points():
    chart = line(pts((0, 0), (1, 0), (2, 5)))
    markers = coordinates(str(chart.svg), "chart__marker")
    baseline = markers[0][1]
    assert markers[1][1] == baseline and markers[2][1] < baseline and len(markers) == 3
    assert "0 clicks" in str(chart.svg)


def test_an_all_zero_series_is_drawn_on_the_baseline_not_hidden():
    markers = coordinates(str(line(pts((0, 0), (1, 0))).svg), "chart__marker")
    assert len(markers) == 2 and markers[0][1] == markers[1][1]


def test_a_missing_day_is_a_gap_in_the_line_and_never_a_zero_point():
    chart = line(pts((0, 3), (1, 5), (3, 4), (4, 6)))  # day 2 has no record
    svg = str(chart.svg)
    assert svg.count("<polyline") == 2 and len(coordinates(svg, "chart__marker")) == 4
    xs = [x for x, _ in coordinates(svg, "chart__marker")]
    assert xs[2] - xs[1] == pytest.approx(2 * (xs[1] - xs[0]), rel=1e-3)  # day 3 sits two days after day 1
    assert chart.gaps == 28 - 4 and "4 of 28 days have a record" in chart.description


def test_consecutive_days_form_one_line_and_a_single_day_is_only_a_marker():
    assert str(line(pts(*[(d, d) for d in range(28)])).svg).count("<polyline") == 1
    single = str(line(pts((5, 9))).svg)
    assert "<polyline" not in single and len(coordinates(single, "chart__marker")) == 1


def test_x_labels_show_the_period_start_weekly_ticks_and_the_end():
    svg = str(line(pts((0, 1), (27, 2))).svg)
    labels = re.findall(r'chart__label--x"[^>]*>([^<]+)<', svg)
    assert labels == ["Sep 29", "Oct 6", "Oct 13", "Oct 20", "Oct 26"]


def test_points_outside_the_period_widen_the_axis_instead_of_being_clipped():
    chart = line([(END + dt.timedelta(days=3), 4)] + [])
    assert "Oct 29" in str(chart.svg) and len(coordinates(str(chart.svg), "chart__marker")) == 1


def test_a_one_day_period_is_centred():
    chart = line([(START, 3)], period_end=START)
    x = coordinates(str(chart.svg), "chart__marker")[0][0]
    assert x == pytest.approx(rc._LEFT + (rc.WIDTH - rc._LEFT - rc._RIGHT) / 2, abs=0.01)


def test_the_description_and_tooltips_state_only_facts_from_the_data():
    chart = line(pts((0, 3), (1, 1552)))
    assert "Highest day: 1,552 clicks" in chart.description and "lowest day: 3 clicks" in chart.description
    assert "<title>Sep 29 2026: 3 clicks</title>" in str(chart.svg) and "<title>Sep 30 2026: 1,552 clicks</title>" in str(chart.svg)
    assert 'aria-labelledby="c1-title c1-desc"' in str(chart.svg) and '<title id="c1-title">Clicks per day</title>' in str(chart.svg)


def test_line_chart_text_is_escaped():
    svg = str(line(pts((0, 1)), title='<b>"x"</b> & y', unit="<i>u</i>", chart_id='a"b').svg)
    assert "<b>" not in svg and "<i>" not in svg and "&lt;b&gt;" in svg and 'id="a&quot;b-title"' in svg


@pytest.mark.parametrize("bad", [[], [(START, 1), (START, 2)], [(START + dt.timedelta(days=1), 1), (START, 2)]])
def test_a_line_chart_needs_ascending_unique_days(bad):
    with pytest.raises(ValueError):
        line(bad)


# --- bar chart ---------------------------------------------------------------------------------------------------------------------

def test_bars_follow_the_given_order_and_scale_from_zero():
    chart = bar([("United States", 100), ("Canada", 50), ("Mexico", 0)])
    svg = str(chart.svg)
    widths = [float(w) for w in re.findall(r'class="chart__bar"[^>]*width="([\d.]+)"', svg)]
    assert widths == [330.0, 165.0, 0.0]
    assert [m for m in re.findall(r"<tspan[^>]*>([^<]+)</tspan>", svg)] == ["United States", "Canada", "Mexico"]
    assert re.findall(r'chart__label--value"[^>]*>([\d,]+)<', svg) == ["100", "50", "0"]
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|fill=|stroke=|<image|href=", svg)


def test_a_long_label_is_wrapped_never_shortened_and_makes_the_chart_taller():
    name = "The Federal Democratic Republic of a Very Long Country Name"
    tall = bar([(name, 10), ("B", 5)])
    short = bar([("A", 10), ("B", 5)])
    spans = re.findall(r"<tspan[^>]*>([^<]+)</tspan>", str(tall.svg))
    assert len(spans) > 2 and " ".join(spans[:-1]) == name and tall.height_mm > short.height_mm
    assert f"<title>{name}: 10 clicks</title>" in str(tall.svg)


def test_a_label_without_spaces_is_broken_into_lines_not_cut():
    word = "w" * 100
    spans = re.findall(r"<tspan[^>]*>([^<]+)</tspan>", str(bar([(word, 1), ("B", 1)]).svg))
    assert "".join(spans[:-1]) == word


def test_unicode_labels_and_escaping_in_bar_charts():
    svg = str(bar([("Côte d’Ivoire", 3), ("<script>x</script> & Co", 2)]).svg)
    assert "Côte d’Ivoire" in svg and "<script>" not in svg and "&lt;script&gt;x&lt;/script&gt; &amp; Co" in svg


def test_all_zero_bars_have_no_length_but_keep_their_labels():
    svg = str(bar([("A", 0), ("B", 0)]).svg)
    assert re.findall(r'class="chart__bar"[^>]*width="([\d.]+)"', svg) == ["0.00", "0.00"]
    assert re.findall(r'chart__label--value"[^>]*>(\d+)<', svg) == ["0", "0"]


def test_bar_chart_is_deterministic_and_needs_rows():
    assert bar([("A", 1)]).svg == bar([("A", 1)]).svg
    with pytest.raises(ValueError):
        bar([])
    assert rc.bar_chart_height_units(["A", "B"]) < rc.bar_chart_height_units(["A", "B" * 100])


def test_the_chart_module_is_pure():
    source = (ROOT / "src" / "seo_report" / "report_charts.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for word in ("now(", "today(", "random", "uuid", "open(", "requests", "urllib", "socket", "os.environ", "subprocess"):
        assert word not in code, word


# --- x-axis labels stay inside the SVG (R5.1) ----------------------------------------------------------------------------------------------

def x_labels(svg):
    """(x, anchor, text) of every x-axis label, read from the generated SVG."""
    return [(float(x), anchor, text) for x, anchor, text in re.findall(
        r'<text class="chart__label chart__label--x" x="([\d.]+)" y="[\d.]+" text-anchor="(\w+)">([^<]+)</text>', str(svg))]


def box(x, anchor, text):
    width = len(text) * rc.CHAR_UNITS
    left = {"middle": x - width / 2, "end": x - width, "start": x}[anchor]
    return left, left + width


def view_width(svg):
    return float(re.search(r'viewBox="0 0 ([\d.]+) ', str(svg)).group(1))


def test_the_final_label_is_end_anchored_and_fully_inside_the_view_box():
    chart = line(pts((0, 3), (27, 5)))
    labels = x_labels(chart.svg)
    last = labels[-1]
    assert last[2] == "Oct 26" and last[1] == "end"
    left, right = box(*last)
    assert 0 <= left and right <= view_width(chart.svg) - rc.LABEL_EDGE_PAD + 1e-6
    assert last[0] == pytest.approx(rc.WIDTH - rc._RIGHT)  # still at the period end's tick


def test_the_first_and_intermediate_labels_keep_their_centred_positions():
    labels = x_labels(line(pts((0, 3), (27, 5))).svg)
    assert [t for _, _, t in labels] == ["Sep 29", "Oct 6", "Oct 13", "Oct 20", "Oct 26"]
    assert [a for _, a, _ in labels] == ["middle", "middle", "middle", "middle", "end"]
    plot_w = rc.WIDTH - rc._LEFT - rc._RIGHT
    for (x, _, _), offset in zip(labels[:4], (0, 7, 14, 21)):
        assert x == pytest.approx(rc._LEFT + plot_w * offset / 27, abs=0.01)  # exactly on its day's tick


@pytest.mark.parametrize("days", [1, 3, 7, 10, 14, 28, 29, 30, 31, 32, 35, 60, 91, 366])
def test_every_label_is_inside_the_svg_and_none_touches_another_for_any_period_length(days):
    end = START + dt.timedelta(days=days - 1)
    chart = line(pts((0, 1)) if days == 1 else pts((0, 1), (days - 1, 2)), period_end=end)
    labels = x_labels(chart.svg)
    boxes = [box(*l) for l in labels]
    width = view_width(chart.svg)
    assert all(left >= 0 and right <= width for left, right in boxes), labels
    assert all(boxes[i][1] + rc.LABEL_MIN_GAP <= boxes[i + 1][0] + 1e-6 for i in range(len(boxes) - 1)), labels
    span = days - 1
    ticks = list(range(0, span + 1, 7)) + ([span] if span - (span // 7) * 7 >= 3 else [])
    assert labels[0][2] == "Sep 29" and labels[-1][2] == rc.day_label(START + dt.timedelta(days=ticks[-1]))


def test_a_label_close_to_the_last_one_is_left_out_not_the_last_label():
    chart = line(pts((0, 1), (31, 2)), period_end=START + dt.timedelta(days=31))  # tick 28 is only 3 days before the end
    labels = x_labels(chart.svg)
    texts = [t for _, _, t in labels]
    assert texts[0] == "Sep 29" and texts[-1] == rc.day_label(START + dt.timedelta(days=31))
    assert rc.day_label(START + dt.timedelta(days=28)) not in texts and len(texts) == 5


def test_the_period_start_and_end_are_always_labelled():
    for days in (8, 29, 30, 33, 45):
        end = START + dt.timedelta(days=days - 1)
        texts = [t for _, _, t in x_labels(line(pts((0, 1), (days - 1, 2)), period_end=end).svg)]
        assert texts[0] == "Sep 29"
        if (days - 1) % 7 >= 3 or (days - 1) % 7 == 0:
            assert texts[-1] == rc.day_label(end), (days, texts)


def test_place_x_labels_keeps_long_labels_inside_and_never_shortens_them():
    width = 600
    for text in ("Sep 29", "September 29, 2026", "29 September 2026 (end)"):
        placed = rc.place_x_labels([(56.0, text), (330.0, text), (586.0, text)], width)
        assert [t for _, _, t in placed][0] == text and [t for _, _, t in placed][-1] == text
        for x, anchor, t in placed:
            left, right = box(x, anchor, t)
            assert left >= 0 and right <= width - rc.LABEL_EDGE_PAD + 1e-6
    placed = rc.place_x_labels([(4.0, "Sep 29"), (300.0, "Oct 13")], width)
    assert placed[0][1] == "start" and placed[0][0] == 4.0 and placed[1][1] == "middle"


def test_place_x_labels_drops_only_intermediate_labels_that_collide():
    entries = [(56.0, "Sep 29"), (100.0, "Oct 6"), (140.0, "Oct 13"), (586.0, "Oct 26")]
    placed = rc.place_x_labels(entries, 600)
    texts = [t for _, _, t in placed]
    assert texts == ["Sep 29", "Oct 13", "Oct 26"]  # "Oct 6" would touch the first label
    boxes = [box(*p) for p in placed]
    assert all(boxes[i][1] + rc.LABEL_MIN_GAP <= boxes[i + 1][0] + 1e-6 for i in range(len(boxes) - 1))
    assert rc.place_x_labels([], 600) == []
    assert rc.place_x_labels([(300.0, "Oct 13")], 600) == [(300.0, "middle", "Oct 13")]


def test_the_first_label_is_kept_even_when_it_collides_with_the_last():
    placed = rc.place_x_labels([(100.0, "Sep 29"), (110.0, "Oct 6")], 600)
    assert [t for _, _, t in placed] == ["Sep 29", "Oct 6"]


def test_every_line_chart_style_uses_the_same_label_layout():
    primary = x_labels(line(pts((0, 1), (27, 2)), style="primary").svg)
    secondary = x_labels(line(pts((0, 1), (27, 2)), style="secondary").svg)
    assert primary == secondary and primary[-1][1] == "end"


def test_the_three_report_line_charts_use_the_corrected_labels():
    import dataclasses
    from decimal import Decimal
    from seo_report import render_input as ri
    from seo_report import report_sections as rs
    from test_render_input import sample
    from test_report_templates import ASSETS
    daily = tuple(ri.DailyPoint(START + dt.timedelta(days=i), i, i + 1) for i in range(28))
    value = dataclasses.replace(
        sample(), search_console=ri.SearchConsoleVisuals("available", 1, 1, Decimal("0.1"), Decimal("1"), daily),
        ga4=ri.Ga4Visuals("available", 1, 1, Decimal("0.5"), daily=tuple(ri.Ga4DailyPoint(START + dt.timedelta(days=i), i)
                                                                          for i in range(28))))
    found = []
    for page in rs.plan_section_pages(value, ASSETS):
        for placement in page.placements:
            if placement.block.kind == "chart" and "chart--line" in str(placement.block.data["svg"]):
                found.append(placement.block.data["svg"])
    assert len(found) == 3
    for svg in found:
        labels = x_labels(svg)
        assert labels[-1][1] == "end" and labels[-1][2] == "Oct 26"
        left, right = box(*labels[-1])
        assert right <= view_width(svg) - rc.LABEL_EDGE_PAD + 1e-6 and left > 0


def test_missing_days_are_still_gaps_after_the_label_fix():
    chart = line(pts((0, 3), (1, 5), (3, 4), (27, 6)))
    svg = str(chart.svg)
    assert svg.count("<polyline") == 1 and len(coordinates(svg, "chart__marker")) == 4  # one piece (days 0-1); days 3 and 27 stand alone
    assert chart.gaps == 28 - 4 and x_labels(svg)[-1][1] == "end"


def test_the_label_fix_keeps_the_chart_size_scales_and_accessibility():
    chart = line(pts((0, 1552), (27, 900)))
    svg = str(chart.svg)
    assert rc.WIDTH == 600 and rc.LINE_HEIGHT == 210 and 'viewBox="0 0 600 210"' in svg
    assert re.findall(r'chart__label--y"[^>]*>([\d,]+)<', svg) == ["0", "500", "1,000", "1,500", "2,000"]
    assert 'role="img"' in svg and '<title id="c1-title">Clicks per day</title>' in svg and "<desc id=\"c1-desc\">" in svg
    assert "<title>Sep 29 2026: 1,552 clicks</title>" in svg and svg == str(line(pts((0, 1552), (27, 900))).svg)
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|fill=|stroke=", svg)
