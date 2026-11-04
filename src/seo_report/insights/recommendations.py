"""Evidence-driven recommendations, with optional standing recommendations.

Each recommendation is selected only when specific insights (and so specific, validated
evidence) support it, and it records both the trigger and the figures behind it. Insights
from a source whose data is missing, empty or incomplete are not used. Standing
recommendations (backlinks, technical SEO) are never derived from data: they are off unless
a client's configuration turns them on, and they always come after evidence-driven ones.
"""

from dataclasses import dataclass

from seo_report.insights import wording as w
from seo_report.insights.rules import ABOVE_10_LABEL, Insight, ReviewerNote, evidence_tokens
from seo_report.metrics import ReportMetrics, safe_query_label
from seo_report.periods import PERIOD_DAYS
from seo_report.rankings import RankingKind

MAX_RECOMMENDATIONS = 5
STANDING_KEYS = ("backlinks", "technical")
SOURCE_LABELS = {"gsc": "Search Console", "ga4": "GA4"}


@dataclass(frozen=True)
class RecommendationConfig:
    """Per-client settings. Standing recommendations are disabled unless listed.

    `target_city` is only a label for the local SEO action text; it never triggers anything.
    """

    standing: tuple[str, ...] = ()
    target_city: str | None = None
    max_items: int = MAX_RECOMMENDATIONS

    def __post_init__(self):
        unknown = [k for k in self.standing if k not in STANDING_KEYS]
        if unknown:
            raise ValueError(f"unknown standing recommendation(s) {unknown}; choose from {list(STANDING_KEYS)}")
        if not 1 <= self.max_items <= MAX_RECOMMENDATIONS:
            raise ValueError(f"max_items must be between 1 and {MAX_RECOMMENDATIONS}")
        object.__setattr__(self, "standing", tuple(k for k in STANDING_KEYS if k in self.standing))
        city = " ".join(str(self.target_city).split()) if self.target_city else None
        object.__setattr__(self, "target_city", city or None)


@dataclass(frozen=True)
class Recommendation:
    key: str
    title: str
    action: str  # what to do, in practical terms
    trigger: str  # why it was selected
    evidence: dict  # figures behind the trigger, prefixed with the insight key they came from
    supporting: tuple[str, ...]  # keys of the insights that triggered it
    kind: str  # "evidence" or "standing"
    priority: int  # lower comes first


@dataclass(frozen=True)
class RecommendationResult:
    recommendations: tuple[Recommendation, ...]
    omitted: tuple[str, ...]  # keys dropped by the item limit
    notes: tuple[ReviewerNote, ...]


@dataclass(frozen=True)
class _Context:
    insights: dict
    metrics: ReportMetrics
    config: RecommendationConfig
    keywords: tuple


def untraced_recommendation_numbers(rec: Recommendation) -> list[str]:
    """Numbers in a recommendation's text that are not derived from its evidence."""
    allowed = evidence_tokens(rec.evidence)
    return [n for n in w.numbers_in(f"{rec.title} {rec.action} {rec.trigger}") if n not in allowed]


def _make(ctx: _Context, key, title, action, trigger, supporting, priority, extra=None) -> Recommendation:
    evidence = {}
    for insight_key in supporting:
        for name, value in ctx.insights[insight_key].evidence.items():
            evidence[f"{insight_key}.{name}"] = value
    evidence.update(extra or {})
    return Recommendation(key, title, action, w.ensure_sentence(f"Selected because {trigger}"), evidence,
                          tuple(supporting), "evidence", priority)


def _evidence(ctx: _Context, key: str) -> dict | None:
    insight = ctx.insights.get(key)
    return insight.evidence if insight is not None else None


# --- evidence-driven recommendations ---------------------------------------------------------------

def _accelerate_positions(ctx: _Context):
    parts, supporting = [], []
    e = _evidence(ctx, "gsc.position_opportunity")
    if e is not None:
        parts.append(f"the Search Console average position is {w.format_position(e['position'])}")
        supporting.append("gsc.position_opportunity")
    e = _evidence(ctx, "gsc.striking_distance")
    if e is not None:
        n = e["striking_queries"]
        parts.append(f"{w.count_noun(n, 'search query', 'search queries')} {'shows' if n == 1 else 'show'} in positions "
                     f"{w.format_count(e['position_min'])}{w.EN_DASH}{w.format_count(e['position_max'])}")
        supporting.append("gsc.striking_distance")
    e = _evidence(ctx, "keywords.above_10")
    if e is not None:
        n = e["above_10"]
        parts.append(f"{w.count_noun(n, 'tracked keyword')} {'is' if n == 1 else 'are'} labelled "
                     f"{w.quoted(ABOVE_10_LABEL)}")
        supporting.append("keywords.above_10")
    if not parts:
        return None
    return _make(ctx, "accelerate_positions", "Accelerate position improvements",
                 "Focus on-page improvements, internal links, and supporting content on the pages that target the "
                 "keywords and queries closest to the first page of results, and track movement each month to see "
                 "what works.",
                 w.join_list(parts), supporting, 10)


