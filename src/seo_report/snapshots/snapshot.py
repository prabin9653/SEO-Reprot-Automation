"""A deterministic, privacy-filtered snapshot of one completed reporting run.

`build_snapshot` turns the objects a run already has (the engine's ReportInsights, the fetched
Search Console and GA4 data, the sheet's keyword rows and the client) into a JSON-ready payload.
It is pure: no I/O, no clock, no randomness. Run IDs and timestamps are assigned by the store.

Privacy: the payload is built field by field from an explicit whitelist. Nothing is copied from
the inputs wholesale, so credentials, tokens, spreadsheet IDs, manager or sender details, sheet
notes and target URLs cannot reach a snapshot. Free text is masked for emails, phone numbers and
token-like strings. Search queries and pages that are not safe to print are dropped entirely,
with no hash or other trace except a count.

Missing values stay None (stored as NULL); a missing value is never turned into zero.
Search Console totals are never reconciled with breakdown sums, and GA4's daily-session
difference is stored as a diagnostic, not corrected.
"""

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum

from seo_report.metrics import safe_page_label, safe_query_label
from seo_report.rankings import RankingKind

FORMAT_VERSION = 1
ENGINE_VERSION = "insights-1"
GENESIS = "GENESIS"  # the "previous" hash of the very first run in a store
CLIENT_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
MAX_STORED = {"countries": 300, "queries": 500, "pages": 200, "landing_pages": 250}
PLACEHOLDER_PATHS = ("(not set)", "(other)")
METRIC_FIELDS = ("sessions", "total_users", "engaged_sessions", "engagement_rate", "key_events")

# Columns of the queryable tables (after the id and run_id columns), in insertion order.
PROJECTION_COLUMNS = {
    "run_sections": ("ordinal", "key", "title", "status", "has_strength", "paragraphs_json", "omitted_json"),
    "run_keywords": ("ordinal", "keyword", "target_location", "prev_kind", "prev_position", "prev_label",
                     "cur_kind", "cur_position", "cur_label"),
    "gsc_periods": ("which", "status", "clicks", "impressions", "ctr", "position", "latest_data_date",
                    "source_truncated_json", "unsafe_queries_dropped", "unsafe_pages_dropped"),
    "gsc_daily": ("which", "date", "has_data", "clicks", "impressions", "ctr", "position"),
    "gsc_countries": ("which", "code", "name", "clicks", "impressions", "ctr", "position"),
    "gsc_queries": ("which", "label", "clicks", "impressions", "ctr", "position"),
    "gsc_pages": ("which", "path", "clicks", "impressions", "ctr", "position"),
    "ga4_periods": ("which", "scope", "status", "time_zone", "sessions", "total_users", "engaged_sessions",
                    "engagement_rate", "key_events"),
    "ga4_channels": ("which", "channel", "sessions", "total_users", "engaged_sessions", "engagement_rate",
                     "key_events"),
    "ga4_daily": ("which", "date", "has_data", "sessions", "organic_sessions"),
    "ga4_landing_pages": ("which", "scope", "path", "is_placeholder", "sessions", "total_users",
                          "engaged_sessions", "engagement_rate", "key_events"),
    "run_insights": ("section", "ordinal", "key", "tone", "priority", "text", "evidence_json", "tags_json"),
    "run_recommendations": ("ordinal", "key", "kind", "priority", "title", "action", "trigger", "evidence_json",
                            "supporting_json"),
    "run_notes": ("ordinal", "severity", "source", "category", "message"),
    "run_diagnostics": ("source", "which", "name", "value"),
    "run_warnings": ("source", "which", "code", "message", "detail"),
}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


# --- canonical form and hashes -------------------------------------------------------------------------

