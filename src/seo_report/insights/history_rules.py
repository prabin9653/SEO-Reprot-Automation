"""Historical insight rules: what the Stage 6b comparison allows the report to say, and what stays reviewer-only.

`evaluate_history_rules` reads a `HistoricalComparison` (Stage 6b), the live `ReportMetrics`, the base insights and,
optionally, the ranking cross-check of Stage 6c-3. It returns a `HistoryRuleResult` with four separate parts:

  client_candidates     historical statements that may be shown to the client ("Since the last report")
  reviewer_findings     historical observations for the reviewer only
  notes                 ordinary reviewer notes (`ReviewerNote`)
  validation_failures   candidates that were built and then rejected, each with a code from VALIDATION_REASONS

This stage only defines the rules. It does not compose sections or enforce the two-statements-per-section limit
(Stage 6c-5 does); every client candidate carries `section`, `priority` and `group` so that limit can be applied.
Nothing is recalculated from raw data: every figure comes from the comparison, and nothing is invented.

Client-facing rules (everything else is reviewer-only)

  A. GSC and GA4 totals (clicks and impressions; sessions and organic sessions) may be stated against the
     historical baseline only when the live previous period is genuinely empty (`previous_empty`), the live
     current period is not empty or incomplete, the dimension is eligible with a named baseline of the same
     length, no invalidating caveat applies, the change is comparable and, for GA4, both periods have at least
     `thresholds.ga4_min_sessions` sessions. A small or low previous value is never an empty period.
  B. The target-country share of Search Console impressions may be stated only when the totals and countries
     dimensions are eligible and use the same baseline run, both periods have at least 500 impressions
     (GEO_MIN_IMPRESSIONS), the share moves by at least 5 percentage points (GEO_MIN_SHIFT), and no invalidating
     caveat applies. Share is target impressions / total impressions; the change is in percentage points.
  C. Rankings: the sheet stays the source of truth. Pending fallbacks, mismatches, new and no-longer-tracked
     keywords and non-comparable keywords produce reviewer findings only, never client text, markers or footnotes.
  D. New and no-longer-tracked keywords are reviewer-only.
  E. The GA4 daily-sessions discrepancy is a reviewer finding, never a performance statement.

An invalidating caveat is any caveat on the dimension that is not the informational "only the top rows were
stored" note; this is the convention Stage 6b itself uses when it writes its "do not describe this comparison as
complete or exact" notes.

The module is pure: no I/O, no clock, no database and no mutation of its inputs.
"""

import datetime as dt
import math
import re
from dataclasses import dataclass

from seo_report.insights import wording as w
from seo_report.insights.rules import SECTIONS, ReviewerNote, Tone, evidence_tokens
from seo_report.metrics import Direction
from seo_report.snapshots import eligibility as el

RULES_VERSION = "1"
STATEMENT_GROUP = "since_last_report"
MAX_STATEMENTS_PER_SECTION = 2  # applied by Stage 6c-5, not here
AUDIENCE_CLIENT = "client"
AUDIENCE_REVIEWER = "reviewer"
REVIEWER_SECTIONS = (*SECTIONS, "history")
# Rankings never get client wording from history (Rules C and D), so "keywords" is not a client section here.
CLIENT_SECTIONS = tuple(section for section in SECTIONS if section != "keywords")

GEO_MIN_IMPRESSIONS = 500
GEO_MIN_SHIFT = 0.05  # 5 percentage points, as a fraction
_INFORMATIONAL_CAVEAT = "only the top rows"

# --- registries ----------------------------------------------------------------------------------------------------------
# Evidence keys: name -> (meaning, may appear in client candidates). Values are flat, finite numbers.

