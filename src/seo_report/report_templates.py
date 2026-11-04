"""Rendering of the Jasper IT report HTML from a `ClientRenderInput` (R3: page shell, branding, cover, notices).

This module turns the immutable render input (R1) into HTML with Jinja2 and the print stylesheet in
`templates/report/`. It is presentation only: it decides wording, formats the date, builds links and prepares view
models in Python, and the templates just draw them. It does not extract data (R2), build tables or charts (later
stages), start a browser, write a PDF or touch the finalization workflow.

  render_cover_document(render_input, assets)       a complete HTML document with the cover page
  render_shell_page_document(render_input, assets,  a document with one interior page (header, body, footer): the
                             page_number=..., body=...)    shell that later stages fill
  build_visual_notice(visual, label) / render_visual_notice(visual, label)
                                                    the message for an `available`, `incomplete` or `unavailable`
                                                    Search Console, geography or GA4 visual, with truncation notes

Determinism: the output depends only on the arguments and the template files. There is no clock, random value,
environment variable, absolute path or network access; Jinja2 runs with autoescaping and `StrictUndefined`, so client
text is escaped and a misspelt variable is an error rather than an empty string. Dates use a fixed English month
table, not the locale.

Assets: the caller supplies where the logo and optional fonts are, as relative locations or `data:` URLs
(`TemplateAssets`). Fonts are never bundled here; without font locations the stylesheet asks for an installed
"DejaVu Sans" and falls back to the generic sans-serif. Page numbers are supplied by the document builder (it knows
the page order); a page without a number simply has none. Nothing here invents a page number or a client detail.
"""

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import jinja2
from markupsafe import Markup

from seo_report import render_input as ri

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates" / "report"
STYLESHEET = "report.css"
REPORT_TITLE = "SEARCH ENGINE OPTIMIZATION REPORT"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FONT_WEIGHTS = ("400", "700")
_FONT_STYLES = ("normal", "italic")
_FAMILY = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}")
_UNSAFE_HREF_CHARS = re.compile(r"[\x00-\x1f\s\"'<>\\`]")


class TemplateInputError(ValueError):
    """The assets or arguments are not usable. The message names the argument, never a value."""


# --- assets --------------------------------------------------------------------------------------------------------------------

def _location(value, where: str) -> str:
    """A location that does not depend on the machine: relative, or a `data:` URL. Never absolute or `file:`."""
    if not isinstance(value, str) or not value or len(value) > 2_000_000:
        raise TemplateInputError(f"{where}: must be a non-empty location")
    if value.startswith("data:"):
        return value
    if (re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", value) or value.startswith(("/", "\\", "//", "~")) or "\\" in value
            or ".." in value.split("/") or _UNSAFE_HREF_CHARS.search(value)):
        raise TemplateInputError(f"{where}: must be a relative location or a data: URL, not an absolute path")
    return value


