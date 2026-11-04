"""The keyword rankings section of the report: display rules, pagination and HTML (R4).

`plan_keyword_pages(render_input, assets)` turns the keyword rows, the ranking footnote and the "keywords" section of a
`ClientRenderInput` into a tuple of `KeywordPage` view models; `render_keyword_report_document` renders the cover plus
those pages with the R3 shell. HTML only: no browser, PDF, data extraction or finalization is involved, and the render
input and its hash are never changed.

Table (exactly these columns, in this order): Keyword | Location | Previous Rank | Current Rank | Movement.

Display rules (presentation only; source values are never altered and nothing is calculated or inferred)
  rank        position -> the number; above_10 -> the sheet's own text when supplied, else "Above 10"; missing ->
              "Not recorded" (`rankings.NOT_RECORDED`), never 0
  location    the supplied text; blank -> "–" (an en dash), never an invented place
  movement    the model's `movement` value only: positive "▲ n" (improved), negative "▼ n" (declined), 0 "0" (no
              change), None "–". It is not recomputed from the rankings, and None is never read as 0

Pagination. Pages are planned explicitly, because the page shell clips overflow and must never be what handles a long
table. The planner estimates the height of every element from the A4 geometry in the stylesheet and gives each page
only the rows it can hold:
  * A4 height 297 mm; the header takes 10 mm top padding + the logo (38 mm wide, so 38 x height/width mm tall) + 3.5 mm
    + 0.3 mm rule; the footer takes 3 mm + one 8.5 pt line + 8 mm + 0.2 mm; the rest is the page body
  * `SAFETY_MM` (8 mm) of the body is held back for rounding differences between this estimate and the browser
  * text width is 174 mm (210 - 22 - 14); a character is estimated at 0.62 em (0.70 em for bold headings), a line is
    10 pt x 1.45 high, table cells have 3.4 mm vertical and 2 mm horizontal padding and a 0.2 mm border
  * the number of lines of a cell is found by word-wrapping its text into the column's character capacity (words
    longer than a line are broken, as `overflow-wrap: anywhere` does), so long keywords and locations get taller rows
  * rows are placed in the original order; a row that does not fit starts a new page; a row is never split; every
    page repeats the complete header row; no keyword is ever dropped; there is no fixed row count or maximum
  * the footnote stays with the last row (the last row moves to the next page rather than being separated from it)
  * the insights heading stays with the first paragraph; the insights come only after the last row, never on
    earlier pages; when they do not fit under the last rows they continue on following pages
  * zero keywords draw one page with a fixed message instead of a table
Oversized rows (R4.1). A row is judged against a completely fresh continuation page (`fresh_page_capacity_mm`: the
usable body minus the page heading and the repeated header row), not against the space left on the current page. A
row that merely does not fit where it is moves to a new page (if it is the very first row and the first page, with its
website line, is too small, the first page then holds only its heading and the row starts the next page). A row that
cannot fit even a fresh page (or, for the last row, together with the footnote that stays with it) cannot be split,
shrunk, truncated or hidden, so `plan_keyword_pages` raises `KeywordPaginationError` before anything is planned or
rendered; no document is returned. `KeywordPage.overflow` is therefore only ever True for the non-row elements (an
insights paragraph taller than a page, a very long website line); rows never set it.

These estimates are assumptions: the physical fit must be verified with Chromium in R6.
"""

import math
from dataclasses import dataclass

from markupsafe import Markup

from seo_report import render_input as ri
from seo_report import report_templates as rt
from seo_report.rankings import NOT_RECORDED

COLUMNS = (("Keyword", 34, "left"), ("Location", 22, "left"), ("Previous Rank", 14, "center"),
           ("Current Rank", 14, "center"), ("Movement", 16, "center"))
PLACEHOLDER = "–"  # en dash: no value
ABOVE_10 = "Above 10"
NO_KEYWORDS = "No keyword rankings were supplied for this report."
HEADING = "Keywords Ranking"
HEADING_CONTINUED = "Keywords Ranking (continued)"
INSIGHTS_HEADING = "Keywords Ranking Insights"
INSIGHTS_SECTION_KEY = "keywords"

