"""Canonical JSON and SHA-256 of a `ClientRenderInput` (R1).

The hash identifies exactly what the client sees and how it is drawn: it changes when any client-visible or
rendering-affecting value changes, and it does not change when only operational metadata changes.

Allowlist. `FIELD_CLASSES` classifies every field of every model class in `render_input` as `included` or
`excluded`. Serialization walks the dataclass fields and uses the classification, so:
  * an `included` field is written to the canonical payload;
  * an `excluded` field is skipped (only `ClientRenderInput.operational` and its contents: run ID, revision,
    snapshot content hash, client key; they are traceability data, not client-visible and not drawn);
  * a field that is not classified, a classification for a field that no longer exists, and a model class that is
    not classified at all stop the hash with RenderInputHashError. A new field therefore cannot silently join, or
    silently stay out of, the hash: `classification_problems()` reports it and every hash call raises.

Canonical form (format "jasper.render-input", version 2; version 1 had two visual statuses and mandatory totals)
  payload     {"format": "jasper.render-input", "version": 2, "input": {...}}
  JSON        `json.dumps(sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))`, UTF-8;
              the hash is the lowercase hexadecimal SHA-256 of those bytes
  objects     one JSON object per dataclass, keys sorted; no dictionary of the input is ever iterated, so insertion
              order cannot matter
  tuples      JSON arrays in their given order (order is meaningful: keyword rows, sections, paragraphs,
              recommendations, statements, daily points and countries keep it). The only normalisation is the font
              list, which `RenderFingerprints` sorts by label because a set of fonts has no order
  text        written exactly (no trimming, case-folding or Unicode normalisation), so "é" as one character and as
              "e" plus a combining accent hash differently, consistently
  null        JSON null; "" and null are different; absent optional values are always null
  int, bool   JSON numbers and booleans (bool is never accepted for an int field)
  Decimal     a JSON string in plain notation without exponent or trailing zeros ("3.5", "0", never "3.50" or
              "1E+1"), so 1, 1.0 and 1.00 are the same and a float can never leak in (floats are rejected)
  date        ISO text "YYYY-MM-DD" (datetimes are rejected)
  anything else (dict, set, float, bytes, enum, object) raises RenderInputHashError; nothing is turned into text

Never read here: the system clock, random values, the environment, files, paths or the network.

Compatibility and later integration (not done in R1)
  `report_finalize.py` currently calls `client_content_hash.client_content_sha256(bundle.client_content,
  historical_statements=...)` and stores the result as `record.finalization.client_content_sha256`. To make this hash
  the finalization identity, a later, separately approved stage would:
    1. build one `ClientRenderInput` per run (`render_input.from_client_content(...)`), capture `generation_date`
       once and persist it so retries reuse it, and pass the same object to the renderer and to finalization;
    2. in `report_finalize.py`, replace the `client_content_sha256(...)` call with `render_input_sha256(...)`, take
       the input object (instead of `history_insights.client_statements` for hashing) as an argument, and keep
       passing `history_insights` to the record builder; the record field keeps its name and 64-hex shape;
    3. in `client_content_hash.py`, either retire the function or keep it unchanged for reference (it has its own
       format identifier, "format": 1, and its own tests); the two hashes must never be compared;
    4. in `record_schema.py`, no structural change is required (`client_content_sha256` is a SHA-256); a later
       schema revision could add a hash-format field so records say which canonical form they used.
  Until that stage, no existing module reads or depends on this one.
"""

import dataclasses
import datetime as dt
import hashlib
import json
import types
import typing
from decimal import Decimal

from seo_report import render_input as ri

FORMAT_NAME = "jasper.render-input"
# Version 2 (R2.1): visual statuses are available/incomplete/unavailable, visual totals may be missing, and the
# geography and GA4 visuals carry truncation notes. No record was ever finalized with version 1.
FORMAT_VERSION = 2

INCLUDED = "included"
EXCLUDED = "excluded"

