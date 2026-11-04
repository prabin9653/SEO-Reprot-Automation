"""The insight engine: turns validated report data into client-facing text and reviewer notes.

Pipeline: build metrics -> run the rules -> validate every insight against the metrics ->
generate recommendations -> compose section paragraphs. Nothing here fetches data or reads
the clock. Insights that fail validation are left out and reported as reviewer notes, so an
unverified claim never reaches the client-facing output.

Client-facing output is `ReportInsights.client_texts`: the section paragraphs plus the
recommendation titles and actions. Reviewer notes, triggers, evidence and diagnostics are
kept separate and are never written into paragraphs.
"""

import math
import re
from dataclasses import dataclass

from seo_report import metrics as mt
from seo_report.insights import rules as rl
from seo_report.insights import wording as w
from seo_report.insights.recommendations import (
    Recommendation,
    RecommendationConfig,
    generate_recommendations,
    untraced_recommendation_numbers,
)
from seo_report.insights.rules import Insight, ReviewerNote, Tone
from seo_report.metrics import ReportMetrics, Thresholds
from seo_report.periods import PERIOD_DAYS

SECTION_TITLES = {
    "keywords": "Keyword Rankings",
    "search_console": "Google Search Console",
    "geography": "Geographical Performance",
    "ga4": "Google Analytics 4",
}
MAX_PARAGRAPHS = 2  # strengths and facts first, opportunities second
MAX_SENTENCES = 5  # per paragraph
MAX_WORDS = 130  # per paragraph


@dataclass(frozen=True)
class SectionInsights:
    key: str
    title: str
    paragraphs: tuple[str, ...]
    insights: tuple[Insight, ...]  # the insights behind the paragraphs, in display order
    omitted: tuple[str, ...]  # insight keys left out by the paragraph limits
    status: str  # "ok" when there is text, otherwise "unavailable"
    has_strength: bool  # at least one verified positive statement is included


@dataclass(frozen=True)
class ReportInsights:
    sections: tuple[SectionInsights, ...]
    recommendations: tuple[Recommendation, ...]
    omitted_recommendations: tuple[str, ...]
    notes: tuple[ReviewerNote, ...]  # for the reviewer only
    validation_failures: tuple[tuple[str, str], ...]  # (insight or recommendation key, reason)
    diagnostics: dict  # source -> period -> sum-minus-total differences, never reconciled
    metrics: ReportMetrics

    def section(self, key: str) -> SectionInsights:
        return next(s for s in self.sections if s.key == key)

    @property
    def insights(self) -> tuple[Insight, ...]:
        return tuple(i for s in self.sections for i in s.insights)

    @property
    def client_texts(self) -> tuple[str, ...]:
        """Everything meant for the client: paragraphs and recommendation titles and actions."""
        texts = [p for s in self.sections for p in s.paragraphs]
        for rec in self.recommendations:
            texts += [rec.title, rec.action]
        return tuple(texts)


# --- authoritative evidence ---------------------------------------------------------------------
#
# Each entry rebuilds, from ReportMetrics alone, the figures an insight must carry. An insight is
# accepted only if its evidence matches, so a number can't be attached to the wrong metric, the
# wrong period or the wrong source. Returns (required, optional) or None when the source is absent.

def _evidence_of(change: mt.Change):
    return change.evidence(), {}


def _gsc(fn):
    return lambda m: fn(m.gsc, m) if m.gsc is not None else None


def _ga4(fn):
    return lambda m: fn(m.ga4, m) if m.ga4 is not None else None


def _keywords_up(m):
    r = m.rankings
    if not r.movements or r.movements[0].change <= 0:
        return None
    lead = r.movements[0]
    return {"improved": r.improved, "previous": lead.previous, "current": lead.current, "change": lead.change}, {}


def _keywords_down(m):
    r = m.rankings
    if not r.movements or r.movements[-1].change >= 0:
        return None
    lead = r.movements[-1]
    return {"declined": r.declined, "previous": lead.previous, "current": lead.current, "change": lead.change}, {}


def _top_page(g, m):
    page = next((p for p in g.top_pages if p.label is not None and p.clicks > 0), None)
    return None if page is None else ({"clicks": page.clicks, "impressions": page.impressions}, {})


def _target_market(g, m):
    t = g.target_country
    if t is None or t.found is not True:
        return None
    return {"share": t.impressions_share, "impressions": t.impressions, "clicks": t.clicks}, {}


