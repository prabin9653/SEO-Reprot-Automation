"""Pure checks on a rendered report PDF and on the browser's layout measurements (R6).

Nothing here starts a browser or touches a file: it reads bytes and plain measurement dictionaries.

  inspect_pdf(data)            page count, page boxes, embedded fonts, header/trailer and cross-reference consistency
  normalize_pdf_dates(data, d) the R0 approach: the two creation/modification date strings are replaced, in place and
                               with exactly the same length, by a fixed value derived from the generation date, so the
                               cross-reference offsets stay valid and nothing else changes
  analyse_layout(measures, ...) turns the measurements of each page (see `pdf_renderer.MEASURE_SCRIPT`) into
                               `LayoutIssue`s: vertical or horizontal overflow, clipped or cut table rows, overlapping
                               blocks, broken images and charts, blank pages, missing header or footer, clipped text,
                               unexpected fonts, and requests other than local files
  compare_estimates(...)       measured content height against the R4/R5 pagination estimate, page by page

A `LayoutIssue` is data (page, code, a plain description with the measured numbers). Issues are reported, never fixed:
the checks do not shrink, hide, truncate or move anything.
"""

import datetime as dt
import re
from dataclasses import dataclass

PX_PER_MM = 96 / 25.4
A4_PT = (595.28, 841.89)  # Chromium writes 594.96 x 841.92 (A4 in CSS pixels); both are A4
A4_TOLERANCE_PT = 1.5
TOLERANCE_PX = 0.75  # sub-pixel rounding of the browser's layout


class PdfValidationError(ValueError):
    """The bytes are not a PDF this module can inspect, or an argument is invalid. No file content is included."""


# --- the PDF ---------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PdfInfo:
    byte_count: int
    version: str
    page_count: int
    page_boxes: tuple[tuple[float, float], ...]  # (width, height) in points of each page's MediaBox
    fonts: tuple[str, ...]  # BaseFont names without the subset prefix, sorted and unique
    xref_consistent: bool
    ends_with_eof: bool
    has_image: bool


def inspect_pdf(data: bytes) -> PdfInfo:
    if not isinstance(data, (bytes, bytearray)) or not data.startswith(b"%PDF-"):
        raise PdfValidationError("the data is not a PDF (no %PDF- header)")
    data = bytes(data)
    pages = re.findall(rb"/Type\s*/Page(?![a-zA-Z])", data)
    boxes = []
    for match in re.finditer(rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]", data):
        x0, y0, x1, y1 = (float(v) for v in match.groups())
        boxes.append((round(x1 - x0, 2), round(y1 - y0, 2)))
    fonts = sorted({re.sub(r"^[A-Z]{6}\+", "", m.decode("latin-1"))
                    for m in re.findall(rb"/BaseFont\s*/([A-Za-z0-9+,_-]+)", data)})
    return PdfInfo(len(data), data[:8].decode("latin-1").strip(), len(pages), tuple(boxes), tuple(fonts),
                   _xref_consistent(data), data.rstrip().endswith(b"%%EOF"), b"/Subtype /Image" in data
                   or b"/Subtype/Image" in data)


def _xref_consistent(data: bytes) -> bool:
    tail = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", data)
    if tail is None:
        return False
    start = int(tail.group(1))
    if data[start:start + 4] != b"xref":
        return False
    head = re.match(rb"xref\s+0 (\d+)\s", data[start:])
    if head is None:
        return False
    count, body = int(head.group(1)), data[start + head.end():]
    for index in range(count):
        entry = body[index * 20:(index + 1) * 20]
        if len(entry) < 18:
            return False
        offset, kind = int(entry[:10]), entry[17:18]
        if kind == b"n" and not data[offset:offset + 24].startswith(b"%d 0 obj" % index):
            return False
    return True


def is_a4(box: tuple[float, float]) -> bool:
    return abs(box[0] - A4_PT[0]) <= A4_TOLERANCE_PT and abs(box[1] - A4_PT[1]) <= A4_TOLERANCE_PT


_DATE_FIELDS = (rb"/CreationDate", rb"/ModDate")


def normalize_pdf_dates(data: bytes, generation_date: dt.date) -> bytes:
    """Replace the creation and modification dates by `D:<generation date>000000+00'00'`, keeping every byte offset.

    The browser writes the wall-clock time there, the only thing that differs between two renders of the same page
    (R0). The replacement has the same length, so the cross-reference table stays valid. Raises PdfValidationError when
    a date does not have the expected form.
    """
    if not isinstance(generation_date, dt.date) or isinstance(generation_date, dt.datetime):
        raise PdfValidationError("the generation date must be a date")
    fixed = f"{generation_date:%Y%m%d}000000+00'00'".encode("ascii")
    out, replaced = data, 0
    for field in _DATE_FIELDS:
        def swap(match):
            nonlocal replaced
            inner = match.group(2)
            if len(inner) != len(fixed) or not re.fullmatch(rb"\d{14}[+-]\d{2}'\d{2}'", inner):
                raise PdfValidationError("a PDF date has an unexpected form")
            replaced += 1
            return match.group(1) + fixed + match.group(3)
        out = re.sub(rb"(" + field + rb"\s*\(D:)([^)]*)(\))", swap, out)
    if replaced == 0:
        raise PdfValidationError("the PDF has no creation or modification date to normalize")
    if len(out) != len(data):
        raise PdfValidationError("normalization changed the length")
    return out