FIELD_CLASSES = {
    "RenderClient": {"name": INCLUDED, "website": INCLUDED},
    "RenderPeriod": {"start": INCLUDED, "end": INCLUDED},
    "RenderSender": {"name": INCLUDED, "title": INCLUDED, "address_lines": INCLUDED, "phone": INCLUDED,
                     "email": INCLUDED, "website_label": INCLUDED, "website_url": INCLUDED},
    "RenderRanking": {"kind": INCLUDED, "position": INCLUDED, "text": INCLUDED},
    "RenderKeywordRow": {"keyword": INCLUDED, "location": INCLUDED, "previous": INCLUDED, "current": INCLUDED,
                         "movement": INCLUDED},
    "RenderSection": {"key": INCLUDED, "title": INCLUDED, "status": INCLUDED, "paragraphs": INCLUDED},
    "RenderRecommendation": {"title": INCLUDED, "action": INCLUDED},
    "RenderStatement": {"section": INCLUDED, "key": INCLUDED, "text": INCLUDED, "group": INCLUDED,
                        "baseline_start": INCLUDED, "baseline_end": INCLUDED},
    "DailyPoint": {"day": INCLUDED, "clicks": INCLUDED, "impressions": INCLUDED},
    "SearchConsoleVisuals": {"status": INCLUDED, "clicks": INCLUDED, "impressions": INCLUDED, "ctr": INCLUDED,
                             "average_position": INCLUDED, "daily": INCLUDED},
    "CountryRow": {"name": INCLUDED, "clicks": INCLUDED, "impressions": INCLUDED},
    "TruncationNote": {"dataset": INCLUDED, "reason": INCLUDED, "omitted_rows": INCLUDED},
    "GeographyVisuals": {"status": INCLUDED, "countries": INCLUDED, "truncation": INCLUDED},
    "ChannelRow": {"channel": INCLUDED, "sessions": INCLUDED, "users": INCLUDED, "engaged_sessions": INCLUDED},
    "Ga4DailyPoint": {"day": INCLUDED, "sessions": INCLUDED},
    "Ga4Visuals": {"status": INCLUDED, "sessions": INCLUDED, "users": INCLUDED, "engagement_rate": INCLUDED,
                   "channels": INCLUDED, "daily": INCLUDED, "truncation": INCLUDED},
    "FontFingerprint": {"name": INCLUDED, "sha256": INCLUDED},
    "RenderFingerprints": {"template_version": INCLUDED, "logo_sha256": INCLUDED, "fonts": INCLUDED,
                           "page_size": INCLUDED},
    "RenderOperationalMetadata": {"client_key": EXCLUDED, "run_id": EXCLUDED, "revision": EXCLUDED,
                                  "snapshot_content_hash": EXCLUDED},
    "ClientRenderInput": {"client": INCLUDED, "period": INCLUDED, "comparison_period": INCLUDED,
                          "sender": INCLUDED, "generation_date": INCLUDED, "keywords": INCLUDED,
                          "ranking_footnote": INCLUDED, "sections": INCLUDED, "recommendations": INCLUDED,
                          "historical_statements": INCLUDED, "search_console": INCLUDED, "geography": INCLUDED,
                          "ga4": INCLUDED, "fingerprints": INCLUDED, "operational": EXCLUDED},
}


class RenderInputHashError(ValueError):
    """The render input cannot be hashed. The message names a class or field and a type, never a value."""


def classification_problems(classes=None, classification=None) -> list[str]:
    """Every mismatch between the model classes and `FIELD_CLASSES`, as fixed phrases (empty when consistent).

    `classes` defaults to the dataclasses of `render_input`; `classification` defaults to `FIELD_CLASSES`.
    """
    all_classes = classes is None  # stale entries are only judged against the complete set of model classes
    classes = ri.model_classes() if classes is None else classes
    classification = FIELD_CLASSES if classification is None else classification
    problems = []
    seen = set()
    for cls in classes:
        seen.add(cls.__name__)
        table = classification.get(cls.__name__)
        if table is None:
            problems.append(f"{cls.__name__}: the class is not classified")
            continue
        actual = {f.name for f in dataclasses.fields(cls)}
        for name in sorted(actual - set(table)):
            problems.append(f"{cls.__name__}.{name}: the field is not classified")
        for name in sorted(set(table) - actual):
            problems.append(f"{cls.__name__}.{name}: classified but the field does not exist")
        for name, kind in table.items():
            if kind not in (INCLUDED, EXCLUDED):
                problems.append(f"{cls.__name__}.{name}: the classification must be 'included' or 'excluded'")
    if all_classes:
        for name in sorted(set(classification) - seen):
            problems.append(f"{name}: classified but the class does not exist")
    return problems