@dataclass(frozen=True)
class LogoAsset:
    """Where the logo is and its pixel size. The size fixes the aspect ratio (the image is never stretched)."""

    href: str
    width_px: int
    height_px: int

    def __post_init__(self):
        _location(self.href, "logo.href")
        for name in ("width_px", "height_px"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise TemplateInputError(f"logo.{name}: must be a positive whole number")


@dataclass(frozen=True)
class FontFace:
    """One font file location for `@font-face` (the file itself is not read or copied here)."""

    family: str
    href: str
    weight: str = "400"
    style: str = "normal"

    def __post_init__(self):
        if not isinstance(self.family, str) or not _FAMILY.fullmatch(self.family):
            raise TemplateInputError("font.family: must be a short font family name")
        _location(self.href, "font.href")
        if self.weight not in _FONT_WEIGHTS or self.style not in _FONT_STYLES:
            raise TemplateInputError("font: weight must be 400 or 700 and style normal or italic")


@dataclass(frozen=True)
class TemplateAssets:
    logo: LogoAsset
    fonts: tuple[FontFace, ...] = ()

    def __post_init__(self):
        if not isinstance(self.logo, LogoAsset):
            raise TemplateInputError("assets.logo: must be a LogoAsset")
        fonts = tuple(self.fonts)
        if any(not isinstance(f, FontFace) for f in fonts):
            raise TemplateInputError("assets.fonts: must be FontFace objects")
        object.__setattr__(self, "fonts", fonts)


def read_png_size(path) -> tuple[int, int]:
    """(width, height) in pixels from the PNG header of a local file. Reads 24 bytes; changes nothing."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
    except OSError:
        raise TemplateInputError("png: the file cannot be read") from None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise TemplateInputError("png: not a PNG file")
    width, height = struct.unpack(">II", head[16:24])
    if width < 1 or height < 1:
        raise TemplateInputError("png: invalid size")
    return width, height


# --- presentation helpers (pure) ----------------------------------------------------------------------------------------------------

def format_report_date(day) -> str:
    """`2026-10-09` -> "9th Oct, 2026" (the reference report's form, fixed English months)."""
    import datetime as dt
    if not isinstance(day, dt.date) or isinstance(day, dt.datetime):
        raise TemplateInputError("date: must be a date")
    suffix = "th" if 10 <= day.day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day.day % 10, "th")
    return f"{day.day}{suffix} {_MONTHS[day.month - 1]}, {day.year}"


def safe_href(url) -> str | None:
    """The URL when it is a plain http(s) address that is safe to put in a link, otherwise None (shown as text)."""
    if not isinstance(url, str) or not url or _UNSAFE_HREF_CHARS.search(url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return url if parts.scheme in ("http", "https") and parts.netloc else None


@dataclass(frozen=True)
class SenderLine:
    text: str
    href: str | None = None


def sender_lines(sender: ri.RenderSender) -> tuple[SenderLine, ...]:
    """The sender block, one line per supplied value; absent values produce no line and no label."""
    lines = [SenderLine(sender.name)]
    if sender.title:
        lines.append(SenderLine(sender.title))
    lines.extend(SenderLine(line) for line in sender.address_lines if line.strip())
    if sender.phone:
        lines.append(SenderLine(f"Cell: {sender.phone}"))
    if sender.email:
        lines.append(SenderLine(f"E-mail: {sender.email}"))
    href = safe_href(sender.website_url) if sender.website_url else None
    if sender.website_label:
        lines.append(SenderLine(f"Website: {sender.website_label}", href))
    elif sender.website_url:
        lines.append(SenderLine(f"Website: {sender.website_url}", href))
    return tuple(lines)


_NOUNS = {"countries": "countries", "channels": "channels", "daily": "daily records"}


def _noun(dataset: str) -> str:
    return _NOUNS.get(dataset, dataset.replace("_", " "))


def _truncation_line(note: ri.TruncationNote) -> str:
    noun = _noun(note.dataset)
    if note.reason == "source_limit":
        return f"The {noun} list is cut off at the data source, so more {noun} exist than are shown."
    if note.reason == "snapshot_cap":
        return f"The {noun} list was capped when the report data was stored, so more {noun} may exist than are shown."
    rows = "row" if note.omitted_rows == 1 else "rows"
    return f"Not every entry of the {noun} list is shown: {note.omitted_rows} more {rows} left out."


@dataclass(frozen=True)
class VisualNotice:
    """What to say about a visual: its status, one message (incomplete and unavailable only) and truncation lines."""

    status: str
    message: str | None
    truncation: tuple[str, ...]


def build_visual_notice(visual, label: str) -> VisualNotice:
    """The notice for a `SearchConsoleVisuals`, `GeographyVisuals` or `Ga4Visuals`. `label` names the data, for
    example "Search Console". Wording is fixed; no omitted count is stated unless the model holds an exact one."""
    if not isinstance(visual, (ri.SearchConsoleVisuals, ri.GeographyVisuals, ri.Ga4Visuals)):
        raise TypeError(f"visual must be a render-input visual, got {type(visual).__name__}")
    if not isinstance(label, str) or not label.strip():
        raise TemplateInputError("label: must be non-empty text")
    if visual.status == "unavailable":
        return VisualNotice("unavailable", f"{label} data is not available for this report.", ())
    message = None
    if visual.status == "incomplete":
        message = (f"{label} data is incomplete for this period. The figures shown are based on the data that was "
                   "available and may change.")
    notes = getattr(visual, "truncation", ())
    return VisualNotice(visual.status, message, tuple(_truncation_line(n) for n in notes))


# --- rendering -------------------------------------------------------------------------------------------------------------------------

def _environment() -> jinja2.Environment:
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True,
                              undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True,
                              keep_trailing_newline=True)


def get_template(name: str):
    """A report template by file name (for example "keywords.html"), from the same autoescaping environment."""
    return _environment().get_template(name)


def _font_css(fonts: tuple[FontFace, ...]) -> Markup:
    rules = []
    for font in fonts:
        family = font.family.replace('"', "")
        href = font.href.replace('"', "%22")
        rules.append(f'@font-face {{ font-family: "{family}"; font-weight: {font.weight}; font-style: {font.style}; '
                     f'src: local("{family}"), url("{href}"); }}')
    return Markup("\n".join(rules))


def _stylesheet() -> Markup:
    return Markup((TEMPLATE_DIR / STYLESHEET).read_text(encoding="utf-8"))


def _check(render_input, assets):
    if not isinstance(render_input, ri.ClientRenderInput):
        raise TypeError(f"render_input must be a ClientRenderInput, got {type(render_input).__name__}")
    if not isinstance(assets, TemplateAssets):
        raise TypeError(f"assets must be TemplateAssets, got {type(assets).__name__}")


def _document(render_input, assets, pages) -> str:
    template = _environment().get_template("document.html")
    return template.render(input=render_input, assets=assets, pages=pages, css=_stylesheet(),
                           font_css=_font_css(assets.fonts))


def cover_page_spec(render_input: ri.ClientRenderInput) -> dict:
    """The cover page as the document template expects it (page 1)."""
    if not isinstance(render_input, ri.ClientRenderInput):
        raise TypeError(f"render_input must be a ClientRenderInput, got {type(render_input).__name__}")
    return {
        "kind": "cover",
        "date_text": format_report_date(render_input.generation_date),
        "sender_lines": sender_lines(render_input.sender),
        "client_href": safe_href(render_input.client.website),
    }


def render_document(render_input: ri.ClientRenderInput, assets: TemplateAssets, pages) -> str:
    """A complete HTML document from page specs: `cover_page_spec(...)` or content pages
    `{"kind": ..., "number": int, "body": Markup}` (the body is escaped unless it is `Markup`)."""
    _check(render_input, assets)
    return _document(render_input, assets, list(pages))


def render_cover_document(render_input: ri.ClientRenderInput, assets: TemplateAssets) -> str:
    """A complete HTML document holding the cover page (page 1)."""
    _check(render_input, assets)
    return _document(render_input, assets, [cover_page_spec(render_input)])


def render_shell_page_document(render_input: ri.ClientRenderInput, assets: TemplateAssets, *, page_number=None,
                               body: "str | Markup" = "") -> str:
    """A document with one interior page: header, the given body, footer. `body` is escaped unless it is `Markup`."""
    _check(render_input, assets)
    if page_number is not None and (isinstance(page_number, bool) or not isinstance(page_number, int)
                                    or page_number < 1):
        raise TemplateInputError("page_number: must be a positive whole number or None")
    page = {"kind": "content", "number": page_number, "body": body}
    return _document(render_input, assets, [page])


def render_visual_notice(visual, label: str) -> str:
    """The HTML for `build_visual_notice(visual, label)`; an empty string when there is nothing to say."""
    notice = build_visual_notice(visual, label)
    macro = _environment().get_template("components.html").module.visual_notice
    return str(macro(notice)).strip()