EVIDENCE_KEYS = {
    "current_value": ("the figure for the current period", True),
    "baseline_value": ("the figure for the baseline period", True),
    "change_percent": ("relative change as a fraction (0.25 = 25%), only when the baseline is not a small base", True),
    "direction": ("1 when the figure went up, -1 when it went down, 0 when it did not change meaningfully", True),
    "baseline_days": ("number of days in the baseline period", True),
    "current_days": ("number of days in the current period", False),
    "share_current": ("target-country share of impressions in the current period, as a fraction", True),
    "share_baseline": ("target-country share of impressions in the baseline period, as a fraction", True),
    "share_change": ("share_current minus share_baseline, as a fraction (0.05 = 5 percentage points)", True),
    "impressions_current": ("total impressions in the current period", True),
    "impressions_baseline": ("total impressions in the baseline period", True),
    "target_impressions_current": ("target-country impressions in the current period", True),
    "target_impressions_baseline": ("target-country impressions in the baseline period", True),
    "min_impressions": ("the impressions minimum that applies", False),
    "min_share_change": ("the minimum share change that applies, as a fraction", False),
    "min_sessions": ("the sessions minimum that applies", False),
    "pending_fallbacks": ("keywords with a blank sheet Previous Ranking and a numeric historical ranking", False),
    "fallback_improved": ("pending fallbacks with a positive movement", False),
    "fallback_declined": ("pending fallbacks with a negative movement", False),
    "fallback_unchanged": ("pending fallbacks with zero movement", False),
    "sheet_mismatches": ("keywords whose sheet Previous Ranking differs from the baseline's Current Ranking", False),
    "new_keywords": ("keywords that were not tracked in the baseline report", False),
    "lost_keywords": ("baseline keywords that are no longer tracked", False),
    "not_comparable_keywords": ("keywords with no numeric ranking on one side or a duplicate match", False),
    "ineligible_dimensions": ("dimensions with no usable baseline although the current data was usable", False),
    "caveated_dimensions": ("eligible dimensions that carry an invalidating caveat", False),
    "baseline_runs": ("number of different baseline reports in use", False),
    "daily_sessions_diff": ("GA4 daily sessions minus the total, preserved and never reconciled", False),
}

# Why a statement that is otherwise computable may not be shown to the client (the `blocked_reason` of a finding).
BLOCK_REASONS = {
    "live_previous_not_empty": "the live previous period has data, so the live comparison is used instead",
    "dimension_not_eligible": "the dimension has no eligible baseline",
    "data_unavailable": "the comparison data needed for the statement is not available",
    "different_period_length": "the baseline covers a different number of days, so counts are not compared",
    "not_comparable": "the figures cannot be compared",
    "caveat_present": "a caveat means the comparison cannot be called complete or exact",
    "current_period_incomplete": "the end of the current period may be incomplete",
    "insufficient_sample": "a period has fewer sessions than the minimum sample",
    "time_zone_differs": "the GA4 reporting time zone differs between the periods",
    "different_baselines": "the totals and countries comparisons use different baseline reports",
    "impressions_below_minimum": "a period has fewer impressions than the minimum",
    "shift_below_minimum": "the change is smaller than the minimum shift",
}

# Validation failure reason codes: stable machine-readable codes for `validation_failures[].reason`.
VALIDATION_REASONS = {
    "invalid_evidence": "the evidence has a key outside the registry, a value that is not a finite number, "
                        "or a key a client candidate may not use",
    "untraced_number": "the text contains a number that is not derived from its evidence",
    "unsafe_wording": "the text has a non-US spelling, an unsupported superlative or a growth word for a figure "
                      "that did not go up",
    "missing_baseline_period": "the baseline period is missing, malformed or not named in the text",
    "duplicate_of_base_insight": "the statement repeats a fact that a base insight already states",
    "inconsistent_comparison": "the wording or evidence contradicts the compared figures",
    "invalid_identity": "the section or key is not a valid historical section or key",
}

FINDING_KINDS = (
    "gsc_historical_change", "ga4_historical_change", "geography_share_change", "ranking_pending_fallback",
    "ranking_sheet_mismatch", "ranking_new_keywords", "ranking_lost_keywords", "ranking_not_comparable",
    "baselines_differ", "dimensions_unusable", "incomplete_comparison_data", "different_period_length",
    "ga4_daily_discrepancy")