def _check_text(text: str, where: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise RenderInputHashError(f"{where}: text cannot be encoded as UTF-8") from None
    return text


def _decimal_text(value: Decimal, where: str) -> str:
    if not value.is_finite():
        raise RenderInputHashError(f"{where}: a Decimal must be finite")
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _conforms(value, hint) -> bool:
    """True when `value` has exactly the type its field is declared with (no bool as int, no str as date...)."""
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        return any(_conforms(value, arm) for arm in typing.get_args(hint))
    if hint is type(None):
        return value is None
    if origin is tuple:
        item_type = typing.get_args(hint)[0]
        return isinstance(value, tuple) and all(_conforms(item, item_type) for item in value)
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if hint is dt.date:
        return isinstance(value, dt.date) and not isinstance(value, dt.datetime)
    return isinstance(value, hint)


def _value(value, where: str):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        return _check_text(value, where)
    if isinstance(value, Decimal):
        return _decimal_text(value, where)
    if isinstance(value, dt.datetime):
        raise RenderInputHashError(f"{where}: a datetime is not accepted; use a date")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_value(item, f"{where}[{index}]") for index, item in enumerate(value)]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _model(value, where)
    raise RenderInputHashError(f"{where}: unsupported value of type {type(value).__name__}")


def _model(obj, where: str) -> dict:
    cls = type(obj)
    if cls not in ri.model_classes():
        raise RenderInputHashError(f"{where}: {cls.__name__} is not a render-input model class")
    table = FIELD_CLASSES.get(cls.__name__)
    if table is None:
        raise RenderInputHashError(f"{where}: {cls.__name__} is not classified")
    actual = [f.name for f in dataclasses.fields(obj)]
    unclassified = [name for name in actual if name not in table]
    if unclassified:
        raise RenderInputHashError(f"{where}: {cls.__name__}.{unclassified[0]} is not classified as included or "
                                   "excluded")
    stale = [name for name in table if name not in actual]
    if stale:
        raise RenderInputHashError(f"{where}: {cls.__name__}.{stale[0]} is classified but does not exist")
    hints = typing.get_type_hints(cls)
    payload = {}
    for name in actual:
        kind = table[name]
        if not _conforms(getattr(obj, name), hints[name]):
            raise RenderInputHashError(f"{where}.{name}: the value does not have the declared type "
                                       f"({type(getattr(obj, name)).__name__})")
        if kind == INCLUDED:
            payload[name] = _value(getattr(obj, name), f"{where}.{name}")
        elif kind != EXCLUDED:
            raise RenderInputHashError(f"{where}: {cls.__name__}.{name} has an invalid classification")
    return payload


def build_payload(render_input) -> dict:
    """The JSON-ready canonical payload of a `ClientRenderInput` (a new dictionary)."""
    if not isinstance(render_input, ri.ClientRenderInput):
        raise RenderInputHashError(f"render_input must be a ClientRenderInput, got {type(render_input).__name__}")
    return {"format": FORMAT_NAME, "version": FORMAT_VERSION, "input": _model(render_input, "render_input")}


def canonical_render_input_json(render_input) -> str:
    """The exact text that is hashed."""
    payload = build_payload(render_input)
    try:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        text.encode("utf-8")
    except (TypeError, ValueError):
        raise RenderInputHashError("the payload cannot be serialized") from None
    return text


def render_input_sha256(render_input) -> str:
    """Lowercase hexadecimal SHA-256 (64 characters) of the canonical JSON."""
    return hashlib.sha256(canonical_render_input_json(render_input).encode("utf-8")).hexdigest()
