"""Reviewer-only historical annotations for the base recommendations.

`annotate_recommendations` looks at the existing base recommendations and says, for each one, whether the
historical comparison supports it, qualifies it, or offers no usable context. It never creates, removes, rewords,
reorders or re-ranks a recommendation, and nothing here is client-facing: the result is a separate structure meant
for the reviewer record (`recommendation_annotations` of the finalized record).

Matching is a fixed table of recommendation key -> historical relations (RELATIONS). Nothing is matched by
similarity or wording. A recommendation that is not in the table, or whose relation cannot be established, gets an
annotation with an empty `history_support`, which means "no usable historical context".

  accelerate_positions   Search Console average position   (gsc.totals, position)
  improve_ctr            Search Console click-through rate  (gsc.totals, ctr)
  improve_engagement     GA4 site-wide engagement rate      (ga4.site, engagement_rate)
  local_seo              the recommendation's own keywords: each keyword that held a page-1 position (1 to 10) in
                         the baseline report and is labelled "Above 10" now (rankings.keywords)

Every other base recommendation (review_declining_keywords, broaden_landing_pages, review_key_events,
standing.backlinks, standing.technical) has no relation that can be established from the stored comparison, so it
never receives support. Evidence for another source, another dimension or another keyword is never attached.

Classification (the `classification` of a support item)
  supports    the compared figure moved against the recommendation's concern (it got worse), or a local-SEO keyword
              fell from page 1 to "Above 10"
  qualifies   the compared figure moved the other way (it already improved), so the case for the recommendation is
              weaker than the current period alone suggests
  An annotation whose `history_support` is empty has no usable historical context. A figure that did not change
  meaningfully (`favorable is None`) gives no support item either.

Support is used only when: the comparison exists; the dimension is eligible with a named baseline; the change is
comparable; no invalidating caveat applies (the Stage 6c-4 convention); and Stage 6c-4 did not block the dimension
for a data-quality reason (a reviewer finding with `blocked_reason` caveat_present, current_period_incomplete,
time_zone_differs, insufficient_sample, data_unavailable or not_comparable).

Exact `history_support` item (closed; every field required):

  classification   "supports" | "qualifies"
  source           "gsc_position" | "gsc_ctr" | "ga4_engagement_rate" | "ranking_keyword"
  subject          the keyword for "ranking_keyword", otherwise null
  dimension        a member of eligibility.DIMENSIONS
  baseline_run_id  the baseline report the figures come from
  baseline_period  {"start": ISO date, "end": ISO date}
  evidence         flat object: registered history evidence keys -> finite numbers
  rules_version    the Stage 6c-4 rules version

Nothing is recalculated: figures come from the Stage 6b `Change` objects and keyword comparisons as they are. The
module is pure: no clock, files, database or network, and its inputs are not changed.
"""

import datetime as dt
import re
from dataclasses import dataclass

from seo_report.insights import history_rules as hr
from seo_report.metrics import Direction
from seo_report.rankings import RESULTS_PER_PAGE
from seo_report.snapshots import eligibility as el

CLASSIFICATIONS = ("supports", "qualifies")
SOURCES = ("gsc_position", "gsc_ctr", "ga4_engagement_rate", "ranking_keyword")
SUPPORT_FIELDS = ("classification", "source", "subject", "dimension", "baseline_run_id", "baseline_period",
                  "evidence", "rules_version")
MAX_KEYWORD_ITEMS = 5
MAX_SUBJECT_CHARS = 300
# Stage 6c-4 block reasons that mean the compared data itself is not trustworthy (not merely not client-ready).
QUALITY_BLOCKS = frozenset({"caveat_present", "current_period_incomplete", "time_zone_differs", "insufficient_sample",
                            "data_unavailable", "not_comparable"})
STATUS_SUPPORTS = "supports"
STATUS_QUALIFIES = "qualifies"
STATUS_NONE = "no_usable_support"


@dataclass(frozen=True)
class _Relation:
    source: str
    dimension: str
    metric: str | None  # key in TotalsComparison.changes; None for the keyword relation


RELATIONS = {
    "accelerate_positions": (_Relation("gsc_position", "gsc.totals", "position"),),
    "improve_ctr": (_Relation("gsc_ctr", "gsc.totals", "ctr"),),
    "improve_engagement": (_Relation("ga4_engagement_rate", "ga4.site", "engagement_rate"),),
    "local_seo": (_Relation("ranking_keyword", el.RANKINGS, None),),
}