_KEY_RE = re.compile(r"history\.[a-z0-9_]+(?:\.[a-z0-9_]+)*")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


# --- result types ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ClientHistoryCandidate:
    """A historical statement that may be shown to the client. It carries no run ID or reviewer information."""

    section: str  # one of rules.SECTIONS
    key: str  # history.<source>.<name>
    dimension: str  # one of eligibility.DIMENSIONS
    text: str
    tone: Tone
    priority: int  # lower comes first within a section
    evidence: dict  # flat: registry keys -> finite numbers
    baseline_period_start: str  # ISO dates of the baseline report named in the text
    baseline_period_end: str
    base_keys: tuple[str, ...]  # base insight keys that cover the same underlying fact (duplicate guard)
    rules_version: str = RULES_VERSION
    group: str = STATEMENT_GROUP
    audience: str = AUDIENCE_CLIENT


@dataclass(frozen=True)
class ReviewerHistoryFinding:
    """A historical observation for the reviewer. Never used as client wording."""

    section: str  # one of REVIEWER_SECTIONS
    key: str  # history.review.<kind>[.<suffix>]
    kind: str  # one of FINDING_KINDS
    dimension: str | None
    message: str
    priority: int
    evidence: dict
    blocked_reason: str | None = None  # one of BLOCK_REASONS when a client statement was withheld
    baseline_period_start: str | None = None
    baseline_period_end: str | None = None
    baseline_run_id: str | None = None
    tone: Tone = Tone.NEUTRAL
    rules_version: str = RULES_VERSION
    audience: str = AUDIENCE_REVIEWER


@dataclass(frozen=True)
class HistoryValidationFailure:
    key: str
    reason: str  # one of VALIDATION_REASONS


@dataclass(frozen=True)
class HistoryRuleResult:
    client_candidates: tuple[ClientHistoryCandidate, ...] = ()
    reviewer_findings: tuple[ReviewerHistoryFinding, ...] = ()
    notes: tuple[ReviewerNote, ...] = ()
    validation_failures: tuple[HistoryValidationFailure, ...] = ()
    rules_version: str = RULES_VERSION


# --- validation ---------------------------------------------------------------------------------------------------------------

def validate_evidence(evidence, *, audience: str = AUDIENCE_CLIENT) -> list[str]:
    """Problems with an evidence dictionary (empty when it is valid): registry keys, flat finite numbers."""
    if not isinstance(evidence, dict):
        return ["evidence must be a flat dictionary"]
    problems = []
    for name, value in evidence.items():
        if not isinstance(name, str) or name not in EVIDENCE_KEYS:
            problems.append(f"{name!r} is not a registered evidence key")
            continue
        if audience == AUDIENCE_CLIENT and not EVIDENCE_KEYS[name][1]:
            problems.append(f"{name!r} is reviewer-only evidence")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{name!r} must be a finite number")
        elif isinstance(value, float) and not math.isfinite(value):
            problems.append(f"{name!r} must be finite")
    return problems


def _normalized_text(text) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(text).casefold()).split())


def _base_list(base_insights) -> list:
    sections = getattr(base_insights, "sections", None)
    if sections is not None:
        return [insight for section in sections for insight in section.insights]
    return list(base_insights or ())


def _duplicates_base(candidate: ClientHistoryCandidate, base: list) -> bool:
    mine = _normalized_text(candidate.text)
    if any(_normalized_text(b.text) == mine for b in base):
        return True
    baseline = candidate.evidence.get("baseline_value")
    for b in base:
        if b.key not in candidate.base_keys:
            continue
        if isinstance(b.evidence, dict) and b.evidence.get("previous") is not None:
            return True  # the base report already states a comparison for this fact
        if baseline is not None and baseline != candidate.evidence.get("current_value") and any(
                token in {t.replace(",", "") for t in w.numbers_in(b.text)}
                for token in _figure_tokens(baseline)):
            return True  # the baseline figure already appears in the base statement of this fact
    return False