# --- geometry assumptions (millimetres unless noted; see the module docstring) ----------------------------------------------------------
PT = 25.4 / 72
PAGE_HEIGHT_MM = 297.0
PAGE_WIDTH_MM = 210.0
CONTENT_WIDTH_MM = PAGE_WIDTH_MM - 22.0 - 14.0
LOGO_WIDTH_MM = 38.0
HEADER_FIXED_MM = 10.0 + 3.5 + 0.3
FOOTER_MM = 3.0 + 8.5 * 1.45 * PT + 8.0 + 0.2
SAFETY_MM = 8.0
BODY_FONT_PT = 10.0
LINE_MM = BODY_FONT_PT * 1.45 * PT
CELL_PAD_Y_MM = 3.4
CELL_PAD_X_MM = 2.0
BORDER_MM = 0.2
CHAR_EM = 0.62
BOLD_CHAR_EM = 0.70
CHAR_MM = CHAR_EM * BODY_FONT_PT * PT
BOLD_CHAR_MM = BOLD_CHAR_EM * BODY_FONT_PT * PT
HEADING_MM = 12 * 1.25 * PT + 6.0 + 3.0
PARAGRAPH_GAP_MM = 3.0
FOOTNOTE_PT = 8.5
FOOTNOTE_LINE_MM = FOOTNOTE_PT * 1.45 * PT
FOOTNOTE_GAP_MM = 3.0 + 3.0


def body_height_mm(assets: "rt.TemplateAssets") -> float:
    """The page body height for these assets (the logo's aspect ratio sets the header height)."""
    logo = assets.logo
    header = HEADER_FIXED_MM + LOGO_WIDTH_MM * logo.height_px / logo.width_px
    return PAGE_HEIGHT_MM - header - FOOTER_MM


def usable_height_mm(assets) -> float:
    return body_height_mm(assets) - SAFETY_MM


# --- display ---------------------------------------------------------------------------------------------------------------------------

def rank_text(ranking: ri.RenderRanking) -> str:
    if ranking.kind == "position":
        return str(ranking.position)
    if ranking.kind == "above_10":
        return ranking.text if ranking.text and ranking.text.strip() else ABOVE_10
    return NOT_RECORDED


def location_text(location: str) -> str:
    return location if location.strip() else PLACEHOLDER


@dataclass(frozen=True)
class MovementView:
    kind: str  # up, down, none or missing
    text: str
    label: str  # a plain-words description for assistive technology


def movement_view(movement) -> MovementView:
    if movement is None:
        return MovementView("missing", PLACEHOLDER, "No movement available")
    if movement > 0:
        return MovementView("up", f"▲ {movement}", f"Improved by {movement} {'position' if movement == 1 else 'positions'}")
    if movement < 0:
        return MovementView("down", f"▼ {-movement}", f"Declined by {-movement} {'position' if movement == -1 else 'positions'}")
    return MovementView("none", "0", "No change")


@dataclass(frozen=True)
class KeywordRowView:
    index: int  # position in the supplied keyword order (0-based)
    keyword: str
    location: str
    previous: str
    current: str
    movement: MovementView


@dataclass(frozen=True)
class KeywordPage:
    number: int  # 1-based position among the keyword pages
    heading: str | None
    website: str | None  # shown on the first page only
    website_href: str | None
    rows: tuple[KeywordRowView, ...]
    empty_message: str | None
    footnote: str | None
    insights_heading: str | None
    paragraphs: tuple[str, ...]
    estimated_mm: float
    overflow: bool  # a non-row element (an insights paragraph, a website line) is taller than the page body


def row_views(keywords) -> tuple[KeywordRowView, ...]:
    return tuple(KeywordRowView(i, k.keyword, location_text(k.location), rank_text(k.previous), rank_text(k.current),
                                movement_view(k.movement)) for i, k in enumerate(keywords))


# --- height estimates --------------------------------------------------------------------------------------------------------------------

def wrapped_lines(text: str, capacity: int) -> int:
    """Lines needed to word-wrap `text` into lines of `capacity` characters (long words are broken)."""
    capacity = max(1, capacity)
    total = 0
    for part in text.split("\n"):
        words = part.split()
        lines, current = 1, 0
        for word in words:
            length = len(word)
            if length > capacity:
                if current > 0:
                    lines += 1
                extra = (length - 1) // capacity
                lines += extra
                current = length - extra * capacity
            elif current == 0:
                current = length
            elif current + 1 + length <= capacity:
                current += 1 + length
            else:
                lines += 1
                current = length
        total += lines
    return total


def _column_capacity(percent: int, char_mm: float) -> int:
    width = CONTENT_WIDTH_MM * percent / 100 - 2 * CELL_PAD_X_MM
    return max(1, int(width / char_mm))


def _cell_height(lines: int) -> float:
    return lines * LINE_MM + 2 * CELL_PAD_Y_MM + BORDER_MM


def header_row_mm() -> float:
    lines = max(wrapped_lines(name, _column_capacity(percent, BOLD_CHAR_MM)) for name, percent, _ in COLUMNS)
    return _cell_height(lines)


def row_mm(row: KeywordRowView) -> float:
    texts = (row.keyword, row.location, row.previous, row.current, row.movement.text)
    lines = max(wrapped_lines(text, _column_capacity(percent, CHAR_MM)) for text, (_, percent, _) in zip(texts, COLUMNS))
    return _cell_height(lines)


