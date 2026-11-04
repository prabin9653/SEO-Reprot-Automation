"""The immutable input of the client PDF renderer (R1).

`ClientRenderInput` holds every value that can change what the client sees in the rendered PDF or how it is drawn:
the client-visible report content, the sender block, the report generation date, the historical statements that are
displayed, the numbers behind the Search Console, geography and GA4 visuals, and fingerprints of the rendering assets
(template version, logo, fonts). One object is meant to be handed to the renderer, hashed
(`render_input_hash.render_input_sha256`) and recorded, so that what is drawn, what is hashed and what is recorded
cannot drift apart.

Immutability. Every model is a frozen dataclass; every collection is a tuple of such models (or of text); numbers are
`int` or `Decimal` (never `float`); text is `str`; dates are `datetime.date`. Lists passed to a constructor are
copied into tuples, so the caller keeping a reference to its list cannot change the object afterwards. There are no
dictionaries and no mutable containers anywhere in the model, so a frozen top-level object is deeply immutable.

Validation. Constructors reject wrong types, empty required text, bad dates, floats, NaN/Infinity, negative counts,
malformed SHA-256 fingerprints, duplicate keys, and visuals whose status contradicts their data. Messages name the
field and the problem, never the value.

Generation date. `generation_date` is an explicit field supplied by the caller. Nothing in R1 reads a clock. A later
integration stage must capture the date once per report run, persist it, and pass the same date on every retry; if it
is recomputed, a retry hashes differently from the stored record.

What is deliberately not here: HTML, charts, PDF code, extraction of visual data from snapshots (R2), and any
database, file or network access. The only existing modules used are read-only: `from_client_content` reads
`report_run.ClientContent`, `clients.Sender` and `history_engine.ClientHistoryStatement` and copies them.

Planned construction from existing report data (not implemented as integration in R1)
  content            `bundle.client_content` (ClientContent)   -> `from_client_content(...)`
  sender             `clients.Sender` of the client (default or per-client override)
  generation_date    supplied once per run by the caller (never recomputed)
  statements         `history_insights.client_statements` (composed in 6c-5), passed to `from_client_content` and
                     merged into the single object. `ClientContent.historical_statements` is empty in the current
                     assembly; if it were not, passing statements as well is refused, as in `client_content_hash`
  visuals            R2 will build them from `bundle.snapshot.payload` (GSC daily/countries, GA4 channels/daily)
  fingerprints       SHA-256 of the logo and font files and the template version, measured by the renderer stage
  operational        run ID, revision, snapshot content hash, client key: kept for traceability, never hashed

Differences from `ClientContent` (which this model does not replace in R1)
  dropped as not displayed   client target country/city, keyword target URLs, section `has_strength` and
                             `omitted`, the insight objects (the paragraphs are the displayed text), recommendation
                             key/trigger/evidence/kind/priority, historical-statement dimension/evidence/priority/
                             tone/rules version. If a later template displays one of these, it must be added to the
                             model and classified (the field-classification test forces that decision)
  new                        sender, generation date, visuals (R2.1: three availability states, optional totals,
                             truncation notes), fingerprints
  refused                    non-empty `ranking_markers` (their element type is undefined)
"""

import datetime as dt
import re
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation

MAX_TEXT_CHARS = 100_000
STATUSES = ("ok", "unavailable")
RANKING_KINDS = ("position", "above_10", "missing")
PAGE_SIZES = ("A4",)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_TEMPLATE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_KEY = re.compile(r"[a-z][a-z0-9_.]{0,63}")
_FONT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")


class RenderInputError(ValueError):
    """The render input is not valid. The message names a field, never a value."""


# --- validation helpers ---------------------------------------------------------------------------------------------------

def _fail(where: str, problem: str):
    raise RenderInputError(f"{where}: {problem}")