def _ga4_top_pages(a, m):
    pages = [p for p in a.top_organic_pages if p.label is not None][:rl.MAX_NAMES]
    return {f"sessions_{i}": p.sessions for i, p in enumerate(pages)}, {}


def _ga4_key_events(a, m):
    ke = a.key_events
    if ke.status == "none_recorded":
        return {"period_days": PERIOD_DAYS}, {}
    if ke.status == "recorded" and ke.current is not None:
        return mt.compare_count(ke.current, ke.previous, m.thresholds).evidence(), {}
    return None


AUTHORITY = {
    "keywords.top_positions": lambda m: ({"at_1": m.rankings.at_1, "top_3": m.rankings.top_3,
                                          "page_1": m.rankings.page_1, "total": m.rankings.total}, {}),
    "keywords.moved_up": _keywords_up,
    "keywords.moved_down": _keywords_down,
    "keywords.steady": lambda m: (({"comparable": m.rankings.comparable}, {})
                                  if not m.rankings.improved and not m.rankings.declined else None),
    "keywords.above_10": lambda m: ({"above_10": m.rankings.above_10_current, "total": m.rankings.total}, {}),
    "gsc.clicks": _gsc(lambda g, m: _evidence_of(g.clicks)),
    "gsc.impressions": _gsc(lambda g, m: _evidence_of(g.impressions)),
    "gsc.ctr": _gsc(lambda g, m: _evidence_of(g.ctr)),
    "gsc.position": _gsc(lambda g, m: _evidence_of(g.position)),
    "gsc.position_opportunity": _gsc(lambda g, m: ({"position": g.position.current}, {})),
    "gsc.new_queries": _gsc(lambda g, m: ({"new_queries": g.new_queries_count}, {})),
    "gsc.striking_distance": _gsc(lambda g, m: ({"striking_queries": g.striking_queries_count,
                                                 "position_min": m.thresholds.striking_min_position,
                                                 "position_max": m.thresholds.striking_max_position}, {})),
    "gsc.zero_click_queries": _gsc(lambda g, m: ({"zero_click_queries": g.zero_click_queries_count,
                                                  "min_impressions": m.thresholds.zero_click_min_impressions}, {})),
    "gsc.zero_click_pages": _gsc(lambda g, m: ({"zero_click_pages": g.zero_click_pages_count,
                                                "min_impressions": m.thresholds.zero_click_min_impressions}, {})),
    "gsc.top_page": _gsc(_top_page),
    "geo.target_market": _gsc(_target_market),
    "geo.other_countries": _gsc(lambda g, m: ({}, {"countries": g.country_count.current})),
    "ga4.sessions": _ga4(lambda a, m: _evidence_of(a.sessions)),
    "ga4.users": _ga4(lambda a, m: _evidence_of(a.total_users)),
    "ga4.organic": _ga4(lambda a, m: _evidence_of(a.organic_sessions)),
    "ga4.organic_share": _ga4(lambda a, m: _evidence_of(a.organic_share)),
    "ga4.engagement": _ga4(lambda a, m: _evidence_of(a.engagement_rate)),
    "ga4.organic_engagement": _ga4(lambda a, m: _evidence_of(a.organic_engagement_rate)),
    "ga4.engagement_gap": _ga4(lambda a, m: ({"organic_rate": a.engagement_gap.organic_rate,
                                              "site_rate": a.engagement_gap.site_rate}, {})),
    "ga4.concentration": _ga4(lambda a, m: (None if a.organic_concentration is None else
                                            ({"top_sessions": a.organic_concentration.top_sessions,
                                              "organic_sessions": a.organic_concentration.organic_sessions}, {}))),
    "ga4.top_pages": _ga4(_ga4_top_pages),
    "ga4.key_events": _ga4(_ga4_key_events),
}

SECTION_PREFIX = {"keywords": "keywords", "search_console": "gsc", "geography": "geo", "ga4": "ga4"}

# Insights that compare with the previous period: the word the text must use for each direction.
COMPARATIVE = {"gsc.clicks", "gsc.impressions", "gsc.ctr", "ga4.sessions", "ga4.users", "ga4.organic",
               "ga4.organic_share", "ga4.engagement", "ga4.organic_engagement", "ga4.key_events"}