def paragraph_mm(text: str) -> float:
    return wrapped_lines(text, int(CONTENT_WIDTH_MM / CHAR_MM)) * LINE_MM + PARAGRAPH_GAP_MM


def footnote_mm(text: str) -> float:
    capacity = int(CONTENT_WIDTH_MM / (CHAR_EM * FOOTNOTE_PT * PT))
    return wrapped_lines(text, capacity) * FOOTNOTE_LINE_MM + FOOTNOTE_GAP_MM


def website_mm(text: str) -> float:
    return wrapped_lines(text, int(CONTENT_WIDTH_MM / CHAR_MM)) * LINE_MM + PARAGRAPH_GAP_MM


# --- oversized rows ----------------------------------------------------------------------------------------------------------------------

ERROR_ROW_EXCEEDS_PAGE = "keyword_row_exceeds_page"
REASON_ROW = "row_exceeds_empty_page"
REASON_ROW_WITH_FOOTNOTE = "row_and_footnote_exceed_empty_page"
_COLUMN_KEYS = ("keyword", "location", "previous", "current", "movement")


class KeywordPaginationError(ValueError):
    """A keyword row cannot be placed on any page, so no document is produced.

    code           always `keyword_row_exceeds_page` (stable)
    row_index      the 0-based position of the row in the supplied keyword order
    reason         `row_exceeds_empty_page`, or `row_and_footnote_exceed_empty_page` when the row is the last one and
                   the row together with the ranking footnote (which stays with it) is too tall
    column         the column whose text needs the most lines (keyword, location, previous, current or movement)
    required_mm    the estimated height the row (and footnote) needs
    available_mm   the height an otherwise empty continuation page offers for it (page body, minus the safety reserve,
                   the page heading and the repeated table header)
    The message holds these numbers and the row position only: never the keyword, the location or any other text.
    """

    def __init__(self, row_index: int, reason: str, column: str, required_mm: float, available_mm: float):
        super().__init__(f"keyword row {row_index}: {reason} (needs about {required_mm:.1f} mm; an empty page offers "
                         f"{available_mm:.1f} mm; the tallest cell is in the {column} column)")
        self.code = ERROR_ROW_EXCEEDS_PAGE
        self.row_index = row_index
        self.reason = reason
        self.column = column
        self.required_mm = round(required_mm, 3)
        self.available_mm = round(available_mm, 3)


def _tallest_column(row: KeywordRowView) -> str:
    texts = (row.keyword, row.location, row.previous, row.current, row.movement.text)
    lines = [wrapped_lines(text, _column_capacity(percent, CHAR_MM)) for text, (_, percent, _) in zip(texts, COLUMNS)]
    return _COLUMN_KEYS[lines.index(max(lines))]


def _oversized(index: int, row: KeywordRowView, required: float, available: float, footnote: float):
    return KeywordPaginationError(index, REASON_ROW_WITH_FOOTNOTE if footnote else REASON_ROW, _tallest_column(row),
                                  required, available)


def fresh_page_capacity_mm(assets: "rt.TemplateAssets") -> float:
    """What an otherwise empty continuation page offers a table row: the usable page body (body height minus the
    safety reserve) less the page heading and the repeated header row. A continuation page has no website line, so
    this is the most room any row can ever get."""
    return usable_height_mm(assets) - HEADING_MM - header_row_mm()


def ensure_rows_fit(rows, assets: "rt.TemplateAssets", footnote_height: float = 0.0) -> None:
    """Raise `KeywordPaginationError` for the first row (in the supplied order) that cannot fit on a fresh page.

    A row is judged against an empty page, never against the space left on the current one: a row that merely does
    not fit where it is moves to the next page. The last row must fit together with the footnote, which stays with it.
    """
    capacity = fresh_page_capacity_mm(assets)
    for position, row in enumerate(rows):
        alone = row_mm(row)
        if alone > capacity:
            raise _oversized(position, row, alone, capacity, 0.0)
        extra = footnote_height if position == len(rows) - 1 else 0.0
        if extra and alone + extra > capacity:  # the row fits alone; it is the footnote that cannot stay with it
            raise _oversized(position, row, alone + extra, capacity, extra)


# --- planning -----------------------------------------------------------------------------------------------------------------------------

