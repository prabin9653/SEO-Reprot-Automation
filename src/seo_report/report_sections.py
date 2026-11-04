"""The Search Console, geography, GA4, recommendations and contact sections of the report (R5): planning and HTML.

`plan_section_pages(render_input, assets)` turns the typed visuals and text of a `ClientRenderInput` into pages of
blocks; `render_section_pages(...)` renders them with `templates/report/sections.html`. The page shell, header, footer
and page numbers are the R3 ones; the cover and the keyword pages are R3/R4 (`report_document.py` assembles them all).
Nothing here reads a clock, file, browser or network, and nothing is drawn that is not in the render input.

Sections, each starting on a new page, in this order
  Search Console   notices (incomplete / unavailable / truncated), four tiles (Clicks, Impressions, CTR, Average
                   position; a missing one shows "–", a measured 0 shows 0), two daily line charts (clicks, impressions),
                   the section's insight paragraphs and the matching historical statements. (Until R8.7h.4 a "Daily
                   performance" table of every date followed; it was removed as a presentation change. The daily series
                   is still collected, kept in the render input and drawn in the two charts.)
  Geography        notices, a bar chart of the TOP 5 countries (only when there are at least two of them and some clicks;
                   otherwise a fixed message), the paragraphs and statements, and the table of the same top 5 countries.
                   The top 5 are ranked by impressions (descending), then clicks (descending), then country name; the
                   render input still carries every country (R8.7h.4)
  GA4              notices, three tiles (Sessions, Users, Engagement rate), a daily sessions line chart, the paragraphs
                   and statements, the channel table
  Recommendations  every recommendation (title and action) in the given order, then the contact block (the sender,
                   the logo); with no recommendations the list is omitted entirely, with no placeholder
Insight paragraphs come from the `ClientRenderInput.sections` entry with the same key (status "ok" only); the heading
is that section's own title. A historical statement is shown in the section named by its `section` field; statements
for "keywords" are shown at the top of the Search Console pages because the keyword pages (R4) have no place for them;
a statement for any other section raises `ContentPaginationError` (code `report_statement_unsupported`) rather than
being hashed but never shown.

Formats (presentation only): counts with thousands separators; CTR and engagement rate as percentages with two
decimals (the source fraction x 100, rounded half up); average position with one decimal; dates like "9th Oct, 2026".

Pagination and safety (the R4.1 principle). Every block has an estimated height from the same A4 geometry as the keyword
pages (`keyword_pages.usable_height_mm`, the stylesheet's fonts and paddings, words wrapped into the estimated column
widths). Tables are split between rows only: the first part carries the caption and header, every continuation page
repeats the header, rows keep their order, none is dropped, none is split. A heading stays with the next block (for a
table: its header and first row). A row that does not fit where it is moves to the next page. When any element cannot
fit even an otherwise empty page (a table row, a chart, a paragraph, a recommendation, the contact block, a notice, a
heading with the first part of what it introduces), `ContentPaginationError` is raised before anything is rendered; no
document is returned and nothing is clipped, shrunk, truncated or hidden.
"""

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from markupsafe import Markup

from seo_report import keyword_pages as kp
from seo_report import render_input as ri
from seo_report import report_charts as rc
from seo_report import report_templates as rt

PLACEHOLDER = "–"
ERROR_CONTENT_EXCEEDS_PAGE = "report_content_exceeds_page"
ERROR_STATEMENT_UNSUPPORTED = "report_statement_unsupported"
RECOMMENDATIONS_HEADING = "Recommendation and Next Month Targeted SEO growth"
CONTACT_HEADING = "CONTACT"
NO_DAILY = "No daily records are available for this period."
NO_CHART_ONE_COUNTRY = "Only one country is available, so no chart is drawn."
NO_CHART_NO_CLICKS = "No clicks were recorded for these countries, so no chart is drawn."
TABLE_COUNTRIES = 5  # the geography table and chart show this many countries (R8.7h.4)