@dataclass(frozen=True)
class HistorySupport:
    classification: str
    source: str
    subject: str | None
    dimension: str
    baseline_run_id: str
    baseline_period_start: str
    baseline_period_end: str
    evidence: dict
    rules_version: str = hr.RULES_VERSION

    def as_record(self) -> dict:
        """The item exactly as it appears in `recommendation_annotations[].history_support`."""
        return {"classification": self.classification, "source": self.source, "subject": self.subject,
                "dimension": self.dimension, "baseline_run_id": self.baseline_run_id,
                "baseline_period": {"start": self.baseline_period_start, "end": self.baseline_period_end},
                "evidence": dict(self.evidence), "rules_version": self.rules_version}


@dataclass(frozen=True)
class RecommendationAnnotation:
    recommendation_key: str
    history_support: tuple[HistorySupport, ...] = ()

    @property
    def status(self) -> str:
        if not self.history_support:
            return STATUS_NONE
        return STATUS_QUALIFIES if any(s.classification == "qualifies" for s in self.history_support) \
            else STATUS_SUPPORTS

    def as_record(self) -> dict:
        """The entry exactly as it appears in the finalized record's `recommendation_annotations`."""
        return {"recommendation_key": self.recommendation_key,
                "history_support": [item.as_record() for item in self.history_support]}


@dataclass(frozen=True)
class RecommendationHistoryAnnotations:
    """One annotation per base recommendation, in the recommendations' own order. Reviewer-only."""

    annotations: tuple[RecommendationAnnotation, ...] = ()

    def as_record(self) -> list:
        return [annotation.as_record() for annotation in self.annotations]


# --- validation of the item shape ---------------------------------------------------------------------------------------------