class _Builder:
    def __init__(self, usable: float):
        self.usable = usable
        self.pages = []
        self.used = 0.0
        self.rows = []
        self.heading = None
        self.website = None
        self.website_href = None
        self.empty_message = None
        self.footnote = None
        self.insights_heading = None
        self.paragraphs = []
        self.overflow = False

    def start(self, *, heading, website=None, website_href=None):
        self.heading, self.website, self.website_href = heading, website, website_href
        self.used = (HEADING_MM if heading else 0.0) + (website_mm(website) if website else 0.0)
        self.rows, self.empty_message, self.footnote = [], None, None
        self.insights_heading, self.paragraphs, self.overflow = None, [], False

    def fits(self, height: float) -> bool:
        return self.used + height <= self.usable

    def close(self):
        self.pages.append(KeywordPage(
            number=len(self.pages) + 1, heading=self.heading, website=self.website, website_href=self.website_href,
            rows=tuple(self.rows), empty_message=self.empty_message, footnote=self.footnote,
            insights_heading=self.insights_heading, paragraphs=tuple(self.paragraphs),
            estimated_mm=round(self.used, 3), overflow=self.overflow))


def _insights(render_input: ri.ClientRenderInput) -> tuple[str, ...]:
    section = next((s for s in render_input.sections if s.key == INSIGHTS_SECTION_KEY), None)
    if section is None or section.status != "ok":
        return ()
    return tuple(p for p in section.paragraphs if p.strip())


def plan_keyword_pages(render_input: ri.ClientRenderInput, assets: "rt.TemplateAssets") -> tuple[KeywordPage, ...]:
    """The keyword pages, in order, for this render input (see the module docstring for the algorithm)."""
    if not isinstance(render_input, ri.ClientRenderInput):
        raise TypeError(f"render_input must be a ClientRenderInput, got {type(render_input).__name__}")
    if not isinstance(assets, rt.TemplateAssets):
        raise TypeError(f"assets must be TemplateAssets, got {type(assets).__name__}")
    usable = usable_height_mm(assets)
    rows = row_views(render_input.keywords)
    footnote = render_input.ranking_footnote if render_input.ranking_footnote and render_input.ranking_footnote.strip() else None
    footnote_height = footnote_mm(footnote) if footnote else 0.0
    paragraphs = _insights(render_input)
    header_height = header_row_mm()
    ensure_rows_fit(rows, assets, footnote_height)  # before anything is planned or rendered
    website = render_input.client.website
    builder = _Builder(usable)
    builder.start(heading=HEADING, website=website, website_href=rt.safe_href(website))

    if not rows:
        builder.empty_message = NO_KEYWORDS
        builder.used += paragraph_mm(NO_KEYWORDS)
    else:
        for position, row in enumerate(rows):
            last = position == len(rows) - 1
            height = row_mm(row) + (0.0 if builder.rows else header_height) + (footnote_height if last else 0.0)
            fresh_page = not builder.rows and builder.heading == HEADING_CONTINUED
            if not builder.fits(height) and not fresh_page:
                # it does not fit here; `ensure_rows_fit` has shown that it fits a fresh continuation page
                builder.close()
                builder.start(heading=HEADING_CONTINUED)
                height = row_mm(row) + header_height + (footnote_height if last else 0.0)
            if not builder.fits(height):  # cannot happen after `ensure_rows_fit`; never place a row that does not fit
                raise _oversized(position, row, height, usable - builder.used, footnote_height if last else 0.0)
            builder.rows.append(row)
            builder.used += height
    if footnote:
        builder.footnote = footnote
        if not rows:
            builder.used += footnote_height  # with rows, its height was reserved together with the last row

    # the insights follow the last row; the heading stays with the first paragraph
    for index, paragraph in enumerate(paragraphs):
        block = paragraph_mm(paragraph)
        if index == 0:
            block += HEADING_MM
        if not builder.fits(block) and (builder.rows or builder.paragraphs or builder.empty_message):
            builder.close()
            builder.start(heading=None)
            if index > 0:
                builder.insights_heading = None
        if index == 0:
            builder.insights_heading = INSIGHTS_HEADING
        if not builder.fits(block):
            builder.overflow = True
        builder.paragraphs.append(paragraph)
        builder.used += block
    builder.close()
    return tuple(builder.pages)


# --- rendering ---------------------------------------------------------------------------------------------------------------------------------

def _page_body(page: KeywordPage) -> Markup:
    template = rt._environment().get_template("keywords.html")
    return template.module.keyword_page_body(page, COLUMNS)


def render_keyword_report_document(render_input: ri.ClientRenderInput, assets: "rt.TemplateAssets") -> str:
    """The cover (page 1) followed by the keyword pages (pages 2, 3, ...), as one HTML document."""
    pages = [rt.cover_page_spec(render_input)]
    for page in plan_keyword_pages(render_input, assets):
        pages.append({"kind": "keywords", "number": page.number + 1, "body": _page_body(page)})
    return rt.render_document(render_input, assets, pages)