def canonical_json(obj) -> str:
    """The one JSON text used for hashing and storage: sorted keys, no spaces, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_content_hash(payload: dict) -> str:
    return sha256_hex(canonical_json(payload))


def chain_hash(prev_chain: str, *, seq: int, run_id: str, client_key: str, period_start: str, period_end: str,
               revision: int, content_hash: str, created_at: str) -> str:
    """Hash linking a run to the one before it.

    A broken or reordered chain reveals accidental damage or edits to individual runs. It is not
    proof against someone who can rewrite the whole database and recompute every hash.
    """
    fields = (prev_chain, seq, run_id, client_key, period_start, period_end, revision, content_hash, created_at)
    return sha256_hex("\n".join(str(f) for f in fields))


@dataclass(frozen=True)
class Snapshot:
    client_key: str
    period_start: str
    period_end: str
    payload: dict
    content_hash: str
    quality: str  # "complete", "partial" or "incomplete"

    @property
    def payload_json(self) -> str:
        return canonical_json(self.payload)


# --- privacy -------------------------------------------------------------------------------------------------

class _Sanitizer:
    """Masks emails, phone numbers and token-like strings, and counts how often it had to."""

    def __init__(self):
        self.masked = 0

    def text(self, value):
        if value is None:
            return None
        text = " ".join(str(value).split())
        cleaned = _EMAIL.sub("[email]", text)
        cleaned = _PHONE.sub(lambda m: "[phone]" if sum(c.isdigit() for c in m.group()) >= 10 else m.group(),
                             cleaned)
        cleaned = _TOKEN.sub("[redacted]", cleaned)
        if cleaned != text:
            self.masked += 1
        return cleaned

    def jsonable(self, value):
        """Convert evidence-like values to plain JSON types; unknown types are refused."""
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("non-finite numbers cannot be stored")
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Enum):
            return self.jsonable(value.value)
        if isinstance(value, (list, tuple)):
            return [self.jsonable(v) for v in value]
        if isinstance(value, dict):
            return {str(k): self.jsonable(v) for k, v in value.items()}
        if hasattr(value, "isoformat"):
            return value.isoformat()
        raise TypeError(f"unsupported value type {type(value).__name__}")


def mask_text(value) -> str | None:
    """Mask personal or secret-looking text (used for free-text event details)."""
    return _Sanitizer().text(value)


# --- metrics and statuses ---------------------------------------------------------------------------------------

def _gsc_metrics(metrics, present: bool = True) -> dict:
    if not present:
        return {"clicks": None, "impressions": None, "ctr": None, "position": None}
    return {"clicks": metrics.clicks, "impressions": metrics.impressions, "ctr": metrics.ctr,
            "position": metrics.position}


def _ga4_metrics(metrics) -> dict:
    if metrics is None:
        return {name: None for name in METRIC_FIELDS}
    return {name: getattr(metrics, name) for name in METRIC_FIELDS}


def _warnings(source: str, warnings, san: _Sanitizer) -> list[dict]:
    return [{"code": w.code, "message": san.text(w.message),
             "detail": san.text(getattr(w, "dimension", None) or getattr(w, "report", None))} for w in warnings]


def _status(empty: bool, codes: set, truncated: list) -> tuple[str, list[str]]:
    issues = []
    if empty:
        issues.append("empty")
    if "incomplete_data" in codes:
        issues.append("incomplete")
    if truncated:
        issues.append("truncated")
    status = "empty" if empty else "incomplete" if "incomplete_data" in codes else "truncated" if truncated else "ok"
    return status, issues


def _top(rows: list[dict], limit: int, sort_key) -> tuple[list[dict], bool]:
    rows = sorted(rows, key=sort_key)
    return rows[:limit], len(rows) > limit


def _metric_sort(label_field: str):
    return lambda r: (-(r["clicks"] or 0), -(r["impressions"] or 0), str(r[label_field]))


def _gsc_period(period, san: _Sanitizer) -> dict:
    codes = {w.code for w in period.warnings}
    truncated = sorted({w.dimension for w in period.warnings if w.code == "truncated" and w.dimension})
    empty = "empty_period" in codes or (period.totals.clicks == 0 and period.totals.impressions == 0)
    status, issues = _status(empty, codes, truncated)
    daily = [{"date": d.date.isoformat(), "has_data": bool(d.has_data),
              **_gsc_metrics(d.metrics, present=bool(d.has_data) and not empty)} for d in period.daily]
    result = {"status": status, "issues": issues, "totals": _gsc_metrics(period.totals, present=not empty),
              "daily": daily, "countries": [], "queries": [], "pages": [],
              "latest_data_date": period.latest_data_date.isoformat() if period.latest_data_date else None,
              "warnings": _warnings("gsc", period.warnings, san), "source_truncated": truncated,
              "dropped": {"queries": 0, "pages": 0}, "capped": {"countries": False, "queries": False, "pages": False}}
    if empty:
        return result

    countries = [{"code": san.text(r.key), "name": san.text(r.label), **_gsc_metrics(r.metrics)}
                 for r in period.countries]
    queries, pages = [], []
    for r in period.queries:
        label = safe_query_label(r.key)
        if label is None:
            result["dropped"]["queries"] += 1
        else:
            queries.append({"label": san.text(label), **_gsc_metrics(r.metrics)})
    for r in period.pages:
        path = safe_page_label(r.key)
        if path is None:
            result["dropped"]["pages"] += 1
        else:
            pages.append({"path": san.text(path), **_gsc_metrics(r.metrics)})
    result["countries"], result["capped"]["countries"] = _top(countries, MAX_STORED["countries"], _metric_sort("code"))
    result["queries"], result["capped"]["queries"] = _top(queries, MAX_STORED["queries"], _metric_sort("label"))
    result["pages"], result["capped"]["pages"] = _top(pages, MAX_STORED["pages"], _metric_sort("path"))
    return result


def _landing(pages, san: _Sanitizer) -> tuple[list[dict], int, bool]:
    kept, dropped = [], 0
    for p in pages:
        if p.is_placeholder or p.path in PLACEHOLDER_PATHS:
            path = p.path if p.path in PLACEHOLDER_PATHS else "(not set)"
        else:
            path = safe_page_label(p.path)
        if path is None:
            dropped += 1
            continue
        kept.append({"path": san.text(path), "is_placeholder": bool(p.is_placeholder), **_ga4_metrics(p.metrics)})
    kept.sort(key=lambda r: (-(r["sessions"] or 0), r["path"]))
    return kept[:MAX_STORED["landing_pages"]], dropped, len(kept) > MAX_STORED["landing_pages"]


def _ga4_period(period, san: _Sanitizer) -> dict:
    codes = {w.code for w in period.warnings}
    truncated = sorted({w.report for w in period.warnings if w.code == "truncated" and w.report})
    empty = bool(period.is_empty) or "empty_period" in codes
    status, issues = _status(empty, codes, truncated)
    daily = [{"date": d.date.isoformat(), "has_data": bool(d.has_data),
              "sessions": d.sessions if d.has_data and not empty else None,
              "organic_sessions": d.organic_sessions if d.has_data and not empty else None} for d in period.daily]
    result = {"status": status, "issues": issues, "time_zone": san.text(period.time_zone),
              "site": _ga4_metrics(None if empty else period.whole_site),
              "organic": _ga4_metrics(None if empty else period.organic),
              "channels": [], "daily": daily, "landing_pages": [], "organic_landing_pages": [],
              "warnings": _warnings("ga4", period.warnings, san), "source_truncated": truncated,
              "dropped": {"landing_pages": 0, "organic_landing_pages": 0},
              "capped": {"landing_pages": False, "organic_landing_pages": False}}
    if empty:
        return result
    channels = [{"channel": san.text(c.channel), **_ga4_metrics(c.metrics)} for c in period.channels]
    channels.sort(key=lambda c: (-(c["sessions"] or 0), c["channel"]))
    result["channels"] = channels[:100]
    for name, source in (("landing_pages", period.landing_pages), ("organic_landing_pages", period.organic_landing_pages)):
        result[name], result["dropped"][name], result["capped"][name] = _landing(source, san)
    return result


def _ranking(ranking, san: _Sanitizer) -> dict:
    label = san.text(ranking.text) if ranking.kind is RankingKind.ABOVE_10 else None  # verbatim
    return {"kind": ranking.kind.value, "position": ranking.position, "label": label}


def _quality(rankings: dict, gsc: dict, ga4: dict, output: dict) -> tuple[str, list[str]]:
    incomplete, partial = [], []
    for name, source in (("Search Console", gsc), ("GA4", ga4)):
        if not source["provided"]:
            incomplete.append(f"{name} data was not provided")
            continue
        current, previous = source["periods"]["current"]["status"], source["periods"]["previous"]["status"]
        if current == "empty":
            incomplete.append(f"{name} has no data for the current period")
        elif current != "ok":
            partial.append(f"{name} current-period data is {current}")
        if previous != "ok":
            partial.append(f"{name} previous-period data is {previous}")
    if rankings["status"] == "missing":
        partial.append("no current keyword rankings are recorded")
    elif rankings["status"] == "partial":
        partial.append("some current keyword rankings are not recorded")
    if output["validation_failures"]:
        partial.append("some insights failed validation and were left out")
    if any(s["status"] == "unavailable" for s in output["sections"]):
        partial.append("a report section has no verified insights")
    level = "incomplete" if incomplete else "partial" if partial else "complete"
    return level, incomplete + partial


# --- the builder ---------------------------------------------------------------------------------------------------

def _check_periods(source, periods, name: str):
    if source is None:
        return
    if (source.current.period.start, source.current.period.end) != (periods.current.start, periods.current.end) \
            or (source.previous.period.start, source.previous.period.end) != (periods.previous.start,
                                                                            periods.previous.end):
        raise ValueError(f"the {name} data does not cover the reporting periods")


def build_snapshot(*, client_key: str, client, periods, gsc, ga4, keywords, insights,
                   recommendation_config=None) -> Snapshot:
    """Build the snapshot for one run.

    `client_key` is a stable identifier chosen by the caller (it never changes when the display name does).
    `gsc` / `ga4` are the fetched data (or None), `keywords` the sheet's keyword rows, `insights` the
    engine's ReportInsights.
    """
    if not isinstance(client_key, str) or not CLIENT_KEY_RE.fullmatch(client_key):
        raise ValueError("client_key must be 1-63 characters of lowercase letters, digits, '-' or '_', "
                         "starting with a letter or digit")
    _check_periods(gsc, periods, "Search Console")
    _check_periods(ga4, periods, "GA4")
    san = _Sanitizer()

    rankings_summary = insights.metrics.rankings
    rankings_status = ("none" if rankings_summary.total == 0 else "missing" if rankings_summary.all_current_missing
                       else "partial" if rankings_summary.missing_current else "ok")
    rankings = {
        "status": rankings_status,
        "counts": {"total": rankings_summary.total, "numeric_current": rankings_summary.numeric_current,
                   "above_10_current": rankings_summary.above_10_current,
                   "missing_current": rankings_summary.missing_current},
        "keywords": [{"ordinal": i, "keyword": san.text(k.keyword),
                      "target_location": san.text(getattr(k, "target_location", None)),
                      "previous": _ranking(k.previous, san), "current": _ranking(k.current, san)}
                     for i, k in enumerate(keywords)],
    }

    if gsc is None:
        gsc_block = {"provided": False}
    else:
        gsc_block = {"provided": True, "site_url": san.text(gsc.site_url),
                     "permission_level": san.text(gsc.permission_level),
                     "periods": {"current": _gsc_period(gsc.current, san), "previous": _gsc_period(gsc.previous, san)}}
    if ga4 is None:
        ga4_block = {"provided": False}
    else:
        ga4_block = {"provided": True, "property_id": san.text(ga4.property_id),
                     "key_events_status": ga4.key_events_status,
                     "periods": {"current": _ga4_period(ga4.current, san), "previous": _ga4_period(ga4.previous, san)}}

    sections = []
    for s in insights.sections:
        sections.append({
            "key": s.key, "title": s.title, "status": s.status, "has_strength": bool(s.has_strength),
            "paragraphs": [san.text(p) for p in s.paragraphs], "omitted": list(s.omitted),
            "insights": [{"key": i.key, "tone": i.tone.value, "priority": i.priority, "text": san.text(i.text),
                          "evidence": san.jsonable(i.evidence), "tags": sorted(i.tags)} for i in s.insights]})
    output = {
        "sections": sections,
        "recommendations": [{"key": r.key, "kind": r.kind, "priority": r.priority, "title": san.text(r.title),
                             "action": san.text(r.action), "trigger": san.text(r.trigger),
                             "evidence": san.jsonable(r.evidence), "supporting": list(r.supporting)}
                            for r in insights.recommendations],
        "omitted_recommendations": list(insights.omitted_recommendations),
        "notes": [{"severity": n.severity, "source": n.source, "category": n.category,
                   "message": san.text(n.message)} for n in insights.notes],
        "validation_failures": [{"key": key, "reason": san.text(reason)} for key, reason in insights.validation_failures],
    }
    diagnostics = {source: {which: {str(k): int(v) for k, v in values.items()} for which, values in per.items()}
                   for source, per in sorted(insights.diagnostics.items())}
    level, reasons = _quality(rankings, gsc_block, ga4_block, output)
    eligibility = {
        "rankings": rankings_summary.total > 0 and not rankings_summary.all_current_missing,
        "gsc": bool(gsc_block["provided"]) and gsc_block["periods"]["current"]["status"] == "ok",
        "ga4": bool(ga4_block["provided"]) and ga4_block["periods"]["current"]["status"] == "ok",
    }
    config = recommendation_config
    settings = {
        "engine_version": ENGINE_VERSION,
        "thresholds": san.jsonable(dataclasses.asdict(insights.metrics.thresholds)),
        "recommendations": (None if config is None else
                            {"standing": list(config.standing), "target_city": san.text(config.target_city),
                             "max_items": config.max_items}),
    }
    payload = {
        "format": FORMAT_VERSION,
        "settings": settings,
        "client": {"client_key": client_key, "name": san.text(client.name), "website": san.text(client.website),
                   "gsc_property": san.text(client.gsc_property), "ga4_property_id": san.text(client.ga4_property_id),
                   "status": san.text(client.status), "target_country": san.text(client.target_country),
                   "target_city": san.text(client.target_city)},
        "periods": {"current": {"start": periods.current.start.isoformat(), "end": periods.current.end.isoformat()},
                    "previous": {"start": periods.previous.start.isoformat(), "end": periods.previous.end.isoformat()}},
        "rankings": rankings, "gsc": gsc_block, "ga4": ga4_block, "output": output,
        "diagnostics": diagnostics,
        "quality": {"level": level, "reasons": reasons},
        "eligibility": eligibility,
    }
    payload["privacy"] = {"masked_fields": san.masked}
    return Snapshot(client_key, payload["periods"]["current"]["start"], payload["periods"]["current"]["end"],
                    payload, compute_content_hash(payload), level)


# --- values derived from a payload (shared by the writer and the verifier) -------------------------------------------

def run_columns(payload: dict) -> tuple:
    """The descriptive columns of a `runs` row, in table order, derived from the payload."""
    client, periods, eligibility = payload["client"], payload["periods"], payload["eligibility"]
    return (client["client_key"], client["name"], client["website"], client["gsc_property"],
            client["ga4_property_id"], client["status"], client["target_country"], client["target_city"],
            periods["current"]["start"], periods["current"]["end"], periods["previous"]["start"],
            periods["previous"]["end"], payload["format"], payload["settings"]["engine_version"],
            payload["quality"]["level"], int(eligibility["rankings"]), int(eligibility["gsc"]),
            int(eligibility["ga4"]))


RUN_COLUMNS = ("client_key", "client_name", "website", "gsc_property", "ga4_property_id", "status_at_run",
               "target_country", "target_city", "period_start", "period_end", "prev_start", "prev_end",
               "format_version", "engine_version", "quality", "eligible_rankings", "eligible_gsc", "eligible_ga4")


def _gsc_row(which: str, key_fields: tuple, row: dict) -> tuple:
    return (which, *key_fields, row["clicks"], row["impressions"], row["ctr"], row["position"])


def projection_rows(payload: dict) -> dict[str, list[tuple]]:
    """The rows of every queryable table for this payload, in the column order of PROJECTION_COLUMNS."""
    rows = {name: [] for name in PROJECTION_COLUMNS}
    output = payload["output"]
    insight_ordinal = 0
    for i, s in enumerate(output["sections"]):
        rows["run_sections"].append((i, s["key"], s["title"], s["status"], int(s["has_strength"]),
                                     canonical_json(s["paragraphs"]), canonical_json(s["omitted"])))
        for ins in s["insights"]:
            rows["run_insights"].append((s["key"], insight_ordinal, ins["key"], ins["tone"], ins["priority"],
                                         ins["text"], canonical_json(ins["evidence"]), canonical_json(ins["tags"])))
            insight_ordinal += 1
    for i, r in enumerate(output["recommendations"]):
        rows["run_recommendations"].append((i, r["key"], r["kind"], r["priority"], r["title"], r["action"],
                                            r["trigger"], canonical_json(r["evidence"]),
                                            canonical_json(r["supporting"])))
    for i, n in enumerate(output["notes"]):
        rows["run_notes"].append((i, n["severity"], n["source"], n["category"], n["message"]))
    for k in payload["rankings"]["keywords"]:
        p, c = k["previous"], k["current"]
        rows["run_keywords"].append((k["ordinal"], k["keyword"], k["target_location"], p["kind"], p["position"],
                                     p["label"], c["kind"], c["position"], c["label"]))
    for source, per in payload["diagnostics"].items():
        for which in ("current", "previous"):
            for name, value in sorted(per.get(which, {}).items()):
                rows["run_diagnostics"].append((source, which, name, value))

    if payload["gsc"]["provided"]:
        for which, p in payload["gsc"]["periods"].items():
            t = p["totals"]
            rows["gsc_periods"].append((which, p["status"], t["clicks"], t["impressions"], t["ctr"], t["position"],
                                        p["latest_data_date"], canonical_json(p["source_truncated"]),
                                        p["dropped"]["queries"], p["dropped"]["pages"]))
            rows["gsc_daily"] += [(which, d["date"], int(d["has_data"]), d["clicks"], d["impressions"], d["ctr"],
                                   d["position"]) for d in p["daily"]]
            rows["gsc_countries"] += [_gsc_row(which, (c["code"], c["name"]), c) for c in p["countries"]]
            rows["gsc_queries"] += [_gsc_row(which, (q["label"],), q) for q in p["queries"]]
            rows["gsc_pages"] += [_gsc_row(which, (g["path"],), g) for g in p["pages"]]
            rows["run_warnings"] += [("gsc", which, w["code"], w["message"], w["detail"]) for w in p["warnings"]]
    if payload["ga4"]["provided"]:
        for which, p in payload["ga4"]["periods"].items():
            for scope in ("site", "organic"):
                m = p[scope]
                rows["ga4_periods"].append((which, scope, p["status"], p["time_zone"], *(m[f] for f in METRIC_FIELDS)))
            rows["ga4_channels"] += [(which, c["channel"], *(c[f] for f in METRIC_FIELDS)) for c in p["channels"]]
            rows["ga4_daily"] += [(which, d["date"], int(d["has_data"]), d["sessions"], d["organic_sessions"])
                                  for d in p["daily"]]
            for scope, name in (("site", "landing_pages"), ("organic", "organic_landing_pages")):
                rows["ga4_landing_pages"] += [(which, scope, g["path"], int(g["is_placeholder"]),
                                               *(g[f] for f in METRIC_FIELDS)) for g in p[name]]
            rows["run_warnings"] += [("ga4", which, w["code"], w["message"], w["detail"]) for w in p["warnings"]]
    return rows