def validate_history_support(item) -> list[str]:
    """Problems with a serialized `history_support` item (empty when it has exactly the approved shape)."""
    if not isinstance(item, dict):
        return ["a history_support item must be an object"]
    problems = []
    missing = [name for name in SUPPORT_FIELDS if name not in item]
    extra = sorted(str(name) for name in item if name not in SUPPORT_FIELDS)
    if missing:
        problems.append(f"missing field(s) {missing}")
    if extra:
        problems.append(f"unexpected field(s) {extra}")
    if problems:
        return problems
    if item["classification"] not in CLASSIFICATIONS:
        problems.append("classification must be 'supports' or 'qualifies'")
    if item["source"] not in SOURCES:
        problems.append("source is not a registered historical source")
    subject = item["subject"]
    if item["source"] == "ranking_keyword":
        if not isinstance(subject, str) or not subject.strip() or len(subject) > MAX_SUBJECT_CHARS:
            problems.append("a keyword item needs a non-empty subject")
    elif subject is not None:
        problems.append("subject must be null unless the source is a keyword")
    if item["dimension"] not in el.DIMENSIONS:
        problems.append("dimension is not a known dimension")
    if not isinstance(item["baseline_run_id"], str) or not item["baseline_run_id"]:
        problems.append("baseline_run_id must be a non-empty string")
    period = item["baseline_period"]
    if not isinstance(period, dict) or set(period) != {"start", "end"}:
        problems.append("baseline_period must be {start, end}")
    else:
        try:
            if not all(isinstance(period[k], str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", period[k])
                       for k in ("start", "end")) or \
                    dt.date.fromisoformat(period["start"]) > dt.date.fromisoformat(period["end"]):
                raise ValueError
        except ValueError:
            problems.append("baseline_period must be ISO dates with start not after end")
    problems += hr.validate_evidence(item["evidence"], audience=hr.AUDIENCE_REVIEWER)
    if not isinstance(item["rules_version"], str) or not item["rules_version"]:
        problems.append("rules_version must be non-empty text")
    return problems


# --- matching ---------------------------------------------------------------------------------------------------------------------

def _normalize(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def _blocked_dimensions(history_insights) -> set[str]:
    findings = getattr(history_insights, "reviewer_findings", ()) or ()
    return {f.dimension for f in findings if f.blocked_reason in QUALITY_BLOCKS and f.dimension}


def _usable_status(comparison, dimension: str, blocked: set[str]):
    status = comparison.status.get(dimension)
    if status is None or not status.eligible or status.baseline is None:
        return None
    if dimension in blocked or hr._blocking_caveats(status):
        return None
    return status


def _direction_code(direction: Direction) -> int:
    return {Direction.UP: 1, Direction.DOWN: -1}.get(direction, 0)


def _support(classification, source, subject, dimension, baseline, evidence) -> HistorySupport:
    return HistorySupport(classification, source, subject, dimension, baseline.run_id, baseline.period_start,
                          baseline.period_end, evidence)


def _metric_support(relation: _Relation, comparison, blocked: set[str]) -> list[HistorySupport]:
    status = _usable_status(comparison, relation.dimension, blocked)
    if status is None:
        return []
    totals = comparison.gsc.totals if relation.dimension.startswith("gsc.") else comparison.ga4.site
    change = totals.changes.get(relation.metric)
    if change is None or not change.comparable or change.favorable is None:
        return []
    baseline = status.baseline
    days = (dt.date.fromisoformat(baseline.period_end) - dt.date.fromisoformat(baseline.period_start)).days + 1
    evidence = {"current_value": change.current, "baseline_value": change.previous,
                "direction": _direction_code(change.direction), "baseline_days": days}
    classification = "qualifies" if change.favorable else "supports"
    return [_support(classification, relation.source, None, relation.dimension, baseline, evidence)]


def _local_keywords(recommendation) -> list[str]:
    names = recommendation.evidence.get("local_keywords.keywords", ()) if isinstance(recommendation.evidence, dict) \
        else ()
    return [name for name in names if isinstance(name, str) and name.strip()]


def _keyword_support(recommendation, comparison, blocked: set[str]) -> list[HistorySupport]:
    status = _usable_status(comparison, el.RANKINGS, blocked)
    if status is None:
        return []
    wanted = {}
    for name in _local_keywords(recommendation):
        wanted.setdefault(_normalize(name), name)
    ranking = comparison.rankings
    listed = [*ranking.moved, *ranking.not_comparable, *ranking.new_keywords, *ranking.no_longer_tracked]
    baseline = status.baseline
    days = (dt.date.fromisoformat(baseline.period_end) - dt.date.fromisoformat(baseline.period_start)).days + 1
    found = []
    for key in sorted(wanted):
        matches = [item for item in listed if _normalize(item.keyword) == key]
        if len(matches) != 1:
            continue  # not in Stage 6b's lists, or ambiguous (several locations or rows): no usable relation
        item = matches[0]
        if item.current is None or item.baseline is None or item.current["kind"] != "above_10" \
                or item.baseline["kind"] != "position" or item.baseline["position"] is None \
                or not 1 <= item.baseline["position"] <= RESULTS_PER_PAGE:
            continue
        evidence = {"baseline_value": item.baseline["position"], "baseline_days": days}
        found.append(_support("supports", "ranking_keyword", wanted[key], el.RANKINGS, baseline, evidence))
        if len(found) == MAX_KEYWORD_ITEMS:
            break
    return found


def _support_for(recommendation, comparison, blocked: set[str]) -> tuple[HistorySupport, ...]:
    items = []
    for relation in RELATIONS.get(recommendation.key, ()):
        if relation.metric is None:
            items += _keyword_support(recommendation, comparison, blocked)
        else:
            items += _metric_support(relation, comparison, blocked)
    return tuple(sorted(items, key=lambda s: (s.source, s.subject or "", s.classification)))


# --- the public interface ------------------------------------------------------------------------------------------------------------

def annotate_recommendations(*, recommendations, comparison, history_insights=None) -> RecommendationHistoryAnnotations:
    """Reviewer-only historical annotations, one per base recommendation, in the recommendations' order.

    `recommendations` are the base `Recommendation` objects (identified by their stable `key`; a repeated key is an
    error), `comparison` the Stage 6b `HistoricalComparison` (or None) and `history_insights` the Stage 6c-5
    `HistoryInsights` (or None), used only to honor the dimensions Stage 6c-4 blocked for data-quality reasons.
    """
    recommendations = tuple(recommendations)
    seen = set()
    for recommendation in recommendations:
        key = getattr(recommendation, "key", None)
        if not isinstance(key, str) or not key:
            raise TypeError("every recommendation needs a stable text key")
        if key in seen:
            raise ValueError(f"the recommendation key {key!r} appears more than once")
        seen.add(key)
    if comparison is None:
        return RecommendationHistoryAnnotations(tuple(RecommendationAnnotation(r.key) for r in recommendations))
    blocked = _blocked_dimensions(history_insights)
    return RecommendationHistoryAnnotations(tuple(
        RecommendationAnnotation(r.key, _support_for(r, comparison, blocked)) for r in recommendations))