# --- layout measurements ------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LayoutIssue:
    page: int  # 1-based page number in the document
    code: str
    detail: str  # plain words with the measured numbers (never client text)


def _mm(px: float) -> float:
    return round(px / PX_PER_MM, 2)


def analyse_layout(measures: list[dict], *, expected_fonts=("DejaVu Sans",), blocked_requests=()) -> tuple[LayoutIssue, ...]:
    """Issues found in the per-page measurements. `measures` come from `pdf_renderer.MEASURE_SCRIPT` (one dict per
    `.page` in document order); an empty list is itself an issue."""
    issues: list[LayoutIssue] = []
    if not measures:
        return (LayoutIssue(0, "no_pages", "the document has no .page elements"),)
    for url in blocked_requests:
        issues.append(LayoutIssue(0, "external_request", "a request other than a local file or data URL was blocked"))
        break
    for index, m in enumerate(measures, start=1):
        add = lambda code, detail: issues.append(LayoutIssue(index, code, detail))
        if abs(m["pageWidth"] - 210 * PX_PER_MM) > TOLERANCE_PX or abs(m["pageHeight"] - 297 * PX_PER_MM) > TOLERANCE_PX:
            add("page_not_a4", f"the page measures {_mm(m['pageWidth'])} x {_mm(m['pageHeight'])} mm")
        if not m["hasHeader"]:
            add("missing_header", "the page has no header")
        if m["kind"] != "cover" and not m["hasFooter"]:
            add("missing_footer", "the page has no footer")
        if not m["logoLoaded"]:
            add("logo_not_loaded", "the logo image did not load")
        if m["textLength"] == 0 and m["svgCount"] == 0 and m["tableRows"] == 0:
            add("blank_page", "the page body has no text, table or chart")
        over = m["contentBottom"] - m["bodyBottom"]
        if over > TOLERANCE_PX or m["scrollHeight"] - m["clientHeight"] > TOLERANCE_PX:
            add("vertical_overflow", f"content ends {_mm(max(over, 0))} mm below the page body "
                                     f"(scroll height {_mm(m['scrollHeight'])} mm, body {_mm(m['clientHeight'])} mm)")
        right = m["contentRight"] - m["frameRight"]
        if right > TOLERANCE_PX:
            add("horizontal_overflow", f"content extends {_mm(right)} mm beyond the right edge of the page frame")
        for row in m["clippedRows"]:
            add("table_row_cut", f"a table row ({row['table']}, row {row['index']}) reaches {_mm(row['over'])} mm "
                                 "beyond the page body")
        for pair in m["overlaps"]:
            add("overlap", f"block {pair['second']} starts {_mm(pair['by'])} mm above the end of block {pair['first']}")
        for svg in m["svgs"]:
            if svg["width"] <= 0 or svg["height"] <= 0 or svg["shapes"] == 0:
                add("broken_chart", f"chart {svg['id']} has no visible size or no drawn shapes")
            if svg["textOutside"]:
                add("chart_text_outside", f"chart {svg['id']} has {svg['textOutside']} label(s) outside its drawing area")
        for cell in m["clippedText"][:5]:
            add("text_clipped", f"{cell['tag']} text is {_mm(cell['by'])} mm wider than its box")
        for font in m["fonts"]:
            if font["family"] not in expected_fonts:
                add("unexpected_font", f"glyphs drawn with {font['family']} ({font['glyphs']})")
    return tuple(issues)


@dataclass(frozen=True)
class EstimateComparison:
    page: int
    kind: str
    estimated_mm: float
    measured_mm: float
    usable_mm: float

    @property
    def delta_mm(self) -> float:
        return round(self.measured_mm - self.estimated_mm, 2)

    @property
    def within_usable(self) -> bool:
        return self.measured_mm <= self.usable_mm + 0.2


def compare_estimates(measures: list[dict], estimates: list, usable_mm: float) -> tuple[EstimateComparison, ...]:
    """Pair each measured page with its planned estimate. `estimates` is a list of (kind, estimated_mm) in document
    order, the same length as `measures`; the cover has no estimate (None) and is skipped."""
    if len(measures) != len(estimates):
        raise PdfValidationError(f"{len(measures)} measured pages but {len(estimates)} planned pages")
    result = []
    for index, (m, (kind, estimated)) in enumerate(zip(measures, estimates), start=1):
        if estimated is None:
            continue
        result.append(EstimateComparison(index, kind, round(estimated, 2), _mm(m["contentExtent"]), round(usable_mm, 2)))
    return tuple(result)
