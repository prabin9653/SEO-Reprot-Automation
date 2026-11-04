"""Which parts of a stored snapshot may be used as a historical baseline, dimension by dimension.

Pure functions over the stored payload (format 1). Eligibility is decided independently for every
dimension, so, for example, a truncated Search Console query list makes only the query dimension
ineligible and leaves the Search Console totals (and every other dimension) usable.

Rules, in short:
  * a source that was not provided, or whose period is empty or incomplete, makes all of its
    dimensions ineligible (the whole date range is missing or partial);
  * a truncated dimension is ineligible, and nothing else is affected;
  * a dimension whose stored list was capped keeps its values but cannot support "new" or "lost"
    claims (membership is incomplete);
  * GA4 thresholding, sampling and "(other)" folding do not block a comparison, but they are
    recorded as caveats: such results are not complete or exact.
"""

from dataclasses import dataclass

SUPPORTED_FORMATS = (1,)

RANKINGS = "rankings.keywords"
GSC_DIMENSIONS = ("gsc.totals", "gsc.daily", "gsc.countries", "gsc.queries", "gsc.pages")
GA4_DIMENSIONS = ("ga4.site", "ga4.organic", "ga4.channels", "ga4.daily", "ga4.landing_pages",
                  "ga4.organic_landing_pages")
DIMENSIONS = (RANKINGS, *GSC_DIMENSIONS, *GA4_DIMENSIONS)

# Which Search Console dimension name (as recorded in `source_truncated`) affects each dimension.
_GSC_TRUNCATION = {"gsc.daily": "date", "gsc.countries": "country", "gsc.queries": "query", "gsc.pages": "page"}
# Which GA4 report (as recorded in `source_truncated` and warning details) each dimension is built from.
# Organic totals come from the channel report, so they share its truncation.
_GA4_REPORT = {"ga4.site": "totals", "ga4.organic": "channels", "ga4.channels": "channels", "ga4.daily": "daily",
               "ga4.landing_pages": "landing_pages", "ga4.organic_landing_pages": "organic_landing_pages"}
_GA4_CAVEATS = {
    "thresholded": "GA4 reported thresholding for this data, so it is not complete or exact",
    "sampled": "GA4 sampled this data, so it is not exact",
    "other_row": "GA4 folded some rows into '(other)', so the list is not complete",
}


@dataclass(frozen=True)
class DimensionEligibility:
    dimension: str
    eligible: bool
    membership_complete: bool  # False when the stored list was capped or folded: no "new" or "lost" claims
    reasons: tuple[str, ...] = ()  # why it is not eligible
    caveats: tuple[str, ...] = ()  # eligible, but with reduced reliability


def _result(dimension, reasons, caveats=(), complete=True) -> DimensionEligibility:
    eligible = not reasons
    return DimensionEligibility(dimension, eligible, eligible and complete, tuple(reasons), tuple(caveats))


def _rankings(payload: dict) -> DimensionEligibility:
    status = payload["rankings"]["status"]
    if status == "none":
        return _result(RANKINGS, ["no keywords were tracked"])
    if status == "missing":
        return _result(RANKINGS, ["no current keyword rankings were recorded"])
    return _result(RANKINGS, [])


def _gsc(payload: dict) -> dict[str, DimensionEligibility]:
    block = payload["gsc"]
    if not block["provided"]:
        return {d: _result(d, ["Search Console data was not provided"]) for d in GSC_DIMENSIONS}
    period = block["periods"]["current"]
    truncated = set(period["source_truncated"])
    results = {}
    for dimension in GSC_DIMENSIONS:
        reasons, caveats = [], []
        if period["status"] == "empty":
            reasons.append("the Search Console period has no data")
        elif period["status"] == "incomplete":
            reasons.append("the Search Console period is incomplete (recent days may not be final)")
        key = _GSC_TRUNCATION.get(dimension)
        if key and key in truncated:
            reasons.append(f"the Search Console {key} data was truncated")
        if not reasons:
            if dimension == "gsc.totals" and period["totals"]["impressions"] is None:
                reasons.append("no Search Console totals were recorded")
            if dimension == "gsc.daily" and not any(d["has_data"] for d in period["daily"]):
                reasons.append("no daily Search Console data was recorded")
        capped = bool(period["capped"].get(dimension.split(".")[1]))
        if capped:
            caveats.append("only the top rows were stored, so new or lost items cannot be reported")
        results[dimension] = _result(dimension, reasons, caveats, complete=not capped)
    return results


def _ga4(payload: dict) -> dict[str, DimensionEligibility]:
    block = payload["ga4"]
    if not block["provided"]:
        return {d: _result(d, ["GA4 data was not provided"]) for d in GA4_DIMENSIONS}
    period = block["periods"]["current"]
    truncated = set(period["source_truncated"])
    results = {}
    for dimension in GA4_DIMENSIONS:
        report = _GA4_REPORT[dimension]
        reasons, caveats, complete = [], [], True
        if period["status"] == "empty":
            reasons.append("the GA4 period has no data")
        elif period["status"] == "incomplete":
            reasons.append("the GA4 period is incomplete (recent days may still be processing)")
        if report in truncated:
            reasons.append(f"the GA4 {report} report was truncated")
        if not reasons:
            if dimension == "ga4.site" and period["site"]["sessions"] is None:
                reasons.append("no GA4 site totals were recorded")
            if dimension == "ga4.organic" and period["organic"]["sessions"] is None:
                reasons.append("no GA4 organic totals were recorded")
            if dimension == "ga4.daily" and not any(d["has_data"] for d in period["daily"]):
                reasons.append("no daily GA4 data was recorded")
        for warning in period["warnings"]:
            if warning["detail"] == report and warning["code"] in _GA4_CAVEATS:
                caveats.append(_GA4_CAVEATS[warning["code"]])
                complete = complete and warning["code"] != "other_row"
        if period["capped"].get(dimension.split(".")[1]):
            caveats.append("only the top rows were stored, so new or lost items cannot be reported")
            complete = False
        results[dimension] = _result(dimension, reasons, tuple(dict.fromkeys(caveats)), complete)
    return results


def assess(payload: dict) -> dict[str, DimensionEligibility]:
    """Eligibility of every dimension of this snapshot's own (current) period, in DIMENSIONS order."""
    results = {RANKINGS: _rankings(payload), **_gsc(payload), **_ga4(payload)}
    return {dimension: results[dimension] for dimension in DIMENSIONS}


def compatibility(current: dict, baseline: dict) -> dict[str, tuple[str, ...]]:
    """Reasons a baseline cannot be compared with the current run, per dimension (empty when fine)."""
    reasons = {dimension: () for dimension in DIMENSIONS}
    if baseline.get("format") not in SUPPORTED_FORMATS or current.get("format") not in SUPPORTED_FORMATS:
        return {dimension: ("the snapshot format is not supported",) for dimension in DIMENSIONS}
    if current["gsc"]["provided"] and baseline["gsc"]["provided"] \
            and current["gsc"]["site_url"] != baseline["gsc"]["site_url"]:
        for dimension in GSC_DIMENSIONS:
            reasons[dimension] = ("the Search Console property differs from the current report's",)
    if current["ga4"]["provided"] and baseline["ga4"]["provided"] \
            and current["ga4"]["property_id"] != baseline["ga4"]["property_id"]:
        for dimension in GA4_DIMENSIONS:
            reasons[dimension] = ("the GA4 property differs from the current report's",)
    return reasons