def _figure_tokens(value) -> set[str]:
    return {fmt(value).replace(",", "") for fmt in (w.format_count, w.format_compact)}


def _direction_words(text: str) -> dict:
    lowered = text.casefold()
    return {"up": bool(re.search(r"\bup\b", lowered)), "down": bool(re.search(r"\bdown\b", lowered)),
            "flat": "unchanged" in lowered}


def validate_client_candidate(candidate: ClientHistoryCandidate, base_insights=()) -> tuple[str, ...]:
    """Reason codes (from VALIDATION_REASONS) for which a client candidate must be rejected; empty when it passes."""
    reasons = []
    if candidate.section not in CLIENT_SECTIONS or not _KEY_RE.fullmatch(candidate.key) \
            or candidate.dimension not in el.DIMENSIONS or candidate.audience != AUDIENCE_CLIENT:
        reasons.append("invalid_identity")
    problems = validate_evidence(candidate.evidence, audience=AUDIENCE_CLIENT)
    if problems:
        reasons.append("invalid_evidence")
    start, end = candidate.baseline_period_start, candidate.baseline_period_end
    period_text = None
    try:
        if not (_ISO_DATE.fullmatch(start) and _ISO_DATE.fullmatch(end)):
            raise ValueError
        period_text = w.format_date_range(dt.date.fromisoformat(start), dt.date.fromisoformat(end))
    except (TypeError, ValueError):
        pass
    if period_text is None or period_text not in candidate.text:
        reasons.append("missing_baseline_period")
    if not problems and set(w.numbers_in(candidate.text)) - evidence_tokens(candidate.evidence):
        reasons.append("untraced_number")
    direction = candidate.evidence.get("direction") if isinstance(candidate.evidence, dict) else None
    if w.find_non_us_spelling(candidate.text) or w.find_superlatives(candidate.text) or (
            w.find_growth_words(candidate.text) and direction != 1):
        reasons.append("unsafe_wording")
    words = _direction_words(candidate.text)
    expected = {1: (True, False, False), -1: (False, True, False), 0: (False, False, True)}.get(direction)
    if expected is not None and (words["up"], words["down"], words["flat"]) != expected:
        reasons.append("inconsistent_comparison")
    if _duplicates_base(candidate, _base_list(base_insights)):
        reasons.append("duplicate_of_base_insight")
    return tuple(dict.fromkeys(reasons))


# --- shared helpers -----------------------------------------------------------------------------------------------------------------

