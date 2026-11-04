"""Rule-based insights and reviewer notes built from ReportMetrics.

Every insight carries the figures it states in `evidence`, and `untraced_numbers` checks
that no number in its text is missing from that evidence. Rules never state a claim the
metrics do not support: affected rules are skipped and a reviewer note explains why.

Keyword rankings (manual), Search Console figures and GA4 figures are described in
separate sections and are never blended into one claim.
"""

from dataclasses import dataclass
from enum import Enum

from seo_report import metrics as mt
from seo_report.insights import wording as w
from seo_report.metrics import Direction, ReportMetrics
from seo_report.periods import PERIOD_DAYS

SECTIONS = ("keywords", "search_console", "geography", "ga4")
TARGET_MARKET_SHARE = 0.70  # share of impressions at which the target market is called concentrated
ABOVE_10_LABEL = "Above 10"
MAX_NAMES = 3


class Tone(Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    OPPORTUNITY = "opportunity"


@dataclass(frozen=True)
class Insight:
    section: str
    key: str
    text: str
    tone: Tone
    priority: int  # lower comes first within a section
    evidence: dict
    tags: frozenset = frozenset()


@dataclass(frozen=True)
class ReviewerNote:
    """A message for the reviewer. Notes are never printed in the client PDF."""

    severity: str  # "info" or "warning"
    source: str  # "rankings", "gsc", "geography" or "ga4"
    category: str  # "suppressed", "data_quality", "diagnostic" or "limitation"
    message: str


@dataclass(frozen=True)
class RuleOutput:
    insights: tuple[Insight, ...] = ()
    notes: tuple[ReviewerNote, ...] = ()

    def __add__(self, other: "RuleOutput") -> "RuleOutput":
        return RuleOutput(self.insights + other.insights, self.notes + other.notes)


# --- evidence tracing -----------------------------------------------------------------------

def _normalized(token: str) -> str:
    return token.replace(",", "")


def evidence_tokens(evidence: dict) -> set[str]:
    """Every way a numeric evidence value may be written in text."""
    tokens = set()
    formatters = (w.format_count, w.format_compact, w.format_rate, w.format_position,
                  w.format_percent_change, w.format_points)
    for value in evidence.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        for fmt in formatters:
            tokens.add(_normalized(fmt(value)))
    return tokens


def untraced_numbers(insight: Insight) -> list[str]:
    """Numbers in the insight's text that are not derived from its evidence."""
    allowed = evidence_tokens(insight.evidence)
    return [n for n in w.numbers_in(insight.text) if n not in allowed]


# --- helpers ------------------------------------------------------------------------------------

def _insight(section, key, text, tone, priority, evidence, tags=()) -> Insight:
    return Insight(section, key, w.ensure_sentence(w.capitalize_first(text)), tone, priority,
                   evidence, frozenset(tags))


def _note(severity, source, category, message) -> ReviewerNote:
    return ReviewerNote(severity, source, category, message)


def _tone(change: mt.Change) -> Tone:
    return Tone.POSITIVE if change.favorable is True else Tone.NEUTRAL


def _is(n: int) -> str:
    return "is" if n == 1 else "are"


def _has(n: int) -> str:
    return "has" if n == 1 else "have"


def _name_list(names, noun: str, quote: bool = True, plural: str | None = None) -> tuple[str, int]:
    """List up to MAX_NAMES names, summarizing the rest. Returns (text, extra count)."""
    names = list(names)
    shown = [w.quoted(n) if quote else str(n) for n in names[:MAX_NAMES]]
    extra = len(names) - MAX_NAMES
    if extra > 0:
        shown.append(w.count_noun(extra, noun, plural))
    return w.join_list(shown), max(extra, 0)


def _previous(phrase: str | None) -> str:
    return f", {phrase} in the previous {PERIOD_DAYS} days" if phrase else ""


def _flag_notes(flags, source: str) -> list[ReviewerNote]:
    notes = []
    for f in flags:
        category = "data_quality"
        notes.append(_note("warning", source, category, f"{source.upper()} {f.period} period: {f.message}"))
    return notes


def _diagnostic_notes_gsc(label_period: str, diagnostics: dict) -> list[ReviewerNote]:
    notes = []
    for key, value in sorted(diagnostics.items()):
        if value == 0:
            continue
        name = key.replace("_diff", "").replace("_", " ")
        if key.startswith("query_") and value < 0:
            message = (f"Search Console {label_period} period: query rows fall short of the totals by {abs(value)} "
                       f"({name.split()[1]}); expected, because anonymized queries are not listed.")
            notes.append(_note("info", "gsc", "diagnostic", message))
        elif key.startswith("page_") and value > 0:
            message = (f"Search Console {label_period} period: page rows exceed the totals by {value} "
                       f"({name.split()[1]}); expected, because page-level aggregation counts impressions per page.")
            notes.append(_note("info", "gsc", "diagnostic", message))
        else:
            message = (f"Search Console {label_period} period: {name} differs from the totals by {value:+d}; "
                       "this was not expected.")
            notes.append(_note("warning", "gsc", "diagnostic", message))
    return notes


def _diagnostic_notes_ga4(label_period: str, diagnostics: dict) -> list[ReviewerNote]:
    notes = []
    for key, value in sorted(diagnostics.items()):
        if value:
            name = key.replace("_diff", "").replace("_", " ")
            notes.append(_note(
                "info", "ga4", "diagnostic",
                f"GA4 {label_period} period: {name} differs from the reference total by {value:+d}. "
                "Headline figures use the validated totals and were not adjusted."))
    return notes


# --- keywords (manual rankings) ----------------------------------------------------------------------

def keyword_rules(metrics: ReportMetrics) -> RuleOutput:
    r = metrics.rankings
    insights, notes = [], []
    if r.total == 0:
        notes.append(_note("warning", "rankings", "suppressed",
                           "No tracked keywords were found; keyword insights were skipped."))
        return RuleOutput((), tuple(notes))
    if r.all_current_missing:
        notes.append(_note("warning", "rankings", "suppressed",
                           f"Current rankings are not recorded for any of the {r.total} tracked keywords. "
                           "Keyword insights and movement claims were suppressed and the table shows “Not recorded”."))
        return RuleOutput((), tuple(notes))
    if r.missing_current:
        notes.append(_note("info", "rankings", "data_quality",
                           f"Current ranking is not recorded for {r.missing_current} of {r.total} tracked keywords."))
    if r.missing_previous or r.above_10_previous or r.above_10_current:
        notes.append(_note("info", "rankings", "data_quality",
                           f"Movement can be shown for {r.comparable} of {r.total} tracked keywords; the others have a "
                           "missing or “Above 10” value in the Previous or Current Ranking column."))

    best = r.best_keywords
    top1 = [k.keyword for k in best if k.position == 1]
    top3_rest = [k.keyword for k in best if 2 <= k.position <= 3]
    sentences, evidence = [], {"at_1": r.at_1, "top_3": r.top_3, "page_1": r.page_1, "total": r.total,
                               "top_rank": 1, "top_n": 3, "page_number": 1}
    if top1:
        names, extra = _name_list(top1, "other tracked keyword")
        sentences.append(f"The website ranks #1 for {names}")
        if extra:
            evidence["extra_top_1"] = extra
    if top3_rest:
        if top1:
            sentences.append(f"{w.count_noun(len(top3_rest), 'other tracked keyword')} "
                             f"{'ranks' if len(top3_rest) == 1 else 'rank'} in the top 3")
            evidence["other_top_3"] = len(top3_rest)
        else:
            names, extra = _name_list(top3_rest, "other tracked keyword")
            sentences.append(f"The website ranks in the top 3 for {names}")
            if extra:
                evidence["extra_top_3"] = extra
    if r.page_1 > r.top_3:
        sentences.append(f"Overall, {r.page_1} of the {r.total} tracked keywords rank on page 1")
    elif not sentences and r.page_1 > 0:
        sentences.append(f"{r.page_1} of the {r.total} tracked keywords rank on page 1")
    if sentences:
        insights.append(_insight("keywords", "keywords.top_positions", w.join_sentences(sentences),
                                 Tone.POSITIVE, 10, evidence, {"top_rankings"}))

    gains = [m for m in r.movements if m.change > 0]
    losses = [m for m in r.movements if m.change < 0]
    if gains:
        lead = gains[0]
        insights.append(_insight(
            "keywords", "keywords.moved_up",
            f"{w.count_noun(r.improved, 'tracked keyword')} {_has(r.improved)} moved up since the previous ranking "
            f"check, led by {w.quoted(lead.keyword)} (from position {lead.previous} to position {lead.current})",
            Tone.POSITIVE, 20,
            {"improved": r.improved, "previous": lead.previous, "current": lead.current, "change": lead.change},
            {"keyword_gains"}))
    if losses:
        lead = r.movements[-1]
        insights.append(_insight(
            "keywords", "keywords.moved_down",
            f"{w.count_noun(r.declined, 'tracked keyword')} {_has(r.declined)} moved down since the previous ranking "
            f"check, led by {w.quoted(lead.keyword)} (from position {lead.previous} to position {lead.current})",
            Tone.NEUTRAL, 30,
            {"declined": r.declined, "previous": lead.previous, "current": lead.current, "change": lead.change},
            {"keyword_declines"}))
    if r.comparable and not gains and not losses:
        insights.append(_insight(
            "keywords", "keywords.steady",
            f"{w.count_noun(r.comparable, 'tracked keyword ranking')} {'was' if r.comparable == 1 else 'were'} "
            "unchanged since the previous ranking check",
            Tone.NEUTRAL, 25, {"comparable": r.comparable}))
    if r.above_10_current:
        names, extra = _name_list(r.above_10_keywords, "other tracked keyword")
        evidence = {"above_10": r.above_10_current, "total": r.total}
        if extra:
            evidence["extra_names"] = extra
        insights.append(_insight(
            "keywords", "keywords.above_10",
            f"{w.count_noun(r.above_10_current, 'tracked keyword')} {_is(r.above_10_current)} currently labelled "
            f"{w.quoted(ABOVE_10_LABEL)}, including {names}, which leaves room to build visibility for those searches",
            Tone.OPPORTUNITY, 40, evidence, {"above_10"}))
    return RuleOutput(tuple(insights), tuple(notes))


# --- Search Console ----------------------------------------------------------------------------------------

def _query_suppression_note(g) -> list[ReviewerNote]:
    notes = []
    if g.current_empty:
        return notes
    if "query" in g.truncated_current:
        notes.append(_note("warning", "gsc", "suppressed",
                           "The current query list was truncated, so new-query, striking-distance and zero-click "
                           "query insights were suppressed."))
    elif g.new_queries_count is None and (g.previous_empty or "query" in g.truncated_previous):
        notes.append(_note("info", "gsc", "suppressed",
                           "New-query detection was suppressed because the previous period was empty or its query "
                           "list was truncated."))
    if "page" in g.truncated_current:
        notes.append(_note("warning", "gsc", "suppressed",
                           "The current page list was truncated, so zero-click page insights were suppressed."))
    return notes


def search_console_rules(metrics: ReportMetrics) -> RuleOutput:
    g = metrics.gsc
    th = metrics.thresholds
    if g is None:
        return RuleOutput((), (_note("warning", "gsc", "suppressed",
                                     "Search Console data was not provided; Search Console and geography insights "
                                     "were omitted."),))
    notes = _flag_notes(g.flags, "gsc")
    notes += _diagnostic_notes_gsc("current", g.diagnostics_current)
    notes += _diagnostic_notes_gsc("previous", g.diagnostics_previous)
    if g.current_empty:
        notes.append(_note("warning", "gsc", "suppressed",
                           "No Search Console data for the current period; Search Console insights were suppressed."))
        return RuleOutput((), tuple(notes))
    if g.previous_empty:
        notes.append(_note("warning", "gsc", "suppressed",
                           "No Search Console data for the previous period; comparisons were suppressed."))
    if g.incomplete_current:
        notes.append(_note("warning", "gsc", "data_quality",
                           "Search Console data for the end of the current period may be incomplete."))
    notes += _query_suppression_note(g)

    insights = []
    d = {"period_days": PERIOD_DAYS}

    clicks = g.clicks
    insights.append(_insight(
        "search_console", "gsc.clicks",
        f"The website earned {w.count_noun(clicks.current, 'organic click')} from Google Search during this "
        f"{PERIOD_DAYS}-day period{_previous(w.change_phrase(clicks))}",
        _tone(clicks), 10, {**d, **clicks.evidence()}, {"gsc_clicks"}))

    impressions = g.impressions
    insights.append(_insight(
        "search_console", "gsc.impressions",
        f"Search impressions totaled {w.format_compact(impressions.current)} during this {PERIOD_DAYS}-day period"
        f"{_previous(w.change_phrase(impressions, w.format_compact))}",
        _tone(impressions), 20, {**d, **impressions.evidence()}, {"gsc_impressions"}))

    ctr = g.ctr
    if ctr.current is not None:
        insights.append(_insight(
            "search_console", "gsc.ctr",
            f"The click-through rate was {w.format_rate(ctr.current)}{_previous(w.rate_change_phrase(ctr))}",
            _tone(ctr), 30, {**d, **ctr.evidence()}, {"gsc_ctr"}))

    position = g.position
    if position.current is not None:
        insights.append(_insight(
            "search_console", "gsc.position",
            f"The average search position was {w.format_position(position.current)}"
            f"{_previous(w.position_change_phrase(position))}",
            _tone(position), 40, {**d, **position.evidence()}, {"gsc_position"}))
        if position.current > 10:
            insights.append(_insight(
                "search_console", "gsc.position_opportunity",
                f"An average position of {w.format_position(position.current)} leaves clear room to climb toward "
                "the first page of results",
                Tone.OPPORTUNITY, 80, {"position": position.current}, {"low_position"}))

    if g.new_queries_count:
        names, extra = _name_list([q.label for q in g.new_queries], "other new search query", plural="other new search queries")
        evidence = {**d, "new_queries": g.new_queries_count}
        text = (f"{w.count_noun(g.new_queries_count, 'new search query', 'new search queries')} appeared in Google "
                f"Search compared with the previous {PERIOD_DAYS} days")
        if g.new_queries:
            text += f", including {names}"
            if extra:
                evidence["extra_names"] = extra
        insights.append(_insight("search_console", "gsc.new_queries", text, Tone.POSITIVE, 50, evidence,
                                 {"new_queries"}))

    if g.striking_queries_count:
        names, extra = _name_list([q.label for q in g.striking_queries], "other search query", plural="other search queries")
        low, high = th.striking_min_position, th.striking_max_position
        evidence = {"striking_queries": g.striking_queries_count, "position_min": low, "position_max": high}
        text = (f"{w.count_noun(g.striking_queries_count, 'search query', 'search queries')} "
                f"{_is(g.striking_queries_count)} showing in positions {w.format_count(low)}{w.EN_DASH}"
                f"{w.format_count(high)} of Google Search")
        if g.striking_queries:
            text += f", including {names}"
            if extra:
                evidence["extra_names"] = extra
        text += ", which makes them candidates for focused optimization"
        insights.append(_insight("search_console", "gsc.striking_distance", text, Tone.OPPORTUNITY, 60, evidence,
                                 {"striking_distance"}))

    if g.zero_click_queries_count:
        names, extra = _name_list([q.label for q in g.zero_click_queries], "other search query", plural="other search queries")
        evidence = {"zero_click_queries": g.zero_click_queries_count, "min_impressions": th.zero_click_min_impressions}
        text = (f"{w.count_noun(g.zero_click_queries_count, 'search query', 'search queries')} generated at least "
                f"{w.format_count(th.zero_click_min_impressions)} impressions without earning a click")
        if g.zero_click_queries:
            text += f", including {names}"
            if extra:
                evidence["extra_names"] = extra
        text += ", which points to an opportunity to improve titles and meta descriptions for those searches"
        insights.append(_insight("search_console", "gsc.zero_click_queries", text, Tone.OPPORTUNITY, 70, evidence,
                                 {"zero_click"}))

    if g.zero_click_pages_count:
        names, extra = _name_list([p.label for p in g.zero_click_pages], "other page")
        evidence = {"zero_click_pages": g.zero_click_pages_count, "min_impressions": th.zero_click_min_impressions}
        text = (f"{w.count_noun(g.zero_click_pages_count, 'page')} appeared in search results at least "
                f"{w.format_count(th.zero_click_min_impressions)} times without earning a click")
        if g.zero_click_pages:
            text += f", including {names}"
            if extra:
                evidence["extra_names"] = extra
        insights.append(_insight("search_console", "gsc.zero_click_pages", text, Tone.OPPORTUNITY, 75, evidence,
                                 {"zero_click"}))

    top = next((p for p in g.top_pages if p.label is not None and p.clicks > 0), None)
    if top is not None:
        insights.append(_insight(
            "search_console", "gsc.top_page",
            f"The page {w.quoted(top.label)} earned the most search clicks ({w.count_noun(top.clicks, 'click')}) "
            f"from {w.count_noun(top.impressions, 'impression', compact=True)}",
            Tone.NEUTRAL, 90, {"clicks": top.clicks, "impressions": top.impressions}))
    return RuleOutput(tuple(insights), tuple(notes))


# --- geography (country level, Search Console) ---------------------------------------------------------------

def geography_rules(metrics: ReportMetrics) -> RuleOutput:
    g = metrics.gsc
    if g is None or g.current_empty:
        return RuleOutput()
    notes = [_note("info", "geography", "limitation",
                   "Search Console reports geography at the country level only; city or metro performance is not "
                   "measured in this report.")]
    insights = []
    target = g.target_country
    if target is None:
        notes.append(_note("info", "geography", "suppressed",
                           "No target country is configured, so the target-market insight was skipped."))
    elif target.found is False:
        notes.append(_note("warning", "geography", "suppressed",
                           f"The target country “{target.name}” was not found among Search Console countries."))
    elif target.found is None:
        notes.append(_note("warning", "geography", "suppressed",
                           f"The country list was truncated and “{target.name}” is not in it; the target-market "
                           "insight was suppressed."))
    elif target.impressions_share is not None:
        concentrated = target.impressions_share >= TARGET_MARKET_SHARE
        text = (f"{target.name} accounted for {w.format_rate(target.impressions_share)} of search impressions "
                f"({w.count_noun(target.impressions, 'impression')}) and {w.count_noun(target.clicks, 'click')} "
                f"during this {PERIOD_DAYS}-day period")
        if concentrated:
            text += ", so search visibility is concentrated in the target market"
        insights.append(_insight(
            "geography", "geo.target_market", text, Tone.POSITIVE if concentrated else Tone.NEUTRAL, 10,
            {"share": target.impressions_share, "impressions": target.impressions, "clicks": target.clicks,
             "period_days": PERIOD_DAYS}, {"target_market"}))

    if g.other_countries_with_clicks:
        names, extra = _name_list([c.name for c in g.other_countries_with_clicks], "other country", quote=False)
        names = names.replace(", and ", " and ")  # this sentence is written without the serial comma
        evidence = {}
        text = f"The website also recorded clicks from several other countries, including {names}"
        if extra:
            evidence["extra_names"] = extra
        if g.country_count.current:
            text += f", while search impressions were recorded across {w.count_noun(g.country_count.current, 'country', 'countries')}"
            evidence["countries"] = g.country_count.current
        insights.append(_insight("geography", "geo.other_countries", text, Tone.NEUTRAL, 20, evidence,
                                 {"other_countries"}))
    return RuleOutput(tuple(insights), tuple(notes))


# --- GA4 ------------------------------------------------------------------------------------------------------------

def ga4_rules(metrics: ReportMetrics) -> RuleOutput:
    a = metrics.ga4
    th = metrics.thresholds
    if a is None:
        return RuleOutput((), (_note("warning", "ga4", "suppressed",
                                     "GA4 data was not provided; GA4 insights were omitted."),))
    notes = _flag_notes(a.flags, "ga4")
    notes += _diagnostic_notes_ga4("current", a.diagnostics_current)
    notes += _diagnostic_notes_ga4("previous", a.diagnostics_previous)
    if a.current_empty:
        notes.append(_note("warning", "ga4", "suppressed",
                           "No GA4 data for the current period; GA4 insights were suppressed."))
        return RuleOutput((), tuple(notes))
    if a.previous_empty:
        notes.append(_note("warning", "ga4", "suppressed",
                           "No GA4 data for the previous period; comparisons were suppressed."))
    if a.incomplete_current:
        notes.append(_note("warning", "ga4", "data_quality",
                           "GA4 data for the end of the current period may still be processing."))
    if a.engagement_rate.insufficient_volume or a.organic_engagement_rate.insufficient_volume \
            or a.engagement_gap.insufficient_volume or a.organic_share.insufficient_volume:
        notes.append(_note("info", "ga4", "suppressed",
                           f"Rate comparisons need at least {th.ga4_min_sessions} sessions in each period; those "
                           "with fewer sessions were not compared."))

    insights = []
    d = {"period_days": PERIOD_DAYS}

    s = a.sessions
    insights.append(_insight(
        "ga4", "ga4.sessions",
        f"The website recorded {w.count_noun(s.current, 'session')} during this {PERIOD_DAYS}-day period"
        f"{_previous(w.change_phrase(s))}", _tone(s), 10, {**d, **s.evidence()}, {"ga4_sessions"}))

    u = a.total_users
    insights.append(_insight(
        "ga4", "ga4.users",
        f"The site had {w.count_noun(u.current, 'total user')} during this {PERIOD_DAYS}-day period"
        f"{_previous(w.change_phrase(u))}", _tone(u), 15, {**d, **u.evidence()}, {"ga4_users"}))

    o = a.organic_sessions
    if o.current == 0:
        insights.append(_insight(
            "ga4", "ga4.organic",
            f"No sessions were attributed to Organic Search during this {PERIOD_DAYS}-day period",
            Tone.OPPORTUNITY, 20, {**d, **o.evidence()}, {"no_organic"}))
    else:
        insights.append(_insight(
            "ga4", "ga4.organic",
            f"Organic Search contributed {w.count_noun(o.current, 'session')} during this {PERIOD_DAYS}-day period"
            f"{_previous(w.change_phrase(o))}", _tone(o), 20, {**d, **o.evidence()}, {"ga4_organic"}))

    share = a.organic_share
    if share.comparable and share.direction is not Direction.FLAT:
        insights.append(_insight(
            "ga4", "ga4.organic_share",
            f"Organic Search accounted for {w.format_rate(share.current)} of all sessions"
            f"{_previous(w.rate_change_phrase(share))}", _tone(share), 25, {**d, **share.evidence()},
            {"organic_share"}))

    e = a.engagement_rate
    if e.current is not None:
        tags = {"ga4_engagement"} | ({"engagement_down"} if e.favorable is False else set())
        insights.append(_insight(
            "ga4", "ga4.engagement",
            f"The site-wide engagement rate was {w.format_rate(e.current)}{_previous(w.rate_change_phrase(e))}",
            _tone(e), 30, {**d, **e.evidence()}, tags))

    oe = a.organic_engagement_rate
    if oe.comparable:
        insights.append(_insight(
            "ga4", "ga4.organic_engagement",
            f"The Organic Search engagement rate was {w.format_rate(oe.current)}"
            f"{_previous(w.rate_change_phrase(oe))}", _tone(oe), 35, {**d, **oe.evidence()}, {"ga4_engagement"}))

    gap = a.engagement_gap
    if gap.comparable and gap.organic_higher is not None:
        evidence = {"organic_rate": gap.organic_rate, "site_rate": gap.site_rate}
        if gap.organic_higher:
            text = (f"Organic Search visitors were more engaged than the site average, with an engagement rate of "
                    f"{w.format_rate(gap.organic_rate)} versus {w.format_rate(gap.site_rate)}")
            insights.append(_insight("ga4", "ga4.engagement_gap", text, Tone.POSITIVE, 40, evidence,
                                     {"organic_engagement_higher"}))
        else:
            text = (f"The Organic Search engagement rate was {w.format_rate(gap.organic_rate)} versus "
                    f"{w.format_rate(gap.site_rate)} site-wide, an opportunity to align landing pages with search intent")
            insights.append(_insight("ga4", "ga4.engagement_gap", text, Tone.OPPORTUNITY, 40, evidence,
                                     {"organic_engagement_lower"}))

    c = a.organic_concentration
    if c is not None and c.concentrated:
        where = f"through {w.quoted(c.top_label)}" if c.top_label else "through the same landing page"
        insights.append(_insight(
            "ga4", "ga4.concentration",
            f"{c.top_sessions} of the {w.count_noun(c.organic_sessions, 'Organic Search session')} entered {where}, "
            "so organic traffic depends heavily on a single page",
            Tone.OPPORTUNITY, 50, {"top_sessions": c.top_sessions, "organic_sessions": c.organic_sessions},
            {"concentration"}))
    elif c is not None:
        pages = [p for p in a.top_organic_pages if p.label is not None][:MAX_NAMES]
        if pages:
            listed = w.join_list(f"{w.quoted(p.label)} ({w.count_noun(p.sessions, 'session')})" for p in pages)
            evidence = {f"sessions_{i}": p.sessions for i, p in enumerate(pages)}
            insights.append(_insight("ga4", "ga4.top_pages", f"The top Organic Search landing pages were {listed}",
                                     Tone.NEUTRAL, 60, evidence))

    ke = a.key_events
    if ke.status == "none_recorded":
        insights.append(_insight(
            "ga4", "ga4.key_events",
            f"No key events were recorded during this {PERIOD_DAYS}-day period or the previous {PERIOD_DAYS} days, "
            "so the report cannot yet show how many visits lead to inquiries",
            Tone.OPPORTUNITY, 70, dict(d), {"no_key_events"}))
    elif ke.status == "recorded" and ke.current is not None:
        change = mt.compare_count(ke.current, ke.previous, th)
        insights.append(_insight(
            "ga4", "ga4.key_events",
            f"The site recorded {w.count_noun(ke.current, 'key event')} during this {PERIOD_DAYS}-day period"
            f"{_previous(w.change_phrase(change))}", _tone(change), 70, {**d, **change.evidence()}, {"key_events"}))
    else:
        notes.append(_note("info", "ga4", "suppressed", "Key events are unavailable for this property; the key-event "
                                                         "insight was skipped."))
    return RuleOutput(tuple(insights), tuple(notes))


# --- entry point ------------------------------------------------------------------------------------------------------

RULES = (keyword_rules, search_console_rules, geography_rules, ga4_rules)


def run_rules(metrics: ReportMetrics) -> RuleOutput:
    """Run every rule and return insights in a deterministic order (section, priority, key)."""
    output = RuleOutput()
    for rule in RULES:
        output = output + rule(metrics)
    order = {name: i for i, name in enumerate(SECTIONS)}
    insights = sorted(output.insights, key=lambda i: (order[i.section], i.priority, i.key))
    return RuleOutput(tuple(insights), output.notes)