def _improve_ctr(ctx: _Context):
    parts, supporting = [], []
    e = _evidence(ctx, "gsc.zero_click_queries")
    if e is not None:
        n = e["zero_click_queries"]
        parts.append(f"{w.count_noun(n, 'search query', 'search queries')} generated at least "
                     f"{w.format_count(e['min_impressions'])} impressions without a click")
        supporting.append("gsc.zero_click_queries")
    e = _evidence(ctx, "gsc.zero_click_pages")
    if e is not None:
        n = e["zero_click_pages"]
        parts.append(f"{w.count_noun(n, 'page')} appeared at least {w.format_count(e['min_impressions'])} times "
                     "without a click")
        supporting.append("gsc.zero_click_pages")
    e = _evidence(ctx, "gsc.ctr")
    if e is not None and e.get("direction") == "down":
        parts.append(f"the click-through rate fell from {w.format_rate(e['previous'])} to {w.format_rate(e['current'])}")
        supporting.append("gsc.ctr")
    if not parts:
        return None
    return _make(ctx, "improve_ctr", "Refresh titles and meta descriptions",
                 "Rewrite the titles and meta descriptions for the pages and queries that earn impressions without "
                 "clicks, then compare click-through rate next period.",
                 w.join_list(parts), supporting, 20)


def _review_declining_keywords(ctx: _Context):
    e = _evidence(ctx, "keywords.moved_down")
    if e is None:
        return None
    n = e["declined"]
    trigger = (f"{w.count_noun(n, 'tracked keyword')} {'has' if n == 1 else 'have'} moved down since the previous "
               f"ranking check, led by a move from position {e['previous']} to position {e['current']}")
    return _make(ctx, "review_declining_keywords", "Review keywords that moved down",
                 "Check content freshness, internal links, and any recent site changes on the pages that target "
                 "those keywords.", trigger, ["keywords.moved_down"], 30)


def _local_keywords(keywords) -> list[tuple[str, str]]:
    """(keyword, target location) for keywords labelled “Above 10” that have a printable target location.

    Only the sheet's Target Location value is used; no location is inferred from the keyword text.
    """
    found = []
    for item in keywords:
        if item.current.kind is not RankingKind.ABOVE_10:
            continue
        location = safe_query_label(getattr(item, "target_location", None))
        if location:
            found.append((item.keyword, location))
    return found


def _local_status(ctx: _Context) -> tuple[list[tuple[str, str]], str | None]:
    """Qualifying keywords for the local SEO recommendation, or why there are none."""
    e = _evidence(ctx, "keywords.above_10")
    if e is None:
        return [], None
    if not ctx.keywords:
        return [], "the keyword rows with target locations were not provided"
    found = _local_keywords(ctx.keywords)
    if not found:
        return [], f"none of the keywords labelled {w.quoted(ABOVE_10_LABEL)} has a printable target location"
    if len(found) > e["above_10"]:
        return [], "the keyword rows do not match the rankings summary"
    return found, None


def _local_seo(ctx: _Context):
    found, _ = _local_status(ctx)
    if not found:
        return None
    locations = []
    for _, location in found:
        if location.casefold() not in {existing.casefold() for existing in locations}:
            locations.append(location)
    shown = [w.quoted(location) for location in locations[:3]]
    extra = len(locations) - len(shown)
    if extra:
        shown.append(w.count_noun(extra, "other location"))
    listed = w.join_list(shown)
    n = len(found)
    trigger = (f"{w.count_noun(n, 'tracked keyword')} labelled {w.quoted(ABOVE_10_LABEL)} "
               f"{'has a target location' if n == 1 else 'have target locations'}: {listed}")
    city = ctx.config.target_city
    focus = f"{w.quoted(city)}, starting with {listed}" if city else listed
    evidence = {
        "local_keywords.count": n,
        "local_keywords.locations": len(locations),
        "local_keywords.keywords": tuple(name for name, _ in found),
        "local_keywords.location_names": tuple(locations),
    }
    if extra:
        evidence["local_keywords.extra_locations"] = extra
    return _make(ctx, "local_seo", "Prioritize local SEO",
                 f"Strengthen local relevance for {focus}: keep business listings consistent, add "
                 "location-specific content for each area, and encourage customer reviews.",
                 trigger, ["keywords.above_10"], 40, extra=evidence)


def _broaden_landing_pages(ctx: _Context):
    e = _evidence(ctx, "ga4.concentration")
    if e is None:
        return None
    trigger = (f"{e['top_sessions']} of {w.count_noun(e['organic_sessions'], 'Organic Search session')} entered "
               "through a single landing page")
    return _make(ctx, "broaden_landing_pages", "Broaden landing-page coverage",
                 "Build or strengthen additional service and location pages, and link to them from the home page, "
                 "so organic visitors have more than one entry point.", trigger, ["ga4.concentration"], 50)