def _days(start: str, end: str) -> int:
    return (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1


def _range(start: str, end: str) -> str:
    return w.format_date_range(dt.date.fromisoformat(start), dt.date.fromisoformat(end))


def _blocking_caveats(status) -> list[str]:
    return [c for c in status.caveats if not c.startswith(_INFORMATIONAL_CAVEAT)]


def _direction_code(direction: Direction) -> int:
    return {Direction.UP: 1, Direction.DOWN: -1}.get(direction, 0)


def _tone(change) -> Tone:
    return Tone.POSITIVE if change.favorable is True else Tone.NEUTRAL


def _count_evidence(change, baseline) -> dict:
    evidence = {"current_value": change.current, "baseline_value": change.previous,
                "direction": _direction_code(change.direction),
                "baseline_days": _days(baseline.period_start, baseline.period_end)}
    if change.percent is not None:
        evidence["change_percent"] = change.percent
    return evidence


@dataclass(frozen=True)
class _CountSpec:
    source: str  # "gsc" or "ga4"
    section: str
    name: str
    dimension: str
    metric: str  # key in TotalsComparison.changes and in the totals dicts
    label: str
    fmt: object
    base_key: str
    priority: int


_COUNT_SPECS = (
    _CountSpec("gsc", "search_console", "clicks", "gsc.totals", "clicks", "Organic clicks from Google Search",
               w.format_count, "gsc.clicks", 10),
    _CountSpec("gsc", "search_console", "impressions", "gsc.totals", "impressions", "Search impressions",
               w.format_compact, "gsc.impressions", 20),
    _CountSpec("ga4", "ga4", "sessions", "ga4.site", "sessions", "Website sessions", w.format_count,
               "ga4.sessions", 10),
    _CountSpec("ga4", "ga4", "organic_sessions", "ga4.organic", "sessions", "Organic Search sessions",
               w.format_count, "ga4.organic", 20),
)


def _totals(comparison, spec):
    return comparison.gsc.totals if spec.source == "gsc" else (
        comparison.ga4.site if spec.dimension == "ga4.site" else comparison.ga4.organic)


def _count_outcome(spec: _CountSpec, comparison, metrics):
    """(candidate or None, finding or None) for one totals statement."""
    live = metrics.gsc if spec.source == "gsc" else metrics.ga4
    if live is None or live.current_empty:
        return None, None
    totals = _totals(comparison, spec)
    status = totals.status
    if not status.eligible or status.baseline is None:
        return None, None  # summarized by the "dimensions unusable" finding
    baseline = status.baseline
    change = totals.changes.get(spec.metric)
    if change is None:
        return None, _count_finding(spec, status, None, "data_unavailable", baseline)
    if not baseline.same_length:
        return None, None  # counts are not compared; the overview finding reports the different lengths
    if not change.comparable:
        return None, None  # nothing to say and nothing to explain
    reason = None
    if not live.previous_empty:
        reason = "live_previous_not_empty"
    elif _blocking_caveats(status):
        reason = "caveat_present"
    elif live.incomplete_current:
        reason = "current_period_incomplete"
    elif spec.source == "ga4" and _ga4_time_zone_differs(comparison, baseline.run_id):
        reason = "time_zone_differs"
    elif spec.source == "ga4" and min(totals.current[spec.metric], totals.baseline[spec.metric]) \
            < metrics.thresholds.ga4_min_sessions:
        reason = "insufficient_sample"
    if reason is not None:
        return None, _count_finding(spec, status, change, reason, baseline)
    phrase = w.change_phrase(change, spec.fmt)
    if phrase is None:
        return None, None
    period = _range(baseline.period_start, baseline.period_end)
    text = w.ensure_sentence(f"{spec.label} were {phrase} in the report for {period}")
    candidate = ClientHistoryCandidate(
        section=spec.section, key=f"history.{spec.source}.{spec.name}", dimension=spec.dimension, text=text,
        tone=_tone(change), priority=spec.priority, evidence=_count_evidence(change, baseline),
        baseline_period_start=baseline.period_start, baseline_period_end=baseline.period_end,
        base_keys=(spec.base_key,))
    return candidate, None


def _ga4_time_zone_differs(comparison, run_id: str) -> bool:
    message = f"The GA4 reporting time zone differs in baseline run {run_id}."
    return any(note.message == message for note in comparison.notes)


def _count_finding(spec, status, change, reason, baseline):
    evidence = _count_evidence(change, baseline) if change is not None and change.comparable else {}
    detail = ""
    if change is not None and change.comparable:
        phrase = w.change_phrase(change, spec.fmt)
        detail = f" {spec.label} were {phrase} in the report for {_range(baseline.period_start, baseline.period_end)}."
    message = f"Historical comparison for {spec.dimension} is reviewer-only: {BLOCK_REASONS[reason]}.{detail}"
    return ReviewerHistoryFinding(
        section=spec.section, key=f"history.review.{spec.source}_{spec.name}", dimension=spec.dimension,
        kind=f"{spec.source}_historical_change", message=message, priority=100 + spec.priority, evidence=evidence,
        blocked_reason=reason, baseline_period_start=baseline.period_start, baseline_period_end=baseline.period_end,
        baseline_run_id=baseline.run_id)


# --- geography (Rule B) ------------------------------------------------------------------------------------------------------------------

def _geography(comparison, metrics, target_country):
    """(candidate or None, finding or None) for the target-country share."""
    live = metrics.gsc
    name = " ".join(str(target_country or "").split())
    if live is None or live.current_empty or not name:
        return None, None
    totals_status = comparison.gsc.totals.status
    country_status = comparison.gsc.countries.status
    if not (totals_status.eligible and country_status.eligible):
        return None, None
    baseline = totals_status.baseline
    if country_status.baseline is None or baseline is None:
        return None, None
    if baseline.run_id != country_status.baseline.run_id:
        return None, _geo_finding(None, baseline, "different_baselines", {})
    share = comparison.gsc.target_country_share
    totals = comparison.gsc.totals
    if share is None or share.current is None or share.previous is None or not totals.current or not totals.baseline:
        return None, None  # no target-country data in one of the periods: nothing to state or explain
    cur_total, base_total = totals.current.get("impressions"), totals.baseline.get("impressions")
    if not cur_total or not base_total:
        return None, None
    difference = share.current - share.previous
    evidence = {"share_current": share.current, "share_baseline": share.previous, "share_change": difference,
                "direction": 1 if difference > 0 else -1 if difference < 0 else 0,
                "impressions_current": cur_total, "impressions_baseline": base_total,
                "target_impressions_current": round(share.current * cur_total),
                "target_impressions_baseline": round(share.previous * base_total),
                "baseline_days": _days(baseline.period_start, baseline.period_end)}
    if min(cur_total, base_total) < GEO_MIN_IMPRESSIONS:
        return None, _geo_finding(name, baseline, "impressions_below_minimum", evidence)
    if round(abs(difference), 10) < GEO_MIN_SHIFT:
        return None, _geo_finding(name, baseline, "shift_below_minimum", evidence)
    if _blocking_caveats(totals_status) or _blocking_caveats(country_status):
        return None, _geo_finding(name, baseline, "caveat_present", evidence)
    if live.incomplete_current:
        return None, _geo_finding(name, baseline, "current_period_incomplete", evidence)
    word = "up" if difference > 0 else "down"
    text = w.ensure_sentence(
        f"{name} accounted for {w.format_rate(share.current)} of search impressions, {word} from "
        f"{w.format_rate(share.previous)} in the report for {_range(baseline.period_start, baseline.period_end)}, "
        f"a change of {w.format_points(difference)} percentage points")
    candidate = ClientHistoryCandidate(
        section="geography", key="history.geo.target_share", dimension="gsc.countries", text=text, tone=Tone.NEUTRAL,
        priority=10, evidence=evidence, baseline_period_start=baseline.period_start,
        baseline_period_end=baseline.period_end, base_keys=("geo.target_market",))
    return candidate, None


def _geo_finding(name, baseline, reason, evidence):
    evidence = dict(evidence)
    if reason in ("impressions_below_minimum",):
        evidence["min_impressions"] = GEO_MIN_IMPRESSIONS
    if reason == "shift_below_minimum":
        evidence["min_share_change"] = GEO_MIN_SHIFT
    message = f"The target-country share comparison is reviewer-only: {BLOCK_REASONS[reason]}."
    return ReviewerHistoryFinding(
        section="geography", key="history.review.geo_target_share", dimension="gsc.countries",
        kind="geography_share_change", message=message, priority=110, evidence=evidence, blocked_reason=reason,
        baseline_period_start=baseline.period_start, baseline_period_end=baseline.period_end,
        baseline_run_id=baseline.run_id)


# --- reviewer-only findings -------------------------------------------------------------------------------------------------------------------

def _finding(kind, key, message, evidence, priority, *, section="history", dimension=None, baseline=None):
    return ReviewerHistoryFinding(
        section=section, key=f"history.review.{key}", kind=kind, dimension=dimension, message=message,
        priority=priority, evidence=evidence,
        baseline_period_start=baseline.period_start if baseline else None,
        baseline_period_end=baseline.period_end if baseline else None,
        baseline_run_id=baseline.run_id if baseline else None)


def _ranking_findings(comparison, crosscheck) -> list:
    if crosscheck is None:
        return []
    baseline = comparison.status[el.RANKINGS].baseline
    findings = []
    pending = tuple(getattr(crosscheck, "pending_fallbacks", ()) or ())
    if pending:
        improved = sum(1 for p in pending if p.movement > 0)
        declined = sum(1 for p in pending if p.movement < 0)
        period = _range(pending[0].baseline_period_start, pending[0].baseline_period_end)
        findings.append(_finding(
            "ranking_pending_fallback", "ranking_pending_fallback",
            f"{w.count_noun(len(pending), 'keyword')} {'has' if len(pending) == 1 else 'have'} a blank Previous "
            f"Ranking and a numeric ranking in the report for {period}. The sheet values were not changed and no "
            "marker or footnote was created.",
            {"pending_fallbacks": len(pending), "fallback_improved": improved, "fallback_declined": declined,
             "fallback_unchanged": len(pending) - improved - declined},
            200, section="keywords", dimension=el.RANKINGS, baseline=baseline))
    counts = (
        ("sheet_previous_mismatch", "sheet_mismatches", "ranking_sheet_mismatch", 210,
         "keyword(s) have a sheet Previous Ranking that differs from the Current Ranking in the baseline report"),
        ("new_keywords", "new_keywords", "ranking_new_keywords", 220,
         "keyword(s) were not tracked in the baseline report; no movement was inferred"),
        ("no_longer_tracked", "lost_keywords", "ranking_lost_keywords", 230,
         "baseline keyword(s) are no longer tracked; no movement was inferred"),
        ("not_comparable", "not_comparable_keywords", "ranking_not_comparable", 240,
         "keyword(s) could not be compared (missing, 'Above 10' or duplicate rows); no movement was inferred"))
    for attribute, evidence_key, kind, priority, phrase in counts:
        count = getattr(crosscheck, attribute, None)
        if count:
            findings.append(_finding(kind, kind, f"{w.format_count(count)} {phrase}.", {evidence_key: count},
                                     priority, section="keywords", dimension=el.RANKINGS, baseline=baseline))
    return findings


def _overview_findings(comparison) -> list:
    statuses = [comparison.status[d] for d in el.DIMENSIONS]
    with_baseline = [s for s in statuses if s.eligible and s.baseline is not None]
    if not with_baseline:
        return []
    findings = []
    runs = sorted({s.baseline.run_id for s in with_baseline})
    if len(runs) > 1:
        findings.append(_finding("baselines_differ", "baselines_differ",
                                 f"Different dimensions use {len(runs)} different baseline reports.",
                                 {"baseline_runs": len(runs)}, 300))
    unusable = [s for s in statuses if not s.eligible and not s.reasons[0].startswith("the current report's data")]
    if unusable:
        findings.append(_finding("dimensions_unusable", "dimensions_unusable",
                                 f"{w.count_noun(len(unusable), 'dimension')} had no usable baseline although the "
                                 "current data was usable.", {"ineligible_dimensions": len(unusable)}, 310))
    caveated = [s for s in with_baseline if _blocking_caveats(s)]
    if caveated:
        findings.append(_finding("incomplete_comparison_data", "incomplete_comparison_data",
                                 f"{w.count_noun(len(caveated), 'compared dimension')} carry a caveat, so "
                                 "they were not described as complete or exact.",
                                 {"caveated_dimensions": len(caveated)}, 320))
    current_days = _days(*comparison.current_period)
    seen = set()
    for status in with_baseline:
        ref = status.baseline
        if not ref.same_length and ref.run_id not in seen:
            seen.add(ref.run_id)
            findings.append(_finding(
                "different_period_length", f"different_period_length.{len(seen)}",
                "The baseline covers a different number of days, so counts were not compared.",
                {"current_days": current_days, "baseline_days": _days(ref.period_start, ref.period_end)}, 330,
                baseline=ref))
    return findings


def _discrepancy_findings(comparison) -> list:
    findings = []
    diagnostics = comparison.diagnostics or {}
    sources = [("current", None, diagnostics.get("current"))]
    refs = {s.baseline.run_id: s.baseline for s in comparison.status.values() if s.baseline is not None}
    for position, run_id in enumerate(sorted((diagnostics.get("baselines") or {})), start=1):
        sources.append((f"baseline_{position}", refs.get(run_id), diagnostics["baselines"][run_id]))
    for suffix, ref, group in sources:
        diff = ((group or {}).get("ga4", {}).get("current", {}) or {}).get("daily_sessions_diff")
        if diff:
            findings.append(_finding(
                "ga4_daily_discrepancy", f"ga4_daily_discrepancy.{suffix}",
                "GA4 daily sessions differ from the total sessions. The difference was preserved and not "
                "reconciled, and totals, not daily sums, were compared.",
                {"daily_sessions_diff": diff}, 400, section="ga4", baseline=ref))
    return findings


# --- the public interface ----------------------------------------------------------------------------------------------------------------------------

def evaluate_history_rules(*, comparison, metrics, base_insights=(), crosscheck=None,
                           target_country=None) -> HistoryRuleResult:
    """Evaluate the historical rules. Pure and deterministic; the inputs are not changed.

    `comparison` is the Stage 6b `HistoricalComparison` (or None), `metrics` the live `ReportMetrics`,
    `base_insights` the base `ReportInsights` (or an iterable of `Insight`) used by the duplicate guard,
    `crosscheck` the Stage 6c-3 `RankingCrosscheck` (or None) and `target_country` the client's target country.
    """
    if comparison is None:
        return HistoryRuleResult(notes=(ReviewerNote(
            "info", "history", "suppressed",
            "No historical comparison was available, so no historical insights were evaluated."),))
    notes = []
    candidates, findings, failures = [], [], []
    base = _base_list(base_insights)

    built = []
    for spec in _COUNT_SPECS:
        candidate, finding = _count_outcome(spec, comparison, metrics)
        built.append((candidate, finding))
    built.append(_geography(comparison, metrics, target_country))
    for candidate, finding in built:
        if finding is not None:
            findings.append(finding)
        if candidate is None:
            continue
        reasons = validate_client_candidate(candidate, base)
        if reasons:
            failures += [HistoryValidationFailure(candidate.key, reason) for reason in reasons]
            notes.append(ReviewerNote("warning", "history", "suppressed",
                                      f"Historical statement {candidate.key} was left out: "
                                      f"{VALIDATION_REASONS[reasons[0]]}."))
        else:
            candidates.append(candidate)

    findings += _ranking_findings(comparison, crosscheck)
    findings += _overview_findings(comparison)
    findings += _discrepancy_findings(comparison)

    if not any(s.eligible and s.baseline is not None for s in comparison.status.values()):
        notes.append(ReviewerNote("info", "history", "suppressed",
                                  "No dimension had an eligible baseline, so no historical insights were produced."))

    valid = []
    for finding in findings:
        problems = validate_evidence(finding.evidence, audience=AUDIENCE_REVIEWER)
        if problems or finding.kind not in FINDING_KINDS or finding.section not in REVIEWER_SECTIONS \
                or finding.blocked_reason not in (None, *BLOCK_REASONS):
            failures.append(HistoryValidationFailure(finding.key, "invalid_evidence" if problems else "invalid_identity"))
        else:
            valid.append(finding)

    order = {section: index for index, section in enumerate(SECTIONS)}
    candidates.sort(key=lambda c: (order[c.section], c.priority, c.key))
    valid.sort(key=lambda f: (f.priority, f.key))
    return HistoryRuleResult(tuple(candidates), tuple(valid), tuple(dict.fromkeys(notes)), tuple(failures))
