"""Client configuration and keyword rows parsed from the workbook's raw values.

Everything here works on plain lists of cell values, so it can be tested offline.
"""

import datetime as dt
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from .rankings import Ranking, parse_ranking

CLIENT_TAB = "Client Configuration"
KEYWORD_TAB = "Keyword Ranking Sheet"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "jasper.yaml"
ACTIVE_STATUS = "active"
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+", re.ASCII)
SHEETS_EPOCH = dt.date(1899, 12, 30)

# Canonical field -> accepted header spellings (normalized). The sheet's existing
# "Project Maanger" header is accepted alongside the correct spelling.
CLIENT_COLUMNS = {
    "client_name": ("client name",),
    "website": ("website",),
    "project_manager": ("project manager", "project maanger"),
    "manager_email": ("manager email",),
    "manager_phone": ("manager phone",),
    "gsc_property": ("gsc property",),
    "ga4_property_id": ("ga4 property id",),
    "target_country": ("target country",),
    "target_city": ("target city",),
    "report_start_date": ("report start date",),
    "report_end_date": ("report end date",),
    "status": ("status",),
}
CLIENT_REQUIRED = ("client_name", "website", "gsc_property", "ga4_property_id", "status")

KEYWORD_COLUMNS = {
    "client_name": ("client name",),
    "keyword": ("keyword",),
    "target_location": ("target location",),
    "previous_ranking": ("previous ranking",),
    "current_ranking": ("current ranking",),
    "target_url": ("target url",),
    "notes": ("notes",),
}
KEYWORD_REQUIRED = ("client_name", "keyword", "previous_ranking", "current_ranking")


class SheetDataError(ValueError):
    """The workbook data is missing, ambiguous or invalid."""


@dataclass(frozen=True)
class ContactProfile:
    """The Jasper business details printed under a Project Manager, for one Target Country (`regional_profiles` in the sender configuration)."""

    country: str
    title: str
    address_lines: tuple[str, ...]
    website_label: str
    website_url: str


@dataclass(frozen=True)
class Sender:
    """`name`, `phone` and `email` of a client's sender come from its own row; the rest from the regional profile.
    On the default sender, `profiles` lists every regional profile (and nothing else of it is ever printed)."""

    name: str
    title: str | None = None
    address_lines: tuple[str, ...] = ()
    phone: str | None = None
    email: str | None = None
    website_label: str | None = None
    website_url: str | None = None
    profiles: tuple[ContactProfile, ...] = ()


@dataclass(frozen=True)
class Keyword:
    keyword: str
    target_location: str
    previous: Ranking
    current: Ranking
    target_url: str
    notes: str
    sheet_row: int


@dataclass
class Client:
    name: str
    website: str
    gsc_property: str
    ga4_property_id: str
    status: str
    sender: Sender
    target_country: str
    target_city: str
    report_start: dt.date | None
    report_end: dt.date | None
    sheet_row: int
    keywords: list[Keyword] = field(default_factory=list)

    @property
    def status_key(self) -> str:
        return normalize(self.status)


@dataclass(frozen=True)
class Selection:
    selected: list[Client]
    skipped: list[tuple[Client, str]]


def normalize(text) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().casefold()


def _is_blank(cell) -> bool:
    return cell is None or (isinstance(cell, str) and not cell.strip())


def _text(cell) -> str:
    return "" if cell is None else str(cell).strip()


@dataclass(frozen=True)
class _Row:
    tab: str
    number: int
    values: dict
    headers: dict

    def text(self, name: str) -> str:
        return _text(self.values.get(name))

    def location(self, name: str) -> str:
        return f"'{self.tab}' row {self.number}, column '{self.headers.get(name, name)}'"


def _read_table(rows, columns, required, tab) -> list[_Row]:
    if not rows or all(_is_blank(c) for c in rows[0]):
        raise SheetDataError(f"'{tab}': header row is missing")

    index = {}
    for col, cell in enumerate(rows[0]):
        key = normalize(cell) if not _is_blank(cell) else ""
        for name, aliases in columns.items():
            if key in aliases:
                if name in index:
                    raise SheetDataError(f"'{tab}': column '{name}' appears more than once in the header row")
                index[name] = (col, _text(cell))

    missing = [name for name in required if name not in index]
    if missing:
        found = [_text(c) for c in rows[0] if not _is_blank(c)]
        raise SheetDataError(f"'{tab}': missing required columns {missing}. Headers found: {found}")

    table = []
    for number, raw in enumerate(rows[1:], start=2):
        if all(_is_blank(c) for c in raw):
            continue
        values = {name: (raw[col] if col < len(raw) else None) for name, (col, _) in index.items()}
        table.append(_Row(tab, number, values, {name: header for name, (_, header) in index.items()}))
    return table


