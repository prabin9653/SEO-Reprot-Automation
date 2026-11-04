"""Inline SVG charts for the report (R5): a daily line chart and a horizontal bar chart.

Pure functions that return deterministic SVG text (as `Markup`) and its height; no chart library, remote asset, file,
clock or browser is involved, and every number comes from the data passed in (nothing is invented, smoothed or filled).

Line chart (`line_chart`)
  * the x axis is the calendar: days run from the start to the end of the reporting period (or beyond them if a point
    lies outside), so a day without a record is a visible gap, never a zero point; the line is drawn in separate pieces
    across gaps and every record also gets a marker (so a single isolated day is visible)
  * the y axis always starts at 0 (counts are never drawn on a truncated scale) and ends at a rounded maximum with
    1, 2 or 5 as its leading digit; zero values sit on the baseline as the measured zeros they are
  * x labels: the first day, every 7th day after it, and the last day when it is not too close to the previous label;
    `place_x_labels` keeps every label inside the SVG (a label that would cross an edge is anchored to that edge) and
    leaves out an intermediate label that would touch the one after it, never the first or the last
Bar chart (`bar_chart`)
  * one bar per row in the given order, scaled from 0 to the largest value; zero is a labelled bar of no length; a
    label is word-wrapped over several lines, never shortened (the full text is also in the bar's tooltip)
Accessibility: each SVG has `role="img"`, a `<title>`, a `<desc>` that states facts computed from the data (range,
number of records, maximum), and every marker or bar has a `<title>` tooltip. Colours and strokes come from CSS classes
(the report's verified palette), so nothing here defines a colour. SVG ids are derived from the chart id.
"""

import datetime as dt
import html
import math
from dataclasses import dataclass

from markupsafe import Markup

WIDTH = 600
CONTENT_WIDTH_MM = 174.0
MM_PER_UNIT = CONTENT_WIDTH_MM / WIDTH
FONT = 11  # SVG units: 11 x 0.29 mm = 3.2 mm, about 9 pt on the page
CHAR_UNITS = 6.8  # estimated width of one label character in SVG units
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class Chart:
    svg: Markup
    height_mm: float
    description: str  # the facts stated in the SVG description
    gaps: int = 0  # days of the period without a record (line charts)


def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def _num(value: float) -> str:
    return f"{value:.2f}"


def thousands(value: int) -> str:
    return f"{value:,}"


def day_label(day: dt.date) -> str:
    return f"{_MONTHS[day.month - 1]} {day.day}"


def nice_maximum(value: int) -> tuple[int, int]:
    """(axis maximum, step) with the step 1, 2 or 5 times a power of ten and at most 5 steps; 0 gives (1, 1)."""
    if value <= 0:
        return 1, 1
    for power in range(0, 18):
        for lead in (1, 2, 5):
            step = lead * 10 ** power
            if math.ceil(value / step) <= 5:
                return math.ceil(value / step) * step, step
    raise ValueError("value too large for a chart axis")