def _improve_engagement(ctx: _Context):
    parts, supporting = [], []
    e = _evidence(ctx, "ga4.engagement")
    if e is not None and e.get("direction") == "down":
        parts.append(f"the site-wide engagement rate fell from {w.format_rate(e['previous'])} to "
                     f"{w.format_rate(e['current'])}")
        supporting.append("ga4.engagement")
    e = _evidence(ctx, "ga4.engagement_gap")
    if e is not None and e["organic_rate"] < e["site_rate"]:
        parts.append(f"the Organic Search engagement rate ({w.format_rate(e['organic_rate'])}) is below the "
                     f"site-wide rate ({w.format_rate(e['site_rate'])})")
        supporting.append("ga4.engagement_gap")
    if not parts:
        return None
    return _make(ctx, "improve_engagement", "Improve landing-page engagement",
                 "Review the highest-traffic landing pages for a clear next step, relevance to what visitors "
                 "searched for, and mobile page speed.", w.join_list(parts), supporting, 60)


def _review_key_events(ctx: _Context):
    ga4 = ctx.metrics.ga4
    e = _evidence(ctx, "ga4.key_events")
    if e is None or ga4 is None or ga4.key_events.status != "none_recorded":
        return None
    trigger = f"no key events were recorded in either {PERIOD_DAYS}-day period"
    rec = _make(ctx, "review_key_events", "Review key event tracking",
                "Decide which actions, such as calls, form submissions, or quote requests, should count as key "
                "events in GA4, so future reports can show how visits lead to inquiries.",
                trigger, ["ga4.key_events"], 70)
    return rec


BUILDERS = (_accelerate_positions, _improve_ctr, _review_declining_keywords, _local_seo,
            _broaden_landing_pages, _improve_engagement, _review_key_events)


# --- standing recommendations ----------------------------------------------------------------------------

_STANDING = {
    "backlinks": ("Strengthen backlink profile",
                  "Earn relevant links from local directories, suppliers, and community organizations, and review "
                  "existing links for quality.", 100),
    "technical": ("Review technical SEO (mobile and page speed)",
                  "Check mobile usability and page speed on key landing pages, and fix any issues found.", 110),
}


def _standing(key: str) -> Recommendation:
    title, action, priority = _STANDING[key]
    return Recommendation(
        key=f"standing.{key}", title=title, action=action,
        trigger="Included as a standing priority for this client; it is not derived from this period's data.",
        evidence={}, supporting=(), kind="standing", priority=priority)


# --- entry point ---------------------------------------------------------------------------------------------

def _source(insight: Insight) -> str:
    return {"keywords": "rankings", "search_console": "gsc", "geography": "gsc", "ga4": "ga4"}[insight.section]


def _unreliable_sources(metrics: ReportMetrics) -> dict[str, str]:
    reasons = {}
    for source, data in (("gsc", metrics.gsc), ("ga4", metrics.ga4)):
        if data is None:
            reasons[source] = "the data was not provided"
        elif data.current_empty:
            reasons[source] = "the current period has no data"
        elif data.incomplete_current:
            reasons[source] = "the current-period data may be incomplete"
    return reasons


def generate_recommendations(insights, metrics: ReportMetrics,
                             config: RecommendationConfig = RecommendationConfig(),
                             keywords=()) -> RecommendationResult:
    """Choose up to `config.max_items` recommendations from the rules' insights.

    Evidence-driven recommendations come first, in priority order; standing ones (when
    enabled) fill any remaining places. Insights from unreliable sources are ignored.

    `keywords` are the sheet's keyword rows (each with `.keyword`, `.target_location` and
    `.current`). The local SEO recommendation needs them: it requires at least one keyword
    labelled “Above 10” that has a Target Location, and is suppressed otherwise.
    """
    reasons = _unreliable_sources(metrics)
    usable, excluded, notes = {}, set(), []
    for insight in sorted(insights, key=lambda i: i.key):
        source = _source(insight)
        if source in reasons:
            excluded.add(source)
        else:
            usable[insight.key] = insight
    for source in sorted(excluded):
        notes.append(ReviewerNote(
            "warning", source, "suppressed",
            f"Recommendations based on {SOURCE_LABELS[source]} evidence were suppressed: {reasons[source]}."))

    ctx = _Context(usable, metrics, config, tuple(keywords))
    _, local_reason = _local_status(ctx)
    if local_reason:
        notes.append(ReviewerNote("info", "recommendations", "suppressed",
                                  f"The local SEO recommendation was not made: {local_reason}."))
    triggered = sorted((rec for build in BUILDERS if (rec := build(ctx)) is not None), key=lambda r: r.priority)
    standing = [_standing(k) for k in config.standing]

    chosen = triggered[:config.max_items]
    room = config.max_items - len(chosen)
    chosen += standing[:room]
    omitted = tuple(r.key for r in triggered[config.max_items:]) + tuple(r.key for r in standing[room:])
    if omitted:
        titles = w.join_list(r.title for r in triggered[config.max_items:] + standing[room:])
        notes.append(ReviewerNote("info", "recommendations", "suppressed",
                                  f"Omitted by the {config.max_items}-item limit: {titles}."))
    return RecommendationResult(tuple(chosen), omitted, tuple(notes))
