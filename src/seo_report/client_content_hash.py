"""A deterministic SHA-256 of the client-visible content of a report (Stage 6c-8b).

Finalization uses this hash to prove that the content it finalizes is the content that was assembled and
reviewed. It covers what the client report is built from, and nothing a reviewer or the system keeps for itself.

What is hashed
  Only a `report_run.ClientContent` is accepted, never a `ReportBundle`, `Snapshot`, reviewer report or record. The
  payload is built by explicit field selection from that object (INCLUDED_FIELDS below); nothing is serialized
  generically, so a field that is added later cannot slip in unnoticed (a test compares both field tables with the
  real dataclasses and fails when they disagree).

  client               name, website, target_country, target_city
  periods              current and previous period, start and end dates
  sections[]           key, title, status, has_strength, paragraphs[], insights[] (key, text, tone, priority,
                       evidence)
  recommendations[]    key, title, action, trigger, evidence, kind, priority
  ranking_rows[]       keyword, target_location, target_url, previous and current ranking, movement
  historical_statements[]
                       section, key, text, tone, priority, evidence, baseline_period_start, baseline_period_end,
                       group (the composed client statements of Stage 6c-5)
  ranking_markers[]    none are defined yet; a non-empty value is refused
  ranking_footnote     text or null

Deliberately not hashed (EXCLUDED_FIELDS): internal bookkeeping on client objects that no reader sees:
  section.omitted and insight.tags (which insights the paragraph limits left out; internal labels),
  recommendation.supporting (internal insight keys), statement.dimension and statement.rules_version (internal
  identifiers). Everything that exists only outside ClientContent is out of reach by construction: reviewer notes,
  validation failures, diagnostics, snapshot and run identifiers, revisions, content hashes, the historical
  comparison and its baselines, recommendation history annotations, source payloads, quality flags, timestamps and
  paths.

How it is serialized
  A dictionary is built from the selected fields, then written as JSON with sorted object keys, separators `,` and
  `:` (no spaces), `ensure_ascii=False`, and encoded as UTF-8. The hash is the lowercase hexadecimal SHA-256 of
  those bytes (64 characters). The payload carries `"format": 1`, so changing this contract changes every hash.
  * Lists keep their order (sections, paragraphs, recommendations, ranking rows and statements are display order).
    Sets are written sorted.
  * Dates are ISO text; enums are their values; rankings are {kind, position, text}.
  * `null`, "", 0, 0.0 and -0.0, and missing fields are all different. `1` and `1.0` differ (integers and floats are
    never merged), and text is never trimmed, normalized or re-cased.
  * NaN, infinity, unknown object types, non-text dictionary keys and text that cannot be encoded raise
    ClientContentHashError. The error names the field and type, never the value.

Limits of the contract
  * It covers the content as assembled, not the rendered PDF: two different PDFs can be drawn from the same hash.
  * A renderer may show fewer or more fields than the hash covers. The hash covers everything the content model
    would let it show, apart from the excluded bookkeeping above.
  * `ClientContent.historical_statements` is empty in the current assembly. The composed statements are passed with
    the `historical_statements` argument until the assembly carries them (passing both is refused).
  * Float text is Python's shortest round-trip form; it is stable on the same Python version and across the
    versions in use, but this module does not claim identical output for exotic platforms.

Pure: no clock, files, database or network, and the input is never changed.
"""

import datetime as dt
import enum
import hashlib
import json
import math

from seo_report import rankings as rk
from seo_report.insights.engine import SectionInsights
from seo_report.insights.history_engine import ClientHistoryStatement
from seo_report.insights.recommendations import Recommendation
from seo_report.insights.rules import Insight
from seo_report.periods import Period, ReportPeriods
from seo_report.report_run import ClientContent, ClientIdentity, RankingRow

CLIENT_CONTENT_FORMAT = 1

INCLUDED_FIELDS = {
    "ClientContent": ("client", "periods", "sections", "recommendations", "ranking_rows", "historical_statements",
                      "ranking_markers", "ranking_footnote"),
    "ClientIdentity": ("name", "website", "target_country", "target_city"),
    "ReportPeriods": ("current", "previous"),
    "Period": ("start", "end"),
    "SectionInsights": ("key", "title", "paragraphs", "insights", "status", "has_strength"),
    "Insight": ("key", "text", "tone", "priority", "evidence"),
    "Recommendation": ("key", "title", "action", "trigger", "evidence", "kind", "priority"),
    "RankingRow": ("keyword", "target_location", "target_url", "previous", "current", "movement"),
    "Ranking": ("kind", "position", "text"),
    "ClientHistoryStatement": ("section", "key", "text", "tone", "priority", "evidence", "baseline_period_start",
                               "baseline_period_end", "group"),
}
# Fields of those types that are internal bookkeeping and so not hashed. Insight.section repeats the section key.
EXCLUDED_FIELDS = {
    "SectionInsights": ("omitted",),
    "Insight": ("section", "tags"),
    "Recommendation": ("supporting",),
    "ClientHistoryStatement": ("dimension", "rules_version"),
}


class ClientContentHashError(ValueError):
    """The content cannot be hashed. The message names a field and a type, never a value."""


def _fail(where: str, problem: str):
    raise ClientContentHashError(f"{where}: {problem}")


# --- plain values -----------------------------------------------------------------------------------------------------------