POSITION_KEY = "gsc.position"  # a lower number is better

# Terms that would show one source's figures being described as another's.
FOREIGN_TERMS = {
    "keywords": ("search console", "average search position", "impressions", "sessions", "total users"),
    "search_console": ("tracked keyword", "sessions", "engagement", "total users"),
    "geography": ("tracked keyword", "sessions", "engagement", "total users"),
    "ga4": ("search console", "impressions", "average search position", "click-through", "tracked keyword"),
}

_QUOTED = re.compile(r"“[^”]*”")
_PERIOD_PHRASES = re.compile(r"(?:(\d+)-day period|previous (\d+) days)")


def _close(actual, expected) -> bool:
    if isinstance(expected, float) or isinstance(actual, float):
        try:
            return math.isclose(float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-12)
        except (TypeError, ValueError):
            return False
    return actual == expected


def _direction_words(text: str) -> dict:
    return {"up": bool(re.search(r"\bup\b", text)), "down": bool(re.search(r"\bdown\b", text)),
            "unchanged": "unchanged" in text, "improved": "improved from" in text,
            "compared": "compared with" in text}


def validate_insight(insight: Insight, metrics: ReportMetrics) -> str | None:
    """Return why an insight cannot be trusted, or None when it checks out."""
    prefix = SECTION_PREFIX.get(insight.section)
    if prefix is None or not insight.key.startswith(prefix + "."):
        return "its key does not belong to its section"
    authority = AUTHORITY.get(insight.key)
    if authority is None:
        return "no authoritative evidence is registered for this insight"
    expected = authority(metrics)
    if expected is None:
        return "the underlying data is not available"
    required, optional = expected
    evidence = insight.evidence
    if any(name.endswith("_diff") for name in evidence):
        return "diagnostic differences must not be used as evidence"
    for name, value in required.items():
        if name not in evidence or not _close(evidence[name], value):
            return f"evidence {name!r} does not match the metrics"
    for name, value in optional.items():
        if name in evidence and not _close(evidence[name], value):
            return f"evidence {name!r} does not match the metrics"
    text = _QUOTED.sub(" ", insight.text)
    lowered = text.lower()
    for phrase in FOREIGN_TERMS[insight.section]:
        if phrase in lowered:
            return f"the text mixes in another source ({phrase!r})"
    for match in _PERIOD_PHRASES.finditer(text):
        if int(match.group(1) or match.group(2)) != PERIOD_DAYS:
            return "the text names a comparison period other than the approved 28 days"
    if "period_days" in evidence and evidence["period_days"] != PERIOD_DAYS:
        return "the evidence names a comparison period other than the approved 28 days"

    no_comparison = insight.key == "ga4.key_events" and "direction" not in evidence  # "none recorded" wording
    if (insight.key in COMPARATIVE or insight.key == POSITION_KEY) and not no_comparison:
        direction = evidence.get("direction")
        words = _direction_words(lowered)
        if "previous" in lowered:
            if direction in (None, "not_comparable") or evidence.get("previous") is None:
                return "the text compares with the previous period but the metric is not comparable"
            if insight.key == POSITION_KEY:
                ok = {"down": words["improved"], "up": words["compared"], "flat": words["unchanged"]}.get(direction)
            else:
                ok = {"up": words["up"] and not words["down"], "down": words["down"] and not words["up"],
                      "flat": words["unchanged"] and not (words["up"] or words["down"])}.get(direction)
            if not ok:
                return f"the wording does not match the metric's direction ({direction})"
        elif words["up"] or words["down"] or words["unchanged"] or words["improved"]:
            return "the text describes a change that its evidence does not support"
        if insight.tone is Tone.POSITIVE:
            favorable = "down" if insight.key == POSITION_KEY else "up"
            if direction != favorable:
                return "a positive statement is not supported by the direction of the metric"
    if insight.key == "geo.target_market" and insight.tone is Tone.POSITIVE:
        if evidence["share"] < rl.TARGET_MARKET_SHARE:
            return "a positive statement is not supported by the target-market share"
    if insight.key == "ga4.engagement_gap" and insight.tone is Tone.POSITIVE:
        if not evidence["organic_rate"] > evidence["site_rate"]:
            return "a positive statement is not supported by the engagement rates"
    if rl.untraced_numbers(insight):  # last, so the more specific reasons above are reported first
        return f"numbers {rl.untraced_numbers(insight)} are not traceable to its evidence"
    return None