def _text(value, where, *, optional=False, empty=False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        _fail(where, f"must be text, got {type(value).__name__}")
    if not empty and not value.strip():
        _fail(where, "must not be empty")
    if len(value) > MAX_TEXT_CHARS or "\x00" in value:
        _fail(where, "is too long or contains a NUL character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(where, "cannot be encoded as UTF-8")
    return value


def _pattern(value, where, pattern, what):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _fail(where, f"must be {what}")
    return value


def _int(value, where, *, minimum=0, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(where, f"must be a whole number, got {type(value).__name__}")
    if value < minimum:
        _fail(where, f"must be at least {minimum}")
    return value


def _decimal(value, where, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (int, Decimal)):
        _fail(where, f"must be a Decimal or whole number (floats are not accepted), got {type(value).__name__}")
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail(where, "is not a number")
    if not number.is_finite():
        _fail(where, "must be finite")
    if number < 0:
        _fail(where, "must not be negative")
    return number


def _date(value, where, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
        _fail(where, f"must be a date (not a datetime), got {type(value).__name__}")
    return value


def _choice(value, where, allowed):
    if not isinstance(value, str) or value not in allowed:
        _fail(where, f"must be one of {list(allowed)}")
    return value


def _tuple(value, where, kind):
    """A tuple copy of a list or tuple whose items are all `kind` (text when kind is str)."""
    if isinstance(value, (str, bytes, dict, set, frozenset)) or not isinstance(value, (list, tuple)):
        _fail(where, f"must be a list or tuple, got {type(value).__name__}")
    items = tuple(value)
    for index, item in enumerate(items):
        if kind is str:
            _text(item, f"{where}[{index}]")
        elif not isinstance(item, kind):
            _fail(f"{where}[{index}]", f"must be a {kind.__name__}, got {type(item).__name__}")
    return items


def _set(obj, name, value):
    object.__setattr__(obj, name, value)  # the dataclasses are frozen; this is the constructor's own normalisation


# --- client-visible content -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RenderClient:
    name: str
    website: str

    def __post_init__(self):
        _text(self.name, "client.name")
        _text(self.website, "client.website")


@dataclass(frozen=True)
class RenderPeriod:
    start: dt.date
    end: dt.date

    def __post_init__(self):
        _date(self.start, "period.start")
        _date(self.end, "period.end")
        if self.end < self.start:
            _fail("period", "end is before start")


@dataclass(frozen=True)
class RenderSender:
    """The sender block of the cover and the contact page. `website_url` may be "" (as in the configuration)."""

    name: str
    title: str | None = None
    address_lines: tuple[str, ...] = ()
    phone: str | None = None
    email: str | None = None
    website_label: str | None = None
    website_url: str | None = None

    def __post_init__(self):
        _text(self.name, "sender.name")
        for name in ("title", "phone", "email", "website_label"):
            _text(getattr(self, name), f"sender.{name}", optional=True)
        _text(self.website_url, "sender.website_url", optional=True, empty=True)
        _set(self, "address_lines", _tuple(self.address_lines, "sender.address_lines", str))


@dataclass(frozen=True)
class RenderRanking:
    """One ranking cell. `position` is set exactly when `kind` is "position"; `text` is the sheet's own wording
    (for example "Above 10") or None."""

    kind: str
    position: int | None = None
    text: str | None = None

    def __post_init__(self):
        _choice(self.kind, "ranking.kind", RANKING_KINDS)
        _int(self.position, "ranking.position", minimum=1, optional=True)
        _text(self.text, "ranking.text", optional=True)
        if (self.kind == "position") != (self.position is not None):
            _fail("ranking", "a position is required for, and only for, kind 'position'")


@dataclass(frozen=True)
class RenderKeywordRow:
    keyword: str
    location: str  # may be "" when the sheet has no target location
    previous: RenderRanking
    current: RenderRanking
    movement: int | None  # positions gained (positive) or lost (negative); None unless both rankings are numeric

    def __post_init__(self):
        _text(self.keyword, "keyword.keyword")
        _text(self.location, "keyword.location", empty=True)
        for name in ("previous", "current"):
            if not isinstance(getattr(self, name), RenderRanking):
                _fail(f"keyword.{name}", "must be a RenderRanking")
        if self.movement is not None:
            _int(self.movement, "keyword.movement", minimum=-10_000_000)


@dataclass(frozen=True)
class RenderSection:
    key: str
    title: str
    status: str
    paragraphs: tuple[str, ...]

    def __post_init__(self):
        _pattern(self.key, "section.key", _KEY, "a lowercase identifier")
        _text(self.title, "section.title")
        _choice(self.status, "section.status", STATUSES)
        _set(self, "paragraphs", _tuple(self.paragraphs, "section.paragraphs", str))


@dataclass(frozen=True)
class RenderRecommendation:
    title: str
    action: str

    def __post_init__(self):
        _text(self.title, "recommendation.title")
        _text(self.action, "recommendation.action")


@dataclass(frozen=True)
class RenderStatement:
    """A historical statement exactly as displayed. The same object feeds the renderer, the hash and the record."""

    section: str
    key: str
    text: str
    group: str
    baseline_start: dt.date
    baseline_end: dt.date

    def __post_init__(self):
        _pattern(self.section, "statement.section", _KEY, "a lowercase identifier")
        _pattern(self.key, "statement.key", _KEY, "a lowercase identifier")
        _text(self.text, "statement.text")
        _text(self.group, "statement.group")
        _date(self.baseline_start, "statement.baseline_start")
        _date(self.baseline_end, "statement.baseline_end")
        if self.baseline_end < self.baseline_start:
            _fail("statement", "baseline end is before start")


# --- visual data ---------------------------------------------------------------------------------------------------------------------
#
# Three availability states, the same for every visual:
#   available    usable data, complete
#   incomplete   usable data, but the source says its coverage is incomplete (for example the period's last day has no
#                data yet); the renderer must say so and must not present the figures as final
#   unavailable  no usable data; the visual carries no data at all
# A total that is None is missing, never zero. A day that is absent from a daily series has no record; a day that is
# present with 0 was measured as 0. A dataset that was cut short carries an explicit `TruncationNote`.

VISUAL_STATUSES = ("available", "incomplete", "unavailable")
TRUNCATION_REASONS = ("source_limit", "snapshot_cap", "row_limit")
# source_limit   the data source returned only its row limit and said more exist (the number omitted is unknown)
# snapshot_cap   the snapshot stored only its own maximum number of rows (the number omitted is unknown)
# row_limit      the extraction kept only the first `omitted_rows` + n rows because the renderer asked for n; the
#                number left out is exact
_DATASET = re.compile(r"[a-z][a-z_]{0,31}")


def _visual_status(value, where):
    return _choice(value, where, VISUAL_STATUSES)


def _ascending(days, where):
    for earlier, later in zip(days, days[1:]):
        if later <= earlier:
            _fail(where, "days must be strictly increasing (each date at most once, in order)")


def _truncation(value, where):
    notes = _tuple(value, where, TruncationNote)
    keys = [(n.dataset, n.reason) for n in notes]
    if len(set(keys)) != len(keys):
        _fail(where, "a dataset can have each truncation reason only once")
    return tuple(sorted(notes, key=lambda n: (n.dataset, n.reason)))  # a set of notes has no meaningful order


@dataclass(frozen=True)
class TruncationNote:
    """A dataset that is not complete. `omitted_rows` is the exact number left out for "row_limit" and None
    otherwise (the source and the snapshot do not say how many rows were cut, and none is invented)."""

    dataset: str
    reason: str
    omitted_rows: int | None = None

    def __post_init__(self):
        _pattern(self.dataset, "truncation.dataset", _DATASET, "a lowercase dataset name")
        _choice(self.reason, "truncation.reason", TRUNCATION_REASONS)
        _int(self.omitted_rows, "truncation.omitted_rows", minimum=1, optional=True)
        if (self.reason == "row_limit") != (self.omitted_rows is not None):
            _fail("truncation", "the omitted row count is required for, and only for, reason 'row_limit'")


@dataclass(frozen=True)
class DailyPoint:
    day: dt.date
    clicks: int
    impressions: int

    def __post_init__(self):
        _date(self.day, "daily.day")
        _int(self.clicks, "daily.clicks")
        _int(self.impressions, "daily.impressions")


@dataclass(frozen=True)
class SearchConsoleVisuals:
    """Totals and the daily series behind the Search Console page. `ctr` is a fraction (0.03 = 3%). Each total may be
    missing (None) while the others are valid; an available or incomplete visual needs at least one total or one
    daily point."""

    status: str
    clicks: int | None = None
    impressions: int | None = None
    ctr: Decimal | None = None
    average_position: Decimal | None = None
    daily: tuple[DailyPoint, ...] = ()

    def __post_init__(self):
        _visual_status(self.status, "search_console.status")
        _int(self.clicks, "search_console.clicks", optional=True)
        _int(self.impressions, "search_console.impressions", optional=True)
        _set(self, "ctr", _decimal(self.ctr, "search_console.ctr", optional=True))
        _set(self, "average_position", _decimal(self.average_position, "search_console.average_position",
                                                optional=True))
        _set(self, "daily", _tuple(self.daily, "search_console.daily", DailyPoint))
        _ascending([p.day for p in self.daily], "search_console.daily")
        usable = any(v is not None for v in (self.clicks, self.impressions, self.ctr, self.average_position)) \
            or bool(self.daily)
        if self.status == "unavailable" and usable:
            _fail("search_console", "an unavailable section must not carry data")
        if self.status != "unavailable" and not usable:
            _fail("search_console", "an available or incomplete section needs at least one usable value")

    @classmethod
    def unavailable(cls):
        return cls("unavailable")


@dataclass(frozen=True)
class CountryRow:
    name: str
    clicks: int
    impressions: int

    def __post_init__(self):
        _text(self.name, "country.name")
        _int(self.clicks, "country.clicks")
        _int(self.impressions, "country.impressions")


@dataclass(frozen=True)
class GeographyVisuals:
    """Measured country rows, in the source's order. `truncation` says when the rows are not the whole dataset."""

    status: str
    countries: tuple[CountryRow, ...] = ()
    truncation: tuple[TruncationNote, ...] = ()

    def __post_init__(self):
        _visual_status(self.status, "geography.status")
        _set(self, "countries", _tuple(self.countries, "geography.countries", CountryRow))
        _set(self, "truncation", _truncation(self.truncation, "geography.truncation"))
        if self.status == "unavailable" and (self.countries or self.truncation):
            _fail("geography", "an unavailable section must not carry data")
        if self.status != "unavailable" and not self.countries:
            _fail("geography", "an available or incomplete section needs at least one country row")

    @classmethod
    def unavailable(cls):
        return cls("unavailable")


@dataclass(frozen=True)
class ChannelRow:
    channel: str
    sessions: int
    users: int
    engaged_sessions: int

    def __post_init__(self):
        _text(self.channel, "channel.channel")
        for name in ("sessions", "users", "engaged_sessions"):
            _int(getattr(self, name), f"channel.{name}")


@dataclass(frozen=True)
class Ga4DailyPoint:
    day: dt.date
    sessions: int

    def __post_init__(self):
        _date(self.day, "ga4_daily.day")
        _int(self.sessions, "ga4_daily.sessions")


@dataclass(frozen=True)
class Ga4Visuals:
    """Totals, channel table and daily sessions behind the GA4 page. `engagement_rate` is a fraction. Each total may
    be missing (None) while the others are valid; an available or incomplete visual needs at least one total, channel
    row or daily point. `truncation` says which datasets (channels, daily) are not the whole dataset."""

    status: str
    sessions: int | None = None
    users: int | None = None
    engagement_rate: Decimal | None = None
    channels: tuple[ChannelRow, ...] = ()
    daily: tuple[Ga4DailyPoint, ...] = ()
    truncation: tuple[TruncationNote, ...] = ()

    def __post_init__(self):
        _visual_status(self.status, "ga4.status")
        _int(self.sessions, "ga4.sessions", optional=True)
        _int(self.users, "ga4.users", optional=True)
        _set(self, "engagement_rate", _decimal(self.engagement_rate, "ga4.engagement_rate", optional=True))
        _set(self, "channels", _tuple(self.channels, "ga4.channels", ChannelRow))
        _set(self, "daily", _tuple(self.daily, "ga4.daily", Ga4DailyPoint))
        _set(self, "truncation", _truncation(self.truncation, "ga4.truncation"))
        _ascending([p.day for p in self.daily], "ga4.daily")
        usable = any(v is not None for v in (self.sessions, self.users, self.engagement_rate)) \
            or bool(self.channels) or bool(self.daily)
        if self.status == "unavailable" and (usable or self.truncation):
            _fail("ga4", "an unavailable section must not carry data")
        if self.status != "unavailable" and not usable:
            _fail("ga4", "an available or incomplete section needs at least one usable value")

    @classmethod
    def unavailable(cls):
        return cls("unavailable")


# --- rendering fingerprints ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FontFingerprint:
    name: str
    sha256: str

    def __post_init__(self):
        _pattern(self.name, "font.name", _FONT_NAME, "a short font file label")
        _pattern(self.sha256, "font.sha256", _SHA256, "64 lowercase hexadecimal characters")


@dataclass(frozen=True)
class RenderFingerprints:
    """Identifiers of everything outside the content that changes the drawn PDF. Measured by the renderer stage;
    this module only validates and carries them (no file is read here)."""

    template_version: str
    logo_sha256: str
    fonts: tuple[FontFingerprint, ...]
    page_size: str = "A4"

    def __post_init__(self):
        _pattern(self.template_version, "fingerprints.template_version", _TEMPLATE_VERSION,
                 "a short version label such as 'r3.1'")
        _pattern(self.logo_sha256, "fingerprints.logo_sha256", _SHA256, "64 lowercase hexadecimal characters")
        _choice(self.page_size, "fingerprints.page_size", PAGE_SIZES)
        fonts = _tuple(self.fonts, "fingerprints.fonts", FontFingerprint)
        if not fonts:
            _fail("fingerprints.fonts", "at least one font is required")
        names = [f.name for f in fonts]
        if len(set(names)) != len(names):
            _fail("fingerprints.fonts", "font labels must be unique")
        _set(self, "fonts", tuple(sorted(fonts, key=lambda f: f.name)))  # a set of fonts has no meaningful order


# --- operational metadata (never hashed) ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RenderOperationalMetadata:
    """Traceability only: not client-visible, not drawn, not hashed."""

    client_key: str | None = None
    run_id: str | None = None
    revision: int | None = None
    snapshot_content_hash: str | None = None

    def __post_init__(self):
        _text(self.client_key, "operational.client_key", optional=True)
        _text(self.run_id, "operational.run_id", optional=True)
        _int(self.revision, "operational.revision", minimum=1, optional=True)
        if self.snapshot_content_hash is not None:
            _pattern(self.snapshot_content_hash, "operational.snapshot_content_hash", _SHA256,
                     "64 lowercase hexadecimal characters")


# --- the render input -----------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ClientRenderInput:
    client: RenderClient
    period: RenderPeriod
    comparison_period: RenderPeriod
    sender: RenderSender
    generation_date: dt.date
    keywords: tuple[RenderKeywordRow, ...]
    ranking_footnote: str | None
    sections: tuple[RenderSection, ...]
    recommendations: tuple[RenderRecommendation, ...]
    historical_statements: tuple[RenderStatement, ...]
    search_console: SearchConsoleVisuals
    geography: GeographyVisuals
    ga4: Ga4Visuals
    fingerprints: RenderFingerprints
    operational: RenderOperationalMetadata = field(default_factory=RenderOperationalMetadata)

    def __post_init__(self):
        for name, kind in (("client", RenderClient), ("period", RenderPeriod), ("comparison_period", RenderPeriod),
                           ("sender", RenderSender), ("search_console", SearchConsoleVisuals),
                           ("geography", GeographyVisuals), ("ga4", Ga4Visuals),
                           ("fingerprints", RenderFingerprints), ("operational", RenderOperationalMetadata)):
            if not isinstance(getattr(self, name), kind):
                _fail(name, f"must be a {kind.__name__}")
        _date(self.generation_date, "generation_date")
        _set(self, "keywords", _tuple(self.keywords, "keywords", RenderKeywordRow))
        _text(self.ranking_footnote, "ranking_footnote", optional=True, empty=True)
        sections = _tuple(self.sections, "sections", RenderSection)
        if len({s.key for s in sections}) != len(sections):
            _fail("sections", "section keys must be unique")
        _set(self, "sections", sections)
        _set(self, "recommendations", _tuple(self.recommendations, "recommendations", RenderRecommendation))
        statements = _tuple(self.historical_statements, "historical_statements", RenderStatement)
        if len({(s.section, s.key) for s in statements}) != len(statements):
            _fail("historical_statements", "a statement key may appear only once per section")
        _set(self, "historical_statements", statements)


# --- construction from existing report data (read-only copy; not an integration step) ---------------------------------------------------------

def from_client_content(content, *, sender, generation_date, fingerprints, historical_statements=None,
                        search_console=None, geography=None, ga4=None, operational=None) -> ClientRenderInput:
    """Copy a `report_run.ClientContent` (and the other inputs) into one immutable `ClientRenderInput`.

    `sender` is a `clients.Sender`; `historical_statements` is an iterable of `history_engine.ClientHistoryStatement`
    (passing them while the content already carries some is refused). Missing visuals default to "unavailable".
    Non-empty `ranking_markers` are refused. Nothing in the arguments is changed.
    """
    from seo_report.clients import Sender
    from seo_report.insights.history_engine import ClientHistoryStatement
    from seo_report.report_run import ClientContent

    if not isinstance(content, ClientContent):
        _fail("content", f"must be a ClientContent, got {type(content).__name__}")
    if not isinstance(sender, Sender):
        _fail("sender", f"must be a clients.Sender, got {type(sender).__name__}")
    if content.ranking_markers:
        _fail("ranking_markers", "markers have no defined content yet and cannot be rendered")
    statements = tuple(content.historical_statements)
    if historical_statements is not None:
        if statements:
            _fail("historical_statements", "the content already carries statements; pass them in one place only")
        statements = tuple(historical_statements)
    for index, statement in enumerate(statements):
        if not isinstance(statement, ClientHistoryStatement):
            _fail(f"historical_statements[{index}]", "must be a ClientHistoryStatement")

    def ranking(value):
        return RenderRanking(value.kind.value, value.position, value.text)

    def period(value):
        return RenderPeriod(value.start, value.end)

    def iso(text, where):
        try:
            return dt.date.fromisoformat(text)
        except (TypeError, ValueError):
            _fail(where, "must be an ISO date")

    return ClientRenderInput(
        client=RenderClient(content.client.name, content.client.website),
        period=period(content.periods.current), comparison_period=period(content.periods.previous),
        sender=RenderSender(sender.name, sender.title, tuple(sender.address_lines), sender.phone, sender.email,
                            sender.website_label, sender.website_url),
        generation_date=generation_date,
        keywords=tuple(RenderKeywordRow(r.keyword, r.target_location or "", ranking(r.previous), ranking(r.current),
                                        r.movement) for r in content.ranking_rows),
        ranking_footnote=content.ranking_footnote,
        sections=tuple(RenderSection(s.key, s.title, s.status, tuple(s.paragraphs)) for s in content.sections),
        recommendations=tuple(RenderRecommendation(r.title, r.action) for r in content.recommendations),
        historical_statements=tuple(
            RenderStatement(s.section, s.key, s.text, s.group, iso(s.baseline_period_start, "statement.baseline_start"),
                            iso(s.baseline_period_end, "statement.baseline_end")) for s in statements),
        search_console=search_console if search_console is not None else SearchConsoleVisuals.unavailable(),
        geography=geography if geography is not None else GeographyVisuals.unavailable(),
        ga4=ga4 if ga4 is not None else Ga4Visuals.unavailable(),
        fingerprints=fingerprints,
        operational=operational if operational is not None else RenderOperationalMetadata())


def model_classes() -> tuple[type, ...]:
    """Every dataclass defined in this module (used by the hash module to verify its field classification)."""
    import sys
    module = sys.modules[__name__]
    return tuple(obj for obj in vars(module).values()
                 if isinstance(obj, type) and getattr(obj, "__dataclass_fields__", None) is not None
                 and obj.__module__ == __name__)


def field_names(cls) -> tuple[str, ...]:
    return tuple(f.name for f in fields(cls))