# --- geometry assumptions (see the module docstring; verified against a browser in R6) ----------------------------------------------------
PT = kp.PT
CONTENT_MM = kp.CONTENT_WIDTH_MM
TABLE_PT = 9.0
TABLE_LINE_MM = TABLE_PT * 1.45 * PT
TABLE_PAD_Y_MM = 1.6
TABLE_PAD_X_MM = 2.0
TABLE_BORDER_MM = 0.2
TABLE_CAPTION_PT = 9.5
TABLE_CAPTION_MM = TABLE_CAPTION_PT * 1.45 * PT + 2.0
TABLE_GAP_MM = 3.0
FIGURE_TITLE_MM = 9.5 * 1.45 * PT + 1.5
CAPTION_LINE_MM = 8.5 * 1.45 * PT
BLOCK_GAP_MM = 4.0
TILE_GAP_MM = 3.0
TILE_PAD_MM = 3.0
TILE_LABEL_PT, TILE_VALUE_PT = 8.5, 16.0
NOTICE_PT = 9.0
NOTICE_LINE_MM = NOTICE_PT * 1.45 * PT
NOTICE_PAD_MM = 2.5 * 2 + 0.5
NOTICE_TEXT_MM = CONTENT_MM - 3.5 * 2 - 1.2
RECOMMENDATION_TITLE_PT = 10.5
RECOMMENDATION_INDENT_MM = 8.0
CONTACT_LINE_MM = 10 * 1.5 * PT


class ContentPaginationError(ValueError):
    """Content cannot be placed on any page, or cannot be shown, so no document is produced.

    code           `report_content_exceeds_page`, or `report_statement_unsupported`
    section        the report section (search_console, geography, ga4, recommendations, contact, keywords)
    element        what does not fit: `table_row:<table>`, `chart`, `paragraph`, `notice`, `recommendation`, `contact`,
                   `heading_group`, or `statement`
    index          the 0-based position of the row, paragraph or recommendation in the supplied order (None otherwise)
    reason         `row_exceeds_empty_page`, `element_exceeds_empty_page`, `group_exceeds_empty_page` or
                   `no_section_for_statement`
    required_mm / available_mm   the estimated height needed and what an otherwise empty page offers
    The message holds these values only, never client text. The error is deterministic for identical input.
    """

    def __init__(self, code, section, element, index, reason, required_mm=0.0, available_mm=0.0):
        where = f"{section} {element}" + ("" if index is None else f" {index}")
        super().__init__(f"{where}: {reason} (needs about {required_mm:.1f} mm; an empty page offers "
                         f"{available_mm:.1f} mm)")
        self.code, self.section, self.element, self.index, self.reason = code, section, element, index, reason
        self.required_mm, self.available_mm = round(required_mm, 3), round(available_mm, 3)


# --- formatting --------------------------------------------------------------------------------------------------------------------------

def fmt_int(value) -> str:
    return PLACEHOLDER if value is None else f"{value:,}"


def fmt_percent(fraction) -> str:
    if fraction is None:
        return PLACEHOLDER
    return f"{(fraction * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"