# --- composition ------------------------------------------------------------------------------------

def _fill(candidates):
    """Take insights in priority order until the paragraph limits are reached."""
    chosen, omitted, words = [], [], 0
    for insight in candidates:
        n = w.word_count(insight.text)
        if chosen and (len(chosen) >= MAX_SENTENCES or words + n > MAX_WORDS):
            omitted.append(insight.key)
            continue
        chosen.append(insight)
        words += n
    return chosen, omitted


def _compose(key: str, insights) -> SectionInsights:
    ordered = sorted(insights, key=lambda i: (i.priority, i.key))
    facts, omitted_facts = _fill([i for i in ordered if i.tone is not Tone.OPPORTUNITY])
    opportunities, omitted_opps = _fill([i for i in ordered if i.tone is Tone.OPPORTUNITY])
    facts.sort(key=lambda i: (i.tone is not Tone.POSITIVE, i.priority, i.key))  # verified strengths first
    paragraphs = tuple(w.join_sentences(i.text for i in group) for group in (facts, opportunities) if group)
    used = tuple(facts + opportunities)
    return SectionInsights(
        key=key, title=SECTION_TITLES[key], paragraphs=paragraphs, insights=used,
        omitted=tuple(omitted_facts + omitted_opps), status="ok" if paragraphs else "unavailable",
        has_strength=any(i.tone is Tone.POSITIVE for i in used))


def _diagnostics(metrics: ReportMetrics) -> dict:
    result = {}
    if metrics.gsc is not None:
        result["gsc"] = {"current": dict(metrics.gsc.diagnostics_current),
                         "previous": dict(metrics.gsc.diagnostics_previous)}
    if metrics.ga4 is not None:
        result["ga4"] = {"current": dict(metrics.ga4.diagnostics_current),
                         "previous": dict(metrics.ga4.diagnostics_previous)}
    return result


def generate_insights(*, gsc=None, ga4=None, keywords=(), target_country=None,
                      thresholds: Thresholds = Thresholds(),
                      recommendation_config: RecommendationConfig = RecommendationConfig()) -> ReportInsights:
    """Build the insights, recommendations and reviewer notes for one report.

    `gsc` and `ga4` are the fetched Search Console and GA4 data (either may be None), and
    `keywords` are the sheet's keyword rows, used both for the ranking summary and for the
    local SEO recommendation's target locations.
    """
    keywords = tuple(keywords)
    metrics = mt.build_report_metrics(gsc=gsc, ga4=ga4, keywords=keywords, target_country=target_country,
                                      thresholds=thresholds)
    output = rl.run_rules(metrics)
    notes = list(output.notes)
    failures, valid = [], []
    for insight in output.insights:
        reason = validate_insight(insight, metrics)
        if reason is None:
            valid.append(insight)
        else:
            failures.append((insight.key, reason))
            notes.append(ReviewerNote("warning", "engine", "suppressed",
                                      f"Insight {insight.key} was left out: {reason}."))

    result = generate_recommendations(valid, metrics, recommendation_config, keywords)
    notes += result.notes
    recommendations = []
    for rec in result.recommendations:
        if untraced_recommendation_numbers(rec):
            failures.append((rec.key, "numbers are not traceable to its evidence"))
            notes.append(ReviewerNote("warning", "engine", "suppressed",
                                      f"Recommendation {rec.key} was left out: numbers are not traceable to its "
                                      "evidence."))
        else:
            recommendations.append(rec)

    sections = []
    for key in SECTION_TITLES:
        section = _compose(key, [i for i in valid if i.section == key])
        sections.append(section)
        if section.omitted:
            notes.append(ReviewerNote("info", "engine", "suppressed",
                                      f"{section.title}: left out by the paragraph limits: "
                                      f"{', '.join(section.omitted)}."))
        if section.status == "unavailable":
            notes.append(ReviewerNote("info", "engine", "suppressed",
                                      f"{section.title}: no verified insights, so the section has no text."))

    return ReportInsights(
        sections=tuple(sections), recommendations=tuple(recommendations),
        omitted_recommendations=result.omitted, notes=tuple(dict.fromkeys(notes)),
        validation_failures=tuple(failures), diagnostics=_diagnostics(metrics), metrics=metrics)