def wrap_label(text: str, capacity: int) -> list[str]:
    """Word-wrap into lines of at most `capacity` characters; words longer than a line are broken. Never shortens."""
    capacity = max(1, capacity)
    lines, current = [], ""
    for word in text.split():
        while len(word) > capacity:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:capacity])
            word = word[capacity:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= capacity:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current or not lines:
        lines.append(current)
    return lines


# --- x axis labels ------------------------------------------------------------------------------------------------------------------------

LABEL_EDGE_PAD = 2.0  # units kept free between a label and the edge of the SVG
LABEL_MIN_GAP = 6.0  # units kept free between two labels


def place_x_labels(entries, view_width: float):
    """Where to draw the x-axis labels so that every one stays inside the SVG and none touches its neighbour.

    `entries` are (x, text) pairs in ascending x order (the first is the period start, the last the period end). The
    label width is estimated at `CHAR_UNITS` per character. A label is centred on its tick (`middle`); one that would
    cross the right edge is anchored `end` at the edge instead, one that would cross the left edge `start`. An
    intermediate label that would touch the first label or the label to its right is left out, never the first or the
    last label.
    Returns (x, anchor, text) triples in the same order. The text is never shortened.
    """
    boxes = []
    for x, text in entries:
        width = len(text) * CHAR_UNITS
        if x + width / 2 > view_width - LABEL_EDGE_PAD:
            anchor, at = "end", min(x, view_width - LABEL_EDGE_PAD)
            left = at - width
        elif x - width / 2 < LABEL_EDGE_PAD:
            anchor, at = "start", max(x, LABEL_EDGE_PAD)
            left = at
        else:
            anchor, at, left = "middle", x, x - width / 2
        boxes.append((at, anchor, text, left, left + width))
    kept = [boxes[-1]] if boxes else []
    for index in range(len(boxes) - 2, -1, -1):
        touches_next = boxes[index][4] + LABEL_MIN_GAP > kept[0][3]
        touches_first = boxes[index][3] < boxes[0][4] + LABEL_MIN_GAP
        if index > 0 and (touches_next or touches_first):
            continue  # an intermediate label that would touch the first label or the next one
        kept.insert(0, boxes[index])
    return [(at, anchor, text) for at, anchor, text, _, _ in kept]


# --- line chart -------------------------------------------------------------------------------------------------------------------------

LINE_HEIGHT = 210
_LEFT, _RIGHT, _TOP, _BOTTOM = 56, 14, 14, 40


def line_chart(*, chart_id: str, title: str, unit: str, points, period_start: dt.date, period_end: dt.date,
               style: str = "primary") -> Chart:
    """A daily line chart of `points` ((day, value) pairs in ascending day order, values whole numbers >= 0).

    `unit` names what is counted ("clicks"); `style` is a CSS modifier (primary or secondary). Requires at least one
    point; the caller decides what to show when there is none.
    """
    points = [(d, v) for d, v in points]
    if not points:
        raise ValueError("a line chart needs at least one point")
    days = [d for d, _ in points]
    if days != sorted(set(days)):
        raise ValueError("points must be in strictly ascending day order")
    first_day, last_day = min(period_start, days[0]), max(period_end, days[-1])
    span = (last_day - first_day).days
    axis_max, step = nice_maximum(max(v for _, v in points))
    plot_w, plot_h = WIDTH - _LEFT - _RIGHT, LINE_HEIGHT - _TOP - _BOTTOM

    def x_of(day):
        return _LEFT + (plot_w / 2 if span == 0 else plot_w * (day - first_day).days / span)

    def y_of(value):
        return _TOP + plot_h * (1 - value / axis_max)

    total_days = span + 1
    gaps = total_days - len(points)
    present = f"{len(points)} of {total_days} days have a record"
    description = (f"{title}. {day_label(first_day)} {first_day.year} to {day_label(last_day)} {last_day.year}. "
                   f"{present}. Highest day: {thousands(max(v for _, v in points))} {unit}; "
                   f"lowest day: {thousands(min(v for _, v in points))} {unit}.")
    tid, did = f"{chart_id}-title", f"{chart_id}-desc"
    out = [f'<svg class="chart chart--line" viewBox="0 0 {WIDTH} {LINE_HEIGHT}" role="img" '
           f'aria-labelledby="{_esc(tid)} {_esc(did)}" xmlns="http://www.w3.org/2000/svg">',
           f'<title id="{_esc(tid)}">{_esc(title)}</title>', f'<desc id="{_esc(did)}">{_esc(description)}</desc>']
    for tick in range(0, axis_max + 1, step):
        y = y_of(tick)
        out.append(f'<line class="chart__grid" x1="{_num(_LEFT)}" y1="{_num(y)}" x2="{_num(WIDTH - _RIGHT)}" y2="{_num(y)}"/>')
        out.append(f'<text class="chart__label chart__label--y" x="{_num(_LEFT - 6)}" y="{_num(y + FONT * 0.35)}" '
                   f'text-anchor="end">{thousands(tick)}</text>')
    out.append(f'<line class="chart__axis" x1="{_num(_LEFT)}" y1="{_num(_TOP + plot_h)}" x2="{_num(WIDTH - _RIGHT)}" '
               f'y2="{_num(_TOP + plot_h)}"/>')
    ticks = list(range(0, span + 1, 7)) or [0]
    if span - ticks[-1] >= 3:
        ticks.append(span)
    entries = [(x_of(first_day + dt.timedelta(days=offset)), day_label(first_day + dt.timedelta(days=offset)))
               for offset in ticks]
    for label_x, anchor, text in place_x_labels(entries, WIDTH):
        out.append(f'<text class="chart__label chart__label--x" x="{_num(label_x)}" y="{_num(_TOP + plot_h + 16)}" '
                   f'text-anchor="{anchor}">{_esc(text)}</text>')
    segment = []
    segments = []
    for index, (day, value) in enumerate(points):
        if segment and (day - points[index - 1][0]).days != 1:
            segments.append(segment)
            segment = []
        segment.append((day, value))
    segments.append(segment)
    for piece in segments:
        if len(piece) > 1:
            coords = " ".join(f"{_num(x_of(d))},{_num(y_of(v))}" for d, v in piece)
            out.append(f'<polyline class="chart__line chart__line--{_esc(style)}" points="{coords}"/>')
    for day, value in points:
        out.append(f'<circle class="chart__marker chart__marker--{_esc(style)}" cx="{_num(x_of(day))}" '
                   f'cy="{_num(y_of(value))}" r="2.4"><title>{day_label(day)} {day.year}: {thousands(value)} '
                   f'{_esc(unit)}</title></circle>')
    out.append("</svg>")
    return Chart(Markup("".join(out)), round(LINE_HEIGHT * MM_PER_UNIT, 3), description, gaps)


# --- bar chart --------------------------------------------------------------------------------------------------------------------------------

_LABEL_W, _BAR_MAX, _ROW_PAD, _BAR_H, _LINE_H = 190, 330, 10, 14, 13
_BAR_TOP, _BAR_BOTTOM = 8, 8


def _label_capacity() -> int:
    return int((_LABEL_W - 10) / CHAR_UNITS)


def bar_chart_height_units(labels) -> int:
    return _BAR_TOP + _BAR_BOTTOM + sum(max(len(wrap_label(l, _label_capacity())) * _LINE_H, _BAR_H) + _ROW_PAD
                                       for l in labels)


def bar_chart(*, chart_id: str, title: str, unit: str, rows) -> Chart:
    """A horizontal bar chart of `rows` ((label, value) pairs, in the given order, values whole numbers >= 0)."""
    rows = [(l, v) for l, v in rows]
    if not rows:
        raise ValueError("a bar chart needs at least one row")
    top = max(v for _, v in rows)
    height = bar_chart_height_units([l for l, _ in rows])
    description = (f"{title}. {len(rows)} rows in the given order. Largest value: {thousands(top)} {unit}.")
    tid, did = f"{chart_id}-title", f"{chart_id}-desc"
    out = [f'<svg class="chart chart--bar" viewBox="0 0 {WIDTH} {height}" role="img" '
           f'aria-labelledby="{_esc(tid)} {_esc(did)}" xmlns="http://www.w3.org/2000/svg">',
           f'<title id="{_esc(tid)}">{_esc(title)}</title>', f'<desc id="{_esc(did)}">{_esc(description)}</desc>']
    axis_x = _LABEL_W + 6
    y = _BAR_TOP
    for label, value in rows:
        lines = wrap_label(label, _label_capacity())
        row_h = max(len(lines) * _LINE_H, _BAR_H) + _ROW_PAD
        centre = y + (row_h - _ROW_PAD) / 2
        first_baseline = centre - (len(lines) - 1) * _LINE_H / 2 + FONT * 0.35
        spans = "".join(f'<tspan x="{_num(_LABEL_W)}" y="{_num(first_baseline + i * _LINE_H)}">{_esc(line)}</tspan>'
                        for i, line in enumerate(lines))
        width = 0 if top == 0 else _BAR_MAX * value / top
        out.append(f'<g class="chart__row"><title>{_esc(label)}: {thousands(value)} {_esc(unit)}</title>'
                   f'<text class="chart__label chart__label--bar" text-anchor="end">{spans}</text>'
                   f'<rect class="chart__bar" x="{_num(axis_x)}" y="{_num(centre - _BAR_H / 2)}" width="{_num(width)}" '
                   f'height="{_BAR_H}"/><text class="chart__label chart__label--value" x="{_num(axis_x + width + 5)}" '
                   f'y="{_num(centre + FONT * 0.35)}">{thousands(value)}</text></g>')
        y += row_h
    out.append(f'<line class="chart__axis" x1="{_num(axis_x)}" y1="{_BAR_TOP}" x2="{_num(axis_x)}" y2="{height - _BAR_BOTTOM}"/>')
    out.append("</svg>")
    return Chart(Markup("".join(out)), round(height * MM_PER_UNIT, 3), description)