def fmt_position(value) -> str:
    return PLACEHOLDER if value is None else str(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def fmt_date(day: dt.date) -> str:
    return rt.format_report_date(day)


# --- blocks --------------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    name: str
    percent: int
    align: str  # left or center


@dataclass(frozen=True)
class Block:
    kind: str
    section: str
    mm: float
    data: dict = field(default_factory=dict)
    keep_next: bool = False  # stays on the page of the block that follows it
    index: int | None = None  # position of a paragraph or recommendation in the supplied order


@dataclass(frozen=True)
class TableBlock:
    section: str
    name: str  # daily, countries or channels
    caption: str
    columns: tuple[Column, ...]
    rows: tuple[tuple[str, ...], ...]
    record: str  # the data-record value of each row, for counting
    kind: str = "table"
    keep_next: bool = False


@dataclass(frozen=True)
class Placement:
    block: object  # a Block, or a TableBlock with `rows` limited to this page's slice
    first_slice: bool = True


@dataclass(frozen=True)
class SectionPage:
    number: int  # 1-based among the section pages
    placements: tuple[Placement, ...]
    estimated_mm: float


def _lines(text: str, width_mm: float, char_em: float, pt: float) -> int:
    return kp.wrapped_lines(text, max(1, int(width_mm / (char_em * pt * PT))))


def _paragraph_mm(text: str, width_mm: float = CONTENT_MM) -> float:
    return _lines(text, width_mm, kp.CHAR_EM, kp.BODY_FONT_PT) * kp.LINE_MM + kp.PARAGRAPH_GAP_MM


def _column_chars(column: Column, em: float) -> int:
    return max(1, int((CONTENT_MM * column.percent / 100 - 2 * TABLE_PAD_X_MM) / (em * TABLE_PT * PT)))


def table_header_mm(table: TableBlock) -> float:
    lines = max(kp.wrapped_lines(c.name, _column_chars(c, kp.BOLD_CHAR_EM)) for c in table.columns)
    return lines * TABLE_LINE_MM + 2 * TABLE_PAD_Y_MM + TABLE_BORDER_MM


def table_row_mm(table: TableBlock, row: tuple[str, ...]) -> float:
    lines = max(kp.wrapped_lines(text, _column_chars(c, kp.CHAR_EM)) for text, c in zip(row, table.columns))
    return lines * TABLE_LINE_MM + 2 * TABLE_PAD_Y_MM + TABLE_BORDER_MM


def table_caption_mm(table: TableBlock) -> float:
    return _lines(table.caption, CONTENT_MM, kp.BOLD_CHAR_EM, TABLE_CAPTION_PT) * (TABLE_CAPTION_PT * 1.45 * PT) + 2.0


def _heading(section: str, text: str) -> Block:
    return Block("heading", section, kp.HEADING_MM, {"text": text}, keep_next=True)


def _notice_blocks(section: str, visual, label: str) -> list[Block]:
    notice = rt.build_visual_notice(visual, label)
    html = rt.render_visual_notice(visual, label)
    if not html:
        return []
    boxes = []
    if notice.status == "unavailable":
        boxes.append([notice.message])
    else:
        if notice.message:
            boxes.append(["Incomplete data", notice.message])
        if notice.truncation:
            boxes.append(list(notice.truncation))
    mm = sum(sum(_lines(t, NOTICE_TEXT_MM, kp.CHAR_EM, NOTICE_PT) * NOTICE_LINE_MM + 1.5 for t in box) + NOTICE_PAD_MM
             + BLOCK_GAP_MM for box in boxes)
    return [Block("notice", section, mm, {"html": Markup(html)})]


def _tiles(section: str, items: list[tuple[str, str]]) -> Block:
    n = len(items)
    width = (CONTENT_MM - (n - 1) * TILE_GAP_MM) / n - 2 * TILE_PAD_MM
    label_lines = max(_lines(label, width, kp.CHAR_EM, TILE_LABEL_PT) for label, _ in items)
    value_lines = max(_lines(value, width, kp.BOLD_CHAR_EM, TILE_VALUE_PT) for _, value in items)
    mm = (2 * TILE_PAD_MM + label_lines * TILE_LABEL_PT * 1.3 * PT + value_lines * TILE_VALUE_PT * 1.25 * PT
          + 0.5 + BLOCK_GAP_MM)
    return Block("tiles", section, mm, {"items": tuple(items)})


def _chart_block(section: str, title: str, chart: rc.Chart, caption: str | None = None) -> Block:
    mm = FIGURE_TITLE_MM + chart.height_mm + BLOCK_GAP_MM
    if caption:
        mm += _lines(caption, CONTENT_MM, kp.CHAR_EM, 8.5) * CAPTION_LINE_MM + 1.0
    return Block("chart", section, mm, {"title": title, "svg": chart.svg, "caption": caption})


def _message(section: str, text: str) -> Block:
    return Block("message", section, _paragraph_mm(text), {"text": text})


def _paragraphs(section: str, paragraphs) -> list[Block]:
    return [Block("paragraph", section, _paragraph_mm(p), {"text": p}, index=i) for i, p in enumerate(paragraphs)]


def _statement_blocks(section: str, statements) -> list[Block]:
    blocks = []
    for index, s in enumerate(statements):
        baseline = f"Compared with {fmt_date(s.baseline_start)} to {fmt_date(s.baseline_end)}"
        mm = _paragraph_mm(s.text) + _lines(baseline, CONTENT_MM, kp.CHAR_EM, 8.5) * CAPTION_LINE_MM + 1.0
        blocks.append(Block("statement", section, mm, {"text": s.text, "baseline": baseline}, index=index))
    return blocks


def _section_text(render_input, key: str):
    return next((s for s in render_input.sections if s.key == key), None)


def _section_heading(render_input, key: str, fallback: str) -> str:
    section = _section_text(render_input, key)
    return section.title if section is not None else fallback


def _insight_paragraphs(render_input, key: str) -> tuple[str, ...]:
    section = _section_text(render_input, key)
    if section is None or section.status != "ok":
        return ()
    return tuple(p for p in section.paragraphs if p.strip())


def _statements_for(render_input, key: str):
    return tuple(s for s in render_input.historical_statements if s.section == key)


def _check_statements(render_input):
    supported = {"keywords", "search_console", "geography", "ga4"}
    for index, s in enumerate(render_input.historical_statements):
        if s.section not in supported:
            raise ContentPaginationError(ERROR_STATEMENT_UNSUPPORTED, s.section, "statement", index,
                                         "no_section_for_statement")


def _gap_days(period: ri.RenderPeriod, days) -> int:
    span = (period.end - period.start).days + 1
    return span - len({d for d in days if period.start <= d <= period.end})


# --- section blocks ---------------------------------------------------------------------------------------------------------------------------

def _daily_table(section, name, caption, columns, rows, record) -> TableBlock:
    return TableBlock(section, name, caption, tuple(columns), tuple(rows), record)


def ranked_countries(rows, limit: int = TABLE_COUNTRIES) -> tuple:
    """The `limit` countries with the most impressions: ranked by impressions (descending), then clicks (descending), then the country name
    (case-insensitive, then exact), so equal figures always give the same order. Presentation only: `rows` is not changed and the render input keeps
    every country."""
    return tuple(sorted(rows, key=lambda c: (-c.impressions, -c.clicks, c.name.casefold(), c.name))[:limit])


def search_console_blocks(render_input: ri.ClientRenderInput) -> list:
    key, visual = "search_console", render_input.search_console
    blocks = []
    # statements about the keyword rankings have no place on the keyword pages, so they open this section
    kw_statements = _statements_for(render_input, "keywords")
    if kw_statements:
        blocks.append(_heading("keywords", "Keywords Ranking: history"))
        blocks.extend(_statement_blocks("keywords", kw_statements))
    blocks.append(_heading(key, _section_heading(render_input, key, "Google Search Console")))
    blocks.extend(_notice_blocks(key, visual, "Search Console"))
    if visual.status != "unavailable":
        blocks.append(_tiles(key, [("Clicks", fmt_int(visual.clicks)), ("Impressions", fmt_int(visual.impressions)),
                                   ("CTR", fmt_percent(visual.ctr)),
                                   ("Average position", fmt_position(visual.average_position))]))
        if visual.daily:
            period = render_input.period
            for chart_key, title, unit, pick, style in (
                    ("clicks", "Clicks per day", "clicks", lambda p: p.clicks, "primary"),
                    ("impressions", "Impressions per day", "impressions", lambda p: p.impressions, "secondary")):
                chart = rc.line_chart(chart_id=f"chart-sc-{chart_key}", title=title, unit=unit,
                                      points=[(p.day, pick(p)) for p in visual.daily], period_start=period.start,
                                      period_end=period.end, style=style)
                caption = (f"{chart.gaps} day{'s' if chart.gaps != 1 else ''} in this period have no record and are "
                           "left blank, not shown as zero.") if chart.gaps else None
                blocks.append(_chart_block(key, title, chart, caption))
        else:
            blocks.append(_message(key, NO_DAILY))
    blocks.extend(_paragraphs(key, _insight_paragraphs(render_input, key)))
    blocks.extend(_statement_blocks(key, _statements_for(render_input, key)))
    return blocks


def geography_blocks(render_input: ri.ClientRenderInput) -> list:
    key, visual = "geography", render_input.geography
    blocks = [_heading(key, _section_heading(render_input, key, "Geographical Performance"))]
    blocks.extend(_notice_blocks(key, visual, "Geographic"))
    if visual.status != "unavailable":
        rows = visual.countries
        shown = ranked_countries(rows)  # the chart and the table show the same countries in the same order
        if len(shown) < 2:
            blocks.append(_message(key, NO_CHART_ONE_COUNTRY))
        elif max(c.clicks for c in shown) == 0:
            blocks.append(_message(key, NO_CHART_NO_CLICKS))
        else:
            chart = rc.bar_chart(chart_id="chart-geo-clicks", title="Clicks by country", unit="clicks",
                                 rows=[(c.name, c.clicks) for c in shown])
            caption = (f"The chart shows the top {len(shown)} of {len(rows)} countries by impressions, as in the table."
                       if len(rows) > len(shown) else None)
            blocks.append(_chart_block(key, "Clicks by country", chart, caption))
    blocks.extend(_paragraphs(key, _insight_paragraphs(render_input, key)))
    blocks.extend(_statement_blocks(key, _statements_for(render_input, key)))
    if visual.status != "unavailable":
        partial = any(n.dataset == "countries" for n in visual.truncation)
        shown = ranked_countries(visual.countries)
        caption = ("Countries" if len(visual.countries) <= len(shown)
                   else f"Top {len(shown)} countries by impressions (of {len(visual.countries)})") + (" (not a complete list)" if partial else "")
        blocks.append(_daily_table(
            key, "countries", caption, (Column("Country", 50, "left"), Column("Clicks", 25, "center"),
                                        Column("Impressions", 25, "center")),
            [(c.name, fmt_int(c.clicks), fmt_int(c.impressions)) for c in shown], "country"))
    return blocks


def ga4_blocks(render_input: ri.ClientRenderInput) -> list:
    key, visual = "ga4", render_input.ga4
    blocks = [_heading(key, _section_heading(render_input, key, "Google Analytics 4"))]
    blocks.extend(_notice_blocks(key, visual, "Google Analytics 4"))
    if visual.status != "unavailable":
        blocks.append(_tiles(key, [("Sessions", fmt_int(visual.sessions)), ("Users", fmt_int(visual.users)),
                                   ("Engagement rate", fmt_percent(visual.engagement_rate))]))
        if visual.daily:
            period = render_input.period
            chart = rc.line_chart(chart_id="chart-ga4-sessions", title="Sessions per day", unit="sessions",
                                  points=[(p.day, p.sessions) for p in visual.daily], period_start=period.start,
                                  period_end=period.end)
            notes = []
            if chart.gaps:
                notes.append(f"{chart.gaps} day{'s' if chart.gaps != 1 else ''} in this period have no record and are "
                             "left blank, not shown as zero.")
            if any(n.dataset == "daily" for n in visual.truncation):
                notes.append("The daily series is cut off at the data source and may not cover every day.")
            blocks.append(_chart_block(key, "Sessions per day", chart, " ".join(notes) or None))
        else:
            blocks.append(_message(key, NO_DAILY))
    blocks.extend(_paragraphs(key, _insight_paragraphs(render_input, key)))
    blocks.extend(_statement_blocks(key, _statements_for(render_input, key)))
    if visual.status != "unavailable" and visual.channels:
        partial = any(n.dataset == "channels" for n in visual.truncation)
        caption = "Channel performance" + (" (not a complete list of channels)" if partial else "")
        blocks.append(_daily_table(
            key, "channels", caption, (Column("Channel", 34, "left"), Column("Sessions", 22, "center"),
                                       Column("Users", 20, "center"), Column("Engaged sessions", 24, "center")),
            [(c.channel, fmt_int(c.sessions), fmt_int(c.users), fmt_int(c.engaged_sessions))
             for c in visual.channels], "channel"))
    return blocks


def recommendation_blocks(render_input: ri.ClientRenderInput, assets) -> list:
    key = "recommendations"
    blocks = []
    if render_input.recommendations:
        blocks.append(_heading(key, RECOMMENDATIONS_HEADING))
        width = CONTENT_MM - RECOMMENDATION_INDENT_MM
        for index, r in enumerate(render_input.recommendations):
            mm = (_lines(r.title, width, kp.BOLD_CHAR_EM, RECOMMENDATION_TITLE_PT) * RECOMMENDATION_TITLE_PT * 1.45 * PT
                  + _paragraph_mm(r.action, width) + 1.5)
            blocks.append(Block("recommendation", key, mm, {"title": r.title, "action": r.action}, index=index))
    lines = rt.sender_lines(render_input.sender)
    logo_mm = 38.0 * assets.logo.height_px / assets.logo.width_px
    width = CONTENT_MM
    contact_mm = (kp.HEADING_MM + sum(_lines(line.text, width, kp.BOLD_CHAR_EM, 10) for line in lines) * CONTACT_LINE_MM
                  + 4.0 + logo_mm + BLOCK_GAP_MM)
    blocks.append(Block("contact", "contact", contact_mm, {"lines": lines, "heading": CONTACT_HEADING}))
    return blocks


def section_segments(render_input: ri.ClientRenderInput, assets) -> list[list]:
    """The blocks of each section, in report order; each segment starts on a new page."""
    _check_statements(render_input)
    return [search_console_blocks(render_input), geography_blocks(render_input), ga4_blocks(render_input),
            recommendation_blocks(render_input, assets)]


# --- packing ------------------------------------------------------------------------------------------------------------------------------------

class _Packer:
    def __init__(self, usable: float):
        self.usable = usable
        self.pages: list[list[Placement]] = []
        self.sizes: list[float] = []
        self.items: list[Placement] = []
        self.used = 0.0

    def close(self):
        if self.items:
            self.pages.append(self.items)
            self.sizes.append(round(self.used, 3))
        self.items, self.used = [], 0.0

    def add(self, placement: Placement, mm: float):
        self.items.append(placement)
        self.used += mm

    def fits(self, mm: float) -> bool:
        return self.used + mm <= self.usable


def _error(block, reason, required, available, index=None, element=None):
    return ContentPaginationError(ERROR_CONTENT_EXCEEDS_PAGE, block.section, element or block.kind, index, reason,
                                  required, available)


def _table_first_min(table: TableBlock) -> float:
    return table_caption_mm(table) + table_header_mm(table) + table_row_mm(table, table.rows[0]) + TABLE_GAP_MM


def _check_table(table: TableBlock, usable: float):
    available = usable - table_header_mm(table) - TABLE_GAP_MM  # an empty continuation page: header, row, trailing gap
    for index, row in enumerate(table.rows):
        required = table_row_mm(table, row)
        if required > available:
            raise _error(table, "row_exceeds_empty_page", required, available, index, f"table_row:{table.name}")


def _pack_segment(packer: _Packer, blocks: list, usable: float):
    held: list = []
    for block in blocks:
        if block.keep_next:
            held.append(block)
            continue
        is_table = isinstance(block, TableBlock)
        if is_table:
            _check_table(block, usable)
            minimum = _table_first_min(block)
        else:
            minimum = block.mm
            if block.mm > usable:
                raise _error(block, "element_exceeds_empty_page", block.mm, usable, block.index)
        group = sum(h.mm for h in held) + minimum
        if group > usable:
            raise _error(held[0] if held else block, "group_exceeds_empty_page", group, usable,
                         None, "heading_group" if held else None)
        if not packer.fits(group) and packer.items:
            packer.close()
        for h in held:
            packer.add(Placement(h), h.mm)
        held = []
        if not is_table:
            packer.add(Placement(block), block.mm)
            continue
        header = table_header_mm(block)
        remaining = list(block.rows)
        first = True
        while remaining:
            head_mm = (table_caption_mm(block) if first else 0.0) + header
            slice_rows, used = [], head_mm  # the first part fits (checked with its heading); later parts start a page
            while remaining and packer.used + used + table_row_mm(block, remaining[0]) + TABLE_GAP_MM <= usable:
                used += table_row_mm(block, remaining[0])
                slice_rows.append(remaining.pop(0))
            packer.add(Placement(TableBlock(block.section, block.name, block.caption, block.columns,
                                            tuple(slice_rows), block.record), first), used + TABLE_GAP_MM)
            first = False
            if remaining:
                packer.close()
    if held:  # a heading with nothing after it
        for h in held:
            packer.add(Placement(h), h.mm)


def plan_section_pages(render_input: ri.ClientRenderInput, assets: "rt.TemplateAssets") -> tuple[SectionPage, ...]:
    """The pages of the sections after the keyword pages (see the module docstring)."""
    if not isinstance(render_input, ri.ClientRenderInput):
        raise TypeError(f"render_input must be a ClientRenderInput, got {type(render_input).__name__}")
    if not isinstance(assets, rt.TemplateAssets):
        raise TypeError(f"assets must be TemplateAssets, got {type(assets).__name__}")
    usable = kp.usable_height_mm(assets)
    segments = section_segments(render_input, assets)
    packer = _Packer(usable)
    for blocks in segments:
        packer.close()
        _pack_segment(packer, blocks, usable)
    packer.close()
    return tuple(SectionPage(i + 1, tuple(items), size) for i, (items, size) in enumerate(zip(packer.pages, packer.sizes)))


# --- rendering -----------------------------------------------------------------------------------------------------------------------------------

def render_section_page_body(page: SectionPage, assets: "rt.TemplateAssets") -> Markup:
    return rt.get_template("sections.html").module.section_page_body(page, assets)