def _parse_date(value, location: str) -> dt.date | None:
    if _is_blank(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not (math.isfinite(value) and value.is_integer()):
            raise SheetDataError(f"{location}: invalid date {value!r}")
        try:
            return SHEETS_EPOCH + dt.timedelta(days=int(value))
        except OverflowError:
            raise SheetDataError(f"{location}: invalid date {value!r}") from None
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise SheetDataError(f"{location}: invalid date {value!r} (expected a date cell or YYYY-MM-DD)")


def _parse_property_id(value, location: str) -> str:
    if _is_blank(value):
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not (math.isfinite(value) and value.is_integer()):
            raise SheetDataError(f"{location}: invalid GA4 property ID {value!r}")
        return str(int(value))
    text = _text(value)
    if not re.fullmatch(r"\d+", text, re.ASCII):
        raise SheetDataError(f"{location}: invalid GA4 property ID {value!r} (expected digits)")
    return text


def _optional(value) -> str | None:
    text = _text(value)
    return text or None


def load_default_sender(path: Path = DEFAULT_CONFIG_PATH) -> Sender:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    sender = data.get("sender")
    if not isinstance(sender, dict) or not _optional(sender.get("name")):
        raise SheetDataError(f"{path}: 'sender' section with a 'name' is required")
    return Sender(
        name=_text(sender["name"]),
        title=_optional(sender.get("title")),
        address_lines=tuple(_text(line) for line in sender.get("address_lines") or [] if _text(line)),
        phone=_optional(sender.get("phone")),
        email=_optional(sender.get("email")),
        website_label=_optional(sender.get("website_label")),
        website_url=_optional(sender.get("website_url")),
        profiles=_load_profiles(data.get("regional_profiles"), path),
    )


def _load_profiles(raw, path) -> tuple[ContactProfile, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise SheetDataError(f"{path}: 'regional_profiles' must map a country name to its contact details")
    profiles, seen = [], set()
    for country, item in raw.items():
        key = normalize(country)
        if not isinstance(item, dict) or key in seen:
            raise SheetDataError(f"{path}: regional profile {str(country)!r} is invalid or repeated")
        lines = tuple(_text(line) for line in item.get("address_lines") or [] if _text(line))
        title, label, url = _text(item.get("title")), _text(item.get("website_label")), _text(item.get("website_url"))
        if not (title and lines and label and url):
            raise SheetDataError(f"{path}: regional profile {str(country)!r} needs title, address_lines, website_label and website_url")
        seen.add(key)
        profiles.append(ContactProfile(_text(country), title, lines, label, url))
    return tuple(profiles)


def profile_for(default: Sender, country: str) -> ContactProfile | None:
    """The regional contact profile of a Target Country (exact name, ignoring case and spacing); None if there is none."""
    key = normalize(country)
    return next((p for p in default.profiles if normalize(p.country) == key), None)


def resolve_sender(default: Sender, country: str, manager_name: str, email: str, phone: str) -> Sender:
    """The report's contact block of one client (R8.7h.11, .12, .15).

    The name, e-mail and phone come only from the client's own row (Project Manager, Manager Email, Manager Phone); the name is
    mandatory and a blank e-mail or phone stays unset. The title, address and website come from the regional profile of the
    Target Country; a country without a profile is an error, never a default. Nothing else of the default sender is used.
    """
    name, email, phone = _text(manager_name), _text(email), _text(phone)
    if not name:
        raise SheetDataError("the Project Manager name is required")
    profile = profile_for(default, country)
    if profile is None:
        raise SheetDataError("no Jasper contact profile is configured for this Target Country")
    return Sender(name=name, title=profile.title, address_lines=profile.address_lines, phone=phone or None, email=email or None,
                  website_label=profile.website_label, website_url=profile.website_url)


def build_clients(client_rows, keyword_rows, default_sender: Sender) -> list[Client]:
    """Parse both tabs' raw values (header row first) into clients with keywords."""
    clients: dict[str, Client] = {}
    for row in _read_table(client_rows, CLIENT_COLUMNS, CLIENT_REQUIRED, CLIENT_TAB):
        name = row.text("client_name")
        if not name:
            raise SheetDataError(f"{row.location('client_name')}: client name is empty")
        key = normalize(name)
        if key in clients:
            raise SheetDataError(
                f"'{CLIENT_TAB}' row {row.number}: duplicate client {name!r} (first seen on row {clients[key].sheet_row})"
            )
        if normalize(row.text("status")) != ACTIVE_STATUS and not (row.text("project_manager") and profile_for(default_sender, row.text("target_country"))):
            sender = Sender(name=row.text("project_manager"))  # a client that is not reported needs no contact block and cannot be rendered
        elif not row.text("project_manager"):
            raise SheetDataError(f"{row.location('project_manager')}: the Project Manager name is required for the report contact")
        elif profile_for(default_sender, row.text("target_country")) is None:
            raise SheetDataError(f"{row.location('target_country')}: no Jasper contact profile is configured for this Target Country "
                                 f"(configured: {', '.join(sorted(p.country for p in default_sender.profiles)) or 'none'})")
        else:
            sender = resolve_sender(default_sender, row.text("target_country"), row.text("project_manager"),
                                    row.text("manager_email"), row.text("manager_phone"))
        if row.text("manager_email") and not EMAIL_PATTERN.fullmatch(row.text("manager_email")):
            raise SheetDataError(f"{row.location('manager_email')}: not a valid e-mail address")  # the value is not echoed
        clients[key] = Client(
            name=name,
            website=row.text("website"),
            gsc_property=row.text("gsc_property"),
            ga4_property_id=_parse_property_id(
                row.values.get("ga4_property_id"), row.location("ga4_property_id")
            ),
            status=row.text("status"),
            sender=sender,
            target_country=row.text("target_country"),
            target_city=row.text("target_city"),
            report_start=_parse_date(row.values.get("report_start_date"), row.location("report_start_date")),
            report_end=_parse_date(row.values.get("report_end_date"), row.location("report_end_date")),
            sheet_row=row.number,
        )

    for row in _read_table(keyword_rows, KEYWORD_COLUMNS, KEYWORD_REQUIRED, KEYWORD_TAB):
        name = row.text("client_name")
        if not name:
            raise SheetDataError(f"{row.location('client_name')}: client name is empty")
        client = clients.get(normalize(name))
        if client is None:
            raise SheetDataError(f"{row.location('client_name')}: client {name!r} is not in '{CLIENT_TAB}'")
        keyword = row.text("keyword")
        if not keyword:
            raise SheetDataError(f"{row.location('keyword')}: keyword is empty")
        client.keywords.append(
            Keyword(
                keyword=keyword,
                target_location=row.text("target_location"),
                previous=parse_ranking(row.values.get("previous_ranking"), row.location("previous_ranking")),
                current=parse_ranking(row.values.get("current_ranking"), row.location("current_ranking")),
                target_url=row.text("target_url"),
                notes=row.text("notes"),
                sheet_row=row.number,
            )
        )
    return list(clients.values())


def select_clients(clients: list[Client], include_statuses=()) -> Selection:
    """Pick clients to process: Active, plus any status named explicitly.

    Other statuses (including blank or unknown ones) are skipped. Selected clients
    must have a website, GSC property, GA4 property ID and at least one keyword.
    """
    allowed = {ACTIVE_STATUS} | {normalize(s) for s in include_statuses}
    selected, skipped = [], []
    for client in clients:
        if client.status_key not in allowed:
            skipped.append((client, f"status {client.status!r} is not enabled"))
            continue
        problems = [
            label
            for label, value in (
                ("Website", client.website),
                ("GSC Property", client.gsc_property),
                ("GA4 Property ID", client.ga4_property_id),
            )
            if not value
        ]
        if not client.keywords:
            problems.append("at least one keyword")
        if problems:
            raise SheetDataError(
                f"Client {client.name!r} ('{CLIENT_TAB}' row {client.sheet_row}) is missing: {', '.join(problems)}"
            )
        selected.append(client)
    return Selection(selected, skipped)