def _plain(value, where: str):
    """A value as JSON-ready data: null, bool, int, finite float, text, list, or object with text keys."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(where, "must be a finite number")
        return value
    if isinstance(value, enum.Enum):
        return _plain(value.value, where)
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(item, f"{where}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, (set, frozenset)):
        items = [_plain(item, where) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(where, f"has a key of type {type(key).__name__}; keys must be text")
            result[key] = _plain(item, f"{where}.{key}")
        return result
    _fail(where, f"holds an unsupported value of type {type(value).__name__}")


def _expect(value, kind, where: str):
    if not isinstance(value, kind):
        _fail(where, f"must be a {kind.__name__}, got {type(value).__name__}")
    return value


def _fields(value, name: str, where: str) -> dict:
    """The allowlisted fields of a known dataclass instance, read one by one."""
    return {field: getattr(value, field) for field in INCLUDED_FIELDS[name]}


# --- payload ----------------------------------------------------------------------------------------------------------------------

def _ranking(value, where: str) -> dict:
    _expect(value, rk.Ranking, where)
    raw = _fields(value, "Ranking", where)
    return {"kind": _plain(raw["kind"], f"{where}.kind"), "position": _plain(raw["position"], f"{where}.position"),
            "text": _plain(raw["text"], f"{where}.text")}


def _period(value, where: str) -> dict:
    _expect(value, Period, where)
    raw = _fields(value, "Period", where)
    return {"start": _plain(raw["start"], f"{where}.start"), "end": _plain(raw["end"], f"{where}.end")}


def _insight(value, where: str) -> dict:
    _expect(value, Insight, where)
    raw = _fields(value, "Insight", where)
    return {name: _plain(raw[name], f"{where}.{name}") for name in INCLUDED_FIELDS["Insight"]}


def _section(value, where: str) -> dict:
    _expect(value, SectionInsights, where)
    raw = _fields(value, "SectionInsights", where)
    payload = {name: _plain(raw[name], f"{where}.{name}")
               for name in ("key", "title", "paragraphs", "status", "has_strength")}
    payload["insights"] = [_insight(item, f"{where}.insights[{i}]") for i, item in enumerate(raw["insights"])]
    return payload


def _recommendation(value, where: str) -> dict:
    _expect(value, Recommendation, where)
    raw = _fields(value, "Recommendation", where)
    return {name: _plain(raw[name], f"{where}.{name}") for name in INCLUDED_FIELDS["Recommendation"]}


def _row(value, where: str) -> dict:
    _expect(value, RankingRow, where)
    raw = _fields(value, "RankingRow", where)
    payload = {name: _plain(raw[name], f"{where}.{name}") for name in ("keyword", "target_location", "target_url",
                                                                       "movement")}
    payload["previous"] = _ranking(raw["previous"], f"{where}.previous")
    payload["current"] = _ranking(raw["current"], f"{where}.current")
    return payload


def _statement(value, where: str) -> dict:
    _expect(value, ClientHistoryStatement, where)
    raw = _fields(value, "ClientHistoryStatement", where)
    return {name: _plain(raw[name], f"{where}.{name}") for name in INCLUDED_FIELDS["ClientHistoryStatement"]}


def build_client_payload(content, *, historical_statements=None) -> dict:
    """The explicit, JSON-ready client-visible payload of a `ClientContent`.

    `historical_statements` (composed `ClientHistoryStatement` objects) is used when the content itself carries
    none; passing statements for content that already holds some is refused rather than guessed at.
    """
    _expect(content, ClientContent, "content")
    raw = _fields(content, "ClientContent", "content")
    statements = tuple(raw["historical_statements"])
    if historical_statements is not None:
        if statements:
            _fail("historical_statements", "the content already carries statements; pass them in one place only")
        statements = tuple(historical_statements)
    if raw["ranking_markers"]:
        _fail("ranking_markers", "markers have no defined content yet and cannot be hashed")
    identity = _expect(raw["client"], ClientIdentity, "client")
    periods = _expect(raw["periods"], ReportPeriods, "periods")
    footnote = raw["ranking_footnote"]
    if footnote is not None and not isinstance(footnote, str):
        _fail("ranking_footnote", f"must be text or null, got {type(footnote).__name__}")
    client = _fields(identity, "ClientIdentity", "client")
    period_fields = _fields(periods, "ReportPeriods", "periods")
    return {
        "format": CLIENT_CONTENT_FORMAT,
        "client": {name: _plain(client[name], f"client.{name}") for name in INCLUDED_FIELDS["ClientIdentity"]},
        "periods": {name: _period(period_fields[name], f"periods.{name}") for name in ("current", "previous")},
        "sections": [_section(item, f"sections[{i}]") for i, item in enumerate(raw["sections"])],
        "recommendations": [_recommendation(item, f"recommendations[{i}]")
                            for i, item in enumerate(raw["recommendations"])],
        "ranking_rows": [_row(item, f"ranking_rows[{i}]") for i, item in enumerate(raw["ranking_rows"])],
        "historical_statements": [_statement(item, f"historical_statements[{i}]")
                                  for i, item in enumerate(statements)],
        "ranking_markers": [],
        "ranking_footnote": footnote,
    }


def canonical_client_json(content, *, historical_statements=None) -> str:
    """The canonical text that is hashed: sorted keys, compact separators, UTF-8-safe text."""
    payload = build_client_payload(content, historical_statements=historical_statements)
    try:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ClientContentHashError("a text value cannot be encoded as UTF-8") from None
    except (TypeError, ValueError):
        raise ClientContentHashError("the payload cannot be serialized") from None
    return text


def client_content_sha256(content, *, historical_statements=None) -> str:
    """The full SHA-256 (64 lowercase hex characters) of the canonical client-visible payload."""
    text = canonical_client_json(content, historical_statements=historical_statements)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
